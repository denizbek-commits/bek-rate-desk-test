"""
Bek Rate Desk — P&L Attribution Engine
========================================
Günlük P&L'yi faktörlere ayırır:

  1. Carry       — Kupon tahakkuku (accrual)
  2. Duration    — Faiz oranı hareketi × DV01
  3. Curve Shift — Eğri şekli değişimi (paralel olmayan hareket)
  4. FX Delta    — Döviz pozisyonu × kur değişimi
  5. Spread      — Kredi/OAS spread değişimi
  6. Theta       — Zaman değeri kaybı (opsiyonlar için)
  7. Artık       — Açıklanamayan (model hatası veya işlem P&L)

Anlık fiyat snapshot → data/pnl_snapshots.json
"""

import os, json, math
from utils.io_utils import _atomic_json_write
from datetime import datetime, date, timedelta
from typing import Optional

SCRIPT_DIR     = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR       = os.path.join(SCRIPT_DIR, "data")
SNAPSHOT_FILE  = os.path.join(DATA_DIR, "pnl_snapshots.json")
PNL_HIST_FILE  = os.path.join(DATA_DIR, "pnl_history.json")

os.makedirs(DATA_DIR, exist_ok=True)


# ──────────────────────────────────────────────────────────────────────────────
# Snapshot yönetimi
# ──────────────────────────────────────────────────────────────────────────────

def _load_snapshots() -> list:
    if os.path.exists(SNAPSHOT_FILE):
        try:
            return json.load(open(SNAPSHOT_FILE))
        except Exception:
            pass
    return []


def _save_snapshots(snaps: list):
    # Son 30 snapshot tut
    snaps = snaps[-30:]
    _atomic_json_write(SNAPSHOT_FILE, snaps)


def take_snapshot(label: str = "auto") -> dict:
    """
    Mevcut portföy ve piyasa fiyatlarını kaydeder.
    Her gün EOD veya talep üzerine çağrılır.
    """
    try:
        from utils.dealer_analytics_engine import load_portfolio, run_portfolio_oas
        from utils.oas_engine import fetch_treasury_curve
        from utils.tcmb_evds import fetch_tr_yield_curve_evds, fetch_fx_rates_tcmb

        positions = load_portfolio()
        oas_data  = run_portfolio_oas(positions)
        us_curve  = fetch_treasury_curve()
        tr_curve  = fetch_tr_yield_curve_evds()
        fx_rates  = fetch_fx_rates_tcmb()

        # TR 2Y ve 10Y
        tr_pts = {p["tenor"]: p["yield"] for p in tr_curve.get("curve", [])}

        snap = {
            "timestamp":       datetime.now().isoformat(),
            "date":            date.today().isoformat(),
            "label":           label,
            "portfolio": {
                "total_mv_m":      oas_data.get("total_market_value_m", 0.0),
                "total_par_m":     oas_data.get("total_par_m", 0.0),
                "net_dv01_m":      oas_data.get("total_net_dv01", 0.0),
                "avg_duration":    oas_data.get("portfolio_avg_dur", 0.0),
                "position_count":  oas_data.get("position_count", 0),
            },
            "positions": [
                {
                    "id":         p.get("id"),
                    "name":       p.get("name"),
                    "side":       p.get("side"),
                    "par_m":      p.get("par_m"),
                    "price":      p.get("market_price_pct"),
                    "coupon_pct": p.get("coupon_pct"),
                    "maturity_y": p.get("maturity_years"),
                    "ytm_pct":    next((x.get("ytm_pct",0) for x in oas_data.get("positions",[])
                                        if x.get("id")==p.get("id")), 0.0),
                    "mod_dur":    next((x.get("mod_duration",0) for x in oas_data.get("positions",[])
                                        if x.get("id")==p.get("id")), 0.0),
                    "dv01_m":     next((x.get("dv01_portfolio_m",0) for x in oas_data.get("positions",[])
                                        if x.get("id")==p.get("id")), 0.0),
                }
                for p in positions
            ],
            "us_curve":    {k: round(v * 100, 3) for k, v in us_curve.items()},
            "tr_curve":    tr_pts,
            "fx_rates":    fx_rates.get("rates", {}),
        }

        snaps = _load_snapshots()
        snaps.append(snap)
        _save_snapshots(snaps)
        return {"status": "ok", "snapshot": snap}

    except Exception as e:
        return {"status": "error", "message": str(e)}


def get_latest_snapshot() -> Optional[dict]:
    snaps = _load_snapshots()
    return snaps[-1] if snaps else None


def get_previous_snapshot() -> Optional[dict]:
    snaps = _load_snapshots()
    if len(snaps) >= 2:
        return snaps[-2]
    return None


# ──────────────────────────────────────────────────────────────────────────────
# P&L attribution hesabı
# ──────────────────────────────────────────────────────────────────────────────

def _interp(curve_dict: dict, tenor_y: float) -> Optional[float]:
    """Tenor bazında eğri interpolasyonu."""
    tenor_map = {
        "1MO": 1/12, "3MO": 0.25, "6MO": 0.5,
        "1Y": 1, "2Y": 2, "3Y": 3, "5Y": 5, "7Y": 7, "10Y": 10, "20Y": 20, "30Y": 30,
        "1M": 1/12, "91D": 0.25, "182D": 0.5,
    }
    pts = []
    for k, v in curve_dict.items():
        ky = tenor_map.get(str(k).upper(), None)
        if ky is None:
            try:
                s = str(k).upper()
                ky = float(s.replace("Y","")) if "Y" in s else float(s.replace("M",""))/12
            except Exception:
                continue
        try:
            pts.append((float(ky), float(v)))
        except Exception:
            continue
    if not pts:
        return None
    pts.sort()
    if tenor_y <= pts[0][0]:  return pts[0][1]
    if tenor_y >= pts[-1][0]: return pts[-1][1]
    for i in range(len(pts)-1):
        t0, y0 = pts[i]; t1, y1 = pts[i+1]
        if t0 <= tenor_y <= t1:
            return y0 + (y1-y0)*(tenor_y-t0)/(t1-t0)
    return pts[-1][1]


def calculate_attribution(current_snap: dict = None,
                           previous_snap: dict = None,
                           holding_days: float = 1.0) -> dict:
    """
    İki snapshot arası P&L faktör ayrıştırması.
    """
    if current_snap is None:
        current_snap = get_latest_snapshot()
    if previous_snap is None:
        previous_snap = get_previous_snapshot()

    # Snapshot yoksa simüle et
    if current_snap is None or previous_snap is None:
        return _simulated_attribution()

    holding_y = holding_days / 365.0
    results   = []
    total_carry   = 0.0
    total_duration = 0.0
    total_curve   = 0.0
    total_fx      = 0.0
    total_residual = 0.0

    prev_positions = {p["id"]: p for p in previous_snap.get("positions", [])}
    curr_positions = {p["id"]: p for p in current_snap.get("positions", [])}

    prev_us = previous_snap.get("us_curve", {})
    curr_us = current_snap.get("us_curve", {})
    prev_tr = previous_snap.get("tr_curve", {})
    curr_tr = current_snap.get("tr_curve", {})
    prev_fx = previous_snap.get("fx_rates", {})
    curr_fx = current_snap.get("fx_rates", {})

    # Paralel faiz hareketi (2Y-10Y ortalaması)
    us_2y_chg  = (curr_us.get("2Y", 0) - prev_us.get("2Y", 0)) if prev_us.get("2Y") else 0
    us_10y_chg = (curr_us.get("10Y",0) - prev_us.get("10Y",0)) if prev_us.get("10Y") else 0
    tr_2y_chg  = (_interp(curr_tr, 2) or 0) - (_interp(prev_tr, 2) or 0)
    tr_10y_chg = (_interp(curr_tr,10) or 0) - (_interp(prev_tr,10) or 0)

    for pos_id, curr_p in curr_positions.items():
        prev_p = prev_positions.get(pos_id, curr_p)

        par_m  = float(curr_p.get("par_m", 0))
        sign   = 1.0 if curr_p.get("side","asset") == "asset" else -1.0
        dv01_m = abs(float(curr_p.get("dv01_m", 0)))
        dur    = abs(float(curr_p.get("mod_dur", 0)))
        cp     = float(curr_p.get("coupon_pct", 0))
        ccy    = "TRY"  # tüm pozisyonlar TRY varsayım; EUR/USD için genişletilebilir

        # 1. Carry (kupon tahakkuku)
        carry_m = sign * (cp / 100) * par_m * holding_y
        total_carry += carry_m

        # 2. Duration P&L — ilgili eğri hareketi
        mat_y = float(curr_p.get("maturity_y", 2))
        if mat_y >= 2:
            rate_chg_bps = tr_10y_chg * 100 if mat_y >= 7 else tr_2y_chg * 100
        else:
            rate_chg_bps = tr_2y_chg * 100

        dur_pnl = -sign * dv01_m * rate_chg_bps  # negatif çünkü faiz artınca fiyat düşer
        total_duration += dur_pnl

        # 3. Curve (eğim değişimi = steepening/flattening)
        slope_chg = (tr_10y_chg - tr_2y_chg) * 100  # bps
        # Uzun vade pozisyonları eğim değişimine daha duyarlı
        curve_sensitivity = sign * dv01_m * slope_chg * (mat_y - 2) / 8.0
        total_curve += curve_sensitivity

        results.append({
            "id":       pos_id,
            "name":     curr_p.get("name", pos_id),
            "carry_m":  round(carry_m, 4),
            "dur_m":    round(dur_pnl, 4),
            "curve_m":  round(curve_sensitivity, 4),
        })

    # 4. FX Delta — USDTRY, EURTRY pozisyon P&L
    fx_pnl = 0.0
    usd_pos_m = _get_fx_position("USD")
    eur_pos_m = _get_fx_position("EUR")
    usdtry_chg = (curr_fx.get("USDTRY", 0) - prev_fx.get("USDTRY", 0)) if prev_fx.get("USDTRY") else 0
    eurtry_chg = (curr_fx.get("EURTRY", 0) - prev_fx.get("EURTRY", 0)) if prev_fx.get("EURTRY") else 0
    fx_pnl = usd_pos_m * usdtry_chg + eur_pos_m * eurtry_chg
    total_fx = fx_pnl

    # 5. Toplam açıklanan P&L
    explained = total_carry + total_duration + total_curve + total_fx

    # Piyasa değeri değişimi (gerçekleşen P&L)
    prev_mv = previous_snap.get("portfolio", {}).get("total_mv_m", 0.0)
    curr_mv = current_snap.get("portfolio", {}).get("total_mv_m", 0.0)
    actual_pnl = curr_mv - prev_mv + total_carry  # carry zaten değere yansımış

    total_residual = actual_pnl - explained

    # Faiz değişim özeti
    rate_moves = {}
    if prev_us.get("2Y") and curr_us.get("2Y"):
        rate_moves["UST 2Y"] = round(us_2y_chg, 3)
    if prev_us.get("10Y") and curr_us.get("10Y"):
        rate_moves["UST 10Y"] = round(us_10y_chg, 3)
    if tr_2y_chg:
        rate_moves["TR 2Y"] = round(tr_2y_chg, 3)
    if tr_10y_chg:
        rate_moves["TR 10Y"] = round(tr_10y_chg, 3)
    if usdtry_chg:
        rate_moves["USDTRY"] = round(usdtry_chg, 4)

    return {
        "attribution": {
            "carry_m":    round(total_carry, 3),
            "duration_m": round(total_duration, 3),
            "curve_m":    round(total_curve, 3),
            "fx_m":       round(total_fx, 3),
            "explained_m":round(explained, 3),
            "residual_m": round(total_residual, 3),
            "total_m":    round(actual_pnl, 3),
        },
        "positions":    results,
        "rate_moves":   rate_moves,
        "from_date":    previous_snap.get("date", "—"),
        "to_date":      current_snap.get("date", "—"),
        "from_label":   previous_snap.get("label", "önceki"),
        "to_label":     current_snap.get("label", "güncel"),
        "holding_days": holding_days,
        "has_data":     True,
    }


def _get_fx_position(ccy: str) -> float:
    """FX pozisyonu — data/fx_position.json varsa okur."""
    try:
        fp = os.path.join(DATA_DIR, "fx_position.json")
        if os.path.exists(fp):
            d = json.load(open(fp))
            return float(d.get(ccy, 0.0))
    except Exception:
        pass
    return 0.0


def _simulated_attribution() -> dict:
    """Snapshot yoksa örnek verilerle simüle edilmiş attribution."""
    return {
        "attribution": {
            "carry_m":    12.5,
            "duration_m": -4.2,
            "curve_m":    1.8,
            "fx_m":       0.0,
            "explained_m":10.1,
            "residual_m": 0.3,
            "total_m":    10.4,
        },
        "positions": [],
        "rate_moves": {"TR 2Y": 0.15, "TR 10Y": -0.05, "UST 10Y": 0.03},
        "from_date":  (date.today() - timedelta(days=1)).isoformat(),
        "to_date":    date.today().isoformat(),
        "from_label": "Dün EOD",
        "to_label":   "Bugün",
        "holding_days": 1.0,
        "has_data":   False,
        "note":       "Snapshot bulunamadı — örnek veriler gösteriliyor. EOD Brief çalıştırıldığında snapshot otomatik alınır.",
    }


# ──────────────────────────────────────────────────────────────────────────────
# P&L tarihçesi
# ──────────────────────────────────────────────────────────────────────────────

def load_pnl_history() -> list:
    if os.path.exists(PNL_HIST_FILE):
        try:
            return json.load(open(PNL_HIST_FILE))
        except Exception:
            pass
    return []


def append_pnl_history(attribution_result: dict):
    hist = load_pnl_history()
    hist.append({
        "date":        attribution_result.get("to_date"),
        "carry_m":     attribution_result["attribution"]["carry_m"],
        "duration_m":  attribution_result["attribution"]["duration_m"],
        "curve_m":     attribution_result["attribution"]["curve_m"],
        "fx_m":        attribution_result["attribution"]["fx_m"],
        "total_m":     attribution_result["attribution"]["total_m"],
    })
    hist = hist[-60:]  # Son 60 gün
    _atomic_json_write(PNL_HIST_FILE, hist)

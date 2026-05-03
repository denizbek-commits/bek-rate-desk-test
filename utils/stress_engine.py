"""
Bek Rate Desk — Historical Stress Library
==========================================
Türkiye'ye özgü tarihsel stres senaryoları ile portföy etkisi hesabı.

Her senaryo gerçekleşen piyasa hareketlerinden kalibre edilmiştir:
  - Ağustos 2018: TRY döviz krizi
  - Mart 2021: TCMB başkanı değişikliği
  - COVID Mart 2020: Küresel likidite sıkışması
  - Mayıs 2013: Fed Taper Tantrum (EM geneli)
  - 2022 Fed sıkılaşma döngüsü
  - Haziran 2023: Seçim sonrası normalleşme
  - Kullanıcı tanımlı özel senaryo
"""

import os, json, math
from datetime import datetime
from typing import Optional
from utils.io_utils import _atomic_json_write

SCRIPT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR   = os.path.join(SCRIPT_DIR, "data")
CUSTOM_SCENARIOS_FILE = os.path.join(DATA_DIR, "custom_stress_scenarios.json")

# ──────────────────────────────────────────────────────────────────────────────
# Kalibre Edilmiş Tarihsel Senaryo Kütüphanesi
# ──────────────────────────────────────────────────────────────────────────────
HISTORICAL_SCENARIOS = [
    {
        "id":          "aug2018",
        "name":        "Ağustos 2018 — TRY Krizi",
        "date":        "2018-08-10",
        "duration_days": 14,
        "category":    "FX Krizi",
        "description": "ABD yaptırımları ve cari açık endişeleri ile USDTRY %40 değer kaybetti. TCMB acil faiz artışı yaparak %24'e yükseltti. 10Y DİBS faizi 300bps arttı.",
        "shocks": {
            "tr_rate_parallel_bps":  300,   # TR faiz kısa vadeli artış
            "tr_2y_bps":             400,   # 2Y en çok etkilendi
            "tr_10y_bps":            300,
            "us_rate_parallel_bps":  -10,
            "usdtry_pct":            40.0,  # % değer kaybı
            "eurtry_pct":            38.0,
            "credit_spread_bps":     250,
            "equity_pct":           -25.0,
        },
        "color": "#ef4444",
    },
    {
        "id":          "mar2021",
        "name":        "Mart 2021 — TCMB Başkanı Değişikliği",
        "date":        "2021-03-20",
        "duration_days": 3,
        "category":    "Politik Risk",
        "description": "TCMB Başkanı Ağbal ani olarak görevden alındı. USDTRY bir haftada %15 değer kaybetti, 10Y DİBS faizi 200bps arttı. Enflasyon baskısı ivmelendi.",
        "shocks": {
            "tr_rate_parallel_bps":  200,
            "tr_2y_bps":             250,
            "tr_10y_bps":            200,
            "us_rate_parallel_bps":  5,
            "usdtry_pct":            15.0,
            "eurtry_pct":            13.0,
            "credit_spread_bps":     150,
            "equity_pct":           -18.0,
        },
        "color": "#f59e0b",
    },
    {
        "id":          "covid2020",
        "name":        "Mart 2020 — COVID Likidite Krizi",
        "date":        "2020-03-16",
        "duration_days": 10,
        "category":    "Küresel Şok",
        "description": "Fed sıfır faize indi, EM'den 85 milyar dolar çıktı. USDTRY %12 değer kaybetti. ABD 10Y 60bps düştü (uçuş kalitesi), TR spread 180bps açıldı.",
        "shocks": {
            "tr_rate_parallel_bps":  50,
            "tr_2y_bps":             80,
            "tr_10y_bps":            50,
            "us_rate_parallel_bps":  -60,
            "usdtry_pct":            12.0,
            "eurtry_pct":            8.0,
            "credit_spread_bps":     180,
            "equity_pct":           -30.0,
        },
        "color": "#8b5cf6",
    },
    {
        "id":          "taper2013",
        "name":        "Mayıs 2013 — Fed Taper Tantrum",
        "date":        "2013-05-22",
        "duration_days": 60,
        "category":    "EM Geneli",
        "description": "Fed QE azaltımı sinyali verdi. EM'den kapsamlı çıkış. TR 10Y +400bps, USDTRY %25 değer kaybı. TR'nin cari açığı kırılganlığı artırdı.",
        "shocks": {
            "tr_rate_parallel_bps":  350,
            "tr_2y_bps":             300,
            "tr_10y_bps":            400,
            "us_rate_parallel_bps":  100,
            "usdtry_pct":            25.0,
            "eurtry_pct":            20.0,
            "credit_spread_bps":     200,
            "equity_pct":           -20.0,
        },
        "color": "#06b6d4",
    },
    {
        "id":          "fed2022",
        "name":        "2022 — Fed Sıkılaşma Döngüsü",
        "date":        "2022-03-16",
        "duration_days": 270,
        "category":    "Faiz Döngüsü",
        "description": "Fed 425bps faiz artışı yaptı. UST 2Y 350bps yükseldi. TR TL enflasyon ortamında politika divergence gösterdi; nominal kurda %45 değer kaybı.",
        "shocks": {
            "tr_rate_parallel_bps":  -200,  # TR ters yön gitti (faiz düşürdü)
            "tr_2y_bps":             -150,
            "tr_10y_bps":            -100,
            "us_rate_parallel_bps":  350,
            "usdtry_pct":            45.0,
            "eurtry_pct":            35.0,
            "credit_spread_bps":     100,
            "equity_pct":           -10.0,
        },
        "color": "#10b981",
    },
    {
        "id":          "election2023",
        "name":        "Haziran 2023 — Seçim Sonrası Normalleşme",
        "date":        "2023-06-01",
        "duration_days": 90,
        "category":    "Politik Geçiş",
        "description": "Seçim sonrası ortodoks para politikasına dönüş. TCMB 6 ayda 3650bps faiz artırdı. TL 100→% değer kaybı yavaşladı, reel faiz pozitife döndü.",
        "shocks": {
            "tr_rate_parallel_bps":  1500,  # hızlı normalleşme
            "tr_2y_bps":             1800,
            "tr_10y_bps":            1200,
            "us_rate_parallel_bps":  20,
            "usdtry_pct":            8.0,
            "eurtry_pct":            6.0,
            "credit_spread_bps":     -100,  # CDS daraldı
            "equity_pct":            5.0,
        },
        "color": "#2563eb",
    },
]


def load_custom_scenarios() -> list:
    if os.path.exists(CUSTOM_SCENARIOS_FILE):
        try:
            return json.load(open(CUSTOM_SCENARIOS_FILE))
        except Exception:
            pass
    return []


def save_custom_scenario(scenario: dict) -> dict:
    os.makedirs(DATA_DIR, exist_ok=True)
    scenarios = load_custom_scenarios()
    # Güncelle veya ekle
    idx = next((i for i, s in enumerate(scenarios) if s.get("id") == scenario.get("id")), None)
    if idx is not None:
        scenarios[idx] = scenario
    else:
        scenario["id"] = f"custom_{len(scenarios)+1}"
        scenarios.append(scenario)
    _atomic_json_write(CUSTOM_SCENARIOS_FILE, scenarios)
    return {"status": "saved", "id": scenario["id"]}


def get_all_scenarios() -> list:
    return HISTORICAL_SCENARIOS + load_custom_scenarios()


# ──────────────────────────────────────────────────────────────────────────────
# Stres hesabı
# ──────────────────────────────────────────────────────────────────────────────

def _price_at_ytm(maturity_y: float, coupon_pct: float, ytm_pct: float,
                   face: float = 100.0, freq: int = 2) -> float:
    n = max(1, round(maturity_y * freq))
    c = face * (coupon_pct / 100) / freq
    r = (ytm_pct / 100) / freq
    if abs(r) < 1e-10:
        return face + c * n
    return sum(c / (1+r)**t for t in range(1, n+1)) + face / (1+r)**n


def apply_scenario_to_portfolio(scenario_id: str, custom_shocks: dict = None) -> dict:
    """
    Verilen senaryonun şoklarını mevcut portföye uygular.
    Döndürür: her pozisyon için fiyat etkisi, toplam P&L etkisi.
    """
    # Senaryo bul
    scenario = None
    for s in get_all_scenarios():
        if s["id"] == scenario_id:
            scenario = s
            break
    if scenario is None:
        return {"error": f"Senaryo bulunamadı: {scenario_id}"}

    shocks = custom_shocks if custom_shocks else scenario["shocks"]

    try:
        from utils.dealer_analytics_engine import load_portfolio, run_portfolio_oas
        positions = load_portfolio()
        oas_data  = run_portfolio_oas(positions)
    except Exception as e:
        return {"error": str(e)}

    pos_map = {p["id"]: p for p in oas_data.get("positions", [])}
    results = []
    total_base_mv   = 0.0
    total_stress_mv = 0.0
    total_dv01_impact = 0.0

    for pos in positions:
        pid      = pos.get("id")
        name     = pos.get("name", pid)
        par_m    = float(pos.get("par_m", 0))
        cp       = float(pos.get("coupon_pct", 0))
        mat_y    = float(pos.get("maturity_years", 2))
        price    = float(pos.get("market_price_pct", 100))
        side     = pos.get("side", "asset")
        sign     = 1.0 if side == "asset" else -1.0
        freq     = int(pos.get("freq", 2))

        # Mevcut YTM
        pd = pos_map.get(pid, {})
        ytm  = float(pd.get("ytm_pct", cp))
        dv01 = float(pd.get("dv01_portfolio_m", 0))

        # TR faiz şoku seçimi (vadeye göre)
        if mat_y <= 1.5:
            rate_shock = shocks.get("tr_2y_bps", shocks.get("tr_rate_parallel_bps", 0))
        elif mat_y <= 6:
            # Interpolasyon
            w = (mat_y - 1.5) / (10.0 - 1.5)
            rate_shock = (shocks.get("tr_2y_bps", 0) * (1-w) +
                          shocks.get("tr_10y_bps", 0) * w)
        else:
            rate_shock = shocks.get("tr_10y_bps", shocks.get("tr_rate_parallel_bps", 0))

        # Stres fiyatı
        stressed_ytm   = max(0.1, ytm + rate_shock / 100.0)
        base_price     = _price_at_ytm(mat_y, cp, ytm,          100.0, freq)
        stressed_price = _price_at_ytm(mat_y, cp, stressed_ytm, 100.0, freq)

        base_mv     = sign * par_m * base_price / 100
        stressed_mv = sign * par_m * stressed_price / 100
        price_impact = stressed_mv - base_mv

        # DV01 yaklaşım
        dv01_impact = sign * dv01 * rate_shock  # M TL

        total_base_mv   += base_mv
        total_stress_mv += stressed_mv
        total_dv01_impact += dv01_impact

        results.append({
            "id":             pid,
            "name":           name,
            "side":           side,
            "par_m":          par_m,
            "base_ytm_pct":   round(ytm, 3),
            "rate_shock_bps": round(rate_shock, 0),
            "stressed_ytm":   round(stressed_ytm, 3),
            "base_price":     round(base_price, 3),
            "stressed_price": round(stressed_price, 3),
            "base_mv_m":      round(base_mv, 3),
            "stressed_mv_m":  round(stressed_mv, 3),
            "price_impact_m": round(price_impact, 3),
            "impact_pct":     round((stressed_price - base_price) / base_price * 100, 2),
        })

    total_pnl_m    = total_stress_mv - total_base_mv
    total_pnl_pct  = (total_pnl_m / abs(total_base_mv) * 100) if total_base_mv else 0

    # FX etkisi
    fx_impact_m = 0.0
    try:
        from utils.stress_engine import _fx_pnl
        fx_impact_m = _fx_pnl(shocks)
    except Exception:
        pass

    return {
        "scenario":        scenario,
        "positions":       results,
        "summary": {
            "base_mv_m":       round(total_base_mv, 3),
            "stressed_mv_m":   round(total_stress_mv, 3),
            "total_pnl_m":     round(total_pnl_m, 3),
            "total_pnl_pct":   round(total_pnl_pct, 2),
            "fx_impact_m":     round(fx_impact_m, 3),
            "combined_pnl_m":  round(total_pnl_m + fx_impact_m, 3),
        },
        "shocks_applied":  shocks,
        "as_of":           datetime.now().strftime("%Y-%m-%dT%H:%M:%S"),
    }


def _fx_pnl(shocks: dict) -> float:
    """FX pozisyon P&L tahmini."""
    try:
        from utils.pnl_attribution_engine import _get_fx_position
        usd_pos = _get_fx_position("USD")
        eur_pos = _get_fx_position("EUR")
        usd_chg = shocks.get("usdtry_pct", 0) / 100
        eur_chg = shocks.get("eurtry_pct", 0) / 100
        # Uzun USD → TRY değer kaybı → USD pozisyonu değer kazanır (TRY bakiyeden)
        # Net FX P&L için: pozisyon (döviz tutarı M) × kur değişimi
        # Kısa TRY pozisyonu için pozitif
        try:
            import requests
            spot = 38.5
            try:
                from utils.tcmb_evds import fetch_fx_rates_tcmb
                rates = fetch_fx_rates_tcmb().get("rates", {})
                spot = rates.get("USDTRY", 38.5)
            except Exception:
                pass
        except Exception:
            spot = 38.5
        # FX P&L (TRY cinsinden M)
        return usd_pos * spot * usd_chg + eur_pos * spot * eur_chg
    except Exception:
        return 0.0


def run_all_scenarios() -> list:
    """Tüm senaryoları tek seferde çalıştırır — özet karşılaştırma için."""
    results = []
    for s in HISTORICAL_SCENARIOS:
        try:
            r = apply_scenario_to_portfolio(s["id"])
            results.append({
                "id":          s["id"],
                "name":        s["name"],
                "category":    s["category"],
                "color":       s["color"],
                "total_pnl_m": r["summary"]["total_pnl_m"],
                "pnl_pct":     r["summary"]["total_pnl_pct"],
                "usdtry_pct":  s["shocks"].get("usdtry_pct", 0),
                "tr_rate_bps": s["shocks"].get("tr_rate_parallel_bps", 0),
            })
        except Exception as e:
            results.append({"id": s["id"], "name": s["name"], "error": str(e)})
    return results

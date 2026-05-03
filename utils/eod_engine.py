"""
Bek Rate Desk — EOD (Gün Sonu) Brief Engine
=============================================
Günün kapanışında otomatik üretilen özet:
  - Bugün ne oldu (faiz, FX, spread hareketleri)
  - Portföy durumu (pozisyon özeti, DV01, duration)
  - Limit kullanımı
  - Yarın dikkat edilecekler (ekonomi takvimi, vadeler, call tarihleri)
  - P&L attribution özeti
  - Açık aksiyonlar
"""

import os, json, math
from utils.io_utils import _atomic_json_write
from datetime import datetime, date, timedelta
from typing import Optional

SCRIPT_DIR  = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR    = os.path.join(SCRIPT_DIR, "data")
EOD_CACHE   = os.path.join(DATA_DIR, "eod_cache.json")

os.makedirs(DATA_DIR, exist_ok=True)


def _safe(fn, default=None):
    try:
        return fn()
    except Exception:
        return default


def _pct_change_label(val: float, unit: str = "bps", invert: bool = False) -> str:
    """Değişimi formatlı ve yönlü döndürür."""
    sign = "+" if val >= 0 else ""
    arrow = "↑" if (val > 0) != invert else "↓"
    return f"{arrow} {sign}{val:.1f} {unit}"


def _fetch_market_snapshot() -> dict:
    """Canlı piyasa verilerini toplar."""
    from utils.oas_engine import fetch_treasury_curve
    from utils.tcmb_evds import fetch_policy_rates, fetch_tr_yield_curve_evds, fetch_fx_rates_tcmb, fetch_tr_macro

    us_curve   = _safe(fetch_treasury_curve, {})
    tr_data    = _safe(fetch_tr_yield_curve_evds, {"curve": []})
    policy     = _safe(fetch_policy_rates, {"lending_rate": 42.5})
    fx_data    = _safe(fetch_fx_rates_tcmb, {"rates": {}})
    macro      = _safe(fetch_tr_macro, {})

    tr_pts     = {p["tenor"]: p["yield"] for p in tr_data.get("curve", [])}

    return {
        "us_curve":    {k: round(v * 100, 3) for k, v in us_curve.items()},
        "tr_curve":    tr_pts,
        "policy":      policy,
        "fx":          fx_data.get("rates", {}),
        "macro":       macro,
        "timestamp":   datetime.now().isoformat(),
    }


def _compare_curves(curr: dict, prev: dict) -> dict:
    """İki eğri arasındaki değişimleri hesaplar."""
    changes = {}
    for key in set(list(curr.keys()) + list(prev.keys())):
        if key in curr and key in prev:
            try:
                changes[key] = round(float(curr[key]) - float(prev[key]), 3)
            except Exception:
                pass
    return changes


def _get_prev_snapshot() -> Optional[dict]:
    """Dünün EOD snapshot'ını veya P&L snapshot'ını yükler."""
    from utils.pnl_attribution_engine import get_previous_snapshot, get_latest_snapshot
    snap = _safe(get_previous_snapshot)
    if snap:
        return {
            "us_curve": snap.get("us_curve", {}),
            "tr_curve": snap.get("tr_curve", {}),
            "fx":       snap.get("fx_rates", {}),
        }
    return None


def _get_portfolio_summary() -> dict:
    """Portföy özet metriklerini toplar."""
    from utils.dealer_analytics_engine import load_portfolio, run_portfolio_oas, build_risk_ladder
    positions = load_portfolio()
    oas       = run_portfolio_oas(positions)
    ladder    = build_risk_ladder(positions)

    # Vadesi yaklaşan pozisyonlar (30 gün içinde)
    today      = date.today()
    maturing   = []
    call_dates = []
    for p in positions:
        mat_y = float(p.get("maturity_years", 99))
        mat_d = today + timedelta(days=int(mat_y * 365))
        if (mat_d - today).days <= 30:
            maturing.append({
                "name": p.get("name"),
                "days": (mat_d - today).days,
                "par_m": p.get("par_m"),
                "date": mat_d.strftime("%d.%m.%Y"),
            })

    return {
        "position_count":  oas.get("position_count", 0),
        "total_par_m":     oas.get("total_par_m", 0),
        "total_mv_m":      oas.get("total_market_value_m", 0),
        "net_dv01_m":      oas.get("total_net_dv01", 0),
        "avg_duration":    oas.get("portfolio_avg_dur", 0),
        "maturing_30d":    maturing,
        "ladder_summary":  ladder.get("ladder", []),
    }


def _get_limit_summary() -> dict:
    """Limit kullanım durumu."""
    from utils.limit_engine import calculate_limit_dashboard
    dash = calculate_limit_dashboard()
    return {
        "overall_rag":   dash["summary"]["overall_rag"],
        "overall_tr":    dash["summary"]["overall_tr"],
        "breach_count":  dash["summary"]["breach_count"],
        "warning_count": dash["summary"]["warning_count"],
        "var_1d_m":      dash["summary"]["var_1d_m"],
        "limits":        [
            {"label": l["label"], "rag": l["rag"], "usage_pct": l["usage_pct"],
             "current": l["current"], "limit": l["limit"], "unit": l["unit"]}
            for l in dash["limits"] if l["rag"] in ("amber","red")
        ],
    }


def _get_alm_summary() -> dict:
    """Pull latest ALM risk metrics from the results cache."""
    try:
        from utils.results_cache import get_prefill
        return get_prefill("eod")
    except Exception:
        return {}


def _generate_narrative(market: dict, changes: dict, portfolio: dict,
                         limits: dict, pnl_attr: dict) -> dict:
    """
    Günün hikayesini yapılandırılmış bölümler halinde üretir.
    """
    today_str = date.today().strftime("%d %B %Y, %A")
    sections  = []

    # ── 1. Piyasa Hareketi ────────────────────────────────────────────────────
    us_chg    = changes.get("us_curve", {})
    tr_chg    = changes.get("tr_curve", {})
    fx_rates  = market.get("fx", {})
    usdtry    = fx_rates.get("USDTRY", 0)

    market_lines = []
    if us_chg.get("10Y"):
        market_lines.append(f"ABD 10Y hazine {'yükseldi' if us_chg['10Y']>0 else 'düştü'} "
                             f"({_pct_change_label(us_chg['10Y'])})")
    if us_chg.get("2Y"):
        market_lines.append(f"ABD 2Y {'yükseldi' if us_chg['2Y']>0 else 'düştü'} "
                             f"({_pct_change_label(us_chg['2Y'])})")
    if tr_chg.get("2Y"):
        market_lines.append(f"TR 2Y DİBS {'yükseldi' if tr_chg['2Y']>0 else 'düştü'} "
                             f"({_pct_change_label(tr_chg['2Y'])})")
    if tr_chg.get("10Y"):
        market_lines.append(f"TR 10Y DİBS {'yükseldi' if tr_chg['10Y']>0 else 'düştü'} "
                             f"({_pct_change_label(tr_chg['10Y'])})")
    if usdtry:
        market_lines.append(f"USDTRY kapanış: {usdtry:.4f}")

    policy_rate = market.get("policy", {}).get("lending_rate")
    if policy_rate:
        market_lines.append(f"TCMB üst bant: %{policy_rate:.2f}")

    sections.append({
        "title": "📊 Bugünkü Piyasa Hareketleri",
        "items": market_lines or ["Karşılaştırma verisi yok — ilk snapshot alındığında aktif olacak."],
    })

    # ── 2. Portföy Durumu ─────────────────────────────────────────────────────
    port_lines = [
        f"Aktif pozisyon sayısı: {portfolio['position_count']}",
        f"Toplam nominal: {portfolio['total_par_m']:.1f} M TL",
        f"Piyasa değeri: {portfolio['total_mv_m']:.1f} M TL",
        f"Net DV01: {portfolio['net_dv01_m']:.3f} M TL/bps",
        f"Ağırlıklı duration: {portfolio['avg_duration']:.2f} yıl",
    ]
    if portfolio["maturing_30d"]:
        for m in portfolio["maturing_30d"]:
            port_lines.append(f"⚠ Vadeye {m['days']} gün: {m['name']} ({m['par_m']} M TL) — {m['date']}")

    sections.append({
        "title": "💼 Portföy Durumu",
        "items": port_lines,
    })

    # ── 3. Limit Kullanımı ────────────────────────────────────────────────────
    lim_lines = [
        f"Genel durum: {limits['overall_tr']}",
        f"VaR (1G, %99): {limits['var_1d_m']:.2f} M TL",
    ]
    if limits["breach_count"] > 0:
        lim_lines.append(f"🔴 {limits['breach_count']} limit AŞIMDA — acil inceleme gerekiyor")
    if limits["warning_count"] > 0:
        lim_lines.append(f"🟡 {limits['warning_count']} limit uyarı seviyesinde")
    for lim in limits.get("limits", []):
        emoji = "🔴" if lim["rag"] == "red" else "🟡"
        lim_lines.append(f"{emoji} {lim['label']}: {lim['current']:.2f} / {lim['limit']} {lim['unit']} (%{lim['usage_pct']:.1f})")

    sections.append({
        "title": "🛡 Limit Durumu",
        "items": lim_lines,
    })

    # ── 4. P&L Attribution ───────────────────────────────────────────────────
    attr = pnl_attr.get("attribution", {})
    pnl_lines = []
    if attr:
        total = attr.get("total_m", 0)
        pnl_lines.append(f"Günlük P&L: {'+' if total>=0 else ''}{total:.3f} M TL")
        pnl_lines.append(f"  Carry:    {'+' if attr.get('carry_m',0)>=0 else ''}{attr.get('carry_m',0):.3f} M TL")
        pnl_lines.append(f"  Duration: {'+' if attr.get('duration_m',0)>=0 else ''}{attr.get('duration_m',0):.3f} M TL")
        pnl_lines.append(f"  Eğri:     {'+' if attr.get('curve_m',0)>=0 else ''}{attr.get('curve_m',0):.3f} M TL")
        pnl_lines.append(f"  FX:       {'+' if attr.get('fx_m',0)>=0 else ''}{attr.get('fx_m',0):.3f} M TL")
        pnl_lines.append(f"  Artık:    {'+' if attr.get('residual_m',0)>=0 else ''}{attr.get('residual_m',0):.3f} M TL")
    else:
        pnl_lines.append("Attribution hesaplanamadı — snapshot gerekli.")

    sections.append({
        "title": "💰 P&L Attribution",
        "items": pnl_lines,
    })

    # ── 5. ALM Risk Özeti (results cache) ───────────────────────────────────
    alm = _safe(_get_alm_summary, {})
    sourced = alm.get("sourced", [])
    alm_lines = []
    if sourced:
        def _fmt(v, unit=""):
            if v is None: return "—"
            return f"{v:+.1f}{unit}" if isinstance(v, float) else str(v)

        nii_base  = alm.get("nii_base")
        nii_risk  = alm.get("nii_at_risk")
        eve_risk  = alm.get("eve_at_risk")
        dur_gap   = alm.get("duration_gap")
        tier1     = alm.get("tier1")
        lcr_v     = alm.get("lcr")
        nsfr_v    = alm.get("nsfr")
        fx_nop    = alm.get("fx_nop_tl")
        worst_sc  = alm.get("worst_scenario")

        if nii_base is not None:
            alm_lines.append(f"NII baz: {nii_base:.1f} M — En kötü senaryo ({worst_sc or '?'}): {_fmt(nii_risk, ' M')}")
        if eve_risk is not None:
            t1 = tier1 or 1500
            pct = abs(eve_risk / t1 * 100) if t1 else 0
            alm_lines.append(f"ΔEVE (en kötü): {_fmt(eve_risk, ' M')} — Tier1 oranı: %{pct:.1f}")
        if dur_gap is not None:
            alm_lines.append(f"Duration gap: {dur_gap:+.2f} yıl")
        if lcr_v is not None:
            status = "✓" if lcr_v >= 100 else "⚠"
            alm_lines.append(f"LCR: %{lcr_v:.1f} {status}  |  NSFR: %{nsfr_v:.1f} {status}" if nsfr_v else f"LCR: %{lcr_v:.1f} {status}")
        if fx_nop is not None:
            alm_lines.append(f"FX Net Açık Pozisyon: {fx_nop:+.1f} M TL")

        ts_map = alm.get("timestamps", {})
        if ts_map:
            ages = [f"{k.upper()} {v['ts_iso'][11:16]}" for k, v in ts_map.items()]
            alm_lines.append(f"Kaynak: {', '.join(sourced)} — {', '.join(ages)}")
    else:
        alm_lines.append("ALM hesaplaması yok — NII/EVE/LCR modüllerini çalıştırın.")

    sections.append({
        "title": "🏦 ALM Risk Özeti",
        "items": alm_lines,
    })

    # ── 7. Yarın İçin ────────────────────────────────────────────────────────
    tomorrow     = date.today() + timedelta(days=1)
    tomorrow_str = tomorrow.strftime("%d.%m.%Y %A")
    tomorrow_lines = [f"Tarih: {tomorrow_str}"]

    # FRED verileri açıklama takvimi (bilinen sabitler)
    scheduled_releases = _get_scheduled_releases(tomorrow)
    if scheduled_releases:
        tomorrow_lines += [f"📅 {r}" for r in scheduled_releases]
    else:
        tomorrow_lines.append("Bilinen program yok.")

    if portfolio["maturing_30d"]:
        for m in portfolio["maturing_30d"]:
            if m["days"] <= 1:
                tomorrow_lines.append(f"🔔 VADE: {m['name']} — {m['par_m']} M TL")

    sections.append({
        "title": "📅 Yarın Dikkat",
        "items": tomorrow_lines,
    })

    return {
        "date":     today_str,
        "sections": sections,
        "generated_at": datetime.now().strftime("%H:%M:%S"),
    }


def _get_scheduled_releases(target_date: date) -> list:
    """Basit ekonomik takvim — haftalık sabit yayınlar."""
    releases = []
    wd = target_date.weekday()  # 0=Pzt, 4=Cum
    dom = target_date.day

    if wd == 0:   releases.append("TCMB Haftalık Para İstatistikleri")
    if wd == 3:   releases.append("ABD Haftalık İşsizlik Başvuruları (Perşembe)")
    if wd == 4:   releases.append("ABD Tarım Dışı İstihdam (eğer ay başı Cuma)")

    if 1 <= dom <= 7 and wd == 3:
        releases.append("TCMB MPC Kararı (olası)")

    # FOMC toplantı haftaları — kaba yaklaşım
    if dom in (25, 26, 27, 28, 29, 30, 31) and wd == 2:
        releases.append("FOMC Karar Toplantısı (olası)")

    return releases


def generate_eod_brief(save_cache: bool = True) -> dict:
    """
    Tam EOD Brief üretir.
    """
    # Piyasa verisi
    market  = _safe(_fetch_market_snapshot, {})

    # Dünkü ile karşılaştır
    prev    = _get_prev_snapshot()
    changes = {}
    if prev:
        changes = {
            "us_curve": _compare_curves(market.get("us_curve", {}), prev.get("us_curve", {})),
            "tr_curve": _compare_curves(market.get("tr_curve", {}), prev.get("tr_curve", {})),
            "fx":       _compare_curves(market.get("fx", {}), prev.get("fx", {})),
        }

    # Portföy
    portfolio = _safe(_get_portfolio_summary, {
        "position_count": 0, "total_par_m": 0, "total_mv_m": 0,
        "net_dv01_m": 0, "avg_duration": 0, "maturing_30d": [], "ladder_summary": [],
    })

    # Limitler
    limits = _safe(_get_limit_summary, {
        "overall_rag": "green", "overall_tr": "Normal",
        "breach_count": 0, "warning_count": 0, "var_1d_m": 0, "limits": [],
    })

    # P&L Attribution
    from utils.pnl_attribution_engine import calculate_attribution, take_snapshot
    pnl_attr = _safe(calculate_attribution, {})

    # ALM Risk (from cross-module results cache)
    alm_summary = _safe(_get_alm_summary, {})

    # Anlatı oluştur
    narrative = _generate_narrative(market, changes, portfolio, limits, pnl_attr)


    # EOD snapshot al
    if save_cache:
        _safe(lambda: take_snapshot(label="EOD"))

    result = {
        "narrative":    narrative,
        "market":       market,
        "portfolio":    portfolio,
        "limits":       limits,
        "pnl_attr":     pnl_attr,
        "changes":      changes,
        "alm_summary":  alm_summary,
        "generated_at": datetime.now().isoformat(),
    }

    if save_cache:
        try:
            _atomic_json_write(EOD_CACHE, result, default=str)
        except Exception:
            pass

    return result


def load_cached_eod() -> Optional[dict]:
    if os.path.exists(EOD_CACHE):
        try:
            return json.load(open(EOD_CACHE))
        except Exception:
            pass
    return None

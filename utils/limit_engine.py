"""
Bek Rate Desk — Limit Dashboard Engine
========================================
Tüm risk limitleri için canlı kullanım oranı ve RAG durumu hesabı.

Limit tipleri:
  - duration_max     : portföy ağırlıklı ortalama duration üst sınırı
  - dv01_total_m     : toplam net DV01 (M TL/bps) mutlak değer
  - dv01_bucket_m    : kova bazında DV01 (M TL/bps)
  - nop_pct          : NOP / özkaynak % limiti
  - var_1d_m         : 1 günlük parametrik VaR (M TL)
  - concentration_pct: tek ihraçcı konsantrasyon % limiti

Limitler data/limit_config.json'dan okunur; varsayılanlar aşağıda.
"""

import os, json, math
from utils.io_utils import _atomic_json_write
from datetime import datetime
from typing import Optional

SCRIPT_DIR  = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR    = os.path.join(SCRIPT_DIR, "data")
CONFIG_FILE = os.path.join(DATA_DIR, "limit_config.json")

# ──────────────────────────────────────────────────────────────────────────────
# Varsayılan limit yapılandırması
# ──────────────────────────────────────────────────────────────────────────────
DEFAULT_LIMITS = {
    "duration_max":       {"value": 5.0,   "unit": "yıl",     "label": "Ort. Duration"},
    "dv01_total_m":       {"value": 2.5,   "unit": "M TL/bps","label": "Toplam Net DV01"},
    "dv01_bucket_0_1":    {"value": 0.8,   "unit": "M TL/bps","label": "DV01 Kovası 0-1Y"},
    "dv01_bucket_1_2":    {"value": 0.8,   "unit": "M TL/bps","label": "DV01 Kovası 1-2Y"},
    "dv01_bucket_2_5":    {"value": 1.2,   "unit": "M TL/bps","label": "DV01 Kovası 2-5Y"},
    "dv01_bucket_5_10":   {"value": 1.0,   "unit": "M TL/bps","label": "DV01 Kovası 5-10Y"},
    "dv01_bucket_10plus": {"value": 0.5,   "unit": "M TL/bps","label": "DV01 Kovası 10Y+"},
    "nop_pct":            {"value": 20.0,  "unit": "%",        "label": "NOP / Özkaynak"},
    "var_1d_m":           {"value": 5.0,   "unit": "M TL",    "label": "VaR (1G, %99)"},
    "concentration_pct":  {"value": 25.0,  "unit": "%",        "label": "Tek İhraçcı Maks."},
    "equity_m":           {"value": 1000.0,"unit": "M TL",    "label": "Özkaynak (referans)"},
}

# RAG eşikleri (kullanım oranı)
AMBER_THRESHOLD = 0.75   # %75 → sarı
RED_THRESHOLD   = 0.90   # %90 → kırmızı


def load_limits() -> dict:
    os.makedirs(DATA_DIR, exist_ok=True)
    if os.path.exists(CONFIG_FILE):
        try:
            stored = json.load(open(CONFIG_FILE))
            # Eksik anahtarları varsayılanlarla tamamla
            merged = {**DEFAULT_LIMITS}
            for k, v in stored.items():
                if k in merged:
                    merged[k] = {**merged[k], **v}
            return merged
        except Exception:
            pass
    return dict(DEFAULT_LIMITS)


def save_limits(limits: dict) -> dict:
    os.makedirs(DATA_DIR, exist_ok=True)
    _atomic_json_write(CONFIG_FILE, limits)
    return {"status": "saved"}


def _rag(usage_ratio: float) -> str:
    if usage_ratio >= RED_THRESHOLD:
        return "red"
    if usage_ratio >= AMBER_THRESHOLD:
        return "amber"
    return "green"


def _rag_tr(status: str) -> str:
    return {"green": "Normal", "amber": "Dikkat", "red": "Limit Aşımı"}.get(status, "—")


# ──────────────────────────────────────────────────────────────────────────────
# Canlı değer çekme — mevcut modüllerden
# ──────────────────────────────────────────────────────────────────────────────

def _get_portfolio_metrics() -> dict:
    """Portföyden DV01 merdiveni ve duration çeker."""
    try:
        from utils.dealer_analytics_engine import load_portfolio, run_portfolio_oas, build_risk_ladder
        positions = load_portfolio()
        oas = run_portfolio_oas(positions)
        ladder = build_risk_ladder(positions)

        buckets = {row["bucket"]: abs(row["net_dv01"]) for row in ladder.get("ladder", [])}
        return {
            "avg_duration":    abs(oas.get("portfolio_avg_dur", 0.0)),
            "net_dv01_total":  abs(oas.get("total_net_dv01", 0.0)),
            "dv01_0_1":        buckets.get("0-1Y",  0.0),
            "dv01_1_2":        buckets.get("1-2Y",  0.0),
            "dv01_2_5":        buckets.get("2-5Y",  0.0),
            "dv01_5_10":       buckets.get("5-10Y", 0.0),
            "dv01_10plus":     buckets.get("10Y+",  0.0),
            "total_mv_m":      oas.get("total_market_value_m", 0.0),
            "positions":       positions,
        }
    except Exception:
        return {
            "avg_duration": 0.0, "net_dv01_total": 0.0,
            "dv01_0_1": 0.0, "dv01_1_2": 0.0, "dv01_2_5": 0.0,
            "dv01_5_10": 0.0, "dv01_10plus": 0.0,
            "total_mv_m": 0.0, "positions": [],
        }


def _get_nop_metrics(equity_m: float) -> dict:
    """NOP verisi — trading desk veya varsayılan."""
    try:
        nop_file = os.path.join(DATA_DIR, "nop_snapshot.json")
        if os.path.exists(nop_file):
            snap = json.load(open(nop_file))
            nop_try_m = float(snap.get("nop_try_m", 0.0))
            nop_pct = (nop_try_m / equity_m * 100) if equity_m > 0 else 0.0
            return {"nop_try_m": nop_try_m, "nop_pct": nop_pct}
    except Exception:
        pass
    return {"nop_try_m": 0.0, "nop_pct": 0.0}


def _calc_parametric_var(dv01_total: float, confidence: float = 0.99,
                          holding_days: int = 1) -> float:
    """
    Parametrik VaR: DV01 × σ_faiz_günlük × z_skoru × √T
    σ_faiz_günlük: MOVE endeksinden veya varsayılan 8bps/gün (TR için ~120 MOVE).
    """
    try:
        from utils.oas_engine import fetch_move_vol
        mv = fetch_move_vol()
        move = mv.get("move_index", 100.0)
    except Exception:
        move = 100.0

    # TR için MOVE yaklaşık ölçekleme: TR faiz vol ≈ 2.5× USD MOVE
    tr_vol_bps_annual = move * 2.5
    tr_vol_bps_daily  = tr_vol_bps_annual / math.sqrt(252)
    z = 2.326 if confidence >= 0.99 else 1.645  # z0.99 veya z0.95
    var_m = dv01_total * tr_vol_bps_daily * z * math.sqrt(holding_days)
    return round(var_m, 3)


def _calc_concentration(positions: list, total_mv_m: float) -> dict:
    """En büyük tek ihraçcı konsantrasyonunu hesapla."""
    if not positions or total_mv_m <= 0:
        return {"max_pct": 0.0, "max_issuer": "—"}
    issuer_mv = {}
    for pos in positions:
        issuer = pos.get("name", "Bilinmeyen")
        mv     = abs(float(pos.get("par_m", 0)) * float(pos.get("market_price_pct", 100)) / 100)
        issuer_mv[issuer] = issuer_mv.get(issuer, 0.0) + mv
    if not issuer_mv:
        return {"max_pct": 0.0, "max_issuer": "—"}
    max_issuer = max(issuer_mv, key=issuer_mv.get)
    max_pct    = issuer_mv[max_issuer] / total_mv_m * 100
    return {"max_pct": round(max_pct, 1), "max_issuer": max_issuer}


# ──────────────────────────────────────────────────────────────────────────────
# Ana hesaplama
# ──────────────────────────────────────────────────────────────────────────────

def calculate_limit_dashboard() -> dict:
    """
    Tüm limit kullanım oranlarını ve RAG durumlarını hesaplar.
    """
    limits  = load_limits()
    metrics = _get_portfolio_metrics()
    equity  = float(limits.get("equity_m", {}).get("value", 1000.0))
    nop     = _get_nop_metrics(equity)
    var_1d  = _calc_parametric_var(metrics["net_dv01_total"])
    conc    = _calc_concentration(metrics["positions"], metrics["total_mv_m"])

    def _entry(key, current_val, current_label=None):
        cfg   = limits.get(key, {})
        limit = float(cfg.get("value", 1.0))
        ratio = abs(current_val) / limit if limit > 0 else 0.0
        rag   = _rag(ratio)
        return {
            "key":          key,
            "label":        cfg.get("label", key),
            "limit":        round(limit, 3),
            "current":      round(current_val, 3),
            "current_label": current_label,
            "unit":         cfg.get("unit", ""),
            "usage_pct":    round(min(ratio * 100, 150), 1),  # cap görsel %150
            "rag":          rag,
            "rag_tr":       _rag_tr(rag),
            "headroom":     round(limit - abs(current_val), 3),
        }

    entries = [
        _entry("duration_max",       metrics["avg_duration"]),
        _entry("dv01_total_m",        metrics["net_dv01_total"]),
        _entry("dv01_bucket_0_1",     metrics["dv01_0_1"]),
        _entry("dv01_bucket_1_2",     metrics["dv01_1_2"]),
        _entry("dv01_bucket_2_5",     metrics["dv01_2_5"]),
        _entry("dv01_bucket_5_10",    metrics["dv01_5_10"]),
        _entry("dv01_bucket_10plus",  metrics["dv01_10plus"]),
        _entry("nop_pct",             nop["nop_pct"]),
        _entry("var_1d_m",            var_1d),
        _entry("concentration_pct",   conc["max_pct"], conc["max_issuer"]),
    ]

    # Özet
    breaches = [e for e in entries if e["rag"] == "red"]
    warnings = [e for e in entries if e["rag"] == "amber"]
    overall  = "red" if breaches else ("amber" if warnings else "green")

    return {
        "limits":        entries,
        "summary": {
            "overall_rag":   overall,
            "overall_tr":    _rag_tr(overall),
            "breach_count":  len(breaches),
            "warning_count": len(warnings),
            "total_mv_m":    round(metrics["total_mv_m"], 1),
            "var_1d_m":      var_1d,
            "nop_pct":       round(nop["nop_pct"], 2),
            "equity_m":      equity,
        },
        "as_of": datetime.now().strftime("%Y-%m-%dT%H:%M:%S"),
    }

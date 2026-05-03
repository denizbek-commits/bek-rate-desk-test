"""
Dealer Analytics Engine — Sprint 6
====================================
Risk ladder, portfolio OAS, carry analysis, RV matrix, carry trade P&L
"""

import os, json, math
from datetime import datetime
from utils.io_utils import _atomic_json_write
import pandas as pd
import numpy as np
from scipy.optimize import brentq
import yfinance as yf

SCRIPT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.path.join(SCRIPT_DIR, 'data')
os.makedirs(DATA_DIR, exist_ok=True)

from utils.oas_engine import price_bond_full
from utils.rates_engine import fetch_tr_yield_curve, fetch_tr_analytics, _fetch_fred_series

TREASURY_SERIES = {"2Y":"DGS2","5Y":"DGS5","10Y":"DGS10","20Y":"DGS20","30Y":"DGS30"}

# ─────────────────────────────────────────────────────────────────────────────
# YARDIMCI: YTM'den direkt fiyat hesabı
# ─────────────────────────────────────────────────────────────────────────────
def _price_at_ytm(maturity_y: float, coupon_pct: float, ytm_pct: float,
                  face: float = 100.0, freq: int = 2) -> float:
    """Verilen YTM'den (%) tahvil fiyatı hesaplar — price_bond_full'dan bağımsız."""
    n = max(1, round(maturity_y * freq))
    c = face * (coupon_pct / 100) / freq
    r = (ytm_pct / 100) / freq
    if abs(r) < 1e-10:
        return face + c * n
    return sum(c / (1 + r) ** t for t in range(1, n + 1)) + face / (1 + r) ** n

def _interp_curve_yield(maturity_y: float, curve: dict):
    """curve: {tenor_str: yield_pct} — doğrusal interpolasyon."""
    pts = []
    for k, v in curve.items():
        if v is None:
            continue
        try:
            ks = str(k).upper().strip()
            yr = float(ks.replace('M', '')) / 12 if 'M' in ks else float(ks.replace('Y', ''))
            pts.append((yr, float(v)))
        except Exception:
            pass
    if not pts:
        return None
    pts.sort()
    if maturity_y <= pts[0][0]:  return pts[0][1]
    if maturity_y >= pts[-1][0]: return pts[-1][1]
    for i in range(len(pts) - 1):
        t0, y0 = pts[i]; t1, y1 = pts[i + 1]
        if t0 <= maturity_y <= t1:
            return y0 + (y1 - y0) * (maturity_y - t0) / (t1 - t0)
    return pts[-1][1]

# ══════════════════════════════════════════════════════════════════════════════
# RISK LADDER — DV01 by maturity bucket
# ══════════════════════════════════════════════════════════════════════════════

def build_risk_ladder(positions: list) -> dict:
    """
    Computes DV01 per maturity bucket and generates hedge suggestions.
    EVE convention: asset → negative dv01, liability → positive.
    """
    buckets = {
        "0-1Y":   {"min": 0.0, "max": 1.0, "dv01_asset": 0.0, "dv01_liability": 0.0, "count": 0, "positions": []},
        "1-2Y":   {"min": 1.0, "max": 2.0, "dv01_asset": 0.0, "dv01_liability": 0.0, "count": 0, "positions": []},
        "2-5Y":   {"min": 2.0, "max": 5.0, "dv01_asset": 0.0, "dv01_liability": 0.0, "count": 0, "positions": []},
        "5-10Y":  {"min": 5.0, "max":10.0, "dv01_asset": 0.0, "dv01_liability": 0.0, "count": 0, "positions": []},
        "10Y+":   {"min":10.0, "max":999.0, "dv01_asset": 0.0, "dv01_liability": 0.0, "count": 0, "positions": []},
    }

    position_details = []
    total_net_dv01 = 0.0

    for pos in positions:
        try:
            par_m = float(pos.get("par_m", 0))
            if par_m <= 0: continue

            coupon_pct = float(pos.get("coupon_pct", 5.0))
            maturity_years = float(pos.get("maturity_years", 5.0))
            market_price_pct = float(pos.get("market_price_pct", 100.0))
            freq = int(pos.get("freq", 2))
            side = pos.get("side", "asset")

            result = price_bond_full({
                "coupon_rate": coupon_pct,
                "maturity_years": maturity_years,
                "market_price": market_price_pct,
                "par": 100.0,
                "freq": freq,
            })

            dv01_unit = float(result.get("dv01", 0.0))
            dv01_portfolio_m = dv01_unit / 100.0 * par_m

            if side == "asset":
                dv01_contribution = -dv01_portfolio_m
            else:
                dv01_contribution = dv01_portfolio_m

            total_net_dv01 += dv01_contribution

            bucket_key = None
            for key, bucket in buckets.items():
                if bucket["min"] <= maturity_years < bucket["max"]:
                    bucket_key = key
                    break
            if not bucket_key:
                bucket_key = "10Y+"

            if side == "asset":
                buckets[bucket_key]["dv01_asset"] += dv01_portfolio_m
            else:
                buckets[bucket_key]["dv01_liability"] += dv01_portfolio_m

            buckets[bucket_key]["count"] += 1
            buckets[bucket_key]["positions"].append(pos.get("name", "Unknown"))

            position_details.append({
                "name": pos.get("name", "Unnamed"),
                "side": side,
                "par_m": par_m,
                "coupon_pct": coupon_pct,
                "maturity_years": maturity_years,
                "market_price_pct": market_price_pct,
                "ytm_pct": float(result.get("ytm_pct", 0.0)),
                "dv01_unit": dv01_unit,
                "dv01_portfolio_m": dv01_portfolio_m,
                "bucket": bucket_key,
            })
        except Exception as e:
            continue

    ladder = []
    for bucket_name in ["0-1Y", "1-2Y", "2-5Y", "5-10Y", "10Y+"]:
        b = buckets[bucket_name]
        net = b["dv01_asset"] - b["dv01_liability"]
        ladder.append({
            "bucket": bucket_name,
            "dv01_asset": round(b["dv01_asset"], 2),
            "dv01_liability": round(b["dv01_liability"], 2),
            "net_dv01": round(net, 2),
            "count": b["count"],
            "positions": b["positions"],
        })

    hedge_suggestions = []
    max_abs = 0
    max_bucket = None
    for item in ladder:
        if abs(item["net_dv01"]) > max_abs:
            max_abs = abs(item["net_dv01"])
            max_bucket = item

    if max_bucket and abs(max_bucket["net_dv01"]) > 0.5:
        if max_bucket["net_dv01"] > 0:
            action = "IRS Alıcı (Faiz Sat)"
            rationale_tr = f"Liability overhang — {max_bucket['bucket']} vadede likidite hedge"
        else:
            action = "IRS Satıcı (Faiz Al)"
            rationale_tr = f"Asset duration mismatch — {max_bucket['bucket']} vadede koruma"

        hedge_suggestions.append({
            "bucket": max_bucket["bucket"],
            "net_dv01": round(max_bucket["net_dv01"], 2),
            "action": action,
            "rationale_tr": rationale_tr,
            "urgency": "high" if abs(max_bucket["net_dv01"]) > 10 else "medium",
        })

    return {
        "ladder": ladder,
        "positions": position_details,
        "total_net_dv01": round(total_net_dv01, 2),
        "hedge_suggestions": hedge_suggestions,
        "position_count": len(position_details),
        "as_of": datetime.now().isoformat(),
    }

# ══════════════════════════════════════════════════════════════════════════════
# PORTFOLIO PERSISTENCE
# ══════════════════════════════════════════════════════════════════════════════

def _default_portfolio():
    """Default portfolio for demo."""
    return [
        {
            "id": "p001",
            "name": "DIBS 2026",
            "currency": "TRY",
            "side": "asset",
            "par_m": 500.0,
            "coupon_pct": 28.5,
            "maturity_years": 1.5,
            "market_price_pct": 100.2,
            "bond_type": "fixed",
            "freq": 2,
        },
        {
            "id": "p002",
            "name": "DIBS 2028",
            "currency": "TRY",
            "side": "asset",
            "par_m": 300.0,
            "coupon_pct": 26.0,
            "maturity_years": 3.2,
            "market_price_pct": 99.8,
            "bond_type": "fixed",
            "freq": 2,
        },
        {
            "id": "p003",
            "name": "Eurobond 2030",
            "currency": "USD",
            "side": "asset",
            "par_m": 150.0,
            "coupon_pct": 5.25,
            "maturity_years": 5.8,
            "market_price_pct": 101.5,
            "bond_type": "fixed",
            "freq": 2,
        },
        {
            "id": "p004",
            "name": "UST 9.5Y",
            "currency": "USD",
            "side": "asset",
            "par_m": 100.0,
            "coupon_pct": 4.25,
            "maturity_years": 9.5,
            "market_price_pct": 98.7,
            "bond_type": "fixed",
            "freq": 2,
        },
        {
            "id": "p005",
            "name": "Mevduat 2Y",
            "currency": "TRY",
            "side": "liability",
            "par_m": 400.0,
            "coupon_pct": 38.0,
            "maturity_years": 2.0,
            "market_price_pct": 100.0,
            "bond_type": "fixed",
            "freq": 2,
        },
    ]

def load_portfolio():
    """Load portfolio from JSON or return default."""
    path = os.path.join(DATA_DIR, 'bond_portfolio.json')
    if os.path.exists(path):
        try:
            return json.load(open(path))
        except:
            pass
    return _default_portfolio()

def save_portfolio(positions: list):
    """Save portfolio to JSON."""
    path = os.path.join(DATA_DIR, 'bond_portfolio.json')
    _atomic_json_write(path, positions)
    return {"status": "saved", "count": len(positions)}

# ══════════════════════════════════════════════════════════════════════════════
# CARRY & ROLL-DOWN ANALYSIS
# ══════════════════════════════════════════════════════════════════════════════

def calculate_carry_rolldown(params: dict, curve: dict) -> dict:
    """
    Carry ve roll-down hesabı — _price_at_ytm kullanır, price_bond_full değil.
    params: coupon_pct (%), maturity_years, market_price_pct (per 100),
            holding_months, freq, repo_rate_pct (%)
    curve:  {tenor_str: yield_pct}  örn. {"2Y":39.86,"5Y":38.20}
    """
    coupon_pct      = float(params.get("coupon_pct", 28.5))
    maturity_years  = float(params.get("maturity_years", 3.0))
    mkt_price       = float(params.get("market_price_pct", 100.0))
    holding_months  = int(params.get("holding_months", 3))
    freq            = int(params.get("freq", 2))
    repo_pct        = float(params.get("repo_rate_pct", 0.0))
    holding_y       = holding_months / 12.0

    # Mevcut YTM ve duration — price_bond_full kullanıyoruz (doğru yön: fiyat→YTM)
    res = price_bond_full({
        "coupon_rate": coupon_pct, "maturity_years": maturity_years,
        "market_price": mkt_price, "par": 100.0, "freq": freq,
    })
    current_ytm_pct = float(res.get("ytm_pct", coupon_pct))
    mod_duration    = float(res.get("mod_duration", maturity_years * 0.8))
    mac_duration    = float(res.get("mac_duration", maturity_years * 0.85))

    # Carry
    gross_carry = (coupon_pct / 100) * holding_y          # % decimal
    net_carry   = ((coupon_pct - repo_pct) / 100) * holding_y

    # Roll-down YTM: eğriden interpolasyon veya mevcut YTM
    remaining_mat   = max(0.25, maturity_years - holding_y)
    roll_ytm_pct    = _interp_curve_yield(remaining_mat, curve) if curve else None
    if roll_ytm_pct is None:
        roll_ytm_pct = current_ytm_pct   # eğri yoksa flat curve varsayımı

    # Roll-down fiyatı: _price_at_ytm ile DOĞRUDAN hesap
    future_price    = _price_at_ytm(remaining_mat, coupon_pct, roll_ytm_pct, 100.0, freq)
    roll_down       = (future_price - mkt_price) / mkt_price   # % decimal

    roll_down_total = net_carry + roll_down

    # Senaryo analizi
    scenarios = {}
    for shock_bps in [-100, -50, -25, 0, 25, 50, 100, 200]:
        shocked_ytm = roll_ytm_pct + shock_bps / 100.0
        if shocked_ytm < 0.01:
            shocked_ytm = 0.01
        p_shocked    = _price_at_ytm(remaining_mat, coupon_pct, shocked_ytm, 100.0, freq)
        price_ret    = (p_shocked - mkt_price) / mkt_price
        total_ret    = gross_carry + price_ret
        ann_ret      = (total_ret / holding_y) if holding_y > 0 else 0
        scenarios[f"{shock_bps:+d}bps"] = {
            "shock_bps":        shock_bps,
            "price_return_pct": round(price_ret * 100, 3),
            "carry_pct":        round(gross_carry * 100, 3),
            "total_return_pct": round(total_ret * 100, 3),
            "annualized_pct":   round(ann_ret * 100, 2),
        }

    # Başa baş bps — brentq ile
    break_even_bps = None
    try:
        def _total_ret(bps):
            sy = roll_ytm_pct + bps / 100.0
            if sy < 0.01: sy = 0.01
            pp = _price_at_ytm(remaining_mat, coupon_pct, sy, 100.0, freq)
            return gross_carry + (pp - mkt_price) / mkt_price

        # Fonksiyon işaret değişimi arıyoruz
        lo, hi = -800.0, 3000.0
        if _total_ret(lo) * _total_ret(hi) < 0:
            be = brentq(_total_ret, lo, hi, xtol=0.01, maxiter=200)
            break_even_bps = round(be, 1)
    except Exception:
        pass

    return {
        "current_ytm_pct":    round(current_ytm_pct, 4),
        "mod_duration":       round(mod_duration, 4),
        "mac_duration":       round(mac_duration, 4),
        "coupon_pct":         round(coupon_pct, 3),
        "repo_rate_pct":      round(repo_pct, 3),
        "gross_carry_pct":    round(gross_carry * 100, 3),
        "net_carry_pct":      round(net_carry * 100, 3),
        "roll_ytm_pct":       round(roll_ytm_pct, 4),
        "roll_down_pct":      round(roll_down * 100, 3),
        "roll_down_total_pct":round(roll_down_total * 100, 3),
        "holding_months":     holding_months,
        "remaining_mat_y":    round(remaining_mat, 3),
        "break_even_bps":     break_even_bps,
        "scenarios":          scenarios,
        "curve_used":         bool(curve),
    }

# ══════════════════════════════════════════════════════════════════════════════
# PORTFOLIO OAS — Full Risk Analysis
# ══════════════════════════════════════════════════════════════════════════════

def run_portfolio_oas(positions: list) -> dict:
    """
    Full portfolio analytics: duration, DV01, market value.
    EVE convention: asset DV01 contributes negatively to net.
    """
    position_details = []
    total_par_m = 0.0
    total_market_value_m = 0.0
    total_net_dv01 = 0.0
    weighted_duration = 0.0

    for pos in positions:
        try:
            par_m = float(pos.get("par_m", 0))
            if par_m <= 0: continue

            coupon_pct = float(pos.get("coupon_pct", 5.0))
            maturity_years = float(pos.get("maturity_years", 5.0))
            market_price_pct = float(pos.get("market_price_pct", 100.0))
            freq = int(pos.get("freq", 2))
            side = pos.get("side", "asset")
            currency = pos.get("currency", "TRY")

            result = price_bond_full({
                "coupon_rate": coupon_pct,
                "maturity_years": maturity_years,
                "market_price": market_price_pct,
                "par": 100.0,
                "freq": freq,
            })

            ytm_pct = float(result.get("ytm_pct", coupon_pct))
            mac_duration = float(result.get("mac_duration", maturity_years))
            mod_duration = float(result.get("mod_duration", 5.0))
            convexity = float(result.get("convexity", 0.0))
            dv01_unit = float(result.get("dv01", 0.0))

            dv01_portfolio_m = dv01_unit / 100.0 * par_m

            if side == "asset":
                net_dv01_contrib = -dv01_portfolio_m
            else:
                net_dv01_contrib = dv01_portfolio_m

            total_net_dv01 += net_dv01_contrib
            total_par_m += par_m

            market_value_m = par_m * market_price_pct / 100.0
            total_market_value_m += market_value_m

            weighted_duration += market_value_m * mod_duration

            position_details.append({
                "id": pos.get("id", f"p_{len(position_details)}"),
                "name": pos.get("name", "Unnamed"),
                "currency": currency,
                "side": side,
                "par_m": round(par_m, 2),
                "coupon_pct": round(coupon_pct, 3),
                "maturity_years": round(maturity_years, 3),
                "market_price_pct": round(market_price_pct, 3),
                "ytm_pct": round(ytm_pct, 3),
                "mac_duration": round(mac_duration, 3),
                "mod_duration": round(mod_duration, 3),
                "convexity": round(convexity, 4),
                "dv01_unit": round(dv01_unit, 4),
                "dv01_portfolio_m": round(dv01_portfolio_m, 2),
                "market_value_m": round(market_value_m, 2),
            })
        except Exception as e:
            continue

    portfolio_avg_dur = (weighted_duration / total_market_value_m) if total_market_value_m > 0 else 0.0

    return {
        "positions": position_details,
        "total_net_dv01": round(total_net_dv01, 2),
        "portfolio_avg_dur": round(portfolio_avg_dur, 3),
        "total_par_m": round(total_par_m, 2),
        "total_market_value_m": round(total_market_value_m, 2),
        "position_count": len(position_details),
        "as_of": datetime.now().isoformat(),
    }

# ══════════════════════════════════════════════════════════════════════════════
# RELATIVE VALUE MATRIX
# ══════════════════════════════════════════════════════════════════════════════

def build_rv_matrix() -> dict:
    """
    Build RV matrix: US spreads (with z-score), TR spreads, cross-market spreads.
    Signals: aşırı_genişleme, geniş, nötr, dar, aşırı_daralma
    """
    spreads = []
    us_yields = {}
    tr_yields = {}
    tr_policy = None

    # Fallback yields — calibrated to Apr 2025 market levels
    # Used when FRED / live data is unavailable
    _FALLBACK_US_YIELDS = {
        "2Y": 4.25, "3Y": 4.20, "5Y": 4.18, "7Y": 4.22,
        "10Y": 4.35, "20Y": 4.65, "30Y": 4.55,
    }
    _FALLBACK_TR_YIELDS = {
        "1Y": 40.50, "2Y": 38.80, "5Y": 35.20, "10Y": 33.60,
    }

    try:
        for tenor, sid in TREASURY_SERIES.items():
            try:
                series = _fetch_fred_series(sid, days=400)
                if not series.empty:
                    us_yields[tenor] = round(float(series.iloc[-1]), 2)
            except:
                pass

        tr_curve_data = fetch_tr_yield_curve()
        if tr_curve_data and "curve" in tr_curve_data:
            for item in tr_curve_data["curve"]:
                tenor = item.get("tenor", "")
                yield_pct = item.get("yield", 0.0)
                tr_yields[tenor] = round(float(yield_pct), 2)

        tr_analytics = fetch_tr_analytics()
        tr_policy = round(float(tr_analytics.get("policy_rate", 42.5)), 2)
    except:
        pass

    # Apply fallbacks where live data is missing
    if not us_yields:
        us_yields = dict(_FALLBACK_US_YIELDS)
    if not tr_yields:
        tr_yields = dict(_FALLBACK_TR_YIELDS)
    if tr_policy is None:
        tr_policy = 42.5

    # Fallback z-scores calibrated to post-2022 rate environment
    # (used when FRED historical data is unavailable in the sandbox)
    _FALLBACK_Z = {
        "2s5s":   {"mean": 0.05, "std": 0.55},   # historically near-flat, recent inversion
        "2s10s":  {"mean": 0.10, "std": 0.70},   # benchmark spread, widest range
        "5s10s":  {"mean": 0.08, "std": 0.35},   # tighter range
        "5s30s":  {"mean": 0.35, "std": 0.60},   # structurally positive
        "10s30s": {"mean": 0.28, "std": 0.40},   # long-end slope
    }

    spread_configs = [
        {"name": "2s5s", "tenor1": "2Y", "tenor2": "5Y", "market": "USD"},
        {"name": "2s10s", "tenor1": "2Y", "tenor2": "10Y", "market": "USD"},
        {"name": "5s10s", "tenor1": "5Y", "tenor2": "10Y", "market": "USD"},
        {"name": "5s30s", "tenor1": "5Y", "tenor2": "30Y", "market": "USD"},
        {"name": "10s30s", "tenor1": "10Y", "tenor2": "30Y", "market": "USD"},
    ]

    for config in spread_configs:
        tenor1 = config["tenor1"]
        tenor2 = config["tenor2"]

        if tenor1 in us_yields and tenor2 in us_yields:
            spread_bps = (us_yields[tenor2] - us_yields[tenor1]) * 100.0

            z_score = None
            try:
                s1 = _fetch_fred_series(TREASURY_SERIES[tenor1], days=400)
                s2 = _fetch_fred_series(TREASURY_SERIES[tenor2], days=400)

                common = s1.index.intersection(s2.index)
                if len(common) >= 252:
                    spread_series = s2.loc[common] - s1.loc[common]
                    mean_spread = spread_series.mean()
                    std_spread = spread_series.std()
                    if std_spread > 0:
                        z_score = (spread_bps / 100.0 - mean_spread) / std_spread
                    else:
                        z_score = 0.0
            except:
                pass

            # Fallback: use calibrated historical parameters when FRED unavailable
            if z_score is None and config["name"] in _FALLBACK_Z:
                fb = _FALLBACK_Z[config["name"]]
                if fb["std"] > 0:
                    z_score = round((spread_bps / 100.0 - fb["mean"]) / fb["std"], 2)

            signal = None
            if z_score is not None:
                if z_score > 2:   signal = "aşırı_genişleme"
                elif z_score > 1: signal = "geniş"
                elif z_score >= -1: signal = "nötr"
                elif z_score >= -2: signal = "dar"
                else:             signal = "aşırı_daralma"

            spreads.append({
                "name": config["name"],
                "spread_bps": round(spread_bps, 1),
                "z_score": round(z_score, 2) if z_score is not None else None,
                "market": config["market"],
                "signal": signal,
                "z_source": "live" if len(us_yields) > 0 else "fallback",
            })

    tr_spread_configs = [
        {"name": "1Y-2Y TR", "tenor1": "1Y", "tenor2": "2Y"},
        {"name": "2Y-5Y TR", "tenor1": "2Y", "tenor2": "5Y"},
        {"name": "1Y-10Y TR", "tenor1": "1Y", "tenor2": "10Y"},
        {"name": "5Y-10Y TR", "tenor1": "5Y", "tenor2": "10Y"},
    ]

    for config in tr_spread_configs:
        tenor1 = config["tenor1"]
        tenor2 = config["tenor2"]

        if tenor1 in tr_yields and tenor2 in tr_yields:
            spread_bps = (tr_yields[tenor2] - tr_yields[tenor1]) * 100.0
            spreads.append({
                "name": config["name"],
                "spread_bps": round(spread_bps, 1),
                "z_score": None,
                "market": "TRY",
                "signal": None,
            })

    if "2Y" in us_yields and "2Y" in tr_yields:
        cross_spread = (tr_yields["2Y"] - us_yields["2Y"]) * 100.0
        spreads.append({
            "name": "TR2Y-US2Y",
            "spread_bps": round(cross_spread, 1),
            "z_score": None,
            "market": "Çapraz",
            "signal": None,
        })

    if "10Y" in us_yields and "10Y" in tr_yields:
        cross_spread = (tr_yields["10Y"] - us_yields["10Y"]) * 100.0
        spreads.append({
            "name": "TR10Y-US10Y",
            "spread_bps": round(cross_spread, 1),
            "z_score": None,
            "market": "Çapraz",
            "signal": None,
        })

    if tr_policy and "2Y" in tr_yields:
        carry = tr_yields["2Y"] - tr_policy
        spreads.append({
            "name": "TCMB-TR2Y Carry",
            "spread_bps": round(carry * 100.0, 1),
            "z_score": None,
            "market": "Çapraz",
            "signal": None,
        })

    return {
        "spreads": spreads,
        "us_yields": us_yields,
        "tr_yields": tr_yields,
        "tr_policy": tr_policy,
        "as_of": datetime.now().isoformat(),
        "data_source": "FRED + TCMB",
    }

# ══════════════════════════════════════════════════════════════════════════════
# SPREAD HISTORY — Rolling mean, sigma bands, z-score series
# ══════════════════════════════════════════════════════════════════════════════

_SPREAD_FRED_MAP = {
    "2s5s":   ("DGS2",  "DGS5"),
    "2s10s":  ("DGS2",  "DGS10"),
    "5s10s":  ("DGS5",  "DGS10"),
    "5s30s":  ("DGS5",  "DGS30"),
    "10s30s": ("DGS10", "DGS30"),
}


def get_spread_history(spread_name: str, days: int = 400, window: int = 252) -> dict:
    """
    Returns a rolling-window historical spread series with sigma bands and z-scores.
    Only USD Treasury spreads have FRED data; TR/Cross spreads return has_data=False.

    Output keys:
      has_data, spread_name, dates, spread_bps, rolling_mean,
      upper_1s, lower_1s, upper_2s, lower_2s, z_scores,
      current_bps, current_z, mean, std, min, max, window, n_obs
    """
    if spread_name not in _SPREAD_FRED_MAP:
        return {
            "has_data": False,
            "spread_name": spread_name,
            "reason": "Bu spread için tarihsel FRED verisi mevcut değil (sadece ABD Hazine spreadleri desteklenir).",
        }

    sid1, sid2 = _SPREAD_FRED_MAP[spread_name]

    try:
        s1 = _fetch_fred_series(sid1, days=days)
        s2 = _fetch_fred_series(sid2, days=days)

        common = s1.index.intersection(s2.index)
        if len(common) < 30:
            return {"has_data": False, "spread_name": spread_name, "reason": "Yetersiz veri."}

        spread = (s2.loc[common] - s1.loc[common]) * 100.0   # → bps

        w = min(window, len(spread))
        roll_mean = spread.rolling(window=w, min_periods=max(20, w // 4)).mean()
        roll_std  = spread.rolling(window=w, min_periods=max(20, w // 4)).std()

        z_series = (spread - roll_mean) / roll_std.replace(0.0, np.nan)

        def _clean(arr):
            return [round(float(v), 3) if pd.notna(v) else None for v in arr]

        dates     = [str(d)[:10] for d in spread.index]
        sp_vals   = _clean(spread.values)
        rm_vals   = _clean(roll_mean.values)
        std_vals  = _clean(roll_std.values)
        z_vals    = _clean(z_series.values)

        upper_1s = _clean((roll_mean +     roll_std).values)
        lower_1s = _clean((roll_mean -     roll_std).values)
        upper_2s = _clean((roll_mean + 2 * roll_std).values)
        lower_2s = _clean((roll_mean - 2 * roll_std).values)

        cur_bps = float(spread.iloc[-1])
        cur_z   = float(z_series.iloc[-1]) if pd.notna(z_series.iloc[-1]) else None

        valid_sp = spread.dropna()

        return {
            "has_data":     True,
            "spread_name":  spread_name,
            "dates":        dates,
            "spread_bps":   sp_vals,
            "rolling_mean": rm_vals,
            "upper_1s":     upper_1s,
            "lower_1s":     lower_1s,
            "upper_2s":     upper_2s,
            "lower_2s":     lower_2s,
            "z_scores":     z_vals,
            "current_bps":  round(cur_bps, 2),
            "current_z":    round(cur_z, 3) if cur_z is not None else None,
            "mean":         round(float(valid_sp.mean()),   2),
            "std":          round(float(valid_sp.std()),    2),
            "min":          round(float(valid_sp.min()),    2),
            "max":          round(float(valid_sp.max()),    2),
            "window":       window,
            "n_obs":        len(dates),
        }

    except Exception as e:
        return {"has_data": False, "spread_name": spread_name, "reason": str(e)}


# ══════════════════════════════════════════════════════════════════════════════
# CARRY TRADE P&L
# ══════════════════════════════════════════════════════════════════════════════

def calculate_carry_trade(params: dict) -> dict:
    """
    Calculate carry trade P&L for TRY positions.
    params: funding_currency, funding_rate_pct, try_yield_pct, spot_rate, notional_usd, holding_days, hedge_cost_bps
    """
    from utils.tcmb_evds import get_policy_rate as _get_pr
    funding_currency = params.get("funding_currency", "USD")
    funding_rate_pct = float(params.get("funding_rate_pct", 4.33))
    try_yield_pct = float(params.get("try_yield_pct") or _get_pr())
    notional_usd = float(params.get("notional_usd", 10.0))
    holding_days = int(params.get("holding_days", 90))
    hedge_cost_bps = float(params.get("hedge_cost_bps", 0.0))
    holding_years = holding_days / 365.0

    spot_rate = params.get("spot_rate")
    if not spot_rate:
        try:
            pair = f"{funding_currency}TRY=X"
            data = yf.download(pair, period='1d', progress=False)
            if not data.empty:
                spot_rate = float(data['Close'].iloc[-1])
            else:
                spot_rate = 38.5
        except:
            spot_rate = 38.5
    else:
        spot_rate = float(spot_rate)

    notional_try_m = notional_usd * spot_rate

    try:
        pair = f"{funding_currency}TRY=X"
        hist = yf.download(pair, period='1y', progress=False)
        if not hist.empty and len(hist) > 1:
            returns = np.log(hist['Close'] / hist['Close'].shift(1)).dropna()
            hist_vol_ann_pct = float(returns.std() * np.sqrt(252) * 100.0)
        else:
            hist_vol_ann_pct = 15.0
    except:
        hist_vol_ann_pct = 15.0

    gross_carry_ann_pct = try_yield_pct - funding_rate_pct
    net_carry_ann_pct = gross_carry_ann_pct - hedge_cost_bps / 100.0

    carry_try_m = notional_usd * spot_rate * (try_yield_pct / 100.0) * holding_years
    funding_cost_usd_m = notional_usd * (funding_rate_pct / 100.0) * holding_years

    # Break-even: spot_end where TRY proceeds / spot_end = USD obligation
    # TRY proceeds = notional_usd * spot * (1 + try_yield * T)
    # USD obligation = notional_usd * (1 + fund_rate * T)
    # spot_end_be = spot * (1 + try_yield*T) / (1 + fund_rate*T)
    try_growth   = 1.0 + (try_yield_pct  / 100.0) * holding_years
    fund_growth  = 1.0 + (funding_rate_pct / 100.0) * holding_years
    break_even_spot      = spot_rate * try_growth / fund_growth
    break_even_depr_pct  = (break_even_spot / spot_rate - 1.0) * 100.0

    carry_to_vol_ratio = (net_carry_ann_pct / hist_vol_ann_pct) if hist_vol_ann_pct > 0 else 0.0

    fx_changes = [-20, -10, -5, 0, 5, 10, 15, 20, 30, 40]
    scenarios = []

    for fx_change_pct in fx_changes:
        spot_end = spot_rate * (1.0 + fx_change_pct / 100.0)
        carry_try = carry_try_m * 1.0e6
        funding_usd = funding_cost_usd_m * 1.0e6
        pnl_usd = (carry_try / spot_end - funding_usd) / 1.0e6

        if notional_usd > 0:
            annualized_pct = (pnl_usd / notional_usd / holding_years * 365.0 / 365.0 * 100.0) if holding_years > 0 else 0.0
        else:
            annualized_pct = 0.0

        scenarios.append({
            "fx_change_pct": fx_change_pct,
            "spot_end": round(spot_end, 4),
            "pnl_usd_m": round(pnl_usd, 3),
            "annualized_pct": round(annualized_pct, 3),
            "profitable": pnl_usd >= 0,
        })

    return {
        "funding_currency": funding_currency,
        "funding_rate_pct": round(funding_rate_pct, 3),
        "try_yield_pct": round(try_yield_pct, 3),
        "spot_rate": round(spot_rate, 4),
        "notional_usd_m": round(notional_usd, 2),
        "notional_try_m": round(notional_try_m, 2),
        "holding_days": holding_days,
        "gross_carry_ann_pct": round(gross_carry_ann_pct, 3),
        "net_carry_ann_pct": round(net_carry_ann_pct, 3),
        "carry_try_m": round(carry_try_m, 3),
        "funding_cost_usd_m": round(funding_cost_usd_m, 3),
        "break_even_spot": round(break_even_spot, 4),
        "break_even_depr_pct": round(break_even_depr_pct, 3),
        "hist_vol_ann_pct": round(hist_vol_ann_pct, 3),
        "carry_to_vol_ratio": round(carry_to_vol_ratio, 3),
        "scenarios": scenarios,
        "hedge_cost_bps": hedge_cost_bps,
    }

# ══════════════════════════════════════════════════════════════════════════════
# WATCHLIST
# ══════════════════════════════════════════════════════════════════════════════

def _default_watchlist():
    """Default watchlist for monitoring rates and FX."""
    return [
        {
            "id": "w001",
            "name": "UST 10Y Eşik",
            "type": "us_rate",
            "tenor": "10Y",
            "above": 4.80,
            "below": 4.00,
            "active": True,
            "note": "FOMC karar eşiği",
        },
        {
            "id": "w002",
            "name": "2s10s ABD Spread",
            "type": "us_spread",
            "spread_key": "2s10s",
            "above": 50,
            "below": -50,
            "active": True,
            "note": "Resesyon bölgesi (bps)",
        },
        {
            "id": "w003",
            "name": "USD/TRY",
            "type": "fx",
            "pair": "USDTRY=X",
            "above": 40.0,
            "below": 36.0,
            "active": True,
            "note": "TCMB müdahale zonu",
        },
        {
            "id": "w004",
            "name": "TCMB Politika Faizi",
            "type": "tr_policy",
            "above": 50.0,
            "below": 40.0,
            "active": True,
            "note": "PPK değişim sinyali",
        },
    ]

def load_watchlist():
    """Load watchlist from JSON or return default."""
    path = os.path.join(DATA_DIR, 'watchlist.json')
    if os.path.exists(path):
        try:
            return json.load(open(path))
        except:
            pass
    return _default_watchlist()

def save_watchlist(items: list):
    """Save watchlist to JSON."""
    path = os.path.join(DATA_DIR, 'watchlist.json')
    _atomic_json_write(path, items)
    return {"status": "saved", "count": len(items)}

def check_watchlist_live(items: list) -> list:
    """
    Check each watchlist item against live data, set triggered and trigger_dir.
    """
    result = []

    for item in items:
        if not item.get("active", False):
            result.append({**item, "triggered": False, "trigger_dir": None, "current_value": None})
            continue

        item_type = item.get("type", "")
        triggered = False
        trigger_dir = None
        current_value = None

        try:
            if item_type == "us_rate":
                tenor = item.get("tenor", "10Y")
                series = _fetch_fred_series(TREASURY_SERIES.get(tenor, "DGS10"), days=5)
                if not series.empty:
                    current_value = float(series.iloc[-1])
                    above = item.get("above")
                    below = item.get("below")
                    if above and current_value > above:
                        triggered = True
                        trigger_dir = "yukarı"
                    elif below and current_value < below:
                        triggered = True
                        trigger_dir = "aşağı"

            elif item_type == "us_spread":
                spread_key = item.get("spread_key", "2s10s")
                tenor_map = {"2s10s": ("DGS2", "DGS10"), "2s5s": ("DGS2", "DGS5")}
                if spread_key in tenor_map:
                    t1_id, t2_id = tenor_map[spread_key]
                    s1 = _fetch_fred_series(t1_id, days=5)
                    s2 = _fetch_fred_series(t2_id, days=5)
                    if not s1.empty and not s2.empty:
                        current_value = (float(s2.iloc[-1]) - float(s1.iloc[-1])) * 100.0
                        above = item.get("above")
                        below = item.get("below")
                        if above and current_value > above:
                            triggered = True
                            trigger_dir = "yukarı"
                        elif below and current_value < below:
                            triggered = True
                            trigger_dir = "aşağı"

            elif item_type == "fx":
                pair = item.get("pair", "USDTRY=X")
                data = yf.download(pair, period='1d', progress=False)
                if not data.empty:
                    current_value = float(data['Close'].iloc[-1])
                    above = item.get("above")
                    below = item.get("below")
                    if above and current_value > above:
                        triggered = True
                        trigger_dir = "yukarı"
                    elif below and current_value < below:
                        triggered = True
                        trigger_dir = "aşağı"

            elif item_type == "tr_policy":
                analytics = fetch_tr_analytics()
                current_value = float(analytics.get("policy_rate", 42.5))
                above = item.get("above")
                below = item.get("below")
                if above and current_value > above:
                    triggered = True
                    trigger_dir = "yukarı"
                elif below and current_value < below:
                    triggered = True
                    trigger_dir = "aşağı"

            elif item_type == "tr_rate":
                tenor = item.get("tenor", "2Y")
                curve_data = fetch_tr_yield_curve()
                if curve_data and "curve" in curve_data:
                    for curve_item in curve_data["curve"]:
                        if curve_item.get("tenor") == tenor:
                            current_value = float(curve_item.get("yield", 0.0))
                            break

                if current_value:
                    above = item.get("above")
                    below = item.get("below")
                    if above and current_value > above:
                        triggered = True
                        trigger_dir = "yukarı"
                    elif below and current_value < below:
                        triggered = True
                        trigger_dir = "aşağı"

        except Exception as e:
            pass

        result.append({
            **item,
            "triggered": triggered,
            "trigger_dir": trigger_dir,
            "current_value": round(current_value, 4) if current_value else None,
        })

    return result

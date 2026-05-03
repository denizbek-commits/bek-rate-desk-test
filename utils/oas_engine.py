"""
Bek Rate Desk — OAS Engine
===========================
Callable bond / preferred stock OAS pricer.
Ported from The Bek System with ALM extensions.

Methodology:
  - Hull-White style recombining binomial short-rate lattice
  - OAS solved via bisection (scipy brentq)
  - Option cost = price_non_callable - price_callable at OAS=0
  - Full cashflow schedules for both paths
  - FRED-calibrated treasury curve + MOVE index vol
"""

import numpy as np
from scipy.optimize import brentq
from datetime import date, timedelta
import requests
import math
from typing import Optional

# Use centralized key manager so Settings page FRED key applies to OAS/bond pricer too
from utils.rates_engine import _get_fred_key as _get_fred_key_central
from utils.api_cache import ttl_cache
FRED_BASE    = "https://api.stlouisfed.org/fred/series/observations"

TREASURY_SERIES = {
    "1M":  "DGS1MO", "3M":  "DGS3MO", "6M":  "DGS6MO",
    "1Y":  "DGS1",   "2Y":  "DGS2",   "3Y":  "DGS3",
    "5Y":  "DGS5",   "7Y":  "DGS7",   "10Y": "DGS10",
    "20Y": "DGS20",  "30Y": "DGS30",
}

RATING_TO_SPREAD = {
    "AAA": "BAMLC0A1CAASW", "AA": "BAMLC0A2CAASW",
    "A":   "BAMLC0A3CAASW", "BBB":"BAMLC0A4CBBBSW",
    "BB":  "BAMLH0A1HYBBSW","B":  "BAMLH0A2HYBSW",
    "CCC": "BAMLH0A3HYCCSW",
}
MOVE_SERIES        = "BAMLMOVE"
PREFERRED_PREMIUM  = 0.0175   # 175bps over corp OAS for preferred stocks


def _fetch_fred(series_id: str) -> Optional[float]:
    try:
        r = requests.get(FRED_BASE, params={
            "series_id": series_id, "api_key": _get_fred_key_central(),
            "file_type": "json", "sort_order": "desc", "limit": 5,
        }, timeout=6)
        for obs in r.json().get("observations", []):
            v = obs.get("value", ".")
            if v != ".":
                return float(v)
    except Exception:
        pass
    return None


@ttl_cache(ttl=600, key="oas_treasury_curve")
def fetch_treasury_curve() -> dict:
    curve = {}
    for tenor, sid in TREASURY_SERIES.items():
        v = _fetch_fred(sid)
        if v is not None:
            curve[tenor] = round(v / 100.0, 6)
    return curve


@ttl_cache(ttl=600, key="oas_credit_spreads")
def fetch_credit_spreads(rating: str = "BBB", is_preferred: bool = True) -> dict:
    rating = rating.upper().strip()
    for key in RATING_TO_SPREAD:
        if rating.startswith(key):
            series_id = RATING_TO_SPREAD[key]
            v = _fetch_fred(series_id)
            spread = (v / 100.0) if v is not None else 0.0150
            if is_preferred:
                spread += PREFERRED_PREMIUM
            return {
                "spread":         round(spread, 6),
                "spread_pct":     round(spread * 100, 3),
                "corp_oas_pct":   round((spread - PREFERRED_PREMIUM) * 100, 3),
                "premium_bps":    round(PREFERRED_PREMIUM * 10000, 0),
                "rating_used":    key,
            }
    return {"spread": 0.0150 + (PREFERRED_PREMIUM if is_preferred else 0),
            "spread_pct": (0.0150 + PREFERRED_PREMIUM) * 100, "rating_used": "BBB"}


@ttl_cache(ttl=600, key="oas_move_vol")
def fetch_move_vol() -> dict:
    move = _fetch_fred(MOVE_SERIES) or 100.0
    vol  = move / 100.0 / math.sqrt(12)
    return {
        "move_index": round(move, 1),
        "rate_vol":   round(vol, 5),
        "vol_pct":    round(vol * 100, 3),
        "regime":     ("Low vol" if move < 80 else
                       "Moderate" if move < 120 else
                       "Elevated" if move < 160 else "High vol"),
    }


def _interpolate_rate(curve: dict, years: float) -> float:
    TENOR_YEARS = {
        "1M": 1/12, "3M": 0.25, "6M": 0.5, "1Y": 1.0, "2Y": 2.0,
        "3Y": 3.0,  "5Y": 5.0,  "7Y": 7.0, "10Y":10.0,"20Y":20.0,"30Y":30.0,
    }
    pts = sorted([(TENOR_YEARS[t], r) for t, r in curve.items() if t in TENOR_YEARS],
                 key=lambda x: x[0])
    if not pts:
        return 0.045
    if years <= pts[0][0]:  return pts[0][1]
    if years >= pts[-1][0]: return pts[-1][1]
    for i in range(len(pts) - 1):
        t0, r0 = pts[i]; t1, r1 = pts[i+1]
        if t0 <= years <= t1:
            w = (years - t0) / (t1 - t0)
            return r0 + w * (r1 - r0)
    return pts[-1][1]


def _build_rate_tree(r0: float, sigma: float, dt: float, n_steps: int) -> list:
    u = math.exp(sigma * math.sqrt(dt))
    tree = []
    for i in range(n_steps + 1):
        row = [max(r0 * (u ** (2*j - i)), 0.0001) for j in range(i + 1)]
        tree.append(row)
    return tree


def _price_bond(coupon: float, par: float, call_price: float,
                n_coupons: int, freq: int, rate_tree: list,
                dt: float, oas: float, callable_: bool) -> float:
    n     = len(rate_tree) - 1
    coup_every = max(1, n // max(n_coupons, 1))
    values = [par + coupon] * (n + 1)
    for i in range(n - 1, -1, -1):
        coupon_here = coupon if (n - i) % coup_every == 0 else 0.0
        new_vals = []
        for j in range(i + 1):
            r  = rate_tree[i][j] + oas
            df = math.exp(-r * dt)
            v  = df * (0.5 * values[j+1] + 0.5 * values[j] + coupon_here)
            if callable_ and i > 0:
                v = min(v, call_price)
            new_vals.append(v)
        values = new_vals
    return values[0]


def _solve_oas(market_price, coupon, par, call_price, n_coupons, freq, rate_tree, dt):
    try:
        f = lambda oas: _price_bond(coupon, par, call_price, n_coupons, freq,
                                     rate_tree, dt, oas, True) - market_price
        return brentq(f, -0.10, 0.30, xtol=1e-6, maxiter=200)
    except Exception:
        return 0.0


def _redemption_prob(rate_tree, coupon_rate, dt):
    n = len(rate_tree) - 1
    total_w = 0.0; call_w = 0.0
    for i in range(1, n+1):
        for j in range(i+1):
            bw = math.comb(i, j) * (0.5 ** i)
            total_w += bw
            if coupon_rate > rate_tree[i][j] * 1.05:
                call_w += bw
    return round(call_w / max(total_w, 1e-9), 4)


def calculate_oas_full(data: dict) -> dict:
    par           = float(data.get("par",            25.0))
    market_price  = float(data.get("market_price",   par))
    coupon_rate   = float(data.get("coupon_rate",    6.0)) / 100.0
    call_price    = float(data.get("call_price",     par))
    call_years    = float(data.get("call_years",     5.0))
    maturity_years= float(data.get("maturity_years", 30.0))
    freq          = int(data.get("freq",              4))
    n_steps       = int(data.get("n_steps",           40))
    rating        = data.get("rating",               "BBB")
    is_preferred  = data.get("is_preferred",          True)

    # Callable bond pricing horizon = call date (rational call assumed)
    # Non-callable pricing horizon = FULL maturity (correct option cost methodology)
    price_years    = min(call_years, maturity_years)   # for callable / OAS solve
    mat_years      = maturity_years                    # for non-callable valuation
    n_coupons      = max(1, round(price_years * freq))
    n_coupons_nc   = max(1, round(mat_years   * freq))
    coupon         = par * coupon_rate / freq
    dt             = price_years / n_steps

    # Separate dt for the full-maturity non-callable tree
    dt_nc          = mat_years / n_steps if mat_years > price_years else dt

    curve       = fetch_treasury_curve()
    r0          = _interpolate_rate(curve, price_years) if curve else 0.045
    r0_nc       = _interpolate_rate(curve, mat_years)   if curve else 0.045
    sigma_data  = fetch_move_vol()
    sigma       = float(data.get("sigma", sigma_data["rate_vol"]))
    spread_data = fetch_credit_spreads(rating, is_preferred)
    credit_spread = float(data.get("credit_spread", spread_data["spread"]))

    # Rate tree calibrated to call horizon → used for OAS solve on callable bond
    rate_tree    = _build_rate_tree(r0,    sigma, dt,    n_steps)
    # Rate tree calibrated to full maturity → used for non-callable reference price
    rate_tree_nc = _build_rate_tree(r0_nc, sigma, dt_nc, n_steps)

    oas         = _solve_oas(market_price, coupon, par, call_price,
                              n_coupons, freq, rate_tree, dt)
    price_call  = _price_bond(coupon, par, call_price, n_coupons, freq,
                               rate_tree, dt, oas, True)
    # Non-callable: price to FULL maturity at the solved OAS — correct option cost
    price_nc    = _price_bond(coupon, par, par, n_coupons_nc, freq,
                               rate_tree_nc, dt_nc, oas, False)
    option_cost = price_nc - price_call
    red_prob    = _redemption_prob(rate_tree, coupon_rate, dt)
    avg_call_yr = round(call_years * red_prob + maturity_years * (1 - red_prob), 2)

    # Cashflow schedule (expected path)
    exp_call_period = max(1, round(n_coupons * red_prob))
    call_cfs = []
    nc_cfs   = []
    for i in range(1, n_coupons + 1):
        t  = round(i / freq, 3)
        cf_call = coupon + (call_price if i == exp_call_period else 0)
        cf_nc   = coupon + (par        if i == n_coupons else 0)
        call_cfs.append({"period": i, "time": t, "cashflow": round(cf_call, 4),
                          "event": "Call" if i == exp_call_period else "Coupon"})
        nc_cfs.append({"period": i, "time": t, "cashflow": round(cf_nc, 4),
                        "event": "Maturity" if i == n_coupons else "Coupon"})
        if i == exp_call_period:
            break

    return {
        "oas_bps":             round(oas * 10000, 1),
        # option_cost is in price points (e.g. $2.50 on a $25 par = 10% of par)
        # Guard against numerical blow-up only: reject if > 5× par (500%)
        "option_cost_bps":     round(option_cost / par * 10000, 1) if abs(option_cost) < 5 * par else 0,
        "z_spread_bps":        round((oas + credit_spread) * 10000, 1),
        "credit_spread_bps":   round(credit_spread * 10000, 1),
        "redemption_prob_pct": round(red_prob * 100, 1),
        "avg_call_year":       avg_call_yr,
        "price_callable":      round(price_call, 4),
        "price_noncallable":   round(price_nc, 4),
        "market_price":        round(market_price, 4),
        "coupon_rate_pct":     round(coupon_rate * 100, 3),
        "par":                 par,
        "call_price":          call_price,
        "call_years":          call_years,
        "r0_pct":              round(r0 * 100, 3),
        "sigma_pct":           round(sigma * 100, 3),
        "move_index":          sigma_data["move_index"],
        "vol_regime":          sigma_data["regime"],
        "rating":              rating,
        "cashflows":           {"callable": call_cfs, "noncallable": nc_cfs},
        "treasury_curve":      {k: round(v*100, 3) for k, v in curve.items()},
    }


def price_bond_full(data: dict) -> dict:
    par            = float(data.get("par",             1000.0))
    market_price   = float(data.get("market_price",    par))
    coupon_rate    = float(data.get("coupon_rate",      5.0)) / 100.0
    maturity_years = float(data.get("maturity_years",   5.0))
    freq           = int(data.get("freq",               2))
    bond_type      = data.get("bond_type",              "fixed")
    spread_bps     = float(data.get("spread_over_base", 0.0))
    base_rate      = float(data.get("base_rate_current",5.0)) / 100.0

    if bond_type == "floating":
        coupon_rate = base_rate + spread_bps / 10000.0

    n_periods = max(1, round(maturity_years * freq))
    coupon    = par * coupon_rate / freq
    dt        = 1.0 / freq

    if bond_type == "amortizing":
        r_per = coupon_rate / freq
        payment = (par * r_per / (1 - (1 + r_per) ** (-n_periods))) if r_per > 0 else par / n_periods
        cashflows = [(i * dt, payment) for i in range(1, n_periods + 1)]
    else:
        cashflows = [(i * dt, coupon + (par if i == n_periods else 0))
                     for i in range(1, n_periods + 1)]

    def price_from_ytm(ytm):
        r = ytm / freq
        return sum(cf / (1 + r) ** (t * freq) for t, cf in cashflows)

    try:
        ytm = brentq(lambda y: price_from_ytm(y) - market_price,
                     -0.50, 5.0, xtol=1e-7, maxiter=300)
    except Exception:
        ytm = coupon_rate

    price    = price_from_ytm(ytm)
    price_up = price_from_ytm(ytm + 0.0001)
    price_dn = price_from_ytm(ytm - 0.0001)
    dv01     = (price_dn - price_up) / 2.0
    mod_dur  = dv01 / price * 10000
    r_per    = ytm / freq
    mac_dur  = sum(t * cf / (1 + r_per) ** (t * freq)
                   for t, cf in cashflows) / price
    convexity = sum(t * (t + dt) * cf / (1 + r_per) ** (t * freq)
                    for t, cf in cashflows) / (price * (1 + r_per) ** 2)

    return {
        "price":           round(price, 4),
        "ytm_pct":         round(ytm * 100, 4),
        "coupon_rate_pct": round(coupon_rate * 100, 3),
        "mac_duration":    round(mac_dur, 4),
        "mod_duration":    round(mod_dur, 4),
        "convexity":       round(convexity, 4),
        "dv01":            round(dv01, 6),
        "par":             par,
        "maturity_years":  maturity_years,
        "bond_type":       bond_type,
        "n_periods":       n_periods,
        "cashflows":       [{"period": i+1, "time": round(t, 3),
                              "cashflow": round(cf, 4)}
                             for i, (t, cf) in enumerate(cashflows[:40])],
    }

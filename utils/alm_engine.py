"""
Bek Rate Desk — ALM Engine
===========================
Core analytics for all ALM modules.
  - NII Sensitivity & Repricing Gap
  - EVE / Duration Gap (IRRBB)
  - FX Gap (Turkish context)
  - Liquidity Ratios (LCR / NSFR)
  - FTP Curve Construction
"""

import numpy as np
import pandas as pd
import requests
from datetime import datetime, date
from typing import Optional
from utils.api_cache import ttl_cache

# ─── FRED Config ──────────────────────────────────────────────────────────────
# Use centralized key manager (reads env → config.json → hardcoded default)
# This ensures Settings page changes to FRED key take effect in all ALM calcs.
from utils.rates_engine import _get_fred_key as _get_fred_key_central
FRED_BASE = "https://api.stlouisfed.org/fred/series/observations"

TREASURY_SERIES = {
    "ON":  "DFF",       # Fed Funds (overnight proxy)
    "1M":  "DGS1MO",
    "3M":  "DGS3MO",
    "6M":  "DGS6MO",
    "1Y":  "DGS1",
    "2Y":  "DGS2",
    "3Y":  "DGS3",
    "5Y":  "DGS5",
    "7Y":  "DGS7",
    "10Y": "DGS10",
    "20Y": "DGS20",
    "30Y": "DGS30",
}

# Repricing buckets used across all ALM modules (in months)
REPRICING_BUCKETS = [
    ("Overnight",  0,    1),
    ("1M",         1,    3),
    ("3M",         3,    6),
    ("6M",         6,   12),
    ("1Y",        12,   24),
    ("2Y",        24,   60),
    ("5Y+",       60, 9999),
]

def _fetch_fred(series_id: str) -> Optional[float]:
    """Fetch most recent value from FRED using the centralized key manager."""
    try:
        r = requests.get(FRED_BASE, params={
            "series_id": series_id,
            "api_key":   _get_fred_key_central(),
            "file_type": "json",
            "sort_order":"desc",
            "limit":     5,
        }, timeout=6)
        for obs in r.json().get("observations", []):
            v = obs.get("value", ".")
            if v != ".":
                return float(v) / 100.0   # convert % to decimal
    except Exception:
        pass
    return None


# ══════════════════════════════════════════════════════════════════════════════
# MODULE 1 — NII SENSITIVITY & REPRICING GAP
# ══════════════════════════════════════════════════════════════════════════════

def run_nii_sensitivity(data: dict) -> dict:
    """
    Computes NII sensitivity across rate shock scenarios.

    Input (data dict):
      assets: list of {name, amount, rate, repricing_months, type: fixed|floating}
      liabilities: list of {name, amount, rate, repricing_months, type: fixed|floating}
      base_rate: float (%)           — current base rate (e.g. policy rate)
      horizon_months: int            — NII horizon (default 12)
      turkish_context: bool          — use TCMB rate if True
      scenarios: list[str]           — e.g. ["parallel_up_100", "parallel_up_200",
                                             "parallel_down_100", "bear_steepen",
                                             "bull_flatten"]

    Output:
      base_nii, repricing_gap_table, scenario_results, summary_stats
    """
    from utils.tcmb_evds import get_policy_rate as _get_pr
    assets      = data.get("assets",      [])
    liabilities = data.get("liabilities", [])
    base_rate   = float(data.get("base_rate") or _get_pr()) / 100.0
    horizon     = int(data.get("horizon_months",  12))
    scenarios   = data.get("scenarios", [
        "parallel_up_100", "parallel_up_200",
        "parallel_down_100", "parallel_down_200",
        "bear_steepen", "bull_flatten"
    ])

    # ── 1. Repricing Gap Table ─────────────────────────────────────────────
    gap_table = _build_repricing_gap(assets, liabilities)

    # ── 2. Base NII ────────────────────────────────────────────────────────
    base_nii = _compute_nii(assets, liabilities, shocks={}, horizon=horizon)

    # ── 3. Scenario NII ────────────────────────────────────────────────────
    scenario_results = {}
    for sc in scenarios:
        shocks = _build_shocks(sc, base_rate)
        sc_nii = _compute_nii(assets, liabilities, shocks=shocks, horizon=horizon)
        scenario_results[sc] = {
            "nii":          round(sc_nii, 2),
            "nii_change":   round(sc_nii - base_nii, 2),
            "nii_change_pct": round((sc_nii - base_nii) / max(abs(base_nii), 1) * 100, 2),
            "shocks":       {k: round(v * 100, 1) for k, v in shocks.items()},
        }

    # ── 4. Cumulative RSA / RSL / Gap by bucket ────────────────────────────
    cum_gap    = 0.0
    cum_table  = []
    for bucket in gap_table:
        cum_gap += bucket["gap"]
        cum_table.append({**bucket, "cumulative_gap": round(cum_gap, 2)})

    # ── 5. NII-at-Risk (simplified — max adverse scenario) ─────────────────
    nii_changes = [v["nii_change"] for v in scenario_results.values()]
    nii_at_risk = min(nii_changes) if nii_changes else 0.0

    return {
        "base_nii":        round(base_nii, 2),
        "nii_at_risk":     round(nii_at_risk, 2),
        "repricing_gap":   cum_table,
        "scenarios":       scenario_results,
        "total_assets":    round(sum(a["amount"] for a in assets), 2),
        "total_liabilities": round(sum(l["amount"] for l in liabilities), 2),
        "horizon_months":  horizon,
    }


def _build_repricing_gap(assets: list, liabilities: list) -> list:
    """
    Distributes assets and liabilities into repricing buckets.
    Returns list of {bucket, rsa, rsl, gap, avg_asset_rate, avg_liability_rate}.
    """
    rows = []
    for (label, lo, hi) in REPRICING_BUCKETS:
        rsa = sum(
            a["amount"] for a in assets
            if _is_rate_sensitive(a, lo, hi)
        )
        rsl = sum(
            l["amount"] for l in liabilities
            if _is_rate_sensitive(l, lo, hi)
        )

        # Volume-weighted average rates
        rsa_total = rsa or 1
        rsl_total = rsl or 1
        avg_ar = sum(
            a["amount"] * float(a.get("rate", 0)) / 100 for a in assets
            if _is_rate_sensitive(a, lo, hi)
        ) / rsa_total
        avg_lr = sum(
            l["amount"] * float(l.get("rate", 0)) / 100 for l in liabilities
            if _is_rate_sensitive(l, lo, hi)
        ) / rsl_total

        rows.append({
            "bucket":           label,
            "rsa":              round(rsa, 2),
            "rsl":              round(rsl, 2),
            "gap":              round(rsa - rsl, 2),
            "avg_asset_rate":   round(avg_ar * 100, 3),
            "avg_liab_rate":    round(avg_lr * 100, 3),
        })
    return rows


def _floating_reset_months(item: dict) -> float:
    """
    Return the effective repricing horizon (in months) for a floating-rate item.

    Priority:
      1. item["reset_frequency_months"]  — explicit reset period set by the user
         (e.g. 1 for monthly TLREF, 3 for quarterly EURIBOR, 12 for annual CPI)
      2. item["repricing_months"]        — legacy field, honoured for compatibility
      3. Default → 0 (Overnight / demand deposit — reprices daily)

    This allows granular bucket placement instead of lumping every floating
    instrument into the Overnight bucket.
    """
    reset = item.get("reset_frequency_months")
    if reset is not None:
        return float(reset)
    rp = item.get("repricing_months")
    if rp is not None:
        return float(rp)
    return 0.0  # Overnight default


def _is_rate_sensitive(item: dict, lo_months: int, hi_months: int) -> bool:
    """
    An item is rate-sensitive in a bucket if:
      - It's floating rate and its reset frequency falls within [lo, hi), OR
      - It's fixed rate and its repricing_months falls within [lo, hi)

    Floating-rate handling:
      reset_frequency_months=0  (or unset) → Overnight bucket  (demand deposits, O/N REPO)
      reset_frequency_months=1             → 1M bucket         (monthly TLREF loans)
      reset_frequency_months=3             → 3M bucket         (quarterly LIBOR/EURIBOR)
      reset_frequency_months=6             → 6M bucket         (semi-annual reset)
      reset_frequency_months=12            → 1Y bucket         (annual CPI-linked)
    """
    itype = item.get("type", "fixed").lower()

    if itype == "floating":
        reset_m = _floating_reset_months(item)
        return lo_months <= reset_m < hi_months

    rp = float(item.get("repricing_months", 9999))
    return lo_months <= rp < hi_months


def _resolve_shock(shocks: dict, itype: str, repricing_months: float,
                   reset_frequency_months: float = None) -> float:
    """
    Resolve the rate shock for a single instrument.

    - Floating items: shock is looked up by their reset-frequency bucket
      (monthly TLREF → 1M shock, quarterly → 3M shock, etc.).
      Falls back to Overnight, then parallel.
    - Fixed items: shock from their repricing bucket, then parallel.
    """
    if itype == "floating":
        reset_m = reset_frequency_months if reset_frequency_months is not None else 0.0
        bucket  = _get_bucket_label(reset_m)
        return shocks.get(bucket, shocks.get("Overnight", shocks.get("parallel", 0.0)))
    bucket = _get_bucket_label(repricing_months)
    return shocks.get(bucket, shocks.get("parallel", 0.0))


def _compute_nii(assets: list, liabilities: list, shocks: dict,
                 horizon: int = 12) -> float:
    """
    Computes NII = sum(asset interest income) - sum(liability interest expense)
    under a given rate shock scenario over [horizon] months.

    Intra-period repricing (fixed instruments):
      - Before repricing date:  earns/pays the original contractual rate
      - After repricing date:   earns/pays (rate + shock)
      - Pre-shock income  = amount * rate        * (rp / 12)
      - Post-shock income = amount * (rate+shock) * ((horizon - rp) / 12)
      This correctly weights the shock impact by the fraction of the horizon
      that actually runs at the new rate.  The previous flat time_fraction
      approach overstated sensitivity for short-dated fixed instruments.

    Floating items reprice continuously → full horizon at (rate + shock).
    """
    nii = 0.0

    for a in assets:
        rate    = float(a.get("rate",   0)) / 100.0
        amount  = float(a.get("amount", 0))
        itype   = a.get("type", "fixed").lower()
        rp      = float(a.get("repricing_months", 9999))
        reset_m = _floating_reset_months(a) if itype == "floating" else None

        if itype == "floating":
            shock = _resolve_shock(shocks, itype, rp, reset_m)
            nii  += amount * (rate + shock) * (horizon / 12.0)
        elif rp <= horizon:
            shock       = _resolve_shock(shocks, itype, rp)
            pre_months  = min(rp, horizon)
            post_months = horizon - pre_months
            nii += amount * rate         * (pre_months  / 12.0)
            nii += amount * (rate+shock) * (post_months / 12.0)
        else:
            # Reprices outside horizon — pure contractual income, no shock
            nii += amount * rate * (horizon / 12.0)

    for l in liabilities:
        rate    = float(l.get("rate",   0)) / 100.0
        amount  = float(l.get("amount", 0))
        itype   = l.get("type", "fixed").lower()
        rp      = float(l.get("repricing_months", 9999))
        reset_m = _floating_reset_months(l) if itype == "floating" else None

        if itype == "floating":
            shock = _resolve_shock(shocks, itype, rp, reset_m)
            nii  -= amount * (rate + shock) * (horizon / 12.0)
        elif rp <= horizon:
            shock       = _resolve_shock(shocks, itype, rp)
            pre_months  = min(rp, horizon)
            post_months = horizon - pre_months
            nii -= amount * rate         * (pre_months  / 12.0)
            nii -= amount * (rate+shock) * (post_months / 12.0)
        else:
            nii -= amount * rate * (horizon / 12.0)

    return nii


def _get_bucket_label(repricing_months: float) -> str:
    for (label, lo, hi) in REPRICING_BUCKETS:
        if lo <= repricing_months < hi:
            return label
    return "5Y+"


def _build_shocks(scenario: str, base_rate: float) -> dict:
    """
    Returns a dict of {bucket: shock_in_decimal} for each scenario.

    Parallel shocks: single "parallel" key applies to all repricing items.
    Curve shocks: explicit per-bucket keys. Floating items use "Overnight".

    Bear steepen:  short end +75bps, long end +150bps
                   (short rates rise, long rates rise more → curve steepens)
    Bull steepen:  short end −150bps, long end −50bps
                   (short rates fall fast, long rates fall less → curve steepens)
    Bear flatten:  short end +150bps, long end +75bps
                   (short rates rise fast → curve flattens, most dangerous for
                    liability-sensitive banks with floating/short-term deposits)
    Bull flatten:  short end −50bps, long end −150bps
                   (long rates fall more → curve flattens)
    """
    if scenario == "parallel_up_100":
        return {"parallel": 0.01}
    elif scenario == "parallel_up_200":
        return {"parallel": 0.02}
    elif scenario == "parallel_down_100":
        return {"parallel": -0.01}
    elif scenario == "parallel_down_200":
        return {"parallel": -0.02}
    elif scenario == "bear_steepen":
        # Short end +75bps, long end +150bps
        # Floating assets AND short deposits both get hit, but less than long end
        return {
            "Overnight": 0.0075,
            "1M":        0.0075,
            "3M":        0.0100,
            "6M":        0.0110,
            "1Y":        0.0120,
            "2Y":        0.0135,
            "5Y+":       0.0150,
        }
    elif scenario == "bull_flatten":
        # Short end −50bps, long end −150bps
        return {
            "Overnight": -0.0050,
            "1M":        -0.0050,
            "3M":        -0.0075,
            "6M":        -0.0100,
            "1Y":        -0.0120,
            "2Y":        -0.0140,
            "5Y+":       -0.0150,
        }
    elif scenario == "bear_flatten":
        # Short end +150bps, long end +75bps — most dangerous for liability-sensitive banks
        # Demand deposits and floating liabilities reprice sharply upward
        return {
            "Overnight":  0.0150,
            "1M":         0.0150,
            "3M":         0.0130,
            "6M":         0.0110,
            "1Y":         0.0090,
            "2Y":         0.0080,
            "5Y+":        0.0075,
        }
    elif scenario == "bull_steepen":
        # Short end −150bps, long end −50bps
        return {
            "Overnight": -0.0150,
            "1M":        -0.0150,
            "3M":        -0.0130,
            "6M":        -0.0100,
            "1Y":        -0.0075,
            "2Y":        -0.0060,
            "5Y+":       -0.0050,
        }
    else:
        return {"parallel": 0.0}


# ══════════════════════════════════════════════════════════════════════════════
# MODULE 2 — EVE / DURATION GAP  (full production build)
# ══════════════════════════════════════════════════════════════════════════════

# Basel IRRBB standard shocks (decimal, applied to discount curve)
# Source: BCBS d368 (2016), Table 2 — representative shocks for EVE
IRRBB_SHOCKS = {
    "parallel_up_200":  {"label": "+200bps Parallel",     "short": 0.020, "long": 0.020},
    "parallel_down_200":{"label": "-200bps Parallel",     "short":-0.020, "long":-0.020},
    "bear_steepen":     {"label": "Bear Steepen",          "short": 0.000, "long": 0.015},
    "bull_flatten":     {"label": "Bull Flatten",          "short":-0.015, "long": 0.000},
    "short_rate_up":    {"label": "Short Rate Up (+250bps)","short": 0.025, "long": 0.000},
    "short_rate_down":  {"label": "Short Rate Down (-250bps)","short":-0.025,"long": 0.000},
}

# Maturity cutoff: <= 2 years = "short", > 2 years = "long" for non-parallel shocks
SHORT_CUTOFF_YEARS = 2.0


def _cashflow_schedule(amount: float, coupon_rate: float, maturity_years: float,
                       freq: int = 2) -> list:
    """
    Generate cashflow schedule for a fixed-rate instrument.
    Returns list of (time_years, cashflow).

    amount       : face/notional value
    coupon_rate  : annual coupon rate (decimal)
    maturity_years: years to maturity
    freq         : coupon payments per year (2 = semi-annual, 1 = annual, 4 = quarterly)
    """
    dt       = 1.0 / freq
    coupon   = amount * coupon_rate / freq
    n_periods = max(1, round(maturity_years * freq))
    cfs = []
    for i in range(1, n_periods + 1):
        t  = i * dt
        cf = coupon + (amount if i == n_periods else 0.0)
        cfs.append((t, cf))
    return cfs


def _pv_and_duration(cashflows: list, discount_rate: float,
                     rate_shock: float = 0.0) -> tuple:
    """
    Compute PV and Macaulay duration from cashflow list.
    discount_rate: base flat discount rate (decimal)
    rate_shock:    shock to add to discount rate (decimal)

    Returns (pv, macaulay_duration, modified_duration, dv01)
    PV       = Σ CF_t / (1 + r_eff)^t
    Mac_Dur  = Σ t * PV(CF_t) / PV_total
    Mod_Dur  = Mac_Dur / (1 + r_eff)        [annual compounding]
    DV01     = -Mod_Dur * PV * 0.0001
    """
    r_eff = discount_rate + rate_shock
    if r_eff < -0.99:
        r_eff = -0.99  # floor to avoid division by zero

    pv_total     = 0.0
    weighted_t   = 0.0

    for (t, cf) in cashflows:
        df       = (1.0 + r_eff) ** (-t)
        pv_cf    = cf * df
        pv_total    += pv_cf
        weighted_t  += t * pv_cf

    if pv_total == 0:
        return 0.0, 0.0, 0.0, 0.0

    mac_dur = weighted_t / pv_total
    mod_dur = mac_dur / (1.0 + r_eff)
    dv01    = -mod_dur * pv_total * 0.0001

    return round(pv_total, 4), round(mac_dur, 4), round(mod_dur, 4), round(dv01, 4)


def _shock_for_item(maturity_years: float, shock_def: dict) -> float:
    """
    Select short or long shock based on maturity and shock definition.
    For parallel shocks short == long so it doesn't matter.
    """
    if maturity_years <= SHORT_CUTOFF_YEARS:
        return shock_def["short"]
    else:
        # Interpolate linearly between short and long for intermediate maturities
        # Short zone: <= 2Y, Long zone: >= 5Y, blend in between
        if maturity_years >= 5.0:
            return shock_def["long"]
        blend = (maturity_years - SHORT_CUTOFF_YEARS) / (5.0 - SHORT_CUTOFF_YEARS)
        return shock_def["short"] + blend * (shock_def["long"] - shock_def["short"])


def run_eve_analysis(data: dict) -> dict:
    """
    Full EVE (Economic Value of Equity) and Duration Gap analysis.
    Uses discounted cashflow pricing for each instrument.

    Input (data dict):
      assets:      list of {name, amount, rate, maturity_years, coupon_freq, type}
      liabilities: list of {name, amount, rate, maturity_years, coupon_freq, type}
      discount_rate: float (%) — base flat discount curve rate
      tier1_capital: float (M) — for regulatory limit check (BRSA: ΔEVE <= 15% Tier1)
      turkish_context: bool

    Each item:
      name           : string label
      amount         : notional/face value (M)
      rate           : coupon/interest rate (%)
      maturity_years : years to maturity (e.g. 5.0)
      coupon_freq    : payments per year (1=annual, 2=semi, 4=quarterly; default 2)
      type           : "fixed" | "floating"  (floating = duration ~ 0, reprices at par)

    Returns:
      base_eve, duration_gap, per_item details, scenario results, BRSA limit check
    """
    from utils.tcmb_evds import get_policy_rate as _get_pr
    assets       = data.get("assets",       [])
    liabilities  = data.get("liabilities",  [])
    disc_rate    = float(data.get("discount_rate") or _get_pr()) / 100.0
    tier1        = float(data.get("tier1_capital", 1500.0))
    brsa_limit   = 0.15   # ΔEVE must stay within ±15% of Tier 1

    # ── Per-item base valuation ────────────────────────────────────────────
    def value_item(item, shock=0.0):
        amount  = float(item.get("amount",        0))
        rate    = float(item.get("rate",           0)) / 100.0
        years   = float(item.get("maturity_years", 1.0))
        freq    = int(item.get("coupon_freq",       2))
        itype   = item.get("type", "fixed").lower()

        if itype == "floating":
            # Floating rate: duration ≈ time to next repricing (1 period)
            # PV ≈ par (reprices at par on next reset), duration ≈ 1/freq
            pv      = amount * (1.0 + shock * (1.0 / freq) * (-1))  # simplified sensitivity
            mac_dur = 1.0 / freq
            mod_dur = mac_dur / (1.0 + disc_rate + shock)
            dv01    = -mod_dur * pv * 0.0001
            return round(pv, 4), round(mac_dur, 4), round(mod_dur, 4), round(dv01, 4)

        cfs = _cashflow_schedule(amount, rate, years, freq)
        return _pv_and_duration(cfs, disc_rate, shock)

    asset_details = []
    for a in assets:
        pv, mac, mod, dv01 = value_item(a)
        years = float(a.get("maturity_years", 1.0))
        asset_details.append({
            "name":         a.get("name", ""),
            "amount":       float(a.get("amount", 0)),
            "rate":         float(a.get("rate", 0)),
            "maturity_years": years,
            "type":         a.get("type", "fixed"),
            "pv":           pv,
            "mac_duration": mac,
            "mod_duration": mod,
            "dv01":         dv01,
        })

    liab_details = []
    for l in liabilities:
        pv, mac, mod, dv01 = value_item(l)
        years = float(l.get("maturity_years", 1.0))
        liab_details.append({
            "name":         l.get("name", ""),
            "amount":       float(l.get("amount", 0)),
            "rate":         float(l.get("rate", 0)),
            "maturity_years": years,
            "type":         l.get("type", "fixed"),
            "pv":           pv,
            "mac_duration": mac,
            "mod_duration": mod,
            "dv01":         dv01,
        })

    # ── Base EVE ───────────────────────────────────────────────────────────
    pv_assets = sum(a["pv"] for a in asset_details)
    pv_liabs  = sum(l["pv"] for l in liab_details)
    base_eve  = pv_assets - pv_liabs

    # ── Duration Gap ───────────────────────────────────────────────────────
    # D_gap = D_A - D_L * (PV_L / PV_A)
    # Weighted average modified durations
    d_assets = (sum(a["pv"] * a["mod_duration"] for a in asset_details) /
                max(pv_assets, 1))
    d_liabs  = (sum(l["pv"] * l["mod_duration"] for l in liab_details) /
                max(pv_liabs, 1))
    duration_gap = d_assets - d_liabs * (pv_liabs / max(pv_assets, 1))

    # Total portfolio DV01
    total_dv01_assets = sum(a["dv01"] for a in asset_details)
    total_dv01_liabs  = sum(l["dv01"] for l in liab_details)
    net_dv01          = total_dv01_assets - total_dv01_liabs

    # ── IRRBB Scenarios ────────────────────────────────────────────────────
    scenarios = {}
    for sc_key, shock_def in IRRBB_SHOCKS.items():
        # Re-price every item under the shock
        shocked_pv_assets = 0.0
        shocked_pv_liabs  = 0.0

        for a in assets:
            years = float(a.get("maturity_years", 1.0))
            shock = _shock_for_item(years, shock_def)
            pv, _, _, _ = value_item(a, shock)
            shocked_pv_assets += pv

        for l in liabilities:
            years = float(l.get("maturity_years", 1.0))
            shock = _shock_for_item(years, shock_def)
            pv, _, _, _ = value_item(l, shock)
            shocked_pv_liabs += pv

        shocked_eve = shocked_pv_assets - shocked_pv_liabs
        delta_eve   = shocked_eve - base_eve
        brsa_breach = abs(delta_eve) > brsa_limit * tier1

        scenarios[sc_key] = {
            "label":            shock_def["label"],
            "delta_eve":        round(delta_eve, 2),
            "shocked_eve":      round(shocked_eve, 2),
            "delta_eve_pct_equity": round(delta_eve / max(abs(base_eve), 1) * 100, 2),
            "delta_eve_pct_tier1":  round(delta_eve / max(tier1, 1) * 100, 2),
            "brsa_breach":      brsa_breach,
            "short_shock_bps":  round(shock_def["short"] * 100, 0),
            "long_shock_bps":   round(shock_def["long"] * 100, 0),
        }

    # ── Worst-case EVE at Risk ─────────────────────────────────────────────
    eve_at_risk = min(v["delta_eve"] for v in scenarios.values())
    brsa_breaches = [k for k, v in scenarios.items() if v["brsa_breach"]]

    return {
        "base_eve":          round(base_eve, 2),
        "pv_assets":         round(pv_assets, 2),
        "pv_liabilities":    round(pv_liabs, 2),
        "duration_gap":      round(duration_gap, 4),
        "d_assets":          round(d_assets, 4),
        "d_liabilities":     round(d_liabs, 4),
        "net_dv01":          round(net_dv01, 2),
        "total_dv01_assets": round(total_dv01_assets, 2),
        "total_dv01_liabs":  round(total_dv01_liabs, 2),
        "eve_at_risk":       round(eve_at_risk, 2),
        "tier1_capital":     round(tier1, 2),
        "brsa_limit_pct":    brsa_limit * 100,
        "brsa_breaches":     brsa_breaches,
        "scenarios":         scenarios,
        "asset_details":     asset_details,
        "liability_details": liab_details,
    }


# ══════════════════════════════════════════════════════════════════════════════
# MODULE 3 — FX GAP (Turkish context, configurable)
# ══════════════════════════════════════════════════════════════════════════════

# FX stress scenarios — BRSA-style shocks
FX_STRESS_SCENARIOS = {
    "usd_depreciation_20": {"label": "USD +20% (TRY depreciation)", "USD": 0.20, "EUR": 0.10},
    "usd_appreciation_20": {"label": "USD -20% (TRY appreciation)", "USD": -0.20, "EUR": -0.10},
    "eur_shock_15":        {"label": "EUR +15% shock",              "USD": 0.05, "EUR": 0.15},
    "broad_depreciation":  {"label": "Broad TRY -25%",             "USD": 0.25, "EUR": 0.25},
}


def run_fx_gap(data: dict) -> dict:
    """
    FX Gap analysis: TL/USD/EUR mismatch by tenor bucket.
    Includes:
      - Per-currency gap tables (own units + TL equivalent)
      - Consolidated TL-equivalent gap across all currencies
      - Net open position (NOP) per currency
      - FX stress P&L scenarios (BRSA-style)
      - Carry analysis: rate differential TL vs USD/EUR
      - Optional swap-adjusted positions

    Input:
      positions: list of {
        name, currency (TL|USD|EUR),
        asset_amount, liab_amount,
        tenor_months,
        swap_adjusted (bool, optional)  — if True, position already hedge-adjusted
      }
      fx_rates:       {USD: float, EUR: float}  fallback if live fetch fails
      use_live_fx:    bool
      tcmb_rate:      float (%)  TCMB overnight policy rate
      fed_rate:       float (%)  Fed Funds rate
      ecb_rate:       float (%)  ECB deposit rate
      turkish_context: bool
    """
    positions       = data.get("positions",       [])
    use_live        = data.get("use_live_fx",      True)
    tcmb_rate       = float(data.get("tcmb_rate",  42.5))
    fed_rate        = float(data.get("fed_rate",    5.25))
    ecb_rate        = float(data.get("ecb_rate",    4.00))
    turkish_context = data.get("turkish_context",  True)

    # ── FX rates ──────────────────────────────────────────────────────────
    fx_rates = {"USD": 44.85, "EUR": 52.10}   # fallback (Nisan 2026)
    fx_source = "fallback"
    if use_live:
        live = fetch_fx_rates()
        if not live.get("error") and live.get("rates"):
            r = live["rates"]
            if r.get("USD"): fx_rates["USD"] = r["USD"]
            if r.get("EUR"): fx_rates["EUR"] = r["EUR"]
            fx_source = live.get("source", "live")

    # ── Per-currency gap tables ───────────────────────────────────────────
    currencies = ["TL", "USD", "EUR"]
    ccy_data   = {}

    for ccy in currencies:
        ccy_pos = [p for p in positions if p.get("currency", "TL") == ccy]
        fx      = fx_rates.get(ccy, 1.0) if ccy != "TL" else 1.0
        buckets = []

        for (label, lo, hi) in REPRICING_BUCKETS:
            in_bucket = [p for p in ccy_pos
                         if lo <= float(p.get("tenor_months", 0)) < hi]
            a_sum = sum(float(p.get("asset_amount", 0)) for p in in_bucket)
            l_sum = sum(float(p.get("liab_amount",  0)) for p in in_bucket)
            gap   = a_sum - l_sum
            buckets.append({
                "bucket":      label,
                "assets":      round(a_sum, 2),
                "liabilities": round(l_sum, 2),
                "gap":         round(gap, 2),
                "gap_tl":      round(gap * fx, 2),
            })

        total_assets = sum(b["assets"]      for b in buckets)
        total_liabs  = sum(b["liabilities"] for b in buckets)
        total_gap    = total_assets - total_liabs   # net open position (NOP)

        ccy_data[ccy] = {
            "buckets":      buckets,
            "total_assets": round(total_assets, 2),
            "total_liabs":  round(total_liabs, 2),
            "nop":          round(total_gap, 2),           # Net Open Position
            "nop_tl":       round(total_gap * fx, 2),
            "fx_rate":      fx,
        }

    # ── Consolidated gap (TL equivalent across all CCY) ───────────────────
    consolidated = []
    for (label, lo, hi) in REPRICING_BUCKETS:
        total_gap_tl = sum(
            ccy_data[c]["buckets"][i]["gap_tl"]
            for i, (bl, _, _) in enumerate(REPRICING_BUCKETS)
            for c in currencies
            if bl == label
        )
        consolidated.append({"bucket": label, "gap_tl": round(total_gap_tl, 2)})

    # ── Carry analysis ────────────────────────────────────────────────────
    # Carry = rate_differential × NOP × time_fraction
    usd_carry_annual = (tcmb_rate - fed_rate) / 100.0 * ccy_data["USD"]["nop_tl"]
    eur_carry_annual = (tcmb_rate - ecb_rate) / 100.0 * ccy_data["EUR"]["nop_tl"]

    carry = {
        "tcmb_rate":      tcmb_rate,
        "fed_rate":       fed_rate,
        "ecb_rate":       ecb_rate,
        "usd_differential_bps": round((tcmb_rate - fed_rate) * 100, 0),
        "eur_differential_bps": round((tcmb_rate - ecb_rate) * 100, 0),
        "usd_carry_annual_tl":  round(usd_carry_annual, 2),
        "eur_carry_annual_tl":  round(eur_carry_annual, 2),
        "total_carry_annual_tl":round(usd_carry_annual + eur_carry_annual, 2),
    }

    # ── FX Stress P&L ─────────────────────────────────────────────────────
    stress_results = {}
    for sc_key, sc_def in FX_STRESS_SCENARIOS.items():
        pnl_tl = 0.0
        for ccy in ["USD", "EUR"]:
            nop     = ccy_data[ccy]["nop"]           # in own ccy
            fx_base = fx_rates[ccy]
            shock   = sc_def.get(ccy, 0.0)
            fx_new  = fx_base * (1 + shock)
            pnl_tl += nop * (fx_new - fx_base)       # gain/loss in TL

        stress_results[sc_key] = {
            "label":   sc_def["label"],
            "pnl_tl":  round(pnl_tl, 2),
            "usd_shock_pct": round(sc_def.get("USD", 0) * 100, 0),
            "eur_shock_pct": round(sc_def.get("EUR", 0) * 100, 0),
        }

    return {
        "currencies":    ccy_data,
        "consolidated":  consolidated,
        "fx_rates":      fx_rates,
        "fx_source":     fx_source,
        "carry":         carry,
        "stress":        stress_results,
        "turkish_context": turkish_context,
    }


@ttl_cache(ttl=300, key="alm_fx_rates")   # 5 min TTL — FX rates move faster
def fetch_fx_rates() -> dict:
    """Fetch USD/TRY and EUR/TRY — try multiple sources."""
    # Source 1: exchangerate-api (free, no key)
    try:
        r = requests.get(
            "https://api.exchangerate-api.com/v4/latest/TRY",
            timeout=5
        )
        rates   = r.json().get("rates", {})
        usd_try = round(1.0 / rates["USD"], 4) if rates.get("USD") else None
        eur_try = round(1.0 / rates["EUR"], 4) if rates.get("EUR") else None
        if usd_try and eur_try:
            return {"rates": {"USD": usd_try, "EUR": eur_try},
                    "source": "exchangerate-api"}
    except Exception:
        pass

    # Source 2: open.er-api.com (another free source)
    try:
        r = requests.get(
            "https://open.er-api.com/v6/latest/TRY",
            timeout=5
        )
        rates   = r.json().get("rates", {})
        usd_try = round(1.0 / rates["USD"], 4) if rates.get("USD") else None
        eur_try = round(1.0 / rates["EUR"], 4) if rates.get("EUR") else None
        if usd_try and eur_try:
            return {"rates": {"USD": usd_try, "EUR": eur_try},
                    "source": "open.er-api"}
    except Exception:
        pass

    return {"error": "Could not fetch live FX rates",
            "rates": {"USD": 44.85, "EUR": 52.10}}


# ══════════════════════════════════════════════════════════════════════════════
# MODULE 4 — LIQUIDITY RATIOS (LCR / NSFR) — full production build
# ══════════════════════════════════════════════════════════════════════════════

# Basel III / BRSA standard runoff and ASF/RSF factors
# Source: BCBS LCR standard (Jan 2013), NSFR standard (Oct 2014)

HQLA_HAIRCUTS = {
    "1":  1.00,   # Level 1 : cash, central bank reserves, sovereign bonds 0% RW
    "2a": 0.85,   # Level 2A: sovereign/agency bonds 20% RW, high-grade corp bonds
    "2b": 0.50,   # Level 2B: RMBS, lower-rated corp bonds, equities
}

# Standard LCR outflow runoff rates by category (%)
STANDARD_RUNOFF = {
    "retail_stable":        5,    # Stable retail deposits (insured)
    "retail_less_stable":  10,    # Less stable retail deposits
    "small_business":      10,    # Small business deposits
    "operational_deposits": 25,   # Operational deposits (non-financial)
    "wholesale_nonop":     100,   # Non-operational wholesale deposits
    "secured_funding":       0,   # Secured funding backed by HQLA L1
    "credit_facilities":     5,   # Committed credit facilities (retail)
    "liquidity_facilities": 30,   # Committed liquidity facilities (non-fin corp)
    "other_outflows":      100,
}

# Standard LCR inflow rates (%)
STANDARD_INFLOW = {
    "retail_loans":         50,
    "wholesale_loans":     100,
    "secured_lending_l1":    0,   # Backed by L1 HQLA — no inflow
    "other_inflows":        100,
}

# NSFR ASF factors (%)
STANDARD_ASF = {
    "tier1_capital":       100,
    "tier2_capital":       100,
    "retail_stable_12m":   95,    # Stable retail deposits >12M
    "retail_less_12m":     90,    # Less stable retail < 12M
    "wholesale_nonop_12m": 50,    # Non-operational wholesale > 6M
    "wholesale_op_12m":    50,
    "other_liabilities":    0,
}

# NSFR RSF factors (%)
STANDARD_RSF = {
    "hqla_l1":              5,    # Unencumbered L1 HQLA
    "hqla_l2a":            15,
    "hqla_l2b":            50,
    "loans_residential":   65,    # Performing residential mortgages
    "loans_corporate_1y":  50,    # Performing corp loans <1Y
    "loans_corporate_1y+": 85,    # Performing corp loans >1Y
    "other_assets":       100,
    "off_balance":          5,    # Committed credit/liquidity facilities
}


def run_liquidity_ratios(data: dict) -> dict:
    """
    LCR  = HQLA / Net Cash Outflows (30-day stress)  ≥ 100%
    NSFR = Available Stable Funding / Required Stable Funding  ≥ 100%

    Input:
      hqla:     [{name, amount, level: '1'|'2a'|'2b', description}]
      outflows: [{name, amount, runoff_rate (%), category, description}]
      inflows:  [{name, amount, inflow_rate (%),  category, description}]
      asf:      [{name, amount, asf_factor (%), category}]
      rsf:      [{name, amount, rsf_factor (%),  category}]
      tier1_capital: float (M)  — for regulatory context display
    """
    hqla_items = data.get("hqla",     [])
    outflows   = data.get("outflows", [])
    inflows    = data.get("inflows",  [])
    asf_items  = data.get("asf",      [])
    rsf_items  = data.get("rsf",      [])
    tier1      = float(data.get("tier1_capital", 1500.0))

    # ── LCR ───────────────────────────────────────────────────────────────
    hqla_detail = []
    for item in hqla_items:
        amt     = float(item.get("amount",  0))
        level   = str(item.get("level", "1")).lower()
        haircut = HQLA_HAIRCUTS.get(level, 1.0)
        haircut_pct = round((1 - haircut) * 100, 0)
        adj_amt = amt * haircut
        hqla_detail.append({
            "name":        item.get("name", ""),
            "amount":      round(amt, 2),
            "level":       level.upper(),
            "haircut_pct": haircut_pct,
            "adj_amount":  round(adj_amt, 2),
        })

    hqla_l1  = sum(d["adj_amount"] for d in hqla_detail if d["level"] == "1")
    hqla_l2a = sum(d["adj_amount"] for d in hqla_detail if d["level"] == "2A")
    hqla_l2b = sum(d["adj_amount"] for d in hqla_detail if d["level"] == "2B")

    # L2 cap: max 40% of total HQLA; L2B cap: max 15%
    hqla_total_uncapped = hqla_l1 + hqla_l2a + hqla_l2b
    l2_cap   = 0.40 * hqla_total_uncapped
    l2b_cap  = 0.15 * hqla_total_uncapped
    hqla_l2a_capped = min(hqla_l2a, l2_cap)
    hqla_l2b_capped = min(hqla_l2b, l2b_cap)
    hqla_total = hqla_l1 + hqla_l2a_capped + hqla_l2b_capped

    outflow_detail = []
    for item in outflows:
        amt      = float(item.get("amount",      0))
        rr       = float(item.get("runoff_rate", 0))
        stressed = amt * rr / 100.0
        outflow_detail.append({
            "name":         item.get("name", ""),
            "amount":       round(amt, 2),
            "runoff_rate":  rr,
            "stressed_outflow": round(stressed, 2),
            "category":     item.get("category", ""),
        })

    inflow_detail = []
    for item in inflows:
        amt      = float(item.get("amount",      0))
        ir       = float(item.get("inflow_rate", 0))
        stressed = amt * ir / 100.0
        inflow_detail.append({
            "name":           item.get("name", ""),
            "amount":         round(amt, 2),
            "inflow_rate":    ir,
            "stressed_inflow": round(stressed, 2),
            "category":       item.get("category", ""),
        })

    gross_outflows = sum(d["stressed_outflow"] for d in outflow_detail)
    gross_inflows_raw = sum(d["stressed_inflow"] for d in inflow_detail)
    inflow_cap    = 0.75 * gross_outflows
    gross_inflows = min(gross_inflows_raw, inflow_cap)
    net_outflows  = max(gross_outflows - gross_inflows, 0.01)
    lcr           = hqla_total / net_outflows * 100

    # LCR buffer: how much HQLA above/below requirement
    lcr_surplus   = hqla_total - net_outflows   # positive = surplus

    # ── NSFR ──────────────────────────────────────────────────────────────
    asf_detail = []
    for item in asf_items:
        amt    = float(item.get("amount",     0))
        factor = float(item.get("asf_factor", 0))
        asf    = amt * factor / 100.0
        asf_detail.append({
            "name":       item.get("name", ""),
            "amount":     round(amt, 2),
            "asf_factor": factor,
            "asf":        round(asf, 2),
            "category":   item.get("category", ""),
        })

    rsf_detail = []
    for item in rsf_items:
        amt    = float(item.get("amount",     0))
        factor = float(item.get("rsf_factor", 0))
        rsf    = amt * factor / 100.0
        rsf_detail.append({
            "name":       item.get("name", ""),
            "amount":     round(amt, 2),
            "rsf_factor": factor,
            "rsf":        round(rsf, 2),
            "category":   item.get("category", ""),
        })

    asf_total  = sum(d["asf"] for d in asf_detail)
    rsf_total  = sum(d["rsf"] for d in rsf_detail)
    nsfr       = (asf_total / max(rsf_total, 0.01)) * 100
    nsfr_surplus = asf_total - rsf_total

    return {
        "lcr": {
            "ratio":              round(lcr,  1),
            "hqla_total":         round(hqla_total, 2),
            "hqla_l1":            round(hqla_l1, 2),
            "hqla_l2a":           round(hqla_l2a_capped, 2),
            "hqla_l2b":           round(hqla_l2b_capped, 2),
            "gross_outflows":     round(gross_outflows, 2),
            "gross_inflows":      round(gross_inflows, 2),
            "inflow_cap":         round(inflow_cap, 2),
            "net_outflows":       round(net_outflows, 2),
            "surplus":            round(lcr_surplus, 2),
            "status":             "PASS" if lcr >= 100 else "FAIL",
            "hqla_detail":        hqla_detail,
            "outflow_detail":     outflow_detail,
            "inflow_detail":      inflow_detail,
        },
        "nsfr": {
            "ratio":              round(nsfr, 1),
            "asf_total":          round(asf_total, 2),
            "rsf_total":          round(rsf_total, 2),
            "surplus":            round(nsfr_surplus, 2),
            "status":             "PASS" if nsfr >= 100 else "FAIL",
            "asf_detail":         asf_detail,
            "rsf_detail":         rsf_detail,
        },
        "tier1_capital": round(tier1, 2),
    }


# ══════════════════════════════════════════════════════════════════════════════
# MODULE 5 — FTP CURVE (full NIM decomposition build)
# ══════════════════════════════════════════════════════════════════════════════

def run_ftp_curve(data: dict) -> dict:
    """
    Funds Transfer Pricing curve construction with full NIM decomposition.

    FTP Rate(tenor) = Base Rate + Liquidity Premium(tenor) + Optionality Charge(tenor)

    NIM decomposition per item:
      Loan:    Client Rate  = FTP Rate + Client Margin
               FTP Rate     = Base Rate + Liquidity Premium (captured by ALM)
               → NIM = Client Margin (business) + Liquidity Premium (ALM)

      Deposit: Client Rate  = FTP Rate - Client Margin
               → NIM = Client Margin (business) + Liquidity Premium (ALM)

    Turkish context:
      Base rate = TCMB overnight policy rate
      Liquidity premium = term structure over TCMB (wider for longer tenors)
      TLREF = TCMB reference rate for floating instruments

    Input:
      base_rate:          float (%)   TCMB or generic base rate
      turkish_context:    bool
      liquidity_premia:   dict {bucket: bps} — optional override
      optionality_charges: dict {bucket: bps} — for prepayable loans, callable deposits
      items: [{
        name, type (loan|deposit), amount (M),
        tenor_months, client_rate (%),
        has_optionality (bool)
      }]
    """
    from utils.tcmb_evds import get_policy_rate as _get_pr
    base_rate          = float(data.get("base_rate") or _get_pr()) / 100.0
    turkish_context    = data.get("turkish_context",    True)
    lp_override        = data.get("liquidity_premia",   None)
    opt_override       = data.get("optionality_charges",None)
    items              = data.get("items",              [])

    # ── Liquidity premium term structure ──────────────────────────────────
    # Default: Turkish bank (wider than generic due to FX premium and TCMB corridor)
    default_lp_tr = {
        "Overnight": 0,
        "1M":        30,
        "3M":        60,
        "6M":       100,
        "1Y":       150,
        "2Y":       220,
        "5Y+":      350,
    }
    default_lp_generic = {
        "Overnight":  0,
        "1M":        10,
        "3M":        20,
        "6M":        35,
        "1Y":        50,
        "2Y":        75,
        "5Y+":      120,
    }
    lp_bps = lp_override if lp_override else (
        default_lp_tr if turkish_context else default_lp_generic
    )

    # ── Optionality charge term structure ─────────────────────────────────
    # Compensates ALM for embedded options (prepayment on loans, early withdrawal on deposits)
    default_opt = {
        "Overnight":  0,
        "1M":         0,
        "3M":         5,
        "6M":        10,
        "1Y":        20,
        "2Y":        30,
        "5Y+":       50,
    }
    opt_bps = opt_override if opt_override else default_opt

    # ── FTP curve (rate per bucket) ───────────────────────────────────────
    ftp_curve = {}
    for bucket in ["Overnight", "1M", "3M", "6M", "1Y", "2Y", "5Y+"]:
        lp  = lp_bps.get(bucket, 0) / 10000.0
        opt = opt_bps.get(bucket, 0) / 10000.0
        ftp_curve[bucket] = {
            "base_rate_pct":  round(base_rate * 100, 3),
            "lp_bps":         lp_bps.get(bucket, 0),
            "opt_bps":        opt_bps.get(bucket, 0),
            "ftp_rate_pct":   round((base_rate + lp) * 100, 3),
            "ftp_rate_full_pct": round((base_rate + lp + opt) * 100, 3),
        }

    # ── Price each instrument ─────────────────────────────────────────────
    priced_items = []
    for item in items:
        tenor       = float(item.get("tenor_months", 12))
        bucket      = _get_bucket_label(tenor)
        client_rate = float(item.get("client_rate", 0)) / 100.0
        itype       = item.get("type", "loan").lower()
        amount      = float(item.get("amount", 0))
        has_opt     = item.get("has_optionality", False)

        lp_dec  = lp_bps.get(bucket,  0) / 10000.0
        opt_dec = (opt_bps.get(bucket, 0) / 10000.0) if has_opt else 0.0
        ftp_rate = base_rate + lp_dec + opt_dec

        if itype == "loan":
            # Loan: bank receives client_rate, pays FTP to ALM
            client_margin = client_rate - ftp_rate    # business unit margin
            alm_lp_margin = lp_dec                   # liquidity premium → ALM
            alm_opt_margin= opt_dec                  # optionality → ALM
            total_nim_contribution = (client_margin + lp_dec + opt_dec) * amount
        else:
            # Deposit: business unit pays client_rate, receives FTP from ALM
            client_margin = ftp_rate - client_rate    # business unit margin
            alm_lp_margin = lp_dec
            alm_opt_margin= opt_dec
            total_nim_contribution = (client_margin + lp_dec + opt_dec) * amount

        priced_items.append({
            "name":                item.get("name", ""),
            "type":                itype,
            "amount":              amount,
            "tenor_months":        tenor,
            "bucket":              bucket,
            "client_rate_pct":     round(client_rate * 100, 3),
            "ftp_rate_pct":        round(ftp_rate * 100, 3),
            "base_rate_pct":       round(base_rate * 100, 3),
            "lp_bps":              lp_bps.get(bucket, 0),
            "opt_bps":             round(opt_dec * 10000, 1),
            "client_margin_bps":   round(client_margin  * 10000, 1),
            "alm_lp_bps":          round(alm_lp_margin  * 10000, 1),
            "alm_opt_bps":         round(alm_opt_margin * 10000, 1),
            "total_spread_bps":    round((client_rate - base_rate) * 10000, 1),
            "nim_contribution_m":  round(total_nim_contribution, 3),
        })

    # ── Portfolio NIM decomposition ───────────────────────────────────────
    loans    = [i for i in priced_items if i["type"] == "loan"]
    deposits = [i for i in priced_items if i["type"] == "deposit"]

    total_loan_volume    = sum(i["amount"] for i in loans)
    total_deposit_volume = sum(i["amount"] for i in deposits)
    total_volume         = total_loan_volume + total_deposit_volume or 1

    nim_client   = sum(i["client_margin_bps"] * i["amount"] for i in priced_items) / total_volume
    nim_alm_lp   = sum(i["alm_lp_bps"]        * i["amount"] for i in priced_items) / total_volume
    nim_alm_opt  = sum(i["alm_opt_bps"]        * i["amount"] for i in priced_items) / total_volume
    nim_total    = nim_client + nim_alm_lp + nim_alm_opt

    nim_decomp = {
        "client_margin_bps":  round(nim_client,  1),
        "alm_lp_bps":         round(nim_alm_lp,  1),
        "alm_opt_bps":        round(nim_alm_opt, 1),
        "total_nim_bps":      round(nim_total,   1),
        "total_nim_m":        round(sum(i["nim_contribution_m"] for i in priced_items), 2),
        "loan_volume":        round(total_loan_volume,    2),
        "deposit_volume":     round(total_deposit_volume, 2),
    }

    return {
        "ftp_curve":       ftp_curve,
        "base_rate_pct":   round(base_rate * 100, 3),
        "turkish_context": turkish_context,
        "items":           priced_items,
        "nim_decomp":      nim_decomp,
        "lp_bps":          lp_bps,
        "opt_bps":         opt_bps,
    }

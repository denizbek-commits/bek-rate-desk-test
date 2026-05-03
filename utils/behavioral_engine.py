"""
Bek Rate Desk — Behavioral Engine (Sprint 1)
=============================================
Models customer behavior overlaid on contractual cashflows.
Without behavioral adjustments, ALM is accounting.
With them, it reflects reality.

  1. Deposit Beta Model      — pass-through speed of policy rate to deposit rates
  2. Deposit Stickiness      — behavioral maturity of non-maturity deposits
  3. Prepayment Model (CPR)  — loan early repayment driven by rate incentive
  4. Rate Sensitivity Curves — continuous NII/EVE response across shock range
"""

import numpy as np
from typing import Optional

# ──────────────────────────────────────────────────────────────────────────────
# 1. DEPOSIT BETA MODEL
# ──────────────────────────────────────────────────────────────────────────────
# Beta = fraction of policy rate move that passes through to deposit rate
# Beta 0.0 = fully sticky (deposit rate never moves)
# Beta 1.0 = fully floating (deposit rate moves 1-for-1 with policy)
#
# Turkish bank empirical estimates:
#   Demand deposits:    0.15–0.25  (customers slow to renegotiate)
#   Time deposits 3M:   0.75–0.85  (market-driven, resets at maturity)
#   Time deposits 12M:  0.50–0.65
#   Wholesale funding:  0.85–0.95  (institutional)

PRODUCT_BETA_DEFAULTS = {
    "demand_deposit":    {"beta": 0.20, "lag_months": 2,  "label": "Demand Deposits"},
    "time_deposit_3m":   {"beta": 0.80, "lag_months": 0,  "label": "Time Deposits 3M"},
    "time_deposit_6m":   {"beta": 0.70, "lag_months": 0,  "label": "Time Deposits 6M"},
    "time_deposit_12m":  {"beta": 0.60, "lag_months": 1,  "label": "Time Deposits 12M"},
    "savings":           {"beta": 0.35, "lag_months": 1,  "label": "Savings Accounts"},
    "wholesale_funding": {"beta": 0.90, "lag_months": 0,  "label": "Wholesale Funding"},
    "sub_debt":          {"beta": 0.95, "lag_months": 0,  "label": "Subordinated Debt"},
}

STICKINESS_DEFAULTS = {
    "demand_deposit":  {"core_fraction": 0.65, "behavioral_maturity_years": 4.5},
    "savings":         {"core_fraction": 0.55, "behavioral_maturity_years": 3.5},
    "time_deposit_3m": {"core_fraction": 0.00, "behavioral_maturity_years": 0.25},
    "time_deposit_6m": {"core_fraction": 0.00, "behavioral_maturity_years": 0.50},
    "time_deposit_12m":{"core_fraction": 0.00, "behavioral_maturity_years": 1.00},
    "wholesale_funding":{"core_fraction":0.10, "behavioral_maturity_years": 0.50},
}

CPR_DEFAULTS = {
    "mortgage":  {"base_cpr": 8.0,  "rate_multiplier": 2.5},
    "consumer":  {"base_cpr": 15.0, "rate_multiplier": 1.5},
    "corporate": {"base_cpr": 5.0,  "rate_multiplier": 1.0},
    "sme":       {"base_cpr": 10.0, "rate_multiplier": 1.8},
}


def run_deposit_beta_model(data: dict) -> dict:
    """
    Models how fast deposit rates follow a policy rate shock,
    accounting for pass-through speed (beta) and lag (months).
    Returns contractual vs behavioral cost comparison.

    Input:
      rate_shock_bps: float   — policy rate change in bps (e.g. +100)
      horizon_months: int     — simulation horizon (default 12)
      products: list of {
        name, product_type, balance (M), current_rate (%),
        beta (optional override), lag_months (optional override)
      }
    """
    shock_bps     = float(data.get("rate_shock_bps",   100.0))
    shock_decimal = shock_bps / 10000.0
    products      = data.get("products",                [])
    horizon       = int(data.get("horizon_months",      12))

    results = []
    total_contractual_cost = 0.0
    total_behavioral_cost  = 0.0
    total_balance          = 0.0

    for prod in products:
        ptype      = prod.get("product_type", "demand_deposit")
        defaults   = PRODUCT_BETA_DEFAULTS.get(ptype, {"beta": 0.5, "lag_months": 1})
        beta       = float(prod.get("beta",        defaults["beta"]))
        lag        = int(prod.get("lag_months",    defaults["lag_months"]))
        balance    = float(prod.get("balance",     0))
        cur_rate   = float(prod.get("current_rate",0)) / 100.0
        name       = prod.get("name", defaults.get("label", ptype))

        # Contractual: full shock, no lag
        contractual_rate = cur_rate + shock_decimal
        contractual_cost = balance * contractual_rate

        # Behavioral: beta * shock, with lag
        effective_shock  = beta * shock_decimal
        months_at_full   = max(0, horizon - lag)
        # Weighted average effective rate over horizon
        behavioral_avg_rate = (
            cur_rate * (lag / horizon) +
            (cur_rate + effective_shock) * (months_at_full / horizon)
        )
        behavioral_cost = balance * behavioral_avg_rate

        # Monthly rate path
        rate_path = []
        for m in range(1, horizon + 1):
            if m <= lag:
                r = cur_rate
            else:
                ramp = min(1.0, (m - lag) / max(3, lag + 1))
                r = cur_rate + effective_shock * ramp
            rate_path.append(round(r * 100, 4))

        results.append({
            "name":                          name,
            "product_type":                  ptype,
            "balance":                       round(balance, 2),
            "current_rate_pct":              round(cur_rate * 100, 3),
            "beta":                          beta,
            "lag_months":                    lag,
            "shock_bps":                     shock_bps,
            "effective_pass_through_bps":    round(effective_shock * 10000, 1),
            "contractual_new_rate_pct":      round(contractual_rate * 100, 3),
            "behavioral_avg_rate_pct":       round(behavioral_avg_rate * 100, 3),
            "contractual_cost_m":            round(contractual_cost, 2),
            "behavioral_cost_m":             round(behavioral_cost, 2),
            "nii_benefit_vs_contractual_m":  round(contractual_cost - behavioral_cost, 2),
            "rate_path":                     rate_path,
        })

        total_contractual_cost += contractual_cost
        total_behavioral_cost  += behavioral_cost
        total_balance          += balance

    avg_beta = (
        sum(r["beta"] * r["balance"] for r in results) / max(total_balance, 1)
    )

    return {
        "shock_bps":                shock_bps,
        "horizon_months":           horizon,
        "products":                 results,
        "total_balance_m":          round(total_balance, 2),
        "total_contractual_cost_m": round(total_contractual_cost, 2),
        "total_behavioral_cost_m":  round(total_behavioral_cost, 2),
        "total_nii_benefit_m":      round(total_contractual_cost - total_behavioral_cost, 2),
        "avg_portfolio_beta":       round(avg_beta, 4),
    }


def run_deposit_stickiness(data: dict) -> dict:
    """
    Decomposes non-maturity deposits into core (stable, long duration)
    vs volatile (rate-sensitive, short duration).

    Core deposits = stable portion that stays regardless of rate moves.
    Their behavioral duration drives EVE sensitivity much less than contractual.

    Input:
      deposits: list of {
        name, product_type, balance (M), current_rate (%),
        core_fraction (0-1, optional), behavioral_maturity_years (optional)
      }
      rate_shock_bps: float
    """
    deposits  = data.get("deposits",        [])
    shock_bps = float(data.get("rate_shock_bps", 100.0))
    shock_dec = shock_bps / 10000.0

    results = []
    total_core              = 0.0
    total_volatile          = 0.0
    total_eve_contractual   = 0.0
    total_eve_behavioral    = 0.0

    for dep in deposits:
        ptype    = dep.get("product_type", "demand_deposit")
        defs     = STICKINESS_DEFAULTS.get(ptype, {"core_fraction": 0.5, "behavioral_maturity_years": 2.0})
        balance  = float(dep.get("balance",                   0))
        core_f   = float(dep.get("core_fraction",             defs["core_fraction"]))
        beh_mat  = float(dep.get("behavioral_maturity_years", defs["behavioral_maturity_years"]))
        name     = dep.get("name", ptype)

        core_bal     = balance * core_f
        volatile_bal = balance * (1 - core_f)

        # Contractual EVE impact: demand deposits = overnight duration (~1M)
        contractual_dur    = 0.083   # ~1 month
        contractual_eve    = -contractual_dur * balance * shock_dec

        # Behavioral EVE impact
        # Core: long behavioral duration
        # Volatile: overnight duration
        behavioral_eve = (
            -beh_mat        * core_bal     * shock_dec +
            -contractual_dur * volatile_bal * shock_dec
        )

        results.append({
            "name":                          name,
            "product_type":                  ptype,
            "total_balance":                 round(balance, 2),
            "core_balance":                  round(core_bal, 2),
            "volatile_balance":              round(volatile_bal, 2),
            "core_fraction_pct":             round(core_f * 100, 1),
            "behavioral_maturity_years":     beh_mat,
            "contractual_eve_impact_m":      round(contractual_eve, 2),
            "behavioral_eve_impact_m":       round(behavioral_eve, 2),
            "eve_benefit_from_stickiness_m": round(behavioral_eve - contractual_eve, 2),
        })

        total_core           += core_bal
        total_volatile       += volatile_bal
        total_eve_contractual += contractual_eve
        total_eve_behavioral  += behavioral_eve

    return {
        "shock_bps":                     shock_bps,
        "total_core_m":                  round(total_core, 2),
        "total_volatile_m":              round(total_volatile, 2),
        "total_eve_contractual_m":       round(total_eve_contractual, 2),
        "total_eve_behavioral_m":        round(total_eve_behavioral, 2),
        "eve_benefit_from_stickiness_m": round(total_eve_behavioral - total_eve_contractual, 2),
        "products":                      results,
    }


def run_prepayment_model(data: dict) -> dict:
    """
    CPR-based prepayment model with rate-driven refinancing incentive.
    PSA ramp: CPR builds linearly over first 30 months.

    SMM = 1 - (1 - CPR)^(1/12)
    Prepayment_t = (Balance_t - Scheduled_Principal_t) * SMM

    Input:
      loans: list of {
        name, loan_type, balance (M), contract_rate (%),
        market_rate (%), original_term_months, age_months,
        base_cpr (%, optional override), use_psa (bool)
      }
    """
    loans   = data.get("loans", [])
    results = []

    for loan in loans:
        ltype       = loan.get("loan_type",             "mortgage")
        defs        = CPR_DEFAULTS.get(ltype, {"base_cpr": 8.0, "rate_multiplier": 2.0})
        balance     = float(loan.get("balance",          1000))
        contract_r  = float(loan.get("contract_rate",    8.0)) / 100.0
        market_r    = float(loan.get("market_rate",      6.0)) / 100.0
        term        = int(loan.get("original_term_months",120))
        age         = int(loan.get("age_months",          0))
        base_cpr    = float(loan.get("base_cpr", defs["base_cpr"])) / 100.0
        use_psa     = loan.get("use_psa",                True)
        rate_mult   = defs["rate_multiplier"]
        name        = loan.get("name", ltype)

        # Rate-driven CPR adjustment
        refinance_incentive = max(0.0, contract_r - market_r)
        cpr_adj = min(0.60, base_cpr * (1 + rate_mult * refinance_incentive / 0.02))

        remaining = term - age
        r_mo      = contract_r / 12.0
        if r_mo > 0:
            pmt = balance * r_mo / (1 - (1 + r_mo) ** (-remaining))
        else:
            pmt = balance / max(remaining, 1)

        bal            = balance
        monthly_data   = []
        total_prepaid  = 0.0
        total_sched    = 0.0
        balance_path   = []
        weighted_life  = 0.0

        for m in range(1, remaining + 1):
            if bal <= 0.001:
                break
            psa_f      = min(1.0, (age + m) / 30.0) if use_psa else 1.0
            monthly_cpr= cpr_adj * psa_f
            smm        = 1 - (1 - monthly_cpr) ** (1/12)

            interest   = bal * r_mo
            principal  = min(max(pmt - interest, 0), bal)
            prepayment = max(0, bal - principal) * smm
            prepayment = min(prepayment, bal - principal)

            total_sched   += principal
            total_prepaid += prepayment
            cf             = principal + prepayment
            weighted_life += m * cf
            bal            = max(0, bal - cf)

            monthly_data.append({
                "month":       m,
                "balance":     round(bal, 2),
                "scheduled":   round(principal, 4),
                "prepayment":  round(prepayment, 4),
                "interest":    round(interest, 4),
                "cpr_pct":     round(monthly_cpr * 100, 3),
            })
            balance_path.append(round(bal, 2))

        total_cf       = total_sched + total_prepaid
        eff_life       = round(weighted_life / max(total_cf, 1) / 12.0, 2)  # years
        contract_wal   = round(remaining / 2 / 12.0, 2)

        results.append({
            "name":                        name,
            "loan_type":                   ltype,
            "original_balance":            round(balance, 2),
            "contract_rate_pct":           round(contract_r * 100, 3),
            "market_rate_pct":             round(market_r * 100, 3),
            "base_cpr_pct":                round(base_cpr * 100, 3),
            "adjusted_cpr_pct":            round(cpr_adj * 100, 3),
            "refinance_incentive_bps":     round(refinance_incentive * 10000, 0),
            "effective_life_years":        eff_life,
            "contractual_wal_years":       contract_wal,
            "duration_shortening_years":   round(contract_wal - eff_life, 2),
            "total_prepaid_m":             round(total_prepaid, 2),
            "total_scheduled_m":           round(total_sched, 2),
            "monthly_data":                monthly_data[:36],
            "balance_path":                balance_path[:36],
        })

    return {"loans": results}


def run_rate_sensitivity_curves(data: dict) -> dict:
    """
    Sweeps NII across a continuous range of rate shocks.
    Shows: position, DV01, convexity, asymmetry.

    Input:
      assets, liabilities: same format as NII module
      shock_range_bps: int (default 300)
      steps: int (default 13 per side)
      horizon_months: int (default 12)
    """
    import sys, os
    sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
    from utils.alm_engine import _compute_nii

    assets      = data.get("assets",       [])
    liabilities = data.get("liabilities",  [])
    shock_range = int(data.get("shock_range_bps", 300))
    steps       = int(data.get("steps",           13))
    horizon     = int(data.get("horizon_months",  12))

    shock_pts = list(np.linspace(-shock_range, shock_range, steps * 2 + 1))

    base_nii  = _compute_nii(assets, liabilities, {"parallel": 0.0}, horizon)
    curve     = []

    for shock_bps in shock_pts:
        shock_dec = float(shock_bps) / 10000.0
        nii_val   = _compute_nii(assets, liabilities, {"parallel": shock_dec}, horizon)
        curve.append({
            "shock_bps": round(float(shock_bps), 1),
            "nii":       round(nii_val, 2),
            "delta_nii": round(nii_val - base_nii, 2),
        })

    mid       = len(curve) // 2
    dv01      = (curve[mid+1]["nii"] - curve[mid-1]["nii"]) / 2.0
    d2        = curve[mid+1]["nii"] + curve[mid-1]["nii"] - 2 * curve[mid]["nii"]
    step_dec  = (shock_pts[1] - shock_pts[0]) / 10000.0
    convexity = d2 / max(step_dec ** 2, 1e-10)

    return {
        "base_nii":       round(base_nii, 2),
        "curve":          curve,
        "dv01_approx_m":  round(dv01, 3),
        "convexity":      round(convexity, 2),
        "nii_up300_m":    curve[-1]["delta_nii"],
        "nii_down300_m":  curve[0]["delta_nii"],
        "asymmetry_m":    round(abs(curve[-1]["delta_nii"]) - abs(curve[0]["delta_nii"]), 2),
        "position":       "Asset-sensitive" if dv01 > 0 else "Liability-sensitive",
    }

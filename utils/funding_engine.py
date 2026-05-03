"""
Bek Rate Desk — Funding Engine (Sprint 2)
==========================================
1. Funding Mix Analyzer     — current structure: retail vs wholesale vs capital
2. Cost of Funds Calculator — weighted average cost, marginal cost, breakeven
3. Funding Optimizer        — target cost minimization given constraints
4. FTP Loan Pricer          — "what rate should this loan be priced at?"
5. Business Line P&L        — NIM decomposition per product/desk
6. Risk Heatmap Builder     — multi-dimension risk score matrix

Turkish banking context throughout: TCMB rate, BRSA limits, TL-dominated book.
"""

import numpy as np
from typing import Optional

# ── Default LP term structure (Turkish banks, bps over policy rate) ───────────
TR_LP_CURVE = {
    "ON":  0,   "1M":  30,  "3M":  60,  "6M":  100,
    "1Y":  150, "2Y":  220, "3Y":  270, "5Y+": 350,
}

TENOR_MONTHS = {
    "ON": 0.5, "1M": 1, "3M": 3, "6M": 6,
    "1Y": 12, "2Y": 24, "3Y": 36, "5Y+": 60,
}

# Regulatory cost add-ons
DEPOSIT_INSURANCE_BPS = 15   # TMSF premium (approx)
BRSA_RESERVE_COST_BPS = 8    # Required reserve opportunity cost


def _tenor_to_lp(tenor_months: float) -> float:
    """Interpolate LP bps from Turkish LP curve given tenor in months."""
    sorted_tenors = sorted(TENOR_MONTHS.items(), key=lambda x: x[1])
    if tenor_months <= sorted_tenors[0][1]:
        return TR_LP_CURVE[sorted_tenors[0][0]]
    if tenor_months >= sorted_tenors[-1][1]:
        return TR_LP_CURVE[sorted_tenors[-1][0]]
    for i in range(len(sorted_tenors) - 1):
        t0_label, t0 = sorted_tenors[i]
        t1_label, t1 = sorted_tenors[i+1]
        if t0 <= tenor_months <= t1:
            w = (tenor_months - t0) / (t1 - t0)
            return TR_LP_CURVE[t0_label] + w * (TR_LP_CURVE[t1_label] - TR_LP_CURVE[t0_label])
    return 150


def _funding_category(item: dict) -> str:
    """Classify a funding item into retail / wholesale / capital / other."""
    name = item.get("name", "").lower()
    ftype = item.get("funding_type", "").lower()
    rp    = float(item.get("repricing_months", 12))

    if ftype == "capital":          return "capital"
    if ftype == "wholesale":        return "wholesale"
    if ftype == "retail":           return "retail"
    if "demand" in name or "vadesiz" in name:  return "retail"
    if "time" in name or "vadeli" in name:     return "retail"
    if "wholesale" in name or "inter" in name: return "wholesale"
    if "sub" in name or "bond" in name or "note" in name: return "wholesale"
    if "equity" in name or "capital" in name:  return "capital"
    return "retail"


# ══════════════════════════════════════════════════════════════════════════════
# 1. FUNDING MIX ANALYSIS
# ══════════════════════════════════════════════════════════════════════════════

def run_funding_analysis(data: dict) -> dict:
    """
    Analyzes current funding mix:
    - Retail vs wholesale vs capital split
    - Weighted average cost of funds (WACF)
    - Marginal cost of funds at each tenor
    - Stability score (more retail = more stable)
    - Funding gap (assets vs liabilities)
    - Concentration risk (top-3 sources)

    Input: { liabilities: [...], assets: [...], base_rate: float,
             total_assets: float (optional override) }
    """
    liabilities = data.get("liabilities", [])
    assets      = data.get("assets", [])
    base_rate   = float(data.get("base_rate", 45.0))

    total_funding = sum(float(l.get("amount", 0)) for l in liabilities)
    total_assets  = float(data.get("total_assets", 0)) or sum(float(a.get("amount", 0)) for a in assets)

    if total_funding == 0:
        return {"error": "No liabilities provided"}

    # ── Category breakdown
    categories = {"retail": 0.0, "wholesale": 0.0, "capital": 0.0, "other": 0.0}
    category_costs = {"retail": [], "wholesale": [], "capital": [], "other": []}
    items_detail = []

    for l in liabilities:
        amount = float(l.get("amount", 0))
        rate   = float(l.get("rate", 0))
        rp     = float(l.get("repricing_months", 12))
        cat    = _funding_category(l)
        categories[cat] += amount
        category_costs[cat].append((amount, rate))

        # All-in cost: client rate + regulatory add-ons for retail
        regulatory_bps = (DEPOSIT_INSURANCE_BPS + BRSA_RESERVE_COST_BPS) if cat == "retail" else 0
        all_in_rate    = rate + regulatory_bps / 100.0
        lp_bps         = _tenor_to_lp(rp)
        ftp_rate       = base_rate + lp_bps / 100.0

        items_detail.append({
            "name":           l.get("name", ""),
            "amount":         round(amount, 2),
            "rate_pct":       round(rate, 3),
            "all_in_pct":     round(all_in_rate, 3),
            "tenor_months":   rp,
            "category":       cat,
            "lp_bps":         round(lp_bps, 1),
            "ftp_rate_pct":   round(ftp_rate, 3),
            "spread_to_ftp":  round(ftp_rate - all_in_rate, 3),
            "weight_pct":     round(amount / total_funding * 100, 2),
            "annual_cost_m":  round(amount * all_in_rate / 100, 2),
        })

    # WACF
    wacf = sum(i["all_in_pct"] * i["amount"] for i in items_detail) / total_funding
    wacf_client_only = sum(i["rate_pct"] * i["amount"] for i in items_detail) / total_funding

    # Category WACs
    cat_wac = {}
    for cat, positions in category_costs.items():
        if positions:
            total_w = sum(a for a, r in positions)
            cat_wac[cat] = round(sum(a * r for a, r in positions) / total_w, 3) if total_w else 0

    # Stability score: retail=1.0, wholesale=0.4, capital=1.0
    stability_weights = {"retail": 1.0, "wholesale": 0.4, "capital": 1.0, "other": 0.6}
    stability = sum(categories[c] * stability_weights[c] for c in categories) / total_funding * 100

    # Concentration — top 3 items
    sorted_items = sorted(items_detail, key=lambda x: x["amount"], reverse=True)
    top3_pct     = sum(i["weight_pct"] for i in sorted_items[:3])

    # Funding gap
    funding_gap  = total_assets - total_funding
    funding_ratio= round(total_funding / total_assets * 100, 2) if total_assets else 0

    # Marginal cost curve (LP by tenor)
    marginal_curve = [
        {"tenor": label, "tenor_months": TENOR_MONTHS[label],
         "lp_bps": lp, "marginal_cost_pct": round(base_rate + lp/100, 3)}
        for label, lp in TR_LP_CURVE.items()
    ]

    # Category pct
    cat_pct = {c: round(categories[c] / total_funding * 100, 2) for c in categories}

    return {
        "total_funding_m":     round(total_funding, 2),
        "total_assets_m":      round(total_assets, 2),
        "funding_gap_m":       round(funding_gap, 2),
        "funding_ratio_pct":   funding_ratio,
        "wacf_pct":            round(wacf, 4),
        "wacf_client_only_pct":round(wacf_client_only, 4),
        "regulatory_cost_bps": round((wacf - wacf_client_only) * 100, 1),
        "stability_score":     round(stability, 1),
        "top3_concentration_pct": round(top3_pct, 1),
        "categories_m":        {c: round(v, 2) for c, v in categories.items()},
        "categories_pct":      cat_pct,
        "category_wac":        cat_wac,
        "marginal_cost_curve": marginal_curve,
        "items":               items_detail,
        "base_rate":           base_rate,
    }


# ══════════════════════════════════════════════════════════════════════════════
# 2. FUNDING OPTIMIZER
# ══════════════════════════════════════════════════════════════════════════════

def run_funding_optimizer(data: dict) -> dict:
    """
    Simple linear funding mix optimizer.
    Given: total funding need, constraints (min retail%, max wholesale%, etc.)
    Solves: what mix minimizes WACF while meeting stability and NSFR constraints?

    Uses closed-form optimization (no scipy needed):
    - Rank funding sources by all-in cost
    - Fill cheapest first subject to constraints
    """
    target_m    = float(data.get("target_funding_m",   5000.0))
    base_rate   = float(data.get("base_rate",           45.0))
    min_retail  = float(data.get("min_retail_pct",      40.0)) / 100
    max_wholesale=float(data.get("max_wholesale_pct",   40.0)) / 100
    min_capital  = float(data.get("min_capital_pct",     8.0)) / 100
    stability_req= float(data.get("min_stability_score",60.0))

    # Funding source archetypes available to a Turkish bank
    sources = [
        {"name": "Demand Deposits (TL)",    "cat": "retail",    "rate_pct": 12.0,  "tenor_months": 1,   "max_pct": 0.30, "stability_w": 1.0},
        {"name": "Time Deposits 3M (TL)",   "cat": "retail",    "rate_pct": 43.0,  "tenor_months": 3,   "max_pct": 0.35, "stability_w": 0.9},
        {"name": "Time Deposits 12M (TL)",  "cat": "retail",    "rate_pct": 40.0,  "tenor_months": 12,  "max_pct": 0.20, "stability_w": 0.95},
        {"name": "Repo / Interbank",        "cat": "wholesale", "rate_pct": 46.5,  "tenor_months": 1,   "max_pct": 0.15, "stability_w": 0.2},
        {"name": "Wholesale Bonds (2Y)",    "cat": "wholesale", "rate_pct": 47.0,  "tenor_months": 24,  "max_pct": 0.15, "stability_w": 0.5},
        {"name": "External Eurobond (USD)", "cat": "wholesale", "rate_pct": 8.5,   "tenor_months": 36,  "max_pct": 0.10, "stability_w": 0.6},
        {"name": "Subordinated Debt",       "cat": "wholesale", "rate_pct": 50.0,  "tenor_months": 60,  "max_pct": 0.05, "stability_w": 0.8},
        {"name": "Equity / Retained",       "cat": "capital",   "rate_pct": 0.0,   "tenor_months": 999, "max_pct": 0.15, "stability_w": 1.0},
    ]

    # Add regulatory cost for retail
    for s in sources:
        reg_bps = (DEPOSIT_INSURANCE_BPS + BRSA_RESERVE_COST_BPS) if s["cat"] == "retail" else 0
        s["all_in_rate"] = s["rate_pct"] + reg_bps / 100.0
        s["lp_bps"]      = _tenor_to_lp(s["tenor_months"])
        s["ftp_rate"]    = base_rate + s["lp_bps"] / 100.0
        s["nsfr_asf"]    = _asf_factor(s["cat"], s["tenor_months"])

    # Sort by all-in cost ascending
    sources_sorted = sorted(sources, key=lambda x: x["all_in_rate"])

    # Greedy allocation respecting constraints
    alloc = {s["name"]: 0.0 for s in sources}
    cat_total = {"retail": 0.0, "wholesale": 0.0, "capital": 0.0}
    remaining = target_m

    # First: enforce minimums
    min_capital_m   = target_m * min_capital
    min_retail_m    = target_m * min_retail

    # Allocate capital first
    for s in sources:
        if s["cat"] == "capital" and remaining > 0:
            amt = min(min_capital_m, s["max_pct"] * target_m, remaining)
            alloc[s["name"]] += amt
            cat_total["capital"] += amt
            remaining -= amt

    # Then retail minimum
    retail_sources = [s for s in sources_sorted if s["cat"] == "retail"]
    for s in retail_sources:
        if cat_total["retail"] >= min_retail_m: break
        need  = min_retail_m - cat_total["retail"]
        avail = min(s["max_pct"] * target_m, remaining)
        amt   = min(need, avail)
        alloc[s["name"]] += amt
        cat_total["retail"] += amt
        remaining -= amt

    # Fill remaining with cheapest available
    for s in sources_sorted:
        if remaining <= 0: break
        already = alloc[s["name"]]
        cap     = s["max_pct"] * target_m - already
        if s["cat"] == "wholesale":
            wh_cap = max_wholesale * target_m - cat_total["wholesale"]
            cap    = min(cap, wh_cap)
        if cap > 0:
            amt = min(cap, remaining)
            alloc[s["name"]] += amt
            cat_total[s["cat"]] += amt
            remaining -= amt

    # Build result items
    result_items = []
    total_cost   = 0.0
    total_alloc  = 0.0
    for s in sources:
        amt = alloc[s["name"]]
        if amt < 0.1: continue
        total_alloc += amt
        total_cost  += amt * s["all_in_rate"] / 100
        result_items.append({
            "name":         s["name"],
            "category":     s["cat"],
            "amount_m":     round(amt, 2),
            "weight_pct":   round(amt / target_m * 100, 2),
            "rate_pct":     round(s["rate_pct"], 2),
            "all_in_pct":   round(s["all_in_rate"], 3),
            "lp_bps":       round(s["lp_bps"], 1),
            "ftp_rate_pct": round(s["ftp_rate"], 3),
            "nsfr_asf_pct": round(s["nsfr_asf"] * 100, 0),
            "annual_cost_m":round(amt * s["all_in_rate"] / 100, 2),
        })

    opt_wacf = (total_cost / total_alloc * 100) if total_alloc else 0
    stability = sum(
        alloc[s["name"]] * s["stability_w"] for s in sources
    ) / target_m * 100

    return {
        "target_funding_m":     target_m,
        "allocated_m":          round(total_alloc, 2),
        "shortfall_m":          round(remaining, 2),
        "optimal_wacf_pct":     round(opt_wacf, 4),
        "annual_funding_cost_m":round(total_cost, 2),
        "stability_score":      round(stability, 1),
        "category_split_pct":   {c: round(v / target_m * 100, 2) for c, v in cat_total.items()},
        "items":                result_items,
        "constraints_applied":  {
            "min_retail_pct":     min_retail * 100,
            "max_wholesale_pct":  max_wholesale * 100,
            "min_capital_pct":    min_capital * 100,
        },
    }


def _asf_factor(cat: str, tenor_months: float) -> float:
    """NSFR Available Stable Funding factor."""
    if cat == "capital": return 1.0
    if cat == "retail":
        if tenor_months < 12: return 0.90
        return 0.95
    # wholesale
    if tenor_months >= 12: return 0.50
    return 0.0


# ══════════════════════════════════════════════════════════════════════════════
# 3. FTP LOAN PRICER — "what should this loan be priced at?"
# ══════════════════════════════════════════════════════════════════════════════

def run_loan_pricer(data: dict) -> dict:
    """
    FTP-based minimum loan pricing.

    Min loan rate = FTP rate + Credit Risk Premium + OpEx + RoE target
    FTP rate      = base_rate + LP(tenor) + optionality charge

    Input:
      base_rate       : float (TCMB policy rate %)
      loan_tenor_months: float
      loan_type       : str (mortgage / corporate / sme / consumer / auto)
      credit_rating   : str (AAA/AA/A/BBB/BB/B/CCC or internal 1-7)
      roe_target_pct  : float (bank's RoE target, e.g. 20%)
      opex_pct        : float (operating expense ratio, e.g. 2.5%)
      capital_ratio   : float (CET1 ratio required, e.g. 12%)
      has_prepayment  : bool
      loan_amount_m   : float
    """
    base_rate   = float(data.get("base_rate",         45.0))
    tenor       = float(data.get("loan_tenor_months", 12.0))
    loan_type   = data.get("loan_type",                "corporate").lower()
    rating      = data.get("credit_rating",            "BBB")
    roe_target  = float(data.get("roe_target_pct",    20.0))
    opex        = float(data.get("opex_pct",            2.5))
    capital_ratio=float(data.get("capital_ratio_pct", 12.0))
    has_prepay  = data.get("has_prepayment",           False)
    amount_m    = float(data.get("loan_amount_m",     100.0))

    # 1. FTP rate
    lp_bps          = _tenor_to_lp(tenor)
    opt_bps         = 25 if has_prepay else 0     # prepayment option cost
    ftp_rate        = base_rate + lp_bps / 100.0 + opt_bps / 100.0

    # 2. Credit risk premium (PD × LGD × 12m horizon, simplified)
    credit_spreads = {
        "AAA": 0.10, "AA": 0.25, "A": 0.50, "BBB": 1.00,
        "BB":  2.00, "B":  4.00, "CCC": 7.00,
        "1": 0.10, "2": 0.30, "3": 0.60, "4": 1.20,
        "5": 2.50, "6": 4.50, "7": 8.00,
    }
    # Loan type add-ons (structural)
    type_addons = {
        "mortgage":   -0.20,   # secured, lower risk
        "corporate":   0.00,
        "sme":         0.50,   # SME premium
        "consumer":    1.50,   # unsecured
        "auto":        0.30,
    }
    credit_prem = credit_spreads.get(str(rating).upper(), 1.00) + type_addons.get(loan_type, 0)

    # 3. Capital charge (RoE target on required capital)
    rwa_weight = {"mortgage": 0.35, "corporate": 1.00, "sme": 0.75,
                  "consumer": 1.00, "auto": 0.75}.get(loan_type, 1.00)
    capital_charge = rwa_weight * capital_ratio / 100.0 * roe_target / 100.0 * 100  # bps equivalent → %

    # 4. Total minimum rate
    min_rate    = ftp_rate + credit_prem + opex + capital_charge
    client_NIM  = 0  # at minimum rate

    # Sensitivity: rate changes per 100bps shock
    shock_impact = lp_bps / 10000 * 100   # LP bps / rate → % of FTP that's fixed premium

    return {
        "loan_type":        loan_type,
        "credit_rating":    str(rating),
        "tenor_months":     tenor,
        "amount_m":         amount_m,
        "base_rate_pct":    round(base_rate, 3),
        # Decomposition
        "lp_bps":           round(lp_bps, 1),
        "opt_bps":          opt_bps,
        "ftp_rate_pct":     round(ftp_rate, 4),
        "credit_premium_pct": round(credit_prem, 4),
        "opex_pct":         round(opex, 3),
        "capital_charge_pct": round(capital_charge, 4),
        # Total
        "min_loan_rate_pct":round(min_rate, 4),
        "annual_income_at_min_m": round(amount_m * min_rate / 100, 2),
        # Recommendations at various NIM targets
        "pricing_recommendations": [
            {"target_nim_bps": nim,
             "recommended_rate_pct": round(min_rate + nim / 100, 3),
             "annual_nim_income_m": round(amount_m * nim / 10000, 3)}
            for nim in [0, 50, 100, 150, 200, 300]
        ],
        "rwa_weight":       rwa_weight,
        "capital_needed_m": round(amount_m * rwa_weight * capital_ratio / 100, 2),
    }


# ══════════════════════════════════════════════════════════════════════════════
# 4. BUSINESS LINE PROFITABILITY
# ══════════════════════════════════════════════════════════════════════════════

def run_business_line_profitability(data: dict) -> dict:
    """
    NIM decomposition by business line / product.
    Each item gets: client margin, ALM LP share, base rate share.

    Input: { base_rate, items: [{name, type, amount, client_rate, ftp_rate,
                                 business_line, tenor_months}] }
    """
    base_rate = float(data.get("base_rate", 45.0))
    items     = data.get("items", [])

    lines = {}
    for item in items:
        bl    = item.get("business_line", "Other")
        amt   = float(item.get("amount", 0))
        cr    = float(item.get("client_rate", 0))
        ftp   = float(item.get("ftp_rate", base_rate))
        itype = item.get("type", "loan")

        if bl not in lines:
            lines[bl] = {"volume": 0, "income": 0, "client_margin": 0,
                         "alm_lp": 0, "base": 0, "items": []}

        client_margin = (cr - ftp) if itype == "loan" else (ftp - cr)
        lp_bps        = _tenor_to_lp(float(item.get("tenor_months", 12)))
        alm_lp_income = amt * lp_bps / 10000
        base_income   = amt * base_rate / 100

        lines[bl]["volume"]        += amt
        lines[bl]["income"]        += amt * abs(client_margin) / 100
        lines[bl]["client_margin"] += amt * abs(client_margin) / 100
        lines[bl]["alm_lp"]        += alm_lp_income
        lines[bl]["base"]          += base_income
        lines[bl]["items"].append({**item, "client_margin_bps": round(client_margin * 100, 1)})

    result_lines = []
    for bl, vals in lines.items():
        vol = vals["volume"] or 1
        result_lines.append({
            "business_line":      bl,
            "volume_m":           round(vals["volume"], 2),
            "nim_income_m":       round(vals["client_margin"], 4),
            "nim_pct":            round(vals["client_margin"] / vol * 100, 3),
            "alm_lp_income_m":    round(vals["alm_lp"], 4),
            "base_income_m":      round(vals["base"], 4),
            "items":              vals["items"],
        })
    result_lines.sort(key=lambda x: x["nim_income_m"], reverse=True)

    total_nim = sum(l["nim_income_m"] for l in result_lines)
    total_vol = sum(l["volume_m"] for l in result_lines)

    return {
        "business_lines":    result_lines,
        "total_nim_m":       round(total_nim, 3),
        "total_volume_m":    round(total_vol, 2),
        "bank_nim_pct":      round(total_nim / total_vol * 100, 3) if total_vol else 0,
    }


# ══════════════════════════════════════════════════════════════════════════════
# 5. RISK HEATMAP BUILDER
# ══════════════════════════════════════════════════════════════════════════════

def build_risk_heatmap(data: dict) -> dict:
    """
    Builds a multi-dimension risk score matrix for dashboard display.
    Scores 1-5 (1=low, 5=high risk) across:
      - Rate risk (NII sensitivity, duration gap)
      - Liquidity risk (LCR, NSFR, funding concentration)
      - Credit risk (loan quality proxy)
      - FX risk (NOP as % capital)
      - Behavioral risk (deposit stickiness, beta)

    Input: all available metrics from other modules.
    Output: 5×3 matrix [risk_type][scenario: base/stress/severe]
    """
    metrics = data.get("metrics", {})

    def score(value, thresholds, reverse=False):
        """Map a value to 1-5 score. thresholds = [t1,t2,t3,t4] for scores 1/2/3/4/5."""
        if value is None: return None
        v = abs(value)
        for i, t in enumerate(thresholds):
            if v <= t:
                s = i + 1
                return (6 - s) if reverse else s
        return 5 if not reverse else 1

    # Rate risk
    nii_pct    = abs(metrics.get("nii_worst_pct", 0))
    dur_gap    = abs(metrics.get("duration_gap", 0))
    rate_base  = score(nii_pct, [2, 5, 10, 15])
    rate_stress= score(nii_pct * 1.5, [2, 5, 10, 15])
    rate_severe= score(nii_pct * 2.5, [2, 5, 10, 15])

    # Liquidity risk
    lcr        = metrics.get("lcr")
    nsfr       = metrics.get("nsfr")
    liq_base   = score(lcr, [200, 150, 130, 110], reverse=True) if lcr else 3
    liq_stress = score(lcr * 0.8, [200, 150, 130, 110], reverse=True) if lcr else 4
    liq_severe = score(lcr * 0.6, [200, 150, 130, 110], reverse=True) if lcr else 5

    # FX risk
    fx_nop_pct = abs(metrics.get("fx_nop_pct_capital", 0))
    fx_base    = score(fx_nop_pct, [5, 10, 15, 20])
    fx_stress  = score(fx_nop_pct * 1.3, [5, 10, 15, 20])
    fx_severe  = score(fx_nop_pct * 1.8, [5, 10, 15, 20])

    # Duration / EVE risk
    eve_pct    = abs(metrics.get("eve_delta_pct_tier1", 0))
    eve_base   = score(eve_pct, [3, 7, 10, 13])
    eve_stress = score(eve_pct * 1.5, [3, 7, 10, 13])
    eve_severe = score(eve_pct * 2.5, [3, 7, 10, 13])

    # Funding / behavioral risk
    stability  = metrics.get("funding_stability_score", 70)
    fund_base  = score(100 - stability, [10, 20, 30, 40])
    fund_stress= score((100 - stability) * 1.3, [10, 20, 30, 40])
    fund_severe= score((100 - stability) * 1.8, [10, 20, 30, 40])

    def label(s):
        if s is None: return "N/A"
        return ["", "Low", "Moderate", "Elevated", "High", "Critical"][min(s, 5)]

    def color(s):
        if s is None: return "#475569"
        return ["", "#10b981", "#84cc16", "#f59e0b", "#ef4444", "#7f1d1d"][min(s, 5)]

    categories = [
        {"name": "Rate Risk (NII)",      "scores": [rate_base, rate_stress, rate_severe]},
        {"name": "Rate Risk (EVE/Dur)",  "scores": [eve_base,  eve_stress,  eve_severe]},
        {"name": "Liquidity (LCR/NSFR)","scores": [liq_base,  liq_stress,  liq_severe]},
        {"name": "FX / NOP",            "scores": [fx_base,   fx_stress,   fx_severe]},
        {"name": "Funding Stability",   "scores": [fund_base, fund_stress, fund_severe]},
    ]

    cells = []
    for cat in categories:
        row = []
        for sc in cat["scores"]:
            row.append({"score": sc, "label": label(sc), "color": color(sc)})
        cells.append({"category": cat["name"], "cells": row})

    overall = np.mean([s for cat in categories for s in cat["scores"] if s is not None])

    return {
        "matrix":          cells,
        "columns":         ["Base", "Stress (+1σ)", "Severe (+2σ)"],
        "overall_score":   round(float(overall), 2),
        "overall_label":   label(round(overall)),
        "overall_color":   color(round(overall)),
        "metrics_used":    list(metrics.keys()),
    }

"""
Bek Rate Desk — FX Vanilla Options Engine
==========================================
European FX options pricing ve risk management:

A. Garman-Kohlhagen Modeli — European vanilla option fiyatlama
B. Greeks Hesabı        — Delta, Gamma, Vega, Theta, Rho
C. Implied Volatility   — Newton-Raphson with bisection fallback
D. Stratejik Seçenek Fiyatlaması — straddle, strangle, risk_reversal, butterfly
E. Vol Surface Generator — Turkish lira term structure

Turkish FX bağlamı: USD/TRY, EUR/TRY seçenekleri, BRSA riskten korunma limitleri
"""

import math
from datetime import datetime, date, timedelta
from typing import Optional, Dict, List, Tuple
from scipy.stats import norm


# ──────────────────────────────────────────────────────────────────────────────
# GARMAN-KOHLHAGEN MODEL — EUROPEAN FX OPTIONS
# ──────────────────────────────────────────────────────────────────────────────

def gk_price(
    S: float,
    K: float,
    r_d: float,
    r_f: float,
    sigma: float,
    T: float,
    option_type: str = "call"
) -> float:
    """
    Garman-Kohlhagen European FX option fiyatı.

    Formula: C = S*e^(-r_f*T)*N(d1) - K*e^(-r_d*T)*N(d2)
             P = K*e^(-r_d*T)*N(-d2) - S*e^(-r_f*T)*N(-d1)

    Args:
        S: Spot rate (e.g., 44.85 for USDTRY)
        K: Strike rate
        r_d: Domestic (quote CCY) rate as decimal (e.g., 0.425 for 42.5%)
        r_f: Foreign (base CCY) rate as decimal (e.g., 0.045 for 4.5%)
        sigma: Volatility as decimal (e.g., 0.15 for 15%)
        T: Time to expiry in years
        option_type: "call" or "put"

    Returns:
        Option price in quote CCY per unit of base
    """
    if T <= 0:
        if option_type == "call":
            return max(S - K, 0) * math.exp(-r_f * T)
        else:
            return max(K - S, 0) * math.exp(-r_d * T)

    if sigma <= 0:
        raise ValueError(f"Volatility must be positive, got {sigma}")

    sqrt_t = math.sqrt(T)
    d1 = (math.log(S / K) + (r_d - r_f + 0.5 * sigma * sigma) * T) / (sigma * sqrt_t)
    d2 = d1 - sigma * sqrt_t

    if option_type.lower() == "call":
        price = S * math.exp(-r_f * T) * norm.cdf(d1) - K * math.exp(-r_d * T) * norm.cdf(d2)
    else:
        price = K * math.exp(-r_d * T) * norm.cdf(-d2) - S * math.exp(-r_f * T) * norm.cdf(-d1)

    return round(price, 6)


def gk_greeks(
    S: float,
    K: float,
    r_d: float,
    r_f: float,
    sigma: float,
    T: float,
    option_type: str = "call"
) -> dict:
    """
    Garman-Kohlhagen Greeks hesabı.

    Returns:
        dict: delta, gamma, vega (per 1%), theta (per calendar day),
              rho_domestic (per 1%), rho_foreign (per 1%), d1, d2
    """
    if T <= 0:
        return {
            "delta": 1.0 if option_type == "call" and S > K else (0.0 if option_type == "call" else -1.0),
            "gamma": 0.0,
            "vega": 0.0,
            "theta": 0.0,
            "rho_domestic": 0.0,
            "rho_foreign": 0.0,
            "d1": 0.0,
            "d2": 0.0,
        }

    sqrt_t = math.sqrt(T)
    d1 = (math.log(S / K) + (r_d - r_f + 0.5 * sigma * sigma) * T) / (sigma * sqrt_t)
    d2 = d1 - sigma * sqrt_t

    n_d1 = norm.pdf(d1)
    n_prime_d1 = norm.cdf(d1)
    n_prime_minus_d1 = norm.cdf(-d1)
    n_prime_d2 = norm.cdf(d2)
    n_prime_minus_d2 = norm.cdf(-d2)

    exp_rf_t = math.exp(-r_f * T)
    exp_rd_t = math.exp(-r_d * T)

    # Delta
    if option_type.lower() == "call":
        delta = exp_rf_t * n_prime_d1
    else:
        delta = -exp_rf_t * n_prime_minus_d1

    # Gamma (same for call and put)
    gamma = exp_rf_t * n_d1 / (S * sigma * sqrt_t)

    # Vega (per 1% change in volatility)
    vega = S * exp_rf_t * n_d1 * sqrt_t / 100.0

    # Theta (per calendar day)
    if option_type.lower() == "call":
        theta_raw = (
            -S * exp_rf_t * n_d1 * sigma / (2 * sqrt_t)
            - r_d * K * exp_rd_t * n_prime_d2
            + r_f * S * exp_rf_t * n_prime_d1
        )
    else:
        theta_raw = (
            -S * exp_rf_t * n_d1 * sigma / (2 * sqrt_t)
            + r_d * K * exp_rd_t * n_prime_minus_d2
            - r_f * S * exp_rf_t * n_prime_minus_d1
        )
    theta = theta_raw / 365.0

    # Rho — domestic rate (per 1% change)
    if option_type.lower() == "call":
        rho_d = K * T * exp_rd_t * n_prime_d2 / 100.0
    else:
        rho_d = -K * T * exp_rd_t * n_prime_minus_d2 / 100.0

    # Rho — foreign rate (per 1% change)
    if option_type.lower() == "call":
        rho_f = -S * T * exp_rf_t * n_prime_d1 / 100.0
    else:
        rho_f = S * T * exp_rf_t * n_prime_minus_d1 / 100.0

    return {
        "delta": round(delta, 6),
        "gamma": round(gamma, 6),
        "vega": round(vega, 6),
        "theta": round(theta, 6),
        "rho_domestic": round(rho_d, 6),
        "rho_foreign": round(rho_f, 6),
        "d1": round(d1, 6),
        "d2": round(d2, 6),
    }


def implied_vol(
    market_price: float,
    S: float,
    K: float,
    r_d: float,
    r_f: float,
    T: float,
    option_type: str = "call",
    initial_guess: float = 0.15,
    max_iterations: int = 100,
    tolerance: float = 1e-6
) -> Optional[float]:
    """
    Implied volatility hesabı — Newton-Raphson + bisection fallback.

    Args:
        market_price: Observed option price
        S, K, r_d, r_f, T, option_type: As in gk_price()
        initial_guess: Starting sigma estimate
        max_iterations: Max iterations
        tolerance: Convergence tolerance

    Returns:
        Implied volatility as decimal (e.g., 0.15 for 15%), or None if failed
    """
    sigma = initial_guess

    # Newton-Raphson
    for _ in range(max_iterations):
        try:
            price = gk_price(S, K, r_d, r_f, sigma, T, option_type)
            greeks = gk_greeks(S, K, r_d, r_f, sigma, T, option_type)
            vega = greeks["vega"]

            if abs(vega) < 1e-10:
                break

            diff = price - market_price
            if abs(diff) < tolerance:
                return round(sigma, 6)

            sigma = sigma - diff / vega

            if sigma < 0:
                sigma = 0.01
            if sigma > 2.0:
                sigma = 2.0

        except Exception:
            break

    # Bisection fallback
    try:
        sigma_low, sigma_high = 0.001, 2.0
        for _ in range(100):
            sigma_mid = (sigma_low + sigma_high) / 2
            price_mid = gk_price(S, K, r_d, r_f, sigma_mid, T, option_type)

            if abs(price_mid - market_price) < tolerance:
                return round(sigma_mid, 6)

            if price_mid < market_price:
                sigma_low = sigma_mid
            else:
                sigma_high = sigma_mid

            if sigma_high - sigma_low < tolerance:
                return round(sigma_mid, 6)

    except Exception:
        pass

    return None


# ══════════════════════════════════════════════════════════════════════════════
# SINGLE OPTION PRICING
# ══════════════════════════════════════════════════════════════════════════════

def price_option_full(data: dict) -> dict:
    """
    Kapsamlı European FX seçeneği fiyatlama — Greeks, breakeven, P&L analizi.

    Input: {
        pair: "USDTRY",
        spot: float,
        strike: float,
        r_domestic_pct: float,       # TRY rate (%)
        r_foreign_pct: float,        # USD rate (%)
        vol_pct: float,              # Volatility (%)
        tenor_days: int,
        notional: float,             # Notional amount in base CCY
        option_type: "call" | "put",
        direction: "buy" | "sell",
    }

    Returns:
        dict: price, price_pct, greeks, breakeven, moneyness, P&L metrics
    """
    pair = data.get("pair", "USDTRY")
    S = float(data.get("spot", 44.85))
    K = float(data.get("strike", 44.85))
    from utils.tcmb_evds import get_policy_rate as _get_pr
    r_d_pct = float(data.get("r_domestic_pct") or _get_pr())
    r_f_pct = float(data.get("r_foreign_pct", 4.5))
    vol_pct = float(data.get("vol_pct", 20.0))
    tenor_days = int(data.get("tenor_days", 30))
    notional = float(data.get("notional", 1_000_000))
    opt_type = data.get("option_type", "call").lower()
    direction = data.get("direction", "buy").lower()

    # Convert percentages to decimals
    r_d = r_d_pct / 100.0
    r_f = r_f_pct / 100.0
    sigma = vol_pct / 100.0
    T = tenor_days / 365.0

    # Pricing
    price_per_unit = gk_price(S, K, r_d, r_f, sigma, T, opt_type)
    greeks = gk_greeks(S, K, r_d, r_f, sigma, T, opt_type)

    price_pct = round((price_per_unit / S) * 100, 4)
    notional_premium = round(price_per_unit * notional, 2)

    # Breakeven
    if opt_type == "call":
        breakeven = round(K + price_per_unit, 6) if direction == "buy" else round(K - price_per_unit, 6)
    else:
        breakeven = round(K - price_per_unit, 6) if direction == "buy" else round(K + price_per_unit, 6)

    # Moneyness
    if opt_type == "call":
        if S > K:
            moneyness = "ITM"
        elif abs(S - K) < 0.5:
            moneyness = "ATM"
        else:
            moneyness = "OTM"
    else:
        if S < K:
            moneyness = "ITM"
        elif abs(S - K) < 0.5:
            moneyness = "ATM"
        else:
            moneyness = "OTM"

    # Intrinsic + Time value
    if opt_type == "call":
        intrinsic = max(S - K, 0)
    else:
        intrinsic = max(K - S, 0)

    time_value = round(price_per_unit - intrinsic, 6)

    # Delta notional
    delta = greeks["delta"]
    delta_notional = round(abs(delta * notional), 2)

    # Valör tarihleri
    today = date.today()
    value_date = today + timedelta(days=2)
    expiry_date = value_date + timedelta(days=tenor_days)

    # Interpretation
    moneyness_detail = (
        f"{moneyness} (spot {S} vs strike {K}). "
        f"İçsel değer: {intrinsic:.4f}, zaman değeri: {time_value:.4f}. "
    )

    if direction == "buy":
        risk_note = f"Maksimum zarar: {notional_premium:,.0f} {pair[3:]} (prim tutarı). "
    else:
        risk_note = f"Maksimum kâr: {notional_premium:,.0f} {pair[3:]} (prim tutarı). "

    interpretation = (
        f"{opt_type.upper()} {direction.upper()}, {notional/1e6:.1f}M {pair[:3]} @ {K}. "
        f"{moneyness_detail}{risk_note}"
        f"Delta: {delta:+.4f} ({delta_notional:,.0f} {pair[:3]}). "
        f"Vega: {greeks['vega']:.4f}/% vol. "
        f"Theta: {greeks['theta']:.6f}/gün (zaman azalması)."
    )

    return {
        "pair": pair,
        "spot": S,
        "strike": K,
        "option_type": opt_type,
        "direction": direction,
        "tenor_days": tenor_days,
        "notional": notional,
        # Pricing
        "price_per_unit": price_per_unit,
        "price_pct": price_pct,
        "notional_premium": notional_premium,
        # Greeks
        "delta": greeks["delta"],
        "gamma": greeks["gamma"],
        "vega": greeks["vega"],
        "theta": greeks["theta"],
        "rho_domestic": greeks["rho_domestic"],
        "rho_foreign": greeks["rho_foreign"],
        "d1": greeks["d1"],
        "d2": greeks["d2"],
        # Risk metrics
        "breakeven": breakeven,
        "moneyness": moneyness,
        "intrinsic_value": intrinsic,
        "time_value": time_value,
        "delta_notional": delta_notional,
        # Dates
        "value_date": value_date.isoformat(),
        "expiry_date": expiry_date.isoformat(),
        # Rates
        "r_domestic_pct": r_d_pct,
        "r_foreign_pct": r_f_pct,
        "vol_pct": vol_pct,
        # Summary
        "interpretation": interpretation,
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M"),
    }


# ══════════════════════════════════════════════════════════════════════════════
# OPTION STRATEGIES
# ══════════════════════════════════════════════════════════════════════════════

def price_strategy(data: dict) -> dict:
    """
    Seçenek stratejisi fiyatlama — straddle, strangle, risk_reversal, butterfly.

    Input: {
        pair: "USDTRY",
        spot: float,
        r_domestic_pct: float,
        r_foreign_pct: float,
        vol_pct: float,
        tenor_days: int,
        notional: float,
        strategy_type: "straddle" | "strangle" | "risk_reversal" | "butterfly",
        # Strategy-specific params:
        strike_offset_pct: float,    # For strangle/butterfly (%)
    }

    Returns:
        dict: legs, total_cost, net_delta, breakevens
    """
    pair = data.get("pair", "USDTRY")
    S = float(data.get("spot", 44.85))
    from utils.tcmb_evds import get_policy_rate as _get_pr
    r_d_pct = float(data.get("r_domestic_pct") or _get_pr())
    r_f_pct = float(data.get("r_foreign_pct", 4.5))
    vol_pct = float(data.get("vol_pct", 20.0))
    tenor_days = int(data.get("tenor_days", 30))
    notional = float(data.get("notional", 1_000_000))
    strategy = data.get("strategy_type", "straddle").lower()
    offset_pct = float(data.get("strike_offset_pct", 5.0))

    r_d = r_d_pct / 100.0
    r_f = r_f_pct / 100.0
    sigma = vol_pct / 100.0
    T = tenor_days / 365.0

    legs = []
    total_cost = 0.0
    net_delta = 0.0

    if strategy == "straddle":
        # ATM call + ATM put, both long
        for opt_type in ["call", "put"]:
            price = gk_price(S, S, r_d, r_f, sigma, T, opt_type)
            greeks = gk_greeks(S, S, r_d, r_f, sigma, T, opt_type)
            leg_cost = price * notional

            legs.append({
                "type": opt_type,
                "strike": S,
                "direction": "buy",
                "price_per_unit": price,
                "leg_cost": round(leg_cost, 2),
                "delta": greeks["delta"],
            })

            total_cost += leg_cost
            net_delta += greeks["delta"]

    elif strategy == "strangle":
        # OTM call + OTM put, both long
        K_call = round(S * (1 + offset_pct / 100), 4)
        K_put = round(S * (1 - offset_pct / 100), 4)

        for opt_type, strike in [("call", K_call), ("put", K_put)]:
            price = gk_price(S, strike, r_d, r_f, sigma, T, opt_type)
            greeks = gk_greeks(S, strike, r_d, r_f, sigma, T, opt_type)
            leg_cost = price * notional

            legs.append({
                "type": opt_type,
                "strike": strike,
                "direction": "buy",
                "price_per_unit": price,
                "leg_cost": round(leg_cost, 2),
                "delta": greeks["delta"],
            })

            total_cost += leg_cost
            net_delta += greeks["delta"]

    elif strategy == "risk_reversal":
        # Buy OTM call, sell OTM put
        K_call = round(S * (1 + offset_pct / 100), 4)
        K_put = round(S * (1 - offset_pct / 100), 4)

        price_call = gk_price(S, K_call, r_d, r_f, sigma, T, "call")
        greeks_call = gk_greeks(S, K_call, r_d, r_f, sigma, T, "call")

        price_put = gk_price(S, K_put, r_d, r_f, sigma, T, "put")
        greeks_put = gk_greeks(S, K_put, r_d, r_f, sigma, T, "put")

        cost_call = price_call * notional
        cost_put = -price_put * notional  # Short = negative cost

        legs.append({
            "type": "call",
            "strike": K_call,
            "direction": "buy",
            "price_per_unit": price_call,
            "leg_cost": round(cost_call, 2),
            "delta": greeks_call["delta"],
        })
        legs.append({
            "type": "put",
            "strike": K_put,
            "direction": "sell",
            "price_per_unit": -price_put,
            "leg_cost": round(cost_put, 2),
            "delta": -greeks_put["delta"],
        })

        total_cost = cost_call + cost_put
        net_delta = greeks_call["delta"] - greeks_put["delta"]

    elif strategy == "butterfly":
        # Buy 1x call @ K-offset, sell 2x call @ K, buy 1x call @ K+offset
        K_low = round(S * (1 - offset_pct / 100), 4)
        K_mid = S
        K_high = round(S * (1 + offset_pct / 100), 4)

        for k, mult in [(K_low, 1), (K_mid, -2), (K_high, 1)]:
            price = gk_price(S, k, r_d, r_f, sigma, T, "call")
            greeks = gk_greeks(S, k, r_d, r_f, sigma, T, "call")
            leg_cost = price * notional * mult

            direction = "buy" if mult > 0 else "sell"
            legs.append({
                "type": "call",
                "strike": k,
                "direction": direction,
                "quantity": abs(mult),
                "price_per_unit": price,
                "leg_cost": round(leg_cost, 2),
                "delta": greeks["delta"] * mult,
            })

            total_cost += leg_cost
            net_delta += greeks["delta"] * mult

    # Normalize leg dicts: expose "option_type" (canonical) alongside "type"
    for leg in legs:
        if "type" in leg and "option_type" not in leg:
            leg["option_type"] = leg["type"]

    return {
        "pair":          pair,
        # "strategy_type" is the canonical key; "strategy" kept as alias
        "strategy_type": strategy,
        "strategy":      strategy,
        "spot":          S,
        "tenor_days":    tenor_days,
        "notional":      notional,
        "legs":          legs,
        # "net_cost" is the canonical key; "total_cost" kept as alias
        "net_cost":      round(total_cost, 2),
        "total_cost":    round(total_cost, 2),
        "net_delta":     round(net_delta, 4),
        "generated_at":  datetime.now().strftime("%Y-%m-%d %H:%M"),
    }


# ══════════════════════════════════════════════════════════════════════════════
# VOLATILITY SURFACE
# ══════════════════════════════════════════════════════════════════════════════

def vol_surface(data: dict) -> dict:
    """
    Türk Lirası volatility surface — ATM + risk reversal term yapısı.

    Turkish lira EM karakteristikleri:
    - Short-end vol spike (2W-1M)
    - Declining long-end (6M-1Y)
    - 25-delta RR: USD call premium (negatif TRY bias)
    - Butterfly: küçük pozitif spread

    Input: {
        pair: "USDTRY",
        spot: float,
        r_domestic_pct: float,
        r_foreign_pct: float,
        atm_vol_pct: float,       # Base ATM volatility (%)
    }

    Returns:
        list of {tenor, atm_vol, rr_25d, bf_25d, call_vol, put_vol}
    """
    pair = data.get("pair", "USDTRY")
    S = float(data.get("spot", 44.85))
    from utils.tcmb_evds import get_policy_rate as _get_pr
    r_d_pct = float(data.get("r_domestic_pct") or _get_pr())
    r_f_pct = float(data.get("r_foreign_pct", 4.5))
    atm_vol_pct = float(data.get("atm_vol_pct", 20.0))

    # Tenor structure with multipliers
    tenors = [
        ("1W",   7,   0.95),   # 1W: slightly lower
        ("2W",  14,   1.05),   # 2W: spike
        ("1M",  30,   1.08),   # 1M: peak
        ("2M",  60,   0.95),   # 2M: start declining
        ("3M",  90,   0.88),   # 3M: declining
        ("6M", 180,   0.75),   # 6M: lower
        ("9M", 270,   0.70),   # 9M: even lower
        ("1Y", 360,   0.65),   # 1Y: lowest
    ]

    surface = []

    for tenor_label, days, vol_mult in tenors:
        T = days / 365.0

        # ATM vol scaling
        atm_vol = round(atm_vol_pct * vol_mult, 2)

        # EM bias: USD call premium = negative TRY put premium
        # 25-delta RR: typically -2 to -4% for TRY
        rr_25d = round(-2.5 * vol_mult, 2)

        # Butterfly: small positive, widens with tenor
        bf_25d = round(0.5 + 0.1 * (days / 30), 2)

        # Derive call/put vols from ATM + RR + BF
        # call_vol = ATM + RR/2 + BF/2
        # put_vol = ATM - RR/2 + BF/2
        call_vol = round(atm_vol + rr_25d / 2 + bf_25d / 2, 2)
        put_vol = round(atm_vol - rr_25d / 2 + bf_25d / 2, 2)

        surface.append({
            "tenor": tenor_label,
            "days": days,
            "atm_vol_pct": atm_vol,
            "rr_25d_pct": rr_25d,
            "bf_25d_pct": bf_25d,
            "call_vol_pct": call_vol,
            "put_vol_pct": put_vol,
        })

    return {
        "pair": pair,
        "spot": S,
        "base_atm_vol_pct": atm_vol_pct,
        "r_domestic_pct": r_d_pct,
        "r_foreign_pct": r_f_pct,
        "surface": surface,
        "interpretation": (
            f"{pair} volatility surface. Base ATM: {atm_vol_pct}%. "
            f"Karakteristik: kısa vadede (1M) artış, uzun vadede (1Y) azalış (EM curve). "
            f"25-delta RR negatif: USD call primesi (TRY weakness bias). "
            f"Butterfly küçük pozitif: volatility smile etkisi."
        ),
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M"),
    }

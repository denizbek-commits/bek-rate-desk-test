"""
Bek Rate Desk — Dealer Management Engine
=========================================
Banka operasyon ve risk yönetimi:

A. BRSA FX NOP Takibi — Net Open Position limits and monitoring
B. Müşteri Fiyat Tablosu — Multi-tenor, multi-pair rate sheet generator
C. Günlük P&L Raporlama — Daily realized/unrealized P&L breakdown

Turkish regulatory: BRSA FX NOP limit 20% of equity, daily monitoring
"""

import json
import os
import math
from datetime import datetime, date, timedelta
from typing import Optional, Dict, List


# ──────────────────────────────────────────────────────────────────────────────
# BRSA NOP TRACKING
# ──────────────────────────────────────────────────────────────────────────────

def calculate_nop(data: dict) -> dict:
    """
    BRSA FX Net Open Position tracker.

    Input: {
        positions: [
            {pair: "USDTRY", direction: "long"|"short", amount: float, entry_rate: float},
            ...
        ],
        current_rates: {USDTRY: float, EURTRY: float, GBPTRY: float},
        equity_m: float,                # Bank equity in million TRY
        off_balance_usd_m: float,       # Optional off-balance USD position
        off_balance_eur_m: float,       # Optional off-balance EUR position
    }

    Returns:
        dict: NOP by currency, total NOP, ratio, BRSA status, remaining headroom
    """
    positions = data.get("positions", [])
    current_rates = data.get("current_rates", {})
    equity_m = float(data.get("equity_m", 1000.0))
    off_usd_m = float(data.get("off_balance_usd_m", 0.0))
    off_eur_m = float(data.get("off_balance_eur_m", 0.0))

    # Default rates if not provided
    if "USDTRY" not in current_rates:
        current_rates["USDTRY"] = 44.85
    if "EURTRY" not in current_rates:
        current_rates["EURTRY"] = 52.10
    if "GBPTRY" not in current_rates:
        current_rates["GBPTRY"] = 57.50

    # Calculate net positions by currency (in TRY equivalent)
    nop_by_ccy = {}

    for pos in positions:
        pair = pos.get("pair", "USDTRY").upper()
        direction = pos.get("direction", "long").lower()
        amount = float(pos.get("amount", 0))

        # Extract base currency
        if len(pair) == 6:
            base_ccy = pair[:3]
        else:
            base_ccy = "USD"

        # Get current rate
        rate = current_rates.get(pair, 1.0)

        # Convert to TRY
        amount_try = amount * rate

        # Net position (long adds, short subtracts)
        if base_ccy not in nop_by_ccy:
            nop_by_ccy[base_ccy] = 0.0

        if direction == "long":
            nop_by_ccy[base_ccy] += amount_try
        else:
            nop_by_ccy[base_ccy] -= amount_try

    # Add off-balance positions
    if off_usd_m != 0:
        if "USD" not in nop_by_ccy:
            nop_by_ccy["USD"] = 0.0
        nop_by_ccy["USD"] += off_usd_m * 1_000_000 * current_rates.get("USDTRY", 44.85)

    if off_eur_m != 0:
        if "EUR" not in nop_by_ccy:
            nop_by_ccy["EUR"] = 0.0
        nop_by_ccy["EUR"] += off_eur_m * 1_000_000 * current_rates.get("EURTRY", 52.10)

    # Total NOP = sum of absolute values (not net)
    total_nop = sum(abs(v) for v in nop_by_ccy.values())
    total_nop_m = total_nop / 1_000_000

    # Limits and status
    limit_pct = 20.0
    limit_tl = equity_m * 1_000_000 * (limit_pct / 100.0)
    limit_m = limit_tl / 1_000_000

    nop_ratio_pct = (total_nop / (equity_m * 1_000_000)) * 100.0 if equity_m > 0 else 0.0

    # Status determination — BRSA FX NOP limit is exactly 20% of equity
    # At exactly 20% → BREACH (at the regulatory limit, not below it)
    # Above 20%       → OVER (limit exceeded, immediate action required)
    if nop_ratio_pct > 20.0:
        status = "OVER"    # Exceeds BRSA 20% limit — regulatory violation
    elif nop_ratio_pct >= 20.0:
        status = "BREACH"  # Exactly at the 20% regulatory threshold
    elif nop_ratio_pct >= 18.0:
        status = "WARN"    # Early warning — within 2% of limit
    elif nop_ratio_pct >= 15.0:
        status = "WATCH"   # Internal watch zone
    else:
        status = "OK"

    # Remaining headroom
    remaining_m = limit_m - total_nop_m

    # Max additional FX before breach
    breach_limit = equity_m * (20.0 / 100.0)
    max_additional_m = (breach_limit - total_nop) / 1_000_000

    # Gauge percentage for circular display (0-100%)
    gauge_pct = min((nop_ratio_pct / limit_pct) * 100, 100.0)

    # Convert nop_by_ccy to million TRY for readability
    nop_by_ccy_m = {ccy: round(val / 1_000_000, 2) for ccy, val in nop_by_ccy.items()}

    return {
        "nop_by_ccy_m": nop_by_ccy_m,
        "total_nop_m": round(total_nop_m, 2),
        "nop_ratio_pct": round(nop_ratio_pct, 2),
        "limit_pct": limit_pct,
        "limit_m": round(limit_m, 2),
        "remaining_m": round(remaining_m, 2),
        "max_additional_m": round(max_additional_m, 2),
        "status": status,
        "status_color": "red" if status in ("OVER", "BREACH") else ("orange" if status in ("WARN", "WATCH") else "green"),
        "equity_m": equity_m,
        "gauge_pct": round(gauge_pct, 2),
        "current_rates": current_rates,
        "interpretation": (
            f"FX NOP: {total_nop_m:,.0f}M TRY ({nop_ratio_pct:.1f}% of equity). "
            f"BRSA limit: 20% ({limit_m:,.0f}M TRY). "
            f"Durum: {status}. Kalan baş alanı: {remaining_m:,.0f}M TRY."
        ),
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M"),
    }


# ──────────────────────────────────────────────────────────────────────────────
# RATE SHEET GENERATION
# ──────────────────────────────────────────────────────────────────────────────

# Shared FX primitives — single source of truth
from utils.fx_utils import TYPICAL_SPREADS, CUSTOMER_TYPES, _pip_size, _business_days_add

# dealer_engine uses label-only CUSTOMER_TYPES for rate sheet display
# fx_utils provides full dicts; derive the label mapping here
_CUSTOMER_LABELS = {k: v["label"] for k, v in CUSTOMER_TYPES.items()}


def build_rate_sheet(data: dict) -> dict:
    """
    Multi-pair, multi-tenor customer rate sheet generator.

    Input: {
        spot_rates: {USDTRY: float, EURTRY: float, GBPTRY: float, EURUSD: float},
        r_try_pct: float,        # TRY interest rate (%)
        r_usd_pct: float,        # USD interest rate (%)
        r_eur_pct: float,        # EUR interest rate (%)
        r_gbp_pct: float,        # GBP interest rate (%)
        customer_type: "retail" | "corporate" | "vip" | "interbank",
        pairs: ["USDTRY", "EURTRY", ...],  # pairs to include
        include_forward: bool,    # include forward tenors
    }

    Returns:
        dict: customer_type, customer_label, pairs with spot + forward prices
    """
    spot_rates = data.get("spot_rates", {
        "USDTRY": 44.85,
        "EURTRY": 52.10,
        "GBPTRY": 57.50,
        "EURUSD": 1.0850,
    })
    r_try = float(data.get("r_try_pct", 42.5))
    r_usd = float(data.get("r_usd_pct", 4.5))
    r_eur = float(data.get("r_eur_pct", 2.5))
    r_gbp = float(data.get("r_gbp_pct", 4.75))
    cust_type = data.get("customer_type", "corporate").lower()
    pairs = data.get("pairs", ["USDTRY", "EURTRY", "GBPTRY", "EURUSD"])
    include_fwd = data.get("include_forward", True)

    # Interest rate mapping
    rate_map = {
        "USD": r_usd,
        "EUR": r_eur,
        "GBP": r_gbp,
        "TRY": r_try,
        "JPY": 0.1,
    }

    customer_label = _CUSTOMER_LABELS.get(cust_type, "Kurumsal")

    # Tenor structure
    tenors = [
        ("1W",   7),
        ("2W",  14),
        ("1M",  30),
        ("2M",  60),
        ("3M",  90),
        ("6M", 180),
        ("1Y", 360),
    ]

    result_pairs = []

    for pair in pairs:
        if pair not in spot_rates:
            continue

        spot_mid = float(spot_rates[pair])
        pip = _pip_size(pair)

        # Spread
        typical_spread = TYPICAL_SPREADS.get(pair, {}).get(cust_type, 15)
        spread_pips = typical_spread
        half_spread_val = spread_pips * pip / 2

        spot_bid = round(spot_mid - half_spread_val, 6)
        spot_ask = round(spot_mid + half_spread_val, 6)

        # Extract base and quote currencies
        if len(pair) == 6:
            base_ccy = pair[:3]
            quote_ccy = pair[3:]
        else:
            base_ccy = "?"
            quote_ccy = "?"

        r_base = rate_map.get(base_ccy, 0.0)
        r_quote = rate_map.get(quote_ccy, 0.0)

        pair_data = {
            "pair": pair,
            "base_ccy": base_ccy,
            "quote_ccy": quote_ccy,
            "spot_mid": round(spot_mid, 6),
            "spot_bid": spot_bid,
            "spot_ask": spot_ask,
            "spread_pips": round(spread_pips, 1),
            "tenors": [],
        }

        # Forward tenors (if requested)
        if include_fwd:
            for tenor_label, tenor_days in tenors:
                T = tenor_days / 360.0

                # Interest rate parity
                fwd_mid = spot_mid * (1 + r_quote / 100 * T) / (1 + r_base / 100 * T)

                # Spread widens with tenor
                spread_tenor = spread_pips * (1 + tenor_days / 365.0)
                half_spread_fwd = spread_tenor * pip / 2

                fwd_bid = round(fwd_mid - half_spread_fwd, 6)
                fwd_ask = round(fwd_mid + half_spread_fwd, 6)

                swap_points = round(fwd_mid - spot_mid, 6)

                pair_data["tenors"].append({
                    "tenor": tenor_label,
                    "days": tenor_days,
                    "fwd_mid": round(fwd_mid, 6),
                    "bid": fwd_bid,
                    "ask": fwd_ask,
                    "spread_pips": round(spread_tenor, 1),
                    "swap_points": swap_points,
                })

        result_pairs.append(pair_data)

    return {
        "customer_type": cust_type,
        "customer_label": customer_label,
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "valid_until": (datetime.now() + timedelta(hours=1)).strftime("%H:%M"),
        "interest_rates": {
            "TRY": r_try,
            "USD": r_usd,
            "EUR": r_eur,
            "GBP": r_gbp,
        },
        "pairs": result_pairs,
        "note": f"{len(result_pairs)} çift, {len(tenors)} vade. {customer_label} müşteri fiyatlandırması.",
    }


# ══════════════════════════════════════════════════════════════════════════════
# DAILY P&L REPORTING
# ══════════════════════════════════════════════════════════════════════════════

def get_daily_pnl_detail(data: dict) -> dict:
    """
    Comprehensive daily P&L reporting — realized + unrealized by pair/type.

    Input: {
        current_rates: {USDTRY: float, ...},
        positions_file: list,  # raw positions list (from trading_engine)
    }

    Returns:
        dict: realized_pnl, unrealized_pnl, total, by_pair, by_type, extremes
    """
    current_rates = data.get("current_rates", {})
    positions = data.get("positions_file", [])

    today_str = date.today().isoformat()

    # Default rates
    if "USDTRY" not in current_rates:
        current_rates["USDTRY"] = 44.85
    if "EURTRY" not in current_rates:
        current_rates["EURTRY"] = 52.10
    if "GBPTRY" not in current_rates:
        current_rates["GBPTRY"] = 57.50

    realized_pnl = 0.0
    unrealized_pnl = 0.0
    n_open = 0
    n_closed_today = 0

    pnl_by_pair = {}
    pnl_by_type = {}

    open_positions = []
    closed_positions = []

    for pos in positions:
        pair = pos.get("pair", "USDTRY")
        product_type = pos.get("product_type", "spot")
        direction = pos.get("direction", "long")
        amount = float(pos.get("amount", 0))
        entry_rate = float(pos.get("entry_rate", 0))
        status = pos.get("status", "open")

        # P&L calculation
        current_rate = current_rates.get(pair, entry_rate)

        if direction == "long":
            pos_pnl = round((current_rate - entry_rate) * amount, 2)
        else:
            pos_pnl = round((entry_rate - current_rate) * amount, 2)

        # Track by pair and type
        if pair not in pnl_by_pair:
            pnl_by_pair[pair] = {"realized": 0.0, "unrealized": 0.0, "total": 0.0}
        if product_type not in pnl_by_type:
            pnl_by_type[product_type] = 0.0

        if status == "open":
            unrealized_pnl += pos_pnl
            pnl_by_pair[pair]["unrealized"] += pos_pnl
            n_open += 1
            open_positions.append({**pos, "current_rate": current_rate, "mtm_pnl": pos_pnl})

        elif status == "closed":
            # Check if closed today
            close_time = pos.get("close_time", "")
            if close_time.startswith(today_str):
                realized_pnl += float(pos.get("realized_pnl", 0))
                pnl_by_pair[pair]["realized"] += float(pos.get("realized_pnl", 0))
                n_closed_today += 1
                closed_positions.append(pos)

    # Calculate totals by pair
    for pair in pnl_by_pair:
        pnl_by_pair[pair]["total"] = pnl_by_pair[pair]["realized"] + pnl_by_pair[pair]["unrealized"]

    # Aggregate P&L by type
    for pos in open_positions:
        ptype = pos.get("product_type", "spot")
        if ptype not in pnl_by_type:
            pnl_by_type[ptype] = 0.0
        pnl_by_type[ptype] += pos.get("mtm_pnl", 0)

    for pos in closed_positions:
        ptype = pos.get("product_type", "spot")
        if ptype not in pnl_by_type:
            pnl_by_type[ptype] = 0.0
        pnl_by_type[ptype] += pos.get("realized_pnl", 0)

    total_pnl = round(realized_pnl + unrealized_pnl, 2)

    # Find largest winner and loser
    largest_winner = None
    largest_loser = None
    max_pnl = -float("inf")
    min_pnl = float("inf")

    for pos in open_positions:
        mtm = pos.get("mtm_pnl", 0)
        if mtm > max_pnl:
            max_pnl = mtm
            largest_winner = pos

        if mtm < min_pnl:
            min_pnl = mtm
            largest_loser = pos

    return {
        "date": today_str,
        "realized_pnl": round(realized_pnl, 2),
        "unrealized_pnl": round(unrealized_pnl, 2),
        "total_pnl": total_pnl,
        "pnl_color": "green" if total_pnl >= 0 else "red",
        "n_open": n_open,
        "n_closed_today": n_closed_today,
        "pnl_by_pair": {k: {kk: round(vv, 2) for kk, vv in v.items()} for k, v in pnl_by_pair.items()},
        "pnl_by_type": {k: round(v, 2) for k, v in pnl_by_type.items()},
        "largest_winner": largest_winner if largest_winner else {},
        "largest_loser": largest_loser if largest_loser else {},
        "current_rates": current_rates,
        "summary": (
            f"Gerçekleşmiş: {realized_pnl:+,.0f} TRY | "
            f"Gerçekleşmemiş: {unrealized_pnl:+,.0f} TRY | "
            f"Toplam: {total_pnl:+,.0f} TRY | "
            f"Açık pozisyon: {n_open}, Kapalı (bugün): {n_closed_today}"
        ),
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }

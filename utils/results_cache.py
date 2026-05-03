"""
Bek Rate Desk — Cross-Module Results Cache
==========================================
Every calculation endpoint auto-caches its result here.
Any page can then call get_prefill() to pre-populate its input form.

Cache keys:
  "nii"     ← alm_engine.run_nii_sensitivity()
  "eve"     ← alm_engine.run_eve_analysis()
  "lcr"     ← alm_engine.run_liquidity_ratios()
  "fx_gap"  ← alm_engine.run_fx_gap()
  "mc"      ← monte_carlo_engine simulation result
  "rates"   ← live rates snapshot (policy, usdtry, us10y)

Usage:
  from utils.results_cache import cache_result, get_result, get_prefill
"""

import time
from datetime import datetime
from typing import Any, Optional
from utils.io_utils import _atomic_json_write
import os, json

_CACHE_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "data", "results_cache.json"
)

# In-memory fast layer (survives within a process, JSON backs across restarts)
_MEM: dict[str, dict] = {}


# ── Write ──────────────────────────────────────────────────────────────────────

def cache_result(key: str, result: dict) -> None:
    """Store a module result. Called automatically after each /api/.../calculate."""
    entry = {
        "key":       key,
        "ts":        time.time(),
        "ts_iso":    datetime.now().isoformat(timespec="seconds"),
        "result":    result,
    }
    _MEM[key] = entry
    # Persist to disk (best-effort — never crash the calculate route)
    try:
        disk = _load_disk()
        disk[key] = entry
        _atomic_json_write(_CACHE_PATH, disk)
    except Exception:
        pass


# ── Read ───────────────────────────────────────────────────────────────────────

def get_result(key: str) -> Optional[dict]:
    """Return the cached result dict for *key*, or None."""
    if key in _MEM:
        return _MEM[key]["result"]
    disk = _load_disk()
    if key in disk:
        _MEM[key] = disk[key]           # warm the in-memory layer
        return disk[key]["result"]
    return None


def get_cache_meta(key: str) -> Optional[dict]:
    """Return {ts_iso, age_s} for a key, or None."""
    entry = _MEM.get(key) or _load_disk().get(key)
    if not entry:
        return None
    age = round(time.time() - entry["ts"])
    return {"ts_iso": entry["ts_iso"], "age_s": age}


# ── Live rate snapshot ─────────────────────────────────────────────────────────

def snapshot_live_rates() -> dict:
    """
    Fetch current TCMB policy rate, USDTRY (XML), and US 10Y (FRED).
    Returns a dict ready to be used in prefill.
    Best-effort — returns None values on failure.
    """
    rates = {"policy_rate": None, "usdtry": None, "us10y": None}
    try:
        from utils.tcmb_evds import get_policy_rate
        rates["policy_rate"] = get_policy_rate()
    except Exception:
        pass
    try:
        from utils.tcmb_evds import fetch_fx_rates_tcmb
        fx = fetch_fx_rates_tcmb()
        rates["usdtry"] = fx.get("rates", {}).get("USDTRY")
    except Exception:
        pass
    try:
        from utils.rates_engine import _fetch_fred_series
        s = _fetch_fred_series("DGS10", days=5)
        if not s.empty:
            rates["us10y"] = round(float(s.iloc[-1]), 3)
    except Exception:
        pass
    return rates


# ── Prefill assembler ──────────────────────────────────────────────────────────

def get_prefill(target: str = "morning_brief") -> dict:
    """
    Assemble a prefill dict for *target* module from cached results + live rates.

    Targets: "morning_brief" | "hedge" | "alco" | "eod"

    All values may be None if that module hasn't been calculated yet.
    """
    nii  = get_result("nii")  or {}
    eve  = get_result("eve")  or {}
    lcr  = get_result("lcr")  or {}
    fx   = get_result("fx_gap") or {}
    mc   = get_result("mc")   or {}

    # Balance sheet session supplements (tier1, total_assets, base_rate)
    session = {}
    try:
        from utils.balance_sheet_session import load_session
        session = load_session() or {}
    except Exception:
        pass

    # ── Derived values ─────────────────────────────────────────────────────────

    # NII
    base_nii        = nii.get("base_nii")
    nii_at_risk     = nii.get("nii_at_risk")
    total_assets    = nii.get("total_assets") or session.get("total_assets")
    total_liabs     = nii.get("total_liabilities") or session.get("total_liabilities")

    # Worst NII scenario name
    worst_sc_name = None
    worst_sc_delta = None
    if nii.get("scenarios"):
        worst_entry = min(nii["scenarios"].items(), key=lambda x: x[1].get("nii_change", 0))
        _sc_labels = {
            "parallel_up_100": "+100bps Parallel", "parallel_up_200": "+200bps Parallel",
            "parallel_down_100": "-100bps Parallel", "parallel_down_200": "-200bps Parallel",
            "bear_steepen": "Bear Steepen", "bull_flatten": "Bull Flatten",
            "bear_flatten": "Bear Flatten", "bull_steepen": "Bull Steepen",
        }
        worst_sc_name  = _sc_labels.get(worst_entry[0], worst_entry[0])
        worst_sc_delta = worst_entry[1].get("nii_change")

    # Worst repricing bucket
    worst_rgap = None
    worst_bucket = None
    if nii.get("repricing_gap"):
        worst_row = min(nii["repricing_gap"], key=lambda r: r.get("gap", 0))
        worst_rgap   = worst_row.get("gap")
        worst_bucket = worst_row.get("bucket")

    # EVE
    duration_gap  = eve.get("duration_gap")
    eve_at_risk   = eve.get("eve_at_risk")
    tier1_capital = (
        eve.get("tier1_capital")
        or session.get("tier1_capital")
        or 1500.0
    )

    # LCR / NSFR  — alm_engine returns {"lcr": {"ratio": ...}, "nsfr": {"ratio": ...}}
    lcr_val  = None
    nsfr_val = None
    if isinstance(lcr.get("lcr"), dict):
        lcr_val  = lcr["lcr"].get("ratio")
    elif lcr.get("lcr") is not None:
        lcr_val  = lcr["lcr"]
    if isinstance(lcr.get("nsfr"), dict):
        nsfr_val = lcr["nsfr"].get("ratio")
    elif lcr.get("nsfr") is not None:
        nsfr_val = lcr["nsfr"]

    # FX Gap — alm_engine returns:
    #   "currencies": {"USD": {"nop_tl": ..., "nop": ...}, "EUR": {...}, "TL": {...}}
    #   "consolidated": [{"bucket": ..., "gap_tl": ...}, ...]  ← list of buckets, not a dict
    fx_nop_tl  = None
    capital_tl = None
    if fx.get("currencies") and isinstance(fx["currencies"], dict):
        # Sum NOP (TL equivalent) across all non-TL currencies
        total = sum(
            v.get("nop_tl", 0)
            for k, v in fx["currencies"].items()
            if k != "TL" and isinstance(v, dict)
        )
        fx_nop_tl = round(total, 2)
    if fx_nop_tl is None:
        fx_nop_tl = fx.get("net_open_position_tl") or fx.get("fx_nop_tl")

    # Monte Carlo supplement
    mc_nii_var = mc.get("nii_var_95") or mc.get("var_95")

    # Base rate from session
    base_rate = session.get("base_rate")

    # ── Live rates (best-effort, may be None) ─────────────────────────────────
    live = snapshot_live_rates()
    policy_rate = live["policy_rate"] or base_rate
    usdtry      = live["usdtry"]
    us10y       = live["us10y"]

    # ── Meta: which modules were actually cached? ──────────────────────────────
    sourced = []
    if nii: sourced.append("NII")
    if eve: sourced.append("EVE")
    if lcr: sourced.append("LCR")
    if fx:  sourced.append("FX Gap")
    if mc:  sourced.append("Monte Carlo")

    cache_timestamps = {}
    for k in ("nii", "eve", "lcr", "fx_gap", "mc"):
        m = get_cache_meta(k)
        if m:
            cache_timestamps[k] = m

    # ── Target-specific field mapping ─────────────────────────────────────────

    if target == "morning_brief":
        return {
            "fields": {
                "i-nii-base":     base_nii,
                "i-nii-delta":    nii_at_risk,
                "i-nii-scenario": worst_sc_name,
                "i-eve":          eve_at_risk,
                "i-tier1":        tier1_capital,
                "i-dur":          duration_gap,
                "i-lcr":          lcr_val,
                "i-nsfr":         nsfr_val,
                "i-fx":           fx_nop_tl,
                "i-cap":          capital_tl,
                "i-rgap":         worst_rgap,
                "i-bucket":       worst_bucket,
                "i-assets":       total_assets,
                "i-base":         policy_rate,
                "i-usdtry":       usdtry,
                "i-us10y":        us10y,
            },
            "sourced":   sourced,
            "timestamps": cache_timestamps,
        }

    if target == "hedge":
        return {
            "fields": {
                "hDurGap":   duration_gap,
                "hAssets":   total_assets,
                "hLiabs":    total_liabs,
                "hTier1":    tier1_capital,
                "hEVE":      eve_at_risk,
                "hNIIBase":  base_nii,
                "hNIIDelta": nii_at_risk,
                "hBaseRate": policy_rate,
            },
            "sourced":   sourced,
            "timestamps": cache_timestamps,
        }

    if target in ("alco", "alco_memo"):
        return {
            "fields": {
                "mAssets":    total_assets,
                "mBaseRate":  policy_rate,
                "m-nii-base": base_nii,
                "m-nii-delta":nii_at_risk,
                "m-scenario": worst_sc_name,
                "m-eve":      eve_at_risk,
                "m-tier1":    tier1_capital,
                "m-dur":      duration_gap,
                "m-lcr":      lcr_val,
                "m-nsfr":     nsfr_val,
                "m-fx":       fx_nop_tl,
                "m-cap":      capital_tl,
                "m-usdtry":   usdtry,
                "m-us10y":    us10y,
            },
            "sourced":   sourced,
            "timestamps": cache_timestamps,
        }

    if target == "eod":
        return {
            "nii_base":      base_nii,
            "nii_at_risk":   nii_at_risk,
            "worst_scenario":worst_sc_name,
            "eve_at_risk":   eve_at_risk,
            "duration_gap":  duration_gap,
            "tier1":         tier1_capital,
            "lcr":           lcr_val,
            "nsfr":          nsfr_val,
            "fx_nop_tl":     fx_nop_tl,
            "total_assets":  total_assets,
            "base_rate":     policy_rate,
            "usdtry":        usdtry,
            "us10y":         us10y,
            "mc_nii_var":    mc_nii_var,
            "sourced":       sourced,
            "timestamps":    cache_timestamps,
        }

    # Generic fallback — return everything
    return {
        "nii": nii, "eve": eve, "lcr": lcr, "fx_gap": fx, "mc": mc,
        "live_rates": live, "sourced": sourced, "timestamps": cache_timestamps,
    }


# ── Helpers ────────────────────────────────────────────────────────────────────

def _load_disk() -> dict:
    if os.path.exists(_CACHE_PATH):
        try:
            with open(_CACHE_PATH, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {}

"""
Bek Rate Desk — Data Health Tracker
=====================================
Tracks the last successful fetch for each external data source and exposes
a per-source freshness status that the UI displays as coloured badges.

Status levels:
  live    🟢  — fetched within the last 10 minutes
  cached  🟡  — fetched within the last 60 minutes
  stale   🔴  — older than 60 minutes, or never fetched (fallback in use)

Usage (in any engine):
  from utils.data_health import mark_fetch, get_health

  mark_fetch("FRED", "live")       # call after a successful FRED API call
  mark_fetch("TCMB_XML", "live")   # call after a successful TCMB XML call
  mark_fetch("EVDS", "fallback")   # call when falling back to hardcoded values
"""

import time as _time
from typing import Literal

# ── State (module-level, shared within process) ───────────────────────────────
_HEALTH: dict[str, dict] = {}

# Freshness thresholds in seconds
_LIVE_TTL   = 600    # 10 min — counts as "live"
_CACHED_TTL = 3600   # 60 min — counts as "cached"; older → "stale"

SourceKey = Literal["FRED", "TCMB_XML", "EVDS", "yfinance", "TCMB_policy"]


def mark_fetch(source: str, status: Literal["live", "cached", "fallback"]) -> None:
    """Record a data fetch event for *source* with the given *status*."""
    _HEALTH[source] = {
        "status":      status,
        "ts":          _time.time(),
        "ts_iso":      _now_iso(),
    }


def get_health() -> dict:
    """
    Return freshness status for all tracked sources.
    Demotes 'live' → 'cached' → 'stale' based on elapsed time.
    """
    now = _time.time()
    result = {}
    for source, entry in _HEALTH.items():
        age = now - entry["ts"]
        raw = entry["status"]

        if raw == "fallback":
            effective = "stale"
        elif age <= _LIVE_TTL:
            effective = "live"
        elif age <= _CACHED_TTL:
            effective = "cached"
        else:
            effective = "stale"

        result[source] = {
            "status":    effective,
            "raw":       raw,
            "age_s":     round(age),
            "fetched_at": entry["ts_iso"],
            "badge":     _badge(effective),
            "label":     _label(effective),
        }

    # Sources we know about but have never seen → show as unknown
    for s in ("FRED", "TCMB_XML", "EVDS", "yfinance", "TCMB_policy"):
        if s not in result:
            result[s] = {
                "status": "unknown", "raw": "unknown", "age_s": None,
                "fetched_at": None,
                "badge": "⚪", "label": "Henüz çekilmedi",
            }

    return result


def _badge(status: str) -> str:
    return {"live": "🟢", "cached": "🟡", "stale": "🔴", "unknown": "⚪"}.get(status, "⚪")


def _label(status: str) -> str:
    return {
        "live":    "Canlı",
        "cached":  "Önbellekten",
        "stale":   "Eski / Fallback",
        "unknown": "Henüz çekilmedi",
    }.get(status, "?")


def _now_iso() -> str:
    import datetime
    return datetime.datetime.now().isoformat(timespec="seconds")

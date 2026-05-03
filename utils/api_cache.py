"""
Bek Rate Desk — In-Process API Response Cache
==============================================
Lightweight TTL cache for external API calls (FRED, EVDS, yfinance).

Design goals:
  • Zero dependencies beyond the stdlib (no redis, no diskcache)
  • Thread-safe via per-key locking (prevents cache stampede)
  • TTL aligned with data_health.py _LIVE_TTL (600 s / 10 min)
  • Transparent decorator — cached functions have identical signatures
  • Cache bypass: pass  force_refresh=True  as a keyword argument to
    any wrapped function to skip the cache and re-fetch live data

Usage:

    from utils.api_cache import ttl_cache

    @ttl_cache(ttl=600, key="fred_yield_curve")
    def fetch_yield_curve_data() -> dict:
        ...

    # Force a live fetch (e.g. triggered by the Settings page):
    result = fetch_yield_curve_data(force_refresh=True)

Global cache control:
    from utils.api_cache import invalidate_all, invalidate_key
    invalidate_all()          # nuke everything (useful on Settings save)
    invalidate_key("fred_yield_curve")

Metrics:
    from utils.api_cache import cache_stats
    cache_stats()  →  {"entries": int, "hits": int, "misses": int, ...}
"""

import functools
import threading
import time
from typing import Any, Callable, Dict, Optional

# ── Module-level state ────────────────────────────────────────────────────────
_STORE:   Dict[str, dict]         = {}   # {cache_key: {value, expires_at}}
_LOCKS:   Dict[str, threading.Lock] = {} # per-key lock to prevent stampedes
_META_LOCK = threading.Lock()            # guards _LOCKS and _STATS dicts

_STATS = {"hits": 0, "misses": 0, "evictions": 0}

DEFAULT_TTL = 600   # 10 minutes — aligned with data_health._LIVE_TTL


# ── Public helpers ────────────────────────────────────────────────────────────

def invalidate_key(cache_key: str) -> None:
    """Remove a single entry from the cache."""
    with _META_LOCK:
        if cache_key in _STORE:
            del _STORE[cache_key]
            _STATS["evictions"] += 1


def invalidate_all() -> None:
    """Flush the entire cache (e.g. after API key change in Settings)."""
    with _META_LOCK:
        n = len(_STORE)
        _STORE.clear()
        _STATS["evictions"] += n


def cache_stats() -> dict:
    """Return cache hit/miss/size metrics."""
    with _META_LOCK:
        return {
            "entries": len(_STORE),
            "hits":    _STATS["hits"],
            "misses":  _STATS["misses"],
            "evictions": _STATS["evictions"],
            "hit_rate_pct": round(
                _STATS["hits"] / max(_STATS["hits"] + _STATS["misses"], 1) * 100, 1
            ),
            "keys": list(_STORE.keys()),
        }


def _get_lock(cache_key: str) -> threading.Lock:
    with _META_LOCK:
        if cache_key not in _LOCKS:
            _LOCKS[cache_key] = threading.Lock()
        return _LOCKS[cache_key]


# ── Decorator ─────────────────────────────────────────────────────────────────

def ttl_cache(ttl: int = DEFAULT_TTL, key: Optional[str] = None) -> Callable:
    """
    Decorator factory.  Wraps a zero-argument (or keyword-only) function with
    a TTL cache.

    Parameters
    ----------
    ttl : int
        Time-to-live in seconds.  Defaults to DEFAULT_TTL (600 s).
    key : str, optional
        Explicit cache key.  If omitted, the wrapped function's __qualname__
        is used.

    The wrapped function accepts one extra keyword-only parameter:
        force_refresh : bool = False
            When True the cache is bypassed for this call and the result
            replaces any existing entry.
    """
    def decorator(func: Callable) -> Callable:
        cache_key = key or func.__qualname__

        @functools.wraps(func)
        def wrapper(*args, force_refresh: bool = False, **kwargs) -> Any:
            lock = _get_lock(cache_key)
            now  = time.monotonic()

            # Fast path — no lock needed for read
            entry = _STORE.get(cache_key)
            if entry and not force_refresh and now < entry["expires_at"]:
                with _META_LOCK:
                    _STATS["hits"] += 1
                return entry["value"]

            # Slow path — acquire per-key lock to prevent stampede
            with lock:
                # Double-check under lock (another thread may have populated it)
                entry = _STORE.get(cache_key)
                if entry and not force_refresh and now < entry["expires_at"]:
                    with _META_LOCK:
                        _STATS["hits"] += 1
                    return entry["value"]

                # Actually call the underlying function
                result = func(*args, **kwargs)

                _STORE[cache_key] = {
                    "value":      result,
                    "expires_at": time.monotonic() + ttl,
                    "fetched_at": time.time(),
                }
                with _META_LOCK:
                    _STATS["misses"] += 1

                return result

        # Expose cache management on the wrapper itself
        wrapper.invalidate   = lambda: invalidate_key(cache_key)
        wrapper.cache_key    = cache_key
        wrapper.ttl          = ttl
        return wrapper

    return decorator

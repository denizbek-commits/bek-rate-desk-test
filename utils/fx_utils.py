"""
Bek Rate Desk — Shared FX Utilities
=====================================
Single source-of-truth for FX primitives used by trading_engine.py
and dealer_engine.py.  Import from here; never re-define locally.

  _pip_size()           — pip size for any currency pair
  _business_days_add()  — T+N value date (weekends + TR public holidays skipped)
  _is_tr_holiday()      — True when a date is a Turkish public holiday
  TYPICAL_SPREADS       — interbank/retail/corporate/VIP spread table (pips)
  CUSTOMER_TYPES        — label and spread-multiplier per client tier

Turkish holiday coverage:
  Fixed national holidays are hardcoded (1 Ocak, 23 Nisan, 1 Mayıs, 19 Mayıs,
  15 Temmuz, 30 Ağustos, 29 Ekim).
  Lunar holidays (Ramazan Bayramı 3 days + Kurban Bayramı 4½ days) shift ~11 days
  earlier each Gregorian year.  A pre-computed lookup table covers 2023-2030.
  Dates outside that range fall back to the fixed-holiday set only.

  For production beyond 2030 extend _LUNAR_HOLIDAYS or integrate exchange_calendars
  'XIST' (Borsa İstanbul) which tracks TCMB-published holiday calendars.
"""

from datetime import date, timedelta

# ── Spread table (pips) ───────────────────────────────────────────────────────
TYPICAL_SPREADS: dict = {
    "USDTRY": {"interbank": 5,  "retail": 15,  "corporate": 8,  "vip": 5},
    "EURTRY": {"interbank": 8,  "retail": 20,  "corporate": 12, "vip": 8},
    "GBPTRY": {"interbank": 10, "retail": 25,  "corporate": 15, "vip": 10},
    "EURUSD": {"interbank": 2,  "retail": 10,  "corporate": 4,  "vip": 2},
    "GBPUSD": {"interbank": 3,  "retail": 12,  "corporate": 5,  "vip": 3},
    "USDJPY": {"interbank": 3,  "retail": 15,  "corporate": 6,  "vip": 3},
}

# ── Customer tiers ────────────────────────────────────────────────────────────
CUSTOMER_TYPES: dict = {
    "retail":    {"label": "Bireysel",     "spread_mult": 1.0},
    "corporate": {"label": "Kurumsal",     "spread_mult": 0.6},
    "vip":       {"label": "VIP/Prime",    "spread_mult": 0.4},
    "interbank": {"label": "Bankalararası","spread_mult": 0.2},
}


# ── Turkish public holiday calendar ──────────────────────────────────────────
# Fixed national holidays (month, day) — repeat every year
_TR_FIXED_HOLIDAYS: set = {
    (1,  1),   # Yılbaşı          — New Year's Day
    (4, 23),   # Ulusal Egemenlik ve Çocuk Bayramı
    (5,  1),   # İşçi Bayramı     — Labour Day
    (5, 19),   # Atatürk'ü Anma, Gençlik ve Spor Bayramı
    (7, 15),   # Demokrasi ve Millî Birlik Günü
    (8, 30),   # Zafer Bayramı    — Victory Day
    (10,29),   # Cumhuriyet Bayramı — Republic Day (eve + day)
    (10,28),   # Republic Day Eve (half-day; treated as full closure)
}

# Lunar holidays: (year, month, day) for each holiday day.
# Ramazan Bayramı = 3 days starting Arife (eve); Kurban Bayramı = 4 days + Arife.
# Source: Turkish Official Gazette holiday announcements 2023-2030.
_LUNAR_HOLIDAYS: set = {
    # ── 2023 ────────────────────────────────────────────────
    # Ramazan Bayramı (Eid al-Fitr) 20–23 Nisan
    (2023, 4, 20), (2023, 4, 21), (2023, 4, 22), (2023, 4, 23),
    # Kurban Bayramı (Eid al-Adha) 27 Haziran – 1 Temmuz
    (2023, 6, 27), (2023, 6, 28), (2023, 6, 29), (2023, 6, 30), (2023, 7, 1),

    # ── 2024 ────────────────────────────────────────────────
    # Ramazan Bayramı 9–12 Nisan (Arife 9 Nisan)
    (2024, 4,  9), (2024, 4, 10), (2024, 4, 11), (2024, 4, 12),
    # Kurban Bayramı 15–19 Haziran (Arife 15 Haziran)
    (2024, 6, 15), (2024, 6, 16), (2024, 6, 17), (2024, 6, 18), (2024, 6, 19),

    # ── 2025 ────────────────────────────────────────────────
    # Ramazan Bayramı 29 Mart – 1 Nisan (Arife 29 Mart)
    (2025, 3, 29), (2025, 3, 30), (2025, 3, 31), (2025, 4,  1),
    # Kurban Bayramı 5–9 Haziran (Arife 5 Haziran)
    (2025, 6,  5), (2025, 6,  6), (2025, 6,  7), (2025, 6,  8), (2025, 6,  9),

    # ── 2026 ────────────────────────────────────────────────
    # Ramazan Bayramı 19–22 Mart (Arife 19 Mart)
    (2026, 3, 19), (2026, 3, 20), (2026, 3, 21), (2026, 3, 22),
    # Kurban Bayramı 26–30 Mayıs (Arife 26 Mayıs)
    (2026, 5, 26), (2026, 5, 27), (2026, 5, 28), (2026, 5, 29), (2026, 5, 30),

    # ── 2027 ────────────────────────────────────────────────
    # Ramazan Bayramı 8–11 Mart (Arife 8 Mart)
    (2027, 3,  8), (2027, 3,  9), (2027, 3, 10), (2027, 3, 11),
    # Kurban Bayramı 15–19 Mayıs (Arife 15 Mayıs)
    (2027, 5, 15), (2027, 5, 16), (2027, 5, 17), (2027, 5, 18), (2027, 5, 19),

    # ── 2028 ────────────────────────────────────────────────
    # Ramazan Bayramı 25–28 Şubat (Arife 25 Şubat)
    (2028, 2, 25), (2028, 2, 26), (2028, 2, 27), (2028, 2, 28),
    # Kurban Bayramı 3–7 Mayıs (Arife 3 Mayıs)
    (2028, 5,  3), (2028, 5,  4), (2028, 5,  5), (2028, 5,  6), (2028, 5,  7),

    # ── 2029 ────────────────────────────────────────────────
    # Ramazan Bayramı 14–17 Şubat (Arife 14 Şubat)
    (2029, 2, 14), (2029, 2, 15), (2029, 2, 16), (2029, 2, 17),
    # Kurban Bayramı 22–26 Nisan (Arife 22 Nisan)
    (2029, 4, 22), (2029, 4, 23), (2029, 4, 24), (2029, 4, 25), (2029, 4, 26),

    # ── 2030 ────────────────────────────────────────────────
    # Ramazan Bayramı 2–5 Şubat (Arife 2 Şubat)
    (2030, 2,  2), (2030, 2,  3), (2030, 2,  4), (2030, 2,  5),
    # Kurban Bayramı 11–15 Nisan (Arife 11 Nisan)
    (2030, 4, 11), (2030, 4, 12), (2030, 4, 13), (2030, 4, 14), (2030, 4, 15),
}


def _is_tr_holiday(d: date) -> bool:
    """Return True if *d* is a Turkish public holiday (fixed or lunar)."""
    if (d.month, d.day) in _TR_FIXED_HOLIDAYS:
        return True
    return (d.year, d.month, d.day) in _LUNAR_HOLIDAYS


def _pip_size(pair: str) -> float:
    """Return the pip size for a currency pair."""
    pair = pair.upper().replace("/", "")
    if "JPY" in pair:
        return 0.01
    # TRY pairs quote to 4 decimal places (e.g. 44.8521)
    if pair.endswith("TRY") or pair.startswith("TRY"):
        return 0.0001
    return 0.0001


def _business_days_add(start: date, days: int) -> date:
    """
    Add *days* Turkish business days to *start*.

    Skips:
      • Weekends (Saturday & Sunday)
      • Turkish fixed national holidays (_TR_FIXED_HOLIDAYS)
      • Turkish lunar holidays for 2023–2030 (_LUNAR_HOLIDAYS)

    Dates outside 2023–2030 still skip weekends and fixed holidays; lunar
    holidays are not excluded in that range — extend _LUNAR_HOLIDAYS if needed.
    """
    current = start
    added   = 0
    while added < days:
        current += timedelta(days=1)
        if current.weekday() < 5 and not _is_tr_holiday(current):
            added += 1
    return current

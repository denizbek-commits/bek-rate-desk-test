"""
Bek Rate Desk — Active Balance Sheet Session
=============================================
Persists the user's current balance sheet across all ALM modules:
  NII Sensitivity, EVE / Duration Gap, FTP Pricing, Monte Carlo, Morning Brief.

Session file: data/balance_sheet_session.json

Schema:
  {
    "name":          str   — user-defined label (e.g. "April 2026 – Q2 Plan")
    "saved_at":      ISO   — last save timestamp
    "base_rate":     float — current base / policy rate (%)
    "tier1_capital": float — Tier-1 capital (M TL or $)
    "assets":        list  — [{name, amount, rate, repricing_months, maturity_years,
                               coupon_freq, type}, ...]
    "liabilities":   list  — same schema as assets
  }

Usage:
    from utils.balance_sheet_session import load_session, save_session, get_session_summary, clear_session
"""

import os, json
from datetime import datetime
from typing import Optional
from utils.io_utils import _atomic_json_write

_SESSION_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "data", "balance_sheet_session.json"
)

# ── Default sample balance sheet ──────────────────────��───────────────────────
_SAMPLE_ASSETS = [
    {"name": "Sabit Getirili Kredi Portföyü",   "amount": 2500.0, "rate": 24.50, "repricing_months": 36, "maturity_years": 3.0,  "coupon_freq": 2, "type": "fixed"},
    {"name": "Değişken Faizli Ticari Kredi",     "amount": 1800.0, "rate": 22.00, "repricing_months":  3, "maturity_years": 1.5,  "coupon_freq": 4, "type": "floating"},
    {"name": "Hazine Bonosu Portföyü",           "amount":  800.0, "rate": 30.00, "repricing_months": 12, "maturity_years": 2.0,  "coupon_freq": 2, "type": "fixed"},
    {"name": "Gecelik Repo Varlıkları",          "amount":  400.0, "rate": 45.00, "repricing_months":  1, "maturity_years": 0.08, "coupon_freq": 12,"type": "floating"},
    {"name": "Menkul Kıymet Portföyü",           "amount":  500.0, "rate": 28.50, "repricing_months": 24, "maturity_years": 4.0,  "coupon_freq": 2, "type": "fixed"},
]

_SAMPLE_LIABILITIES = [
    {"name": "Mevduat — 3 Aylık TL",            "amount": 2200.0, "rate": 38.00, "repricing_months":  3, "maturity_years": 0.25, "coupon_freq": 4, "type": "fixed"},
    {"name": "Mevduat — 1 Yıllık TL",           "amount": 1500.0, "rate": 35.00, "repricing_months": 12, "maturity_years": 1.0,  "coupon_freq": 2, "type": "fixed"},
    {"name": "Sendikasyon Kredisi — Değişken",   "amount":  900.0, "rate": 20.00, "repricing_months":  6, "maturity_years": 2.0,  "coupon_freq": 2, "type": "floating"},
    {"name": "Merkez Bankası Fonlaması",         "amount":  600.0, "rate": 46.00, "repricing_months":  1, "maturity_years": 0.08, "coupon_freq": 12,"type": "floating"},
    {"name": "Uzun Vadeli Tahvil İhracı",        "amount":  300.0, "rate": 27.00, "repricing_months": 60, "maturity_years": 5.0,  "coupon_freq": 2, "type": "fixed"},
]


def load_session() -> Optional[dict]:
    """Load the active balance sheet session. Returns None if no session saved."""
    if not os.path.exists(_SESSION_PATH):
        return None
    try:
        with open(_SESSION_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        # Validate minimal keys
        if "assets" in data and "liabilities" in data:
            # Normalise on load so old sessions get maturity_years filled in
            data["assets"]      = [_normalise_instrument(a) for a in data["assets"]]
            data["liabilities"] = [_normalise_instrument(l) for l in data["liabilities"]]
            return data
    except Exception:
        pass
    return None


def _normalise_instrument(item: dict) -> dict:
    """
    Ensure each instrument has both repricing_months and maturity_years.
    NII Sensitivity stores repricing_months; EVE needs maturity_years.
    We derive whichever is missing so both engines get valid inputs.
    """
    item = dict(item)
    rp  = item.get("repricing_months")
    mat = item.get("maturity_years")
    itype = item.get("type", "fixed").lower()

    if mat is None or mat == 0:
        if rp is not None and rp > 0:
            # Derive maturity from repricing period
            # Fixed: assume maturity ≈ repricing (e.g. 3Y fixed → 36mo repricing)
            # Floating: maturity ≈ a bit longer than repricing
            item["maturity_years"] = round(float(rp) / 12.0, 2)
        else:
            item["maturity_years"] = 1.0  # safe default

    if rp is None or rp == 0:
        if mat is not None and mat > 0:
            item["repricing_months"] = max(1, round(float(mat) * 12))
        else:
            item["repricing_months"] = 12  # safe default

    # Ensure coupon_freq exists
    if not item.get("coupon_freq"):
        item["coupon_freq"] = 2

    return item


def save_session(payload: dict) -> dict:
    """
    Persist the balance sheet session.
    payload must contain 'assets' and 'liabilities'.
    Optional: 'name', 'base_rate', 'tier1_capital'.
    Returns the saved session dict.
    """
    os.makedirs(os.path.dirname(_SESSION_PATH), exist_ok=True)
    session = {
        "name":          payload.get("name", "Active Balance Sheet"),
        "saved_at":      datetime.now().isoformat(timespec="seconds"),
        "base_rate":     float(payload.get("base_rate", 46.0)),
        "tier1_capital": float(payload.get("tier1_capital", 1500.0)),
        "assets":        [_normalise_instrument(a) for a in payload.get("assets", [])],
        "liabilities":   [_normalise_instrument(l) for l in payload.get("liabilities", [])],
    }
    _atomic_json_write(_SESSION_PATH, session)
    return session


def clear_session() -> None:
    """Clear the active session (writes an empty marker so has_session is False)."""
    try:
        os.makedirs(os.path.dirname(_SESSION_PATH), exist_ok=True)
        _atomic_json_write(_SESSION_PATH, {"_cleared": True})
    except Exception:
        pass


def get_session_summary() -> dict:
    """
    Returns a compact summary dict for Morning Brief and status display.
    Always returns a dict (with has_session=False if nothing saved).
    """
    session = load_session()
    if not session:
        return {
            "has_session": False,
            "name":        None,
            "saved_at":    None,
            "n_assets":    0,
            "n_liabilities": 0,
            "total_assets":      0.0,
            "total_liabilities": 0.0,
            "net_gap":           0.0,
            "base_rate":         None,
            "tier1_capital":     None,
        }

    assets      = session.get("assets",      [])
    liabilities = session.get("liabilities", [])
    total_a     = sum(float(a.get("amount", 0)) for a in assets)
    total_l     = sum(float(l.get("amount", 0)) for l in liabilities)

    return {
        "has_session":       True,
        "name":              session.get("name", "Active Balance Sheet"),
        "saved_at":          session.get("saved_at"),
        "n_assets":          len(assets),
        "n_liabilities":     len(liabilities),
        "total_assets":      round(total_a, 2),
        "total_liabilities": round(total_l, 2),
        "net_gap":           round(total_a - total_l, 2),
        "base_rate":         session.get("base_rate"),
        "tier1_capital":     session.get("tier1_capital"),
    }


def load_sample_session() -> dict:
    """Convenience: returns the built-in sample balance sheet (does not persist it)."""
    return {
        "name":          "Sample Balance Sheet (TR Bank)",
        "saved_at":      datetime.now().isoformat(timespec="seconds"),
        "base_rate":     46.0,
        "tier1_capital": 1500.0,
        "assets":        _SAMPLE_ASSETS,
        "liabilities":   _SAMPLE_LIABILITIES,
    }

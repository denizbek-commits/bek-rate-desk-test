"""
Bek Rate Desk — Trading Desk Engine
=====================================
FX Dealer'ın günlük iş araçları:

A. Spot FX Fiyatlama    — cross rate, bid/ask spread, müşteri fiyatı
B. Forward Fiyatlama    — faiz paritesi, swap puanı, valör hesabı
C. FX Swap Fiyatlama    — near/far leg, all-in maliyet, rollover
D. Pozisyon Defteri     — gün içi açık pozisyon, P&L, limit takibi
E. Cross Rate Matrisi   — multi-CCY matrix, arbitraj tespiti

Türkiye bağlamı: TCMB seansları, valör kuralları, BRSA limitleri
"""

import json, os, math
from utils.io_utils import _atomic_json_write
from datetime import datetime, date, timedelta
from typing import Optional, List

DATA_DIR     = os.path.join(os.path.dirname(os.path.dirname(__file__)), "data")
POSITION_FILE= os.path.join(DATA_DIR, "fx_positions.json")
os.makedirs(DATA_DIR, exist_ok=True)

# ──────────────────────────────────────────────────────────────────────────────
# TCMB SEANS VE VALÖR KURALLARI
# ──────────────────────────────────────────────────────────────────────────────
TCMB_SESSIONS = {
    "BIST_FX":   {"open": "09:30", "close": "17:30", "note": "BIST Döviz Pazarı"},
    "INTERBANK": {"open": "08:00", "close": "17:00", "note": "Bankalararası FX"},
    "TCMB_REPO": {"open": "10:00", "close": "11:00", "note": "TCMB Repo İhalesi"},
    "TCMB_DEPO": {"open": "15:30", "close": "16:00", "note": "TCMB Depo İhalesi"},
}

# Spot valör: işlem günü + 2 iş günü (T+2)
# TL/USD için Türkiye'de genellikle T+2
# TL spot bazı işlemler için T+0 veya T+1 de mümkün

# Shared FX primitives — single source of truth
from utils.fx_utils import TYPICAL_SPREADS, CUSTOMER_TYPES, _pip_size, _business_days_add


# ══════════════════════════════════════════════════════════════════════════════
# A. SPOT FX FİYATLAMA
# ══════════════════════════════════════════════════════════════════════════════

def calculate_spot_fx(data: dict) -> dict:
    """
    Spot FX işlemi fiyatlama ve kâr hesabı.

    Input: {
      base_pair: "USDTRY",          # ana parite
      spot_bid: float,               # interbank alış
      spot_ask: float,               # interbank satış
      action: "buy"|"sell",          # bankanın aksiyonu
      amount: float,                 # base currency miktarı
      amount_ccy: "base"|"quote",    # miktar hangi CCY cinsinden
      customer_type: "retail"|"corporate"|"vip"|"interbank",
      custom_spread_pips: int,       # opsiyonel — elle spread gir
      value_date: "spot"|"tom"|"tod" # valör (default: spot = T+2)
    }
    """
    pair         = data.get("base_pair", "USDTRY").upper().replace("/","")
    bid          = float(data.get("spot_bid", 44.80))
    ask          = float(data.get("spot_ask", 44.85))
    action       = data.get("action", "sell").lower()   # bankanın aksiyonu
    amount       = float(data.get("amount", 1_000_000))
    amount_ccy   = data.get("amount_ccy", "base")
    cust_type    = data.get("customer_type", "corporate")
    custom_spread= data.get("custom_spread_pips")
    value_type   = data.get("value_date", "spot")

    pip     = _pip_size(pair)
    mid     = round((bid + ask) / 2, 6)
    ib_spread_pips = round((ask - bid) / pip, 1)

    # Müşteri spread
    cust_info = CUSTOMER_TYPES.get(cust_type, CUSTOMER_TYPES["corporate"])
    typical   = TYPICAL_SPREADS.get(pair, {}).get(cust_type, ib_spread_pips * 2)
    if custom_spread is not None:
        cust_spread_pips = float(custom_spread)
    else:
        cust_spread_pips = typical

    # Müşteri fiyatı
    half_spread = cust_spread_pips * pip / 2
    cust_bid    = round(mid - half_spread, 6)
    cust_ask    = round(mid + half_spread, 6)

    # Bankanın aksiyonu:
    # Banka "sell" (müşteriye satıyor) → müşteri ask fiyatından alır
    # Banka "buy"  (müşteriden alıyor) → müşteri bid fiyatından satar
    if action == "sell":
        customer_rate = cust_ask    # müşteri bu fiyattan alır
        bank_hedge    = bid         # banka interbank'ta satın alır (cheapest hedge)
        bank_pnl_pip  = cust_ask - ask   # müşteri ask - interbank ask
    else:
        customer_rate = cust_bid    # müşteri bu fiyattan satar
        bank_hedge    = ask         # banka interbank'ta satar
        bank_pnl_pip  = bid - cust_bid   # interbank bid - müşteri bid

    # Miktar hesabı
    if amount_ccy == "base":
        base_amount  = amount
        quote_amount = round(amount * customer_rate, 2)
    else:
        quote_amount = amount
        base_amount  = round(amount / customer_rate, 2)

    # Banka kârı
    pnl_per_unit   = abs(bank_pnl_pip)
    pnl_total      = round(pnl_per_unit * base_amount, 2)
    pnl_pips       = round(pnl_per_unit / pip, 1)

    # Valör tarihi
    today = date.today()
    if value_type == "tod":
        value_date = today
        value_label= f"Today ({today.strftime('%d.%m.%Y')})"
    elif value_type == "tom":
        value_date = _business_days_add(today, 1)
        value_label= f"Tomorrow ({value_date.strftime('%d.%m.%Y')})"
    else:
        value_date = _business_days_add(today, 2)
        value_label= f"Spot T+2 ({value_date.strftime('%d.%m.%Y')})"

    # CCY ayrıştırma
    if len(pair) == 6:
        base_ccy  = pair[:3]
        quote_ccy = pair[3:]
    else:
        base_ccy = quote_ccy = "?"

    return {
        "pair":          pair,
        "base_ccy":      base_ccy,
        "quote_ccy":     quote_ccy,
        "action":        action,
        "customer_type": cust_type,
        "customer_label":cust_info["label"],
        # Interbank
        "ib_bid":        bid,
        "ib_ask":        ask,
        "ib_mid":        mid,
        "ib_spread_pips":ib_spread_pips,
        # Müşteri
        "cust_bid":      cust_bid,
        "cust_ask":      cust_ask,
        "cust_spread_pips": cust_spread_pips,
        "customer_rate": customer_rate,
        "bank_hedge_rate":bank_hedge,
        # İşlem
        "base_amount":   round(base_amount, 2),
        "quote_amount":  round(quote_amount, 2),
        "value_date":    value_date.isoformat(),
        "value_label":   value_label,
        # Kâr
        "pnl_pips":      pnl_pips,
        "pnl_total_quote":round(pnl_total, 2),
        "pnl_summary":   f"{pnl_pips:.1f} pip = {pnl_total:,.0f} {quote_ccy}",
        # Yorum
        "trade_summary": (
            f"Müşteriye {base_amount:,.0f} {base_ccy} {'satıldı' if action=='sell' else 'alındı'} "
            f"@ {customer_rate} | Banka kârı: {pnl_pips:.1f} pip = {pnl_total:,.0f} {quote_ccy}"
        ),
    }


# ══════════════════════════════════════════════════════════════════════════════
# B. FORWARD FİYATLAMA
# ══════════════════════════════════════════════════════════════════════════════

def calculate_forward(data: dict) -> dict:
    """
    Forward döviz kuru hesabı — faiz paritesi (Interest Rate Parity).

    F = S × (1 + r_quote × T/360) / (1 + r_base × T/360)

    Input: {
      pair: "USDTRY",
      spot_mid: float,
      rate_base: float,    # USD faizi (%)
      rate_quote: float,   # TRY faizi (%)  
      tenor_days: int,     # vade gün sayısı
      tenor_label: str,    # "1W","1M","3M","6M","1Y"
      customer_type: str,
      custom_spread_pips: int,
      amount: float,
      action: "buy"|"sell",
    }
    """
    pair        = data.get("pair", "USDTRY").upper().replace("/","")
    spot        = float(data.get("spot_mid", 44.85))
    r_base      = float(data.get("rate_base", 4.5))   # USD faizi %
    r_quote     = float(data.get("rate_quote", 42.5)) # TRY faizi %
    tenor_days  = int(data.get("tenor_days", 90))
    tenor_label = data.get("tenor_label", f"{tenor_days}D")
    cust_type   = data.get("customer_type", "corporate")
    custom_sp   = data.get("custom_spread_pips")
    amount      = float(data.get("amount", 1_000_000))
    action      = data.get("action", "sell").lower()

    # Faiz paritesi
    T         = tenor_days / 360
    fwd_mid   = round(spot * (1 + r_quote/100 * T) / (1 + r_base/100 * T), 6)

    # Swap puanı (forward premium/discount)
    swap_points     = round(fwd_mid - spot, 6)
    swap_points_pips= round(swap_points / _pip_size(pair), 2)
    premium_pct     = round((fwd_mid / spot - 1) * 100, 4)

    # Müşteri spread
    pip     = _pip_size(pair)
    typical = TYPICAL_SPREADS.get(pair, {}).get(cust_type, 15)
    cust_sp = float(custom_sp) if custom_sp else typical * (1 + tenor_days/365)
    half    = cust_sp * pip / 2
    cust_bid= round(fwd_mid - half, 6)
    cust_ask= round(fwd_mid + half, 6)

    customer_rate = cust_ask if action == "sell" else cust_bid

    # Valör
    today      = date.today()
    value_date = _business_days_add(today, 2)   # spot date
    fwd_date   = value_date + timedelta(days=tenor_days)

    # P&L estimate
    hedge_rate = fwd_mid
    pnl_pip    = abs(customer_rate - hedge_rate) / pip
    pnl_total  = round(pnl_pip * pip * amount, 2) if "TRY" in pair else round(pnl_pip * pip * amount, 2)

    # Kırılım tablosu — farklı tenorlar için
    tenors = [
        ("1W",  7), ("2W", 14), ("1M", 30), ("2M", 60), ("3M", 90),
        ("6M", 180), ("9M", 270), ("1Y", 360), ("2Y", 720),
    ]
    forward_curve = []
    for lbl, days in tenors:
        t    = days / 360
        fmid = round(spot * (1 + r_quote/100 * t) / (1 + r_base/100 * t), 4)
        fps  = round(fmid - spot, 4)   # swap points in price terms (not pips — TRY pairs too large)
        forward_curve.append({
            "tenor": lbl, "days": days,
            "forward_mid": fmid,
            "swap_points_pips": fps,
            "premium_pct": round((fmid/spot - 1)*100, 3),
        })

    return {
        "pair":            pair,
        "spot_mid":        spot,
        "tenor_days":      tenor_days,
        "tenor_label":     tenor_label,
        "rate_base":       r_base,
        "rate_quote":      r_quote,
        # Forward hesabı
        "forward_mid":     fwd_mid,
        "swap_points":     swap_points,
        "swap_points_pips":swap_points_pips,
        "premium_pct":     premium_pct,
        "premium_discount":"premium" if swap_points > 0 else "discount",
        # Müşteri
        "customer_type":   cust_type,
        "cust_bid":        cust_bid,
        "cust_ask":        cust_ask,
        "customer_rate":   customer_rate,
        "cust_spread_pips":round(cust_sp, 1),
        # İşlem
        "amount":          amount,
        "action":          action,
        "spot_date":       value_date.isoformat(),
        "value_date":      fwd_date.isoformat(),
        "pnl_pips":        round(pnl_pip, 1),
        "pnl_total":       pnl_total,
        # Tablo
        "forward_curve":   forward_curve,
        # Yorum
        "interpretation": (
            f"{tenor_label} forward: {fwd_mid:.4f} "
            f"({'premium' if swap_points>0 else 'discount'} +{abs(swap_points):.4f} TL). "
            f"TRY faizi ({r_quote}%) > USD faizi ({r_base}%) → USD forward premium, "
            f"yani {tenor_label} vadede dolar daha pahalı."
        ),
    }


# ══════════════════════════════════════════════════════════════════════════════
# C. FX SWAP FİYATLAMA
# ══════════════════════════════════════════════════════════════════════════════

def calculate_fx_swap(data: dict) -> dict:
    """
    FX Swap fiyatlama:
    Near leg (spot): bugün döviz sat, TL al
    Far leg (forward): vadede dövizi geri al, TL öde

    Kullanım: kısa vadeli TL likidite ihtiyacı için
    Maliyet = TL faizi - USD faizi (yaklaşık)

    Input: {
      pair: "USDTRY",
      spot_bid: float, spot_ask: float,
      near_action: "sell"|"buy",   # near leg'de bankanın aksiyonu
      near_days: int,              # near leg valörü (genellikle 0 veya 2)
      far_days: int,               # far leg valörü
      amount: float,               # USD miktarı
      rate_usd: float,             # USD faizi
      rate_try: float,             # TRY faizi
    }
    """
    pair        = data.get("pair", "USDTRY").upper().replace("/","")
    bid         = float(data.get("spot_bid", 44.80))
    ask         = float(data.get("spot_ask", 44.85))
    near_action = data.get("near_action", "sell").lower()
    near_days   = int(data.get("near_days", 2))     # T+2 spot
    far_days    = int(data.get("far_days", 9))       # 1 hafta sonrası
    amount      = float(data.get("amount", 10_000_000))
    r_usd       = float(data.get("rate_usd", 4.5))
    r_try       = float(data.get("rate_try", 42.5))

    mid         = (bid + ask) / 2
    pip         = _pip_size(pair)

    # Near leg fiyatı
    if near_action == "sell":
        near_rate = bid    # banka USD satıyor (alış fiyatından satın alır)
        far_action = "buy"
    else:
        near_rate = ask
        far_action = "sell"

    # Far leg — forward hesabı
    far_tenor_days = far_days - near_days
    t_near = near_days / 360
    t_far  = far_days  / 360

    fwd_near = mid * (1 + r_try/100 * t_near) / (1 + r_usd/100 * t_near)
    fwd_far  = mid * (1 + r_try/100 * t_far)  / (1 + r_usd/100 * t_far)

    swap_points_near = round(fwd_near - mid, 6)
    swap_points_far  = round(fwd_far  - mid, 6)
    net_swap_points  = round(swap_points_far - swap_points_near, 6)
    net_swap_pips    = round(net_swap_points / pip, 2)

    if near_action == "sell":
        near_rate_final = round(fwd_near - abs(bid - mid), 6)
        far_rate_final  = round(fwd_far + abs(ask - mid), 6)
    else:
        near_rate_final = round(fwd_near + abs(ask - mid), 6)
        far_rate_final  = round(fwd_far  - abs(bid - mid), 6)

    # TL nakit akışları
    if near_action == "sell":
        # Near: USD sat → TL al
        near_tl = round(amount * near_rate_final, 2)
        # Far: USD geri al → TL öde
        far_tl  = round(amount * far_rate_final, 2)
        net_tl_cost = round(far_tl - near_tl, 2)
    else:
        near_tl = round(amount * near_rate_final, 2)
        far_tl  = round(amount * far_rate_final, 2)
        net_tl_cost = round(near_tl - far_tl, 2)

    # Annualized cost
    tenor_actual = far_days - near_days
    ann_cost_pct = round(net_tl_cost / (amount * mid) * (360 / max(tenor_actual, 1)) * 100, 3)

    # Valör tarihleri
    today     = date.today()
    spot_date = _business_days_add(today, 2)
    near_date = spot_date if near_days <= 2 else _business_days_add(today, near_days)
    far_date  = _business_days_add(today, far_days)

    return {
        "pair":          pair,
        "amount_usd":    amount,
        "near_action":   near_action,
        "far_action":    far_action,
        # Near leg
        "near_days":     near_days,
        "near_date":     near_date.isoformat(),
        "near_rate":     round(near_rate_final, 4),
        "near_tl":       near_tl,
        # Far leg
        "far_days":      far_days,
        "far_date":      far_date.isoformat(),
        "far_rate":      round(far_rate_final, 4),
        "far_tl":        far_tl,
        # Swap maliyeti
        "swap_points_pips": net_swap_pips,
        "net_tl_flow":   net_tl_cost,
        "annualized_cost_pct": ann_cost_pct,
        "tenor_days":    tenor_actual,
        # Yorum
        "interpretation": (
            f"{tenor_actual} günlük FX swap. Near: {near_action.upper()} {amount/1e6:.1f}M USD @ {near_rate_final:.4f}. "
            f"Far: {far_action.upper()} @ {far_rate_final:.4f}. "
            f"Net TL maliyeti: {net_tl_cost:,.0f} TL | Yıllıklaştırılmış: %{ann_cost_pct:.2f}"
        ),
        "alm_note": (
            "FX Swap ile geçici TL likidite sağlandı. "
            "Far leg'de USD geri alınacak — rollover riski izlenmeli. "
            f"BRSA FX NOP etkisi: Near leg'de {amount/1e6:.1f}M USD pozisyon kısmi kapatılır."
        ),
    }


# ══════════════════════════════════════════════════════════════════════════════
# D. POZİSYON DEFTERİ (GÜN İÇİ)
# ══════════════════════════════════════════════════════════════════════════════

def _load_positions() -> list:
    if not os.path.exists(POSITION_FILE):
        return []
    try:
        return json.load(open(POSITION_FILE))
    except Exception:
        return []

def _save_positions(pos: list):
    _atomic_json_write(POSITION_FILE, pos, ensure_ascii=False)


def add_position(trade: dict) -> dict:
    """
    Yeni işlem ekle (upsert).

    • If *trade* carries an "id" that already exists in the position book the
      existing record is updated in-place (status, timestamp refreshed) rather
      than appending a duplicate entry.
    • New trades without an "id" get a millisecond-precision FX… identifier.
    """
    positions = _load_positions()
    if "id" not in trade:
        # Generate a collision-resistant ID using ms timestamp
        trade["id"] = f"FX{int(datetime.now().timestamp() * 1000)}"

    trade["timestamp"] = datetime.now().isoformat()
    trade["status"]    = trade.get("status", "open")

    # Upsert: update existing record if same ID already in book
    existing_idx = next(
        (i for i, p in enumerate(positions) if p.get("id") == trade["id"]), None
    )
    if existing_idx is not None:
        positions[existing_idx] = trade
        _save_positions(positions)
        return {"added": False, "updated": True, "id": trade["id"]}

    positions.append(trade)
    _save_positions(positions)
    return {"added": True, "updated": False, "id": trade["id"]}


def get_position_book(current_rates: dict = None) -> dict:
    """
    Tüm açık pozisyonları getir, MTM hesapla.

    current_rates: {"USDTRY": 44.85, "EURTRY": 52.10, ...}
    """
    positions = _load_positions()
    current_rates = current_rates or {}

    book = []
    totals = {}

    for p in positions:
        if p.get("status") != "open":
            continue

        pair      = p.get("pair","USDTRY")
        direction = p.get("direction","long")   # long = USD uzun (TRY kısa)
        amount    = float(p.get("amount", 0))
        entry     = float(p.get("entry_rate", 0))
        ccy       = pair[:3] if len(pair)==6 else "USD"

        current = current_rates.get(pair, entry)

        # MTM P&L
        if direction == "long":
            pnl = round((current - entry) * amount, 2)
        else:
            pnl = round((entry - current) * amount, 2)

        pnl_pct = round(pnl / (entry * amount) * 100, 4) if entry else 0

        book.append({
            **p,
            "current_rate": current,
            "mtm_pnl":      pnl,
            "pnl_pct":      pnl_pct,
            "pnl_color":    "green" if pnl >= 0 else "red",
        })

        # CCY toplamları
        if ccy not in totals:
            totals[ccy] = {"long": 0, "short": 0, "net": 0, "pnl": 0}
        if direction == "long":
            totals[ccy]["long"]  += amount
            totals[ccy]["net"]   += amount
        else:
            totals[ccy]["short"] += amount
            totals[ccy]["net"]   -= amount
        totals[ccy]["pnl"] += pnl

    total_pnl = sum(v["pnl"] for v in totals.values())

    return {
        "positions":   book,
        "totals":      totals,
        "total_pnl":   round(total_pnl, 2),
        "n_open":      len(book),
        "generated_at":datetime.now().strftime("%Y-%m-%d %H:%M"),
    }


def close_position(pos_id: str, close_rate: float = None) -> dict:
    """Pozisyon kapat."""
    positions = _load_positions()
    for p in positions:
        if p.get("id") == pos_id:
            p["status"]     = "closed"
            p["close_rate"] = close_rate or p.get("entry_rate")
            p["close_time"] = datetime.now().isoformat()
            amount = float(p.get("amount", 0))
            entry  = float(p.get("entry_rate", 0))
            cr     = float(p["close_rate"])
            if p.get("direction","long") == "long":
                p["realized_pnl"] = round((cr - entry) * amount, 2)
            else:
                p["realized_pnl"] = round((entry - cr) * amount, 2)
    _save_positions(positions)
    return {"closed": True, "id": pos_id}


def get_daily_pnl_summary() -> dict:
    """Günün gerçekleşmiş P&L özeti."""
    positions = _load_positions()
    today_str = date.today().isoformat()
    realized  = 0.0
    count     = 0
    for p in positions:
        if p.get("status") == "closed" and p.get("close_time","").startswith(today_str):
            realized += float(p.get("realized_pnl", 0))
            count    += 1
    return {
        "date":           today_str,
        "realized_pnl":   round(realized, 2),
        "closed_trades":  count,
        "generated_at":   datetime.now().strftime("%H:%M"),
    }


# ══════════════════════════════════════════════════════════════════════════════
# E. CROSS RATE MATRİSİ
# ══════════════════════════════════════════════════════════════════════════════

def calculate_cross_rates(data: dict) -> dict:
    """
    Multi-CCY cross rate matrisi.

    Input: {
      USDTRY: float, EURTRY: float, GBPTRY: float,
      EURUSD: float, GBPUSD: float,
      USDJPY: float  (opsiyonel)
    }

    Her parite için: mid, 24h değişim, spread
    Arbitraj tespiti: teorik vs gerçek cross
    """
    rates = {
        "USDTRY": float(data.get("USDTRY", 44.85)),
        "EURTRY": float(data.get("EURTRY", 52.10)),
        "GBPTRY": float(data.get("GBPTRY", 57.50)),
        "EURUSD": float(data.get("EURUSD", 1.0850)),
        "GBPUSD": float(data.get("GBPUSD", 1.2800)),
    }
    prev = {
        "USDTRY": float(data.get("prev_USDTRY", rates["USDTRY"])),
        "EURTRY": float(data.get("prev_EURTRY", rates["EURTRY"])),
        "EURUSD": float(data.get("prev_EURUSD", rates["EURUSD"])),
    }

    # Cross rate hesabı
    # EUR/TRY teorik = EUR/USD × USD/TRY
    eur_try_theoretical = round(rates["EURUSD"] * rates["USDTRY"], 4)
    gbp_try_theoretical = round(rates["GBPUSD"] * rates["USDTRY"], 4)

    # Arbitraj fırsatı
    eur_try_arb = round(rates["EURTRY"] - eur_try_theoretical, 4)
    gbp_try_arb = round(rates["GBPTRY"] - gbp_try_theoretical, 4)

    # 24 saatlik değişim
    changes = {}
    for pair in ["USDTRY","EURTRY","EURUSD"]:
        prev_rate = prev.get(pair, rates[pair])
        changes[pair] = {
            "change":    round(rates[pair] - prev_rate, 4),
            "change_pct":round((rates[pair]/prev_rate - 1)*100, 3) if prev_rate else 0,
        }

    # TL değer kaybı/kazanç özeti
    tl_vs_usd = changes.get("USDTRY",{}).get("change_pct", 0)
    tl_status = "değer kaybetti" if tl_vs_usd > 0 else "değer kazandı"

    # İşlem önerileri
    arb_opps = []
    if abs(eur_try_arb) > 0.05:
        direction = "sat" if eur_try_arb > 0 else "al"
        arb_opps.append({
            "pair":  "EURTRY",
            "type":  "Cross Arbitraj",
            "note":  f"EURTRY market ({rates['EURTRY']}) vs teorik ({eur_try_theoretical}): "
                     f"fark {eur_try_arb:+.4f} → EURTRY {direction}",
        })

    # Matrix oluştur
    pairs_matrix = [
        {"pair":"USD/TRY","mid":rates["USDTRY"],"prev":prev.get("USDTRY",rates["USDTRY"])},
        {"pair":"EUR/TRY","mid":rates["EURTRY"],"prev":prev.get("EURTRY",rates["EURTRY"]),"theoretical":eur_try_theoretical,"arb":eur_try_arb},
        {"pair":"GBP/TRY","mid":rates["GBPTRY"],"prev":rates["GBPTRY"],"theoretical":gbp_try_theoretical,"arb":gbp_try_arb},
        {"pair":"EUR/USD","mid":rates["EURUSD"],"prev":prev.get("EURUSD",rates["EURUSD"])},
        {"pair":"GBP/USD","mid":rates["GBPUSD"],"prev":rates["GBPUSD"]},
    ]

    for p in pairs_matrix:
        prev_r = p.get("prev", p["mid"])
        p["change"]     = round(p["mid"] - prev_r, 4)
        p["change_pct"] = round((p["mid"]/prev_r - 1)*100, 3) if prev_r else 0
        p["direction"]  = "▲" if p["change"] > 0 else ("▼" if p["change"] < 0 else "—")

    return {
        "rates":                rates,
        "matrix":               pairs_matrix,
        "eur_try_theoretical":  eur_try_theoretical,
        "gbp_try_theoretical":  gbp_try_theoretical,
        "eur_try_arb_pct":      round(eur_try_arb / eur_try_theoretical * 100, 4),
        "gbp_try_arb_pct":      round(gbp_try_arb / gbp_try_theoretical * 100, 4),
        "eur_try_arb":          round(eur_try_arb, 4),
        "gbp_try_arb":          round(gbp_try_arb, 4),
        "arbitrage_opps":       arb_opps,
        "tl_vs_usd_pct":        tl_vs_usd,
        "tl_status":            tl_status,
        "sessions":             TCMB_SESSIONS,
        "generated_at":         datetime.now().strftime("%Y-%m-%d %H:%M"),
    }


# ══════════════════════════════════════════════════════════════════════════════
# SAMPLE DATA
# ══════════════════════════════════════════════════════════════════════════════

def get_sample_market_data() -> dict:
    """
    Gerçek zamanlı piyasa verisi — yfinance üzerinden.
    Bağlantı yoksa makul fallback değerlere döner.
    """
    _FALLBACK = {
        "USDTRY": 38.50, "EURTRY": 41.80, "GBPTRY": 49.20,
        "EURUSD": 1.0850, "GBPUSD": 1.2790,
        "prev_USDTRY": 38.30, "prev_EURTRY": 41.55, "prev_EURUSD": 1.0820,
        "rate_usd": 4.33, "rate_try": 46.00, "rate_eur": 2.40,
        "usdtry_bid": 38.48, "usdtry_ask": 38.52,
        "eurtry_bid": 41.77, "eurtry_ask": 41.83,
        "_source": "fallback",
    }
    try:
        import yfinance as yf
        symbols = ["USDTRY=X", "EURTRY=X", "GBPTRY=X", "EURUSD=X", "GBPUSD=X"]
        raw = yf.download(symbols, period="5d", progress=False, auto_adjust=True)

        if raw.empty:
            return _FALLBACK

        close = raw["Close"] if "Close" in raw else raw
        result = {}
        pair_map = {
            "USDTRY=X": "USDTRY", "EURTRY=X": "EURTRY",
            "GBPTRY=X": "GBPTRY", "EURUSD=X": "EURUSD", "GBPUSD=X": "GBPUSD",
        }
        for sym, pair in pair_map.items():
            if sym in close.columns:
                series = close[sym].dropna()
                if len(series) >= 1:
                    result[pair] = round(float(series.iloc[-1]), 4)
                if len(series) >= 2:
                    result["prev_" + pair] = round(float(series.iloc[-2]), 4)
                else:
                    result["prev_" + pair] = result.get(pair, _FALLBACK.get("prev_" + pair, 0))

        if not result.get("USDTRY"):
            return _FALLBACK

        # Bid/ask from mid with typical interbank spread
        for pair, spread in [("USDTRY", 0.02), ("EURTRY", 0.03), ("GBPTRY", 0.04)]:
            mid = result.get(pair)
            if mid:
                key_lo = pair[:3].lower() + "try_bid"
                key_hi = pair[:3].lower() + "try_ask"
                result[key_lo] = round(mid - spread, 4)
                result[key_hi] = round(mid + spread, 4)

        # Fetch US Fed Funds rate via FRED (fallback to known approx)
        try:
            import os
            fred_key = os.environ.get("FRED_API_KEY", "")
            if fred_key:
                import requests
                r = requests.get(
                    f"https://api.stlouisfed.org/fred/series/observations"
                    f"?series_id=DFF&sort_order=desc&limit=1&api_key={fred_key}&file_type=json",
                    timeout=5)
                val = float(r.json()["observations"][0]["value"])
                result["rate_usd"] = val
            else:
                result["rate_usd"] = _FALLBACK["rate_usd"]
        except Exception:
            result["rate_usd"] = _FALLBACK["rate_usd"]

        # TCMB policy rate — use centralized tcmb_evds helper
        try:
            from utils.tcmb_evds import get_policy_rate as _get_pr
            result["rate_try"] = _get_pr(fallback=_FALLBACK["rate_try"])
        except Exception:
            result["rate_try"] = _FALLBACK["rate_try"]

        result["rate_eur"] = _FALLBACK["rate_eur"]
        result["_source"] = "live"
        return result

    except Exception:
        return _FALLBACK

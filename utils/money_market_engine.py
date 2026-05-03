"""
Bek Rate Desk — Money Market Engine
=====================================
Para Piyasası Masası günlük iş araçları:

A. Günlük Likidite Yönetimi  — açık/fazla tespiti, kaynak optimizasyonu
B. Repo Fiyatlama            — teminat haircut, nakit akışı, rollover
C. TCMB Penceresi            — ihale simülatörü, teklif stratejisi
D. Interbank Limit Takibi    — karşı banka limitleri, kullanım takibi
E. Fonlama Maliyet Raporu    — WACF, mix analizi, günlük özet

Türkiye bağlamı:
  TCMB 1 haftalık repo: politika faizi (%42.50)
  TCMB gecelik borç verme: politika+150bps (%44.00)
  TCMB gecelik borç alma:  politika-150bps (%41.00)
  TLREF: gecelik referans (%43.20)
  Interbank O/N: piyasaya göre değişken
"""

import json, os
from datetime import datetime, date, timedelta
from typing import Optional

DATA_DIR    = os.path.join(os.path.dirname(os.path.dirname(__file__)), "data")
MM_FILE     = os.path.join(DATA_DIR, "mm_positions.json")
IB_FILE     = os.path.join(DATA_DIR, "ib_limits.json")
os.makedirs(DATA_DIR, exist_ok=True)

# ──────────────────────────────────────────────────────────────────────────────
# TCMB FAİZ KORİDORU VE TANIMLARI
# ──────────────────────────────────────────────────────────────────────────────
TCMB_SESSIONS = {
    "repo_ihale":  {"open":"10:00","close":"11:00","note":"TCMB Haftalık Repo İhalesi"},
    "on_lending":  {"open":"16:00","close":"16:30","note":"TCMB Gecelik Borç Verme"},
    "on_borrow":   {"open":"16:00","close":"16:30","note":"TCMB Gecelik Borç Alma"},
    "interbank":   {"open":"09:00","close":"17:00","note":"Bankalararası Para Piyasası"},
    "bist_bond":   {"open":"09:30","close":"17:30","note":"BIST Tahvil Piyasası"},
}

# Devlet tahvili haircut oranları (TCMB teminat değerleme)
HAIRCUT_TABLE = {
    "ON-1M":   0.01,   # %1  — çok kısa vadeli
    "1M-3M":   0.02,   # %2
    "3M-6M":   0.03,   # %3
    "6M-1Y":   0.05,   # %5
    "1Y-2Y":   0.07,   # %7
    "2Y-5Y":   0.10,   # %10
    "5Y-10Y":  0.125,  # %12.5
    "10Y+":    0.15,   # %15
}

# Varsayılan interbank limitleri (eğitim amaçlı)
DEFAULT_IB_LIMITS = [
    {"bank":"Garanti BBVA",  "limit_m":500, "used_m":0, "rating":"AA-", "tenor":"O/N"},
    {"bank":"İş Bankası",    "limit_m":400, "used_m":0, "rating":"AA-", "tenor":"O/N"},
    {"bank":"Yapı Kredi",    "limit_m":350, "used_m":0, "rating":"A+",  "tenor":"O/N"},
    {"bank":"Akbank",        "limit_m":300, "used_m":0, "rating":"A+",  "tenor":"O/N"},
    {"bank":"Ziraat Bankası","limit_m":600, "used_m":0, "rating":"AA",  "tenor":"O/N"},
    {"bank":"Halkbank",      "limit_m":250, "used_m":0, "rating":"A",   "tenor":"O/N"},
    {"bank":"VakıfBank",     "limit_m":250, "used_m":0, "rating":"A",   "tenor":"O/N"},
]


def _load_mm() -> list:
    if not os.path.exists(MM_FILE): return []
    try: return json.load(open(MM_FILE))
    except: return []

def _save_mm(data: list):
    _atomic_json_write(MM_FILE, data, ensure_ascii=False)

def _load_ib() -> list:
    if not os.path.exists(IB_FILE): return list(DEFAULT_IB_LIMITS)
    try: return json.load(open(IB_FILE))
    except: return list(DEFAULT_IB_LIMITS)

def _save_ib(data: list):
    _atomic_json_write(IB_FILE, data, ensure_ascii=False)


# ══════════════════════════════════════════════════════════════════════════════
# A. GÜNLÜK LİKİDİTE YÖNETİMİ
# ══════════════════════════════════════════════════════════════════════════════

def calculate_liquidity_position(data: dict) -> dict:
    """
    Sabah likidite pozisyonu hesabı ve kaynak optimizasyonu.

    Input: {
      opening_balance_m: float,  # gün başı TL bakiye (+ fazla, - açık)
      expected_inflows_m: float, # gün içi beklenen girişler
      expected_outflows_m: float,# gün içi beklenen çıkışlar
      tcmb_collateral_m: float,  # mevcut TCMB teminat havuzu
      tcmb_policy_rate: float,   # TCMB politika faizi %
      tlref: float,              # TLREF gecelik %
      ib_rate: float,            # interbank O/N faizi %
      ib_available_m: float,     # interbank kullanılabilir limit
      reserve_buffer_m: float,   # tutulması gereken asgari rezerv
    }
    """
    opening     = float(data.get("opening_balance_m", -850))
    inflows     = float(data.get("expected_inflows_m", 200))
    outflows    = float(data.get("expected_outflows_m", 150))
    tcmb_coll   = float(data.get("tcmb_collateral_m", 1200))
    from utils.tcmb_evds import get_policy_rate as _get_pr
    policy_rate = float(data.get("tcmb_policy_rate") or _get_pr())
    tlref       = float(data.get("tlref") or round(policy_rate * 1.016, 2))   # TLREF ≈ policy+70bps
    ib_rate     = float(data.get("ib_rate") or round(policy_rate + 1.0, 2))
    ib_avail    = float(data.get("ib_available_m", 800))
    reserve_buf = float(data.get("reserve_buffer_m", 100))

    # Net pozisyon
    net_position = round(opening + inflows - outflows, 2)
    need         = max(0, -(net_position - reserve_buf))   # ne kadar fonlamaya ihtiyaç var
    excess       = max(0,   net_position - reserve_buf)    # ne kadar fazla var

    # Strateji
    strategy     = []
    total_cost   = 0.0
    funded       = 0.0

    if need > 0:
        # Kaynak optimizasyonu — ucuzdan pahalıya
        # 1. TCMB repo (en ucuz — politika faizi)
        tcmb_max    = min(tcmb_coll * 0.85, need)   # haircut sonrası teminat değeri
        tcmb_amount = min(tcmb_max, need)

        # Ama TCMB ihalesi 10:00 — sabah acil için interbank önce
        now_h = datetime.now().hour
        tcmb_available_now = now_h >= 10

        if tcmb_available_now:
            # TCMB ihalesinden al
            if tcmb_amount > 0:
                cost = round(tcmb_amount * policy_rate/100 / 365, 3)
                strategy.append({
                    "source":    "TCMB Repo İhalesi",
                    "amount_m":  round(tcmb_amount, 2),
                    "rate":      policy_rate,
                    "daily_cost_m": cost,
                    "priority":  1,
                    "note":      f"Teminat: {tcmb_coll}M → maks {tcmb_max:.0f}M",
                })
                funded    += tcmb_amount
                total_cost+= cost
        else:
            # 10:00 öncesi — interbank önce, sonra TCMB
            ib_bridge   = min(ib_avail, need * 0.35)   # açığın %35'i interbank
            ib_cost     = round(ib_bridge * ib_rate/100 / 365, 3)
            if ib_bridge > 0:
                strategy.append({
                    "source":    "Interbank O/N",
                    "amount_m":  round(ib_bridge, 2),
                    "rate":      ib_rate,
                    "daily_cost_m": ib_cost,
                    "priority":  1,
                    "note":      "TCMB açılmadan önce köprü fonlama",
                })
                funded    += ib_bridge
                total_cost+= ib_cost

            # TCMB ihalesi için plan
            tcmb_plan = min(tcmb_max, need - ib_bridge)
            if tcmb_plan > 0:
                tcmb_cost = round(tcmb_plan * policy_rate/100 / 365, 3)
                strategy.append({
                    "source":    "TCMB Repo İhalesi (10:00)",
                    "amount_m":  round(tcmb_plan, 2),
                    "rate":      policy_rate,
                    "daily_cost_m": tcmb_cost,
                    "priority":  2,
                    "note":      "İhale açıldığında fon al, interbank'ı kapat",
                })
                funded    += tcmb_plan
                total_cost+= tcmb_cost

        # Kalan açık interbank ile kapat
        remaining = need - funded
        if remaining > 0 and ib_avail >= remaining:
            ib_cost2 = round(remaining * ib_rate/100 / 365, 3)
            strategy.append({
                "source":    "Interbank O/N (ek)",
                "amount_m":  round(remaining, 2),
                "rate":      ib_rate,
                "daily_cost_m": ib_cost2,
                "priority":  3,
                "note":      "TCMB teminatı yetmezse interbank ile tamamla",
            })
            funded    += remaining
            total_cost+= ib_cost2

    # WACF — ağırlıklı ortalama fonlama maliyeti
    if funded > 0:
        wacf = sum(s["rate"] * s["amount_m"] for s in strategy) / funded
    else:
        wacf = 0

    # Alternatif: tamamı interbank
    all_ib_cost = round(need * ib_rate/100 / 365, 3) if need > 0 else 0
    saving      = round(all_ib_cost - total_cost, 3)

    return {
        "date":             date.today().isoformat(),
        "opening_balance":  opening,
        "expected_inflows": inflows,
        "expected_outflows":outflows,
        "net_position":     net_position,
        "need_m":           round(need, 2),
        "excess_m":         round(excess, 2),
        "reserve_buffer":   reserve_buf,
        "status":           "AÇIK" if need > 0 else ("FAZLA" if excess > 0 else "DENGEDE"),
        "strategy":         strategy,
        "funded_m":         round(funded, 2),
        "unfunded_m":       round(max(0, need - funded), 2),
        "total_daily_cost": round(total_cost, 3),
        "wacf":             round(wacf, 4),
        "all_ib_cost":      all_ib_cost,
        "saving_vs_allIB":  saving,
        "tcmb_sessions":    TCMB_SESSIONS,
        "generated_at":     datetime.now().strftime("%Y-%m-%d %H:%M"),
    }


# ══════════════════════════════════════════════════════════════════════════════
# B. REPO FİYATLAMA
# ══════════════════════════════════════════════════════════════════════════════

def calculate_repo(data: dict) -> dict:
    """
    Repo işlemi fiyatlaması — TCMB veya interbank.

    Input: {
      collateral_type: "govbond"|"tbill"|"eurobond",
      collateral_face_m: float,   # nominal değer
      collateral_price_pct: float,# piyasa fiyatı (% nominal)
      maturity_years: float,       # teminat vadesi (yıl)
      repo_rate: float,            # repo faizi %
      repo_tenor_days: int,        # repo vadesi (gün)
      repo_type: "tcmb"|"interbank"|"bist",
      direction: "borrow"|"lend",  # borç alan mı veren mi
    }
    """
    coll_type   = data.get("collateral_type", "govbond")
    face_m      = float(data.get("collateral_face_m", 1000))
    price_pct   = float(data.get("collateral_price_pct", 98.5)) / 100
    mat_years   = float(data.get("maturity_years", 3))
    from utils.tcmb_evds import get_policy_rate as _get_pr
    repo_rate   = float(data.get("repo_rate") or _get_pr())
    tenor_days  = int(data.get("repo_tenor_days", 1))
    repo_type   = data.get("repo_type", "tcmb")
    direction   = data.get("direction", "borrow")

    # Haircut belirle
    if   mat_years <= 1/12: hc_key = "ON-1M"
    elif mat_years <= 3/12: hc_key = "1M-3M"
    elif mat_years <= 6/12: hc_key = "3M-6M"
    elif mat_years <= 1:    hc_key = "6M-1Y"
    elif mat_years <= 2:    hc_key = "1Y-2Y"
    elif mat_years <= 5:    hc_key = "2Y-5Y"
    elif mat_years <= 10:   hc_key = "5Y-10Y"
    else:                   hc_key = "10Y+"

    haircut         = HAIRCUT_TABLE[hc_key]
    market_value_m  = round(face_m * price_pct, 3)
    collateral_value= round(market_value_m * (1 - haircut), 3)   # TCMB'nin kabul ettiği değer

    # Repo nakit akışları
    repo_amount_m   = collateral_value   # bu kadar TL alırsın
    repo_interest_m = round(repo_amount_m * repo_rate/100 * tenor_days/365, 4)
    repo_repay_m    = round(repo_amount_m + repo_interest_m, 4)

    # Rollover analizi — 30 günlük
    rollover_30d_cost = round(repo_amount_m * repo_rate/100 * 30/365, 3)

    # Alternatif: TCMB vs interbank karşılaştırma
    from utils.tcmb_evds import get_policy_rate as _get_pr
    tcmb_rate  = data.get("tcmb_rate") or _get_pr()
    ib_rate    = data.get("ib_rate", 43.5)
    alt_saving = round(repo_amount_m * (ib_rate - tcmb_rate)/100 * tenor_days/365, 4)

    return {
        # Teminat
        "collateral_type":    coll_type,
        "face_value_m":       face_m,
        "market_price_pct":   round(price_pct*100, 3),
        "market_value_m":     market_value_m,
        "maturity_years":     mat_years,
        "haircut_key":        hc_key,
        "haircut_pct":        round(haircut*100, 1),
        "collateral_value_m": collateral_value,
        # Repo
        "repo_type":          repo_type,
        "direction":          direction,
        "repo_rate":          repo_rate,
        "tenor_days":         tenor_days,
        "repo_amount_m":      repo_amount_m,
        "repo_interest_m":    repo_interest_m,
        "repo_repay_m":       repo_repay_m,
        # Değer tarihleri
        "near_date":          date.today().isoformat(),
        "far_date":           (date.today() + timedelta(days=tenor_days)).isoformat(),
        # Analiz
        "rollover_30d_cost":  rollover_30d_cost,
        "alt_saving_vs_ib":   alt_saving,
        # Yorum
        "summary": (
            f"{face_m:.0f}M nominal teminat → {collateral_value:.1f}M TL repo ({haircut*100:.1f}% haircut). "
            f"{tenor_days} günlük repo @ %{repo_rate} → faiz: {repo_interest_m:.3f}M TL. "
            f"Geri ödeme: {repo_repay_m:.3f}M TL."
        ),
    }


# ══════════════════════════════════════════════════════════════════════════════
# C. TCMB PENCERESİ — İHALE SİMÜLATÖRÜ
# ══════════════════════════════════════════════════════════════════════════════

def simulate_tcmb_auction(data: dict) -> dict:
    """
    TCMB haftalık repo ihalesi simülasyonu.

    TCMB ihalesi nasıl çalışır:
    1. Bankalar teklif verir (miktar + faiz)
    2. TCMB politika faizinden sabit fiyatlı ihale yapar
    3. Teminat havuzun kadar fonlama alabilirsin
    4. Tahsis oranı sisteme olan toplam talebe göre belirlenir

    Input: {
      bank_need_m: float,         # bankanın ihtiyacı
      tcmb_collateral_m: float,   # teminat havuzu
      policy_rate: float,         # TCMB politika faizi
      system_total_demand_m: float,# piyasanın toplam talebi (tahmini)
      tcmb_total_supply_m: float, # TCMB'nin sunacağı miktar (tahmini)
      bid_strategy: "full"|"partial"|"aggressive"
    }
    """
    need        = float(data.get("bank_need_m", 850))
    coll        = float(data.get("tcmb_collateral_m", 1200))
    from utils.tcmb_evds import get_policy_rate as _get_pr
    policy      = float(data.get("policy_rate") or _get_pr())
    sys_demand  = float(data.get("system_total_demand_m", 50000))
    tcmb_supply = float(data.get("tcmb_total_supply_m", 45000))
    strategy    = data.get("bid_strategy", "full")

    # Teklif stratejisi
    coll_limit  = coll * 0.85   # haircut sonrası maks teklif
    if strategy == "full":
        bid_amount = min(need, coll_limit)
        note = "Tüm ihtiyacı tek ihaleden karşıla"
    elif strategy == "partial":
        bid_amount = min(need * 0.65, coll_limit)
        note = "İhtiyacın %65'ini TCMB'den, kalanını interbank'tan al"
    else:  # aggressive
        bid_amount = coll_limit
        note = "Maksimum teklif — fazlayı diğer bankalara ver"

    # Tahsis oranı (sistem geneli)
    alloc_ratio = min(1.0, tcmb_supply / sys_demand) if sys_demand > 0 else 1.0
    allocated   = round(bid_amount * alloc_ratio, 2)
    unfunded    = round(max(0, need - allocated), 2)

    # Maliyet
    daily_cost  = round(allocated * policy/100 / 365, 4)
    weekly_cost = round(allocated * policy/100 * 7/365, 4)

    # Alternatif — interbank ile tamamlama
    ib_rate_assumed = policy + 1.0   # interbank tipik olarak politika+100bps
    ib_cost_unfunded= round(unfunded * ib_rate_assumed/100 / 365, 4) if unfunded > 0 else 0
    total_cost  = round(daily_cost + ib_cost_unfunded, 4)
    wacf        = round(
        (allocated * policy + unfunded * ib_rate_assumed) / (allocated + unfunded), 4
    ) if (allocated + unfunded) > 0 else policy

    # Senaryo analizi — farklı tahsis oranları
    scenarios = []
    for ratio in [0.70, 0.80, 0.90, 1.0]:
        alloc_s  = round(bid_amount * ratio, 2)
        unfund_s = round(max(0, need - alloc_s), 2)
        cost_s   = round(alloc_s * policy/100/365 + unfund_s * ib_rate_assumed/100/365, 4)
        scenarios.append({
            "alloc_ratio":   ratio,
            "tcmb_alloc_m":  alloc_s,
            "ib_need_m":     unfund_s,
            "daily_cost_m":  cost_s,
            "wacf":          round((alloc_s*policy + unfund_s*ib_rate_assumed)/(need) if need else policy, 3),
        })

    return {
        "bank_need_m":       need,
        "tcmb_collateral_m": coll,
        "collateral_limit_m":round(coll_limit, 2),
        "policy_rate":       policy,
        "bid_strategy":      strategy,
        "bid_amount_m":      round(bid_amount, 2),
        "strategy_note":     note,
        # İhale sonucu
        "system_demand_m":   sys_demand,
        "tcmb_supply_m":     tcmb_supply,
        "alloc_ratio_pct":   round(alloc_ratio*100, 1),
        "allocated_m":       allocated,
        "unfunded_m":        unfunded,
        # Maliyet
        "daily_cost_m":      daily_cost,
        "weekly_cost_m":     weekly_cost,
        "ib_cost_unfunded":  ib_cost_unfunded,
        "total_daily_cost":  total_cost,
        "wacf":              wacf,
        # Senaryolar
        "alloc_scenarios":   scenarios,
        "sessions":          TCMB_SESSIONS,
        "generated_at":      datetime.now().strftime("%Y-%m-%d %H:%M"),
    }


# ══════════════════════════════════════════════════════════════════════════════
# D. INTERBANK LİMİT TAKİBİ
# ══════════════════════════════════════════════════════════════════════════════

def get_ib_limits() -> dict:
    """Interbank limit durumunu getir."""
    limits   = _load_ib()
    total    = sum(l["limit_m"] for l in limits)
    used     = sum(l["used_m"]  for l in limits)
    available= total - used

    # Limit kullanım durumu
    for l in limits:
        l["available_m"]  = l["limit_m"] - l["used_m"]
        l["usage_pct"]    = round(l["used_m"] / l["limit_m"] * 100, 1) if l["limit_m"] else 0
        l["status"]       = "DOLU" if l["usage_pct"] >= 90 else ("UYARI" if l["usage_pct"] >= 70 else "AÇIK")

    return {
        "limits":        limits,
        "total_limit_m": total,
        "total_used_m":  used,
        "total_available_m": available,
        "usage_pct":     round(used/total*100, 1) if total else 0,
        "banks_full":    [l["bank"] for l in limits if l["usage_pct"] >= 90],
        "generated_at":  datetime.now().strftime("%Y-%m-%d %H:%M"),
    }


def update_ib_limit(bank: str, used_m: float) -> dict:
    """Interbank kullanımı güncelle."""
    limits = _load_ib()
    for l in limits:
        if l["bank"] == bank:
            l["used_m"] = float(used_m)
            _save_ib(limits)
            return {"updated": True, "bank": bank, "used_m": used_m}
    return {"updated": False, "error": f"{bank} bulunamadı"}


def reset_ib_limits() -> dict:
    """Gün sonu — tüm limitleri sıfırla."""
    limits = _load_ib()
    for l in limits:
        l["used_m"] = 0
    _save_ib(limits)
    return {"reset": True, "date": date.today().isoformat()}


# ══════════════════════════════════════════════════════════════════════════════
# E. FONLAMA MALİYET RAPORU
# ══════════════════════════════════════════════════════════════════════════════

def generate_funding_report(data: dict) -> dict:
    """
    Günlük fonlama maliyet raporu — para masası günü kapatırken kullanır.

    Input: {
      transactions: [
        {source: "TCMB Repo", amount_m: 550, rate: 42.5, tenor_days: 1},
        {source: "Interbank",  amount_m: 300, rate: 43.5, tenor_days: 1},
        ...
      ],
      total_need_m: float,
      policy_rate: float,
      tlref: float,
    }
    """
    transactions = data.get("transactions", [])
    total_need   = float(data.get("total_need_m", 850))
    from utils.tcmb_evds import get_policy_rate as _get_pr
    policy_rate  = float(data.get("policy_rate") or _get_pr())
    tlref        = float(data.get("tlref") or round(policy_rate * 1.016, 2))

    if not transactions:
        # Varsayılan örnek
        transactions = [
            {"source":"TCMB Repo",  "amount_m":550, "rate":42.5, "tenor_days":1},
            {"source":"Interbank",   "amount_m":300, "rate":43.5, "tenor_days":1},
        ]

    # Hesapla
    total_funded = sum(t["amount_m"] for t in transactions)
    total_cost   = sum(
        t["amount_m"] * t["rate"]/100 * t.get("tenor_days",1)/365
        for t in transactions
    )
    wacf = sum(t["amount_m"] * t["rate"] for t in transactions) / total_funded if total_funded else 0

    # Mix analizi
    tcmb_amount = sum(t["amount_m"] for t in transactions if "TCMB" in t["source"])
    ib_amount   = total_funded - tcmb_amount
    tcmb_pct    = round(tcmb_amount/total_funded*100, 1) if total_funded else 0
    ib_pct      = round(ib_amount/total_funded*100, 1)   if total_funded else 0

    # Karşılaştırma — tamamı politika faizinden olsaydı
    optimal_cost = round(total_funded * policy_rate/100 / 365, 4)
    excess_cost  = round(total_cost - optimal_cost, 4)

    # Aylık projeksiyon
    monthly_cost = round(total_cost * 30, 2)

    # İşlem detayları
    for t in transactions:
        t["daily_cost_m"] = round(t["amount_m"] * t["rate"]/100 * t.get("tenor_days",1)/365, 4)
        t["share_pct"]    = round(t["amount_m"]/total_funded*100, 1) if total_funded else 0

    return {
        "date":             date.today().isoformat(),
        "transactions":     transactions,
        "total_need_m":     total_need,
        "total_funded_m":   round(total_funded, 2),
        "total_daily_cost": round(total_cost, 4),
        "wacf":             round(wacf, 3),
        "policy_rate":      policy_rate,
        "tlref":            tlref,
        # Mix
        "tcmb_amount_m":    round(tcmb_amount, 2),
        "ib_amount_m":      round(ib_amount, 2),
        "tcmb_pct":         tcmb_pct,
        "ib_pct":           ib_pct,
        # Verimlilik
        "optimal_cost_m":   optimal_cost,
        "excess_cost_m":    excess_cost,
        "efficiency_pct":   round((1 - excess_cost/total_cost)*100, 1) if total_cost else 100,
        # Projeksiyon
        "monthly_cost_m":   monthly_cost,
        "annual_cost_m":    round(monthly_cost*12, 2),
        "generated_at":     datetime.now().strftime("%Y-%m-%d %H:%M"),
    }


# ══════════════════════════════════════════════════════════════════════════════
# SAMPLE DATA
# ══════════════════════════════════════════════════════════════════════════════

def get_mm_sample() -> dict:
    """
    Örnek veri — TCMB politika faizini EVDS'den almaya çalışır,
    yoksa makul bir sabit değer kullanır.
    """
    policy_rate = _fetch_tcmb_policy_rate()
    return {
        "opening_balance_m":   -850,
        "expected_inflows_m":   200,
        "expected_outflows_m":  150,
        "tcmb_collateral_m":   1200,
        "tcmb_policy_rate":    policy_rate,
        "tlref":               round(policy_rate + 0.70, 2),   # TLREF ≈ politika + ~70bps
        "ib_rate":             round(policy_rate + 1.00, 2),   # IB O/N ≈ politika + ~100bps
        "ib_available_m":      800,
        "reserve_buffer_m":    100,
        "_policy_source":      "live" if policy_rate != 46.0 else "fallback",
    }


def _fetch_tcmb_policy_rate() -> float:
    """
    TCMB 1 haftalık repo (politika) faizini EVDS API'sinden çeker.
    API anahtarı TCMB_EVDS_KEY ortam değişkeninden okunur.
    Başarısız olursa bilinen son değeri döndürür.
    """
    try:
        import os, requests
        key = os.environ.get("TCMB_EVDS_KEY", "")
        if not key:
            return 46.0   # TCMB politika faizi bilinen son değer
        url = (
            "https://evds2.tcmb.gov.tr/service/evds/"
            "series=TP.MB.B.A.G.V.1.TR/"
            f"startDate=01-01-2025&endDate=31-12-2099&type=json&key={key}"
        )
        r = requests.get(url, timeout=6)
        items = r.json().get("items", [])
        if items:
            val = items[-1].get("TP_MB_B_A_G_V_1_TR")
            if val:
                return float(val)
    except Exception:
        pass
    return 46.0


def get_live_mm_rates() -> dict:
    """
    Para piyasası için canlı oran bilgisi:
      - TCMB politika faizi (EVDS)
      - TLREF tahmini (politika + spread)
      - USD/TRY spot (yfinance)
    """
    policy = _fetch_tcmb_policy_rate()
    usdtry = None
    try:
        import yfinance as yf
        t = yf.Ticker("USDTRY=X")
        h = t.history(period="2d")
        if not h.empty:
            usdtry = round(float(h["Close"].iloc[-1]), 4)
    except Exception:
        pass
    return {
        "tcmb_policy_rate": policy,
        "tlref_estimate":   round(policy + 0.70, 2),
        "ib_rate_estimate": round(policy + 1.00, 2),
        "usdtry":           usdtry,
        "generated_at":     datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "_source": "live" if policy != 46.0 else "fallback",
    }

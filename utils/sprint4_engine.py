"""
Bek Rate Desk — Sprint 4 Engine
=================================
D. Pricing Simulator  — deposit & loan ekonomik kârlılık analizi
H. FX Liquidity Ladder — CCY bazında likidite vade merdiveni, swap rollover, stres
J. Hedge Book          — IRS/swap kayıt defteri, MTM, residual gap, P&L
"""

import json, os, math
from utils.io_utils import _atomic_json_write
from datetime import datetime, date, timedelta
from typing import Optional

DATA_DIR     = os.path.join(os.path.dirname(os.path.dirname(__file__)), "data")
HEDGEBOOK_FILE = os.path.join(DATA_DIR, "hedge_book.json")
os.makedirs(DATA_DIR, exist_ok=True)

# ──────────────────────────────────────────────────────────────────────────────
# TÜRKÇE BANKA BAĞLAMI
# ──────────────────────────────────────────────────────────────────────────────
DEPOSIT_INSURANCE_BPS = 15   # TMSF prim tahmini
RESERVE_COST_BPS      = 8    # BDDK zorunlu karşılık fırsat maliyeti

TR_LP_CURVE = {            # TCMB üzeri LP (bps)
    "ON":0,"1M":30,"3M":60,"6M":100,"1Y":150,"2Y":220,"3Y":270,"5Y+":350
}
TR_LP_MONTHS = {"ON":0.5,"1M":1,"3M":3,"6M":6,"1Y":12,"2Y":24,"3Y":36,"5Y+":60}

def _lp(tenor_months: float) -> float:
    pts = sorted(TR_LP_MONTHS.items(), key=lambda x: x[1])
    if tenor_months <= pts[0][1]: return TR_LP_CURVE[pts[0][0]]
    if tenor_months >= pts[-1][1]: return TR_LP_CURVE[pts[-1][0]]
    for i in range(len(pts)-1):
        l0,m0 = pts[i]; l1,m1 = pts[i+1]
        if m0 <= tenor_months <= m1:
            w = (tenor_months-m0)/(m1-m0)
            return TR_LP_CURVE[l0] + w*(TR_LP_CURVE[l1]-TR_LP_CURVE[l0])
    return 150


# ══════════════════════════════════════════════════════════════════════════════
# D. PRICING SIMULATOR
# ══════════════════════════════════════════════════════════════════════════════

CREDIT_SPREADS = {
    "AAA":0.10,"AA":0.25,"A":0.50,"BBB":1.00,
    "BB":2.00,"B":4.00,"CCC":7.00,
    "1":0.10,"2":0.30,"3":0.60,"4":1.20,"5":2.50,"6":4.50,"7":8.00,
}
RWA_WEIGHTS = {
    "mortgage":0.35,"corporate":1.00,"sme":0.75,
    "consumer":1.00,"auto":0.75,"trade_finance":0.80,
}
TYPE_ADDONS = {
    "mortgage":-0.20,"corporate":0.00,"sme":0.50,
    "consumer":1.50,"auto":0.30,"trade_finance":0.20,
}


RATE_TYPE_PARAMS = {
    "fixed": {
        "label":       "Sabit Faiz",
        "label_en":    "Fixed Rate",
        "description": "Vade boyunca sabit. Banka faiz riskini taşır.",
        "lp_multiplier": 1.0,     # tam likidite primi
        "repricing_months": None,  # vade = repricing
    },
    "floating_tlref": {
        "label":       "Değişken — TLREF Bağlı",
        "label_en":    "Floating — TLREF Linked",
        "description": "Her 3 ayda TLREF + spread. Faiz riski müşteride.",
        "lp_multiplier": 0.15,    # çok düşük LP (kısa repricing)
        "repricing_months": 3,
    },
    "floating_3m": {
        "label":       "Değişken — 3 Aylık Yenileme",
        "label_en":    "Floating — 3M Reset",
        "description": "Her 3 ayda piyasa faizine göre yenilenir.",
        "lp_multiplier": 0.15,
        "repricing_months": 3,
    },
    "floating_6m": {
        "label":       "Değişken — 6 Aylık Yenileme",
        "label_en":    "Floating — 6M Reset",
        "description": "Her 6 ayda piyasa faizine göre yenilenir.",
        "lp_multiplier": 0.30,
        "repricing_months": 6,
    },
}

# Gerçekçi parametre rehberi (Türkiye — Nisan 2026)
PRICING_BENCHMARKS = {
    "opex_by_type": {
        "corporate":    {"min": 1.5, "typical": 2.0, "max": 2.5},
        "sme":          {"min": 2.5, "typical": 3.0, "max": 4.0},
        "consumer":     {"min": 3.0, "typical": 3.5, "max": 5.0},
        "mortgage":     {"min": 1.0, "typical": 1.5, "max": 2.0},
        "auto":         {"min": 2.0, "typical": 2.5, "max": 3.5},
        "trade_finance":{"min": 1.5, "typical": 2.0, "max": 3.0},
    },
    "roe_typical":    {"min": 15, "typical": 20, "max": 30},
    "cet1_bddk_min":  10.5,
    "cet1_typical":   {"small": 12, "medium": 14, "large": 16},
    "note": "Kaynak: BDDK Banka İstatistikleri, sektör ortalamalar — Nisan 2026",
}


def simulate_loan_pricing(data: dict) -> dict:
    """
    Kredi fiyat simülasyonu — sabit ve değişken faiz destekli.
    Kullanıcı fiyat girer → ekonomik kârlılık hesaplanır
    VEYA hedef NIM girer → minimum fiyat hesaplanır.

    Çıktı: tam maliyet ayrıştırması + kârlılık matrisi + duyarlılık
    """
    from utils.tcmb_evds import get_policy_rate as _get_pr
    base_rate      = float(data.get("base_rate") or _get_pr())
    tlref          = float(data.get("tlref", base_rate * 1.016))  # TLREF ≈ policy+70bps
    tenor_months   = float(data.get("tenor_months", 12))
    loan_type      = data.get("loan_type", "corporate").lower()
    rate_type      = data.get("rate_type", "fixed").lower()       # fixed / floating_tlref / floating_3m / floating_6m
    rating         = str(data.get("credit_rating", "BBB")).upper()
    amount_m       = float(data.get("amount_m", 100))
    client_rate    = data.get("client_rate")
    client_spread  = data.get("client_spread")    # floating için: TLREF + X bps
    target_nim_bps = data.get("target_nim_bps", 100)
    roe_target     = float(data.get("roe_target_pct", 20))
    opex           = float(data.get("opex_pct", 2.5))
    capital_ratio  = float(data.get("capital_ratio_pct", 12))
    has_prepay     = bool(data.get("has_prepayment", False))
    collateral_pct = float(data.get("collateral_pct", 0))

    # Rate type parametreleri
    rt_params    = RATE_TYPE_PARAMS.get(rate_type, RATE_TYPE_PARAMS["fixed"])
    repricing_m  = rt_params["repricing_months"] or tenor_months
    is_floating  = rate_type != "fixed"

    # ── Maliyet bileşenleri ───────────────────────────────────────────────────
    # Floating kredide LP çok düşük — repricing riski müşteride
    lp_bps       = _lp(repricing_m) * rt_params["lp_multiplier"]
    opt_bps      = 25 if has_prepay else 0

    # Floating için FTP base = TLREF (gecelik referans)
    ftp_base     = tlref if is_floating else base_rate
    ftp_rate     = ftp_base + lp_bps/100 + opt_bps/100

    credit_base  = CREDIT_SPREADS.get(rating, 1.00)
    type_addon   = TYPE_ADDONS.get(loan_type, 0)
    # Teminat indirimi
    collateral_disc = collateral_pct / 100 * 0.5    # %100 teminat → %50 kredi spread azalır
    credit_prem  = max(0, credit_base + type_addon - collateral_disc)

    rwa_weight   = RWA_WEIGHTS.get(loan_type, 1.00)
    capital_chrg = rwa_weight * capital_ratio/100 * roe_target/100 * 100   # %

    # Total minimum rate
    min_rate     = ftp_rate + credit_prem + opex + capital_chrg

    # ── Ekonomik kârlılık (verilen fiyat için) ───────────────────────────────
    if client_rate is not None:
        cr = float(client_rate)
        nim_bps    = round((cr - min_rate) * 100, 1)
        roe_actual = round(nim_bps / (rwa_weight * capital_ratio) * 100, 2) if rwa_weight else 0
        verdict    = "KÂRLI" if nim_bps > 0 else "ZARAR"
        verdict_en = "PROFITABLE" if nim_bps > 0 else "UNPROFITABLE"
    else:
        cr = round(min_rate + float(target_nim_bps or 100)/100, 4)
        nim_bps    = float(target_nim_bps or 100)
        roe_actual = round(nim_bps / (rwa_weight * capital_ratio) * 100, 2) if rwa_weight else 0
        verdict    = "HEDEFLENMİŞ"
        verdict_en = "TARGETED"

    annual_income_m  = round(amount_m * cr / 100, 3)
    annual_min_m     = round(amount_m * min_rate / 100, 3)
    annual_nim_m     = round(amount_m * nim_bps / 10000, 3)
    capital_needed_m = round(amount_m * rwa_weight * capital_ratio / 100, 2)

    # ── Kârlılık matrisi: farklı fiyatlar için NIM ───────────────────────────
    profitability_matrix = []
    for rate_offset in [-1.0, -0.5, 0, 0.5, 1.0, 1.5, 2.0, 3.0]:
        test_rate  = min_rate + rate_offset
        test_nim   = round(rate_offset * 100, 0)
        test_annual= round(amount_m * test_nim / 10000, 2)
        profitability_matrix.append({
            "rate":         round(test_rate, 3),
            "nim_bps":      test_nim,
            "annual_nim_m": test_annual,
            "verdict":      "Kârlı" if test_nim > 0 else "Zararlı",
        })

    # ── Base rate duyarlılığı: TCMB +/-200bps ────────────────────────────────
    sensitivity = []
    for shock in [-2.0, -1.0, -0.5, 0, 0.5, 1.0, 2.0]:
        shocked_ftp  = (base_rate + shock) + lp_bps/100 + opt_bps/100
        shocked_min  = shocked_ftp + credit_prem + opex + capital_chrg
        shocked_nim  = round((cr - shocked_min) * 100, 1)
        sensitivity.append({
            "shock_pct":  shock,
            "ftp":        round(shocked_ftp, 3),
            "min_rate":   round(shocked_min, 3),
            "nim_bps":    shocked_nim,
        })

    # Floating için TLREF spread hesabı
    min_spread_bps = None
    client_spread_val = None
    if is_floating:
        # Minimum spread: minimum_rate - TLREF
        min_spread_bps = round((min_rate - tlref) * 100, 0)
        if client_spread is not None:
            client_spread_val = float(client_spread)
            cr = round(tlref + client_spread_val/100, 4)
            nim_bps = round((cr - min_rate)*100, 1)
            verdict = "KÂRLI" if nim_bps > 0 else "ZARAR"

    # Floating TLREF duyarlılığı
    floating_sensitivity = []
    if is_floating:
        for tlref_shock in [-5.0, -3.0, -2.0, -1.0, 0, 1.0, 2.0]:
            shocked_tlref = tlref + tlref_shock
            shocked_rate  = shocked_tlref + (client_spread_val or min_spread_bps or 0)/100
            shocked_min   = shocked_tlref + lp_bps/100 + credit_prem + opex + capital_chrg
            shocked_nim   = round((shocked_rate - shocked_min)*100, 1)
            floating_sensitivity.append({
                "tlref_shock":  tlref_shock,
                "shocked_tlref":round(shocked_tlref, 2),
                "client_rate":  round(shocked_rate, 3),
                "min_rate":     round(shocked_min, 3),
                "nim_bps":      shocked_nim,
                "verdict":      "Kârlı" if shocked_nim > 0 else "Zararlı",
            })

    return {
        # Instrument
        "loan_type":      loan_type,
        "rate_type":      rate_type,
        "rate_type_label":rt_params["label"],
        "is_floating":    is_floating,
        "repricing_months":repricing_m,
        "rating":         rating,
        "tenor_months":   tenor_months,
        "amount_m":       amount_m,
        # Cost decomposition
        "base_rate":      round(base_rate, 3),
        "tlref":          round(tlref, 3),
        "lp_bps":         round(lp_bps, 1),
        "opt_bps":        opt_bps,
        "ftp_rate":       round(ftp_rate, 4),
        "ftp_base":       round(ftp_base, 3),
        "credit_premium": round(credit_prem, 4),
        "opex":           round(opex, 3),
        "capital_charge": round(capital_chrg, 4),
        "min_rate":       round(min_rate, 4),
        # Floating specific
        "min_spread_bps":    min_spread_bps,
        "client_spread_bps": client_spread_val,
        "floating_note":     rt_params["description"] if is_floating else None,
        # Result
        "client_rate":    round(cr, 4),
        "nim_bps":        round(nim_bps, 1),
        "nim_pct":        round(nim_bps/100, 4),
        "annual_income_m":   annual_income_m,
        "annual_min_m":      annual_min_m,
        "annual_nim_m":      annual_nim_m,
        "capital_needed_m":  capital_needed_m,
        "rwa_weight":     rwa_weight,
        "roe_actual_pct": roe_actual,
        "verdict":        verdict,
        "verdict_en":     verdict_en,
        # Analysis
        "profitability_matrix":  profitability_matrix,
        "rate_sensitivity":      sensitivity,
        "floating_sensitivity":  floating_sensitivity,
        "benchmarks":            PRICING_BENCHMARKS,
    }


def simulate_deposit_pricing(data: dict) -> dict:
    """
    Mevduat kampanya simülasyonu:
    Mevduat faizi gir → NII ve funding mix etkisini gör.
    """
    from utils.tcmb_evds import get_policy_rate as _get_pr
    base_rate      = float(data.get("base_rate") or _get_pr())
    deposit_type   = data.get("deposit_type", "time_3m").lower()
    tenor_months   = float(data.get("tenor_months", 3))
    amount_m       = float(data.get("amount_m", 1000))
    offered_rate   = float(data.get("offered_rate", 42.0))
    current_rate   = float(data.get("current_rate", 40.0))
    beta           = float(data.get("deposit_beta", 0.80))
    portfolio_nii_m= float(data.get("portfolio_nii_m", 620))
    total_deposits_m = float(data.get("total_deposits_m", 6000))

    # FTP rate ve maliyet
    lp_bps      = _lp(tenor_months)
    ftp_rate    = base_rate + lp_bps/100
    all_in_cost = offered_rate + (DEPOSIT_INSURANCE_BPS + RESERVE_COST_BPS)/100
    spread_to_ftp = round(ftp_rate - all_in_cost, 3)     # pozitif = faydalı

    # Mevcut vs kampanya maliyet farkı
    cost_delta_bps    = round((offered_rate - current_rate) * 100, 1)
    annual_cost_delta = round(amount_m * cost_delta_bps / 10000, 2)

    # NII etkisi: bu mevduat portföyde kaçıncı sırada?
    nii_impact_bps  = round(-cost_delta_bps, 1)   # maliyet artar → NII düşer
    nii_impact_m    = round(-annual_cost_delta, 3)
    nii_impact_pct  = round(nii_impact_m / portfolio_nii_m * 100, 3) if portfolio_nii_m else 0

    # Funding mix etkisi
    portfolio_share = round(amount_m / total_deposits_m * 100, 2) if total_deposits_m else 0

    # Rekabetçi eşik: rakip bankalar ne ödüyor? (kaba tahmin)
    competitor_est = round(base_rate * beta, 2)
    competitive_gap = round(offered_rate - competitor_est, 2)

    # Breakeven: bu mevduat hangi kredi fiyatında kârsız olur?
    breakeven_loan_rate = round(ftp_rate + 1.0 + 2.5, 2)  # FTP + avg credit + opex

    # Kampanya ekonomisi: farklı faiz seviyelerinde maliyet
    matrix = []
    for rate_offset in [-1.0, -0.5, -0.25, 0, 0.25, 0.50, 1.0, 1.5]:
        test_rate   = current_rate + rate_offset
        test_cost   = round(test_rate + (DEPOSIT_INSURANCE_BPS + RESERVE_COST_BPS)/100, 3)
        test_spread = round(ftp_rate - test_cost, 3)
        test_nii    = round(-amount_m * rate_offset / 100, 2)
        matrix.append({
            "offered_rate":  round(test_rate, 2),
            "all_in_cost":   test_cost,
            "spread_to_ftp": test_spread,
            "nii_impact_m":  test_nii,
            "verdict":       "Faydalı" if test_spread > 0 else "Maliyetli",
        })

    return {
        "deposit_type":      deposit_type,
        "tenor_months":      tenor_months,
        "amount_m":          amount_m,
        "current_rate":      current_rate,
        "offered_rate":      offered_rate,
        "all_in_cost":       round(all_in_cost, 3),
        "ftp_rate":          round(ftp_rate, 3),
        "lp_bps":            round(lp_bps, 1),
        "spread_to_ftp":     spread_to_ftp,
        "cost_delta_bps":    cost_delta_bps,
        "annual_cost_delta_m": annual_cost_delta,
        "nii_impact_m":      nii_impact_m,
        "nii_impact_pct":    nii_impact_pct,
        "portfolio_share_pct": portfolio_share,
        "competitor_est":    competitor_est,
        "competitive_gap":   competitive_gap,
        "breakeven_loan_rate": breakeven_loan_rate,
        "campaign_matrix":   matrix,
        "verdict":           "FTP ALTI" if spread_to_ftp < 0 else "FTP ÜSTÜ",
        "verdict_detail":    f"Bu mevduat FTP'nin {'altında' if spread_to_ftp<0 else 'üstünde'} — ALM için {'maliyetli' if spread_to_ftp<0 else 'faydalı'}.",
    }


# ══════════════════════════════════════════════════════════════════════════════
# H. FX LIQUIDITY LADDER
# ══════════════════════════════════════════════════════════════════════════════

FX_BUCKETS = [
    ("O/N",   0,  1),
    ("1W",    1,  7),
    ("1M",    7, 30),
    ("3M",   30, 90),
    ("6M",   90,180),
    ("1Y",  180,365),
    ("2Y+", 365,9999),
]


def _bucket_label(days: float) -> str:
    for label, lo, hi in FX_BUCKETS:
        if lo <= days < hi:
            return label
    return "2Y+"


def build_fx_ladder(data: dict) -> dict:
    """
    CCY bazında (TL / USD / EUR) likidite vade merdiveni.

    Her CCY için:
      - Inflow (varlıklar): krediler, menkul kıymetler, swap alacakları
      - Outflow (yükümlülükler): mevduat, wholesale, swap borçları
      - Net gap per bucket
      - Kümülatif gap (survival horizon)

    Swap rollover riski: vadesi gelen swap'ların yenilenmeme riski
    Stres senaryosu: %30 outflow artışı
    """
    positions  = data.get("positions", [])
    usdtry     = float(data.get("usdtry", 38.0))
    eurtry     = float(data.get("eurtry", 42.0))
    stress_pct = float(data.get("stress_outflow_pct", 30))

    def to_tl(amount, ccy):
        if ccy == "USD": return amount * usdtry
        if ccy == "EUR": return amount * eurtry
        return amount

    ccys = ["TL", "USD", "EUR"]
    results = {}

    for ccy in ccys:
        ccy_pos = [p for p in positions if p.get("ccy", "TL").upper() == ccy]
        buckets = {b[0]: {"inflow":0.0,"outflow":0.0,"swap_inflow":0.0,"swap_outflow":0.0}
                   for b in FX_BUCKETS}

        for p in ccy_pos:
            days   = float(p.get("maturity_days", 30))
            amount = float(p.get("amount", 0))
            ptype  = p.get("type", "inflow").lower()   # inflow|outflow|swap_in|swap_out
            bucket = _bucket_label(days)

            if ptype == "inflow":       buckets[bucket]["inflow"]       += amount
            elif ptype == "outflow":    buckets[bucket]["outflow"]      += amount
            elif ptype == "swap_in":    buckets[bucket]["swap_inflow"]  += amount
            elif ptype == "swap_out":   buckets[bucket]["swap_outflow"] += amount

        # Build ladder
        ladder = []
        cumulative = 0.0
        cumulative_stress = 0.0
        for label, lo, hi in FX_BUCKETS:
            b      = buckets[label]
            inflow = b["inflow"] + b["swap_inflow"]
            outflow= b["outflow"] + b["swap_outflow"]
            gap    = inflow - outflow
            stress_out = outflow * (1 + stress_pct/100)
            stress_gap = inflow - stress_out
            cumulative        += gap
            cumulative_stress += stress_gap
            ladder.append({
                "bucket":          label,
                "inflow":          round(inflow, 2),
                "outflow":         round(outflow, 2),
                "swap_inflow":     round(b["swap_inflow"], 2),
                "swap_outflow":    round(b["swap_outflow"], 2),
                "gap":             round(gap, 2),
                "cumulative_gap":  round(cumulative, 2),
                "stress_gap":      round(stress_gap, 2),
                "stress_cum":      round(cumulative_stress, 2),
                "tl_equivalent_gap": round(to_tl(gap, ccy), 2),
            })

        # Survival horizon: son pozitif kümülatif bucket
        survival = "2Y+"
        for row in ladder:
            if row["cumulative_gap"] < 0:
                survival = row["bucket"]
                break

        stress_survival = "2Y+"
        for row in ladder:
            if row["stress_cum"] < 0:
                stress_survival = row["bucket"]
                break

        total_inflow  = sum(r["inflow"]  for r in ladder)
        total_outflow = sum(r["outflow"] for r in ladder)

        results[ccy] = {
            "ccy":             ccy,
            "ladder":          ladder,
            "total_inflow":    round(total_inflow, 2),
            "total_outflow":   round(total_outflow, 2),
            "net_position":    round(total_inflow - total_outflow, 2),
            "survival_horizon":survival,
            "stress_survival": stress_survival,
            "tl_equivalent":   round(to_tl(total_inflow - total_outflow, ccy), 2),
        }

    # Consolidated TL equivalent
    total_tl_net = sum(results[c]["tl_equivalent"] for c in ccys)
    swap_rollover_risk = sum(
        p.get("amount", 0) * (1 if p.get("is_swap_rollover") else 0)
        for p in positions
    )

    return {
        "ccys":               results,
        "total_net_tl":       round(total_tl_net, 2),
        "swap_rollover_risk": round(swap_rollover_risk, 2),
        "usdtry":             usdtry,
        "eurtry":             eurtry,
        "stress_outflow_pct": stress_pct,
        "generated_at":       datetime.now().strftime("%Y-%m-%d %H:%M"),
    }


def fx_ladder_sample() -> dict:
    """Örnek FX ladder pozisyonları."""
    return {
        "usdtry": 38.10,
        "eurtry": 42.20,
        "stress_outflow_pct": 30,
        "positions": [
            # TL
            {"ccy":"TL","type":"inflow",    "amount":800,  "maturity_days":1,   "name":"TL O/N plasman"},
            {"ccy":"TL","type":"inflow",    "amount":1200, "maturity_days":30,  "name":"TL 1M kredi"},
            {"ccy":"TL","type":"inflow",    "amount":900,  "maturity_days":90,  "name":"TL 3M kredi"},
            {"ccy":"TL","type":"outflow",   "amount":1500, "maturity_days":1,   "name":"TL vadesiz mevduat"},
            {"ccy":"TL","type":"outflow",   "amount":1900, "maturity_days":90,  "name":"TL 3M mevduat"},
            {"ccy":"TL","type":"outflow",   "amount":600,  "maturity_days":180, "name":"TL 6M mevduat"},
            {"ccy":"TL","type":"swap_in",   "amount":300,  "maturity_days":30,  "name":"TL TRY-USD swap giriş"},
            {"ccy":"TL","type":"swap_out",  "amount":300,  "maturity_days":30,  "name":"TL swap çıkış", "is_swap_rollover":True},
            # USD
            {"ccy":"USD","type":"inflow",   "amount":20,   "maturity_days":90,  "name":"USD kredi"},
            {"ccy":"USD","type":"inflow",   "amount":15,   "maturity_days":180, "name":"USD tahvil"},
            {"ccy":"USD","type":"outflow",  "amount":25,   "maturity_days":30,  "name":"USD mevduat 1M"},
            {"ccy":"USD","type":"outflow",  "amount":18,   "maturity_days":90,  "name":"USD repo"},
            {"ccy":"USD","type":"swap_out", "amount":10,   "maturity_days":7,   "name":"USD FX swap", "is_swap_rollover":True},
            # EUR
            {"ccy":"EUR","type":"inflow",   "amount":12,   "maturity_days":180, "name":"EUR trade kredi"},
            {"ccy":"EUR","type":"outflow",  "amount":15,   "maturity_days":90,  "name":"EUR mevduat"},
            {"ccy":"EUR","type":"outflow",  "amount":8,    "maturity_days":30,  "name":"EUR interbank"},
        ]
    }


# ══════════════════════════════════════════════════════════════════════════════
# J. HEDGE BOOK / TRADE BLOTTER
# ══════════════════════════════════════════════════════════════════════════════

def _load_book() -> list:
    if not os.path.exists(HEDGEBOOK_FILE):
        return []
    try:
        return json.load(open(HEDGEBOOK_FILE))
    except Exception:
        return []


def _save_book(book: list):
    _atomic_json_write(HEDGEBOOK_FILE, book, ensure_ascii=False)


def _mtm_irs(trade: dict, current_rate: float) -> float:
    """
    IRS mark-to-market (basitleştirilmiş):
    MTM = Σ (fixed_leg - floating_leg) × notional × discount_factor
    Yaklaşım: DV01 × (initial_rate - current_rate) × 10000
    """
    notional     = float(trade.get("notional_m", 0))
    tenor_yrs    = float(trade.get("tenor_years", 1))
    initial_rate = float(trade.get("fixed_rate", 0))
    direction    = trade.get("direction", "receive-fixed")
    trade_date   = datetime.fromisoformat(trade.get("trade_date", datetime.now().isoformat()))
    days_elapsed = (datetime.now() - trade_date).days
    remaining_yr = max(0, tenor_yrs - days_elapsed/365)

    # Modified duration of remaining swap
    md_swap = remaining_yr * 0.88
    dv01    = notional * md_swap / 10000

    # Rate change since inception
    rate_change = current_rate - initial_rate   # bps in decimal
    # Receive-fixed: faiz düşerse → pozitif MTM
    sign = 1 if direction == "receive-fixed" else -1
    mtm  = sign * (-dv01 * rate_change * 100)   # rate_change is %, dv01 per 1bp
    return round(mtm, 3)


def _pnl_irs(trade: dict, current_rate: float) -> dict:
    """Yıllık P&L: floating leg - fixed leg (receive-fixed)."""
    notional  = float(trade.get("notional_m", 0))
    fixed_rate= float(trade.get("fixed_rate", 0))
    direction = trade.get("direction", "receive-fixed")
    freq      = int(trade.get("freq", 4))
    period_cf = notional * (current_rate - fixed_rate) / 100 / freq
    if direction == "pay-fixed":
        period_cf = -period_cf
    annual_pnl = period_cf * freq
    return {
        "annual_pnl_m": round(annual_pnl, 3),
        "period_cf_m":  round(period_cf, 3),
        "freq":         freq,
    }


def add_trade(trade: dict) -> dict:
    """Hedge book'a yeni işlem ekle."""
    book = _load_book()
    if "id" not in trade:
        trade["id"] = f"T{int(datetime.now().timestamp())}"
    trade["entry_date"] = datetime.now().isoformat()
    if "trade_date" not in trade:
        trade["trade_date"] = datetime.now().isoformat()
    book.append(trade)
    _save_book(book)
    return {"added": True, "id": trade["id"], "total_trades": len(book)}


def get_book(current_base_rate: float = 42.5) -> dict:
    """Tüm hedge book'u MTM ve P&L ile döndür."""
    book   = _load_book()
    trades = []
    total_mtm     = 0.0
    total_dv01    = 0.0
    total_notional= 0.0
    total_pnl_m   = 0.0

    for t in book:
        if t.get("status","active") != "active":
            continue
        mtm  = _mtm_irs(t, current_base_rate)
        pnl  = _pnl_irs(t, current_base_rate)
        tenor_yrs = float(t.get("tenor_years", 1))
        notional  = float(t.get("notional_m", 0))
        md_swap   = tenor_yrs * 0.88
        dv01      = notional * md_swap / 10000

        trade_date= datetime.fromisoformat(t.get("trade_date", datetime.now().isoformat()))
        remaining = max(0, tenor_yrs - (datetime.now()-trade_date).days/365)

        trades.append({
            **t,
            "mtm_m":       mtm,
            "dv01_m":      round(dv01, 4),
            "remaining_yr":round(remaining, 2),
            "annual_pnl_m":pnl["annual_pnl_m"],
            "period_cf_m": pnl["period_cf_m"],
        })
        total_mtm      += mtm
        total_dv01     += dv01
        total_notional += notional
        total_pnl_m    += pnl["annual_pnl_m"]

    # Aggregate duration gap contribution
    # Each receive-fixed adds positive duration; pay-fixed subtracts
    dur_contribution = sum(
        t["remaining_yr"] * float(t.get("notional_m",0)) / 1000
        * (1 if t.get("direction")=="receive-fixed" else -1)
        for t in trades
    )

    return {
        "trades":           trades,
        "total_notional_m": round(total_notional, 2),
        "total_mtm_m":      round(total_mtm, 3),
        "total_dv01_m":     round(total_dv01, 4),
        "total_annual_pnl_m": round(total_pnl_m, 3),
        "duration_contribution_yr": round(dur_contribution, 3),
        "current_rate":     current_base_rate,
        "n_trades":         len(trades),
        "generated_at":     datetime.now().strftime("%Y-%m-%d %H:%M"),
    }


def close_trade(trade_id: str) -> dict:
    """Bir işlemi kapat (status → closed)."""
    book = _load_book()
    found = False
    for t in book:
        if t.get("id") == trade_id:
            t["status"] = "closed"
            t["close_date"] = datetime.now().isoformat()
            found = True
    _save_book(book)
    return {"closed": found, "id": trade_id}


def update_trade(trade_id: str, updates: dict) -> dict:
    """İşlem güncelle."""
    book = _load_book()
    for t in book:
        if t.get("id") == trade_id:
            t.update({k:v for k,v in updates.items() if k != "id"})
            _save_book(book)
            return {"updated": True, "id": trade_id}
    return {"updated": False, "id": trade_id}


SAMPLE_TRADES = [
    {
        "id": "T001",
        "instrument":    "IRS",
        "direction":     "receive-fixed",
        "notional_m":    500,
        "fixed_rate":    43.50,
        "tenor_years":   3,
        "freq":          4,
        "counterparty":  "Garanti BBVA",
        "purpose":       "Duration gap hedge — liability-sensitive NII koruması",
        "trade_date":    (datetime.now() - timedelta(days=90)).isoformat(),
        "maturity_date": (datetime.now() + timedelta(days=3*365-90)).strftime("%Y-%m-%d"),
        "status":        "active",
    },
    {
        "id": "T002",
        "instrument":    "IRS",
        "direction":     "receive-fixed",
        "notional_m":    300,
        "fixed_rate":    44.00,
        "tenor_years":   5,
        "freq":          4,
        "counterparty":  "İş Bankası",
        "purpose":       "EVE hedge — IRRBB Tier 1 koruması",
        "trade_date":    (datetime.now() - timedelta(days=30)).isoformat(),
        "maturity_date": (datetime.now() + timedelta(days=5*365-30)).strftime("%Y-%m-%d"),
        "status":        "active",
    },
]

def load_sample_trades() -> dict:
    """Sample trade'leri hedge book'a yükle."""
    book = _load_book()
    existing_ids = {t["id"] for t in book}
    added = 0
    for t in SAMPLE_TRADES:
        if t["id"] not in existing_ids:
            book.append(t)
            added += 1
    _save_book(book)
    return {"loaded": added, "total": len(book)}

"""
ALM Aksiyon Motoru — Senaryo Bazlı Türev İşlem Önerileri
=========================================================
Mevcut bilanço metriklerini (DV01, Duration Gap, Repricing Gap, LCR, NSFR, EVE)
okuyarak her faiz senaryosu için hesaplanmış türev önerileri üretir.

Her senaryo için:
  - Tahmini EVE / NII etkisi
  - Önerilen türev işlem (IRS, FRA, CRS, Tahvil alım-satımı)
  - Hesaplanmış nominal büyüklük (M TL)
  - Tek cümle Türkçe gerekçe (uyarı niteliğinde)
  - Aciliyet seviyesi: ZORUNLU / ÖNERİLİR / İHTİYATİ

Bağlamsal notlar:
  - TCMB repo oranı (default 42.5%) TRY IRS swap rate proxy olarak kullanılır
  - DV01 birimleri: M TL per bps  (alm_engine.py ile tutarlı)
  - Notional birimleri: M TL
  - BRSA IRRBB sınırı: ΔEVE ≤ ±%15 Tier 1 sermaye
"""
from __future__ import annotations
import math
from typing import Optional


# ──────────────────────────────────────────────────────────────────────────────
# IRS modified duration yaklaşımı (yüksek faiz ortamı için)
# ──────────────────────────────────────────────────────────────────────────────
def _irs_mod_duration(tenor_y: float, rate: float) -> float:
    """
    T-yıllık sabit faiz ödemeli IRS (swap) için modified duration.
    Parite fiyatlanmış tahvil yaklaşımı:
      MacD = (1/r) * [1 - 1/(1+r)^T]   (yıllık kupon)
      ModD = MacD / (1+r)
    """
    if rate <= 0 or tenor_y <= 0:
        return tenor_y * 0.75  # rough fallback
    mac = (1.0 / rate) * (1 - 1 / (1 + rate) ** tenor_y)
    return mac / (1 + rate)


def _dv01_per_m_notional(tenor_y: float, rate: float) -> float:
    """
    1M TL nominal IRS'nin DV01'i (M TL per bps).
    DV01 = notional × ModD × 0.0001
    """
    return 1.0 * _irs_mod_duration(tenor_y, rate) * 0.0001


def _notional_for_dv01(target_dv01_m: float, tenor_y: float, swap_rate: float) -> float:
    """
    Hedef DV01 değişimini (M TL/bps) sağlayacak IRS nominalini (M TL) hesaplar.
    """
    dv01_unit = _dv01_per_m_notional(tenor_y, swap_rate)
    if dv01_unit <= 0:
        return 0.0
    return round(abs(target_dv01_m) / dv01_unit, 1)


# ──────────────────────────────────────────────────────────────────────────────
# NII etkisi yaklaşımı (kümülatif 1Y repricing gap üzerinden)
# ──────────────────────────────────────────────────────────────────────────────
def _nii_impact(repricing_gap_1y: float, shock_bps: float) -> float:
    """
    ΔNII ≈ Repricing Gap (1Y) × Δr   (M TL)
    shock_bps: faiz değişimi (bps); ör. +200
    """
    return round(repricing_gap_1y * (shock_bps / 10_000), 2)


# ──────────────────────────────────────────────────────────────────────────────
# Aciliyet sınıflandırması
# ──────────────────────────────────────────────────────────────────────────────
def _urgency(brsa_breach: bool, pct_tier1: float) -> str:
    if brsa_breach or abs(pct_tier1) >= 15:
        return "ZORUNLU"
    if abs(pct_tier1) >= 8:
        return "ÖNERİLİR"
    return "İHTİYATİ"


# ──────────────────────────────────────────────────────────────────────────────
# Senaryo önerileri üretici
# ──────────────────────────────────────────────────────────────────────────────
def generate_recommendations(metrics: dict) -> dict:
    """
    Mevcut bilanço metriklerinden 6 faiz senaryosu için aksiyon önerileri üretir.

    Beklenen metrik anahtarları:
      net_dv01          : float (M TL/bps) — negatif = varlık duyarlı (EVE engine convention: dv01 = -ModD × PV × 0.0001)
      duration_gap      : float (yıl)
      repricing_gap_1y  : float (M TL) — kümülatif 1Y RSA - RSL
      base_eve          : float (M TL)
      tier1_capital     : float (M TL)
      eve_scenarios     : dict  {senaryo_kodu: {delta_eve, brsa_breach, label}}
      lcr               : float (%) — ör. 135.0
      nsfr              : float (%) — ör. 112.0
      total_assets      : float (M TL)
      swap_rate         : float (%) — TRY IRS proxy (default: 42.5)
    """
    net_dv01       = float(metrics.get("net_dv01", 0))
    dur_gap        = float(metrics.get("duration_gap", 0))
    rp_gap_1y      = float(metrics.get("repricing_gap_1y", 0))
    base_eve       = float(metrics.get("base_eve", 0))
    tier1          = float(metrics.get("tier1_capital", 1500))
    eve_sc         = metrics.get("eve_scenarios", {})
    lcr            = float(metrics.get("lcr", 120))
    nsfr           = float(metrics.get("nsfr", 115))
    total_assets   = float(metrics.get("total_assets", 50_000))
    swap_rate      = float(metrics.get("swap_rate", 42.5)) / 100.0  # → ondalık

    results = {}

    # ────────────────────────────────────────────────────────────────────────
    # SENARYO 1 — Paralel Yükseliş (+200bps)
    # ────────────────────────────────────────────────────────────────────────
    sc = eve_sc.get("parallel_up_200", {})
    delta_eve  = sc.get("delta_eve", net_dv01 * 200)   # net_dv01<0 → delta_eve<0 for asset-sensitive ✓
    pct_t1     = round(delta_eve / max(tier1, 1) * 100, 1)
    nii_chg    = _nii_impact(rp_gap_1y, +200)
    breach     = sc.get("brsa_breach", abs(pct_t1) >= 15)

    if net_dv01 < 0:  # varlık duyarlı (EVE convention: negatif DV01) → faiz artışı EVE'yi düşürür
        hedge_dv01 = abs(delta_eve) / 200 * 0.5   # yarı koruma
        n5y  = _notional_for_dv01(hedge_dv01, 5,  swap_rate)
        n2y  = _notional_for_dv01(hedge_dv01 * 0.3, 2, swap_rate)
        trades = [
            {"instrument": "IRS", "direction": "Sabit Ödeyici (Pay Fixed)",
             "tenor": "5Y", "notional_m": n5y, "urgency": _urgency(breach, pct_t1),
             "rationale_tr": f"Net DV01 +{net_dv01:.2f}M TL/bps → 200bps paralel şokta EVE {abs(delta_eve):.0f}M TL erimesi tahmini; 5Y IRS ile duration kısaltılmalıdır."},
            {"instrument": "IRS", "direction": "Sabit Ödeyici (Pay Fixed)",
             "tenor": "2Y", "notional_m": n2y, "urgency": "İHTİYATİ",
             "rationale_tr": f"Kısa vade IRS ile repricing gap ({rp_gap_1y:+.0f}M TL) kaynaklı NII etkisi ({nii_chg:+.0f}M TL) sınırlanabilir."},
        ]
        summary = (f"Bilanço faiz artışına duyarlı (DV01 +{net_dv01:.2f}M TL/bps); "
                   f"200bps yükselişte EVE {abs(delta_eve):.0f}M TL ({abs(pct_t1):.1f}% Tier1) erimesi bekleniyor — "
                   f"{'BRSA ihlali söz konusu; acil aksiyon gerekli.' if breach else '5Y sabit ödeyici IRS ile açık kapatılmalıdır.'}")
    else:
        trades = [
            {"instrument": "IRS", "direction": "Sabit Alıcı (Receive Fixed)",
             "tenor": "5Y", "notional_m": _notional_for_dv01(abs(net_dv01)*0.3, 5, swap_rate),
             "urgency": "İHTİYATİ",
             "rationale_tr": "Bilanço faiz artışından fayda sağlıyor; mevcut pozisyonun bir kısmı koruma altına alınabilir."},
        ]
        summary = (f"Bilanço yükümlülük duyarlı (DV01 {net_dv01:.2f}M TL/bps); "
                   f"paralel yükseliş EVE'ye olumlu etki ({abs(delta_eve):.0f}M TL artış tahmini); "
                   f"pozisyonun bir kısmını kilitlemek için alıcı IRS değerlendirilebilir.")

    results["parallel_up_200"] = {
        "label": "Paralel Yükseliş +200bps", "label_tr": "Paralel Şok — Faiz +200bps",
        "eve_impact_m": round(delta_eve, 1), "nii_impact_m": nii_chg,
        "eve_pct_tier1": pct_t1, "brsa_breach": breach,
        "trades": trades, "summary_tr": summary,
    }

    # ────────────────────────────────────────────────────────────────────────
    # SENARYO 2 — Paralel Düşüş (-200bps)
    # ────────────────────────────────────────────────────────────────────────
    sc = eve_sc.get("parallel_down_200", {})
    delta_eve = sc.get("delta_eve", -net_dv01 * 200)  # -200bp shock: net_dv01<0 → delta_eve>0 (asset-sensitive gains) ✓
    pct_t1    = round(delta_eve / max(tier1, 1) * 100, 1)
    nii_chg   = _nii_impact(rp_gap_1y, -200)
    breach    = sc.get("brsa_breach", abs(pct_t1) >= 15)

    if net_dv01 > 0:  # yükümlülük duyarlı (EVE convention: pozitif DV01) → faiz düşüşü EVE'yi düşürür
        n5y = _notional_for_dv01(abs(delta_eve) / 200 * 0.5, 5, max(swap_rate - 0.02, 0.01))
        trades = [
            {"instrument": "IRS", "direction": "Sabit Alıcı (Receive Fixed)",
             "tenor": "5Y", "notional_m": n5y, "urgency": _urgency(breach, pct_t1),
             "rationale_tr": f"Net DV01 {net_dv01:.2f}M TL/bps; faiz düşüşünde EVE {abs(delta_eve):.0f}M TL azalıyor — 5Y alıcı IRS ile uzun vade kilitlenmeli."},
            {"instrument": "Hazine Tahvili", "direction": "Alım (long duration)",
             "tenor": "10Y", "notional_m": round(abs(delta_eve) * 0.3, 0),
             "urgency": "İHTİYATİ",
             "rationale_tr": "Uzun vadeli tahvil alımı faiz düşüşünde EVE kaybını hedge eder ve portföy duration'ını artırır."},
        ]
        summary = (f"Bilanço yükümlülük duyarlı; 200bps faiz düşüşünde EVE {abs(delta_eve):.0f}M TL "
                   f"({abs(pct_t1):.1f}% Tier1) erimesi bekleniyor — "
                   f"{'BRSA limitine yakın; acil alıcı IRS yapılmalıdır.' if breach else 'alıcı IRS ile uzun vade faiz oranı kilitlenmeli.'}")
    else:
        trades = [
            {"instrument": "FRA", "direction": "Satış (short)",
             "tenor": "6M×12M", "notional_m": round(abs(rp_gap_1y) * 0.3, 0),
             "urgency": "İHTİYATİ",
             "rationale_tr": "Faiz düşüşü ortamında yeniden fiyatlama geliri azalır; FRA ile NII koruma sağlanabilir."},
        ]
        summary = (f"Bilanço faiz düşüşünden EVE bazında fayda sağlıyor (+{abs(delta_eve):.0f}M TL tahmini); "
                   f"ancak repricing gap ({rp_gap_1y:+.0f}M TL) NII gelirini olumsuz etkileyebilir — FRA değerlendirilebilir.")

    results["parallel_down_200"] = {
        "label": "Paralel Düşüş -200bps", "label_tr": "Paralel Şok — Faiz -200bps",
        "eve_impact_m": round(delta_eve, 1), "nii_impact_m": nii_chg,
        "eve_pct_tier1": pct_t1, "brsa_breach": breach,
        "trades": trades, "summary_tr": summary,
    }

    # ────────────────────────────────────────────────────────────────────────
    # SENARYO 3 — Bear Steepening (kısa +300bps, uzun +100bps)
    # ────────────────────────────────────────────────────────────────────────
    sc = eve_sc.get("bear_steepen", {})
    # Short-end dominant shock → repricing liabilities reprice up fast
    delta_eve_est = net_dv01 * 150   # +150bps weighted shock: net_dv01<0 → delta_eve<0 for asset-sensitive ✓
    delta_eve = sc.get("delta_eve", delta_eve_est)
    pct_t1    = round(delta_eve / max(tier1, 1) * 100, 1)
    nii_chg   = _nii_impact(rp_gap_1y, +300)
    breach    = sc.get("brsa_breach", abs(pct_t1) >= 15)

    # Bear steepening: short funding costs jump → pay fixed on short IRS, receive long
    n2y_pay  = _notional_for_dv01(abs(rp_gap_1y) * 0.0001 * 3, 2, swap_rate)
    n10y_rec = _notional_for_dv01(abs(net_dv01) * 0.4, 10, swap_rate * 0.7)
    trades = [
        {"instrument": "IRS", "direction": "Sabit Ödeyici (Pay Fixed) — kısa vade",
         "tenor": "2Y", "notional_m": n2y_pay, "urgency": _urgency(breach, pct_t1),
         "rationale_tr": f"Kısa vade faizlerdeki sert yükseliş ({'+300bps'}) fonlama maliyetini artırıyor; 2Y sabit öde ile mevduat repricing riski sınırlanmalı."},
        {"instrument": "IRS", "direction": "Sabit Alıcı (Receive Fixed) — uzun vade",
         "tenor": "10Y", "notional_m": n10y_rec, "urgency": "İHTİYATİ",
         "rationale_tr": "Uzun vade faiz görece sabit kalırken kısa vade yükselirse eğri dikleşir; 10Y alıcı IRS curve carry'den faydalanır."},
        {"instrument": "FRA", "direction": "Satış (short)",
         "tenor": "3M×6M", "notional_m": round(abs(rp_gap_1y) * 0.2, 0),
         "urgency": "ÖNERİLİR",
         "rationale_tr": "Kısa vadeli fonlama maliyeti yükseliyor; FRA ile ara dönem faiz sabitlenerek NII korunabilir."},
    ]
    summary = (f"Bear steepening: kısa vade +300bps, uzun vade +100bps — "
               f"fonlama maliyeti sert yükseliyor, repricing gap {rp_gap_1y:+.0f}M TL üzerinden "
               f"NII {nii_chg:+.0f}M TL etkileniyor — kısa sabit ödeyici IRS ile maliyet kilitlenmeli.")

    results["bear_steepen"] = {
        "label": "Bear Steepening", "label_tr": "Bear Dikleşme (Kısa↑ > Uzun↑)",
        "eve_impact_m": round(delta_eve, 1), "nii_impact_m": nii_chg,
        "eve_pct_tier1": pct_t1, "brsa_breach": breach,
        "trades": trades, "summary_tr": summary,
    }

    # ────────────────────────────────────────────────────────────────────────
    # SENARYO 4 — Bull Flattening (kısa sabit, uzun -150bps)
    # ────────────────────────────────────────────────────────────────────────
    sc = eve_sc.get("bull_flatten", {})
    # Long-end rallies → asset PV rises if long-duration assets
    delta_eve_est = -net_dv01 * 100  # -100bp long-end shock: net_dv01<0 → delta_eve>0 (asset-sensitive gains) ✓
    delta_eve = sc.get("delta_eve", delta_eve_est)
    pct_t1    = round(delta_eve / max(tier1, 1) * 100, 1)
    nii_chg   = _nii_impact(rp_gap_1y, 0)  # short rates flat, NII ~ unchanged
    breach    = sc.get("brsa_breach", abs(pct_t1) >= 15)

    n10y = _notional_for_dv01(abs(net_dv01) * 0.5, 10, swap_rate * 0.7)
    trades = [
        {"instrument": "IRS", "direction": "Sabit Alıcı (Receive Fixed) — uzun vade",
         "tenor": "10Y", "notional_m": n10y, "urgency": "ÖNERİLİR",
         "rationale_tr": f"Uzun vade faizler düşüyor; alıcı 10Y IRS ile yüksek sabit oran kilitlenmeli ve uzun vade varlık duration'ı korunmalı."},
        {"instrument": "Hazine Tahvili", "direction": "Alım — uzun vade",
         "tenor": "10Y", "notional_m": round(total_assets * 0.005, 0),
         "urgency": "İHTİYATİ",
         "rationale_tr": "Bull flatten ortamında uzun vade tahvil fiyatı yükselir; portföy değer kazancı için uzun vade tahvil biriktirilmesi düşünülebilir."},
    ]
    summary = (f"Bull flattening: kısa vade sabit, uzun vade -150bps düşüş — "
               f"{'EVE artışı bekleniyor (+' if delta_eve > 0 else 'EVE etkisi sınırlı ('}{abs(delta_eve):.0f}M TL); "
               f"uzun vade faizleri kilit için 10Y alıcı IRS değerlendirilmeli.")

    results["bull_flatten"] = {
        "label": "Bull Flattening", "label_tr": "Bull Düzleşme (Kısa = , Uzun↓)",
        "eve_impact_m": round(delta_eve, 1), "nii_impact_m": nii_chg,
        "eve_pct_tier1": pct_t1, "brsa_breach": breach,
        "trades": trades, "summary_tr": summary,
    }

    # ────────────────────────────────────────────────────────────────────────
    # SENARYO 5 — Bear Flattening (kısa +200bps, uzun +100bps — BRSA IRRBB)
    # ────────────────────────────────────────────────────────────────────────
    sc = eve_sc.get("bear_flatten", {})
    delta_eve_est = net_dv01 * 150   # +150bps weighted shock: net_dv01<0 → delta_eve<0 ✓
    delta_eve = sc.get("delta_eve", delta_eve_est)
    pct_t1    = round(delta_eve / max(tier1, 1) * 100, 1)
    nii_chg   = _nii_impact(rp_gap_1y, +200)
    breach    = sc.get("brsa_breach", abs(pct_t1) >= 15)

    n3y = _notional_for_dv01(abs(net_dv01) * 0.6, 3, swap_rate)
    n5y = _notional_for_dv01(abs(net_dv01) * 0.3, 5, swap_rate)
    trades = [
        {"instrument": "IRS", "direction": "Sabit Ödeyici (Pay Fixed)",
         "tenor": "3Y", "notional_m": n3y, "urgency": _urgency(breach, pct_t1),
         "rationale_tr": f"Bear flatten'da hem kısa hem uzun vade yükseliyor; orta vadeli sabit öde ile duration kısaltılarak EVE kaybı ({abs(delta_eve):.0f}M TL) sınırlanmalı."},
        {"instrument": "IRS", "direction": "Sabit Ödeyici (Pay Fixed)",
         "tenor": "5Y", "notional_m": n5y, "urgency": "ÖNERİLİR",
         "rationale_tr": "Tüm eğri yükseliyor; 5Y sabit öde ile portföy duration'ı daha da kısaltılabilir."},
    ]
    summary = (f"Bear flattening: kısa +200bps, uzun +100bps — eğri tüm noktalardan yükseliyor; "
               f"EVE {abs(delta_eve):.0f}M TL ({abs(pct_t1):.1f}% Tier1) erime riski — "
               f"{'BRSA sınırı aşılıyor; acil DV01 azaltma gerekli.' if breach else 'orta vade sabit ödeyici IRS ile açık kapatılmalıdır.'}")

    results["bear_flatten"] = {
        "label": "Bear Flattening", "label_tr": "Bear Düzleşme (Kısa↑↑ , Uzun↑)",
        "eve_impact_m": round(delta_eve, 1), "nii_impact_m": nii_chg,
        "eve_pct_tier1": pct_t1, "brsa_breach": breach,
        "trades": trades, "summary_tr": summary,
    }

    # ────────────────────────────────────────────────────────────────────────
    # SENARYO 6 — Bull Steepening (kısa -200bps, uzun sabit/hafif yükseliş)
    # ────────────────────────────────────────────────────────────────────────
    sc = eve_sc.get("bull_steepen", {})
    delta_eve_est = -net_dv01 * 100  # -100bp short-end dominant: net_dv01<0 → delta_eve>0 ✓
    delta_eve = sc.get("delta_eve", delta_eve_est)
    pct_t1    = round(delta_eve / max(tier1, 1) * 100, 1)
    nii_chg   = _nii_impact(rp_gap_1y, -200)
    breach    = sc.get("brsa_breach", abs(pct_t1) >= 15)

    trades = [
        {"instrument": "FRA", "direction": "Alım (long)",
         "tenor": "6M×12M", "notional_m": round(abs(rp_gap_1y) * 0.25, 0),
         "urgency": "ÖNERİLİR",
         "rationale_tr": f"Kısa vade faizler düşüyor; mevduat yenileme maliyeti azalacak ancak repricing geliri de düşecek — FRA ile geçiş dönemi NII korunabilir."},
        {"instrument": "Hazine Tahvili", "direction": "Alım — kısa vade",
         "tenor": "2Y", "notional_m": round(total_assets * 0.003, 0),
         "urgency": "İHTİYATİ",
         "rationale_tr": "Kısa vade faiz düşüşünde kısa tahvil fiyatı yükselir; likidite yönetimi açısından kısa tahvil alımı değerli olabilir."},
    ]
    summary = (f"Bull steepening: kısa vade -200bps düşüş, uzun vade sabit — "
               f"fonlama maliyeti düşüyor ancak repricing gap ({rp_gap_1y:+.0f}M TL) üzerinden "
               f"NII {nii_chg:+.0f}M TL etkileniyor; FRA ile geçiş dönemi NII korunabilir.")

    results["bull_steepen"] = {
        "label": "Bull Steepening", "label_tr": "Bull Dikleşme (Kısa↓↓ , Uzun=)",
        "eve_impact_m": round(delta_eve, 1), "nii_impact_m": nii_chg,
        "eve_pct_tier1": pct_t1, "brsa_breach": breach,
        "trades": trades, "summary_tr": summary,
    }

    # ────────────────────────────────────────────────────────────────────────
    # YAPI ÖNERİLERİ — LCR / NSFR
    # ────────────────────────────────────────────────────────────────────────
    structural = []

    if lcr < 100:
        gap_m = round(total_assets * 0.01, 0)   # ~1% varlık büyüklüğü
        structural.append({
            "metric": "LCR", "value": round(lcr, 1), "limit": 100,
            "urgency": "ZORUNLU",
            "action": "HQLA Alımı — Kısa Vadeli DİBS / T-Bill",
            "notional_m": gap_m,
            "summary_tr": (f"LCR {lcr:.1f}% ile yasal minimum %100'ün altında; "
                           f"acil olarak ~{gap_m:.0f}M TL kısa vadeli DİBS alımı ile likit varlık tamponu güçlendirilmeli."),
        })
    elif lcr < 115:
        structural.append({
            "metric": "LCR", "value": round(lcr, 1), "limit": 115,
            "urgency": "ÖNERİLİR",
            "action": "HQLA Tamponu Güçlendirme",
            "notional_m": round(total_assets * 0.005, 0),
            "summary_tr": (f"LCR {lcr:.1f}% ile %115 tampon eşiğinin altında; "
                           f"stres dönemlerine hazırlık için likit varlık portföyü artırılmalıdır."),
        })

    if nsfr < 100:
        structural.append({
            "metric": "NSFR", "value": round(nsfr, 1), "limit": 100,
            "urgency": "ZORUNLU",
            "action": "Uzun Vadeli Borçlanma — Bono / Sendikasyon",
            "notional_m": round(total_assets * 0.02, 0),
            "summary_tr": (f"NSFR {nsfr:.1f}% ile %100 yasal minimumun altında; "
                           f"uzun vadeli bono ihracı veya sendikasyon kredisi ile stabil fonlama tabanı artırılmalıdır."),
        })
    elif nsfr < 110:
        structural.append({
            "metric": "NSFR", "value": round(nsfr, 1), "limit": 110,
            "urgency": "ÖNERİLİR",
            "action": "Fonlama Vade Uzatma",
            "notional_m": round(total_assets * 0.01, 0),
            "summary_tr": (f"NSFR {nsfr:.1f}% ile %110 buffer eşiğinin altında; "
                           f"kısa vadeli mevduatların bir kısmı uzun vadeli ürünlere dönüştürülerek stabil fonlama artırılabilir."),
        })

    # ────────────────────────────────────────────────────────────────────────
    # Özet bilanço durumu
    # ────────────────────────────────────────────────────────────────────────
    orientation = "varlık duyarlı" if net_dv01 < 0 else "yükümlülük duyarlı"
    brsa_breaches = [k for k, v in results.items() if v.get("brsa_breach")]

    meta = {
        "orientation":        orientation,
        "net_dv01":           net_dv01,
        "duration_gap":       round(dur_gap, 3),
        "repricing_gap_1y":   round(rp_gap_1y, 1),
        "base_eve":           round(base_eve, 1),
        "tier1_capital":      round(tier1, 1),
        "lcr":                round(lcr, 1),
        "nsfr":               round(nsfr, 1),
        "total_assets":       round(total_assets, 1),
        "brsa_breach_scenarios": brsa_breaches,
        "overall_risk": (
            "YÜKSEK — Acil Aksiyon" if brsa_breaches else
            "ORTA — Yakın Takip" if any(abs(v["eve_pct_tier1"]) > 8 for v in results.values()) else
            "DÜŞÜK — Rutin İzleme"
        ),
    }

    return {
        "scenarios":   results,
        "structural":  structural,
        "meta":        meta,
    }

"""
Bek Rate Desk — Sprint 3 Engine
=================================
A. Morning Dashboard   — tüm modülleri birleştiren tek ekran risk özeti
B. Hedge Recommender   — duration gap, EVE ve NII bazlı IRS/swap öneri motoru
C. ALCO Memo Generator — yönetim özeti üretici
D. Scenario Library    — kayıtlı bilanço senaryoları (CRUD)

ALM bağlamı: Türk bankacılığı, TCMB politika faizi, BRSA limitleri.
"""

import json, os, math
from datetime import datetime, date
from typing import Optional
from utils.io_utils import _atomic_json_write

DATA_DIR      = os.path.join(os.path.dirname(os.path.dirname(__file__)), "data")
SCENARIOS_FILE = os.path.join(DATA_DIR, "scenarios.json")
os.makedirs(DATA_DIR, exist_ok=True)


# ══════════════════════════════════════════════════════════════════════════════
# A. MORNING DASHBOARD — CHIEF RISK BRIEF
# ══════════════════════════════════════════════════════════════════════════════

def build_morning_brief(data: dict) -> dict:
    """
    Tüm ALM metriklerini alır, önceliklendirir, aksiyon önerir.

    Input: {
      nii_base, nii_worst_delta, nii_worst_scenario,
      eve_delta_worst, tier1_capital,
      duration_gap,
      lcr, nsfr,
      fx_nop_tl, capital_tl,
      base_rate,                 # TCMB politika faizi
      us_10y, us_2y,             # US Treasury (opsiyonel)
      usdtry,                    # FX kur
      repricing_gap,             # dominant bucket gap (M)
      worst_bucket,              # "3M", "6M" vb.
      total_assets, total_liabs,
    }

    Output: {
      risk_level,            # "LOW" | "MODERATE" | "ELEVATED" | "HIGH" | "CRITICAL"
      risk_color,
      top_risks: [...],      # öncelikli 3 risk
      worst_metric,          # tek cümlelik en kritik metrik
      worst_scenario,        # hangi şok en çok zarar veriyor
      open_buckets: [...],   # negatif gap olan bucketlar
      kpis: {...},           # tüm KPI'lar renk kodlu
      actions: [...],        # önerilen aksiyonlar (öncelikli)
      alco_headline,         # tek cümlelik ALCO özeti
      generated_at,
    }
    """
    m = data  # alias

    nii_base        = float(m.get("nii_base",          0))
    nii_delta       = float(m.get("nii_worst_delta",   0))
    nii_scenario    = m.get("nii_worst_scenario",      "N/A")
    eve_delta       = float(m.get("eve_delta_worst",   0))
    tier1           = float(m.get("tier1_capital",     1))
    dur_gap         = float(m.get("duration_gap",      0))
    lcr             = m.get("lcr")
    nsfr            = m.get("nsfr")
    fx_nop          = float(m.get("fx_nop_tl",         0))
    capital         = float(m.get("capital_tl",        1))
    from utils.tcmb_evds import get_policy_rate as _get_pr
    base_rate       = float(m.get("base_rate") or _get_pr())
    repricing_gap   = float(m.get("repricing_gap",     0))
    worst_bucket    = m.get("worst_bucket",            "N/A")
    total_assets    = float(m.get("total_assets",      0))
    us_10y          = m.get("us_10y")
    usdtry          = m.get("usdtry")

    # ── Metrik skorlama (0-100, 100=en riskli) ────────────────────────────────
    scores = {}

    # NII risk
    nii_pct = abs(nii_delta) / abs(nii_base) * 100 if nii_base else 0
    scores["nii"] = min(nii_pct / 20 * 100, 100)

    # EVE / IRRBB risk
    eve_pct = abs(eve_delta) / tier1 * 100 if tier1 else 0
    scores["eve"] = min(eve_pct / 15 * 100, 100)   # Basel limit 15%

    # Duration gap risk
    scores["duration"] = min(abs(dur_gap) / 5 * 100, 100)

    # Liquidity risk
    if lcr is not None:
        scores["lcr"] = max(0, min((150 - lcr) / 50 * 100, 100))
    else:
        scores["lcr"] = 50

    # FX risk
    fx_pct = abs(fx_nop) / capital * 100 if capital else 0
    scores["fx"] = min(fx_pct / 20 * 100, 100)

    # Overall score
    weights = {"nii": 0.30, "eve": 0.25, "duration": 0.15, "lcr": 0.20, "fx": 0.10}
    overall = sum(scores[k] * weights[k] for k in weights)

    # Risk level
    if   overall < 20: risk_level = "LOW";      risk_color = "#10b981"
    elif overall < 40: risk_level = "MODERATE";  risk_color = "#84cc16"
    elif overall < 60: risk_level = "ELEVATED";  risk_color = "#f59e0b"
    elif overall < 80: risk_level = "HIGH";      risk_color = "#ef4444"
    else:              risk_level = "CRITICAL";  risk_color = "#7f1d1d"

    # ── Top risks (en yüksek 3 skor) ─────────────────────────────────────────
    risk_labels = {
        "nii":      f"NII-at-Risk: {nii_delta:+.1f}M ({nii_pct:.1f}% of base) under {nii_scenario}",
        "eve":      f"ΔEVE: {eve_delta:+.1f}M = {eve_pct:.1f}% of Tier 1 (Basel limit 15%)",
        "duration": f"Duration Gap: {dur_gap:+.2f}Y ({'asset' if dur_gap>0 else 'liability'}-sensitive)",
        "lcr":      f"LCR: {lcr:.1f}%" if lcr else "LCR: N/A",
        "fx":       f"FX NOP: {fx_nop:.0f}M TL = {fx_pct:.1f}% of capital",
    }
    top_risks = sorted(scores.items(), key=lambda x: x[1], reverse=True)[:3]
    top_risks = [{"metric": k, "score": round(s, 1), "label": risk_labels[k]}
                 for k, s in top_risks]

    # ── Worst metrik (tek cümle) ──────────────────────────────────────────────
    top_key = top_risks[0]["metric"]
    worst_metric = top_risks[0]["label"]

    # ── Open buckets ─────────────────────────────────────────────────────────
    open_buckets = []
    repricing_data = m.get("repricing_gap_table", [])
    for row in repricing_data:
        if row.get("gap", 0) < -50:
            open_buckets.append({
                "bucket": row["bucket"],
                "gap_m":  round(row["gap"], 0),
                "rsa":    round(row.get("rsa", 0), 0),
                "rsl":    round(row.get("rsl", 0), 0),
            })

    # ── KPI kartları ─────────────────────────────────────────────────────────
    def kpi_color(score):
        if score < 25: return "#10b981"
        if score < 50: return "#84cc16"
        if score < 75: return "#f59e0b"
        return "#ef4444"

    kpis = {
        "nii_at_risk":   {"value": f"{nii_delta:+.1f}M",  "sub": f"{nii_pct:.1f}% of base",  "color": kpi_color(scores["nii"]),      "label": "NII-at-Risk"},
        "eve_tier1":     {"value": f"{eve_pct:.1f}%",      "sub": f"ΔEVE {eve_delta:+.0f}M",  "color": kpi_color(scores["eve"]),      "label": "EVE / Tier 1"},
        "duration_gap":  {"value": f"{dur_gap:+.2f}Y",     "sub": "asset" if dur_gap>0 else "liability", "color": kpi_color(scores["duration"]),  "label": "Duration Gap"},
        "lcr":           {"value": f"{lcr:.1f}%" if lcr else "N/A", "sub": "min 100%",         "color": kpi_color(scores["lcr"]),      "label": "LCR"},
        "nsfr":          {"value": f"{nsfr:.1f}%" if nsfr else "N/A", "sub": "min 100%",       "color": "#10b981" if (nsfr or 0)>110 else "#f59e0b", "label": "NSFR"},
        "fx_nop":        {"value": f"{fx_pct:.1f}%",       "sub": f"{fx_nop:.0f}M TL",        "color": kpi_color(scores["fx"]),       "label": "FX NOP / Capital"},
        "base_rate":     {"value": f"{base_rate:.2f}%",    "sub": "TCMB politika",             "color": "#f59e0b",                     "label": "Policy Rate"},
        "usdtry":        {"value": str(usdtry) if usdtry else "N/A", "sub": "spot",            "color": "#64748b",                     "label": "USD/TRY"},
        "us_10y":        {"value": f"{us_10y:.3f}%" if us_10y else "N/A", "sub": "US Treasury","color": "#10b981",                     "label": "US 10Y"},
    }

    # ── Aksiyon önerileri ─────────────────────────────────────────────────────
    actions = _generate_actions(
        nii_pct=nii_pct, nii_scenario=nii_scenario,
        eve_pct=eve_pct, dur_gap=dur_gap,
        lcr=lcr, fx_pct=fx_pct,
        repricing_gap=repricing_gap, worst_bucket=worst_bucket,
        base_rate=base_rate,
    )

    # ── ALCO headline ─────────────────────────────────────────────────────────
    alco_headline = _alco_headline(
        risk_level=risk_level, dur_gap=dur_gap,
        nii_scenario=nii_scenario, nii_pct=nii_pct,
        eve_pct=eve_pct, lcr=lcr,
    )

    return {
        "risk_level":    risk_level,
        "risk_color":    risk_color,
        "overall_score": round(overall, 1),
        "top_risks":     top_risks,
        "worst_metric":  worst_metric,
        "worst_scenario":nii_scenario,
        "open_buckets":  open_buckets,
        "kpis":          kpis,
        "actions":       actions,
        "alco_headline": alco_headline,
        "scores":        {k: round(v, 1) for k, v in scores.items()},
        "generated_at":  datetime.now().strftime("%Y-%m-%d %H:%M"),
    }


def _generate_actions(nii_pct, nii_scenario, eve_pct, dur_gap,
                       lcr, fx_pct, repricing_gap, worst_bucket, base_rate):
    actions = []

    # NII aksiyonları
    if nii_pct > 10:
        if dur_gap < 0:  # liability-sensitive
            actions.append({
                "priority": "HIGH",
                "category": "Rate Risk",
                "action":   "Receive-fixed IRS ekle — liability sensitivity azalt",
                "detail":   f"Worst-case NII kaybı %{nii_pct:.1f}. {nii_scenario} senaryosunda kritik.",
                "color":    "#ef4444",
            })
        else:
            actions.append({
                "priority": "HIGH",
                "category": "Rate Risk",
                "action":   "Pay-fixed IRS değerlendir — asset sensitivity düzelt",
                "detail":   f"NII-at-Risk %{nii_pct:.1f}. Rate düşüşüne karşı koruma gerekli.",
                "color":    "#ef4444",
            })
    elif nii_pct > 5:
        actions.append({
            "priority": "MODERATE",
            "category": "Rate Risk",
            "action":   "Repricing gap izle — 3M vadeli mevduat/kredi dengesini gözden geçir",
            "detail":   f"NII hassasiyeti %{nii_pct:.1f}. Yakın takip yeterli.",
            "color":    "#f59e0b",
        })

    # EVE / IRRBB
    if eve_pct > 12:
        actions.append({
            "priority": "CRITICAL",
            "category": "IRRBB / EVE",
            "action":   "ACİL: Basel III limiti yakın — duration gap kapatılmalı",
            "detail":   f"ΔEVE/Tier1 = %{eve_pct:.1f} (limit %15). BRSA'ya raporlama gerekebilir.",
            "color":    "#7f1d1d",
        })
    elif eve_pct > 8:
        actions.append({
            "priority": "HIGH",
            "category": "IRRBB / EVE",
            "action":   "Duration gap kıs — IRS veya varlık/yükümlülük vade yapısı değiştir",
            "detail":   f"ΔEVE/Tier1 = %{eve_pct:.1f}. Sınıra %{15-eve_pct:.1f}% marj kaldı.",
            "color":    "#ef4444",
        })

    # Likidite
    if lcr is not None and lcr < 110:
        actions.append({
            "priority": "HIGH",
            "category": "Liquidity",
            "action":   "HQLA güçlendir — repo veya kısa TCMB işlemi değerlendir",
            "detail":   f"LCR %{lcr:.1f} iç buffer'ın altında. Kısa vadeli çıkış baskısına dikkat.",
            "color":    "#ef4444",
        })

    # FX
    if fx_pct > 15:
        actions.append({
            "priority": "HIGH",
            "category": "FX Risk",
            "action":   "FX NOP kapat — spot satış veya FX swap ile pozisyon düzelt",
            "detail":   f"FX NOP sermayenin %{fx_pct:.1f}'i. BRSA limiti genellikle %20.",
            "color":    "#ef4444",
        })

    # Repricing bucket
    if repricing_gap < -500:
        actions.append({
            "priority": "MODERATE",
            "category": "Repricing",
            "action":   f"'{worst_bucket}' bucket negatif gap — bu vadeye varlık çek veya borçlanma uzat",
            "detail":   f"Repricing açığı {repricing_gap:.0f}M. Orta vade LCR/NII baskısı oluşabilir.",
            "color":    "#f59e0b",
        })

    if not actions:
        actions.append({
            "priority": "LOW",
            "category": "General",
            "action":   "Tüm metrikler sınırlar dahilinde — rutin izleme sürdür",
            "detail":   "Büyük aksiyon gerekmiyor. Haftalık ALCO özeti hazırla.",
            "color":    "#10b981",
        })

    return sorted(actions, key=lambda x: ["CRITICAL","HIGH","MODERATE","LOW"].index(x["priority"]))


def _alco_headline(risk_level, dur_gap, nii_scenario, nii_pct, eve_pct, lcr):
    sensitivity = "liability-sensitive" if dur_gap < 0 else "asset-sensitive"
    parts = [f"Balance sheet remains {sensitivity}"]
    if nii_pct > 5:
        parts.append(f"worst-case ΔNII {nii_pct:.1f}% under {nii_scenario}")
    if eve_pct > 8:
        parts.append(f"ΔEVE at {eve_pct:.1f}% of Tier 1 — approaching IRRBB limit")
    if lcr and lcr < 120:
        parts.append(f"LCR at {lcr:.1f}% requires monitoring")
    headline = ". ".join(parts) + "."
    if risk_level in ("HIGH", "CRITICAL"):
        headline += " Immediate ALCO review recommended."
    return headline


# ══════════════════════════════════════════════════════════════════════════════
# B. HEDGE RECOMMENDATION ENGINE
# ══════════════════════════════════════════════════════════════════════════════

def run_hedge_engine(data: dict) -> dict:
    """
    Duration gap, EVE ve NII'ye göre IRS/swap hedge öneri üretir.

    Input: {
      duration_gap       : float  (years, +asset-sensitive, -liability-sensitive)
      total_assets       : float  (M)
      total_liabs        : float  (M)
      tier1_capital      : float  (M)
      eve_delta_worst    : float  (M, negatif = kayıp)
      eve_limit_pct      : float  (%, default 15)
      nii_base           : float  (M)
      nii_worst_delta    : float  (M)
      base_rate          : float  (% TCMB)
      target_duration_gap: float  (hedef gap, default 0)
      hedge_tenors       : list   ([1, 2, 3, 5, 10] yıl)
    }
    """
    dur_gap        = float(data.get("duration_gap",        0))
    total_assets   = float(data.get("total_assets",     5000))
    total_liabs    = float(data.get("total_liabs",      4500))
    tier1          = float(data.get("tier1_capital",    1500))
    eve_worst      = float(data.get("eve_delta_worst",     0))
    eve_limit_pct  = float(data.get("eve_limit_pct",      15))
    nii_base       = float(data.get("nii_base",           620))
    nii_worst      = float(data.get("nii_worst_delta",      0))
    from utils.tcmb_evds import get_policy_rate as _get_pr
    base_rate      = float(data.get("base_rate") or _get_pr())
    target_gap     = float(data.get("target_duration_gap",  0))
    tenors         = data.get("hedge_tenors", [1, 2, 3, 5, 10])

    # Mevcut EVE %
    eve_pct = abs(eve_worst) / tier1 * 100 if tier1 else 0

    # ── 1. Duration Gap Kapama ─────────────────────────────────────────────────
    # Gerekli DV01 değişimi: gap kapatmak için ne kadar notional IRS lazım?
    # Modified Duration of Assets ≈ abs(dur_gap) + MD_liabilities
    # Basit yaklaşım: ΔDUR = target_gap - dur_gap
    # Notional = (ΔDUR × Portfolio_Market_Value) / (MD_swap)
    # MD_swap ≈ tenor × 0.85 (basitleştirilmiş)

    gap_to_close  = dur_gap - target_gap          # kapatılacak gap (+ veya -)
    portfolio_mv  = (total_assets + total_liabs) / 2

    hedge_direction = "receive-fixed" if dur_gap < 0 else "pay-fixed"
    # Liability-sensitive → receive-fixed IRS (uzun sabit faiz al, değer artar rates düşünce)
    # Asset-sensitive → pay-fixed IRS (sabit faiz öde, kısa vade al)

    tenor_hedges = []
    for tenor_yr in tenors:
        md_swap = tenor_yr * 0.88        # IRS modifiye duration yaklaşımı
        notional = abs(gap_to_close) * portfolio_mv / md_swap if md_swap else 0

        # Bu tenor ile kaç bps NII iyileştirmesi?
        # Receive-fixed IRS → floating nakit girişi = base_rate + spread
        # Kaba etki: notional × (base_rate - fixed_swap_rate) × time_fraction
        swap_rate_approx = base_rate - (tenor_yr * 0.15)  # uzun vade → daha düşük sabit oran
        nim_impact_m     = notional * (base_rate - swap_rate_approx) / 100 / 12  # aylık

        # EVE iyileştirmesi: IRS uzun vade ekler → duration gap kapanır
        eve_improvement_pct = abs(gap_to_close) / (tenor_yr * 0.88) * 100 * 0.5  # yaklaşık

        tenor_hedges.append({
            "tenor_yr":          tenor_yr,
            "notional_m":        round(notional, 1),
            "hedge_direction":   hedge_direction,
            "swap_rate_approx":  round(swap_rate_approx, 2),
            "monthly_nim_m":     round(nim_impact_m, 3),
            "annual_nim_m":      round(nim_impact_m * 12, 2),
            "eve_improvement_pct": round(min(eve_improvement_pct, eve_pct), 2),
            "residual_gap_yr":   round(target_gap, 2),
            "dv01_m":            round(notional * tenor_yr * 0.88 / 10000, 4),
        })

    # ── 2. Optimal Tenor Seçimi ────────────────────────────────────────────────
    # En küçük notional ile en fazla EVE iyileştirmesini sağlayan
    optimal = None
    best_efficiency = -1
    for h in tenor_hedges:
        if h["notional_m"] > 0:
            eff = h["eve_improvement_pct"] / h["notional_m"]
            if eff > best_efficiency:
                best_efficiency = eff
                optimal = h

    # ── 3. NII Koruma Analizi ──────────────────────────────────────────────────
    # Hangi hedge NII'yi en iyi korur?
    nii_pct = abs(nii_worst) / abs(nii_base) * 100 if nii_base else 0

    nii_hedges = []
    for tenor_yr in [1, 2, 3, 5]:
        # Kısa vadeli receive-fixed IRS → kısa vade libilite maliyetini sabitle
        coverage = min(100, (1 / tenor_yr) * nii_pct * 15)
        nii_hedges.append({
            "tenor_yr":       tenor_yr,
            "nii_coverage_pct": round(coverage, 1),
            "label":          f"{tenor_yr}Y receive-fixed IRS",
            "comment":        ("En iyi kısa vade koruma" if tenor_yr <= 2
                               else "Orta vade stabilizasyon"),
        })
    best_nii_hedge = max(nii_hedges, key=lambda x: x["nii_coverage_pct"])

    # ── 4. EVE Breach Fix ─────────────────────────────────────────────────────
    eve_breach_fix = None
    if eve_pct > 10:
        # Hedef: EVE'yi limit'in %80'ine indir
        target_eve_pct   = eve_limit_pct * 0.80
        reduction_needed = eve_pct - target_eve_pct
        # Gerekli notional (yaklaşık)
        notional_needed  = reduction_needed / 100 * tier1 / 0.003  # DV01 yaklaşımı
        eve_breach_fix = {
            "current_eve_pct":  round(eve_pct, 2),
            "target_eve_pct":   round(target_eve_pct, 2),
            "reduction_pct":    round(reduction_needed, 2),
            "recommended_notional_m": round(notional_needed, 0),
            "recommended_tenor": 5,
            "action": f"5Y receive-fixed IRS, ~{notional_needed:.0f}M notional — "
                      f"EVE'yi %{eve_pct:.1f}'den %{target_eve_pct:.1f}'e indirir",
        }

    return {
        "duration_gap":      round(dur_gap, 3),
        "target_gap":        round(target_gap, 3),
        "gap_to_close":      round(gap_to_close, 3),
        "hedge_direction":   hedge_direction,
        "portfolio_mv_m":    round(portfolio_mv, 0),
        "tenor_hedges":      tenor_hedges,
        "optimal_tenor":     optimal,
        "nii_hedges":        nii_hedges,
        "best_nii_hedge":    best_nii_hedge,
        "eve_pct":           round(eve_pct, 2),
        "eve_breach_fix":    eve_breach_fix,
        "nii_pct":           round(nii_pct, 2),
        "base_rate":         base_rate,
        "computed_at":       datetime.now().strftime("%Y-%m-%d %H:%M"),
    }


# ══════════════════════════════════════════════════════════════════════════════
# C. ALCO MEMO GENERATOR
# ══════════════════════════════════════════════════════════════════════════════

def generate_alco_memo(data: dict) -> dict:
    """
    Tek tuşla ALCO yönetim özeti üretir.
    Hem Türkçe hem İngilizce çıktı.

    Input: build_morning_brief() çıktısı + ek alanlar
    """
    brief        = data.get("brief",        {})
    bank_name    = data.get("bank_name",    "Bank")
    period       = data.get("period",       datetime.now().strftime("%B %Y"))
    preparer     = data.get("preparer",     "Treasury / ALM Desk")
    total_assets = data.get("total_assets", 0)
    base_rate    = data.get("base_rate",    45.0)

    risk_level   = brief.get("risk_level",   "MODERATE")
    kpis         = brief.get("kpis",         {})
    actions      = brief.get("actions",      [])
    top_risks    = brief.get("top_risks",    [])
    open_buckets = brief.get("open_buckets", [])
    headline     = brief.get("alco_headline","")
    scores       = brief.get("scores",       {})

    def kv(key, default="N/A"):
        v = kpis.get(key, {})
        return v.get("value", default)

    # ── Türkçe Memo ──────────────────────────────────────────────────────────
    tr_memo = f"""ALCO TOPLANTI NOTU — {period.upper()}
{"="*60}
Kurum    : {bank_name}
Tarih    : {datetime.now().strftime("%d.%m.%Y %H:%M")}
Hazırlayan: {preparer}
Risk Seviyesi: {risk_level}
{"="*60}

1. GENEL DEĞERLENDİRME
{headline}

2. ANA RİSK METRİKLERİ
   • Faiz Geliri Riski (NII-at-Risk) : {kv('nii_at_risk')}
   • EVE / Tier 1                    : {kv('eve_tier1')}
   • Duration Gap                    : {kv('duration_gap')}
   • LCR                             : {kv('lcr')}
   • NSFR                            : {kv('nsfr')}
   • FX NOP / Sermaye                : {kv('fx_nop')}
   • TCMB Politika Faizi             : {kv('base_rate')}

3. ÖNCELİKLİ RİSKLER
"""
    for i, r in enumerate(top_risks, 1):
        tr_memo += f"   {i}. [{r['metric'].upper()}] {r['label']}\n"

    if open_buckets:
        tr_memo += "\n4. AÇIK REPRICING BUCKETlari\n"
        for b in open_buckets:
            tr_memo += f"   • {b['bucket']}: Gap = {b['gap_m']:.0f}M (RSA={b['rsa']:.0f}M, RSL={b['rsl']:.0f}M)\n"

    tr_memo += f"\n{'5' if open_buckets else '4'}. ÖNERİLEN AKSİYONLAR\n"
    for i, a in enumerate(actions[:4], 1):
        tr_memo += f"   {i}. [{a['priority']}] {a['action']}\n"
        tr_memo += f"      → {a['detail']}\n"

    tr_memo += f"""
6. PİYASA BAĞLAMI
   • TCMB Politika Faizi : {base_rate:.2f}%
   • USD/TRY             : {kv('usdtry')}
   • US 10Y Treasury     : {kv('us_10y')}
   • Toplam Aktif        : {total_assets:.0f}M TL (tahmini)

{"="*60}
NOT: Bu not Bek Rate Desk ALM platformu tarafından otomatik üretilmiştir.
Nihai karar ALCO komitesine aittir.
"""

    # ── İngilizce Memo ────────────────────────────────────────────────────────
    en_memo = f"""ALCO COMMITTEE NOTE — {period.upper()}
{"="*60}
Institution : {bank_name}
Date        : {datetime.now().strftime("%d/%m/%Y %H:%M")}
Prepared by : {preparer}
Risk Status : {risk_level}
{"="*60}

EXECUTIVE SUMMARY
{headline}

KEY RISK METRICS
  NII-at-Risk (worst-case)  : {kv('nii_at_risk')}
  EVE / Tier 1 Capital      : {kv('eve_tier1')}
  Duration Gap              : {kv('duration_gap')}
  LCR                       : {kv('lcr')}
  NSFR                      : {kv('nsfr')}
  FX NOP / Capital          : {kv('fx_nop')}
  TCMB Policy Rate          : {kv('base_rate')}

TOP RISKS
"""
    for i, r in enumerate(top_risks, 1):
        en_memo += f"  {i}. {r['label']}\n"

    en_memo += "\nRECOMMENDED ACTIONS\n"
    for i, a in enumerate(actions[:4], 1):
        en_memo += f"  {i}. [{a['priority']}] {a['action']}\n"
        en_memo += f"     Rationale: {a['detail']}\n"

    en_memo += f"""
MARKET CONTEXT
  TCMB Policy Rate  : {base_rate:.2f}%
  USD/TRY           : {kv('usdtry')}
  US 10Y Treasury   : {kv('us_10y')}
  Total Assets      : {total_assets:.0f}M TL (est.)

{"="*60}
DISCLAIMER: Generated automatically by Bek Rate Desk ALM Platform.
Final decisions rest with the ALCO committee.
"""

    return {
        "tr_memo":     tr_memo,
        "en_memo":     en_memo,
        "risk_level":  risk_level,
        "period":      period,
        "generated_at":datetime.now().strftime("%Y-%m-%d %H:%M"),
        "kpi_snapshot":{k: v.get("value") for k, v in kpis.items()},
    }


# ══════════════════════════════════════════════════════════════════════════════
# D. SCENARIO LIBRARY — CRUD
# ══════════════════════════════════════════════════════════════════════════════

BUILTIN_SCENARIOS = [
    {
        "id":          "base_case",
        "name":        "ALCO Base Case",
        "description": "Standart bilanço yapısı — merkez senaryosu",
        "category":    "Base",
        "builtin":     True,
        "data": {
            "assets": [
                {"name":"Floating Rate Loans",   "amount":2000, "rate":47.5, "repricing_months":1,  "type":"floating"},
                {"name":"Fixed Corporate Loans",  "amount":1200, "rate":44.0, "repricing_months":12, "type":"fixed"},
                {"name":"Gov Bond Portfolio",     "amount":900,  "rate":38.5, "repricing_months":36, "type":"fixed"},
                {"name":"TLREF-linked Loans",     "amount":500,  "rate":46.0, "repricing_months":1,  "type":"floating"},
                {"name":"Mortgages (Fixed)",      "amount":300,  "rate":35.0, "repricing_months":60, "type":"fixed"},
            ],
            "liabilities": [
                {"name":"Demand Deposits",        "amount":1500, "rate":15.0, "repricing_months":1,  "type":"floating"},
                {"name":"Time Deposits (3M)",     "amount":1900, "rate":42.0, "repricing_months":3,  "type":"fixed"},
                {"name":"Time Deposits (6M)",     "amount":600,  "rate":40.0, "repricing_months":6,  "type":"fixed"},
                {"name":"Wholesale Funding",      "amount":400,  "rate":44.0, "repricing_months":12, "type":"fixed"},
                {"name":"Subordinated Debt",      "amount":200,  "rate":50.0, "repricing_months":60, "type":"fixed"},
            ],
            "base_rate": 42.5, "horizon_months": 12, "turkish_context": True,
        }
    },
    {
        "id":          "deposit_stress",
        "name":        "Mevduat Rekabeti Stres",
        "description": "Agresif mevduat rekabeti — mevduat maliyeti +150bps, kısa vadeli yenileme baskısı",
        "category":    "Stress",
        "builtin":     True,
        "data": {
            "assets": [
                {"name":"Floating Rate Loans",   "amount":2000, "rate":47.5, "repricing_months":1,  "type":"floating"},
                {"name":"Fixed Corporate Loans",  "amount":1200, "rate":44.0, "repricing_months":12, "type":"fixed"},
                {"name":"Gov Bond Portfolio",     "amount":900,  "rate":38.5, "repricing_months":36, "type":"fixed"},
                {"name":"TLREF-linked Loans",     "amount":500,  "rate":46.0, "repricing_months":1,  "type":"floating"},
                {"name":"Mortgages (Fixed)",      "amount":300,  "rate":35.0, "repricing_months":60, "type":"fixed"},
            ],
            "liabilities": [
                {"name":"Demand Deposits (outflow)",  "amount":1200, "rate":18.0, "repricing_months":1,  "type":"floating"},
                {"name":"Time Deposits 3M (+150bps)", "amount":2200, "rate":43.5, "repricing_months":3,  "type":"fixed"},
                {"name":"Time Deposits (6M)",         "amount":600,  "rate":41.5, "repricing_months":6,  "type":"fixed"},
                {"name":"Wholesale (rolled)",         "amount":500,  "rate":46.5, "repricing_months":12, "type":"fixed"},
                {"name":"Subordinated Debt",          "amount":200,  "rate":50.0, "repricing_months":60, "type":"fixed"},
            ],
            "base_rate": 42.5, "horizon_months": 12, "turkish_context": True,
        }
    },
    {
        "id":          "bear_flatten",
        "name":        "Bear Flatten Şoku",
        "description": "Kısa faizler +150bps — liability-sensitive bank için worst case",
        "category":    "Stress",
        "builtin":     True,
        "data": {
            "assets": [
                {"name":"Floating Rate Loans",  "amount":2000, "rate":49.0, "repricing_months":1,  "type":"floating"},
                {"name":"Fixed Corp Loans",     "amount":1200, "rate":44.0, "repricing_months":12, "type":"fixed"},
                {"name":"Gov Bonds",            "amount":900,  "rate":38.5, "repricing_months":36, "type":"fixed"},
                {"name":"TLREF Loans",          "amount":500,  "rate":47.5, "repricing_months":1,  "type":"floating"},
                {"name":"Mortgages",            "amount":300,  "rate":35.0, "repricing_months":60, "type":"fixed"},
            ],
            "liabilities": [
                {"name":"Demand Deposits",     "amount":1500, "rate":16.5, "repricing_months":1,  "type":"floating"},
                {"name":"Time Deposits 3M",    "amount":1900, "rate":43.5, "repricing_months":3,  "type":"fixed"},
                {"name":"Time Deposits 6M",    "amount":600,  "rate":41.5, "repricing_months":6,  "type":"fixed"},
                {"name":"Wholesale Funding",   "amount":400,  "rate":45.5, "repricing_months":12, "type":"fixed"},
                {"name":"Sub Debt",            "amount":200,  "rate":50.0, "repricing_months":60, "type":"fixed"},
            ],
            "base_rate": 44.0, "horizon_months": 12, "turkish_context": True,
        }
    },
    {
        "id":          "swap_hedged",
        "name":        "IRS Hedge Uygulandı",
        "description": "500M 3Y receive-fixed IRS hedge sonrası bilanço",
        "category":    "Hedged",
        "builtin":     True,
        "data": {
            "assets": [
                {"name":"Floating Rate Loans",  "amount":2000, "rate":47.5, "repricing_months":1,  "type":"floating"},
                {"name":"Fixed Corp Loans",     "amount":1200, "rate":44.0, "repricing_months":12, "type":"fixed"},
                {"name":"Gov Bonds",            "amount":900,  "rate":38.5, "repricing_months":36, "type":"fixed"},
                {"name":"TLREF Loans",          "amount":500,  "rate":46.0, "repricing_months":1,  "type":"floating"},
                {"name":"Mortgages",            "amount":300,  "rate":35.0, "repricing_months":60, "type":"fixed"},
                {"name":"IRS (receive-fixed)",  "amount":500,  "rate":44.5, "repricing_months":36, "type":"fixed"},
            ],
            "liabilities": [
                {"name":"Demand Deposits",     "amount":1500, "rate":15.0, "repricing_months":1,  "type":"floating"},
                {"name":"Time Deposits 3M",    "amount":1900, "rate":42.0, "repricing_months":3,  "type":"fixed"},
                {"name":"Time Deposits 6M",    "amount":600,  "rate":40.0, "repricing_months":6,  "type":"fixed"},
                {"name":"Wholesale Funding",   "amount":400,  "rate":44.0, "repricing_months":12, "type":"fixed"},
                {"name":"Sub Debt",            "amount":200,  "rate":50.0, "repricing_months":60, "type":"fixed"},
                {"name":"IRS (pay-floating)",  "amount":500,  "rate":47.0, "repricing_months":1,  "type":"floating"},
            ],
            "base_rate": 42.5, "horizon_months": 12, "turkish_context": True,
        }
    },
    {
        "id":          "fx_outflow",
        "name":        "FX Çıkış Senaryosu",
        "description": "USD mevduat çıkışı + TRY kuru baskısı",
        "category":    "Stress",
        "builtin":     True,
        "data": {
            "assets": [
                {"name":"TL Floating Loans",   "amount":1800, "rate":47.5, "repricing_months":1,  "type":"floating"},
                {"name":"TL Fixed Corp",       "amount":1200, "rate":44.0, "repricing_months":12, "type":"fixed"},
                {"name":"USD Loans (TL eq.)",  "amount":600,  "rate":9.5,  "repricing_months":12, "type":"fixed"},
                {"name":"Gov Bonds",           "amount":900,  "rate":38.5, "repricing_months":36, "type":"fixed"},
                {"name":"TLREF Loans",         "amount":400,  "rate":46.0, "repricing_months":1,  "type":"floating"},
            ],
            "liabilities": [
                {"name":"TL Demand Deposits",  "amount":1200, "rate":15.0, "repricing_months":1,  "type":"floating"},
                {"name":"USD Deposits (TL eq.)","amount":800, "rate":5.5,  "repricing_months":3,  "type":"fixed"},
                {"name":"TL Time Dep 3M",      "amount":1600, "rate":42.0, "repricing_months":3,  "type":"fixed"},
                {"name":"TL Time Dep 6M",      "amount":500,  "rate":40.0, "repricing_months":6,  "type":"fixed"},
                {"name":"Wholesale TL",        "amount":400,  "rate":44.0, "repricing_months":12, "type":"fixed"},
            ],
            "base_rate": 42.5, "horizon_months": 12, "turkish_context": True,
        }
    },
    {
        "id":          "rate_cut_cycle",
        "name":        "Faiz İndirim Döngüsü",
        "description": "TCMB faiz indirim senaryosu — 2025 sonrası gevşeme beklentisi",
        "category":    "Base",
        "builtin":     True,
        "data": {
            "assets": [
                {"name":"Floating Loans (low)",  "amount":2000, "rate":38.0, "repricing_months":1,  "type":"floating"},
                {"name":"Fixed Corp Loans",      "amount":1200, "rate":44.0, "repricing_months":12, "type":"fixed"},
                {"name":"Gov Bond Portfolio",    "amount":900,  "rate":38.5, "repricing_months":36, "type":"fixed"},
                {"name":"TLREF Loans (low)",     "amount":500,  "rate":39.0, "repricing_months":1,  "type":"floating"},
                {"name":"Mortgages",             "amount":300,  "rate":35.0, "repricing_months":60, "type":"fixed"},
            ],
            "liabilities": [
                {"name":"Demand Deposits",       "amount":1500, "rate":12.0, "repricing_months":1,  "type":"floating"},
                {"name":"Time Deposits 3M",      "amount":1900, "rate":36.0, "repricing_months":3,  "type":"fixed"},
                {"name":"Time Deposits 6M",      "amount":600,  "rate":35.0, "repricing_months":6,  "type":"fixed"},
                {"name":"Wholesale",             "amount":400,  "rate":38.0, "repricing_months":12, "type":"fixed"},
                {"name":"Sub Debt",              "amount":200,  "rate":45.0, "repricing_months":60, "type":"fixed"},
            ],
            "base_rate": 35.0, "horizon_months": 12, "turkish_context": True,
        }
    },
]


def list_scenarios() -> list:
    """Tüm senaryoları listele (builtin + kullanıcı)."""
    scenarios = list(BUILTIN_SCENARIOS)
    if os.path.exists(SCENARIOS_FILE):
        try:
            user_scenarios = json.load(open(SCENARIOS_FILE))
            scenarios.extend(user_scenarios)
        except Exception:
            pass
    # Sadece metadata döndür (data hariç — liste için)
    return [
        {"id": s["id"], "name": s["name"], "description": s["description"],
         "category": s.get("category","Custom"), "builtin": s.get("builtin", False)}
        for s in scenarios
    ]


def get_scenario(scenario_id: str) -> Optional[dict]:
    """Senaryo ID'sinden tam veriyi getir."""
    # Builtin'lerde ara
    for s in BUILTIN_SCENARIOS:
        if s["id"] == scenario_id:
            return s
    # Kullanıcı senaryolarında ara
    if os.path.exists(SCENARIOS_FILE):
        try:
            for s in json.load(open(SCENARIOS_FILE)):
                if s["id"] == scenario_id:
                    return s
        except Exception:
            pass
    return None


def save_scenario(scenario: dict) -> dict:
    """Kullanıcı senaryosu kaydet."""
    existing = []
    if os.path.exists(SCENARIOS_FILE):
        try:
            existing = json.load(open(SCENARIOS_FILE))
        except Exception:
            pass
    # ID yoksa oluştur
    if "id" not in scenario:
        scenario["id"] = f"user_{int(datetime.now().timestamp())}"
    scenario["builtin"]    = False
    scenario["saved_at"]   = datetime.now().isoformat()
    # Aynı ID varsa güncelle
    existing = [s for s in existing if s.get("id") != scenario["id"]]
    existing.append(scenario)
    _atomic_json_write(SCENARIOS_FILE, existing, ensure_ascii=False)
    return {"saved": True, "id": scenario["id"]}


def delete_scenario(scenario_id: str) -> dict:
    """Kullanıcı senaryosu sil (builtin silinemez)."""
    if not os.path.exists(SCENARIOS_FILE):
        return {"deleted": False, "reason": "Not found"}
    try:
        existing = json.load(open(SCENARIOS_FILE))
        new_list = [s for s in existing if s.get("id") != scenario_id]
        _atomic_json_write(SCENARIOS_FILE, new_list, ensure_ascii=False)
        return {"deleted": len(new_list) < len(existing), "id": scenario_id}
    except Exception as e:
        return {"deleted": False, "reason": str(e)}

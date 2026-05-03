"""
Bek Rate Desk — Sprint 5: Data Engine
=======================================
Operasyonel kullanım için veri köprüsü.

A. Import Engine     — Excel/CSV bilanço yükleme, sütun mapping, doğrulama
B. Template Export   — boş bilanço template oluştur (standart format)
C. Scenario Compare  — iki senaryoyu yan yana karşılaştır
D. Regulatory Output — BDDK LCR/NSFR formatı, Basel III IRRBB EVE tablosu
E. Core Banking Connector — REST API + CSV adapter şablonu

Desteklenen core banking çözümleri:
  Temenos T24/Transact, Oracle FLEXCUBE, Netsis, Logo, İşbankası CBS,
  Garanti GKOS, Ziraat BANKOS, generik CSV/Excel export formatları.
"""

import json, os, io, csv
from datetime import datetime, date
from typing import Optional

DATA_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "data")
os.makedirs(DATA_DIR, exist_ok=True)

# ──────────────────────────────────────────────────────────────────────────────
# STANDART SÜTUN ŞEMASI
# Core banking'den ne gelirse gelsin bu formata normalize edilir.
# ──────────────────────────────────────────────────────────────────────────────
ASSET_COLUMNS = [
    "name", "amount", "rate", "repricing_months", "type",
    "currency", "maturity_date", "product_code", "book_value",
]
LIABILITY_COLUMNS = [
    "name", "amount", "rate", "repricing_months", "type",
    "currency", "maturity_date", "product_code", "book_value",
]

# Bilinen core banking sütun adı eşleştirmeleri
COLUMN_ALIASES = {
    # Türkçe bankacılık terminolojisi
    "tutar": "amount", "bakiye": "amount", "anapara": "amount",
    "faiz": "rate", "faiz_orani": "rate", "faiz oranı": "rate",
    "vade": "repricing_months", "vade_ay": "repricing_months",
    "ad": "name", "ürün": "name", "hesap_adi": "name",
    "tip": "type", "tür": "type",
    "para_birimi": "currency", "doviz": "currency",
    "vade_tarihi": "maturity_date", "son_vade": "maturity_date",
    "urun_kodu": "product_code", "hesap_kodu": "product_code",
    "defter_degeri": "book_value",
    # İngilizce alternatifler
    "balance": "amount", "outstanding": "amount", "nominal": "amount",
    "interest_rate": "rate", "coupon": "rate", "coupon_rate": "rate",
    "maturity_months": "repricing_months", "reprice_months": "repricing_months",
    "instrument": "name", "product": "name", "account": "name",
    "ccy": "currency",
    "maturity": "maturity_date",
    "code": "product_code",
}

VALID_TYPES = {"fixed", "floating", "sabit", "değişken", "degisken", "float", "fix"}

def _normalize_type(t: str) -> str:
    t = str(t).strip().lower()
    return "floating" if t in {"floating", "değişken", "degisken", "float", "tlref", "libor", "sofr"} else "fixed"

def _normalize_col(col: str) -> str:
    return COLUMN_ALIASES.get(col.strip().lower(), col.strip().lower())

def _parse_number(v) -> Optional[float]:
    try:
        return float(str(v).replace(",", ".").replace("₺","").replace("$","").strip())
    except Exception:
        return None


# ══════════════════════════════════════════════════════════════════════════════
# A. IMPORT ENGINE
# ══════════════════════════════════════════════════════════════════════════════

def parse_csv_import(csv_text: str, side: str = "assets") -> dict:
    """
    CSV metnini parse eder, standart formata normalize eder.
    side: "assets" | "liabilities"
    """
    reader    = csv.DictReader(io.StringIO(csv_text.strip()))
    raw_rows  = list(reader)
    if not raw_rows:
        return {"error": "CSV boş veya başlık satırı bulunamadı", "rows": []}

    # Sütun normalize
    normalized = []
    warnings   = []

    for i, row in enumerate(raw_rows):
        norm = {_normalize_col(k): v for k, v in row.items()}
        item = {}
        errs = []

        # name
        item["name"] = str(norm.get("name", f"Item {i+1}")).strip() or f"Item {i+1}"

        # amount
        amt = _parse_number(norm.get("amount"))
        if amt is None:
            errs.append(f"Satır {i+2}: 'amount' okunamadı ({norm.get('amount')})")
            amt = 0.0
        item["amount"] = abs(amt)

        # rate
        rate = _parse_number(norm.get("rate"))
        if rate is None:
            warnings.append(f"Satır {i+2}: 'rate' bulunamadı, 0 kullanılıyor")
            rate = 0.0
        # 0-1 arası ise yüzdeye çevir
        if 0 < rate <= 1:
            rate *= 100
        item["rate"] = round(rate, 4)

        # repricing_months
        rp = _parse_number(norm.get("repricing_months"))
        if rp is None:
            # maturity_date'ten hesaplamayı dene
            md = norm.get("maturity_date", "")
            if md:
                try:
                    md_date = datetime.strptime(str(md).strip(), "%Y-%m-%d").date()
                    rp = max(1, (md_date - date.today()).days / 30)
                except Exception:
                    rp = 12
            else:
                rp = 12
            warnings.append(f"Satır {i+2}: vade tahmini {rp:.0f}ay")
        item["repricing_months"] = round(float(rp), 1)

        # type
        raw_type = norm.get("type", "fixed")
        item["type"] = _normalize_type(raw_type)

        # optional fields
        item["currency"]     = str(norm.get("currency", "TL")).strip().upper()
        item["product_code"] = str(norm.get("product_code", "")).strip()
        item["book_value"]   = _parse_number(norm.get("book_value")) or item["amount"]

        if errs:
            warnings.extend(errs)
        else:
            normalized.append(item)

    return {
        "rows":     normalized,
        "count":    len(normalized),
        "skipped":  len(raw_rows) - len(normalized),
        "warnings": warnings,
        "side":     side,
        "parsed_at":datetime.now().strftime("%Y-%m-%d %H:%M"),
    }


def parse_excel_import(file_bytes: bytes, side: str = "assets",
                        sheet_name: str = None) -> dict:
    """
    Excel (.xlsx) dosyasını parse eder.
    openpyxl kullanır. Yoksa CSV fallback önerir.
    """
    try:
        import openpyxl
        wb = openpyxl.load_workbook(io.BytesIO(file_bytes), data_only=True)
        ws = wb[sheet_name] if sheet_name and sheet_name in wb.sheetnames else wb.active

        rows = list(ws.iter_rows(values_only=True))
        if len(rows) < 2:
            return {"error": "Excel dosyası çok az satır içeriyor", "rows": []}

        # Header
        headers = [str(h).strip() if h else f"col_{i}" for i, h in enumerate(rows[0])]
        headers = [_normalize_col(h) for h in headers]

        csv_rows = []
        for row in rows[1:]:
            if all(v is None for v in row):
                continue
            csv_rows.append(dict(zip(headers, [str(v) if v is not None else "" for v in row])))

        if not csv_rows:
            return {"error": "Veri satırı bulunamadı", "rows": []}

        # CSV parser'a yönlendir
        temp_csv = ",".join(headers) + "\n"
        for r in csv_rows:
            temp_csv += ",".join(f'"{r.get(h,"")}"' for h in headers) + "\n"

        return parse_csv_import(temp_csv, side)

    except ImportError:
        return {
            "error": "openpyxl kurulu değil. 'pip install openpyxl' çalıştırın veya CSV kullanın.",
            "rows": []
        }
    except Exception as e:
        return {"error": f"Excel parse hatası: {str(e)}", "rows": []}


# ══════════════════════════════════════════════════════════════════════════════
# B. TEMPLATE EXPORT — boş bilanço şablonu
# ══════════════════════════════════════════════════════════════════════════════

ASSET_SAMPLE_ROWS = [
    ["Floating Rate Loans",  2000, 47.5, 1,  "floating", "TL", "", "KRD-01", 2000],
    ["Fixed Corporate Loans",1200, 44.0, 12, "fixed",    "TL", "", "KRD-02", 1200],
    ["Gov Bond Portfolio",   900,  38.5, 36, "fixed",    "TL", "", "MKB-01", 900],
    ["TLREF-linked Loans",   500,  46.0, 1,  "floating", "TL", "", "KRD-03", 500],
    ["Mortgages (Fixed)",    300,  35.0, 60, "fixed",    "TL", "", "KRD-04", 300],
    ["USD Trade Finance",    50,   8.5,  6,  "fixed",    "USD","", "DIS-01", 50],
]
LIABILITY_SAMPLE_ROWS = [
    ["Demand Deposits",      1500, 15.0, 1,  "floating", "TL", "", "MVD-01", 1500],
    ["Time Deposits (3M)",   1900, 42.0, 3,  "fixed",    "TL", "", "MVD-02", 1900],
    ["Time Deposits (6M)",   600,  40.0, 6,  "fixed",    "TL", "", "MVD-03", 600],
    ["Wholesale Funding",    400,  44.0, 12, "fixed",    "TL", "", "FND-01", 400],
    ["Subordinated Debt",    200,  50.0, 60, "fixed",    "TL", "", "FND-02", 200],
    ["USD Deposits",         30,   5.5,  3,  "fixed",    "USD","", "MVD-04", 30],
]
COLUMN_HEADERS = ["name","amount","rate","repricing_months","type","currency","maturity_date","product_code","book_value"]
COLUMN_LABELS  = ["Ürün Adı","Tutar (M)","Faiz (%)","Vade (Ay)","Tip (fixed/floating)","Para Birimi","Vade Tarihi (YYYY-MM-DD)","Ürün Kodu","Defter Değeri (M)"]

def generate_template_csv(side: str = "assets") -> str:
    """Standart bilanço şablonu CSV üret."""
    rows = ASSET_SAMPLE_ROWS if side == "assets" else LIABILITY_SAMPLE_ROWS
    lines = [",".join(COLUMN_HEADERS)]
    for r in rows:
        lines.append(",".join(f'"{v}"' for v in r))
    return "\n".join(lines)


def generate_template_excel_bytes() -> bytes:
    """
    Tam bilanço template Excel üretir.
    İki sheet: Assets + Liabilities.
    Renkli başlıklar, veri doğrulama notları.
    """
    try:
        import openpyxl
        from openpyxl.styles import PatternFill, Font, Alignment, Border, Side
        from openpyxl.utils import get_column_letter

        wb = openpyxl.Workbook()
        wb.remove(wb.active)

        accent = "1E3A5F"   # dark navy
        green  = "0D4F2E"
        header_fill_asset = PatternFill("solid", fgColor="1a2744")
        header_fill_liab  = PatternFill("solid", fgColor="1a3a2a")
        header_font       = Font(bold=True, color="E2E8F0", size=9)
        label_fill        = PatternFill("solid", fgColor="0e1017")
        label_font        = Font(color="64748B", size=8, italic=True)
        data_font         = Font(color="CBD5E1", size=9)
        thin_border = Border(
            bottom=Side(style='thin', color='1e2233'),
            right=Side(style='thin', color='1e2233'),
        )

        def make_sheet(ws, title, sample_rows, fill):
            ws.title = title
            ws.sheet_view.showGridLines = False

            # Label row (row 1)
            for col, lbl in enumerate(COLUMN_LABELS, 1):
                cell = ws.cell(row=1, column=col, value=lbl)
                cell.fill = label_fill
                cell.font = label_font

            # Header row (row 2)
            for col, hdr in enumerate(COLUMN_HEADERS, 1):
                cell = ws.cell(row=2, column=col, value=hdr.upper())
                cell.fill = fill
                cell.font = header_font
                cell.alignment = Alignment(horizontal='center')

            # Data rows
            for r_idx, row in enumerate(sample_rows, 3):
                for c_idx, val in enumerate(row, 1):
                    cell = ws.cell(row=r_idx, column=c_idx, value=val)
                    cell.font = data_font
                    cell.border = thin_border

            # Column widths
            widths = [30, 12, 10, 14, 18, 14, 20, 12, 16]
            for i, w in enumerate(widths, 1):
                ws.column_dimensions[get_column_letter(i)].width = w

            # Freeze panes
            ws.freeze_panes = "A3"

        make_sheet(wb.create_sheet("Assets"),      "Assets",      ASSET_SAMPLE_ROWS,     header_fill_asset)
        make_sheet(wb.create_sheet("Liabilities"), "Liabilities", LIABILITY_SAMPLE_ROWS, header_fill_liab)

        # Info sheet
        info = wb.create_sheet("README")
        info.sheet_view.showGridLines = False
        info_data = [
            ["BEK RATE DESK — Bilanço Import Şablonu", ""],
            ["", ""],
            ["KULLANIM:", ""],
            ["1. Assets sayfasına varlık verilerini girin", ""],
            ["2. Liabilities sayfasına yükümlülük verilerini girin", ""],
            ["3. Dosyayı kaydedin (.xlsx)",""],
            ["4. Bek Rate Desk'te Import butonuna tıklayın",""],
            ["", ""],
            ["SÜTUN AÇIKLAMALARI:", ""],
            ["name",           "Ürün/Hesap adı (text)"],
            ["amount",         "Tutar — milyon TL (veya ilgili para birimi)"],
            ["rate",           "Yıllık faiz oranı — yüzde olarak (örn: 42.5)"],
            ["repricing_months","Repricing/vade — ay cinsinden (örn: 3 = 3 aylık)"],
            ["type",           "'fixed' veya 'floating'"],
            ["currency",       "Para birimi: TL, USD, EUR"],
            ["maturity_date",  "Vade tarihi: YYYY-MM-DD (opsiyonel)"],
            ["product_code",   "İç ürün kodu (opsiyonel)"],
            ["book_value",     "Defter değeri — milyon TL (opsiyonel, amount ile aynı olabilir)"],
            ["", ""],
            ["NOT: Sütun sırası önemli değil. Sistem otomatik eşler.",""],
            ["Türkçe sütun adları da desteklenir: tutar, faiz, vade, tip vb.",""],
        ]
        for row in info_data:
            info.append(row)
        info.column_dimensions['A'].width = 50
        info.column_dimensions['B'].width = 55

        buf = io.BytesIO()
        wb.save(buf)
        return buf.getvalue()

    except ImportError:
        # openpyxl yoksa CSV döndür
        return generate_template_csv("assets").encode("utf-8")


# ══════════════════════════════════════════════════════════════════════════════
# C. SCENARIO COMPARISON
# ══════════════════════════════════════════════════════════════════════════════

def compare_scenarios(data: dict) -> dict:
    """
    İki NII/EVE/Gap sonucunu yan yana karşılaştırır.
    Input: { scenario_a: {...nii_result}, scenario_b: {...nii_result},
             label_a: str, label_b: str }
    """
    from utils.alm_engine import run_nii_sensitivity

    a_data  = data.get("scenario_a", {})
    b_data  = data.get("scenario_b", {})
    label_a = data.get("label_a", "Senaryo A")
    label_b = data.get("label_b", "Senaryo B")

    if not a_data or not b_data:
        return {"error": "Her iki senaryo verisi de gerekli"}

    result_a = run_nii_sensitivity(a_data)
    result_b = run_nii_sensitivity(b_data)

    # Shared scenario list
    scenarios = list(result_a.get("scenarios", {}).keys())

    # NII karşılaştırma
    nii_compare = []
    for sc in scenarios:
        a_sc = result_a["scenarios"].get(sc, {})
        b_sc = result_b["scenarios"].get(sc, {})
        a_nii = a_sc.get("nii", result_a.get("base_nii", 0))
        b_nii = b_sc.get("nii", result_b.get("base_nii", 0))
        a_delta = a_sc.get("nii_change", 0)
        b_delta = b_sc.get("nii_change", 0)
        nii_compare.append({
            "scenario":        sc,
            "a_nii":           round(a_nii, 2),
            "b_nii":           round(b_nii, 2),
            "a_delta":         round(a_delta, 2),
            "b_delta":         round(b_delta, 2),
            "diff_nii":        round(b_nii - a_nii, 2),
            "diff_delta":      round(b_delta - a_delta, 2),
            "improvement":     b_nii > a_nii,
        })

    # Repricing gap karşılaştırma
    gap_a = result_a.get("repricing_gap", [])
    gap_b = result_b.get("repricing_gap", [])
    gap_compare = []
    for ga, gb in zip(gap_a, gap_b):
        gap_compare.append({
            "bucket":  ga["bucket"],
            "a_gap":   round(ga.get("gap", 0), 2),
            "b_gap":   round(gb.get("gap", 0), 2),
            "a_cum":   round(ga.get("cumulative_gap", 0), 2),
            "b_cum":   round(gb.get("cumulative_gap", 0), 2),
            "diff":    round(gb.get("gap", 0) - ga.get("gap", 0), 2),
        })

    # Özet metrikler
    summary = {
        "base_nii":     {"a": round(result_a.get("base_nii", 0), 2),
                         "b": round(result_b.get("base_nii", 0), 2),
                         "diff": round(result_b.get("base_nii", 0) - result_a.get("base_nii", 0), 2)},
        "nii_at_risk":  {"a": round(result_a.get("nii_at_risk", 0), 2),
                         "b": round(result_b.get("nii_at_risk", 0), 2),
                         "diff": round(result_b.get("nii_at_risk", 0) - result_a.get("nii_at_risk", 0), 2)},
        "total_assets": {"a": round(result_a.get("total_assets", 0), 2),
                         "b": round(result_b.get("total_assets", 0), 2)},
        "total_liabs":  {"a": round(result_a.get("total_liabilities", 0), 2),
                         "b": round(result_b.get("total_liabilities", 0), 2)},
    }

    return {
        "label_a":     label_a,
        "label_b":     label_b,
        "summary":     summary,
        "nii_compare": nii_compare,
        "gap_compare": gap_compare,
        "compared_at": datetime.now().strftime("%Y-%m-%d %H:%M"),
    }


# ══════════════════════════════════════════════════════════════════════════════
# D. REGULATORY OUTPUT — BDDK & BASEL III FORMAT
# ══════════════════════════════════════════════════════════════════════════════

def generate_lcr_report(lcr_data: dict) -> dict:
    """
    BDDK LCR raporlama formatına uygun yapılandırılmış çıktı.
    Bankacılık Düzenleme ve Denetleme Kurumu
    Likidite Karşılama Oranı — Madde 4, Tablo 1 formatı (özet)
    """
    hqla     = lcr_data.get("hqla_total", 0)
    outflows = lcr_data.get("outflows_total", 0)
    inflows  = lcr_data.get("inflows_total", 0)
    lcr      = lcr_data.get("lcr", 0)

    hqla_l1  = lcr_data.get("hqla_level1", 0)
    hqla_l2a = lcr_data.get("hqla_level2a", 0)
    hqla_l2b = lcr_data.get("hqla_level2b", 0)

    status = ("UYGUN" if lcr >= 100 else "UYGUNSUZ")
    status_detail = ("Zorunlu asgari %100'ün üzerinde" if lcr >= 100
                     else f"Zorunlu asgari %100'ün {100-lcr:.1f}% altında — ACİL AKSİYON GEREKLİ")

    rows = [
        {"kod":"A",   "kalem":"YÜKSEK KALİTELİ LİKİT VARLIKLAR (YKLV)",     "tutar":round(hqla,2),    "not":""},
        {"kod":"A.1", "kalem":"  Seviye 1 Varlıklar (haircut %0)",            "tutar":round(hqla_l1,2), "not":"Nakit, TCMB, T.C. Hazine"},
        {"kod":"A.2", "kalem":"  Seviye 2A Varlıklar (haircut %15)",          "tutar":round(hqla_l2a,2),"not":"AAA-AA rated tahvil, OMO teminatı"},
        {"kod":"A.3", "kalem":"  Seviye 2B Varlıklar (haircut %50)",          "tutar":round(hqla_l2b,2),"not":"Maks. YKLV'nin %15'i"},
        {"kod":"B",   "kalem":"NET LİKİDİTE ÇIKIŞLARI (30 günlük)",          "tutar":round(max(0,outflows-min(inflows,outflows*0.75)),2), "not":"Çıkış − min(Giriş, Çıkış×%75)"},
        {"kod":"B.1", "kalem":"  Toplam Beklenen Çıkışlar",                   "tutar":round(outflows,2), "not":""},
        {"kod":"B.2", "kalem":"  Toplam Beklenen Girişler (maks. %75 sınırı)","tutar":round(min(inflows,outflows*0.75),2), "not":""},
        {"kod":"C",   "kalem":"LİKİDİTE KARŞILAMA ORANI (LKO)",              "tutar":round(lcr,2),      "not":f"A/B × 100 = %{lcr:.1f}"},
        {"kod":"D",   "kalem":"YASAL ASGARİ (BDDK)",                         "tutar":100,               "not":"Bireysel+Toplam: %100"},
        {"kod":"E",   "kalem":"FAZLA / (AÇIK)",                              "tutar":round(lcr-100,2),  "not":status},
    ]

    return {
        "rapor_adi":   "Likidite Karşılama Oranı (LKO) — BDDK Format",
        "rapor_tarihi":datetime.now().strftime("%d.%m.%Y"),
        "hazırlayan":  "Bek Rate Desk — Otomatik Üretim",
        "yasal_dayanak":"5411 Sayılı Bankacılık Kanunu / BDDK Likidite Yönetmeliği",
        "lcr_pct":     round(lcr, 2),
        "status":      status,
        "status_detail":status_detail,
        "rows":        rows,
        "uyari":       [] if lcr >= 110 else (
            ["⚠️ LCR iç buffer (%110) altında — günlük izleme yoğunlaştırılmalı"] if lcr >= 100
            else ["🔴 ACİL: LCR yasal sınır altında — derhal TCMB repo veya HQLA artırımı gerekli"]
        ),
    }


def generate_irrbb_report(eve_data: dict, nii_data: dict) -> dict:
    """
    BCBS d368 / Basel III IRRBB EVE tablosu.
    6 standart şok senaryosu: parallel up/down, bear/bull steepen/flatten
    """
    tier1       = float(eve_data.get("tier1_capital", 1))
    scenarios_eve = eve_data.get("scenarios", {})
    scenarios_nii = nii_data.get("scenarios", {})

    # Standard BCBS scenario names → map to our names
    bcbs_map = {
        "Paralel Yukarı (+200bps)":   "parallel_up_200",
        "Paralel Aşağı (−200bps)":   "parallel_down_200",
        "Kısa Yukarı / Uzun Aşağı (Bear Flatten)": "bear_flatten",
        "Kısa Aşağı / Uzun Yukarı (Bull Steepen)": "bull_steepen",
        "Bear Steepen":               "bear_steepen",
        "Bull Flatten":               "bull_flatten",
    }

    eve_rows = []
    for bcbs_label, our_key in bcbs_map.items():
        eve_sc = scenarios_eve.get(our_key, {})
        nii_sc = scenarios_nii.get(our_key, {})
        delta_eve = float(eve_sc.get("eve_change", eve_sc.get("delta_eve", 0)))
        pct_tier1 = abs(delta_eve) / tier1 * 100 if tier1 else 0
        breach    = pct_tier1 > 15
        delta_nii = float(nii_sc.get("nii_change", 0))

        eve_rows.append({
            "senaryo":       bcbs_label,
            "delta_eve_m":   round(delta_eve, 2),
            "pct_tier1":     round(pct_tier1, 2),
            "breach":        breach,
            "delta_nii_m":   round(delta_nii, 2),
            "durum":         "İHLAL" if breach else ("UYARI" if pct_tier1>10 else "UYGUN"),
        })

    worst_eve = max(eve_rows, key=lambda x: abs(x["delta_eve_m"]), default={})
    worst_nii = min((r for r in scenarios_nii.values() if isinstance(r,dict)), key=lambda x: x.get("nii_change",0), default={})

    return {
        "rapor_adi":    "Faiz Oranı Riski Tablosu — BCBS d368 / Basel III IRRBB",
        "rapor_tarihi": datetime.now().strftime("%d.%m.%Y"),
        "hazırlayan":   "Bek Rate Desk — Otomatik Üretim",
        "yasal_dayanak":"Basel Komitesi BCBS d368 (2016) / BDDK IRRBB Rehberi",
        "tier1_capital":round(tier1, 2),
        "eve_rows":     eve_rows,
        "worst_eve":    worst_eve,
        "ozet": {
            "en_kötü_eve_m":   round(worst_eve.get("delta_eve_m", 0), 2),
            "en_kötü_tier1_pct":round(worst_eve.get("pct_tier1", 0), 2),
            "ihlal_var":        any(r["breach"] for r in eve_rows),
            "ihlal_sayısı":     sum(1 for r in eve_rows if r["breach"]),
            "en_kötü_nii_m":   round(worst_nii.get("nii_change", 0), 2) if isinstance(worst_nii,dict) else 0,
        },
        "uyari": (
            ["🔴 ACİL: ΔEVE Tier 1'in %15'ini aşıyor — BRSA bildirimi gerekebilir"]
            if any(r["breach"] for r in eve_rows)
            else ["⚠️ ΔEVE/Tier 1 %10 üzerinde — yakından izle"] if any(r["pct_tier1"]>10 for r in eve_rows)
            else ["✅ Tüm IRRBB senaryoları Basel III limitinin altında"]
        ),
    }


def generate_html_report(report_data: dict, report_type: str) -> str:
    """
    BDDK/Basel raporunu print-ready HTML olarak döndürür.
    Tarayıcıdan Ctrl+P ile PDF'e dönüştürülebilir.
    """
    now     = datetime.now().strftime("%d.%m.%Y %H:%M")
    title   = report_data.get("rapor_adi", "ALM Raporu")
    tarih   = report_data.get("rapor_tarihi", now)
    hazırl  = report_data.get("hazırlayan", "Bek Rate Desk")
    dayanak = report_data.get("yasal_dayanak", "")

    common_style = """
    <style>
      * { box-sizing:border-box; margin:0; padding:0; }
      body { font-family:'DM Mono',monospace; font-size:9pt; color:#1a1a2e; background:#fff; padding:20px 30px; }
      h1 { font-size:13pt; font-weight:700; color:#1e3a5f; border-bottom:2px solid #1e3a5f; padding-bottom:6px; margin-bottom:16px; }
      .meta { display:flex; gap:30px; font-size:8pt; color:#475569; margin-bottom:16px; }
      .meta span strong { color:#1e3a5f; }
      table { width:100%; border-collapse:collapse; font-size:8.5pt; margin-bottom:16px; }
      th { background:#1e3a5f; color:#fff; padding:5px 8px; text-align:left; font-weight:600; font-size:8pt; }
      td { padding:4px 8px; border-bottom:1px solid #e2e8f0; }
      tr:nth-child(even) td { background:#f8fafc; }
      .ihlal { color:#dc2626; font-weight:700; }
      .uyari { color:#d97706; font-weight:600; }
      .uygun { color:#059669; }
      .warn-box { background:#fef3cd; border:1px solid #f59e0b; border-radius:4px; padding:8px 12px; margin-bottom:12px; font-size:8.5pt; }
      .crit-box { background:#fee2e2; border:1px solid #ef4444; border-radius:4px; padding:8px 12px; margin-bottom:12px; font-size:8.5pt; }
      .ok-box   { background:#d1fae5; border:1px solid #10b981; border-radius:4px; padding:8px 12px; margin-bottom:12px; font-size:8.5pt; }
      .footer   { font-size:7.5pt; color:#94a3b8; border-top:1px solid #e2e8f0; padding-top:8px; margin-top:16px; }
      @media print { body { padding:10px 15px; } }
    </style>
    """

    if report_type == "lcr":
        rows_html = "".join(
            f"<tr><td><strong>{r['kod']}</strong></td><td>{r['kalem']}</td>"
            f"<td style='text-align:right'>{r['tutar']:,.2f}</td><td>{r['not']}</td></tr>"
            for r in report_data.get("rows", [])
        )
        status     = report_data.get("status","")
        lcr_pct    = report_data.get("lcr_pct", 0)
        box_cls    = "crit-box" if lcr_pct < 100 else ("warn-box" if lcr_pct < 110 else "ok-box")
        warnings   = "<br>".join(report_data.get("uyari", []))

        return f"""<!DOCTYPE html><html><head><meta charset='UTF-8'>{common_style}</head><body>
<h1>{title}</h1>
<div class='meta'>
  <span><strong>Tarih:</strong> {tarih}</span>
  <span><strong>Hazırlayan:</strong> {hazırl}</span>
  <span><strong>LKO:</strong> %{lcr_pct:.1f}</span>
  <span><strong>Durum:</strong> {status}</span>
</div>
<div class='{box_cls}'>{warnings}</div>
<table>
  <thead><tr><th>Kod</th><th>Kalem</th><th style='text-align:right'>Tutar (M)</th><th>Not</th></tr></thead>
  <tbody>{rows_html}</tbody>
</table>
<p style='font-size:8pt;color:#64748b'><em>Yasal Dayanak: {dayanak}</em></p>
<div class='footer'>Bu rapor Bek Rate Desk ALM Platformu tarafından otomatik üretilmiştir. Nihai sorumluluğu Hazine/ALM Masası'na aittir.</div>
</body></html>"""

    elif report_type == "irrbb":
        eve_rows = report_data.get("eve_rows", [])
        def _eve_row(r):
            ec = "#dc2626" if r["delta_eve_m"] < 0 else "#059669"
            nc = "#dc2626" if r["delta_nii_m"] < 0 else "#059669"
            c  = "ihlal" if r["breach"] else ("uyari" if r["pct_tier1"] > 10 else "uygun")
            return (f"<tr><td>{r['senaryo']}</td>"
                    f"<td style='text-align:right;color:{ec}'>{r['delta_eve_m']:+.2f}</td>"
                    f"<td style='text-align:right' class='{c}'>%{r['pct_tier1']:.1f}</td>"
                    f"<td style='text-align:right;color:{nc}'>{r['delta_nii_m']:+.2f}</td>"
                    f"<td class='{c}'>{r['durum']}</td></tr>")
        rows_html = "".join(_eve_row(r) for r in eve_rows)
        ozet    = report_data.get("ozet", {})
        warns   = "<br>".join(report_data.get("uyari", []))
        box_cls = "crit-box" if ozet.get("ihlal_var") else ("warn-box" if any(r["pct_tier1"]>10 for r in eve_rows) else "ok-box")

        return f"""<!DOCTYPE html><html><head><meta charset='UTF-8'>{common_style}</head><body>
<h1>{title}</h1>
<div class='meta'>
  <span><strong>Tarih:</strong> {tarih}</span>
  <span><strong>Hazırlayan:</strong> {hazırl}</span>
  <span><strong>Tier 1 Sermaye:</strong> {report_data.get('tier1_capital',0):,.0f}M</span>
  <span><strong>IRRBB İhlal:</strong> {'EVET ⚠️' if ozet.get('ihlal_var') else 'HAYIR ✓'}</span>
</div>
<div class='{box_cls}'>{warns}</div>
<table>
  <thead><tr><th>Senaryo</th><th style='text-align:right'>ΔEVE (M)</th><th style='text-align:right'>ΔEVE/Tier1</th><th style='text-align:right'>ΔNII (M)</th><th>Durum</th></tr></thead>
  <tbody>{rows_html}</tbody>
</table>
<table style='width:auto;margin-top:8px'>
  <thead><tr><th colspan='2'>Özet</th></tr></thead>
  <tbody>
    <tr><td>En kötü ΔEVE</td><td style='text-align:right'><strong>{ozet.get('en_kötü_eve_m',0):+.2f}M (%{ozet.get('en_kötü_tier1_pct',0):.1f} Tier 1)</strong></td></tr>
    <tr><td>En kötü ΔNII</td><td style='text-align:right'><strong>{ozet.get('en_kötü_nii_m',0):+.2f}M</strong></td></tr>
    <tr><td>Basel III Limiti</td><td style='text-align:right'>%15 Tier 1</td></tr>
  </tbody>
</table>
<p style='font-size:8pt;color:#64748b;margin-top:8px'><em>Yasal Dayanak: {dayanak}</em></p>
<div class='footer'>Bu rapor Bek Rate Desk ALM Platformu tarafından otomatik üretilmiştir.</div>
</body></html>"""

    return f"<html><body><h2>{title}</h2><p>Format desteklenmiyor.</p></body></html>"


# ══════════════════════════════════════════════════════════════════════════════
# E. CORE BANKING CONNECTOR
# ══════════════════════════════════════════════════════════════════════════════

CB_SYSTEM_PROFILES = {
    "temenos_t24": {
        "name":        "Temenos T24 / Transact",
        "export_format":"CSV veya XML",
        "asset_tables":["LD.LOANS.AND.DEPOSITS","AM.INVESTMENT"],
        "liab_tables": ["LD.LOANS.AND.DEPOSITS","AC.ACCOUNT"],
        "key_fields":  {"amount":"PRINCIPAL.AMOUNT","rate":"INTEREST.RATE","maturity":"FINAL.MATURITY.DATE"},
        "notes":       "T24'te ENQ (Enquiry) ile CSV export alınır. Tarih formatı DD MMM YYYY.",
    },
    "oracle_flexcube": {
        "name":        "Oracle FLEXCUBE",
        "export_format":"CSV veya Excel",
        "asset_tables":["CSTB_ACCOUNT","LCTB_LIAB_LEDGER"],
        "liab_tables": ["CSTB_ACCOUNT","STTB_ACCT_MASTER"],
        "key_fields":  {"amount":"ACCOUNT_BALANCE","rate":"INTEREST_RATE","maturity":"MATURITY_DATE"},
        "notes":       "Reports → Custom Report → CSV export. Tarih formatı DD-MM-YYYY.",
    },
    "netsis": {
        "name":        "Netsis (Türkiye)",
        "export_format":"Excel (.xls/.xlsx)",
        "asset_tables":["KREDI_HESAPLARI","MENKUL_PORTFOY"],
        "liab_tables": ["MEVDUAT_HESAPLARI","FONLAMA"],
        "key_fields":  {"amount":"BAKIYE","rate":"FAIZ_ORANI","maturity":"VADE_TARIHI"},
        "notes":       "Finans → Muhasebe → Raporlar → Excel Export. Virgüllü sayı formatı.",
    },
    "generic_csv": {
        "name":        "Generik CSV Export",
        "export_format":"CSV (UTF-8)",
        "asset_tables":["Herhangi bir tablo"],
        "liab_tables": ["Herhangi bir tablo"],
        "key_fields":  {"amount":"tutar/amount/bakiye","rate":"faiz/rate","maturity":"vade/maturity"},
        "notes":       "Sütun adları otomatik eşlenir. Türkçe ve İngilizce sütun adları desteklenir.",
    },
}


def get_connector_profile(system: str) -> dict:
    return CB_SYSTEM_PROFILES.get(system, CB_SYSTEM_PROFILES["generic_csv"])


def list_connector_profiles() -> list:
    return [{"id": k, **{kk:vv for kk,vv in v.items() if kk!="notes"}}
            for k, v in CB_SYSTEM_PROFILES.items()]


def generate_connector_guide(system: str) -> dict:
    """Core banking sistemine göre entegrasyon rehberi üretir."""
    profile = get_connector_profile(system)

    steps = {
        "temenos_t24": [
            "T24'e giriş yapın → Browser → ENQ (Enquiry) modülüne gidin",
            "LD.LOANS.AND.DEPOSITS ENQ'yu açın, tüm aktif sözleşmeleri seçin",
            "Format: PRINCIPAL.AMOUNT, INTEREST.RATE, FINAL.MATURITY.DATE sütunlarını ekleyin",
            "CSV olarak export edin (Export → CSV)",
            "Bek Rate Desk → Import → T24 seçin → Dosya yükleyin",
        ],
        "oracle_flexcube": [
            "FLEXCUBE → Reports → Custom Reports",
            "CSTB_ACCOUNT tablosundan ACCOUNT_BALANCE, INTEREST_RATE, MATURITY_DATE seçin",
            "CSV format seçin, dışa aktarın",
            "Bek Rate Desk → Import → FLEXCUBE seçin → Dosya yükleyin",
        ],
        "netsis": [
            "Netsis → Finans → Muhasebe → Kredi/Mevduat Hesapları",
            "Dönem seçin → Raporla",
            "Excel olarak kaydedin (.xlsx)",
            "Bek Rate Desk → Import → Excel seçin → Dosya yükleyin",
        ],
        "generic_csv": [
            "Mevcut sistemden bilanço raporunu CSV veya Excel olarak alın",
            "Sütun adlarını kontrol edin (tutar/amount/bakiye, faiz/rate, vade/repricing_months)",
            "Bek Rate Desk Template'i indirin (doğru format için)",
            "Verilerinizi template formatına uyarlayın",
            "Import edin",
        ],
    }

    return {
        "system":   system,
        "profile":  profile,
        "steps":    steps.get(system, steps["generic_csv"]),
        "template_available": True,
        "auto_mapping": True,
        "supported_formats": ["CSV (UTF-8)", "Excel (.xlsx)", "Excel (.xls)"],
    }

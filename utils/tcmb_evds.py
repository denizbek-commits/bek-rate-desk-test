"""
Bek Rate Desk — TCMB EVDS Integration
======================================
Resmi TCMB EVDS API'sinden TR makro ve faiz verilerini çeker.
API anahtarı: TCMB_EVDS_KEY ortam değişkeni veya config.json.
Anahtar yoksa mevcut yaklaşık yöntemlere (FRED/yfinance) geri döner.

EVDS API: https://evds2.tcmb.gov.tr/service/evds/
"""

import os, json, math, requests
from utils.io_utils import _atomic_json_write
from datetime import date, timedelta
from typing import Optional
import pandas as pd

EVDS_BASE   = "https://evds2.tcmb.gov.tr/service/evds"
CONFIG_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                           "data", "tcmb_config.json")

# ──────────────────────────────────────────────────────────────────────────────
# EVDS seri kodları
# ──────────────────────────────────────────────────────────────────────────────
EVDS_SERIES = {
    # Kısa vadeli faiz
    "policy_rate":       "TP.MB.B.G.015",   # Gecelik borç verme (üst bant)
    "policy_bid":        "TP.MB.B.G.014",   # Gecelik borç alma (alt bant)
    "onfa":              "TP.MB.B.A.005",   # Ağırlıklı Ort. Fonlama Faizi
    # Hazine getiri eğrisi (BIST DİBS benchmark)
    "dibs_2y":           "TP.SGPYF.B91D",  # 2 yıllık DİBS bileşik
    "dibs_5y":           "TP.SGPYF.B91D",  # 5 yıllık placeholder (aynı seri farklı vade)
    # Döviz kurları
    "usdtry":            "TP.DK.USD.S.YTL",
    "eurtry":            "TP.DK.EUR.S.YTL",
    "gbptry":            "TP.DK.GBP.S.YTL",
    # Makro
    "cpi_monthly":       "TP.FG.J0",        # TÜFE aylık değişim
    "m2":                "TP.PARABASE.G1",   # M2 para arzı
    "reserves":          "TP.AB.B1",         # Brüt rezervler (Milyar USD)
    # CDS (ülke risk primi) — FRED'den çekilir
}

# Alternatif: FRED TR proxy serileri
FRED_TR_PROXIES = {
    "cds_5y": "DDDGTXQ3A066NBIS",  # Türkiye 5Y CDS FRED
}

FRED_API_KEY = "dfe19785bb3b852bce75e193315b51da"
FRED_BASE    = "https://api.stlouisfed.org/fred/series/observations"


def _get_evds_key() -> Optional[str]:
    """TCMB EVDS API anahtarını ortam değişkeni veya config.json'dan okur."""
    key = os.environ.get("TCMB_EVDS_KEY")
    if key:
        return key
    if os.path.exists(CONFIG_PATH):
        try:
            cfg = json.load(open(CONFIG_PATH))
            return cfg.get("evds_key")
        except Exception:
            pass
    return None


def save_evds_key(key: str):
    """EVDS anahtarını config.json'a kaydeder."""
    os.makedirs(os.path.dirname(CONFIG_PATH), exist_ok=True)
    cfg = {}
    if os.path.exists(CONFIG_PATH):
        try:
            cfg = json.load(open(CONFIG_PATH))
        except Exception:
            pass
    cfg["evds_key"] = key
    _atomic_json_write(CONFIG_PATH, cfg)


def _fetch_evds_series(series_id: str, days: int = 5) -> Optional[float]:
    """EVDS'den tek seri son değerini çeker."""
    key = _get_evds_key()
    if not key:
        return None
    start = (date.today() - timedelta(days=max(days, 30))).strftime("%d-%m-%Y")
    end   = date.today().strftime("%d-%m-%Y")
    try:
        r = requests.get(f"{EVDS_BASE}/data", params={
            "series":    series_id,
            "startDate": start,
            "endDate":   end,
            "type":      "json",
            "key":       key,
        }, timeout=8)
        data = r.json()
        items = data.get("items", [])
        for item in reversed(items):
            v = item.get(series_id.replace(".", "_"))
            if v and v not in ("", None):
                return float(v)
    except Exception:
        pass
    return None


def _fetch_evds_series_history(series_id: str, days: int = 400) -> pd.Series:
    """EVDS'den tarihsel seri döndürür."""
    key = _get_evds_key()
    if not key:
        return pd.Series(dtype=float)
    start = (date.today() - timedelta(days=days)).strftime("%d-%m-%Y")
    end   = date.today().strftime("%d-%m-%Y")
    try:
        r = requests.get(f"{EVDS_BASE}/data", params={
            "series":    series_id,
            "startDate": start,
            "endDate":   end,
            "type":      "json",
            "key":       key,
        }, timeout=10)
        items = r.json().get("items", [])
        field = series_id.replace(".", "_")
        rows = {}
        for item in items:
            d = item.get("Tarih")
            v = item.get(field)
            if d and v not in ("", None):
                try:
                    rows[pd.to_datetime(d, dayfirst=True)] = float(v)
                except Exception:
                    pass
        if rows:
            return pd.Series(rows).sort_index()
    except Exception:
        pass
    return pd.Series(dtype=float)


def _fetch_fred_value(series_id: str) -> Optional[float]:
    try:
        r = requests.get(FRED_BASE, params={
            "series_id": series_id, "api_key": FRED_API_KEY,
            "file_type": "json", "sort_order": "desc", "limit": 5,
        }, timeout=6)
        for obs in r.json().get("observations", []):
            v = obs.get("value", ".")
            if v != ".":
                return float(v)
    except Exception:
        pass
    return None


# ──────────────────────────────────────────────────────────────────────────────
# Yüksek seviyeli veri çekme fonksiyonları
# ──────────────────────────────────────────────────────────────────────────────

def fetch_policy_rates() -> dict:
    """
    TCMB politika faiz oranlarını çeker.
    EVDS varsa resmi; yoksa FRED / sabit fallback.
    """
    from utils.data_health import mark_fetch
    lending = _fetch_evds_series("TP.MB.B.G.015") or _fetch_fred_value("IRSTCI01TRM156N")
    borrowing = _fetch_evds_series("TP.MB.B.G.014")
    onfa  = _fetch_evds_series("TP.MB.B.A.005")

    # Fallback: last known TCMB upper band (Nisan 2026)
    is_live = lending is not None and _get_evds_key()
    if lending is None:
        lending = 42.5
    if borrowing is None:
        borrowing = lending - 3.0
    if onfa is None:
        onfa = (lending + borrowing) / 2

    source = "EVDS" if is_live else "fallback"
    mark_fetch("TCMB_policy", "live" if is_live else "fallback")

    return {
        "lending_rate":   round(lending, 2),
        "borrowing_rate": round(borrowing, 2),
        "onfa":           round(onfa, 2),
        "source":         source,
    }


# ── Module-level cached policy rate ──────────────────────────────────────────
import time as _time
_POLICY_RATE_CACHE: dict = {"rate": None, "ts": 0.0}
_POLICY_RATE_TTL   = 300   # refresh every 5 minutes


def get_policy_rate(fallback: float = 42.5) -> float:
    """
    Uygulama genelinde tek noktadan TCMB politika faizi (üst bant).
    5 dakika önbelleği — aynı süreç içinde tekrar fetch yapmaz.

    Tüm engine'lerde `data.get("base_rate", 42.5)` yerine bu fonksiyon
    default olarak kullanılmalıdır:

        from utils.tcmb_evds import get_policy_rate
        rate = float(data.get("base_rate") or get_policy_rate())
    """
    global _POLICY_RATE_CACHE
    now = _time.time()
    if _POLICY_RATE_CACHE["rate"] is not None and (now - _POLICY_RATE_CACHE["ts"]) < _POLICY_RATE_TTL:
        return _POLICY_RATE_CACHE["rate"]

    try:
        rates = fetch_policy_rates()
        value = float(rates["lending_rate"])
    except Exception:
        value = fallback

    _POLICY_RATE_CACHE = {"rate": value, "ts": now}
    return value


def fetch_tr_yield_curve_evds() -> dict:
    """
    TCMB/BIST DİBS getiri eğrisini çeker.
    EVDS anahtarı varsa resmi seri; yoksa mevcut fetch_tr_yield_curve() kullanılır.
    Döndürür: {"curve": [{"tenor":"2Y","years":2.0,"yield":38.5}, ...], "source":"EVDS/fallback"}
    """
    key = _get_evds_key()

    if key:
        # EVDS DİBS benchmark serileri
        evds_tenors = {
            "91D":  (0.25, "TP.SGPYF.B91D"),
            "182D": (0.5,  "TP.SGPYF.B182D"),
            "1Y":   (1.0,  "TP.SGPYF.B1Y"),
            "2Y":   (2.0,  "TP.SGPYF.B2Y"),
            "3Y":   (3.0,  "TP.SGPYF.B3Y"),
            "5Y":   (5.0,  "TP.SGPYF.B5Y"),
            "10Y":  (10.0, "TP.SGPYF.B10Y"),
        }
        curve_points = []
        for tenor, (years, sid) in evds_tenors.items():
            v = _fetch_evds_series(sid)
            if v is not None:
                curve_points.append({"tenor": tenor, "years": years, "yield": round(v, 3)})

        if len(curve_points) >= 3:
            curve_points.sort(key=lambda x: x["years"])
            return {"curve": curve_points, "source": "EVDS"}

    # Fallback — mevcut yöntem
    try:
        from utils.alm_advisor_engine import fetch_tr_yield_curve
        result = fetch_tr_yield_curve()
        if result and result.get("curve"):
            result["source"] = "fallback"
            return result
    except Exception:
        pass

    # Son çare: statik yaklaşım (gerçek TR eğrisi şekline göre)
    policy = fetch_policy_rates()
    pr = policy["lending_rate"]
    fallback_curve = [
        {"tenor": "1M",  "years": 1/12, "yield": round(pr + 1.5, 2)},
        {"tenor": "3M",  "years": 0.25, "yield": round(pr + 0.5, 2)},
        {"tenor": "6M",  "years": 0.5,  "yield": round(pr - 1.0, 2)},
        {"tenor": "1Y",  "years": 1.0,  "yield": round(pr - 3.0, 2)},
        {"tenor": "2Y",  "years": 2.0,  "yield": round(pr - 4.5, 2)},
        {"tenor": "3Y",  "years": 3.0,  "yield": round(pr - 5.5, 2)},
        {"tenor": "5Y",  "years": 5.0,  "yield": round(pr - 6.5, 2)},
        {"tenor": "10Y", "years": 10.0, "yield": round(pr - 8.0, 2)},
    ]
    return {"curve": fallback_curve, "source": "parametric_fallback"}


def fetch_tr_macro() -> dict:
    """
    TR makro göstergeler: CDS 5Y, rezervler, M2, enflasyon, USDTRY.
    """
    key = _get_evds_key()

    usdtry = None
    if key:
        usdtry = _fetch_evds_series("TP.DK.USD.S.YTL")
    if usdtry is None:
        try:
            import yfinance as yf
            d = yf.download("USDTRY=X", period="2d", progress=False)
            if not d.empty:
                usdtry = float(d["Close"].iloc[-1])
        except Exception:
            usdtry = 38.5

    cds_5y = _fetch_fred_value(FRED_TR_PROXIES["cds_5y"])
    reserves = _fetch_evds_series("TP.AB.B1") if key else None
    cpi      = _fetch_evds_series("TP.FG.J0") if key else None

    return {
        "usdtry":       round(usdtry or 38.5, 4),
        "cds_5y_bps":   round(cds_5y, 0) if cds_5y else None,
        "reserves_bn":  round(reserves, 1) if reserves else None,
        "cpi_monthly":  round(cpi, 2) if cpi else None,
        "source":       "EVDS" if (key and cpi) else "partial",
    }


def _fetch_tcmb_xml_fx() -> dict:
    """
    Fetches live FX rates from TCMB's public XML endpoint (no API key needed).
    URL: https://www.tcmb.gov.tr/kurlar/today.xml
    Returns dict {pair: rate} e.g. {"USDTRY": 38.52, "EURTRY": 41.80, ...}
    Updates once daily at ~15:30 Ankara time.
    """
    import xml.etree.ElementTree as ET
    _XML_URL = "https://www.tcmb.gov.tr/kurlar/today.xml"
    # Map TCMB currency codes → our pair names
    _CCY_MAP = {"USD": "USDTRY", "EUR": "EURTRY", "GBP": "GBPTRY",
                "JPY": "JPYTRY", "CHF": "CHFTRY", "XAU": "XAUTRY"}
    try:
        r = requests.get(_XML_URL, timeout=8,
                         headers={"User-Agent": "BekRateDesk/6 (contact@ace-trading.com)"})
        if r.status_code != 200:
            return {}
        root = ET.fromstring(r.content)
        rates = {}
        for currency in root.findall("Currency"):
            code = currency.get("CurrencyCode", "")
            pair = _CCY_MAP.get(code)
            if not pair:
                continue
            # ForexSelling is the market ask; BanknoteSelling as fallback
            val = None
            for tag in ("ForexSelling", "BanknoteSelling", "ForexBuying"):
                elem = currency.find(tag)
                if elem is not None and elem.text and elem.text.strip():
                    try:
                        raw = float(elem.text.strip().replace(",", "."))
                        # JPY is quoted per 100 units in TCMB XML
                        if code == "JPY":
                            raw = raw / 100.0
                        val = round(raw, 4)
                        break
                    except ValueError:
                        pass
            if val and val > 0:
                rates[pair] = val
        return rates
    except Exception:
        return {}


def fetch_fx_rates_tcmb() -> dict:
    """
    TCMB resmi döviz kurları.
    Öncelik sırası: TCMB XML (ücretsiz, resmi) → EVDS → yfinance → hardcoded fallback.
    """
    from utils.data_health import mark_fetch
    # 1. Try TCMB public XML (no key needed)
    rates = _fetch_tcmb_xml_fx()
    if rates.get("USDTRY"):
        mark_fetch("TCMB_XML", "live")
        return {"rates": rates, "source": "TCMB_XML"}

    # 2. Try EVDS (requires API key)
    key = _get_evds_key()
    if key:
        evds_pairs = {
            "USDTRY": "TP.DK.USD.S.YTL",
            "EURTRY": "TP.DK.EUR.S.YTL",
            "GBPTRY": "TP.DK.GBP.S.YTL",
            "JPYTRY": "TP.DK.JPY.S.YTL",
        }
        evds_rates = {}
        for pair, sid in evds_pairs.items():
            v = _fetch_evds_series(sid)
            if v:
                evds_rates[pair] = round(v, 4)
        if evds_rates.get("USDTRY"):
            return {"rates": evds_rates, "source": "EVDS"}

    # 3. Try yfinance
    yf_pairs = {"USDTRY": "USDTRY=X", "EURTRY": "EURTRY=X",
                "GBPTRY": "GBPTRY=X",  "JPYTRY": "JPYTRY=X"}
    yf_rates = {}
    try:
        import yfinance as yf
        for pair, ticker in yf_pairs.items():
            try:
                d = yf.download(ticker, period="5d", progress=False)
                if not d.empty:
                    yf_rates[pair] = round(float(d["Close"].dropna().iloc[-1]), 4)
            except Exception:
                pass
    except Exception:
        pass
    if yf_rates.get("USDTRY"):
        mark_fetch("yfinance", "live")
        return {"rates": yf_rates, "source": "yfinance"}

    # 4. Hardcoded fallback (last resort)
    mark_fetch("TCMB_XML", "fallback")
    return {
        "rates": {"USDTRY": 38.50, "EURTRY": 41.80, "GBPTRY": 48.50, "JPYTRY": 0.2580},
        "source": "fallback",
    }


def get_evds_status() -> dict:
    """API anahtarı yapılandırma durumunu döndürür."""
    key = _get_evds_key()
    if not key:
        return {"configured": False, "key_preview": None}

    # Test bağlantısı
    try:
        v = _fetch_evds_series("TP.MB.B.G.015", days=30)
        connected = v is not None
    except Exception:
        connected = False

    return {
        "configured": True,
        "key_preview": f"{key[:4]}***{key[-2:]}",
        "connected": connected,
    }

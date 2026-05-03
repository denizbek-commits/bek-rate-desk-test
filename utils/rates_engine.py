"""
Bek Rate Desk — Rates Engine
==============================
Yield curve, rate delta, regime detection.
Shared by: Rate Delta page, Yield Curve page, NII engine.
"""

import os, json
import numpy as np
import pandas as pd
import requests
import yfinance as yf
from datetime import datetime, timedelta
from typing import Optional
from utils.io_utils import _atomic_json_write
from utils.api_cache import ttl_cache, invalidate_all as _cache_invalidate_all

_FRED_DEFAULT_KEY = os.environ.get("FRED_API_KEY", "")
FRED_BASE         = "https://api.stlouisfed.org/fred/series/observations"

CONFIG_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                           "data", "tcmb_config.json")


def _get_fred_key() -> str:
    """FRED API anahtarını config.json'dan veya hardcoded default'tan döndürür."""
    key = os.environ.get("FRED_API_KEY")
    if key:
        return key
    if os.path.exists(CONFIG_PATH):
        try:
            cfg = json.load(open(CONFIG_PATH))
            stored = cfg.get("fred_key")
            if stored:
                return stored
        except Exception:
            pass
    return _FRED_DEFAULT_KEY


def save_fred_key(key: str) -> None:
    """FRED API anahtarını config.json'a kaydeder."""
    os.makedirs(os.path.dirname(CONFIG_PATH), exist_ok=True)
    cfg = {}
    if os.path.exists(CONFIG_PATH):
        try:
            cfg = json.load(open(CONFIG_PATH))
        except Exception:
            pass
    cfg["fred_key"] = key
    _atomic_json_write(CONFIG_PATH, cfg)


def get_fred_status() -> dict:
    """FRED API anahtarı yapılandırma durumunu döndürür."""
    key = _get_fred_key()
    try:
        r = requests.get(FRED_BASE, params={
            "series_id": "DGS10",
            "api_key": key,
            "file_type": "json",
            "limit": 1,
            "sort_order": "desc",
        }, timeout=6)
        obs = r.json().get("observations", [])
        connected = len(obs) > 0
    except Exception:
        connected = False
    return {
        "configured": True,
        "key_preview": f"{key[:4]}***{key[-2:]}",
        "connected": connected,
    }


# Keep backwards-compat alias
FRED_API_KEY = _FRED_DEFAULT_KEY

TREASURY_SERIES = {
    "1Y":  "DGS1",    # Required for 1Y forward spread calculation
    "2Y":  "DGS2",
    "5Y":  "DGS5",
    "10Y": "DGS10",
    "20Y": "DGS20",
    "30Y": "DGS30",
}


def _fetch_fred_series(series_id: str, days: int = 400) -> pd.Series:
    start = (datetime.today() - timedelta(days=days)).strftime("%Y-%m-%d")
    try:
        r = requests.get(FRED_BASE, params={
            "series_id":    series_id,
            "api_key":      _get_fred_key(),
            "file_type":    "json",
            "observation_start": start,
        }, timeout=8)
        obs = r.json().get("observations", [])
        data = {}
        for o in obs:
            if o["value"] != ".":
                data[o["date"]] = float(o["value"])
        series = pd.Series(data)
        if not series.empty:
            try:
                from utils.data_health import mark_fetch
                mark_fetch("FRED", "live")
            except Exception:
                pass
        return series
    except Exception:
        try:
            from utils.data_health import mark_fetch
            mark_fetch("FRED", "fallback")
        except Exception:
            pass
        return pd.Series(dtype=float)


@ttl_cache(ttl=600, key="fred_rate_delta")
def fetch_rate_delta_data() -> dict:
    """
    Returns data for the 4-chart Rate Delta page:
      1. Rate delta heatmap (tenors x windows)
      2. 10Y-2Y spread time series + regime label
      3. TLT momentum/reversal signals
      4. PFF credit spread Z-score
    """
    # ── 1. Treasury series ────────────────────────────────────────────────
    series = {tenor: _fetch_fred_series(sid) for tenor, sid in TREASURY_SERIES.items()}

    # Align to common dates
    df = pd.DataFrame(series).dropna()

    # Rate deltas
    windows = [5, 10, 20]
    heatmap = {}
    for tenor in TREASURY_SERIES:
        heatmap[tenor] = {}
        for w in windows:
            if len(df) > w and tenor in df.columns:
                delta = df[tenor].iloc[-1] - df[tenor].iloc[-(w+1)]
                heatmap[tenor][f"{w}d"] = round(delta, 3)
            else:
                heatmap[tenor][f"{w}d"] = None

    # ── 2. Curve shape (10Y - 2Y) + Z-score + sigma bands ────────────────
    spread_10y_2y = None
    regime        = "Unknown"
    spread_series = []
    spread_z_stats = {}
    forward_spread = None

    if "10Y" in df.columns and "2Y" in df.columns:
        spread = (df["10Y"] - df["2Y"]).dropna()
        if spread.empty:
            # FRED returned no data for one or both tenors — skip spread calc
            recent_5d_10y, recent_5d_2y = 0, 0
        else:
            recent_5d_10y  = df["10Y"].diff(5).iloc[-1] if len(df) > 5 else 0
            recent_5d_2y   = df["2Y"].diff(5).iloc[-1]  if len(df) > 5 else 0
            spread_10y_2y  = round(spread.iloc[-1], 3)

        if   recent_5d_10y > 0 and recent_5d_2y > 0 and recent_5d_10y > recent_5d_2y:
            regime = "Bear Steepen"
        elif recent_5d_10y > 0 and recent_5d_2y > 0 and recent_5d_10y < recent_5d_2y:
            regime = "Bear Flatten"
        elif recent_5d_10y < 0 and recent_5d_2y < 0 and abs(recent_5d_10y) < abs(recent_5d_2y):
            regime = "Bull Steepen"
        elif recent_5d_10y < 0 and recent_5d_2y < 0 and abs(recent_5d_10y) > abs(recent_5d_2y):
            regime = "Bull Flatten"
        else:
            regime = "Mixed"

        # Rolling Z-score and sigma bands (252-day / 1-year window)
        rw = max(60, min(252, len(spread) - 1))
        roll_mean = spread.rolling(rw).mean()
        roll_std  = spread.rolling(rw).std().replace(0, np.nan)
        z_series  = (spread - roll_mean) / roll_std

        tail = spread.tail(504)
        spread_series = []
        for d in tail.index:
            def _f(s, dd): return round(float(s.loc[dd]), 3) if dd in s.index and not pd.isna(s.loc[dd]) else None
            spread_series.append({
                "date":  str(d.date() if hasattr(d, "date") else d),
                "value": round(float(spread.loc[d]), 3),
                "mean":  _f(roll_mean, d),
                "u1":    _f(roll_mean + roll_std, d),
                "l1":    _f(roll_mean - roll_std, d),
                "u2":    _f(roll_mean + 2*roll_std, d),
                "l2":    _f(roll_mean - 2*roll_std, d),
                "z":     _f(z_series, d),
            })

        # Current Z-score stats
        z_now = float(z_series.iloc[-1]) if not z_series.empty and not pd.isna(z_series.iloc[-1]) else None
        if z_now is not None:
            if z_now >  2.0: signal = "breakout_high"
            elif z_now < -2.0: signal = "breakout_low"
            elif abs(z_now) < 0.5: signal = "mean_reverting"
            else: signal = "watch"
        else:
            signal = "insufficient_data"

        spread_z_stats = {
            "z":          round(z_now, 3)                                if z_now   is not None else None,
            "mean":       round(float(roll_mean.iloc[-1]), 3)            if not pd.isna(roll_mean.iloc[-1]) else None,
            "std":        round(float(roll_std.iloc[-1]), 3)             if not pd.isna(roll_std.iloc[-1]) else None,
            "u1":         round(float((roll_mean + roll_std).iloc[-1]), 3)   if not pd.isna(roll_std.iloc[-1]) else None,
            "l1":         round(float((roll_mean - roll_std).iloc[-1]), 3)   if not pd.isna(roll_std.iloc[-1]) else None,
            "u2":         round(float((roll_mean + 2*roll_std).iloc[-1]), 3) if not pd.isna(roll_std.iloc[-1]) else None,
            "l2":         round(float((roll_mean - 2*roll_std).iloc[-1]), 3) if not pd.isna(roll_std.iloc[-1]) else None,
            "signal":     signal,
            "window":     rw,
        }

    # Forward 10Y-2Y spread (1Y horizon) — implied by spot curve
    try:
        r1  = float(series["1Y"].iloc[-1])  / 100 if "1Y"  in series and not series["1Y"].empty  else None
        r2  = float(series["2Y"].iloc[-1])  / 100 if "2Y"  in series and not series["2Y"].empty  else None
        r10 = float(series["10Y"].iloc[-1]) / 100 if "10Y" in series and not series["10Y"].empty else None
        if r1 and r2 and r10 and r1 > 0:
            # 1Y forward 1Y rate: (1+R2)^2 / (1+R1) - 1
            fwd_1y1y = ((1 + r2)**2 / (1 + r1)) - 1
            # 1Y forward 9Y rate: ((1+R10)^10 / (1+R1))^(1/9) - 1
            fwd_1y9y = ((1 + r10)**10 / (1 + r1))**(1.0/9) - 1
            forward_spread = round((fwd_1y9y - fwd_1y1y) * 100, 3)
    except Exception:
        pass

    # ── 3. TLT momentum signals ───────────────────────────────────────────
    tlt_data    = []
    tlt_signals = []
    try:
        tlt = yf.download("TLT", period="1y", progress=False)["Close"].squeeze()
        if not tlt.empty:
            ma10 = tlt.rolling(10).mean()
            ma20 = tlt.rolling(20).mean()
            high20 = tlt.rolling(20).max()
            low20  = tlt.rolling(20).min()
            for i in range(1, len(tlt)):
                price = float(tlt.iloc[i])
                date  = str(tlt.index[i].date())
                tlt_data.append({"date": date, "price": round(price, 2)})
                # Red triangle: was at 20d high, now crossed below 10d MA
                if float(tlt.iloc[i-1]) >= float(high20.iloc[i-1]) and price < float(ma10.iloc[i]):
                    tlt_signals.append({"date": date, "price": round(price, 2), "type": "red"})
                # Green triangle: was at 20d low, now crossed above 10d MA
                elif float(tlt.iloc[i-1]) <= float(low20.iloc[i-1]) and price > float(ma10.iloc[i]):
                    tlt_signals.append({"date": date, "price": round(price, 2), "type": "green"})
    except Exception:
        pass

    # ── 4. PFF credit spread Z-score ─────────────────────────────────────
    spread_zscore_series = []
    current_z            = None
    try:
        pff = yf.download("PFF", period="2y", progress=False)
        if not pff.empty:
            price    = pff["Close"].squeeze()
            dividends = pff["Close"].squeeze().pct_change().rolling(252).sum() * 0  # placeholder
            # Approximate PFF yield from trailing dividend / price
            hist      = yf.Ticker("PFF").dividends
            if not hist.empty:
                annual_div = hist.last("365D").sum()
                pff_yield  = annual_div / float(price.iloc[-1]) * 100
            else:
                pff_yield  = 6.0  # fallback

            ten_y_series = _fetch_fred_series("DGS10")
            if not ten_y_series.empty:
                ten_y_now = float(ten_y_series.iloc[-1])
                spread_val = pff_yield - ten_y_now
                # Build historical spread (simplified: use constant pff_yield)
                # For a real build: use historical PFF yield series
                spread_hist = pd.Series(
                    {d: pff_yield - float(v) for d, v in ten_y_series.tail(504).items()}
                )
                roll_mean = spread_hist.rolling(252).mean()
                roll_std  = spread_hist.rolling(252).std()
                z_series  = (spread_hist - roll_mean) / roll_std
                current_z = round(float(z_series.iloc[-1]), 3) if not z_series.empty else None
                spread_zscore_series = [
                    {"date": str(d), "z": round(float(z), 3),
                     "spread": round(float(s), 3)}
                    for (d, z), s in zip(z_series.dropna().tail(252).items(),
                                         spread_hist.tail(252))
                ]
    except Exception:
        pass

    # ── Current rates snapshot ────────────────────────────────────────────
    current_rates = {t: round(float(s.iloc[-1]), 3) if not s.empty else None
                     for t, s in series.items()}

    return {
        "heatmap":         heatmap,
        "current_rates":   current_rates,
        "spread_10y_2y":   spread_10y_2y,
        "regime":          regime,
        "spread_series":   spread_series,      # now includes mean/u1/l1/u2/l2/z per point
        "spread_z_stats":  spread_z_stats,     # current Z-score snapshot
        "forward_spread":  forward_spread,     # 1Y forward 10Y-2Y implied spread
        "tlt_data":        tlt_data,
        "tlt_signals":     tlt_signals,
        "pff_z":           current_z,
        "spread_z_series": spread_zscore_series,
    }


@ttl_cache(ttl=600, key="fred_yield_curve")
def fetch_yield_curve_data() -> dict:
    """
    Returns live yield curve + Nelson-Siegel fit + regime classification.
    """
    tenors_years = {"1M": 1/12, "3M": 0.25, "6M": 0.5, "1Y": 1,
                    "2Y": 2, "5Y": 5, "10Y": 10, "20Y": 20, "30Y": 30}
    fred_map     = {"1M": "DGS1MO", "3M": "DGS3MO", "6M": "DGS6MO",
                    "1Y": "DGS1",   "2Y": "DGS2",   "5Y": "DGS5",
                    "10Y": "DGS10", "20Y": "DGS20",  "30Y": "DGS30"}

    rates = {}
    for tenor, sid in fred_map.items():
        s = _fetch_fred_series(sid, days=5)
        if not s.empty:
            rates[tenor] = round(float(s.iloc[-1]), 3)

    # Nelson-Siegel fit
    ns_fit = _nelson_siegel_fit(tenors_years, rates)

    # Regime
    regime = "Unknown"
    if "2Y" in rates and "10Y" in rates:
        spread = rates["10Y"] - rates["2Y"]
        if   spread > 0.5:  regime = "Steep (Expansion signal)"
        elif spread > 0.0:  regime = "Normal (Moderate)"
        elif spread > -0.5: regime = "Flat / Mild Inversion"
        else:               regime = "Inverted (Recession signal)"

    return {
        "rates":   rates,
        "ns_fit":  ns_fit,
        "regime":  regime,
        "spread_10y_2y": round(rates.get("10Y", 0) - rates.get("2Y", 0), 3),
    }


def _nelson_siegel_fit(tenors_years: dict, rates: dict) -> dict:
    """
    Nelson-Siegel model: y(t) = β0 + β1*(1-e^(-t/τ))/(t/τ) + β2*((1-e^(-t/τ))/(t/τ) - e^(-t/τ))
    Returns fitted rates at standard tenors.
    Simple grid-search calibration.
    """
    try:
        from scipy.optimize import minimize

        obs_t = []
        obs_y = []
        for tenor, years in tenors_years.items():
            if tenor in rates and rates[tenor] is not None:
                obs_t.append(years)
                obs_y.append(rates[tenor] / 100.0)

        if len(obs_t) < 4:
            return {}

        obs_t = np.array(obs_t)
        obs_y = np.array(obs_y)

        def ns_yield(t, b0, b1, b2, tau):
            x = t / tau
            load1 = (1 - np.exp(-x)) / x
            load2 = load1 - np.exp(-x)
            return b0 + b1 * load1 + b2 * load2

        def loss(params):
            b0, b1, b2, tau = params
            if tau <= 0 or b0 <= 0:
                return 1e10
            y_hat = ns_yield(obs_t, b0, b1, b2, tau)
            return np.sum((y_hat - obs_y) ** 2)

        res = minimize(loss, [0.04, -0.02, 0.01, 2.0],
                       method='Nelder-Mead',
                       options={'maxiter': 5000, 'xatol': 1e-6})
        b0, b1, b2, tau = res.x

        fit_tenors = [0.25, 0.5, 1, 2, 3, 5, 7, 10, 20, 30]
        fitted = {str(t) + "Y": round(ns_yield(t, b0, b1, b2, tau) * 100, 3)
                  for t in fit_tenors}
        return {"params": {"beta0": round(b0*100,3), "beta1": round(b1*100,3),
                            "beta2": round(b2*100,3), "tau": round(tau,3)},
                "fitted": fitted}
    except Exception:
        return {}


# ══════════════════════════════════════════════════════════════════════════════
# TCMB / TURKISH RATES
# ══════════════════════════════════════════════════════════════════════════════
# FRED does not carry Turkish rates directly.
# We use the following public sources:
#   1. TCMB EVDS API  — policy rate, overnight rates (free, key embedded)
#   2. Investing.com  — BIST bond yields via yfinance proxies where available
#   3. Hardcoded TCMB policy rate as reliable fallback
#
# Turkish benchmarks mapped:
#   Policy rate (1-wk repo) → TCMB EVDS: TP.MB.S.O01  (or fallback 45.0%)
#   TLREF (overnight)       → TCMB EVDS: TP.MB.S.TLREF
#   TR 2Y govt bond         → yfinance: ^TRY2YT=RR (not always available)
#   USD/TRY                 → exchangerate-api / open.er-api

TCMB_EVDS_KEY  = os.environ.get("TCMB_EVDS_KEY", "")   # set in .env
TCMB_EVDS_BASE = "https://evds2.tcmb.gov.tr/service/evds"

# TCMB EVDS seri kodları
# Dokümantasyon: https://evds2.tcmb.gov.tr/help/videos/EVDS_Web_Servis_Kilavuzu.pdf
TCMB_SERIES = {
    "policy_rate": "TP.MB.S.O01",      # 1-haftalık repo (politika faizi)
    "tlref":       "TP.MB.S.TLREF",    # TLREF gecelik referans faiz
    "on_lending":  "TP.MB.S.FAIZDH",   # Gecelik borç verme faizi
    "on_borrow":   "TP.MB.S.FAIZDH2",  # Gecelik borç alma faizi
    "usdtry":      "TP.DK.USD.A.YTL",  # USD/TRY
    "eurtry":      "TP.DK.EUR.A.YTL",  # EUR/TRY
    "bond_2y":     "TP.TG2.Y02",       # 2Y Hazine tahvili
    "bond_10y":    "TP.TG2.Y10",       # 10Y Hazine tahvili
}

# TR Getiri Eğrisi — tam vade yapısı (EVDS seri kodları)
TR_YIELD_CURVE_SERIES = {
    "ON":   ("TP.MB.S.TLREF",  0.003),   # Gecelik — TLREF
    "1M":   ("TP.MB.S.O01",    0.083),   # 1 ay — politika faizine yakın
    "3M":   ("TP.MB.S.O01",    0.250),   # 3 ay — politika faizine yakın
    "1Y":   ("TP.TG2.Y01",     1.0),     # 1Y Hazine
    "2Y":   ("TP.TG2.Y02",     2.0),     # 2Y Hazine
    "3Y":   ("TP.TG2.Y03",     3.0),     # 3Y Hazine
    "5Y":   ("TP.TG2.Y05",     5.0),     # 5Y Hazine
    "7Y":   ("TP.TG2.Y07",     7.0),     # 7Y Hazine
    "10Y":  ("TP.TG2.Y10",     10.0),    # 10Y Hazine
}

# Fallback değerleri (EVDS erişilemezse kullanılır — periyodik güncelle)
TCMB_FALLBACKS = {
    "policy_rate": 42.50,   # Nisan 2026 itibarıyla
    "tlref":       43.20,
    "on_lending":  44.00,
    "on_borrow":   41.00,
    "usdtry":      44.85,
    "eurtry":      52.10,
    "bond_2y":     39.86,
    "bond_10y":    32.45,
}

# TR Getiri eğrisi fallback (Nisan 2026 — inverted curve)
TR_YIELD_FALLBACKS = {
    "ON":   43.20,
    "1M":   42.80,
    "3M":   42.50,
    "1Y":   40.20,
    "2Y":   39.86,
    "3Y":   37.50,
    "5Y":   35.20,
    "7Y":   33.80,
    "10Y":  32.45,
}

# ──────────────────────────────────────────────────────────────────────────────
# TCMB PPK Karar Takvimi — Politika Faizi Tarihi (haftalık fallback serisi)
# Kaynak: TCMB Para Politikası Kurulu toplantı kararları (kamuya açık)
# EVDS erişimi olmadığında bu adım fonksiyon rate delta heatmap'ı besler.
# Her MPK kararı ertesi haftasına etki eder; aradaki haftalar önceki karar.
# ──────────────────────────────────────────────────────────────────────────────
TR_POLICY_HISTORY = {
    # 2023 — Ortodoks para politikasına dönüş (Mayıs 2023 sonrası)
    "2023-06-22": 15.00,
    "2023-07-20": 17.50,
    "2023-08-24": 25.00,
    "2023-09-21": 30.00,
    "2023-10-26": 35.00,
    "2023-11-23": 40.00,
    "2023-12-21": 42.50,
    # 2024 — Sıkılaşma devam + zirve
    "2024-01-25": 45.00,
    "2024-02-22": 45.00,
    "2024-03-21": 50.00,   # Zirve — politika faizi dorukta
    "2024-04-25": 50.00,
    "2024-05-23": 50.00,
    "2024-06-27": 50.00,
    "2024-07-25": 50.00,
    "2024-08-22": 50.00,
    # 2024 — Gevşeme döngüsü başlangıcı
    "2024-09-19": 47.50,
    "2024-10-17": 47.50,
    "2024-11-21": 46.00,
    "2024-12-26": 45.00,
    # 2025 — Kademeli faiz indirimleri
    "2025-01-23": 44.50,
    "2025-02-20": 44.00,
    "2025-03-06": 43.50,   # Olağanüstü toplantı (TRY baskısı)
    "2025-04-17": 43.00,
    "2025-05-22": 43.00,
    "2025-06-19": 42.50,
    "2025-07-17": 42.50,
    "2025-08-21": 42.50,
    "2025-09-18": 42.50,
    "2025-10-23": 42.50,
    "2025-11-20": 42.50,
    "2025-12-18": 42.50,
    # 2026 — Mevcut seyir
    "2026-01-22": 42.50,
    "2026-02-19": 42.50,
    "2026-03-19": 42.50,
    "2026-04-17": 42.50,
}


def _build_tr_policy_series() -> pd.Series:
    """
    TCMB karar takviminden haftalık politika faizi serisi oluşturur.
    MPK kararları arasındaki haftalar önceki kararın faizini taşır (adım fonksiyon).
    """
    decisions = sorted(TR_POLICY_HISTORY.items())
    start = pd.Timestamp(decisions[0][0])
    end   = pd.Timestamp.today() + pd.Timedelta(days=7)
    idx   = pd.date_range(start=start, end=end, freq="W")

    decision_dates = [pd.Timestamp(d) for d, _ in decisions]
    decision_rates = [r for _, r in decisions]

    rates = []
    for dt in idx:
        rate = decision_rates[0]
        for i, dd in enumerate(decision_dates):
            if dd <= dt:
                rate = decision_rates[i]
            else:
                break
        rates.append(rate)
    return pd.Series(rates, index=idx)


def _build_tr_tlref_series(policy_series: pd.Series) -> pd.Series:
    """
    TLREF serisini politika faizinden türetir.
    Tarihsel gözlem: yüksek faiz döneminde spread ~70bps, normal dönemde ~150bps.
    """
    def _premium(rate: float) -> float:
        if rate >= 45: return 0.50   # Zirve dönem — sıkışık likidite spread'i
        if rate >= 40: return 0.70
        if rate >= 25: return 1.20
        return 2.00

    vals = [v + _premium(v) for v in policy_series.values]
    return pd.Series(vals, index=policy_series.index)


def _fetch_tcmb_series(series_id: str, fallback: Optional[float] = None) -> Optional[float]:
    """
    TCMB EVDS'den tek bir seri değeri çeker.
    Doğru endpoint: /service/evds/data?series=...&startDate=...&type=json&key=...
    Frekans 5 = aylık, 1 = haftalık, 0 = günlük
    """
    try:
        from datetime import datetime, timedelta
        start = (datetime.today() - timedelta(days=60)).strftime("%d-%m-%Y")
        r = requests.get(
            f"{TCMB_EVDS_BASE}/data",
            params={
                "series":    series_id,
                "startDate": start,
                "type":      "json",
                "key":       TCMB_EVDS_KEY,
                "frequency": "1",          # haftalık
                "aggregationTypes": "avg",
                "formulas":  "0",
            },
            timeout=8
        )
        if r.status_code != 200:
            return fallback
        items = r.json().get("items", [])
        # Son geçerli değeri bul (geriye doğru tara)
        for item in reversed(items):
            v = item.get(series_id) or item.get(series_id.split(".")[-1])
            if v and str(v).strip() not in ("", "ND", "null", "None"):
                return float(str(v).replace(",", "."))
    except Exception:
        pass
    return fallback


def _fetch_tr_yields_yfinance() -> dict:
    """
    Try to get Turkish govt bond yields via yfinance.
    ^TRY2YT=RR / ^TRY5YT=RR / ^TRY10YT=RR are intermittently available on
    Yahoo Finance and frequently return 404 (delisted / no data).
    All failures are silently swallowed — stderr is suppressed so the gunicorn
    log stays clean.
    """
    import io, contextlib
    yields = {}
    tickers_map = {
        "2Y":  "^TRY2YT=RR",
        "5Y":  "^TRY5YT=RR",
        "10Y": "^TRY10YT=RR",
    }
    for tenor, yticker in tickers_map.items():
        try:
            # Redirect both stdout and stderr to discard noisy "no data" warnings
            _sink = io.StringIO()
            with contextlib.redirect_stdout(_sink), contextlib.redirect_stderr(_sink):
                df = yf.download(yticker, period="5d", progress=False,
                                 auto_adjust=True, raise_errors=False)
            if df is not None and not df.empty:
                close_col = "Close" if "Close" in df.columns else df.columns[0]
                v = float(df[close_col].dropna().iloc[-1])
                if v > 0:
                    yields[tenor] = round(v, 3)
        except Exception:
            pass
    return yields


@ttl_cache(ttl=600, key="evds_tr_yield_curve")
def fetch_tr_yield_curve() -> dict:
    """
    Türkiye tam getiri eğrisini çeker — ON, 1M, 3M, 1Y, 2Y, 3Y, 5Y, 7Y, 10Y.
    Öncelik: TCMB EVDS → TR_YIELD_FALLBACKS

    Döndürür: {
        "curve": [{"tenor": "2Y", "years": 2.0, "yield": 39.86}, ...],
        "spread_10y_2y": float,   # 10Y - 2Y spread (bps)
        "curve_shape": str,        # "Inverted" | "Normal" | "Flat" | "Humped"
        "cds_5y": float,           # Placeholder — manuel girilir
        "source": str,
    }
    """
    curve_points = []
    evds_success = 0

    for tenor, (series_id, years) in TR_YIELD_CURVE_SERIES.items():
        # Kısa uç için politika/TLREF kullan
        if tenor in ("ON", "1M", "3M"):
            fallback = TR_YIELD_FALLBACKS[tenor]
            val = _fetch_tcmb_series(series_id, fallback)
        else:
            val = _fetch_tcmb_series(series_id, None)
            if val is None:
                val = TR_YIELD_FALLBACKS.get(tenor)
            else:
                evds_success += 1

        if val is not None:
            curve_points.append({
                "tenor": tenor,
                "years": years,
                "yield": round(float(val), 3),
            })

    # Sırala
    curve_points.sort(key=lambda x: x["years"])

    # 10Y - 2Y spread
    y2  = next((p["yield"] for p in curve_points if p["tenor"] == "2Y"),  None)
    y10 = next((p["yield"] for p in curve_points if p["tenor"] == "10Y"), None)
    spread_bps = round((y10 - y2) * 100, 1) if (y2 and y10) else None

    # Eğri şekli
    if spread_bps is not None:
        if   spread_bps < -200: curve_shape = "Deeply Inverted"
        elif spread_bps < 0:    curve_shape = "Inverted"
        elif spread_bps < 50:   curve_shape = "Flat"
        elif spread_bps < 150:  curve_shape = "Normal"
        else:                    curve_shape = "Steep"
    else:
        curve_shape = "Unknown"

    # Eğri ALM yorumu
    alm_note = {
        "Deeply Inverted": "Piyasa agresif faiz indirimi bekliyor. Liability-sensitive bankalar için kısa vade maliyet baskısı yüksek.",
        "Inverted":        "Kısa faiz > uzun faiz. TCMB'nin faiz indirim döngüsüne girdiğine işaret. NII için fırsat penceresi.",
        "Flat":            "Eğri düz. Faiz belirsizliği yüksek. IRS hedge'i değerlendir.",
        "Normal":          "Normal eğri. Liability-sensitive pozisyon mantıklı.",
        "Steep":           "Dik eğri. Uzun vadeli varlık cazip ama duration riski yüksek.",
    }.get(curve_shape, "")

    return {
        "curve":          curve_points,
        "spread_10y_2y":  spread_bps,
        "spread_label":   f"{spread_bps:+.0f}bps" if spread_bps else "N/A",
        "curve_shape":    curve_shape,
        "alm_note":       alm_note,
        "evds_points":    evds_success,
        "total_points":   len(curve_points),
        "source":         f"TCMB EVDS + Fallbacks ({evds_success} canlı nokta)",
        "fetched_at":     datetime.now().strftime("%Y-%m-%d %H:%M"),
    }


@ttl_cache(ttl=600, key="evds_tr_rates")
def fetch_tr_rates() -> dict:
    """
    Tüm Türkiye faiz ve kur verilerini çeker.
    Öncelik: TCMB EVDS → yfinance → TCMB_FALLBACKS sabit değerleri

    Döndürür: policy_rate, tlref, on_lending, on_borrow,
              usdtry, eurtry, bond_2y, bond_10y, fetched_at
    """
    from datetime import datetime

    # TCMB EVDS — tüm seriler fallback değerleriyle birlikte
    policy_rate = _fetch_tcmb_series("TP.MB.S.O01",   TCMB_FALLBACKS["policy_rate"])
    tlref       = _fetch_tcmb_series("TP.MB.S.TLREF",  TCMB_FALLBACKS["tlref"])
    on_lending  = _fetch_tcmb_series("TP.MB.S.FAIZDH", TCMB_FALLBACKS["on_lending"])
    on_borrow   = _fetch_tcmb_series("TP.MB.S.FAIZDH2",TCMB_FALLBACKS["on_borrow"])

    # FX — önce TCMB EVDS, yoksa open.er-api
    usdtry = _fetch_tcmb_series("TP.DK.USD.A.YTL", None)
    eurtry = _fetch_tcmb_series("TP.DK.EUR.A.YTL", None)
    if usdtry is None:
        try:
            r = requests.get("https://open.er-api.com/v6/latest/USD", timeout=5)
            usdtry = round(r.json().get("rates",{}).get("TRY",0), 4) or None
        except Exception: pass
    if eurtry is None:
        try:
            r = requests.get("https://open.er-api.com/v6/latest/EUR", timeout=5)
            eurtry = round(r.json().get("rates",{}).get("TRY",0), 4) or None
        except Exception: pass

    # Hazine tahvil getirileri — önce EVDS, yoksa yfinance
    bond_2y  = _fetch_tcmb_series("TP.TG2.Y02", None)
    bond_10y = _fetch_tcmb_series("TP.TG2.Y10", None)
    if bond_2y is None or bond_10y is None:
        yf_yields = _fetch_tr_yields_yfinance()
        if bond_2y  is None: bond_2y  = yf_yields.get("2Y")
        if bond_10y is None: bond_10y = yf_yields.get("10Y")

    # on_rate backward compat
    on_rate = on_lending

    return {
        "policy_rate":  round(policy_rate, 2) if policy_rate else None,
        "tlref":        round(tlref, 2)        if tlref       else None,
        "on_rate":      round(on_rate, 2)      if on_rate     else None,
        "on_lending":   round(on_lending, 2)   if on_lending  else None,
        "on_borrow":    round(on_borrow, 2)    if on_borrow   else None,
        "tr_yields":    {
            k: round(v, 3) for k, v in
            {"2Y": bond_2y, "10Y": bond_10y}.items() if v is not None
        },
        "usdtry":       round(usdtry, 4) if usdtry else None,
        "eurtry":       round(eurtry, 4) if eurtry else None,
        "source":       "TCMB EVDS + open.er-api",
        "fetched_at":   datetime.now().strftime("%Y-%m-%d %H:%M"),
        "note":         "EVDS seri: TP.MB.S.O01 (repo), TP.MB.S.TLREF, TP.DK.USD.A.YTL, TP.TG2.Y02/Y10",
    }

# ══════════════════════════════════════════════════════════════════════════════
# TR RATE ANALYTICS — heatmap, spread series, regime
# ══════════════════════════════════════════════════════════════════════════════

# TCMB EVDS — tarihsel seri çekimi için seri kodları
# Policy rate (haftalık) — tarihsel pencere için daha uzun dönem çekiyoruz
TR_HISTORICAL_SERIES = {
    "policy_rate": "TP.MB.S.O01",
    "tlref":       "TP.MB.S.TLREF",
    "on_lending":  "TP.MB.S.FAIZDH",
}


def _fetch_tcmb_series_history(series_id: str, days: int = 400) -> pd.Series:
    """
    TCMB EVDS'den tarihsel seri çeker, pd.Series olarak döner.
    EVDS erişilemezse boş Series döner.
    """
    try:
        from datetime import datetime, timedelta
        start = (datetime.today() - timedelta(days=days)).strftime("%d-%m-%Y")
        r = requests.get(
            f"{TCMB_EVDS_BASE}/data",
            params={
                "series":           series_id,
                "startDate":        start,
                "type":             "json",
                "key":              TCMB_EVDS_KEY,
                "frequency":        "1",      # haftalık
                "aggregationTypes": "avg",
                "formulas":         "0",
            },
            timeout=10
        )
        if r.status_code != 200:
            return pd.Series(dtype=float)
        items = r.json().get("items", [])
        data = {}
        for item in items:
            date_str = item.get("Tarih") or item.get("DATE") or item.get("date")
            val      = item.get(series_id) or item.get(series_id.split(".")[-1])
            if date_str and val and str(val).strip() not in ("", "ND", "null", "None"):
                try:
                    # EVDS date format: "YYYY-MM" (aylık) veya "DD-MM-YYYY" (günlük)
                    if len(str(date_str)) <= 7:
                        dt = pd.to_datetime(str(date_str), format="%Y-%m")
                    else:
                        dt = pd.to_datetime(str(date_str), dayfirst=True)
                    data[dt] = float(str(val).replace(",", "."))
                except Exception:
                    pass
        return pd.Series(data).sort_index()
    except Exception:
        return pd.Series(dtype=float)


def _tr_regime(policy_series: pd.Series, tlref_series: pd.Series) -> dict:
    """
    TR için faiz rejimi tespit eder.
    
    TR'de eğri şekli US gibi tenor-bazlı değil; bunun yerine:
      - Politika faizi yönü (5 veri noktası değişim)
      - TLREF - Politika faizi farkı (kısa vade baskısı)
      - Politika faizinin seviyesi (yüksek/düşük)
    
    Rejimler:
      Sıkılaşma   — politika faizi yükseliyor
      Gevşeme     — politika faizi düşüyor
      Sabit       — politika faizi değişmiyor
      Baskı       — TLREF > politika faizi (piyasa kısa tarafta geride)
    """
    if policy_series.empty:
        return {"regime": "Bilinmiyor", "regime_en": "Unknown",
                "direction": None, "tlref_premium": None}

    direction = None
    if len(policy_series) >= 3:
        recent = policy_series.iloc[-1]
        past   = policy_series.iloc[-3]
        if   recent > past + 0.1:  direction = "up"
        elif recent < past - 0.1:  direction = "down"
        else:                      direction = "flat"

    tlref_premium = None
    if not tlref_series.empty and not policy_series.empty:
        tlref_premium = round(float(tlref_series.iloc[-1]) - float(policy_series.iloc[-1]), 2)

    if direction == "up":
        regime    = "Sıkılaşma"
        regime_en = "Tightening"
    elif direction == "down":
        regime    = "Gevşeme"
        regime_en = "Easing"
    elif tlref_premium and abs(tlref_premium) > 0.5:
        regime    = "Piyasa Baskısı"
        regime_en = "Market Pressure"
    else:
        regime    = "Sabit"
        regime_en = "On Hold"

    return {
        "regime":         regime,
        "regime_en":      regime_en,
        "direction":      direction,
        "tlref_premium":  tlref_premium,
        "policy_current": round(float(policy_series.iloc[-1]), 2) if not policy_series.empty else None,
        "tlref_current":  round(float(tlref_series.iloc[-1]),  2) if not tlref_series.empty  else None,
    }


def _tr_rate_delta_heatmap(series_dict: dict) -> dict:
    """
    TR faiz serilerinden rate delta heatmap üretir.
    TCMB aylık toplantı yapar — anlamlı lookback pencereleri:
      1m (~4 hafta)  → son PPK kararı sonrası değişim
      3m (~13 hafta) → çeyreklik delta
      6m (~26 hafta) → yarıyıllık delta
      1y (~52 hafta) → yıllık faiz değişimi
    """
    windows = {"1m": 4, "3m": 13, "6m": 26, "1y": 52}
    heatmap = {}
    for label, s in series_dict.items():
        heatmap[label] = {}
        for key, w in windows.items():
            if len(s) > w:
                delta = float(s.iloc[-1]) - float(s.iloc[-(w+1)])
                heatmap[label][key] = round(delta, 2)
            else:
                heatmap[label][key] = None
    return heatmap


@ttl_cache(ttl=600, key="evds_tr_analytics")
def fetch_tr_analytics() -> dict:
    """
    TR için tam analitik paketi döner:
      - Güncel faiz değerleri (policy, tlref, on_lending, fx)
      - Tarihsel seri (spread grafiği için)
      - Rate delta heatmap (policy/tlref/on × 5d/10d/20d)
      - Faiz rejimi (sıkılaşma/gevşeme/sabit)
      - TLREF - policy spread serisi
    """
    from datetime import datetime

    # Güncel değerler
    policy_rate = _fetch_tcmb_series("TP.MB.S.O01",    TCMB_FALLBACKS["policy_rate"])
    tlref       = _fetch_tcmb_series("TP.MB.S.TLREF",  TCMB_FALLBACKS["tlref"])
    on_lending  = _fetch_tcmb_series("TP.MB.S.FAIZDH", TCMB_FALLBACKS["on_lending"])
    on_borrow   = _fetch_tcmb_series("TP.MB.S.FAIZDH2",TCMB_FALLBACKS["on_borrow"])
    usdtry      = _fetch_tcmb_series("TP.DK.USD.A.YTL", None)
    eurtry      = _fetch_tcmb_series("TP.DK.EUR.A.YTL", None)

    # FX fallback
    if usdtry is None:
        try:
            r = requests.get("https://open.er-api.com/v6/latest/USD", timeout=5)
            usdtry = round(r.json().get("rates", {}).get("TRY", 0), 4) or None
        except Exception: pass
    if eurtry is None:
        try:
            r = requests.get("https://open.er-api.com/v6/latest/EUR", timeout=5)
            eurtry = round(r.json().get("rates", {}).get("TRY", 0), 4) or None
        except Exception: pass

    # Hazine tahvil getirileri
    bond_2y  = _fetch_tcmb_series("TP.TG2.Y02", None)
    bond_10y = _fetch_tcmb_series("TP.TG2.Y10", None)
    if bond_2y is None or bond_10y is None:
        yf_yields = _fetch_tr_yields_yfinance()
        if bond_2y  is None: bond_2y  = yf_yields.get("2Y")
        if bond_10y is None: bond_10y = yf_yields.get("10Y")

    # Tarihsel seriler (heatmap + spread grafiği için)
    policy_hist = _fetch_tcmb_series_history("TP.MB.S.O01",    days=400)
    tlref_hist  = _fetch_tcmb_series_history("TP.MB.S.TLREF",  days=400)
    on_hist     = _fetch_tcmb_series_history("TP.MB.S.FAIZDH", days=400)
    _evds_hist_live = not policy_hist.empty  # kaynağı raporlamak için

    # Eğer EVDS tarihsel seri getiremediyse — TCMB karar takvimi fallback'i kullan.
    # Sabit nokta yerine gerçek MPK kararlarından oluşturulan adım fonksiyon serisi:
    # 5d/10d/20d delta'lar anlamlı değerler üretir (kararlar arası = sıfır, karar haftaları ≠ 0).
    if policy_hist.empty:
        policy_hist = _build_tr_policy_series()
    if tlref_hist.empty:
        tlref_hist = _build_tr_tlref_series(policy_hist)
    if on_hist.empty and on_lending:
        # ON borç verme: politika + ~150bps (tarihsel ortalama koridor üst bandı)
        on_hist = policy_hist + 1.50

    # Rate delta heatmap
    series_for_heatmap = {}
    if not policy_hist.empty: series_for_heatmap["Politika Faizi"] = policy_hist
    if not tlref_hist.empty:  series_for_heatmap["TLREF"]          = tlref_hist
    if not on_hist.empty:     series_for_heatmap["ON Borç Verme"]  = on_hist
    heatmap = _tr_rate_delta_heatmap(series_for_heatmap)

    # Current values per heatmap label
    current_tr = {}
    for label, s in series_for_heatmap.items():
        if not s.empty:
            current_tr[label] = round(float(s.iloc[-1]), 2)

    # Spread serisi: TLREF − Politika faizi
    spread_series = []
    try:
        if not policy_hist.empty and not tlref_hist.empty:
            # Normalize her ikisini de aynı haftalık frekansta resample et
            p_w = policy_hist.resample("W").last().dropna()
            t_w = tlref_hist.resample("W").last().dropna()
            common = p_w.index.intersection(t_w.index)
            if len(common) >= 2:
                sp = t_w.loc[common] - p_w.loc[common]
                spread_series = [
                    {"date": str(dt.date()), "value": round(float(v), 3)}
                    for dt, v in sp.tail(104).items()
                ]
            elif policy_rate and tlref:
                # Synthetics intersection başarısız — tek nokta göster
                spread_series = [{"date": str(pd.Timestamp.today().date()),
                                   "value": round(tlref - policy_rate, 3)}]
    except Exception:
        if policy_rate and tlref:
            spread_series = [{"date": str(pd.Timestamp.today().date()),
                               "value": round(tlref - policy_rate, 3)}]

    # Policy faizi ve TLREF tarihsel serileri (grafik için)
    def _series_to_list(s, n=104):
        try:
            return [{"date": str(dt.date()), "value": round(float(v), 2)}
                    for dt, v in s.tail(n).items()]
        except Exception:
            return []

    policy_series_out = _series_to_list(policy_hist) if not policy_hist.empty else []
    tlref_series_out  = _series_to_list(tlref_hist)  if not tlref_hist.empty  else []

    # Rejim
    regime_data = _tr_regime(policy_hist, tlref_hist)

    return {
        # Güncel değerler
        "policy_rate":    round(policy_rate, 2) if policy_rate else None,
        "tlref":          round(tlref, 2)        if tlref       else None,
        "on_lending":     round(on_lending, 2)   if on_lending  else None,
        "on_borrow":      round(on_borrow, 2)    if on_borrow   else None,
        "usdtry":         round(usdtry, 4)       if usdtry      else None,
        "eurtry":         round(eurtry, 4)       if eurtry      else None,
        "bond_2y":        round(bond_2y, 3)      if bond_2y     else None,
        "bond_10y":       round(bond_10y, 3)     if bond_10y    else None,
        # Analitik
        "heatmap":        heatmap,
        "current_rates":  current_tr,
        "regime":         regime_data,
        "spread_series":  spread_series,      # TLREF - policy spread (tarihsel)
        "policy_series":  policy_series_out,  # policy faizi tarihsel
        "tlref_series":   tlref_series_out,   # TLREF tarihsel
        # Meta
        "fetched_at":     datetime.now().strftime("%Y-%m-%d %H:%M"),
        "source":         "TCMB EVDS (canlı)" if _evds_hist_live else "TCMB PPK Karar Takvimi (fallback)",
        "evds_live":      _evds_hist_live,
    }

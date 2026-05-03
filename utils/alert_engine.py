"""
Bek Rate Desk — Alert Engine (Sprint 1)
========================================
Monitors ALM metrics against regulatory and internal thresholds.

Alert types:
  LCR / NSFR breach | NII shock | Duration gap | EVE (IRRBB) | FX NOP | Rate delta
Alerts stored in data/alerts.json.
Optional email dispatch via smtplib.
"""

import json, os, smtplib
from datetime import datetime
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart

DATA_DIR    = os.path.join(os.path.dirname(os.path.dirname(__file__)), "data")
ALERTS_FILE = os.path.join(DATA_DIR, "alerts.json")

DEFAULT_THRESHOLDS = {
    "lcr_min":                110.0,
    "nsfr_min":               110.0,
    "nii_shock_max_pct":       15.0,
    "duration_gap_max":         3.0,
    "eve_shock_max_pct_tier1": 15.0,
    "fx_nop_max_pct_capital":  20.0,
    "rate_delta_alert_bps":    25.0,
}

SEVERITY = {
    "critical": {"color": "#ef4444", "icon": "🔴"},
    "warning":  {"color": "#f59e0b", "icon": "🟡"},
    "info":     {"color": "#2563eb", "icon": "🔵"},
}


def evaluate_alerts(data: dict) -> dict:
    metrics    = data.get("metrics",    {})
    thresholds = {**DEFAULT_THRESHOLDS, **data.get("thresholds", {})}
    alerts     = []

    def add(aid, title, severity, value, threshold, unit, desc, rec):
        alerts.append({
            "id": aid, "title": title, "severity": severity,
            "value": round(float(value), 2) if value is not None else None,
            "threshold": threshold, "unit": unit,
            "description": desc, "recommendation": rec,
            "timestamp": datetime.now().isoformat(), "acknowledged": False,
        })

    # LCR
    lcr = metrics.get("lcr")
    if lcr is not None:
        if lcr < 100:
            add("lcr_reg", "LCR REGULATORY BREACH", "critical", lcr, 100, "%",
                f"LCR {lcr:.1f}% is below the 100% regulatory minimum.",
                "Immediately acquire HQLA or reduce 30-day outflows. Consider central bank repo.")
        elif lcr < thresholds["lcr_min"]:
            add("lcr_buf", "LCR Below Internal Buffer", "warning", lcr, thresholds["lcr_min"], "%",
                f"LCR {lcr:.1f}% is below internal buffer of {thresholds['lcr_min']}%.",
                "Pre-position HQLA. Review outflow assumptions. Monitor daily.")

    # NSFR
    nsfr = metrics.get("nsfr")
    if nsfr is not None:
        if nsfr < 100:
            add("nsfr_reg", "NSFR REGULATORY BREACH", "critical", nsfr, 100, "%",
                f"NSFR {nsfr:.1f}% is below 100% minimum.",
                "Extend liability maturities or reduce RSF. Prioritise long-term funding issuance.")
        elif nsfr < thresholds["nsfr_min"]:
            add("nsfr_buf", "NSFR Below Internal Buffer", "warning", nsfr, thresholds["nsfr_min"], "%",
                f"NSFR {nsfr:.1f}% below internal buffer.",
                "Review ASF mix. Consider longer-tenor wholesale funding.")

    # NII shock
    nii_base  = metrics.get("nii_base")
    nii_delta = metrics.get("nii_worst_delta")
    if nii_base and nii_delta is not None and abs(nii_base) > 0:
        pct     = abs(nii_delta) / abs(nii_base) * 100
        max_pct = thresholds["nii_shock_max_pct"]
        if pct > max_pct:
            sev = "critical" if pct > max_pct * 1.5 else "warning"
            add("nii_shock", "NII Shock Exceeds Limit", sev, pct, max_pct, "% of base NII",
                f"Worst-case ΔNII of {nii_delta:.1f}M = {pct:.1f}% of base NII ({nii_base:.1f}M).",
                "Extend liability tenors or add receive-fixed IRS hedges to reduce liability sensitivity.")

    # Duration gap
    dur_gap = metrics.get("duration_gap")
    if dur_gap is not None:
        max_gap = thresholds["duration_gap_max"]
        if abs(dur_gap) > max_gap:
            sev = "critical" if abs(dur_gap) > max_gap * 1.5 else "warning"
            direction = "asset-sensitive" if dur_gap > 0 else "liability-sensitive"
            add("dur_gap", "Duration Gap Exceeds Limit", sev, dur_gap, max_gap, "years",
                f"Duration gap {dur_gap:.2f}Y ({direction}) exceeds ±{max_gap}Y limit.",
                "Add IRS hedges or restructure asset/liability maturities to close the gap.")

    # EVE (IRRBB)
    eve_delta = metrics.get("eve_delta_worst")
    tier1     = metrics.get("tier1_capital")
    if eve_delta is not None and tier1 and tier1 > 0:
        pct     = abs(eve_delta) / tier1 * 100
        max_pct = thresholds["eve_shock_max_pct_tier1"]
        if pct > max_pct:
            add("eve_irrbb", "IRRBB EVE Breach — Basel III", "critical", pct, max_pct, "% of Tier 1",
                f"ΔEVE {eve_delta:.1f}M = {pct:.1f}% of Tier 1 ({tier1:.1f}M). Basel limit: {max_pct}%.",
                "Urgent: reduce duration gap. Consider IRS or structured note issuance. Report to BRSA.")
        elif pct > max_pct * 0.75:
            add("eve_warn", "EVE Approaching IRRBB Limit", "warning", pct, max_pct, "% of Tier 1",
                f"ΔEVE at {pct:.1f}% Tier 1 is approaching the {max_pct}% Basel limit.",
                "Review hedging strategy before limit is breached.")

    # FX NOP
    fx_nop  = metrics.get("fx_nop_tl")
    capital = metrics.get("capital_tl")
    if fx_nop is not None and capital and capital > 0:
        pct     = abs(fx_nop) / capital * 100
        max_pct = thresholds["fx_nop_max_pct_capital"]
        if pct > max_pct:
            add("fx_nop", "FX Net Open Position Breach", "critical", pct, max_pct, "% of capital",
                f"FX NOP {fx_nop:.1f}M TL = {pct:.1f}% of capital. Limit: {max_pct}%.",
                "Close FX positions via spot or FX swap. Check BRSA NOP limits immediately.")
        elif pct > max_pct * 0.80:
            add("fx_warn", "FX NOP Approaching Limit", "warning", pct, max_pct, "% of capital",
                f"FX NOP at {pct:.1f}% capital is approaching the {max_pct}% limit.",
                "Monitor FX positions. Prepare hedging trades.")

    # Rate delta
    for tenor, key in [("10Y", "rate_delta_10y"), ("2Y", "rate_delta_2y")]:
        rd = metrics.get(key)
        if rd is not None:
            rd_bps = abs(rd * 100)
            lim    = thresholds["rate_delta_alert_bps"]
            if rd_bps > lim:
                direction = "risen" if rd > 0 else "fallen"
                add(f"rate_{tenor.lower()}", f"{tenor} Rate Large Move (5-day)", "info",
                    rd * 100, lim, "bps",
                    f"{tenor} Treasury has {direction} {rd_bps:.1f}bps in last 5 days.",
                    "Update NII/EVE metrics. Review hedging positions.")

    sev_order = {"critical": 0, "warning": 1, "info": 2}
    alerts.sort(key=lambda x: sev_order.get(x["severity"], 3))

    return {
        "alerts":       alerts,
        "n_critical":   sum(1 for a in alerts if a["severity"] == "critical"),
        "n_warning":    sum(1 for a in alerts if a["severity"] == "warning"),
        "n_info":       sum(1 for a in alerts if a["severity"] == "info"),
        "all_clear":    len(alerts) == 0,
        "thresholds":   thresholds,
        "evaluated_at": datetime.now().isoformat(),
    }


def save_alerts(alerts_result: dict) -> dict:
    os.makedirs(DATA_DIR, exist_ok=True)
    existing = []
    if os.path.exists(ALERTS_FILE):
        try:
            existing = json.load(open(ALERTS_FILE))
        except Exception:
            existing = []
    existing.extend(alerts_result.get("alerts", []))
    existing = existing[-200:]
    _atomic_json_write(ALERTS_FILE, existing)
    return {"saved": len(alerts_result.get("alerts", [])), "total_log": len(existing)}


def load_alert_history() -> list:
    if not os.path.exists(ALERTS_FILE):
        return []
    try:
        return json.load(open(ALERTS_FILE))
    except Exception:
        return []


def acknowledge_alert(alert_id: str) -> dict:
    history = load_alert_history()
    count   = 0
    for a in history:
        if a.get("id") == alert_id and not a.get("acknowledged"):
            a["acknowledged"] = True
            count += 1
    _atomic_json_write(ALERTS_FILE, history)
    return {"acknowledged": count}


def send_email_alert(smtp_config: dict, alerts: list) -> dict:
    if not alerts:
        return {"sent": False, "reason": "No alerts"}
    critical = [a for a in alerts if a["severity"] == "critical"]
    subject  = (f"🔴 CRITICAL ALM ALERT — {len(critical)} breach(es)"
                if critical else f"🟡 ALM Warning — {len(alerts)} alert(s)")
    rows = ""
    for a in alerts:
        color = SEVERITY[a["severity"]]["color"]
        icon  = SEVERITY[a["severity"]]["icon"]
        rows += f"""<tr>
          <td style="padding:8px;border-bottom:1px solid #333">
            <strong style="color:{color}">{icon} {a['title']}</strong><br/>
            <span style="font-size:13px;color:#ccc">{a['description']}</span><br/>
            <span style="font-size:12px;color:#888">→ {a['recommendation']}</span>
          </td>
          <td style="padding:8px;border-bottom:1px solid #333;text-align:right">
            <strong style="color:{color}">{a['value']}{a['unit']}</strong><br/>
            <span style="font-size:12px;color:#888">Limit: {a['threshold']}{a['unit']}</span>
          </td></tr>"""
    html = f"""<html><body style="background:#0e1017;color:#e2e8f0;font-family:monospace;padding:20px">
    <h2 style="color:#2563eb">BEK RATE DESK — ALM ALERT</h2>
    <p style="color:#64748b">{datetime.now().strftime('%Y-%m-%d %H:%M')}</p>
    <table style="width:100%;border-collapse:collapse;background:#13151e">{rows}</table>
    <p style="color:#64748b;font-size:12px;margin-top:20px">Bek Rate Desk — Automated ALM Monitoring</p>
    </body></html>"""
    try:
        msg            = MIMEMultipart("alternative")
        msg["Subject"] = subject
        msg["From"]    = smtp_config.get("from_addr", smtp_config.get("user", ""))
        msg["To"]      = ", ".join(smtp_config.get("to_addrs", []))
        msg.attach(MIMEText(html, "html"))
        with smtplib.SMTP(smtp_config["host"], int(smtp_config.get("port", 587))) as s:
            s.starttls()
            s.login(smtp_config["user"], smtp_config["password"])
            s.send_message(msg)
        return {"sent": True, "recipients": smtp_config.get("to_addrs", [])}
    except Exception as e:
        return {"sent": False, "error": str(e)}


def get_default_thresholds() -> dict:
    return DEFAULT_THRESHOLDS.copy()


def save_thresholds(thresholds: dict) -> dict:
    os.makedirs(DATA_DIR, exist_ok=True)
    path = os.path.join(DATA_DIR, "thresholds.json")
    _atomic_json_write(path, thresholds)
    return {"saved": True}


def load_thresholds() -> dict:
    path = os.path.join(DATA_DIR, "thresholds.json")
    if os.path.exists(path):
        try:
            return {**DEFAULT_THRESHOLDS, **json.load(open(path))}
        except Exception:
            pass
    return DEFAULT_THRESHOLDS.copy()

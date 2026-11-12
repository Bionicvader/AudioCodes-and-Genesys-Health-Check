"""
healthcheck/email_sender.py
───────────────────────────
Sends the Health Check email with an HTML body summarising results
from both the AudioCodes and Genesys Cloud modules.

Also sends the Genesys Cloud User Login Report email
(send_user_login_report), which has its own HTML body and attaches
both the PDF and CSV.

Called by main.py after the report files have been generated.
"""

from __future__ import annotations


import logging
import os
import smtplib
from datetime import datetime
from email import encoders
from email.mime.base import MIMEBase
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

log = logging.getLogger(__name__)


# ─────────────────────────────────────────────
#  HELPERS
# ─────────────────────────────────────────────

def _parse_recipients(raw) -> list[str]:
    if isinstance(raw, list):
        return [a.strip() for a in raw if str(a).strip()]
    return [a.strip() for a in str(raw).split(",") if a.strip()]


def _build_subject(base_subject: str, module_results: dict) -> str:
    failures = [name for name, path in module_results.items() if path is None]
    ts = datetime.now().strftime("%Y-%m-%d %H:%M")
    if not failures:
        tag = "OK"
    elif len(failures) == len(module_results):
        tag = "FAILED"
    else:
        tag = f"PARTIAL ({', '.join(failures)} failed)"
    return f"{base_subject} — {tag} — {ts}"


def _badge(text: str, color: str) -> str:
    """Inline HTML badge pill."""
    return (
        f'<span style="background:{color};color:#fff;padding:2px 10px;' +
        f'border-radius:12px;font-size:12px;font-weight:600;">{text}</span>'
    )


def _status_badge(ok: bool) -> str:
    return _badge("OK", "#27AE60") if ok else _badge("FAILED", "#C0392B")


def _count_row(label: str, value: str, warn: bool = False) -> str:
    color = "#C0392B" if warn else "#2C3E50"
    return (
        f'<tr><td style="padding:4px 8px;color:#7F8C8D;font-size:13px;">{label}</td>' +
        f'<td style="padding:4px 8px;color:{color};font-size:13px;font-weight:600;">' +
        f'{value}</td></tr>'
    )


def _build_html_body(
    report_ts: str,
    module_results: dict,
    ac_summary: dict | None,
    gc_summary: dict | None,
    sc_summary: dict | None = None,
) -> str:
    """
    Build the full HTML email body.

    ac_summary keys: reachable, total, active_alarms, unreachable_devices (list of dicts)
    gc_summary keys: total_edges, connected_edges, trunks_total, trunks_up, trunks_down
    sc_summary keys: date, total, ok, issues (list of dicts) — only rendered when
                     "Stats Collectors" is a key in module_results (i.e. enabled)
    """

    ac_ok = module_results.get("AudioCodes")   is not None
    gc_ok = module_results.get("Genesys Cloud") is not None

    # ── AudioCodes card ────────────────────────────────────
    def _sub_table(rows_html: str, headers: list[str]) -> str:
        """Render a compact sub-table inside an email section."""
        ths = "".join(
            f'<th style="padding:4px 8px;font-size:11px;color:#7F8C8D;' +
            f'text-align:left;background:#F8F9FA;font-weight:600;">{h}</th>'
            for h in headers
        )
        return (
            f'<tr><td colspan="2" style="padding:6px 8px 2px 20px;">' +
            f'<table width="100%" cellpadding="0" cellspacing="0" ' +
            f'style="border-collapse:collapse;border:1px solid #ECF0F1;border-radius:4px;">' +
            f'<tr>{ths}</tr>{rows_html}</table></td></tr>'
        )

    if ac_ok and ac_summary:
        ac = ac_summary
        unreachable      = ac.get("unreachable_devices", [])
        critical_devices = ac.get("critical_devices",    [])
        ac_card_counts = (
            _count_row("Devices reachable",
                       f"{ac['reachable']}/{ac['total']}",
                       warn=(ac['reachable'] < ac['total'])) +
            _count_row("Active alarms",
                       str(ac["active_alarms"]),
                       warn=(ac["active_alarms"] > 0)) +
            _count_row(f"Historical alarms ({ac.get('history_days', 7)}d)",
                       str(ac.get("historical_alarms", 0)))
        )

        # Critical devices sub-table
        if critical_devices:
            crit_rows = "".join(
                f'<tr style="border-top:1px solid #F4F6F8;">' +
                f'<td style="padding:4px 8px;font-size:12px;color:#2C3E50;white-space:nowrap;">{d["ip"]}</td>' +
                f'<td style="padding:4px 8px;font-size:12px;color:#7F8C8D;white-space:nowrap;">{d["country"]}</td>' +
                f'<td style="padding:4px 8px;font-size:12px;color:#C0392B;">{d["alarm"]}</td>' +
                f'</tr>'
                for d in critical_devices
            )
            ac_card_counts += (
                f'<tr><td colspan="2" style="padding:6px 8px 2px 0;">' +
                f'<div style="font-size:12px;font-weight:600;color:#C0392B;margin-bottom:4px;">' +
                f'&#9888; {len(critical_devices)} Device(s) with Critical Alarm(s)</div>' +
                f'</td></tr>' +
                _sub_table(crit_rows, ["IP", "Country", "Alarm"])
            )
        # Unreachable devices sub-table
        if unreachable:
            def _short_reason(r: str) -> str:
                """Extract just the key error type from the full exception string."""
                r = r[:120]
                if "timed out" in r.lower() or "ConnectTimeout" in r:
                    return "Connection timed out"
                if "SSL" in r or "ssl" in r:
                    return "SSL handshake failure"
                if "FileNotFoundError" in r:
                    return "Connection aborted (network unreachable)"
                if "ConnectionRefused" in r or "refused" in r.lower():
                    return "Connection refused"
                return r[:80]

            rows = "".join(
                f'<tr style="border-bottom:1px solid #F4F6F8;">' +
                f'<td style="padding:5px 8px;font-size:12px;color:#2C3E50;white-space:nowrap;">{d["ip"]}</td>' +
                f'<td style="padding:5px 8px;font-size:12px;color:#7F8C8D;white-space:nowrap;">{d.get("country","")}</td>' +
                f'<td style="padding:5px 8px;font-size:12px;color:#C0392B;">{_short_reason(d.get("reason",""))}</td>' +
                f'</tr>'
                for d in unreachable
            )
            unreachable_block = f"""
            <tr><td colspan="2" style="padding:8px 8px 2px;">
              <div style="font-size:12px;font-weight:600;color:#C0392B;margin-bottom:4px;">
                &#9888; {len(unreachable)} Unreachable Device(s)
              </div>
              <table width="100%" cellpadding="0" cellspacing="0" style="border-collapse:collapse;">
                <tr style="background:#F8F9FA;">
                  <th style="padding:3px 8px;font-size:11px;color:#7F8C8D;text-align:left;">IP</th>
                  <th style="padding:3px 8px;font-size:11px;color:#7F8C8D;text-align:left;">Country</th>
                  <th style="padding:3px 8px;font-size:11px;color:#7F8C8D;text-align:left;">Reason</th>
                </tr>
                {rows}
              </table>
            </td></tr>"""
        else:
            unreachable_block = ""
    else:
        ac_card_counts  = _count_row("Status", "Module failed — check logs", warn=True)
        unreachable_block = ""

    # ── Genesys card ───────────────────────────────────────
    if gc_ok and gc_summary:
        gc            = gc_summary
        offline_edges = gc.get("offline_edges", [])
        gc_card_counts = (
            _count_row("Edges online",
                       f"{gc['connected_edges']}/{gc['total_edges']}",
                       warn=(gc['connected_edges'] < gc['total_edges'])) +
            _count_row("Trunks in service",
                       f"{gc['trunks_up']}/{gc['trunks_total']}",
                       warn=(gc['trunks_down'] > 0)) +
            _count_row("Trunks out of service",
                       str(gc["trunks_down"]),
                       warn=(gc["trunks_down"] > 0))
        )

        # Offline edges sub-table
        if offline_edges:
            edge_rows = "".join(
                f'<tr style="border-top:1px solid #F4F6F8;">' +
                f'<td style="padding:4px 8px;font-size:12px;color:#2C3E50;">{e["name"]}</td>' +
                f'<td style="padding:4px 8px;font-size:12px;color:#C0392B;white-space:nowrap;">{e["online_status"]}</td>' +
                f'</tr>'
                for e in offline_edges
            )
            gc_card_counts += (
                f'<tr><td colspan="2" style="padding:6px 8px 2px 0;">' +
                f'<div style="font-size:12px;font-weight:600;color:#C0392B;margin-bottom:4px;">' +
                f'&#9888; {len(offline_edges)} Offline Edge(s)</div>' +
                f'</td></tr>' +
                _sub_table(edge_rows, ["Edge Name", "Status"])
            )

        # Out-of-service trunks sub-table (deduplicated by trunk name)
        oos_trunks = gc.get("oos_trunks", [])
        if oos_trunks:
            trunk_rows = "".join(
                f'<tr style="border-top:1px solid #F4F6F8;">' +
                f'<td style="padding:4px 8px;font-size:12px;color:#2C3E50;">{t["name"]}</td>' +
                f'<td style="padding:4px 8px;font-size:12px;color:#7F8C8D;">{t["edges"]}</td>' +
                f'</tr>'
                for t in oos_trunks
            )
            gc_card_counts += (
                f'<tr><td colspan="2" style="padding:6px 8px 2px 0;">' +
                f'<div style="font-size:12px;font-weight:600;color:#C0392B;margin-bottom:4px;">' +
                f'&#9888; {len(oos_trunks)} Out-of-Service Trunk(s)</div>' +
                f'</td></tr>' +
                _sub_table(trunk_rows, ["Trunk Name", "Offline on Edge(s)"])
            )
    else:
        gc_card_counts = _count_row("Status", "Module failed — check logs", warn=True)

    # ── Stats Collectors card (only when enabled) ──────────
    sc_section = ""
    if "Stats Collectors" in module_results:
        sc_ok = module_results.get("Stats Collectors") is not None
        if sc_ok and sc_summary:
            sc = sc_summary
            sc_rows = _count_row(f"Audit files OK ({sc.get('date', '')})",
                                 f"{sc['ok']}/{sc['total']}",
                                 warn=(sc["ok"] < sc["total"]))
            if sc["issues"]:
                sev_color = {"warning": "#E67E22", "critical": "#C0392B"}
                issue_rows = "".join(
                    f'<tr style="border-top:1px solid #F4F6F8;">' +
                    f'<td style="padding:4px 8px;font-size:12px;color:#2C3E50;white-space:nowrap;">{_html_escape(i["name"])}</td>' +
                    f'<td style="padding:4px 8px;font-size:12px;font-weight:600;white-space:nowrap;color:{sev_color.get(i["severity"], "#C0392B")};">{i["status"]}</td>' +
                    f'<td style="padding:4px 8px;font-size:12px;color:#7F8C8D;">{_html_escape(i["detail"])}</td>' +
                    f'</tr>'
                    for i in sc["issues"]
                )
                sc_rows += (
                    f'<tr><td colspan="2" style="padding:6px 8px 2px 0;">' +
                    f'<div style="font-size:12px;font-weight:600;color:#C0392B;margin-bottom:4px;">' +
                    f'&#9888; {len(sc["issues"])} Stats Export Issue(s)</div>' +
                    f'</td></tr>' +
                    _sub_table(issue_rows, ["Collector", "Status", "Detail"])
                )
        else:
            sc_rows = _count_row("Status", "Module failed — check logs", warn=True)

        sc_section = f"""
        <!-- Stats Collectors section -->
        <tr>
          <td style="padding:16px 24px;border-bottom:1px solid #ECF0F1;">
            <table width="100%" cellpadding="0" cellspacing="0">
              <tr>
                <td style="font-size:13px;font-weight:700;color:#2C3E50;padding-bottom:8px;">
                  Stats Collectors &nbsp; {_status_badge(sc_ok)}
                </td>
              </tr>
              {sc_rows}
            </table>
          </td>
        </tr>
"""

    html = f"""<!DOCTYPE html>
<html>
<body style="margin:0;padding:0;font-family:Arial,sans-serif;background:#F4F6F8;">
  <table width="100%" cellpadding="0" cellspacing="0" style="background:#F4F6F8;padding:24px 0;">
    <tr><td align="center">
      <table width="600" cellpadding="0" cellspacing="0" style="background:#fff;border-radius:8px;overflow:hidden;box-shadow:0 2px 8px rgba(0,0,0,0.08);">

        <!-- Header -->
        <tr>
          <td style="background:#1B2A4A;padding:20px 24px;">
            <div style="color:#fff;font-size:18px;font-weight:700;">Contact Centre Health Check</div>
            <div style="color:#BDC3C7;font-size:12px;margin-top:4px;">{report_ts}</div>
          </td>
        </tr>

        <!-- AudioCodes section -->
        <tr>
          <td style="padding:16px 24px;border-bottom:1px solid #ECF0F1;">
            <table width="100%" cellpadding="0" cellspacing="0">
              <tr>
                <td style="font-size:13px;font-weight:700;color:#2C3E50;padding-bottom:8px;">
                  AudioCodes &nbsp; {_status_badge(ac_ok)}
                </td>
              </tr>
              {ac_card_counts}
              {unreachable_block}
            </table>
          </td>
        </tr>

        <!-- Genesys section -->
        <tr>
          <td style="padding:16px 24px;border-bottom:1px solid #ECF0F1;">
            <table width="100%" cellpadding="0" cellspacing="0">
              <tr>
                <td style="font-size:13px;font-weight:700;color:#2C3E50;padding-bottom:8px;">
                  Genesys Cloud &nbsp; {_status_badge(gc_ok)}
                </td>
              </tr>
              {gc_card_counts}
            </table>
          </td>
        </tr>
{sc_section}
        <!-- Footer -->
        <tr>
          <td style="background:#F8F9FA;padding:12px 24px;">
            <div style="font-size:12px;color:#7F8C8D;">
              &#128206; The full Health Check report is attached as a PDF.
            </div>
            <div style="font-size:11px;color:#BDC3C7;margin-top:4px;">
              This message was sent automatically by the Altron Health Check application.
            </div>
          </td>
        </tr>

      </table>
    </td></tr>
  </table>
</body>
</html>"""

    return html


# ─────────────────────────────────────────────
#  PUBLIC API
# ─────────────────────────────────────────────

def send(
    config:         dict,
    pdf_path:       str | None,
    module_results: dict,
    ac_summary:     dict | None = None,
    gc_summary:     dict | None = None,
    sc_summary:     dict | None = None,
) -> bool:
    """
    Send the Health Check email with an HTML body.

    Parameters
    ----------
    config         : full application config dict (reads smtp: namespace)
    pdf_path       : path to the merged PDF, or None if generation failed
    module_results : dict mapping module name → data or None (failed)
    ac_summary     : AudioCodes summary dict for email body (optional)
    gc_summary     : Genesys Cloud summary dict for email body (optional)
    sc_summary     : Stats Collectors summary dict for email body (optional)
    """
    smtp_cfg  = config.get("smtp", {})
    host      = smtp_cfg.get("host",     "")
    port      = int(smtp_cfg.get("port", 25))
    use_tls   = bool(smtp_cfg.get("useTls", False))
    username  = smtp_cfg.get("username", "") or ""
    password  = smtp_cfg.get("password", "") or ""
    from_addr = smtp_cfg.get("from",     "")
    to_raw    = smtp_cfg.get("to",       "")
    subject   = smtp_cfg.get("subject",  "Health Check Report")

    if not host:
        log.error("[email] smtp.host is not configured — skipping email.")
        return False

    to_addrs = _parse_recipients(to_raw)
    if not to_addrs:
        log.error("[email] smtp.to has no valid recipients — skipping email.")
        return False

    report_ts    = datetime.now().strftime("%Y-%m-%d %H:%M UTC")
    full_subject = _build_subject(subject, module_results)
    html_body    = _build_html_body(report_ts, module_results, ac_summary, gc_summary,
                                    sc_summary)

    log.info("[email] Preparing email → %s", ", ".join(to_addrs))
    log.debug("[email] Subject: %s", full_subject)

    # ── Build message ─────────────────────────────────────
    msg = MIMEMultipart("alternative")
    msg["From"]    = from_addr
    msg["To"]      = ", ".join(to_addrs)
    msg["Subject"] = full_subject

    # Plain text fallback
    plain = (
        f"Contact Centre Health Check — {report_ts}\n\n"
        f"AudioCodes : {'OK' if module_results.get('AudioCodes') else 'FAILED'}\n"
        f"Genesys    : {'OK' if module_results.get('Genesys Cloud') else 'FAILED'}\n"
        + (f"Stats      : "
           f"{(str(sc_summary['ok']) + '/' + str(sc_summary['total']) + ' audit files OK') if sc_summary else 'FAILED'}\n"
           if "Stats Collectors" in module_results else "")
        + "\nThe full report is attached as a PDF.\n\n"
        "This message was sent automatically by the Altron Health Check application."
    )
    msg.attach(MIMEText(plain,    "plain"))
    msg.attach(MIMEText(html_body, "html"))

    # ── Convert to mixed for attachment ──────────────────
    outer = MIMEMultipart("mixed")
    outer["From"]    = from_addr
    outer["To"]      = ", ".join(to_addrs)
    outer["Subject"] = full_subject
    outer.attach(msg)

    # ── Attach PDF ────────────────────────────────────────
    if pdf_path and os.path.isfile(pdf_path):
        filename = os.path.basename(pdf_path)
        log.info("[email] Attaching PDF: %s", filename)
        try:
            with open(pdf_path, "rb") as f:
                part = MIMEBase("application", "octet-stream")
                part.set_payload(f.read())
            encoders.encode_base64(part)
            part.add_header("Content-Disposition",
                            f'attachment; filename="{filename}"')
            outer.attach(part)
        except OSError as e:
            log.error("[email] Could not read PDF for attachment: %s", e)
    else:
        log.warning("[email] No PDF to attach.")

    # ── Send ──────────────────────────────────────────────
    try:
        log.debug("[email] Connecting to %s:%d  TLS=%s", host, port, use_tls)
        if port == 465:
            server = smtplib.SMTP_SSL(host, port, timeout=30)
        else:
            server = smtplib.SMTP(host, port, timeout=30)
            server.ehlo()
            if use_tls:
                server.starttls()
                server.ehlo()

        if username and password:
            server.login(username, password)
            log.debug("[email] SMTP login successful")
        else:
            log.debug("[email] Unauthenticated relay")

        server.sendmail(from_addr, to_addrs, outer.as_string())
        server.quit()
        log.info("[email] Email sent successfully to: %s", ", ".join(to_addrs))
        return True

    except smtplib.SMTPAuthenticationError as e:
        log.error("[email] SMTP authentication failed: %s", e)
    except smtplib.SMTPConnectError as e:
        log.error("[email] Could not connect to SMTP server %s:%d — %s", host, port, e)
    except smtplib.SMTPException as e:
        log.error("[email] SMTP error: %s", e)
    except OSError as e:
        log.error("[email] Network error sending email: %s", e)

    return False


# ─────────────────────────────────────────────
#  USER LOGIN REPORT EMAIL
# ─────────────────────────────────────────────

def _html_escape(text: str) -> str:
    return (str(text).replace("&", "&amp;").replace("<", "&lt;")
                     .replace(">", "&gt;").replace('"', "&quot;"))


def _attach_file(outer: MIMEMultipart, path: str | None, label: str) -> None:
    """Attach a file as base64 if it exists; log and continue otherwise."""
    if not path or not os.path.isfile(path):
        log.warning("[email] No %s to attach.", label)
        return
    filename = os.path.basename(path)
    try:
        with open(path, "rb") as f:
            part = MIMEBase("application", "octet-stream")
            part.set_payload(f.read())
        encoders.encode_base64(part)
        part.add_header("Content-Disposition", f'attachment; filename="{filename}"')
        outer.attach(part)
        log.info("[email] Attached %s: %s", label, filename)
    except OSError as e:
        log.error("[email] Could not read %s for attachment: %s", label, e)


def _deliver(smtp_cfg: dict, from_addr: str, to_addrs: list[str], message) -> bool:
    """Open the SMTP connection described by smtp_cfg and send `message`."""
    host     = smtp_cfg.get("host", "")
    port     = int(smtp_cfg.get("port", 25))
    use_tls  = bool(smtp_cfg.get("useTls", False))
    username = smtp_cfg.get("username", "") or ""
    password = smtp_cfg.get("password", "") or ""
    try:
        log.debug("[email] Connecting to %s:%d  TLS=%s", host, port, use_tls)
        if port == 465:
            server = smtplib.SMTP_SSL(host, port, timeout=30)
        else:
            server = smtplib.SMTP(host, port, timeout=30)
            server.ehlo()
            if use_tls:
                server.starttls()
                server.ehlo()
        if username and password:
            server.login(username, password)
        server.sendmail(from_addr, to_addrs, message.as_string())
        server.quit()
        log.info("[email] Email sent successfully to: %s", ", ".join(to_addrs))
        return True
    except smtplib.SMTPAuthenticationError as e:
        log.error("[email] SMTP authentication failed: %s", e)
    except smtplib.SMTPConnectError as e:
        log.error("[email] Could not connect to SMTP server %s:%d — %s", host, port, e)
    except smtplib.SMTPException as e:
        log.error("[email] SMTP error: %s", e)
    except OSError as e:
        log.error("[email] Network error sending email: %s", e)
    return False


def send_user_login_report(
    config:   dict,
    pdf_path: str | None,
    csv_path: str | None,
    summary:  dict | None,
    error:    str | None = None,
) -> bool:
    """
    Send the Genesys Cloud User Login Report email.

    Parameters
    ----------
    config   : full application config dict (reads smtp: and userLoginReport:)
    pdf_path : path to the PDF report, or None
    csv_path : path to the CSV export, or None
    summary  : dict with keys total, recent, inactive_count, never_count, days,
               expand_suspect — or None if collection failed
    error    : short failure reason when summary is None
    """
    smtp_cfg  = config.get("smtp", {})
    rpt_cfg   = config.get("userLoginReport", {}) or {}
    from_addr = smtp_cfg.get("from", "")
    base_subj = rpt_cfg.get("subject", "Genesys Cloud User Login Report")

    if not smtp_cfg.get("host"):
        log.error("[email] smtp.host is not configured — skipping email.")
        return False
    to_addrs = _parse_recipients(smtp_cfg.get("to", ""))
    if not to_addrs:
        log.error("[email] smtp.to has no valid recipients — skipping email.")
        return False

    from datetime import timezone
    ts_subject = datetime.now().strftime("%Y-%m-%d %H:%M")
    ts_body    = datetime.now(tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    if summary is None:
        tag   = "FAILED"
        rows  = _count_row("Status", "Report failed — check logs", warn=True)
        if error:
            rows += _count_row("Reason", _html_escape(error[:200]), warn=True)
        plain_body = f"Report failed: {error or 'see logs'}"
    else:
        days     = summary["days"]
        inactive = summary["inactive_count"]
        never    = summary["never_count"]
        flagged  = inactive + never
        tag      = f"{flagged} user(s) flagged" if flagged else "OK"
        if summary.get("expand_suspect"):
            tag = "DATA WARNING — check log"
        rows = (
            _count_row("Total active users",               str(summary["total"])) +
            _count_row(f"Logged in within {days} days",    str(summary["recent"])) +
            _count_row(f"Not logged in for {days}+ days",  str(inactive), warn=inactive > 0) +
            _count_row("Never logged in",                  str(never),    warn=never > 0)
        )
        if summary.get("expand_suspect"):
            rows += _count_row(
                "Warning",
                "No last-login dates were returned — results may be unreliable",
                warn=True)
        plain_body = (
            f"Total active users           : {summary['total']}\n"
            f"Logged in within {days} days  : {summary['recent']}\n"
            f"Not logged in for {days}+ days: {inactive}\n"
            f"Never logged in              : {never}"
        )

    full_subject = f"{base_subj} — {tag} — {ts_subject}"
    badge = (_badge("FAILED", "#C0392B") if summary is None else
             _badge("CHECK", "#C0392B") if summary.get("expand_suspect") else
             _badge("OK", "#27AE60") if tag == "OK" else
             _badge("ACTION", "#E67E22"))

    html = f"""<!DOCTYPE html>
<html>
<body style="margin:0;padding:0;font-family:Arial,sans-serif;background:#F4F6F8;">
  <table width="100%" cellpadding="0" cellspacing="0" style="background:#F4F6F8;padding:24px 0;">
    <tr><td align="center">
      <table width="600" cellpadding="0" cellspacing="0" style="background:#fff;border-radius:8px;overflow:hidden;box-shadow:0 2px 8px rgba(0,0,0,0.08);">
        <tr>
          <td style="background:#1B2A4A;padding:20px 24px;">
            <div style="color:#fff;font-size:18px;font-weight:700;">Genesys Cloud User Login Report</div>
            <div style="color:#BDC3C7;font-size:12px;margin-top:4px;">{ts_body}</div>
          </td>
        </tr>
        <tr>
          <td style="padding:16px 24px;border-bottom:1px solid #ECF0F1;">
            <table width="100%" cellpadding="0" cellspacing="0">
              <tr>
                <td colspan="2" style="font-size:13px;font-weight:700;color:#2C3E50;padding-bottom:8px;">
                  Active users &nbsp; {badge}
                </td>
              </tr>
              {rows}
            </table>
          </td>
        </tr>
        <tr>
          <td style="background:#F8F9FA;padding:12px 24px;">
            <div style="font-size:12px;color:#7F8C8D;">
              &#128206; The full user list is attached as a PDF, with a CSV copy for filtering.
            </div>
            <div style="font-size:11px;color:#BDC3C7;margin-top:4px;">
              This message was sent automatically by the Altron Health Check application.
            </div>
          </td>
        </tr>
      </table>
    </td></tr>
  </table>
</body>
</html>"""

    plain = (
        f"Genesys Cloud User Login Report — {ts_body}\n\n{plain_body}\n\n"
        "The full user list is attached as a PDF, with a CSV copy for filtering.\n\n"
        "This message was sent automatically by the Altron Health Check application."
    )

    alt = MIMEMultipart("alternative")
    alt.attach(MIMEText(plain, "plain"))
    alt.attach(MIMEText(html,  "html"))

    outer = MIMEMultipart("mixed")
    outer["From"]    = from_addr
    outer["To"]      = ", ".join(to_addrs)
    outer["Subject"] = full_subject
    outer.attach(alt)
    _attach_file(outer, pdf_path, "PDF")
    _attach_file(outer, csv_path, "CSV")

    log.info("[email] Preparing user login report email → %s", ", ".join(to_addrs))
    return _deliver(smtp_cfg, from_addr, to_addrs, outer)

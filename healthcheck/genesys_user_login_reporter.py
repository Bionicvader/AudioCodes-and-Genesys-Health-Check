"""
healthcheck/genesys_user_login_reporter.py
──────────────────────────────────────────
Genesys Cloud User Login Report.

Lists every ACTIVE Genesys Cloud user who has either:
  • not logged in within the last `thresholdDays` days (default 30), or
  • never logged in at all (dateLastLogin is null).

Columns: Display Name, Division, Last Login Date.
Outputs: a branded PDF and a CSV (CSV adds a Category column for filtering).

Designed to be used in two ways:
  1. Imported by main.py  — call collect(config), build_pdf(), build_csv().
  2. Run standalone       — python -m healthcheck.genesys_user_login_reporter
                            (writes PDF + CSV, does NOT send email).

Config (healthcheck_config.yaml):
    userLoginReport:
      enabled: true
      time: "06:00"
      thresholdDays: 30
      subject: "Genesys Cloud User Login Report"

Auth:
    Reuses authenticate() from genesys_health_reporter (genesys: namespace).
    The OAuth client additionally needs: Directory > User > View.
"""

from __future__ import annotations

import csv
import logging
import os
import sys
from datetime import datetime, timedelta, timezone

import PureCloudPlatformClientV2
from PureCloudPlatformClientV2.rest import ApiException

log = logging.getLogger(__name__)

PAGE_SIZE              = 100
DEFAULT_THRESHOLD_DAYS = 30

CATEGORY_NEVER = "Never logged in"


# ─────────────────────────────────────────────
#  CONFIG
# ─────────────────────────────────────────────

def _report_cfg(config: dict) -> dict:
    return config.get("userLoginReport", {}) or {}


def threshold_days(config: dict) -> int:
    try:
        return int(_report_cfg(config).get("thresholdDays", DEFAULT_THRESHOLD_DAYS))
    except (TypeError, ValueError):
        log.warning("userLoginReport.thresholdDays is not a number — using %d",
                    DEFAULT_THRESHOLD_DAYS)
        return DEFAULT_THRESHOLD_DAYS


# ─────────────────────────────────────────────
#  DATA FETCHING
# ─────────────────────────────────────────────

def _to_utc(dt) -> datetime | None:
    """Normalise an SDK datetime (or ISO string) to an aware UTC datetime."""
    if dt is None:
        return None
    if isinstance(dt, str):
        try:
            dt = datetime.fromisoformat(dt.replace("Z", "+00:00"))
        except ValueError:
            return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def fetch_active_users(api_client) -> list[dict]:
    """
    Page through GET /api/v2/users?state=active&expand=dateLastLogin.
    Returns a list of {name, division, last_login (aware UTC datetime | None)}.
    Raises RuntimeError on API failure so the caller can report it — a
    partial user list would produce a misleading report.
    """
    users_api = PureCloudPlatformClientV2.UsersApi(api_client)
    users: list[dict] = []
    page_num = 1

    log.info("Fetching active Genesys Cloud users (expand=dateLastLogin) ...")
    while True:
        try:
            result = users_api.get_users(
                page_size=PAGE_SIZE,
                page_number=page_num,
                state="active",
                expand=["dateLastLogin"],
            )
        except ApiException as exc:
            hint = (" — check the OAuth client has Directory > User > View"
                    if exc.status in (401, 403) else "")
            raise RuntimeError(
                f"Failed to fetch users (page {page_num}) -- HTTP {exc.status}: "
                f"{exc.reason}{hint}"
            ) from exc

        if not result or not result.entities:
            break

        for u in result.entities:
            division = ""
            if getattr(u, "division", None):
                division = getattr(u.division, "name", "") or ""
            users.append({
                "name":       getattr(u, "name", "") or "",
                "division":   division,
                "last_login": _to_utc(getattr(u, "date_last_login", None)),
            })

        log.info("  Page %d/%s -- %d user(s) so far",
                 page_num, result.page_count or "?", len(users))

        if page_num >= (result.page_count or 1):
            break
        page_num += 1

    log.info("User fetch complete -- %d active user(s).", len(users))
    return users


def classify_users(users: list[dict], days: int, now: datetime) -> dict:
    """
    Split users into the two report categories.
      never    — last_login is None              (sorted by name)
      inactive — last_login older than `days`    (sorted oldest first)
    """
    cutoff   = now - timedelta(days=days)
    never    = sorted((u for u in users if u["last_login"] is None),
                      key=lambda u: u["name"].lower())
    inactive = sorted((u for u in users
                       if u["last_login"] is not None and u["last_login"] < cutoff),
                      key=lambda u: u["last_login"])
    recent   = len(users) - len(never) - len(inactive)

    # Sanity guard: the SDK documents expand as "best effort". If no user at
    # all came back with a dateLastLogin, the expand was most likely dropped
    # and every user would be wrongly reported as "never logged in".
    expand_suspect = bool(users) and all(u["last_login"] is None for u in users)
    if expand_suspect:
        log.warning("No user returned a dateLastLogin value — the expand may not "
                    "have been honoured. 'Never logged in' results are unreliable.")

    return {
        "total":          len(users),
        "recent":         recent,
        "inactive":       inactive,
        "never":          never,
        "days":           days,
        "cutoff":         cutoff,
        "expand_suspect": expand_suspect,
    }


def collect(config: dict) -> dict:
    """
    Authenticate, fetch active users and classify them.
    Returns the dict produced by classify_users() plus 'report_ts'.
    Raises RuntimeError on failure.
    """
    # Imported lazily so this module stays importable without side effects.
    from healthcheck.genesys_health_reporter import authenticate

    now  = datetime.now(tz=timezone.utc)
    days = threshold_days(config)
    log.info("User login report — threshold %d day(s), cutoff %s",
             days, (now - timedelta(days=days)).strftime("%Y-%m-%d %H:%M UTC"))

    api_client = authenticate(config)
    users      = fetch_active_users(api_client)
    data       = classify_users(users, days, now)
    data["report_ts"] = now

    log.info("User login report — total: %d | logged in within %dd: %d | "
             "inactive %dd+: %d | never: %d",
             data["total"], days, data["recent"], days,
             len(data["inactive"]), len(data["never"]))
    return data


# ─────────────────────────────────────────────
#  OUTPUT HELPERS
# ─────────────────────────────────────────────

def _fmt_date(dt: datetime | None) -> str:
    return dt.strftime("%Y-%m-%d") if dt else "Never"


def _output_path(config: dict, report_ts: datetime, ext: str) -> str:
    reporting  = config.get("reporting", {})
    report_dir = reporting.get("reportDir", ".")
    prefix     = reporting.get("filenamePrefix", "healthcheck_report")
    os.makedirs(report_dir, exist_ok=True)
    return os.path.join(
        report_dir, f"{prefix}_user_login_{report_ts.strftime('%Y%m%d_%H%M%S')}.{ext}"
    )


def _inactive_label(days: int) -> str:
    return f"Not logged in {days}+ days"


# ─────────────────────────────────────────────
#  CSV
# ─────────────────────────────────────────────

def build_csv(config: dict, data: dict) -> str:
    """Write the CSV and return its path. utf-8-sig so Excel opens it cleanly."""
    path  = _output_path(config, data["report_ts"], "csv")
    label = _inactive_label(data["days"])
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(["Category", "Display Name", "Division", "Last Login Date"])
        for u in data["inactive"]:
            w.writerow([label, u["name"], u["division"], _fmt_date(u["last_login"])])
        for u in data["never"]:
            w.writerow([CATEGORY_NEVER, u["name"], u["division"], "Never"])
    log.info("CSV saved --> %s", path)
    return path


# ─────────────────────────────────────────────
#  PDF
# ─────────────────────────────────────────────

def build_pdf(config: dict, data: dict) -> str:
    """Build the branded PDF and return its path. Raises on failure."""
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
    from reportlab.lib.units import mm
    from reportlab.platypus import (
        SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle, HRFlowable,
    )

    C_NAVY    = colors.HexColor("#1B2A4A")
    C_SECTION = colors.HexColor("#2E4070")
    C_GRID    = colors.HexColor("#DEE2E6")
    C_ROW_ALT = colors.HexColor("#F8F9FA")
    C_MUTED   = colors.HexColor("#95A5A6")
    C_OK      = colors.HexColor("#27AE60")
    C_WARN    = colors.HexColor("#E67E22")
    C_CRIT    = colors.HexColor("#C0392B")

    PAGE_W, PAGE_H = A4
    MARGIN   = 18 * mm
    USABLE_W = PAGE_W - 2 * MARGIN

    path      = _output_path(config, data["report_ts"], "pdf")
    days      = data["days"]
    ts_header = data["report_ts"].strftime("%Y-%m-%d  %H:%M:%S UTC")

    base = getSampleStyleSheet()
    st_title = ParagraphStyle("ULTitle", parent=base["Title"], fontSize=18,
                              textColor=C_NAVY, spaceAfter=2, alignment=0)
    st_sub   = ParagraphStyle("ULSub", parent=base["Normal"], fontSize=8.5,
                              textColor=C_MUTED, spaceAfter=2)
    st_h2    = ParagraphStyle("ULH2", parent=base["Heading2"], fontSize=11,
                              textColor=C_NAVY, spaceBefore=8, spaceAfter=4)
    st_cell  = ParagraphStyle("ULCell", parent=base["Normal"], fontSize=7.5, leading=9.5)
    st_hdr   = ParagraphStyle("ULHdr", parent=st_cell, textColor=colors.white)
    st_ok    = ParagraphStyle("ULOk", parent=base["Normal"], fontSize=9,
                              textColor=C_OK, spaceAfter=6)
    st_warn  = ParagraphStyle("ULWarn", parent=base["Normal"], fontSize=8.5,
                              textColor=C_CRIT, spaceAfter=6, leading=11)
    st_foot  = ParagraphStyle("ULFoot", parent=base["Normal"], fontSize=7,
                              textColor=C_MUTED)

    def _tbl_style(header_bg) -> TableStyle:
        return TableStyle([
            ("BACKGROUND",     (0, 0), (-1, 0),  header_bg),
            ("ROWBACKGROUNDS", (0, 1), (-1, -1), [C_ROW_ALT, colors.white]),
            ("GRID",           (0, 0), (-1, -1), 0.4, C_GRID),
            ("VALIGN",         (0, 0), (-1, -1), "MIDDLE"),
            ("LEFTPADDING",    (0, 0), (-1, -1), 5),
            ("RIGHTPADDING",   (0, 0), (-1, -1), 5),
            ("TOPPADDING",     (0, 0), (-1, -1), 3),
            ("BOTTOMPADDING",  (0, 0), (-1, -1), 3),
        ])

    def _user_table(users: list[dict]) -> Table:
        rows = [[Paragraph("<b>Display Name</b>",    st_hdr),
                 Paragraph("<b>Division</b>",        st_hdr),
                 Paragraph("<b>Last Login Date</b>", st_hdr)]]
        for u in users:
            rows.append([Paragraph(_esc(u["name"]),     st_cell),
                         Paragraph(_esc(u["division"]), st_cell),
                         Paragraph(_fmt_date(u["last_login"]), st_cell)])
        return Table(rows, colWidths=[USABLE_W * 0.46, USABLE_W * 0.34, USABLE_W * 0.20],
                     style=_tbl_style(C_SECTION), repeatRows=1)

    story: list = []

    # ── Title + summary ───────────────────────────────────
    story.append(Paragraph("Genesys Cloud User Login Report", st_title))
    story.append(Paragraph(
        f"Active users only  |  Inactivity threshold: {days} days  |  "
        f"Cutoff: {data['cutoff'].strftime('%Y-%m-%d %H:%M UTC')}", st_sub))
    story.append(HRFlowable(width="100%", thickness=1.5, color=C_NAVY,
                            spaceBefore=4, spaceAfter=8))

    if data.get("expand_suspect"):
        story.append(Paragraph(
            "<b>Warning:</b> Genesys Cloud returned no last-login dates for any user. "
            "The <i>dateLastLogin</i> expand may not have been honoured, so the "
            "'Never logged in' list below is likely inaccurate. Re-run the report "
            "and check the run log.", st_warn))

    summary = [
        ["Metric", "Count"],
        ["Total active users",                 str(data["total"])],
        [f"Logged in within {days} days",      str(data["recent"])],
        [f"Not logged in for {days}+ days",    str(len(data["inactive"]))],
        ["Never logged in",                    str(len(data["never"]))],
    ]
    s_style = _tbl_style(C_NAVY)
    s_style.add("TEXTCOLOR", (0, 0), (-1, 0), colors.white)
    s_style.add("FONTNAME",  (0, 0), (-1, 0), "Helvetica-Bold")
    s_style.add("FONTSIZE",  (0, 0), (-1, -1), 8.5)
    s_style.add("ALIGN",     (1, 0), (1, -1), "RIGHT")
    s_style.add("FONTNAME",  (1, 1), (1, -1), "Helvetica-Bold")
    s_style.add("TEXTCOLOR", (1, 2), (1, 2), C_OK)
    if data["inactive"]:
        s_style.add("TEXTCOLOR", (1, 3), (1, 3), C_WARN)
    if data["never"]:
        s_style.add("TEXTCOLOR", (1, 4), (1, 4), C_CRIT)
    story.append(Table(summary, colWidths=[USABLE_W * 0.45, USABLE_W * 0.15],
                       style=s_style, hAlign="LEFT"))
    story.append(Spacer(1, 6 * mm))

    # ── Inactive 30+ days ─────────────────────────────────
    story.append(Paragraph(
        f"Not Logged In for {days}+ Days  ({len(data['inactive'])})", st_h2))
    if data["inactive"]:
        story.append(Paragraph("Sorted by last login date, oldest first.", st_sub))
        story.append(_user_table(data["inactive"]))
    else:
        story.append(Paragraph(
            f"&#10003;  Every user with a login has logged in within {days} days.", st_ok))
    story.append(Spacer(1, 6 * mm))

    # ── Never logged in ───────────────────────────────────
    story.append(Paragraph(f"Never Logged In  ({len(data['never'])})", st_h2))
    if data["never"]:
        story.append(Paragraph("Sorted by display name.", st_sub))
        story.append(_user_table(data["never"]))
    else:
        story.append(Paragraph("&#10003;  All active users have logged in at least once.",
                               st_ok))

    story.append(Spacer(1, 8 * mm))
    story.append(HRFlowable(width="100%", thickness=0.5, color=C_GRID, spaceBefore=4))
    story.append(Paragraph(
        "Generated automatically by the Altron Health Check application. "
        "Source: Genesys Cloud /api/v2/users (state=active, expand=dateLastLogin). "
        "Dates are UTC. A CSV copy of this report is attached for filtering.",
        st_foot))

    def _on_page(canvas, doc):
        canvas.saveState()
        canvas.setFillColor(C_NAVY)
        canvas.rect(0, PAGE_H - 22 * mm, PAGE_W, 22 * mm, fill=1, stroke=0)
        canvas.setFont("Helvetica-Bold", 13)
        canvas.setFillColor(colors.white)
        canvas.drawString(MARGIN, PAGE_H - 14 * mm, "Genesys Cloud -- User Login Report")
        canvas.setFont("Helvetica", 8)
        canvas.drawRightString(PAGE_W - MARGIN, PAGE_H - 14 * mm, ts_header)
        canvas.setFont("Helvetica", 7)
        canvas.setFillColor(colors.HexColor("#888888"))
        canvas.drawString(MARGIN, 8 * mm,
                          "Altron Group  |  Genesys Cloud User Login Report  |  Confidential")
        canvas.drawRightString(PAGE_W - MARGIN, 8 * mm, f"Page {doc.page}")
        canvas.restoreState()

    doc = SimpleDocTemplate(
        path, pagesize=A4,
        leftMargin=MARGIN, rightMargin=MARGIN,
        topMargin=26 * mm, bottomMargin=14 * mm,
        title="Genesys Cloud User Login Report",
        author="Altron Group -- Automated Reporter",
    )
    doc.build(story, onFirstPage=_on_page, onLaterPages=_on_page)
    log.info("PDF saved --> %s", path)
    return path


def _esc(text: str) -> str:
    """Escape characters ReportLab's Paragraph markup would misinterpret."""
    return (str(text).replace("&", "&amp;")
                     .replace("<", "&lt;")
                     .replace(">", "&gt;"))


# ─────────────────────────────────────────────
#  STANDALONE ENTRY POINT
# ─────────────────────────────────────────────

def main() -> None:
    """
    Standalone: reads healthcheck_config.yaml, writes PDF + CSV.
    Email is NOT sent here — use  python main.py --report user-login.
    """
    import yaml
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s  %(levelname)-8s  %(message)s",
                        datefmt="%Y-%m-%d %H:%M:%S",
                        handlers=[logging.StreamHandler(sys.stdout)])
    cfg_file = "healthcheck_config.yaml"
    if not os.path.exists(cfg_file):
        log.error("Config file not found: %s", cfg_file)
        sys.exit(1)
    with open(cfg_file, encoding="utf-8") as f:
        config = yaml.safe_load(f)
    data = collect(config)
    log.info("PDF: %s", build_pdf(config, data))
    log.info("CSV: %s", build_csv(config, data))


if __name__ == "__main__":
    main()

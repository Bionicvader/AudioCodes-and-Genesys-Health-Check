"""
healthcheck/pdf_merger.py
─────────────────────────
Builds the unified Health Check PDF from pre-collected data supplied by
both the AudioCodes and Genesys modules.

Document structure
──────────────────
  Page 1  — Combined Issues Summary
              └─ AudioCodes issues block  (alarms with severity warning+)
              └─ Genesys issues block     (offline edges + out-of-service trunks)
  Page 2  — Clickable Table of Contents  (section-level internal PDF links)
  ──── AudioCodes platform banner ────
  Page N  — AC: Device & Trunk Overview
  Page N  — AC: Active Alarms
  Page N  — AC: Historical Alarms
  ──── Genesys Cloud platform banner ─
  Page N  — GC: Edge Overview
  Page N  — GC: Trunk Health

Called by main.py:
    from healthcheck import pdf_merger
    pdf_path = pdf_merger.build(config, ac_data, gc_data, report_ts)
"""

from __future__ import annotations

import logging
import os
from datetime import datetime, timezone

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.units import mm, inch
from reportlab.platypus import (
    SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle,
    HRFlowable, PageBreak, KeepTogether,
)
from reportlab.platypus.flowables import Flowable

log = logging.getLogger(__name__)

# ─────────────────────────────────────────────
#  BRAND COLOURS
# ─────────────────────────────────────────────

# Shared
C_NAVY        = colors.HexColor("#1B2A4A")
C_WHITE       = colors.white
C_GRID        = colors.HexColor("#DEE2E6")
C_ROW_ALT     = colors.HexColor("#F8F9FA")
C_FOOTER_TEXT = colors.HexColor("#95A5A6")

# Severity
C_CRITICAL    = colors.HexColor("#C0392B")
C_MAJOR       = colors.HexColor("#E67E22")
C_MINOR       = colors.HexColor("#F1C40F")
C_WARNING_AC  = colors.HexColor("#3498DB")
C_OK          = colors.HexColor("#27AE60")
C_DISABLED    = colors.HexColor("#7F8C8D")
C_UNKNOWN     = colors.HexColor("#95A5A6")

# Platform accent colours
C_AC_BANNER   = colors.HexColor("#2C3E50")   # AudioCodes — dark slate
C_AC_SECTION  = colors.HexColor("#34495E")   # AudioCodes section header
C_GC_BANNER   = colors.HexColor("#1B2A4A")   # Genesys     — navy
C_GC_SECTION  = colors.HexColor("#2E4070")   # Genesys section header

PAGE_W, PAGE_H = A4
MARGIN         = 18 * mm
USABLE_W       = PAGE_W - 2 * MARGIN


def _trunc(text: str, max_chars: int = 60) -> str:
    """Truncate text to max_chars, appending ellipsis if truncated."""
    s = str(text).strip()
    return s if len(s) <= max_chars else s[:max_chars - 1] + "…"





# ─────────────────────────────────────────────
#  SHARED STYLE HELPERS
# ─────────────────────────────────────────────

def _styles():
    base = getSampleStyleSheet()
    return {
        "title": ParagraphStyle("MTitle", parent=base["Title"],
                                fontSize=20, textColor=C_NAVY, spaceAfter=4),
        "sub":   ParagraphStyle("MSub",   parent=base["Normal"],
                                fontSize=9,  textColor=C_FOOTER_TEXT, spaceAfter=2),
        "h2":    ParagraphStyle("MH2",    parent=base["Heading2"],
                                fontSize=11, textColor=C_NAVY, spaceBefore=8, spaceAfter=4),
        "h3":    ParagraphStyle("MH3",    parent=base["Heading3"],
                                fontSize=9,  textColor=C_NAVY, spaceBefore=4, spaceAfter=3),
        "body":  ParagraphStyle("MBody",  parent=base["Normal"],  fontSize=7, leading=9),
        "cell":  ParagraphStyle("MCell",  parent=base["Normal"],  fontSize=7),
        "hdr":   ParagraphStyle("MHdr",   parent=base["Normal"],  fontSize=7,
                                textColor=C_WHITE),
        "ok":    ParagraphStyle("MOk",    parent=base["Normal"],  fontSize=9,
                                textColor=C_OK, spaceAfter=6),
        "footer":ParagraphStyle("MFooter",parent=base["Normal"],  fontSize=7,
                                textColor=C_FOOTER_TEXT),
        "toc_entry": ParagraphStyle("MToc", parent=base["Normal"], fontSize=9,
                                    leading=14, textColor=C_NAVY),
        "toc_page":  ParagraphStyle("MTocPg", parent=base["Normal"], fontSize=9,
                                    leading=14, textColor=C_FOOTER_TEXT),
    }


def _std_table_style(header_bg=None) -> TableStyle:
    bg = header_bg or C_NAVY
    return TableStyle([
        ("BACKGROUND",     (0, 0), (-1, 0),  bg),
        ("TEXTCOLOR",      (0, 0), (-1, 0),  C_WHITE),
        ("FONTNAME",       (0, 0), (-1, 0),  "Helvetica-Bold"),
        ("FONTSIZE",       (0, 0), (-1, 0),  7),
        ("FONTNAME",       (0, 1), (-1, -1), "Helvetica"),
        ("FONTSIZE",       (0, 1), (-1, -1), 7),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1), [C_ROW_ALT, C_WHITE]),
        ("GRID",           (0, 0), (-1, -1), 0.4, C_GRID),
        ("VALIGN",         (0, 0), (-1, -1), "TOP"),
        ("LEFTPADDING",    (0, 0), (-1, -1), 4),
        ("RIGHTPADDING",   (0, 0), (-1, -1), 4),
        ("TOPPADDING",     (0, 0), (-1, -1), 3),
        ("BOTTOMPADDING",  (0, 0), (-1, -1), 3),
    ])


def _platform_banner(title: str, subtitle: str, bg_color) -> Table:
    """Full-width coloured banner that visually separates platform sections."""
    st = getSampleStyleSheet()
    title_style = ParagraphStyle("BannerT", parent=st["Normal"],
                                  fontSize=13, textColor=C_WHITE,
                                  fontName="Helvetica-Bold")
    sub_style   = ParagraphStyle("BannerS", parent=st["Normal"],
                                  fontSize=8, textColor=colors.HexColor("#BDC3C7"))
    data = [[
        Paragraph(title,    title_style),
        Paragraph(subtitle, sub_style),
    ]]
    tbl = Table(data, colWidths=[USABLE_W * 0.65, USABLE_W * 0.35])
    tbl.setStyle(TableStyle([
        ("BACKGROUND",    (0, 0), (-1, -1), bg_color),
        ("LEFTPADDING",   (0, 0), (-1, -1), 10),
        ("RIGHTPADDING",  (0, 0), (-1, -1), 10),
        ("TOPPADDING",    (0, 0), (-1, -1), 8),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 8),
        ("VALIGN",        (0, 0), (-1, -1), "MIDDLE"),
        ("ALIGN",         (1, 0), (1,  0),  "RIGHT"),
    ]))
    return tbl


# ─────────────────────────────────────────────
#  PAGE HEADER / FOOTER  (drawn on every page)
# ─────────────────────────────────────────────

def _make_on_page(ts_str: str):
    def _on_page(canvas, doc):
        canvas.saveState()
        # Header bar
        canvas.setFillColor(C_NAVY)
        canvas.rect(0, PAGE_H - 22 * mm, PAGE_W, 22 * mm, fill=1, stroke=0)
        canvas.setFont("Helvetica-Bold", 13)
        canvas.setFillColor(C_WHITE)
        canvas.drawString(MARGIN, PAGE_H - 14 * mm, "Contact Centre Health Check Report")
        canvas.setFont("Helvetica", 8)
        canvas.drawRightString(PAGE_W - MARGIN, PAGE_H - 14 * mm, ts_str)
        # Footer
        canvas.setFont("Helvetica", 7)
        canvas.setFillColor(C_FOOTER_TEXT)
        canvas.drawString(MARGIN, 8 * mm, "Altron Group  |  Contact Centre Health Check  |  Confidential")
        canvas.drawRightString(PAGE_W - MARGIN, 8 * mm, f"Page {doc.page}")
        canvas.restoreState()
    return _on_page


# ─────────────────────────────────────────────
#  SEVERITY HELPERS  (shared between AC + GC)
# ─────────────────────────────────────────────

_AC_SEV_COLORS = {
    "critical": C_CRITICAL,
    "major":    C_MAJOR,
    "minor":    C_MINOR,
    "warning":  C_WARNING_AC,
    "unknown":  C_UNKNOWN,
}
_GC_SEV_COLORS = {
    "ok":       C_OK,
    "warning":  C_MAJOR,
    "critical": C_CRITICAL,
    "disabled": C_DISABLED,
    "unknown":  C_UNKNOWN,
}
_AC_SEV_ORDER = {"critical": 0, "major": 1, "minor": 2, "warning": 3, "unknown": 4}
_AC_SEV_FIELDS = ("severity", "Severity", "alarmSeverity", "AlarmSeverity",
                  "level", "Level", "alarmLevel")


def _ac_severity(alarm: dict) -> str:
    raw = next((alarm[f] for f in _AC_SEV_FIELDS if f in alarm), "unknown")
    return str(raw).strip().lower()


def _sev_cell(text: str, color, st) -> Paragraph:
    hex_c = color.hexval() if hasattr(color, "hexval") else "#888888"
    return Paragraph(
        f'<font color="#{hex_c[2:].upper()}"><b>{text}</b></font>', st
    )


# ─────────────────────────────────────────────
#  SECTION 1 — COMBINED ISSUES PAGE
# ─────────────────────────────────────────────

def _build_issues_page(story: list, ac_data: dict, gc_data: dict, st: dict) -> None:
    """
    Page 1: two clearly separated issue blocks.
    AudioCodes block: alarms of any severity (active only).
    Genesys block:    offline edges + out-of-service trunks.
    """
    story.append(Paragraph("Combined Issues Summary", st["title"]))
    story.append(HRFlowable(width="100%", thickness=1.5, color=C_NAVY, spaceAfter=8))

    # ── AudioCodes Issues ─────────────────────────────────
    story.append(_platform_banner(
        "AudioCodes  —  Active Alarm Issues",
        "Devices with one or more active alarms",
        C_AC_BANNER,
    ))
    story.append(Spacer(1, 4 * mm))

    active_results = ac_data.get("active_results", [])
    device_country = ac_data.get("device_country", {})
    # Issues summary shows Critical alarms only — full detail in the AC section
    ac_issues = [
        d for d in active_results
        if any(_ac_severity(a) == "critical" for a in d.get("alarms", []))
    ]

    if not ac_issues:
        story.append(Paragraph("&#10003;  No critical active alarms across all AudioCodes devices.", st["ok"]))
    else:
        hdr_st, cell_st = st["hdr"], st["cell"]
        # Accent col (4pt) + data columns
        rows = [[
            "",                                          # accent col header
            Paragraph("<b>Device</b>",      hdr_st),
            Paragraph("<b>Country</b>",     hdr_st),
            Paragraph("<b>Severity</b>",    hdr_st),
            Paragraph("<b>Description</b>", hdr_st),
            Paragraph("<b>Source</b>",      hdr_st),
            Paragraph("<b>Date/Time</b>",   hdr_st),
        ]]
        tbl_style = _std_table_style(C_AC_SECTION)
        tbl_style.add("LEFTPADDING",  (0, 0), (0, -1), 0)
        tbl_style.add("RIGHTPADDING", (0, 0), (0, -1), 0)
        # Compact padding for summary — 1pt top/bottom instead of default 3pt
        tbl_style.add("TOPPADDING",    (0, 1), (-1, -1), 1)
        tbl_style.add("BOTTOMPADDING", (0, 1), (-1, -1), 1)
        row_i      = 1
        first_row_per_device = {}   # ip -> first row index, for top-border grouping

        for device in ac_issues:
            ip      = device["ip"]
            country = device_country.get(ip, "")
            critical_alarms = [
                a for a in device["alarms"] if _ac_severity(a) == "critical"
            ]
            marked  = _mark_child_alarms(
                sorted(critical_alarms,
                       key=lambda a: _AC_SEV_ORDER.get(_ac_severity(a), 99))
            )
            first_row_per_device[ip] = row_i
            first_in_device = True

            for alarm in marked:
                sev      = _ac_severity(alarm)
                ts       = _ac_fmt_ts(alarm)
                is_child = alarm.get("_is_child", False)
                par_sev  = alarm.get("_parent_severity", sev)
                accent   = _accent_cell(par_sev) if is_child else _empty_accent_cell()
                # Truncate description to keep rows single-line in summary
                raw_desc = ("    " if is_child else "") + str(alarm.get("description", ""))
                desc_txt = _trunc(raw_desc, 62)

                # Device and country only shown on the first alarm row per device
                ip_cell      = Paragraph(ip,      cell_st) if first_in_device else Paragraph("", cell_st)
                country_cell = Paragraph(country, cell_st) if first_in_device else Paragraph("", cell_st)

                rows.append([
                    accent,
                    ip_cell,
                    country_cell,
                    Paragraph(sev.capitalize(), cell_st),
                    Paragraph(desc_txt,         cell_st),
                    Paragraph(str(alarm.get("source", "")), cell_st),
                    Paragraph(ts,               cell_st),
                ])
                bg = _AC_SEV_COLORS.get(sev, C_UNKNOWN)
                tbl_style.add("BACKGROUND", (3, row_i), (3, row_i), bg)
                tbl_style.add("TEXTCOLOR",  (3, row_i), (3, row_i),
                               C_WHITE if sev in ("critical", "major") else C_NAVY)
                # Thin top border to visually separate device groups
                if first_in_device and row_i > 1:
                    tbl_style.add("LINEABOVE", (0, row_i), (-1, row_i),
                                   0.8, colors.HexColor("#BDC3C7"))
                first_in_device = False
                row_i += 1

        cw = [4] + [USABLE_W * w for w in (0.12, 0.09, 0.09, 0.36, 0.17, 0.13)]
        cw[-1] = USABLE_W - sum(cw[:-1])
        story.append(Table(rows, colWidths=cw, style=tbl_style, repeatRows=1))

    story.append(Spacer(1, 8 * mm))

    # ── Genesys Issues ────────────────────────────────────
    story.append(_platform_banner(
        "Genesys Cloud  —  Infrastructure Issues",
        "Offline edges and out-of-service trunks",
        C_GC_BANNER,
    ))
    story.append(Spacer(1, 4 * mm))

    edges  = gc_data.get("edges",  [])
    trunks = gc_data.get("trunks", [])
    gc_edge_issues  = [e for e in edges  if e.get("severity") != "ok"]
    gc_trunk_issues = [t for t in trunks if t.get("severity") == "critical"]

    if not gc_edge_issues and not gc_trunk_issues:
        story.append(Paragraph("&#10003;  All Genesys Cloud edges and trunks are healthy.", st["ok"]))
    else:
        hdr_st, cell_st = st["hdr"], st["cell"]

        # Edge issues sub-table
        if gc_edge_issues:
            story.append(Paragraph("Edge Issues", st["h3"]))
            rows = [[
                Paragraph("<b>Name</b>",          hdr_st),
                Paragraph("<b>Online Status</b>",  hdr_st),
                Paragraph("<b>State</b>",          hdr_st),
                Paragraph("<b>Site</b>",           hdr_st),
                Paragraph("<b>Edge Group</b>",     hdr_st),
            ]]
            tbl_style = _std_table_style(C_GC_SECTION)
            for row_i, e in enumerate(sorted(gc_edge_issues, key=lambda x: x["name"]), 1):
                clr = _GC_SEV_COLORS.get(e["severity"], C_UNKNOWN)
                rows.append([
                    Paragraph(e["name"],                     cell_st),
                    Paragraph(e.get("online_status") or "--", cell_st),
                    Paragraph(e.get("state") or "--",         cell_st),
                    Paragraph(e.get("site") or "--",          cell_st),
                    Paragraph(e.get("edge_group") or "--",    cell_st),
                ])
                tbl_style.add("BACKGROUND", (1, row_i), (1, row_i), clr)
                tbl_style.add("TEXTCOLOR",  (1, row_i), (1, row_i), C_WHITE)
            cw = [USABLE_W * w for w in (0.32, 0.16, 0.14, 0.20, 0.18)]
            story.append(Table(rows, colWidths=cw, style=tbl_style, repeatRows=1))
            story.append(Spacer(1, 4 * mm))

        # Trunk issues sub-table
        if gc_trunk_issues:
            story.append(Paragraph("Trunk Issues", st["h3"]))
            rows = [[
                Paragraph("<b>Trunk Name</b>",  hdr_st),
                Paragraph("<b>Edge</b>",        hdr_st),
                Paragraph("<b>Status</b>",      hdr_st),
            ]]
            tbl_style = _std_table_style(C_GC_SECTION)
            for row_i, t in enumerate(sorted(gc_trunk_issues, key=lambda x: x["name"]), 1):
                clr = _GC_SEV_COLORS.get(t["severity"], C_UNKNOWN)
                rows.append([
                    Paragraph(t["name"],                       cell_st),
                    Paragraph(t.get("edge_name") or "--",      cell_st),
                    Paragraph(t.get("op_status_raw") or "--",  cell_st),
                ])
                tbl_style.add("BACKGROUND", (2, row_i), (2, row_i), clr)
                tbl_style.add("TEXTCOLOR",  (2, row_i), (2, row_i), C_WHITE)
            cw = [USABLE_W * w for w in (0.48, 0.36, 0.16)]
            story.append(Table(rows, colWidths=cw, style=tbl_style, repeatRows=1))

    # ── Unreachable AudioCodes Devices ──────────────────
    active_results = ac_data.get("active_results", [])
    unreachable    = [d for d in active_results if d.get("status") != "connected"]
    device_country = ac_data.get("device_country", {})

    if unreachable:
        story.append(Spacer(1, 6 * mm))
        story.append(_platform_banner(
            "AudioCodes  —  Unreachable Devices",
            f"{len(unreachable)} device(s) could not be contacted",
            colors.HexColor("#922B21"),   # dark red
        ))
        story.append(Spacer(1, 4 * mm))

        hdr_st, cell_st = st["hdr"], st["cell"]
        rows = [[
            Paragraph("<b>IP Address</b>", hdr_st),
            Paragraph("<b>Country</b>",    hdr_st),
            Paragraph("<b>Reason</b>",     hdr_st),
        ]]
        tbl_style = _std_table_style(colors.HexColor("#C0392B"))
        for row_i, d in enumerate(unreachable, 1):
            ip     = d["ip"]
            reason = d.get("error") or d.get("status") or "Unknown"
            # Truncate long connection error strings for readability
            if len(str(reason)) > 120:
                reason = str(reason)[:117] + "..."
            rows.append([
                Paragraph(ip,                          cell_st),
                Paragraph(device_country.get(ip, ""), cell_st),
                Paragraph(str(reason),                 cell_st),
            ])
            tbl_style.add("BACKGROUND", (0, row_i), (0, row_i),
                           colors.HexColor("#FDECEA"))
        cw = [USABLE_W * w for w in (0.18, 0.12, 0.70)]
        story.append(Table(rows, colWidths=cw, style=tbl_style, repeatRows=1))

    story.append(PageBreak())


# ─────────────────────────────────────────────
#  SECTION 2 — TABLE OF CONTENTS
# ─────────────────────────────────────────────

# ─────────────────────────────────────────────
#  AUDIOCODES TIMESTAMP HELPER
# ─────────────────────────────────────────────

_AC_TS_FIELDS = ("time", "Time", "timestamp", "Timestamp", "date", "Date",
                 "eventTime", "EventTime", "raisedTime", "clearedTime")


def _ac_fmt_ts(alarm: dict) -> str:
    from datetime import datetime, timezone
    if alarm.get("_no_timestamp"):
        return "Unknown date"
    for field in _AC_TS_FIELDS:
        raw = alarm.get(field)
        if raw is None:
            continue
        try:
            if isinstance(raw, (int, float)):
                dt = datetime.fromtimestamp(raw, tz=timezone.utc)
                return dt.strftime("%Y-%m-%d %H:%M")
            s = str(raw).strip().rstrip("Z")
            for fmt in ("%Y-%m-%dT%H:%M:%S.%f", "%Y-%m-%dT%H:%M:%S",
                        "%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
                try:
                    return datetime.strptime(s, fmt).strftime("%Y-%m-%d %H:%M")
                except ValueError:
                    continue
        except Exception:
            continue
    return "Unknown date"

# ─────────────────────────────────────────────
#  ALARM GROUPING HELPER
# ─────────────────────────────────────────────

_TRUNK_ALARM_KEYWORDS  = ("los", "lof", "ais", "rai", "trunk los", "trunk lof",
                           "trunk ais", "trunk rai")
_DCHANNEL_KEYWORDS     = ("d-channel",)

def _is_trunk_alarm(alarm: dict) -> bool:
    desc = str(alarm.get("description", "")).lower()
    return any(kw in desc for kw in _TRUNK_ALARM_KEYWORDS)

def _is_dchannel_alarm(alarm: dict) -> bool:
    desc = str(alarm.get("description", "")).lower()
    return any(kw in desc for kw in _DCHANNEL_KEYWORDS)

def _trunk_source_key(alarm: dict) -> str:
    """Normalise source to trunk identity e.g. 'interface#0/trunk#0'."""
    return str(alarm.get("source", "")).lower().strip()

def _mark_child_alarms(alarms: list[dict]) -> list[dict]:
    """
    For each device alarm list, mark D-Channel alarms as children when
    a trunk alarm (LOS/LOF/AIS/RAI) exists on the same trunk source.
    Returns a new list with _is_child and _parent_severity keys added.
    """
    # Build map: trunk_source → worst severity of trunk alarm on that source
    trunk_sources: dict[str, str] = {}
    for alarm in alarms:
        if _is_trunk_alarm(alarm):
            src = _trunk_source_key(alarm)
            if src:
                sev = _ac_severity(alarm)
                # Keep the worst severity
                existing = trunk_sources.get(src, "unknown")
                if _AC_SEV_ORDER.get(sev, 99) < _AC_SEV_ORDER.get(existing, 99):
                    trunk_sources[src] = sev

    result = []
    for alarm in alarms:
        alarm = dict(alarm)  # shallow copy — don't mutate original
        if _is_dchannel_alarm(alarm):
            src = _trunk_source_key(alarm)
            if src in trunk_sources:
                alarm["_is_child"]       = True
                alarm["_parent_severity"] = trunk_sources[src]
            else:
                alarm["_is_child"] = False
        else:
            alarm["_is_child"] = False
        result.append(alarm)
    return result


def _accent_cell(severity: str) -> Table:
    """
    Returns a 4pt-wide single-cell Table containing a coloured rect —
    used as the left-border accent for child rows.
    """
    clr  = _AC_SEV_COLORS.get(severity, C_UNKNOWN)
    data = [[""]]
    tbl  = Table(data, colWidths=[4], rowHeights=[12])
    tbl.setStyle(TableStyle([
        ("BACKGROUND",    (0, 0), (0, 0), clr),
        ("LEFTPADDING",   (0, 0), (-1, -1), 0),
        ("RIGHTPADDING",  (0, 0), (-1, -1), 0),
        ("TOPPADDING",    (0, 0), (-1, -1), 0),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 0),
    ]))
    return tbl


def _empty_accent_cell() -> Table:
    """Transparent placeholder for parent/standalone rows."""
    data = [[""]]
    tbl  = Table(data, colWidths=[4], rowHeights=[12])
    tbl.setStyle(TableStyle([
        ("LEFTPADDING",   (0, 0), (-1, -1), 0),
        ("RIGHTPADDING",  (0, 0), (-1, -1), 0),
        ("TOPPADDING",    (0, 0), (-1, -1), 0),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 0),
    ]))
    return tbl


# ─────────────────────────────────────────────
#  SECTION 3 — AUDIOCODES DETAIL SECTIONS
# ─────────────────────────────────────────────

def _build_ac_device_overview(story: list, ac_data: dict, st: dict) -> None:
    """AC: Device & Trunk Overview table."""
    from reportlab.platypus import Table as RLTable, TableStyle as RLTS

    story.append(Paragraph("Device &amp; Trunk Overview", st["h2"]))
    story.append(Spacer(1, 3 * mm))

    active_results  = ac_data.get("active_results",  [])
    device_statuses = ac_data.get("device_statuses", {})
    device_country  = ac_data.get("device_country",  {})

    hdr_st, cell_st = st["hdr"], st["cell"]

    rows = [[
        Paragraph("<b>IP Address</b>",  hdr_st),
        Paragraph("<b>Country</b>",     hdr_st),
        Paragraph("<b>Active</b>",      hdr_st),
        Paragraph("<b>Type</b>",        hdr_st),
        Paragraph("<b>Version</b>",     hdr_st),
        Paragraph("<b>Serial No</b>",   hdr_st),
        Paragraph("<b>Uptime</b>",      hdr_st),
        Paragraph("<b>Trunks</b>",      hdr_st),
    ]]
    tbl_style = _std_table_style(C_AC_BANNER)

    for row_i, device in enumerate(active_results, start=1):
        ip     = device["ip"]
        status = device_statuses.get(ip, {})
        country     = device_country.get(ip, "")
        prod_type   = status.get("productType", "")
        version     = status.get("versionID", "")
        serial      = status.get("serialNumber", "")
        uptime      = status.get("systemUpTime", "")
        op_state    = status.get("operationalState", "")
        is_connected = device["status"] == "connected"

        if op_state:
            is_active  = op_state.upper() == "UNLOCKED"
        else:
            is_active  = is_connected
        active_str = "True" if is_active else "False"

        # Trunk badges
        from healthcheck.audiocodes_alarm_reporter import infer_trunk_status
        trunks = infer_trunk_status(ip, device["alarms"])

        trunk_cells, trunk_colors = [], []
        t_cell_st = getSampleStyleSheet()["Normal"]
        for t in trunks:
            tc = (colors.HexColor("#27ae60") if t["status"] == "active" else
                  colors.HexColor("#c0392b") if t["status"] == "alarm" else
                  colors.HexColor("#e67e22"))
            trunk_cells.append(
                Paragraph(f"<b>{t['trunk']}</b>",
                          ParagraphStyle(f"tr{t['trunk']}_m", parent=t_cell_st,
                                         fontSize=7, alignment=1, textColor=C_WHITE))
            )
            trunk_colors.append(tc)

        t_inner = TableStyle([
            ("LEFTPADDING",  (0,0),(-1,-1), 3), ("RIGHTPADDING", (0,0),(-1,-1), 3),
            ("TOPPADDING",   (0,0),(-1,-1), 2), ("BOTTOMPADDING",(0,0),(-1,-1), 2),
        ])
        for ti, tc in enumerate(trunk_colors):
            t_inner.add("BACKGROUND", (ti, 0), (ti, 0), tc)
        trunk_col_w = (USABLE_W * 0.12) / max(len(trunks), 1)
        trunk_cell  = Table([trunk_cells], colWidths=[trunk_col_w] * len(trunks),
                             style=t_inner)

        rows.append([
            Paragraph(ip,         cell_st),
            Paragraph(country,    cell_st),
            Paragraph(active_str, cell_st),
            Paragraph(prod_type,  cell_st),
            Paragraph(version,    cell_st),
            Paragraph(serial,     cell_st),
            Paragraph(uptime,     cell_st),
            trunk_cell,
        ])
        active_bg = colors.HexColor("#eafaf1") if is_active else colors.HexColor("#fdecea")
        tbl_style.add("BACKGROUND", (2, row_i), (2, row_i), active_bg)
        tbl_style.add("TEXTCOLOR",  (2, row_i), (2, row_i), C_NAVY)

    cw = [USABLE_W * w for w in (0.12, 0.09, 0.07, 0.13, 0.13, 0.14, 0.10, 0.12)]
    cw[-1] = USABLE_W - sum(cw[:-1])
    story.append(Table(rows, colWidths=cw, style=tbl_style, repeatRows=1))

    legend = ParagraphStyle("legend_m", parent=getSampleStyleSheet()["Normal"],
                             fontSize=7, textColor=C_FOOTER_TEXT, spaceAfter=8)
    story.append(Paragraph(
        "<b>Trunk badge colours:</b>  "
        "<font color='#27ae60'>■</font> Active &nbsp;&nbsp;"
        "<font color='#e67e22'>■</font> Disabled &nbsp;&nbsp;"
        "<font color='#c0392b'>■</font> Alarm",
        legend))
    story.append(Spacer(1, 4 * mm))


def _build_ac_active_alarms(story: list, ac_data: dict, st: dict) -> None:
    """AC: Active Alarms table."""
    story.append(Paragraph("Active Alarms", st["h2"]))
    story.append(Spacer(1, 3 * mm))

    active_results = ac_data.get("active_results", [])
    device_country = ac_data.get("device_country", {})
    hdr_st, cell_st = st["hdr"], st["cell"]

    rows = [[
        "",                                              # accent col
        Paragraph("<b>Device</b>",      hdr_st),
        Paragraph("<b>Country</b>",     hdr_st),
        Paragraph("<b>Severity</b>",    hdr_st),
        Paragraph("<b>Description</b>", hdr_st),
        Paragraph("<b>Source</b>",      hdr_st),
        Paragraph("<b>ID</b>",          hdr_st),
        Paragraph("<b>Date/Time</b>",   hdr_st),
    ]]
    tbl_style = _std_table_style(C_AC_SECTION)
    tbl_style.add("LEFTPADDING",  (0, 0), (0, -1), 0)
    tbl_style.add("RIGHTPADDING", (0, 0), (0, -1), 0)
    any_alarms = False
    row_i = 1

    for device in active_results:
        ip      = device["ip"]
        country = device_country.get(ip, "")
        if not device["alarms"]:
            continue
        any_alarms = True
        marked = _mark_child_alarms(
            sorted(device["alarms"],
                   key=lambda a: _AC_SEV_ORDER.get(_ac_severity(a), 99))
        )
        for alarm in marked:
            sev      = _ac_severity(alarm)
            is_child = alarm.get("_is_child", False)
            par_sev  = alarm.get("_parent_severity", sev)
            accent   = _accent_cell(par_sev) if is_child else _empty_accent_cell()
            desc_txt = ("    " if is_child else "") + str(alarm.get("description", ""))
            rows.append([
                accent,
                Paragraph(ip,                  cell_st),
                Paragraph(country,             cell_st),
                Paragraph(sev.capitalize(),    cell_st),
                Paragraph(desc_txt,            cell_st),
                Paragraph(str(alarm.get("source", "")), cell_st),
                Paragraph(str(alarm.get("id", "")),     cell_st),
                Paragraph(_ac_fmt_ts(alarm),   cell_st),
            ])
            bg = _AC_SEV_COLORS.get(sev, C_UNKNOWN)
            tbl_style.add("BACKGROUND", (3, row_i), (3, row_i), bg)
            tbl_style.add("TEXTCOLOR",  (3, row_i), (3, row_i),
                           C_WHITE if sev in ("critical", "major") else C_NAVY)
            row_i += 1

    if any_alarms:
        cw = [4] + [USABLE_W * w for w in (0.11, 0.09, 0.09, 0.31, 0.17, 0.05, 0.14)]
        cw[-1] = USABLE_W - sum(cw[:-1])
        story.append(Table(rows, colWidths=cw, style=tbl_style, repeatRows=1))
    else:
        story.append(Paragraph("No active alarms across all devices.", st["ok"]))
    story.append(Spacer(1, 4 * mm))


def _build_ac_historical_alarms(story: list, ac_data: dict, st: dict,
                                  history_days: int = 7) -> None:
    """AC: Historical Alarms table."""
    from datetime import datetime, timezone
    story.append(Paragraph(f"Historical Alarms  (last {history_days} days)", st["h2"]))
    story.append(Spacer(1, 3 * mm))

    history_results = ac_data.get("history_results", [])
    device_country  = ac_data.get("device_country",  {})
    hdr_st, cell_st = st["hdr"], st["cell"]

    hist_hdr_bg = colors.HexColor("#4A235A")
    rows = [[
        Paragraph("<b>Device</b>",      hdr_st),
        Paragraph("<b>Country</b>",     hdr_st),
        Paragraph("<b>Severity</b>",    hdr_st),
        Paragraph("<b>Description</b>", hdr_st),
        Paragraph("<b>Source</b>",      hdr_st),
        Paragraph("<b>ID</b>",          hdr_st),
        Paragraph("<b>Date/Time</b>",   hdr_st),
    ]]
    tbl_style = _std_table_style(hist_hdr_bg)
    tbl_style.add("ROWBACKGROUNDS", (0, 1), (-1, -1),
                   [colors.HexColor("#F5EEF8"), C_WHITE])

    any_history = False
    row_i = 1

    def _sort_key(a):
        ts = _ac_fmt_ts(a)
        return ts or ""

    for device in history_results:
        ip      = device["ip"]
        country = device_country.get(ip, "")
        if not device["alarms"]:
            continue
        any_history = True
        for alarm in sorted(device["alarms"], key=_sort_key, reverse=True):
            sev = _ac_severity(alarm)
            rows.append([
                Paragraph(ip,                                 cell_st),
                Paragraph(country,                            cell_st),
                Paragraph(sev.capitalize(),                   cell_st),
                Paragraph(str(alarm.get("description", "")), cell_st),
                Paragraph(str(alarm.get("source", "")),       cell_st),
                Paragraph(str(alarm.get("id", "")),           cell_st),
                Paragraph(_ac_fmt_ts(alarm),                  cell_st),
            ])
            bg = _AC_SEV_COLORS.get(sev, C_UNKNOWN)
            tbl_style.add("BACKGROUND", (2, row_i), (2, row_i), bg)
            tbl_style.add("TEXTCOLOR",  (2, row_i), (2, row_i),
                           C_WHITE if sev in ("critical", "major") else C_NAVY)
            row_i += 1

    if any_history:
        cw = [USABLE_W * w for w in (0.12, 0.09, 0.09, 0.32, 0.18, 0.05, 0.15)]
        story.append(Table(rows, colWidths=cw, style=tbl_style, repeatRows=1))
    else:
        story.append(Paragraph(
            f"No historical alarms in the last {history_days} days.", st["ok"]))
    story.append(Spacer(1, 4 * mm))


# ─────────────────────────────────────────────
#  SECTION 4 — GENESYS DETAIL SECTIONS
# ─────────────────────────────────────────────

def _build_gc_edge_overview(story: list, gc_data: dict, st: dict) -> None:
    """GC: Edge Overview table."""
    story.append(Paragraph("Edge Overview", st["h2"]))

    edges = gc_data.get("edges", [])
    total        = len(edges)
    connected    = sum(1 for e in edges if e["severity"] == "ok")
    disconnected = sum(1 for e in edges if e["severity"] == "critical")
    unknown      = total - connected - disconnected

    story.append(Paragraph(
        f"Total: <b>{total}</b>  |  Connected: <b>{connected}</b>  |  "
        f"Disconnected: <b>{disconnected}</b>  |  Unknown: <b>{unknown}</b>",
        st["body"]))
    story.append(Spacer(1, 3 * mm))

    if not edges:
        story.append(Paragraph("No Edge data retrieved.", st["body"]))
        return

    hdr_st, cell_st = st["hdr"], st["cell"]
    rows = [[
        Paragraph("<b>Name</b>",           hdr_st),
        Paragraph("<b>Online Status</b>",  hdr_st),
        Paragraph("<b>State</b>",          hdr_st),
        Paragraph("<b>Edge Group</b>",     hdr_st),
        Paragraph("<b>Site</b>",           hdr_st),
        Paragraph("<b>Version</b>",        hdr_st),
        Paragraph("<b>Update</b>",         hdr_st),
        Paragraph("<b>Storage</b>",        hdr_st),
    ]]
    tbl_style = _std_table_style(C_GC_SECTION)

    for row_i, e in enumerate(sorted(edges, key=lambda x: x["name"]), 1):
        clr   = _GC_SEV_COLORS.get(e["severity"], C_UNKNOWN)
        hex_c = f"#{clr.hexval()[2:].upper()}"

        ua    = e.get("update_available")
        ua_txt = "Yes" if ua is True else "No" if ua is False else "--"
        ua_clr = "#E67E22" if ua is True else "#27AE60" if ua is False else "#888888"

        sp    = e.get("storage_pct")
        if sp is not None:
            sp_clr = "#C0392B" if sp >= 90 else "#E67E22" if sp >= 75 else "#27AE60"
            sp_txt = f"{sp:.1f}%"
        else:
            sp_clr, sp_txt = "#888888", "--"

        rows.append([
            Paragraph(e["name"],                                               cell_st),
            Paragraph(f'<font color="{hex_c}"><b>'
                      f'{e.get("online_status") or "UNKNOWN"}</b></font>',     cell_st),
            Paragraph(e.get("state") or "--",                                  cell_st),
            Paragraph(e.get("edge_group") or "--",                             cell_st),
            Paragraph(e.get("site") or "--",                                   cell_st),
            Paragraph(e.get("software_version") or "--",                       cell_st),
            Paragraph(f'<font color="{ua_clr}"><b>{ua_txt}</b></font>',        cell_st),
            Paragraph(f'<font color="{sp_clr}"><b>{sp_txt}</b></font>',        cell_st),
        ])

    cw = [USABLE_W * w for w in (0.22, 0.13, 0.10, 0.17, 0.14, 0.12, 0.06, 0.06)]
    story.append(Table(rows, colWidths=cw, style=tbl_style, repeatRows=1))
    story.append(Spacer(1, 4 * mm))


def _build_gc_trunk_health(story: list, gc_data: dict, st: dict) -> None:
    """GC: Trunk Health table."""
    story.append(Paragraph("Trunk Health", st["h2"]))

    trunks = gc_data.get("trunks", [])
    total  = len(trunks)
    up     = sum(1 for t in trunks if t["severity"] == "ok")
    down   = sum(1 for t in trunks if t["severity"] == "critical")
    other  = total - up - down

    story.append(Paragraph(
        f"Total: <b>{total}</b>  |  In Service: <b>{up}</b>  |  "
        f"Out Of Service: <b>{down}</b>  |  Other: <b>{other}</b>",
        st["body"]))
    story.append(Spacer(1, 3 * mm))

    if not trunks:
        story.append(Paragraph("No Trunk data retrieved.", st["body"]))
        return

    hdr_st, cell_st = st["hdr"], st["cell"]
    rows = [[
        Paragraph("<b>External Trunk Name</b>", hdr_st),
        Paragraph("<b>Edge Name</b>",           hdr_st),
        Paragraph("<b>Status</b>",              hdr_st),
        Paragraph("<b>Recording</b>",           hdr_st),
    ]]
    tbl_style = _std_table_style(C_GC_SECTION)

    for row_i, t in enumerate(sorted(trunks, key=lambda x: x["name"]), 1):
        clr   = _GC_SEV_COLORS.get(t["severity"], C_UNKNOWN)
        hex_c = f"#{clr.hexval()[2:].upper()}"
        rec   = t.get("recording_enabled")
        rec_txt = "True" if rec is True else "False" if rec is False else "--"
        rec_clr = "#27AE60" if rec is True else "#C0392B" if rec is False else "#888888"

        rows.append([
            Paragraph(t["name"],                                                cell_st),
            Paragraph(t.get("edge_name") or "--",                               cell_st),
            Paragraph(f'<font color="{hex_c}"><b>'
                      f'{t.get("op_status_raw") or "--"}</b></font>',           cell_st),
            Paragraph(f'<font color="{rec_clr}"><b>{rec_txt}</b></font>',       cell_st),
        ])

    cw = [USABLE_W * w for w in (0.42, 0.36, 0.14, 0.08)]
    story.append(Table(rows, colWidths=cw, style=tbl_style, repeatRows=1))
    story.append(Spacer(1, 4 * mm))


# ─────────────────────────────────────────────
#  FOOTER NOTE
# ─────────────────────────────────────────────

def _build_footer(story: list, st: dict) -> None:
    story.append(HRFlowable(width="100%", thickness=0.5,
                             color=C_GRID, spaceBefore=8))
    story.append(Paragraph(
        "This report was generated automatically by the Altron Health Check application.  "
        "AudioCodes severity coding: Critical (red), Major (orange), Minor (yellow), Warning (blue).  "
        "Genesys severity coding: Online (green), Offline (red), Unknown (grey).",
        st["footer"]))


# ─────────────────────────────────────────────
#  PUBLIC API
# ─────────────────────────────────────────────

def build(
    config:    dict,
    ac_data:   dict | None,
    gc_data:   dict | None,
    report_ts: datetime,
) -> str:
    """
    Build the merged PDF report.

    Parameters
    ----------
    config     : full application config dict
    ac_data    : dict returned by audiocodes_alarm_reporter.collect()
                 or None if AudioCodes polling failed
    gc_data    : dict returned by genesys_health_reporter.collect()
                 or None if Genesys polling failed
    report_ts  : datetime of the report run (used in header + filename)

    Returns the output PDF file path.
    """
    report_dir = config.get("reporting", {}).get("reportDir", ".")
    prefix     = config.get("reporting", {}).get("filenamePrefix", "healthcheck_report")
    ts_str     = report_ts.strftime("%Y%m%d_%H%M%S")
    output_path = os.path.join(report_dir, f"{prefix}_{ts_str}.pdf")
    os.makedirs(report_dir, exist_ok=True)

    ts_display = report_ts.strftime("%Y-%m-%d  %H:%M:%S UTC")
    log.info("Building merged PDF → %s", output_path)

    # Inject device_country into ac_data so section builders can use it
    if ac_data is not None:
        ac_data["device_country"] = (
            config.get("audiocodes", {}).get("deviceCountry", {})
        )

    # Stub data for failed modules — sections render gracefully with empty data
    _empty_ac = {"active_results": [], "history_results": [],
                  "device_statuses": {}, "device_country": {}}
    _empty_gc = {"edges": [], "trunks": []}
    _ac = ac_data if ac_data is not None else _empty_ac
    _gc = gc_data if gc_data is not None else _empty_gc

    history_days = config.get("audiocodes", {}).get("historyDays", 7)

    # ── Build story ───────────────────────────────────────
    st    = _styles()
    story = []

    # Page 1: combined issues
    _build_issues_page(story, _ac, _gc, st)

    # AudioCodes platform banner + sections
    story.append(_platform_banner(
        "AudioCodes  —  Detailed Report",
        "VoIP / Networking Device Health",
        C_AC_BANNER,
    ))
    story.append(Spacer(1, 4 * mm))

    if ac_data is None:
        story.append(Paragraph(
            "AudioCodes data unavailable — the module failed during this run. "
            "Check the application log for details.",
            ParagraphStyle("warn", parent=getSampleStyleSheet()["Normal"],
                           fontSize=9, textColor=C_CRITICAL)))
    else:
        _build_ac_device_overview(story, _ac, st)
        _build_ac_active_alarms(story, _ac, st)
        _build_ac_historical_alarms(story, _ac, st, history_days)

    story.append(PageBreak())

    # Genesys platform banner + sections
    story.append(_platform_banner(
        "Genesys Cloud  —  Detailed Report",
        "Contact Centre Infrastructure Health",
        C_GC_BANNER,
    ))
    story.append(Spacer(1, 4 * mm))

    if gc_data is None:
        story.append(Paragraph(
            "Genesys Cloud data unavailable — the module failed during this run. "
            "Check the application log for details.",
            ParagraphStyle("warn_gc", parent=getSampleStyleSheet()["Normal"],
                           fontSize=9, textColor=C_CRITICAL)))
    else:
        _build_gc_edge_overview(story, _gc, st)
        _build_gc_trunk_health(story, _gc, st)

    _build_footer(story, st)

    # ── Render ────────────────────────────────────────────
    doc = SimpleDocTemplate(
        output_path,
        pagesize=A4,
        leftMargin=MARGIN, rightMargin=MARGIN,
        topMargin=24 * mm, bottomMargin=14 * mm,
        title="Contact Centre Health Check Report",
        author="Altron Group — Automated Reporter",
    )
    on_page = _make_on_page(ts_display)

    try:
        doc.build(story, onFirstPage=on_page, onLaterPages=on_page)
        log.info("Merged PDF saved → %s", output_path)
    except Exception as exc:
        log.error("PDF generation failed: %s", exc, exc_info=True)
        raise

    return output_path

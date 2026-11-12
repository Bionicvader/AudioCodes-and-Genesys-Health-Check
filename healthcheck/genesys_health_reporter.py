"""
Genesys Cloud Health Check Reporter  —  Alpha v1.0
===================================================
Polls Genesys Cloud (eu-west-1) for Edge and Trunk health via the
PureCloudPlatformClientV2 Python SDK, then generates a PDF report
styled identically to the AudioCodes Alarm Reporter.

Designed to be used in two ways:
  1. Imported by main.py  — call run(config) which returns the PDF path.
  2. Run standalone       — python genesys_health_reporter.py (reads its own YAML).

All configuration is passed in via the config dict (genesys: namespace).
Logging and email are handled by the caller (main.py) when used as a module.

Dependencies:
    pip install PureCloudPlatformClientV2 reportlab pyyaml

Auth:
    Client Credentials Grant (non-human / scheduled task).
    Credentials loaded from healthcheck_config.yaml (genesys: namespace).
    Required OAuth scope: telephony:plugin:all
"""

from __future__ import annotations

import logging
import os
import re
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

import yaml
import PureCloudPlatformClientV2
from PureCloudPlatformClientV2.rest import ApiException

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

PAGE_SIZE = 100   # Genesys API max page size

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

# When imported by main.py, the root logger is already configured.
# When run standalone, basicConfig in main() provides a sensible default.
log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Severity normalisation
# ---------------------------------------------------------------------------

_EDGE_ONLINE_STATUS_SEVERITY: dict[str, str] = {
    "CONNECTED":    "ok",
    "ONLINE":       "ok",        # alternate value from some API versions
    "DISCONNECTED": "critical",
    "OFFLINE":      "critical",  # alternate value from some API versions
    "UNKNOWN":      "unknown",
}
_EDGE_STATE_SEVERITY: dict[str, str] = {
    "ACTIVE":   "ok",
    "INACTIVE": "critical",
    "UNKNOWN":  "unknown",
}

# UUID suffix pattern for trunk display names
_UUID_SUFFIX = re.compile(
    r"\s+[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$",
    re.IGNORECASE,
)


def _edge_severity(online_status: str, state: str) -> str:
    """Resolve edge severity — prefers online_status, falls back to state."""
    if online_status:
        result = _EDGE_ONLINE_STATUS_SEVERITY.get(online_status.upper())
        if result:
            return result
    if state:
        result = _EDGE_STATE_SEVERITY.get(state.upper())
        if result:
            return result
    return "unknown"


def _trunk_severity(in_service: bool | None, enabled: bool | None) -> tuple[str, str]:
    """Return (op_status_raw, severity) for a trunk."""
    if enabled is False:
        return "DISABLED", "disabled"
    if in_service is True:
        return "IN SERVICE", "ok"
    if in_service is False:
        return "OUT OF SERVICE", "critical"
    return "UNKNOWN", "unknown"


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

_STANDALONE_CONFIG = "healthcheck_config.yaml"

def _load_standalone_config(config_file: str = _STANDALONE_CONFIG) -> dict:
    """Load the shared YAML config when running standalone. Exits on error."""
    if not os.path.exists(config_file):
        log.error("Config file not found: %s", config_file)
        sys.exit(1)
    try:
        with open(config_file, encoding="utf-8") as f:
            config = yaml.safe_load(f)
        log.info("Config loaded from %s", config_file)
        return config
    except yaml.YAMLError as exc:
        log.error("Failed to parse %s: %s", config_file, exc)
        sys.exit(1)


# ---------------------------------------------------------------------------
# Authentication
# ---------------------------------------------------------------------------

def authenticate(config: dict) -> PureCloudPlatformClientV2.api_client.ApiClient:
    """
    OAuth2 Client Credentials Grant.
    Sets eu-west-1 region, obtains token, verifies with a lightweight API call.
    Accepts the full application config dict — reads from the genesys: namespace.

    Raises RuntimeError on missing credentials or auth failure. (Previously
    called sys.exit(1), which raised SystemExit — not an Exception — so it
    escaped main.py's per-module handler and stopped the Windows Service.)
    """
    gc_cfg        = config.get("genesys", config)   # fallback to root for compat
    client_id     = gc_cfg.get("clientId",     "")
    client_secret = gc_cfg.get("clientSecret", "")

    if not client_id or not client_secret:
        log.error("clientId / clientSecret missing from config (genesys: namespace).")
        raise RuntimeError("Genesys clientId / clientSecret missing from config")

    region = PureCloudPlatformClientV2.PureCloudRegionHosts.eu_west_1
    PureCloudPlatformClientV2.configuration.host = region.get_api_host()
    log.info("Authenticating with Genesys Cloud (%s) ...", region.get_api_host())

    try:
        api_client = (
            PureCloudPlatformClientV2.api_client.ApiClient()
            .get_client_credentials_token(client_id, client_secret)
        )
        PureCloudPlatformClientV2.AuthorizationApi(api_client).get_authorization_permissions()
        log.info("Authentication successful.")
        return api_client
    except ApiException as exc:
        log.error("Authentication failed -- HTTP %s: %s", exc.status, exc.reason)
        raise RuntimeError(
            f"Genesys Cloud authentication failed -- HTTP {exc.status}: {exc.reason}"
        ) from exc
    except Exception as exc:  # noqa: BLE001
        log.error("Unexpected authentication error: %s", exc)
        raise RuntimeError(f"Unexpected Genesys Cloud authentication error: {exc}") from exc


# ---------------------------------------------------------------------------
# Data fetching
# ---------------------------------------------------------------------------

def fetch_all_edges(api_client: PureCloudPlatformClientV2.api_client.ApiClient) -> list[dict]:
    """Fetch and normalise all Edges with pagination."""
    edge_api = PureCloudPlatformClientV2.TelephonyProvidersEdgeApi(api_client)
    edges    = []
    page_num = 1

    log.info("Fetching Edge list ...")
    while True:
        try:
            result = edge_api.get_telephony_providers_edges(
                page_size=PAGE_SIZE, page_number=page_num,
            )
        except ApiException as exc:
            log.error("Failed to fetch edges (page %d) -- HTTP %s: %s",
                      page_num, exc.status, exc.reason)
            break

        if not result or not result.entities:
            break

        for obj in result.entities:
            online_status = getattr(obj, "online_status", None) or ""
            state         = getattr(obj, "state",         None) or ""

            edge_group = ""
            if getattr(obj, "edge_group", None):
                edge_group = getattr(obj.edge_group, "name", "") or ""

            site = ""
            if getattr(obj, "site", None):
                site = getattr(obj.site, "name", "") or ""

            update_available = None
            try:
                ss = getattr(obj, "software_status", None)
                if ss and getattr(ss, "version", None):
                    latest = getattr(ss.version, "latest_release", None)
                    if latest is not None:
                        update_available = not latest
            except Exception:
                pass

            edges.append({
                "id":               getattr(obj, "id",               "") or "",
                "name":             getattr(obj, "name",             "") or "",
                "online_status":    online_status,
                "state":            state or "UNKNOWN",
                "edge_group":       edge_group,
                "site":             site,
                "software_version": getattr(obj, "software_version", "") or "",
                "update_available": update_available,
                "storage_pct":      None,   # filled by fetch_edge_metrics()
                "severity":         _edge_severity(online_status, state),
            })

        log.info("  Page %d/%d -- %d edge(s) so far",
                 page_num, result.page_count or "?", len(edges))

        if page_num >= (result.page_count or 1):
            break
        page_num += 1

    log.info("Edge fetch complete -- %d edge(s) found.", len(edges))
    return edges


def fetch_edge_metrics(
    api_client: PureCloudPlatformClientV2.api_client.ApiClient,
    edges: list[dict],
) -> None:
    """
    Fetch EdgeMetrics in batches of 100 and merge storage % into edge dicts in-place.
    storage_pct = (total_bytes - available_bytes) / total_bytes * 100
    Summed across all disk partitions per edge.
    """
    if not edges:
        return

    edge_api = PureCloudPlatformClientV2.TelephonyProvidersEdgeApi(api_client)
    id_map   = {e["id"]: e for e in edges if e["id"]}
    ids      = list(id_map.keys())
    batches  = [ids[i:i + 100] for i in range(0, len(ids), 100)]

    log.info("Fetching Edge metrics (%d edge(s), %d batch(es)) ...", len(ids), len(batches))
    for batch in batches:
        try:
            metrics_list = edge_api.get_telephony_providers_edges_metrics(",".join(batch))
        except ApiException as exc:
            log.error("Edge metrics fetch failed -- HTTP %s: %s", exc.status, exc.reason)
            continue

        for metric in (metrics_list or []):
            edge_ref = getattr(metric, "edge", None)
            edge_id  = getattr(edge_ref, "id", None) if edge_ref else None
            if not edge_id or edge_id not in id_map:
                continue
            disks           = getattr(metric, "disks", None) or []
            total_bytes     = sum(getattr(d, "total_bytes",     0) or 0 for d in disks)
            available_bytes = sum(getattr(d, "available_bytes", 0) or 0 for d in disks)
            if total_bytes > 0:
                id_map[edge_id]["storage_pct"] = round(
                    (total_bytes - available_bytes) / total_bytes * 100, 1
                )

    log.info("Edge metrics merge complete.")


def fetch_all_trunks(api_client: PureCloudPlatformClientV2.api_client.ApiClient) -> list[dict]:
    """Fetch and normalise all EXTERNAL Trunks with pagination. PHONE/EDGE types skipped."""
    edge_api = PureCloudPlatformClientV2.TelephonyProvidersEdgeApi(api_client)
    trunks   = []
    page_num = 1

    log.info("Fetching Trunk list ...")
    while True:
        try:
            result = edge_api.get_telephony_providers_edges_trunks(
                page_size=PAGE_SIZE, page_number=page_num,
            )
        except ApiException as exc:
            log.error("Failed to fetch trunks (page %d) -- HTTP %s: %s",
                      page_num, exc.status, exc.reason)
            break

        if not result or not result.entities:
            break

        for obj in result.entities:
            trunk_type = getattr(obj, "trunk_type", "") or ""
            if trunk_type.upper() != "EXTERNAL":
                continue

            op_status_raw, severity = _trunk_severity(
                getattr(obj, "in_service", None),
                getattr(obj, "enabled",    None),
            )

            edge_name = ""
            if getattr(obj, "edge", None):
                edge_name = getattr(obj.edge, "name", "") or ""

            display_name = _UUID_SUFFIX.sub("", getattr(obj, "name", "") or "").strip()

            tb = getattr(obj, "trunk_base", None)
            trunk_base_id = ""
            if tb:
                trunk_base_id = (
                    getattr(tb, "id", None) or
                    (tb.get("id") if isinstance(tb, dict) else "")
                ) or ""

            trunks.append({
                "id":                getattr(obj, "id", "") or "",
                "name":              display_name,
                "trunk_type":        "EXTERNAL",
                "edge_name":         edge_name,
                "op_status_raw":     op_status_raw,
                "severity":          severity,
                "trunk_base_id":     trunk_base_id,
                "recording_enabled": None,   # filled by fetch_trunk_recording()
            })

        log.info("  Page %d/%d -- %d trunk(s) so far",
                 page_num, result.page_count or "?", len(trunks))

        if page_num >= (result.page_count or 1):
            break
        page_num += 1

    log.info("Trunk fetch complete -- %d EXTERNAL trunk(s) retained.", len(trunks))
    return trunks


def fetch_trunk_recording(
    api_client: PureCloudPlatformClientV2.api_client.ApiClient,
    trunks: list[dict],
) -> None:
    """
    Fetch trunk base settings for each unique trunk_base_id and extract
    properties["trunk_recording_enabled"]["value"]["instance"].
    Falls back to ["default"] if instance is not set.
    Merges result into each trunk dict in-place.
    """
    if not trunks:
        return

    edge_api        = PureCloudPlatformClientV2.TelephonyProvidersEdgeApi(api_client)
    unique_base_ids = {t["trunk_base_id"] for t in trunks if t["trunk_base_id"]}
    log.info("Fetching trunk base settings (%d unique base(s)) ...", len(unique_base_ids))

    recording_map: dict[str, bool | None] = {}
    for base_id in unique_base_ids:
        try:
            base     = edge_api.get_telephony_providers_edges_trunkbasesetting(base_id)
            props    = getattr(base, "properties", None) or {}
            rec_prop = props.get("trunk_recording_enabled")
            if rec_prop and isinstance(rec_prop, dict):
                val      = rec_prop.get("value", {})
                instance = val.get("instance")
                if instance is None:
                    instance = val.get("default")
                recording_map[base_id] = bool(instance) if instance is not None else None
            else:
                recording_map[base_id] = None
        except ApiException as exc:
            log.error("Failed to fetch trunk base %s -- HTTP %s: %s",
                      base_id, exc.status, exc.reason)
            recording_map[base_id] = None

    for t in trunks:
        t["recording_enabled"] = recording_map.get(t["trunk_base_id"])

    log.info("Trunk recording fetch complete.")


# ---------------------------------------------------------------------------
# Summary helpers
# ---------------------------------------------------------------------------

def edge_summary(edges: list[dict]) -> dict:
    total        = len(edges)
    connected    = sum(1 for e in edges if e["severity"] == "ok")
    disconnected = sum(1 for e in edges if e["severity"] == "critical")
    return {
        "total":        total,
        "connected":    connected,
        "disconnected": disconnected,
        "unknown":      total - connected - disconnected,
    }


def trunk_summary(trunks: list[dict]) -> dict:
    total   = len(trunks)
    up      = sum(1 for t in trunks if t["severity"] == "ok")
    partial = sum(1 for t in trunks if t["severity"] == "warning")
    down    = sum(1 for t in trunks if t["severity"] == "critical")
    return {
        "total":   total,
        "up":      up,
        "partial": partial,
        "down":    down,
        "other":   total - up - partial - down,
    }


# ---------------------------------------------------------------------------
# Console report
# ---------------------------------------------------------------------------

def print_console_report(edges: list[dict], trunks: list[dict], report_ts: datetime) -> None:
    sep = "=" * 72
    print(f"\n{sep}")
    print(f"  GENESYS CLOUD HEALTH CHECK REPORT")
    print(f"  Generated: {report_ts.strftime('%Y-%m-%d %H:%M:%S UTC')}")
    print(sep)

    e_sum = edge_summary(edges)
    print(f"\nEDGES  --  Total: {e_sum['total']}  Connected: {e_sum['connected']}  "
          f"Disconnected: {e_sum['disconnected']}  Unknown: {e_sum['unknown']}")
    print(f"\n  {'Name':<30}  {'Online':<12}  {'State':<10}  {'Group':<25}  "
          f"{'Site':<20}  {'Version':<18}  {'Upd':4}  Storage")
    print(f"  {'-'*30}  {'-'*12}  {'-'*10}  {'-'*25}  {'-'*20}  {'-'*18}  {'-'*4}  {'-'*7}")
    for e in sorted(edges, key=lambda x: x["name"]):
        ua_str = "Yes" if e.get("update_available") is True  else \
                 "No"  if e.get("update_available") is False else "--"
        sp     = e.get("storage_pct")
        sp_str = f"{sp:.1f}%" if sp is not None else "--"
        print(f"  {e['name']:<30}  {e['online_status'] or '--':<12}  {e['state']:<10}  "
              f"{e['edge_group']:<25}  {e['site']:<20}  {e['software_version']:<18}  "
              f"{ua_str:<4}  {sp_str}")

    t_sum = trunk_summary(trunks)
    print(f"\nTRUNKS  --  Total: {t_sum['total']}  In Service: {t_sum['up']}  "
          f"Out Of Service: {t_sum['down']}  Other: {t_sum['other']}")
    print(f"\n  {'Name':<40}  {'Edge':<30}  {'Status':<16}  Recording")
    print(f"  {'-'*40}  {'-'*30}  {'-'*16}  {'-'*9}")
    for t in sorted(trunks, key=lambda x: x["name"]):
        rec_str = "True" if t.get("recording_enabled") is True  else \
                  "False" if t.get("recording_enabled") is False else "--"
        print(f"  {t['name']:<40}  {t['edge_name']:<30}  "
              f"{t['op_status_raw']:<16}  {rec_str}")

    print(f"\n{sep}\n")


# ---------------------------------------------------------------------------
# PDF helpers
# ---------------------------------------------------------------------------

def _std_table_style(header_bg, brand_dark, row_alt, grid_color):
    """Standard TableStyle shared by all tables in the report."""
    from reportlab.platypus import TableStyle
    from reportlab.lib import colors
    return TableStyle([
        ("BACKGROUND",    (0, 0), (-1, 0),  header_bg),
        ("TEXTCOLOR",     (0, 0), (-1, 0),  brand_dark),
        ("FONTNAME",      (0, 0), (-1, 0),  "Helvetica-Bold"),
        ("FONTSIZE",      (0, 0), (-1, 0),  7),
        ("TOPPADDING",    (0, 0), (-1, 0),  3),
        ("BOTTOMPADDING", (0, 0), (-1, 0),  3),
        ("FONTNAME",      (0, 1), (-1, -1), "Helvetica"),
        ("FONTSIZE",      (0, 1), (-1, -1), 7),
        ("ROWBACKGROUNDS",(0, 1), (-1, -1), [colors.white, row_alt]),
        ("GRID",          (0, 0), (-1, -1), 0.4, grid_color),
        ("VALIGN",        (0, 0), (-1, -1), "MIDDLE"),
        ("TOPPADDING",    (0, 1), (-1, -1), 2),
        ("BOTTOMPADDING", (0, 1), (-1, -1), 2),
    ])


def _coloured_para(text: str, hex_color: str, style, bold: bool = True):
    """Return a Paragraph with coloured (optionally bold) text."""
    from reportlab.platypus import Paragraph
    inner = f"<b>{text}</b>" if bold else text
    return Paragraph(f'<font color="{hex_color}">{inner}</font>', style)


# ---------------------------------------------------------------------------
# PDF page sections
# ---------------------------------------------------------------------------

def _build_issues_page(story: list, edges: list[dict], trunks: list[dict], sm: dict) -> None:
    """Issues summary — page 1. Appends a PageBreak at the end."""
    from reportlab.platypus import Paragraph, Spacer, Table, PageBreak

    edge_issues  = [e for e in edges  if e["severity"] != "ok"]
    trunk_issues = [t for t in trunks if t["severity"] == "critical"]
    mm           = sm["mm"]

    story.append(Paragraph("Active Issues", sm["h1"]))
    story.append(Spacer(1, 2 * mm))

    if not edge_issues and not trunk_issues:
        story.append(Paragraph(
            "<b>&#10003; All systems healthy -- no active issues detected.</b>",
            sm["healthy"],
        ))
    else:
        story.append(Paragraph(
            f"<b>{len(edge_issues)}</b> Edge issue(s)  |  "
            f"<b>{len(trunk_issues)}</b> Trunk issue(s)",
            sm["body"],
        ))
        story.append(Spacer(1, 4 * mm))

        # Edge issues
        story.append(Paragraph("Edge Issues", sm["h2"]))
        if edge_issues:
            rows = [["Name", "Online Status", "State", "Site"]]
            for e in sorted(edge_issues, key=lambda x: x["name"]):
                clr = sm["sev_colors"].get(e["severity"], sm["color_unknown"])
                rows.append([
                    Paragraph(e["name"],  sm["body"]),
                    _coloured_para(e["online_status"] or "UNKNOWN",
                                   f"#{clr.hexval()[2:].upper()}", sm["body"]),
                    Paragraph(e["state"], sm["body"]),
                    Paragraph(e["site"],  sm["body"]),
                ])
            tbl = Table(rows, colWidths=[55*mm, 28*mm, 18*mm, 35*mm], repeatRows=1)
            tbl.setStyle(sm["tbl_style"]())
            story.append(tbl)
        else:
            story.append(_coloured_para("No Edge issues.", "#27AE60", sm["body"]))

        story.append(Spacer(1, 4 * mm))

        # Trunk issues
        story.append(Paragraph("Trunk Issues", sm["h2"]))
        if trunk_issues:
            rows = [["External Trunk Name", "Edge Name", "Status"]]
            for t in sorted(trunk_issues, key=lambda x: x["name"]):
                clr = sm["sev_colors"].get(t["severity"], sm["color_unknown"])
                rows.append([
                    Paragraph(t["name"],      sm["body"]),
                    Paragraph(t["edge_name"], sm["body"]),
                    _coloured_para(t["op_status_raw"],
                                   f"#{clr.hexval()[2:].upper()}", sm["body"]),
                ])
            tbl = Table(rows, colWidths=[75*mm, 65*mm, 34*mm], repeatRows=1)
            tbl.setStyle(sm["tbl_style"]())
            story.append(tbl)
        else:
            story.append(_coloured_para("No Trunk issues.", "#27AE60", sm["body"]))

    story.append(PageBreak())


def _build_edge_table(story: list, edges: list[dict], sm: dict) -> None:
    """Edge Overview detail section."""
    from reportlab.platypus import Paragraph, Spacer, Table

    mm = sm["mm"]
    e_sum = edge_summary(edges)

    story.append(Paragraph("Edge Overview", sm["h1"]))
    story.append(Paragraph(
        f"Total Edges: <b>{e_sum['total']}</b>  |  "
        f"Connected: <b>{e_sum['connected']}</b>  |  "
        f"Disconnected: <b>{e_sum['disconnected']}</b>  |  "
        f"Unknown: <b>{e_sum['unknown']}</b>",
        sm["body"],
    ))
    story.append(Spacer(1, 3 * mm))

    if not edges:
        story.append(Paragraph("No Edge data retrieved.", sm["body"]))
        return

    rows = [["Name", "Online\nStatus", "State", "Edge Group",
             "Site", "Version", "Update\nAvail.", "Storage"]]

    for e in sorted(edges, key=lambda x: x["name"]):
        clr   = sm["sev_colors"].get(e["severity"], sm["color_unknown"])
        hex_c = f"#{clr.hexval()[2:].upper()}"

        ua = e.get("update_available")
        ua_cell = _coloured_para("Yes", "#E67E22", sm["body"]) if ua is True  else \
                  _coloured_para("No",  "#27AE60", sm["body"]) if ua is False else \
                  Paragraph("--", sm["body"])

        sp = e.get("storage_pct")
        storage_cell = _coloured_para(
            f"{sp:.1f}%",
            "#C0392B" if sp >= 90 else "#E67E22" if sp >= 75 else "#27AE60",
            sm["body"],
        ) if sp is not None else Paragraph("--", sm["body"])

        rows.append([
            Paragraph(e["name"],             sm["body"]),
            _coloured_para(e["online_status"] or "UNKNOWN", hex_c, sm["body"]),
            Paragraph(e["state"],            sm["body"]),
            Paragraph(e["edge_group"],       sm["body"]),
            Paragraph(e["site"],             sm["body"]),
            Paragraph(e["software_version"], sm["body"]),
            ua_cell,
            storage_cell,
        ])

    tbl = Table(rows, colWidths=[32*mm, 18*mm, 14*mm, 24*mm, 24*mm, 20*mm, 14*mm, 14*mm],
                repeatRows=1)
    tbl.setStyle(sm["tbl_style"]())
    story.append(tbl)


def _build_trunk_table(story: list, trunks: list[dict], sm: dict) -> None:
    """Trunk Health detail section."""
    from reportlab.platypus import Paragraph, Spacer, Table

    mm = sm["mm"]
    t_sum = trunk_summary(trunks)

    story.append(Paragraph("Trunk Health", sm["h1"]))
    story.append(Paragraph(
        f"Total Trunks: <b>{t_sum['total']}</b>  |  "
        f"In Service: <b>{t_sum['up']}</b>  |  "
        f"Out Of Service: <b>{t_sum['down']}</b>  |  "
        f"Other: <b>{t_sum['other']}</b>",
        sm["body"],
    ))
    story.append(Spacer(1, 3 * mm))

    if not trunks:
        story.append(Paragraph("No Trunk data retrieved.", sm["body"]))
        return

    rows = [["External Trunk Name", "Edge Name", "Status", "Recording"]]

    for t in sorted(trunks, key=lambda x: x["name"]):
        clr = sm["sev_colors"].get(t["severity"], sm["color_unknown"])
        rec = t.get("recording_enabled")
        rec_cell = _coloured_para("True",  "#27AE60", sm["body"]) if rec is True  else \
                   _coloured_para("False", "#C0392B", sm["body"]) if rec is False else \
                   Paragraph("--", sm["body"])
        rows.append([
            Paragraph(t["name"],      sm["body"]),
            Paragraph(t["edge_name"], sm["body"]),
            _coloured_para(t["op_status_raw"], f"#{clr.hexval()[2:].upper()}", sm["body"]),
            rec_cell,
        ])

    tbl = Table(rows, colWidths=[65*mm, 60*mm, 28*mm, 21*mm], repeatRows=1)
    tbl.setStyle(sm["tbl_style"]())
    story.append(tbl)


# ---------------------------------------------------------------------------
# PDF report
# ---------------------------------------------------------------------------

def generate_pdf(
    edges:       list[dict],
    trunks:      list[dict],
    report_ts:   datetime,
    output_path: str,
) -> None:
    """Generate the full PDF report: Issues page, Edge detail, Trunk detail."""
    try:
        from reportlab.lib import colors
        from reportlab.lib.pagesizes import A4
        from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
        from reportlab.lib.units import mm
        from reportlab.platypus import SimpleDocTemplate, Spacer
    except ImportError:
        log.error("reportlab is not installed.  Run: pip install reportlab")
        return

    # ---- Colours ----
    BRAND_DARK    = colors.HexColor("#1B2A4A")
    ROW_ALT       = colors.HexColor("#F0F4FA")
    ROW_HEAD      = colors.HexColor("#D0DCF0")
    GRID_COLOR    = colors.HexColor("#CCCCCC")
    COLOR_OK      = colors.HexColor("#27AE60")
    COLOR_WARN    = colors.HexColor("#E67E22")
    COLOR_CRIT    = colors.HexColor("#C0392B")
    COLOR_DISABLED= colors.HexColor("#7F8C8D")
    COLOR_UNKNOWN = colors.HexColor("#95A5A6")

    SEV_COLORS = {
        "ok":       COLOR_OK,
        "warning":  COLOR_WARN,
        "critical": COLOR_CRIT,
        "disabled": COLOR_DISABLED,
        "unknown":  COLOR_UNKNOWN,
        "managed":  COLOR_DISABLED,
    }

    PAGE_W, PAGE_H = A4
    MARGIN = 18 * mm

    # ---- Styles ----
    base    = getSampleStyleSheet()
    style_h1 = ParagraphStyle("GH1", parent=base["Heading1"],
                               fontSize=11, textColor=BRAND_DARK, spaceAfter=4)
    style_h2 = ParagraphStyle("GH2", parent=base["Heading2"],
                               fontSize=10, textColor=BRAND_DARK,
                               spaceBefore=4, spaceAfter=3)
    style_body = ParagraphStyle("GBody", parent=base["Normal"],
                                fontSize=7, leading=9)
    style_healthy = ParagraphStyle("GHealthy", parent=base["Normal"],
                                   fontSize=11, textColor=COLOR_OK, leading=16)

    # Styles map passed to every section builder
    sm = {
        "h1":          style_h1,
        "h2":          style_h2,
        "body":        style_body,
        "healthy":     style_healthy,
        "sev_colors":  SEV_COLORS,
        "color_unknown": COLOR_UNKNOWN,
        "mm":          mm,
        "tbl_style":   lambda: _std_table_style(ROW_HEAD, BRAND_DARK, ROW_ALT, GRID_COLOR),
    }

    # ---- Document ----
    doc = SimpleDocTemplate(
        output_path,
        pagesize=A4,
        leftMargin=MARGIN, rightMargin=MARGIN,
        topMargin=24 * mm, bottomMargin=14 * mm,
        title="Genesys Cloud Health Check Report",
        author="Altron Group -- Automated Reporter",
    )

    # ---- Page header / footer drawn on every page ----
    ts_str = report_ts.strftime("%Y-%m-%d  %H:%M:%S UTC")

    def _on_page(canvas, doc):
        canvas.saveState()
        canvas.setFillColor(BRAND_DARK)
        canvas.rect(0, PAGE_H - 22 * mm, PAGE_W, 22 * mm, fill=1, stroke=0)
        canvas.setFont("Helvetica-Bold", 13)
        canvas.setFillColor(colors.white)
        canvas.drawString(MARGIN, PAGE_H - 14 * mm, "Genesys Cloud -- Health Check Report")
        canvas.setFont("Helvetica", 8)
        canvas.drawRightString(PAGE_W - MARGIN, PAGE_H - 14 * mm, ts_str)
        canvas.setFont("Helvetica", 7)
        canvas.setFillColor(colors.HexColor("#888888"))
        canvas.drawString(MARGIN, 8 * mm,
                          "Altron Group  |  Genesys Cloud Health Check  |  Confidential")
        canvas.drawRightString(PAGE_W - MARGIN, 8 * mm, f"Page {doc.page}")
        canvas.restoreState()

    # ---- Assemble story ----
    story: list = []
    _build_issues_page(story, edges, trunks, sm)
    _build_edge_table(story, edges, sm)
    story.append(Spacer(1, 3 * mm))
    _build_trunk_table(story, trunks, sm)

    try:
        doc.build(story, onFirstPage=_on_page, onLaterPages=_on_page)
        log.info("PDF report saved --> %s", output_path)
    except Exception as exc:  # noqa: BLE001
        log.error("PDF generation failed: %s", exc)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def collect(config: dict) -> dict:
    """
    Authenticate, poll, and enrich — return raw data without generating a PDF.
    Called by main.py so pdf_merger can build the unified document.

    Returns:
        {
            "edges":  list[dict],   # enriched edge records
            "trunks": list[dict],   # enriched external trunk records
        }
    """
    log.info("Genesys collect() starting ...")
    api_client = authenticate(config)

    with ThreadPoolExecutor(max_workers=2) as pool:
        f_edges  = pool.submit(fetch_all_edges,  api_client)
        f_trunks = pool.submit(fetch_all_trunks, api_client)
        edges  = f_edges.result()
        trunks = f_trunks.result()

    with ThreadPoolExecutor(max_workers=2) as pool:
        f_metrics   = pool.submit(fetch_edge_metrics,    api_client, edges)
        f_recording = pool.submit(fetch_trunk_recording, api_client, trunks)
        f_metrics.result()
        f_recording.result()

    log.info("Genesys collect() complete — %d edge(s)  %d trunk(s)",
             len(edges), len(trunks))
    return {"edges": edges, "trunks": trunks}


def run(config: dict) -> str:
    """
    Module entry point for use by main.py.

    Accepts the full application config dict, reads the genesys: namespace,
    authenticates, polls, enriches, builds the PDF, and returns the output path.

    Returns the PDF file path on success, or raises an exception on failure
    (main.py catches exceptions per-module and notes the failure).
    """
    report_ts  = datetime.now(tz=timezone.utc)
    report_dir = config.get("reporting", {}).get("reportDir", ".")
    prefix     = config.get("reporting", {}).get("filenamePrefix", "healthcheck_report")
    report_path = os.path.join(
        report_dir,
        f"{prefix}_genesys_{report_ts.strftime('%Y%m%d_%H%M%S')}.pdf",
    )

    log.info("Genesys reporter starting — output: %s", report_path)

    # 1. Authenticate
    api_client = authenticate(config)

    # 2. Poll Edges and Trunks concurrently
    with ThreadPoolExecutor(max_workers=2) as pool:
        f_edges  = pool.submit(fetch_all_edges,  api_client)
        f_trunks = pool.submit(fetch_all_trunks, api_client)
        edges  = f_edges.result()
        trunks = f_trunks.result()

    # 3. Enrich — edge metrics and trunk recording run concurrently
    with ThreadPoolExecutor(max_workers=2) as pool:
        f_metrics   = pool.submit(fetch_edge_metrics,    api_client, edges)
        f_recording = pool.submit(fetch_trunk_recording, api_client, trunks)
        f_metrics.result()
        f_recording.result()

    # 4. Console summary
    print_console_report(edges, trunks, report_ts)

    # 5. Generate PDF
    os.makedirs(report_dir, exist_ok=True)
    generate_pdf(edges, trunks, report_ts, report_path)
    log.info("Genesys reporter done — PDF: %s", report_path)
    return report_path


# ---------------------------------------------------------------------------
# Standalone entry point
# ---------------------------------------------------------------------------

def main() -> None:
    """
    Standalone entry point — reads healthcheck_config.yaml directly,
    sets up its own logging, runs the reporter, and exits.
    Email is NOT sent here — use main.py for the full pipeline.
    """
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s  %(levelname)-8s  %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        handlers=[logging.StreamHandler(sys.stdout)],
    )
    log.info("=== Genesys Cloud Health Check Reporter — standalone mode ===")

    config    = _load_standalone_config()
    try:
        pdf_path = run(config)
    except RuntimeError as exc:
        log.error("%s", exc)
        sys.exit(1)
    log.info("Report saved to: %s", pdf_path)


if __name__ == "__main__":
    main()

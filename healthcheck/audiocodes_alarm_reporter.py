"""
AudioCodes Alarm Reporter  —  Alpha v1.0
========================================
Polls AudioCodes devices for active alarms and historical alarms (last 7 days)
and generates a PDF report.

Designed to be used in two ways:
  1. Imported by main.py — call run(config) which returns the PDF path.
  2. Run standalone — python audiocodes_alarm_reporter.py (reads its own YAML).

All configuration is passed in via the config dict (audiocodes: namespace).
Logging and email are handled by the caller (main.py) when used as a module.
"""

import json
import logging
import os
import sys
import time
import requests
import yaml
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone

# ─────────────────────────────────────────────
#  LOGGING
# ─────────────────────────────────────────────

# When imported by main.py, the root logger is already configured.
# When run standalone, basicConfig below provides a sensible default.
log = logging.getLogger("ac_reporter")

from reportlab.lib import colors
from reportlab.lib.pagesizes import letter
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.units import inch
from reportlab.platypus import (
    SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle, HRFlowable
)

requests.packages.urllib3.disable_warnings()


# ─────────────────────────────────────────────
#  SSL RETRY HELPER
# ─────────────────────────────────────────────

def _get_with_ssl_retry(
    url: str,
    headers: dict,
    verify: bool = False,
    timeout: int = 10,
    retries: int = 3,
    backoff: float = 3.0,
):
    """
    requests.get with automatic exponential-backoff retry on SSL handshake
    failures.

    SSL handshake errors occur transiently when:
      • The Windows Service starts before the TLS stack is fully ready.
      • A device momentarily resets its TLS session.

    On each SSLError the call waits `backoff` seconds (doubling each time)
    before retrying.  All other exceptions are re-raised immediately so
    existing error-handling in the caller is unaffected.
    """
    from requests.exceptions import SSLError
    last_exc: Exception | None = None
    delay = backoff
    for attempt in range(1, retries + 1):
        try:
            return requests.get(url, headers=headers, verify=verify, timeout=timeout)
        except SSLError as exc:
            last_exc = exc
            if attempt < retries:
                log.warning(
                    "SSL handshake failure for %s (attempt %d/%d) — retrying in %.0fs: %s",
                    url, attempt, retries, delay, exc,
                )
                time.sleep(delay)
                delay *= 2
            else:
                log.error(
                    "SSL handshake failure for %s — all %d attempts exhausted: %s",
                    url, retries, exc,
                )
    raise last_exc

# ─────────────────────────────────────────────
#  CONFIGURATION  —  injected at runtime
#  These module-level variables are set by _apply_config() from the
#  audiocodes: section of healthcheck_config.yaml.
#  When running standalone they are populated from the same YAML.
# ─────────────────────────────────────────────

AUDIOCODES_DEVICES: list  = []
AUTH_HEADER:        str   = ""
HISTORY_DAYS:       int   = 7
MAX_WORKERS:        int   = 10
DEVICE_COUNTRY:     dict  = {}


def _apply_config(ac_cfg: dict) -> None:
    """Populate module-level config from the audiocodes: config section."""
    global AUDIOCODES_DEVICES, AUTH_HEADER, HISTORY_DAYS, MAX_WORKERS, DEVICE_COUNTRY
    AUDIOCODES_DEVICES = ac_cfg.get("devices",      [])
    AUTH_HEADER        = ac_cfg.get("authHeader",   "")
    HISTORY_DAYS       = int(ac_cfg.get("historyDays", 7))
    MAX_WORKERS        = int(ac_cfg.get("maxWorkers",  10))
    DEVICE_COUNTRY     = ac_cfg.get("deviceCountry", {})
    log.debug("AudioCodes config applied — %d device(s)  historyDays=%d  maxWorkers=%d",
              len(AUDIOCODES_DEVICES), HISTORY_DAYS, MAX_WORKERS)


# ─────────────────────────────────────────────
#  DEVICE STATUS & TRUNK INFERENCE
# ─────────────────────────────────────────────

def _format_uptime(seconds) -> str:
    """Convert systemUpTime seconds to a human-readable string e.g. 12d:3h:22m."""
    try:
        seconds = int(seconds)
        d, rem  = divmod(seconds, 86400)
        h, rem  = divmod(rem, 3600)
        m       = rem // 60
        return f"{d}d:{h:02d}h:{m:02d}m"
    except (TypeError, ValueError):
        return str(seconds)


def fetch_device_status(ip: str) -> dict:
    """
    Fetch /api/v1/status to get device info: productType, versionID,
    serialNumber, systemUpTime, operationalState.
    Returns a dict with all fields, or empty strings on failure.
    """
    url     = f"https://{ip}/api/v1/status"
    headers = {"Authorization": AUTH_HEADER}
    log.info("[%s] Fetching device status → %s", ip, url)

    empty = {
        "productType": "", "versionID": "", "serialNumber": "",
        "systemUpTime": "", "operationalState": "", "error": None,
    }

    try:
        r = _get_with_ssl_retry(url, headers=headers, verify=False, timeout=10)
        log.debug("[%s] Status HTTP %s  |  %.0f ms  |  %d bytes",
                  ip, r.status_code, r.elapsed.total_seconds() * 1000, len(r.content))
        log.debug("[%s] Status raw body: %s", ip, r.text[:500])
        r.raise_for_status()

        if not r.content or not r.text.strip():
            log.warning("[%s] Empty status response", ip)
            return empty

        data = r.json()
        uptime_raw = data.get("systemUpTime", "")
        result = {
            "productType":     data.get("productType", ""),
            "versionID":       data.get("versionID", ""),
            "serialNumber":    data.get("serialNumber", ""),
            "systemUpTime":    _format_uptime(uptime_raw) if uptime_raw else "",
            "operationalState":data.get("operationalState", ""),
            "error":           None,
        }
        log.info("[%s] Device status — type=%s  version=%s  serial=%s  uptime=%s",
                 ip, result["productType"], result["versionID"],
                 result["serialNumber"], result["systemUpTime"])
        return result

    except requests.exceptions.ConnectionError as e:
        log.error("[%s] Status connection failed: %s", ip, e)
        return {**empty, "error": str(e)}
    except requests.exceptions.Timeout:
        log.error("[%s] Status request timed out", ip)
        return {**empty, "error": "Timeout"}
    except requests.exceptions.HTTPError as e:
        log.error("[%s] Status HTTP error: %s", ip, e)
        return {**empty, "error": str(e)}
    except (json.JSONDecodeError, ValueError) as e:
        log.error("[%s] Status response not valid JSON: %s", ip, e)
        return {**empty, "error": str(e)}


def fetch_all_device_statuses(devices: list[str]) -> dict[str, dict]:
    """Returns a dict of ip → status_info for all devices, fetched in parallel."""
    log.info("Fetching device status for %d device(s) in parallel (max %d workers) …",
             len(devices), MAX_WORKERS)
    results = {}
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as ex:
        futures = {ex.submit(fetch_device_status, ip): ip for ip in devices}
        for future in as_completed(futures):
            ip = futures[future]
            try:
                results[ip] = future.result()
            except Exception as e:
                log.error("[%s] Unexpected error in fetch_device_status: %s", ip, e, exc_info=True)
                results[ip] = {"productType": "", "versionID": "", "serialNumber": "",
                                "systemUpTime": "", "operationalState": "", "error": str(e)}
    return results


# Trunk alarm keywords — if any active alarm description contains these
# (case-insensitive) it is considered a trunk alarm. The trunk number is
# extracted from the alarm source or description.
_TRUNK_ALARM_KEYWORDS = ("trunk", "e1", "t1", "pri", "isdn", "los", "d-channel")

def infer_trunk_status(ip: str, active_alarms: list[dict],
                       num_trunks: int = 4) -> list[dict]:
    """
    Infer trunk status from active alarms.
    Returns a list of dicts: [{"trunk": 1, "status": "Active", "alarm": ""}, ...]

    AudioCodes uses zero-based trunk numbering in alarm sources:
      Interface#0/trunk#0  →  Trunk 1
      Interface#0/trunk#1  →  Trunk 2  etc.

    Badge states:
      - Green  "Active"         — no alarm on this trunk
      - Orange "Disabled"       — trunk-related alarm, no specific LOS/LOF/etc label
      - Red    "<Alarm Name>"   — alarm name shown directly (e.g. "LOS / LOF Alarm")
    """
    import re

    # Alarms that get their name shown in red on the badge
    _RED_KEYWORDS = ("los", "lof", "ais", "rai", "lop", "alarm")

    trunk_alarms = {}   # trunk_display_num → best alarm label

    for alarm in active_alarms:
        desc     = str(alarm.get("description") or alarm.get("alarmName") or "").lower()
        source   = str(alarm.get("source") or "").lower()
        combined = desc + " " + source

        if not any(kw in combined for kw in _TRUNK_ALARM_KEYWORDS):
            continue

        # Primary: explicit trunk#N pattern (zero-based → add 1 for display)
        trunk_refs = re.findall(r'trunk#(\d+)', combined)
        matched_trunks = []
        if trunk_refs:
            for n in trunk_refs:
                trunk_display = int(n) + 1
                if 1 <= trunk_display <= num_trunks:
                    matched_trunks.append(trunk_display)
        else:
            # Fallback: bare numbers
            for n in re.findall(r'\b(\d+)\b', combined):
                idx = int(n)
                if 1 <= idx <= num_trunks:
                    matched_trunks.append(idx)

        # Determine badge label — use original-case description for display
        raw_desc = str(alarm.get("description") or alarm.get("alarmName") or "")
        is_red   = any(kw in raw_desc.lower() for kw in _RED_KEYWORDS)
        label    = raw_desc.strip() if is_red else "Disabled"

        for trunk_display in matched_trunks:
            # Prefer red (named) alarms over generic Disabled
            existing = trunk_alarms.get(trunk_display, "")
            if not existing or (label != "Disabled"):
                trunk_alarms[trunk_display] = label
            log.debug("[%s] Trunk %d → label=%r  source=%s",
                      ip, trunk_display, label,
                      (alarm.get("source") or alarm.get("description", ""))[:80])

    trunks = []
    for i in range(1, num_trunks + 1):
        label = trunk_alarms.get(i, "")
        if label:
            status = "alarm" if label != "Disabled" else "disabled"
        else:
            status = "active"
            label  = "Active"
        trunks.append({"trunk": i, "status": status, "label": label})
        log.debug("[%s] Trunk %d → status=%s  label=%s", ip, i, status, label)

    return trunks


# ─────────────────────────────────────────────
#  ALARM POLLING
# ─────────────────────────────────────────────

# Known severity field names returned by the AudioCodes detail endpoint
_SEVERITY_FIELDS = ("severity", "Severity", "alarmSeverity", "AlarmSeverity",
                    "level", "Level", "alarmLevel")

def fetch_alarm_detail(ip: str, alarm: dict, headers: dict) -> dict:
    """
    The /api/v1/alarms/active list only returns stub fields (description, id, url).
    Fetch the individual alarm detail URL to get severity and any extra fields,
    then merge them back into the alarm dict.
    """
    relative_url = alarm.get("url") or alarm.get("Uri") or alarm.get("uri")
    if not relative_url:
        log.debug("[%s]   Alarm id=%s has no 'url' field — cannot fetch detail",
                  ip, alarm.get("id", "?"))
        return alarm

    detail_url = f"https://{ip}{relative_url}"
    log.debug("[%s]   Fetching detail → %s", ip, detail_url)
    try:
        r = _get_with_ssl_retry(detail_url, headers=headers, verify=False, timeout=10)
        log.debug("[%s]   Detail HTTP %s  |  %.0f ms  |  %d bytes",
                  ip, r.status_code, r.elapsed.total_seconds() * 1000, len(r.content))
        log.debug("[%s]   Detail raw body: %s", ip, r.text[:1000])
        r.raise_for_status()
        detail = r.json()

        if isinstance(detail, dict):
            # Merge detail fields into the stub, detail fields take priority
            merged = {**alarm, **detail}
            sev = next((merged[f] for f in _SEVERITY_FIELDS if f in merged), None)
            log.debug("[%s]   Alarm id=%s  severity=%s  fields=%s",
                      ip, merged.get("id", "?"), sev, list(detail.keys()))
            return merged
        else:
            log.warning("[%s]   Detail for alarm id=%s returned unexpected type %s — using stub only",
                        ip, alarm.get("id", "?"), type(detail).__name__)
            return alarm

    except requests.exceptions.RequestException as e:
        log.warning("[%s]   Failed to fetch detail for alarm id=%s from %s: %s",
                    ip, alarm.get("id", "?"), detail_url, e)
        return alarm
    except (json.JSONDecodeError, ValueError) as e:
        log.warning("[%s]   Detail for alarm id=%s is not valid JSON: %s",
                    ip, alarm.get("id", "?"), e)
        return alarm


def poll_device(ip: str) -> dict:
    """
    Poll a single AudioCodes device for active alarms.
    The list endpoint returns stubs only — each alarm's detail URL is fetched
    individually to retrieve severity and full alarm data.
    Returns a dict with keys: ip, status, alarms, error, raw_response.
    """
    url = f"https://{ip}/api/v1/alarms/active"
    headers = {"Authorization": AUTH_HEADER}
    log.info("[%s] Starting poll  →  %s", ip, url)

    try:
        response = _get_with_ssl_retry(url, headers=headers, verify=False, timeout=10)
        log.debug("[%s] HTTP %s  |  %.0f ms  |  %d bytes",
                  ip, response.status_code,
                  response.elapsed.total_seconds() * 1000,
                  len(response.content))
        log.debug("[%s] Raw response body:\n%s", ip, response.text[:2000])

        response.raise_for_status()

        # ── Empty body = no active alarms ────────────────────
        if not response.content or not response.text.strip():
            log.info("[%s] Empty response body — no active alarms", ip)
            return {
                "ip": ip, "status": "connected", "alarms": [],
                "error": None, "raw_response": "",
            }

        # ── Parse JSON ───────────────────────────────────────
        try:
            data = response.json()
        except (json.JSONDecodeError, ValueError) as parse_err:
            # Non-JSON body — log it and return 0 alarms (not a false alarm)
            log.warning(
                "[%s] Response is not valid JSON — skipping as 0 alarms.\n"
                "  Parse error : %s\n"
                "  Raw content : %r",
                ip, parse_err, response.text[:500]
            )
            return {
                "ip": ip, "status": "connected", "alarms": [],
                "error": None, "raw_response": response.text,
            }

        log.debug("[%s] Parsed JSON type: %s", ip, type(data).__name__)

        # ── Extract alarm list ───────────────────────────────
        if isinstance(data, list):
            alarms = data
            log.debug("[%s] Response is a list — treating as alarm array directly", ip)

        elif isinstance(data, dict):
            # Check the known alarm wrapper keys first
            if "alarms" in data:
                alarms = data["alarms"]
                log.debug("[%s] Found alarms under key 'alarms'", ip)
            elif "activeAlarms" in data:
                alarms = data["activeAlarms"]
                log.debug("[%s] Found alarms under key 'activeAlarms'", ip)
            else:
                # Dict with no recognised alarm key — could be an empty-state
                # response like {"status": "ok"} or similar.  Do NOT wrap the
                # whole dict as a single "Unknown" alarm — that's the bug.
                log.warning(
                    "[%s] Response is a dict but contains neither 'alarms' nor "
                    "'activeAlarms' key.  Keys present: %s  — treating as 0 alarms.\n"
                    "  Full response: %s",
                    ip, list(data.keys()), json.dumps(data, indent=2)[:500]
                )
                alarms = []
        else:
            log.warning("[%s] Unexpected JSON root type %s — treating as 0 alarms",
                        ip, type(data).__name__)
            alarms = []

        # ── Validate each alarm is a non-empty dict ──────────
        valid_alarms = []
        for i, item in enumerate(alarms):
            if not isinstance(item, dict):
                log.warning("[%s] Alarm[%d] is not a dict (got %s: %r) — skipping",
                             ip, i, type(item).__name__, item)
                continue
            if not item:
                log.warning("[%s] Alarm[%d] is an empty dict — skipping", ip, i)
                continue
            valid_alarms.append(item)

        if len(valid_alarms) != len(alarms):
            log.warning("[%s] Dropped %d invalid/empty alarm entries (kept %d)",
                        ip, len(alarms) - len(valid_alarms), len(valid_alarms))

        # ── Fetch detail for each alarm in parallel ──────────
        log.info("[%s] Fetching detail for %d alarm(s) in parallel …", ip, len(valid_alarms))
        enriched_alarms = [None] * len(valid_alarms)
        with ThreadPoolExecutor(max_workers=MAX_WORKERS) as detail_pool:
            detail_futures = {
                detail_pool.submit(fetch_alarm_detail, ip, alarm, headers): i
                for i, alarm in enumerate(valid_alarms)
            }
            for future in as_completed(detail_futures):
                i = detail_futures[future]
                try:
                    enriched = future.result()
                except Exception as e:
                    log.warning("[%s]   Detail fetch[%d] failed: %s", ip, i, e)
                    enriched = valid_alarms[i]
                sev  = next((enriched[f] for f in _SEVERITY_FIELDS if f in enriched), "Unknown")
                name = enriched.get("description") or enriched.get("alarmName") or "(no name)"
                log.info("[%s]   Alarm[%d]  id=%-4s  severity=%-10s  description=%s",
                         ip, i, enriched.get("id", "?"), sev, name)
                enriched_alarms[i] = enriched

        log.info("[%s] Poll complete — %d alarm(s)", ip, len(enriched_alarms))
        return {
            "ip": ip, "status": "connected", "alarms": enriched_alarms,
            "error": None, "raw_response": response.text,
        }

    except requests.exceptions.ConnectionError as e:
        log.error("[%s] Connection failed: %s", ip, e)
        return {"ip": ip, "status": "unreachable", "alarms": [],
                "error": str(e), "raw_response": None}

    except requests.exceptions.Timeout:
        log.error("[%s] Request timed out", ip)
        return {"ip": ip, "status": "timeout", "alarms": [],
                "error": "Request timed out", "raw_response": None}

    except requests.exceptions.ChunkedEncodingError as e:
        # Device responded but sent no/incomplete body — treat as 0 active alarms
        log.warning("[%s] ChunkedEncodingError (empty body?) — treating as 0 alarms: %s", ip, e)
        return {"ip": ip, "status": "connected", "alarms": [],
                "error": None, "raw_response": None}

    except requests.exceptions.HTTPError as e:
        log.error("[%s] HTTP error: %s", ip, e)
        return {"ip": ip, "status": "http_error", "alarms": [],
                "error": str(e), "raw_response": getattr(e.response, "text", None)}


def poll_all_devices(devices: list[str]) -> list[dict]:
    log.info("Polling active alarms on %d device(s) in parallel (max %d workers) …",
             len(devices), MAX_WORKERS)
    results = [None] * len(devices)
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as ex:
        futures = {ex.submit(poll_device, ip): i for i, ip in enumerate(devices)}
        for future in as_completed(futures):
            i = futures[future]
            try:
                results[i] = future.result()
            except Exception as e:
                ip = devices[i]
                log.error("[%s] Unexpected error in poll_device: %s", ip, e, exc_info=True)
                results[i] = {"ip": ip, "status": "error", "alarms": [],
                               "error": str(e), "raw_response": None}
    return results


# ─────────────────────────────────────────────
#  HISTORICAL ALARM POLLING
# ─────────────────────────────────────────────

# Known timestamp field names the history endpoint may use
_TIMESTAMP_FIELDS = ("time", "Time", "timestamp", "Timestamp", "date", "Date",
                     "eventTime", "EventTime", "raisedTime", "clearedTime")

def _parse_alarm_timestamp(alarm: dict) -> datetime | None:
    """
    Try each known timestamp field and return a timezone-aware datetime, or None.
    Handles both Unix epoch integers/floats and ISO 8601 strings.
    """
    for field in _TIMESTAMP_FIELDS:
        raw = alarm.get(field)
        if raw is None:
            continue
        try:
            # Unix epoch (int or float)
            if isinstance(raw, (int, float)):
                return datetime.fromtimestamp(raw, tz=timezone.utc)
            # ISO 8601 string
            s = str(raw).strip().rstrip("Z")
            # Handle optional fractional seconds
            for fmt in ("%Y-%m-%dT%H:%M:%S.%f", "%Y-%m-%dT%H:%M:%S",
                        "%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
                try:
                    return datetime.strptime(s, fmt).replace(tzinfo=timezone.utc)
                except ValueError:
                    continue
        except Exception:
            continue
    return None


def fetch_history_device(ip: str) -> dict:
    """
    Fetch /api/v1/alarms/history from a single device, then filter to the
    last HISTORY_DAYS days in Python.
    Returns a dict with keys: ip, status, alarms, error, raw_response.
    """
    url = f"https://{ip}/api/v1/alarms/history"
    headers = {"Authorization": AUTH_HEADER}
    cutoff = datetime.now(tz=timezone.utc) - timedelta(days=HISTORY_DAYS)
    log.info("[%s] Fetching history  →  %s  (cutoff: %s)",
             ip, url, cutoff.strftime("%Y-%m-%d %H:%M:%S UTC"))

    try:
        response = _get_with_ssl_retry(url, headers=headers, verify=False, timeout=15)
        log.debug("[%s] History HTTP %s  |  %.0f ms  |  %d bytes",
                  ip, response.status_code,
                  response.elapsed.total_seconds() * 1000,
                  len(response.content))
        log.debug("[%s] History raw body:\n%s", ip, response.text[:2000])

        response.raise_for_status()

        # ── Empty body ───────────────────────────────────────
        if not response.content or not response.text.strip():
            log.info("[%s] Empty history response — no historical alarms", ip)
            return {"ip": ip, "status": "connected", "alarms": [],
                    "error": None, "raw_response": ""}

        # ── Parse JSON ───────────────────────────────────────
        try:
            data = response.json()
        except (json.JSONDecodeError, ValueError) as parse_err:
            log.warning("[%s] History response is not valid JSON: %s\n  Raw: %r",
                        ip, parse_err, response.text[:500])
            return {"ip": ip, "status": "connected", "alarms": [],
                    "error": None, "raw_response": response.text}

        # ── Extract alarm list ───────────────────────────────
        if isinstance(data, list):
            alarms = data
            log.debug("[%s] History response is a list (%d items)", ip, len(alarms))
        elif isinstance(data, dict):
            alarms = (data.get("alarms") or data.get("history") or
                      data.get("historicalAlarms") or data.get("alarmHistory") or [])
            log.debug("[%s] History response is a dict — extracted %d item(s) "
                      "from keys %s", ip, len(alarms), list(data.keys()))
            if not alarms and data:
                log.warning("[%s] History dict had no recognised alarm key. "
                            "Keys present: %s\n  Full response: %s",
                            ip, list(data.keys()), json.dumps(data, indent=2)[:500])
        else:
            log.warning("[%s] History unexpected root type %s — treating as 0 alarms",
                        ip, type(data).__name__)
            alarms = []

        # ── Validate ─────────────────────────────────────────
        valid = [a for a in alarms if isinstance(a, dict) and a]
        if len(valid) != len(alarms):
            log.warning("[%s] History: dropped %d invalid entries",
                        ip, len(alarms) - len(valid))

        # ── Fetch detail for each alarm in parallel FIRST ────
        # Detail responses contain timestamps and severity — we must enrich
        # before filtering so the date window and cleared exclusion work
        # on accurate data rather than stub fields.
        log.info("[%s] Fetching history detail for %d alarm(s) in parallel …", ip, len(valid))
        enriched_all = [None] * len(valid)
        with ThreadPoolExecutor(max_workers=MAX_WORKERS) as detail_pool:
            detail_futures = {
                detail_pool.submit(fetch_alarm_detail, ip, alarm, headers): i
                for i, alarm in enumerate(valid)
            }
            for future in as_completed(detail_futures):
                i = detail_futures[future]
                try:
                    alarm = future.result()
                except Exception as e:
                    log.warning("[%s]   History detail fetch[%d] failed: %s", ip, i, e)
                    alarm = valid[i]
                sev  = next((alarm[f] for f in _SEVERITY_FIELDS if f in alarm), "Unknown")
                name = alarm.get("description") or alarm.get("alarmName") or "(no name)"
                log.info("[%s]   History alarm[%d]  id=%-4s  severity=%-10s  description=%s",
                         ip, i, alarm.get("id", "?"), sev, name)
                enriched_all[i] = alarm

        # ── Exclude "Cleared" and "Unknown" alarms ─────────────────────────
        # Cleared = resolved events (noise). Unknown = stubs where the detail
        # fetch returned no severity field — typically also cleared entries.
        _EXCLUDE_SEVS = {"cleared", "unknown"}
        pre_excl     = len(enriched_all)
        enriched_all = [
            a for a in enriched_all
            if str(next((a[f] for f in _SEVERITY_FIELDS if f in a),
                        "unknown")).lower() not in _EXCLUDE_SEVS
        ]
        excl_dropped = pre_excl - len(enriched_all)
        if excl_dropped:
            log.info("[%s] History: dropped %d cleared/unknown alarm(s) — %d remaining",
                     ip, excl_dropped, len(enriched_all))

        # ── Filter to last HISTORY_DAYS days ──────────────────
        # Now that detail is fetched, timestamps are present on most alarms.
        # Alarms still missing a timestamp are included and tagged so the
        # PDF can display "Unknown date" rather than a blank.
        filtered, no_ts, too_old = [], 0, 0
        for alarm in enriched_all:
            ts = _parse_alarm_timestamp(alarm)
            if ts is None:
                no_ts += 1
                alarm = {**alarm, "_no_timestamp": True}
                filtered.append(alarm)
            elif ts >= cutoff:
                filtered.append(alarm)
            else:
                too_old += 1

        log.info("[%s] History: %d total  |  %d in window (incl %d undated)  |  %d too old",
                 ip, pre_excl, len(filtered), no_ts, too_old)

        enriched = filtered

        return {"ip": ip, "status": "connected", "alarms": enriched,
                "error": None, "raw_response": response.text}

    except requests.exceptions.ConnectionError as e:
        log.error("[%s] History connection failed: %s", ip, e)
        return {"ip": ip, "status": "unreachable", "alarms": [],
                "error": str(e), "raw_response": None}
    except requests.exceptions.Timeout:
        log.error("[%s] History request timed out", ip)
        return {"ip": ip, "status": "timeout", "alarms": [],
                "error": "Request timed out", "raw_response": None}
    except requests.exceptions.ChunkedEncodingError as e:
        log.warning("[%s] History ChunkedEncodingError (response ended prematurely)"
                    " — treating as 0 historical alarms: %s", ip, e)
        return {"ip": ip, "status": "connected", "alarms": [],
                "error": None, "raw_response": None}
    except requests.exceptions.HTTPError as e:
        log.error("[%s] History HTTP error: %s", ip, e)
        return {"ip": ip, "status": "http_error", "alarms": [],
                "error": str(e), "raw_response": getattr(e.response, "text", None)}


def fetch_history_all_devices(devices: list[str]) -> list[dict]:
    log.info("Fetching history on %d device(s) in parallel (max %d workers) …",
             len(devices), MAX_WORKERS)
    results = [None] * len(devices)
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as ex:
        futures = {ex.submit(fetch_history_device, ip): i for i, ip in enumerate(devices)}
        for future in as_completed(futures):
            i = futures[future]
            try:
                results[i] = future.result()
            except Exception as e:
                ip = devices[i]
                log.error("[%s] Unexpected error in fetch_history_device: %s", ip, e, exc_info=True)
                results[i] = {"ip": ip, "status": "error", "alarms": [],
                               "error": str(e), "raw_response": None}
    return results


# ─────────────────────────────────────────────
#  SEVERITY HELPERS
# ─────────────────────────────────────────────

SEVERITY_ORDER = {"critical": 0, "major": 1, "minor": 2, "warning": 3, "unknown": 4}
SEVERITY_COLORS = {
    "critical": colors.HexColor("#c0392b"),
    "major":    colors.HexColor("#e67e22"),
    "minor":    colors.HexColor("#f1c40f"),
    "warning":  colors.HexColor("#3498db"),
    "unknown":  colors.HexColor("#95a5a6"),
}

def normalize_severity(alarm: dict) -> str:
    raw = next((alarm[f] for f in _SEVERITY_FIELDS if f in alarm), "Unknown")
    return str(raw).strip().lower()


def count_by_severity(alarms: list[dict]) -> dict:
    counts = {"critical": 0, "major": 0, "minor": 0, "warning": 0, "unknown": 0}
    for alarm in alarms:
        sev = normalize_severity(alarm)
        counts[sev] = counts.get(sev, 0) + 1
    return counts


# ─────────────────────────────────────────────
#  PDF GENERATION
# ─────────────────────────────────────────────

def build_pdf(device_results: list[dict], history_results: list[dict],
              device_statuses: dict[str, dict], output_path: str) -> None:
    log.info("Building PDF report → %s", output_path)
    doc = SimpleDocTemplate(
        output_path,
        pagesize=letter,
        leftMargin=0.75 * inch,
        rightMargin=0.75 * inch,
        topMargin=0.75 * inch,
        bottomMargin=0.75 * inch,
    )

    styles   = getSampleStyleSheet()
    W        = letter[0] - 1.5 * inch   # usable width
    ts_str   = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    story    = []

    # ── Title block ──────────────────────────────────────
    title_style = ParagraphStyle(
        "ReportTitle",
        parent=styles["Title"],
        fontSize=20,
        textColor=colors.HexColor("#2c3e50"),
        spaceAfter=4,
    )
    sub_style = ParagraphStyle(
        "SubTitle",
        parent=styles["Normal"],
        fontSize=10,
        textColor=colors.HexColor("#7f8c8d"),
        spaceAfter=2,
    )
    story.append(Paragraph("AudioCodes Active Alarm Report", title_style))
    story.append(Paragraph(f"Generated: {ts_str}", sub_style))
    story.append(Paragraph(f"Devices polled: {len(device_results)}", sub_style))
    story.append(HRFlowable(width="100%", thickness=1.5,
                             color=colors.HexColor("#2c3e50"), spaceAfter=12))

    # ── Device & Trunk Overview table ─────────────────────
    story.append(Paragraph("Device & Trunk Overview", styles["Heading2"]))
    story.append(Spacer(1, 4))

    dev_cell = ParagraphStyle("devcell", parent=styles["Normal"], fontSize=7.5)
    dev_hdr  = ParagraphStyle("devhdr",  parent=styles["Normal"], fontSize=7.5,
                               textColor=colors.white)

    # Header row — white text
    dev_table_data = [[
        Paragraph("<b>IP Address</b>",    dev_hdr),
        Paragraph("<b>Country</b>",       dev_hdr),
        Paragraph("<b>Active</b>",        dev_hdr),
        Paragraph("<b>Type</b>",          dev_hdr),
        Paragraph("<b>Version</b>",       dev_hdr),
        Paragraph("<b>Serial No</b>",     dev_hdr),
        Paragraph("<b>Uptime</b>",        dev_hdr),
        Paragraph("<b>Trunks</b>",        dev_hdr),
    ]]

    dev_table_style = TableStyle([
        ("BACKGROUND",    (0, 0), (-1, 0), colors.HexColor("#2c3e50")),
        ("TEXTCOLOR",     (0, 0), (-1, 0), colors.white),
        ("FONTSIZE",      (0, 0), (-1,-1), 7.5),
        ("ROWBACKGROUNDS",(0, 1), (-1,-1),
         [colors.HexColor("#f8f9fa"), colors.white]),
        ("GRID",          (0, 0), (-1,-1), 0.4, colors.HexColor("#dee2e6")),
        ("LEFTPADDING",   (0, 0), (-1,-1), 4),
        ("RIGHTPADDING",  (0, 0), (-1,-1), 4),
        ("TOPPADDING",    (0, 0), (-1,-1), 3),
        ("BOTTOMPADDING", (0, 0), (-1,-1), 3),
        ("VALIGN",        (0, 0), (-1,-1), "TOP"),
    ])

    for row_i, device in enumerate(device_results, start=1):
        ip      = device["ip"]
        status  = device_statuses.get(ip, {})
        active_alarms = device["alarms"]

        is_connected = device["status"] == "connected"
        country      = DEVICE_COUNTRY.get(ip, "")
        prod_type    = status.get("productType", "")
        version      = status.get("versionID", "")
        serial       = status.get("serialNumber", "")
        uptime       = status.get("systemUpTime", "")

        # Use operationalState from /api/v1/status as the primary Active indicator.
        # Fall back to connection status only if the status call also failed.
        op_state = status.get("operationalState", "")
        if op_state:
            is_active  = op_state.upper() == "UNLOCKED"
            active_str = "True" if is_active else "False"
        else:
            # Status endpoint unreachable — fall back to whether alarms API responded
            is_active  = is_connected
            active_str = "True" if is_connected else "False"

        # Infer trunks from active alarms
        trunks = infer_trunk_status(ip, active_alarms)

        # Build trunk cell — all trunks in a single horizontal row
        trunk_cells = []
        trunk_row_colors = []
        for t in trunks:
            if t["status"] == "active":
                t_color = colors.HexColor("#27ae60")
            elif t["status"] == "alarm":
                t_color = colors.HexColor("#c0392b")
            else:
                t_color = colors.HexColor("#e67e22")

            trunk_cells.append(
                Paragraph(f"<b>{t['trunk']}</b>",
                          ParagraphStyle(f"tr{t['trunk']}",
                                         parent=styles["Normal"],
                                         fontSize=7,
                                         alignment=1,          # centre
                                         textColor=colors.white))
            )
            trunk_row_colors.append(t_color)

        # Single row, one cell per trunk
        trunk_inner_style = TableStyle([
            ("LEFTPADDING",  (0,0),(-1,-1), 3),
            ("RIGHTPADDING", (0,0),(-1,-1), 3),
            ("TOPPADDING",   (0,0),(-1,-1), 2),
            ("BOTTOMPADDING",(0,0),(-1,-1), 2),
        ])
        for ti, tc in enumerate(trunk_row_colors):
            trunk_inner_style.add("BACKGROUND", (ti,0),(ti,0), tc)

        trunk_col_w = (W * 0.14) / max(len(trunks), 1)
        trunk_cell  = Table([trunk_cells],
                             colWidths=[trunk_col_w] * len(trunks),
                             style=trunk_inner_style)

        dev_table_data.append([
            Paragraph(ip,         dev_cell),
            Paragraph(country,    dev_cell),
            Paragraph(active_str, dev_cell),
            Paragraph(prod_type,  dev_cell),
            Paragraph(version,    dev_cell),
            Paragraph(serial,     dev_cell),
            Paragraph(uptime,     dev_cell),
            trunk_cell,
        ])
        # Colour active/inactive cell
        active_bg = colors.HexColor("#eafaf1") if is_active else colors.HexColor("#fdecea")
        dev_table_style.add("BACKGROUND", (2, row_i), (2, row_i), active_bg)

    col_widths = [W*0.12, W*0.09, W*0.07, W*0.13, W*0.13, W*0.14, W*0.10, W*0.15]
    # Adjust so widths sum exactly to W (rounding fix)
    col_widths[-1] = W - sum(col_widths[:-1])

    story.append(Table(dev_table_data, colWidths=col_widths,
                        style=dev_table_style, repeatRows=1))
    # Colour legend for trunk badges
    legend_style = ParagraphStyle("legend", parent=styles["Normal"], fontSize=7,
                                   textColor=colors.HexColor("#7f8c8d"), spaceAfter=12)
    story.append(Paragraph(
        "<b>Trunk badge colours:</b>  "
        "<font color='#27ae60'>■</font> Active &nbsp;&nbsp;"
        "<font color='#e67e22'>■</font> Disabled &nbsp;&nbsp;"
        "<font color='#c0392b'>■</font> Alarm",
        legend_style))
    story.append(Spacer(1, 6))

    # ── Executive summary table ───────────────────────────
    all_active   = [a for d in device_results  for a in d["alarms"]]
    all_history  = [a for d in history_results for a in d["alarms"]]
    act_totals   = count_by_severity(all_active)
    hist_totals  = count_by_severity(all_history)
    connected    = sum(1 for d in device_results if d["status"] == "connected")
    unreachable  = len(device_results) - connected

    summary_header = ["Metric", "Value"]
    summary_data   = [
        summary_header,
        ["Report timestamp",              ts_str],
        ["Devices polled",                str(len(device_results))],
        ["Devices reachable",             str(connected)],
        ["Devices unreachable",           str(unreachable)],
        ["── Active Alarms ──",           ""],
        ["Total active alarms",           str(len(all_active))],
        ["  Critical",                    str(act_totals["critical"])],
        ["  Major",                       str(act_totals["major"])],
        ["  Minor",                       str(act_totals["minor"])],
        ["  Warning",                     str(act_totals["warning"])],
        ["  Unknown severity",            str(act_totals["unknown"])],
        [f"── Historical Alarms (last {HISTORY_DAYS} days) ──", ""],
        ["Total historical alarms",       str(len(all_history))],
        ["  Critical",                    str(hist_totals["critical"])],
        ["  Major",                       str(hist_totals["major"])],
        ["  Minor",                       str(hist_totals["minor"])],
        ["  Warning",                     str(hist_totals["warning"])],
        ["  Unknown severity",            str(hist_totals["unknown"])],
    ]

    def severity_row_color(label):
        mapping = {
            "Critical":  colors.HexColor("#fdecea"),
            "Major":     colors.HexColor("#fef3e2"),
            "Minor":     colors.HexColor("#fefde7"),
            "Warning":   colors.HexColor("#eaf4fb"),
        }
        return mapping.get(label)

    summary_ts = TableStyle([
        ("BACKGROUND",  (0, 0), (-1, 0), colors.HexColor("#2c3e50")),
        ("TEXTCOLOR",   (0, 0), (-1, 0), colors.white),
        ("FONTNAME",    (0, 0), (-1, 0), "Helvetica-Bold"),
        ("FONTSIZE",    (0, 0), (-1,-1), 9),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1),
         [colors.HexColor("#f8f9fa"), colors.white]),
        ("GRID",        (0, 0), (-1, -1), 0.5, colors.HexColor("#dee2e6")),
        ("LEFTPADDING", (0, 0), (-1, -1), 8),
        ("RIGHTPADDING",(0, 0), (-1, -1), 8),
        ("TOPPADDING",  (0, 0), (-1, -1), 5),
        ("BOTTOMPADDING",(0,0), (-1, -1), 5),
    ])
    # Colour the severity rows
    for i, row in enumerate(summary_data[1:], start=1):
        bg = severity_row_color(row[0])
        if bg:
            summary_ts.add("BACKGROUND", (0, i), (-1, i), bg)

    # ── Active Alarms — consolidated table ───────────────
    story.append(Paragraph("Active Alarms", styles["Heading2"]))
    story.append(Spacer(1, 6))

    def fmt_alarm_ts(alarm: dict) -> str:
        """Return a formatted timestamp string from an alarm dict, or blank."""
        ts = _parse_alarm_timestamp(alarm)
        return ts.strftime("%Y-%m-%d %H:%M") if ts else ""

    act_hdr_style = ParagraphStyle("acthdr", parent=styles["Normal"],
                                    fontSize=7.5, textColor=colors.white)
    act_cell_style = ParagraphStyle("actcell", parent=styles["Normal"], fontSize=7.5)

    ACT_COLS = [
        ("device",      "Device",      W * 0.12),
        ("country",     "Country",     W * 0.09),
        ("severity",    "Severity",    W * 0.09),
        ("description", "Description", W * 0.32),
        ("source",      "Source",      W * 0.18),
        ("id",          "ID",          W * 0.05),
        ("datetime",    "Date/Time",   W * 0.15),
    ]
    ACT_COLS = list(ACT_COLS)
    ACT_COLS[-1] = (ACT_COLS[-1][0], ACT_COLS[-1][1],
                    W - sum(c[2] for c in ACT_COLS[:-1]))

    act_table_data = [[
        Paragraph(f"<b>{label}</b>", act_hdr_style)
        for _, label, _ in ACT_COLS
    ]]
    act_table_style = TableStyle([
        ("BACKGROUND",    (0, 0), (-1, 0), colors.HexColor("#34495e")),
        ("TEXTCOLOR",     (0, 0), (-1, 0), colors.white),
        ("FONTSIZE",      (0, 0), (-1,-1), 7.5),
        ("ROWBACKGROUNDS",(0, 1), (-1,-1),
         [colors.HexColor("#f8f9fa"), colors.white]),
        ("GRID",          (0, 0), (-1,-1), 0.4, colors.HexColor("#bdc3c7")),
        ("LEFTPADDING",   (0, 0), (-1,-1), 4),
        ("RIGHTPADDING",  (0, 0), (-1,-1), 4),
        ("TOPPADDING",    (0, 0), (-1,-1), 3),
        ("BOTTOMPADDING", (0, 0), (-1,-1), 3),
        ("VALIGN",        (0, 0), (-1,-1), "TOP"),
    ])

    any_active = False
    global_row = 1
    for device in device_results:
        ip      = device["ip"]
        country = DEVICE_COUNTRY.get(ip, "")
        alarms  = device["alarms"]
        if not alarms:
            continue
        any_active = True
        for alarm in sorted(alarms,
                             key=lambda a: SEVERITY_ORDER.get(normalize_severity(a), 99)):
            sev = normalize_severity(alarm)
            row = []
            for key, _, _ in ACT_COLS:
                if key == "device":
                    val = ip
                elif key == "country":
                    val = country
                elif key == "datetime":
                    val = fmt_alarm_ts(alarm)
                else:
                    val = str(alarm.get(key, ""))
                row.append(Paragraph(val, act_cell_style))
            act_table_data.append(row)

            # Colour severity cell (col index 2)
            bg = SEVERITY_COLORS.get(sev, colors.HexColor("#ecf0f1"))
            act_table_style.add("BACKGROUND", (2, global_row), (2, global_row), bg)
            act_table_style.add("TEXTCOLOR",  (2, global_row), (2, global_row),
                                 colors.white if sev in ("critical", "major")
                                 else colors.HexColor("#2c3e50"))
            global_row += 1

    if any_active:
        story.append(Table(act_table_data,
                            colWidths=[c[2] for c in ACT_COLS],
                            style=act_table_style,
                            repeatRows=1))
    else:
        story.append(Paragraph("  No active alarms across all devices.",
                                ParagraphStyle("noact", parent=styles["Normal"],
                                               fontSize=9,
                                               textColor=colors.HexColor("#27ae60"),
                                               spaceAfter=8)))
    story.append(Spacer(1, 18))

    # ── Historical Alarms — consolidated table ────────────
    story.append(Spacer(1, 10))
    story.append(HRFlowable(width="100%", thickness=1,
                             color=colors.HexColor("#2c3e50"), spaceBefore=4,
                             spaceAfter=8))
    story.append(Paragraph(
        f"Historical Alarms  (last {HISTORY_DAYS} days)",
        styles["Heading2"]))
    story.append(Spacer(1, 6))

    hist_hdr_style  = ParagraphStyle("histhdr",  parent=styles["Normal"],
                                      fontSize=7.5, textColor=colors.white)
    hist_cell_style = ParagraphStyle("histcell", parent=styles["Normal"], fontSize=7.5)

    HIST_COLS = [
        ("device",      "Device",      W * 0.12),
        ("country",     "Country",     W * 0.09),
        ("severity",    "Severity",    W * 0.09),
        ("description", "Description", W * 0.32),
        ("source",      "Source",      W * 0.18),
        ("id",          "ID",          W * 0.05),
        ("datetime",    "Date/Time",   W * 0.15),
    ]
    HIST_COLS = list(HIST_COLS)
    HIST_COLS[-1] = (HIST_COLS[-1][0], HIST_COLS[-1][1],
                     W - sum(c[2] for c in HIST_COLS[:-1]))

    hist_table_data = [[
        Paragraph(f"<b>{label}</b>", hist_hdr_style)
        for _, label, _ in HIST_COLS
    ]]
    hist_table_style = TableStyle([
        ("BACKGROUND",    (0, 0), (-1, 0), colors.HexColor("#4a235a")),
        ("TEXTCOLOR",     (0, 0), (-1, 0), colors.white),
        ("FONTSIZE",      (0, 0), (-1,-1), 7.5),
        ("ROWBACKGROUNDS",(0, 1), (-1,-1),
         [colors.HexColor("#f5eef8"), colors.white]),
        ("GRID",          (0, 0), (-1,-1), 0.4, colors.HexColor("#bdc3c7")),
        ("LEFTPADDING",   (0, 0), (-1,-1), 4),
        ("RIGHTPADDING",  (0, 0), (-1,-1), 4),
        ("TOPPADDING",    (0, 0), (-1,-1), 3),
        ("BOTTOMPADDING", (0, 0), (-1,-1), 3),
        ("VALIGN",        (0, 0), (-1,-1), "TOP"),
    ])

    def sort_key(a):
        ts = _parse_alarm_timestamp(a)
        return ts if ts else datetime.min.replace(tzinfo=timezone.utc)

    any_history = False
    global_hrow = 1
    for device in history_results:
        ip      = device["ip"]
        country = DEVICE_COUNTRY.get(ip, "")
        alarms  = device["alarms"]
        if not alarms:
            continue
        any_history = True
        for alarm in sorted(alarms, key=sort_key, reverse=True):
            sev = normalize_severity(alarm)
            row = []
            for key, _, _ in HIST_COLS:
                if key == "device":
                    val = ip
                elif key == "country":
                    val = country
                elif key == "datetime":
                    val = fmt_alarm_ts(alarm)
                else:
                    val = str(alarm.get(key, ""))
                row.append(Paragraph(val, hist_cell_style))
            hist_table_data.append(row)

            # Colour severity cell (col index 2)
            bg = SEVERITY_COLORS.get(sev, colors.HexColor("#ecf0f1"))
            hist_table_style.add("BACKGROUND", (2, global_hrow), (2, global_hrow), bg)
            hist_table_style.add("TEXTCOLOR",  (2, global_hrow), (2, global_hrow),
                                  colors.white if sev in ("critical", "major")
                                  else colors.HexColor("#2c3e50"))
            global_hrow += 1

    if any_history:
        story.append(Table(hist_table_data,
                            colWidths=[c[2] for c in HIST_COLS],
                            style=hist_table_style,
                            repeatRows=1))
    else:
        story.append(Paragraph(
            f"  No historical alarms across all devices in the last {HISTORY_DAYS} days.",
            ParagraphStyle("nohist", parent=styles["Normal"],
                           fontSize=9,
                           textColor=colors.HexColor("#27ae60"),
                           spaceAfter=8)))
    story.append(Spacer(1, 14))

    # ── Executive Summary ─────────────────────────────────
    story.append(Spacer(1, 10))
    story.append(HRFlowable(width="100%", thickness=1,
                             color=colors.HexColor("#2c3e50"), spaceBefore=4,
                             spaceAfter=8))
    story.append(Paragraph("Executive Summary", styles["Heading2"]))
    story.append(Spacer(1, 6))
    story.append(Table(summary_data,
                        colWidths=[W * 0.55, W * 0.45],
                        style=summary_ts))
    story.append(Spacer(1, 18))

    # ── Footer note ───────────────────────────────────────
    story.append(HRFlowable(width="100%", thickness=0.5,
                             color=colors.HexColor("#bdc3c7"), spaceBefore=8))
    story.append(
        Paragraph("This report was generated automatically by the AudioCodes "
                  "Alarm Reporter script. Severity colour coding: "
                  "Critical (red), Major (orange), Minor (yellow), Warning (blue).",
                  ParagraphStyle("footer", parent=styles["Normal"],
                                 fontSize=7.5,
                                 textColor=colors.HexColor("#95a5a6"),
                                 spaceAfter=0))
    )

    doc.build(story)
    log.info("PDF saved to: %s", output_path)



# ─────────────────────────────────────────────
#  PUBLIC API
# ─────────────────────────────────────────────

def collect(config: dict) -> dict:
    """
    Poll all devices and return raw data without generating a PDF.
    Called by main.py so pdf_merger can build the unified document.

    Returns:
        {
            "active_results":   list[dict],   # per-device active alarm results
            "history_results":  list[dict],   # per-device historical alarm results
            "device_statuses":  dict[str,dict],# ip -> status info
        }
    """
    _apply_config(config.get("audiocodes", {}))
    log.info("AudioCodes collect() starting — %d device(s)", len(AUDIOCODES_DEVICES))
    t_start = datetime.now()

    with ThreadPoolExecutor(max_workers=3) as ex:
        f_status  = ex.submit(fetch_all_device_statuses,  AUDIOCODES_DEVICES)
        f_active  = ex.submit(poll_all_devices,            AUDIOCODES_DEVICES)
        f_history = ex.submit(fetch_history_all_devices,   AUDIOCODES_DEVICES)
        device_statuses = f_status.result()
        active_results  = f_active.result()
        history_results = f_history.result()

    elapsed = (datetime.now() - t_start).total_seconds()
    total_active  = sum(len(d["alarms"]) for d in active_results)
    total_history = sum(len(d["alarms"]) for d in history_results)

    # ── Consolidated reachability summary ─────────────────
    unreachable = [d["ip"] for d in active_results if d["status"] != "connected"]
    connected   = len(active_results) - len(unreachable)
    log.info("AudioCodes collect() complete in %.1fs — reachable: %d/%d  "
             "active alarms: %d  historical: %d",
             elapsed, connected, len(active_results), total_active, total_history)
    if unreachable:
        log.warning("AudioCodes — %d device(s) UNREACHABLE:", len(unreachable))
        for ip in unreachable:
            result = next((d for d in active_results if d["ip"] == ip), {})
            reason = result.get("error") or result.get("status") or "unknown"
            log.warning("  ✗  %-18s  %s", ip,
                        DEVICE_COUNTRY.get(ip, "") + (f"  [{reason}]" if reason else ""))
    else:
        log.info("AudioCodes — all %d device(s) reachable.", len(active_results))

    return {
        "active_results":  active_results,
        "history_results": history_results,
        "device_statuses": device_statuses,
    }


def run(config: dict) -> str:
    """
    Module entry point for use by main.py.

    Accepts the full application config dict, reads the audiocodes: namespace,
    runs all polling concurrently, builds the PDF, and returns the output path.

    Returns the PDF file path on success, or raises an exception on failure
    (main.py catches exceptions per-module and notes the failure).
    """
    _apply_config(config.get("audiocodes", {}))

    report_dir    = config.get("reporting", {}).get("reportDir", ".")
    prefix        = config.get("reporting", {}).get("filenamePrefix", "healthcheck_report")
    ts_str        = datetime.now().strftime("%Y%m%d_%H%M%S")
    output_path   = os.path.join(report_dir, f"{prefix}_audiocodes_{ts_str}.pdf")

    log.info("AudioCodes reporter starting — output: %s", output_path)
    t_start = datetime.now()

    with ThreadPoolExecutor(max_workers=3) as ex:
        f_status  = ex.submit(fetch_all_device_statuses,  AUDIOCODES_DEVICES)
        f_active  = ex.submit(poll_all_devices,            AUDIOCODES_DEVICES)
        f_history = ex.submit(fetch_history_all_devices,   AUDIOCODES_DEVICES)
        device_statuses = f_status.result()
        active_results  = f_active.result()
        history_results = f_history.result()

    elapsed       = (datetime.now() - t_start).total_seconds()
    total_active  = sum(len(d["alarms"]) for d in active_results)
    total_history = sum(len(d["alarms"]) for d in history_results)
    connected     = sum(1 for d in active_results if d["status"] == "connected")
    log.info("Polling complete in %.1fs — reachable: %d/%d | active: %d | historical: %d",
             elapsed, connected, len(active_results), total_active, total_history)

    os.makedirs(report_dir, exist_ok=True)
    build_pdf(active_results, history_results, device_statuses, output_path)
    log.info("AudioCodes reporter done — PDF: %s", output_path)
    return output_path


# ─────────────────────────────────────────────
#  STANDALONE ENTRY POINT
# ─────────────────────────────────────────────

_STANDALONE_CONFIG = "healthcheck_config.yaml"

def main() -> None:
    """
    Standalone entry point — reads healthcheck_config.yaml directly,
    sets up its own logging, runs the reporter, and exits.
    Email is NOT sent here — use main.py for the full pipeline.
    """
    logging.basicConfig(
        level=logging.DEBUG,
        format="%(asctime)s  %(levelname)-8s  %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        handlers=[
            logging.FileHandler("audiocodes_alarm_reporter.log", encoding="utf-8"),
            logging.StreamHandler(),
        ],
    )
    log.info("=" * 50)
    log.info("AudioCodes Alarm Reporter — standalone mode")
    log.info("=" * 50)

    if not os.path.exists(_STANDALONE_CONFIG):
        log.error("Config file not found: %s", _STANDALONE_CONFIG)
        sys.exit(1)

    with open(_STANDALONE_CONFIG, encoding="utf-8") as f:
        config = yaml.safe_load(f)

    pdf_path = run(config)
    log.info("Report saved to: %s", pdf_path)
    log.info("=" * 50)


if __name__ == "__main__":
    main()

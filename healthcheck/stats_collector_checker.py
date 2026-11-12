"""
healthcheck/stats_collector_checker.py
──────────────────────────────────────
Stats Collector export check — ported from the C# Cloud Health Check
(StatsCollected(), v1.0.0.4 – v1.0.0.13).

Each stats collector (General, QA, WFM, ...) writes a daily audit file:
    <filePrefix><yyyyMMdd>.csv      e.g. Audit_20261112.csv
to its export location. Once the BI ETL has processed the file it is moved
into <location>/<completedFolder>  (e.g. "History").

For every configured collector this module looks for TODAY's file (server
local date) — first in <location>, then in <location>/<completedFolder> —
and, unlike the C# version which only tested existence, applies basic
sanity checks to the file it finds.

Statuses
────────
  OK       file found, has data rows, last modified today
  WARNING  file found but header-only (no data rows), or last modified
           before today
  MISSING  file not found in either folder
  EMPTY    file is 0 bytes
  ERROR    location unreachable / permission denied / unreadable file,
           or the collector entry is misconfigured

Config (healthcheck_config.yaml):
    statsCollector:
      enabled: true
      collectors:
        - name: "General Stats"
          location: "//server/share/Production"
          completedFolder: "History"
          filePrefix: "Audit_"

Note: when running as a Windows Service, the service account must be able
to read the share. LocalSystem usually cannot read network shares — run
the service under a domain account with read access.
"""

from __future__ import annotations

import logging
import os
import stat as _stat
from datetime import datetime

log = logging.getLogger(__name__)

STATUS_OK      = "OK"
STATUS_WARNING = "WARNING"
STATUS_MISSING = "MISSING"
STATUS_EMPTY   = "EMPTY"
STATUS_ERROR   = "ERROR"

# Severity bucket per status — used by the PDF and email for colouring
STATUS_SEVERITY = {
    STATUS_OK:      "ok",
    STATUS_WARNING: "warning",
    STATUS_MISSING: "critical",
    STATUS_EMPTY:   "critical",
    STATUS_ERROR:   "critical",
}


# ─────────────────────────────────────────────
#  CONFIG
# ─────────────────────────────────────────────

def is_enabled(config: dict) -> bool:
    """True when the statsCollector section exists and is enabled."""
    cfg = config.get("statsCollector") or {}
    return bool(cfg.get("enabled", False))


# ─────────────────────────────────────────────
#  HELPERS
# ─────────────────────────────────────────────

def _fmt_size(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n} B"


def _count_data_rows(path: str) -> int:
    """
    Count non-blank lines minus the header line.
    Streams the file so large exports don't load into memory.
    """
    lines = 0
    with open(path, "r", encoding="utf-8-sig", errors="replace") as f:
        for line in f:
            if line.strip():
                lines += 1
    return max(lines - 1, 0)


def _result(name: str, status: str, detail: str, **extra) -> dict:
    base = {
        "name":           name,
        "status":         status,
        "severity":       STATUS_SEVERITY[status],
        "detail":         detail,
        "file_name":      "",
        "found_path":     "",
        "found_in":       "",
        "checked_paths":  [],
        "size_bytes":     None,
        "size":           "",
        "data_rows":      None,
        "modified":       "",
    }
    base.update(extra)
    return base


# ─────────────────────────────────────────────
#  CHECK
# ─────────────────────────────────────────────

def check_collector(entry: dict, now: datetime) -> dict:
    """Check one collector's audit file for the date in `now` (local)."""
    name       = str(entry.get("name") or entry.get("filePrefix") or "Unnamed collector")
    location   = str(entry.get("location") or "").strip()
    completed  = str(entry.get("completedFolder") or "").strip()
    prefix     = str(entry.get("filePrefix") or "").strip()

    if not location or not prefix:
        log.error("[Stats] %s — config incomplete (location and filePrefix are required)", name)
        return _result(name, STATUS_ERROR,
                       "Config incomplete — 'location' and 'filePrefix' are required")

    file_name  = f"{prefix}{now.strftime('%Y%m%d')}.csv"
    candidates = [("Base folder", os.path.normpath(os.path.join(location, file_name)))]
    if completed:
        candidates.append(("Completed folder",
                           os.path.normpath(os.path.join(location, completed, file_name))))
    checked = [p for _, p in candidates]
    common  = {"file_name": file_name, "checked_paths": checked}

    log.info("[Stats] %s — looking for %s", name, file_name)

    # ── Is the location reachable at all? ─────────────────
    # os.stat surfaces the real OS error (network path not found,
    # access denied, ...), which isdir() would hide.
    loc_norm = os.path.normpath(location)
    try:
        st = os.stat(loc_norm)
    except OSError as exc:
        log.error("[Stats] %s — location unreachable: %s", name, exc)
        return _result(name, STATUS_ERROR,
                       f"Location unreachable: {exc.strerror or exc}", **common)
    if not _stat.S_ISDIR(st.st_mode):
        log.error("[Stats] %s — location is not a folder: %s", name, loc_norm)
        return _result(name, STATUS_ERROR, "Location is not a folder", **common)

    # ── Find the file ─────────────────────────────────────
    found_in = found_path = ""
    for label, path in candidates:
        try:
            if os.path.isfile(path):
                found_in, found_path = label, path
                break
        except OSError as exc:   # pragma: no cover — isfile rarely raises
            log.warning("[Stats] %s — could not check %s: %s", name, path, exc)

    if not found_path:
        log.warning("[Stats] %s — MISSING (%s)", name, " | ".join(checked))
        return _result(name, STATUS_MISSING,
                       "Today's audit file not found in base or completed folder",
                       **common)

    common.update(found_path=found_path, found_in=found_in)

    # ── Sanity checks ─────────────────────────────────────
    try:
        fst      = os.stat(found_path)
        size     = fst.st_size
        modified = datetime.fromtimestamp(fst.st_mtime)
        common.update(size_bytes=size, size=_fmt_size(size),
                      modified=modified.strftime("%Y-%m-%d %H:%M"))

        if size == 0:
            log.warning("[Stats] %s — EMPTY: %s", name, found_path)
            return _result(name, STATUS_EMPTY, "File is 0 bytes", data_rows=0, **common)

        rows = _count_data_rows(found_path)
        common["data_rows"] = rows
    except OSError as exc:
        log.error("[Stats] %s — cannot read %s: %s", name, found_path, exc)
        return _result(name, STATUS_ERROR,
                       f"File found but unreadable: {exc.strerror or exc}", **common)

    problems = []
    if rows == 0:
        problems.append("header only — no data rows")
    if modified.date() != now.date():
        problems.append(f"last modified {modified.strftime('%Y-%m-%d %H:%M')}, not today")

    if problems:
        detail = "; ".join(problems).capitalize()
        log.warning("[Stats] %s — WARNING: %s (%s)", name, detail, found_path)
        return _result(name, STATUS_WARNING, detail, **common)

    log.info("[Stats] %s — OK: %d data row(s), %s, %s (%s)",
             name, rows, common["size"], common["modified"], found_in)
    return _result(name, STATUS_OK, f"{rows} data row(s)", **common)


def collect(config: dict) -> dict:
    """
    Check every configured collector.

    Returns:
        {
            "date":    "YYYY-MM-DD",      # local date the files were checked for
            "results": list[dict],        # one per collector, see _result()
        }
    Raises RuntimeError if the section has no collectors configured.
    """
    cfg        = config.get("statsCollector") or {}
    collectors = cfg.get("collectors") or []
    if not collectors:
        raise RuntimeError("statsCollector.enabled is true but no collectors are configured")

    now = datetime.now()   # local time — matches the C# DateTime.Now behaviour
    log.info("Stats collector check starting — %d collector(s), date %s",
             len(collectors), now.strftime("%Y-%m-%d"))

    results = []
    for entry in collectors:
        try:
            results.append(check_collector(entry or {}, now))
        except Exception as exc:   # one bad entry must not hide the others
            name = str((entry or {}).get("name", "Unnamed collector"))
            log.error("[Stats] %s — unexpected error: %s", name, exc, exc_info=True)
            results.append(_result(name, STATUS_ERROR, f"Unexpected error: {exc}"))

    ok = sum(1 for r in results if r["status"] == STATUS_OK)
    log.info("Stats collector check complete — %d/%d OK", ok, len(results))
    return {"date": now.strftime("%Y-%m-%d"), "results": results}


def summarise(data: dict) -> dict:
    """Compact summary for the email body."""
    results = data.get("results", [])
    return {
        "date":   data.get("date", ""),
        "total":  len(results),
        "ok":     sum(1 for r in results if r["status"] == STATUS_OK),
        "issues": [
            {
                "name":     r["name"],
                "status":   r["status"],
                "severity": r["severity"],
                "detail":   r["detail"],
                "path":     r["found_path"] or " | ".join(r["checked_paths"]),
            }
            for r in results if r["status"] != STATUS_OK
        ],
    }

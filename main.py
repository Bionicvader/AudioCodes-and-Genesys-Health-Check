"""
main.py
───────
Entry point for the Altron Contact Centre Health Check application.

Reports
───────
  healthcheck  (default) — AudioCodes + Genesys infrastructure health
      1. Load healthcheck_config.yaml
      2. Configure logging
      3. Run AudioCodes, Genesys and (if enabled) Stats Collector checks
         concurrently
      4. Build the merged PDF via pdf_merger.build()
      5. Send the report email via email_sender.send()

  user-login — Genesys Cloud users not logged in for N days / never logged in
      1. Load config, configure logging
      2. Collect + classify active users
      3. Build PDF + CSV
      4. Send via email_sender.send_user_login_report()

Usage
─────
  python main.py
  python main.py --report user-login
  python main.py --config /path/to/custom_config.yaml --report user-login

Both health check modules are fault-isolated: if one fails, the other's data
is still included in the report and the email is sent with a failure note.
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone

import yaml


# ─────────────────────────────────────────────
#  LOGGING  —  configured before any imports
#  that might trigger module-level log calls
# ─────────────────────────────────────────────

# Temporary bootstrap log to stderr only — captures config load errors.
# Replaced by the full timestamped log handler in _configure_logging().
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(name)s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[logging.StreamHandler(sys.stdout)],
)
log = logging.getLogger("healthcheck.main")


def _configure_logging(config: dict, run_ts: datetime,
                       prefix: str = "healthcheck") -> logging.Handler:
    """
    Build a timestamped log file path from config reporting.logDir,
    apply the configured log level to all handlers, and add the file
    handler to the root logger.

    Returns the FileHandler so the caller can remove it when the run
    finishes. This matters inside the Windows Service, where the process
    (and root logger) lives across many runs — without removal, every
    later run would also write into every earlier run's log file.

    Log file format: <prefix>_YYYYMMDD_HHMMSS.log
    Defaults: logDir = "." (same folder as main.py), logLevel = INFO.
    """
    reporting = config.get("reporting", {})
    log_dir   = reporting.get("logDir",   ".")
    raw_level = reporting.get("logLevel", "INFO").upper()
    level     = getattr(logging, raw_level, logging.INFO)

    os.makedirs(log_dir, exist_ok=True)
    log_file = os.path.join(log_dir,
                            f"{prefix}_{run_ts.strftime('%Y%m%d_%H%M%S')}.log")

    root = logging.getLogger()
    root.setLevel(level)

    # Replace bootstrap StreamHandler level and add timestamped FileHandler
    for handler in root.handlers:
        handler.setLevel(level)

    file_handler = logging.FileHandler(log_file, encoding="utf-8")
    file_handler.setLevel(level)
    file_handler.setFormatter(logging.Formatter(
        fmt="%(asctime)s  %(levelname)-8s  %(name)s  %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    ))
    root.addHandler(file_handler)

    log.info("Log file       : %s", log_file)
    log.info("Log level set to %s", logging.getLevelName(level))
    return file_handler


def _release_log_handler(handler: logging.Handler | None) -> None:
    """Detach and close a per-run FileHandler."""
    if handler is None:
        return
    logging.getLogger().removeHandler(handler)
    try:
        handler.close()
    except Exception:  # noqa: BLE001
        pass


# ─────────────────────────────────────────────
#  LOCAL IMPORTS  —  after logging is set up
# ─────────────────────────────────────────────

from healthcheck import audiocodes_alarm_reporter   as ac_module
from healthcheck import genesys_health_reporter     as gc_module
from healthcheck import genesys_user_login_reporter as ul_module
from healthcheck import stats_collector_checker     as sc_module
from healthcheck import pdf_merger
from healthcheck import email_sender


# ─────────────────────────────────────────────
#  CONFIG
# ─────────────────────────────────────────────

DEFAULT_CONFIG = "healthcheck_config.yaml"


def load_config(config_path: str) -> dict:
    """
    Load and return the YAML config.
    Raises RuntimeError on missing file or parse error so callers
    (including the Windows Service) can handle it gracefully.
    """
    if not os.path.exists(config_path):
        raise RuntimeError(f"Config file not found: {config_path}")
    try:
        with open(config_path, encoding="utf-8") as f:
            config = yaml.safe_load(f)
        log.info("Config loaded: %s", config_path)
        return config
    except yaml.YAMLError as exc:
        raise RuntimeError(f"Failed to parse config {config_path}: {exc}") from exc


# ─────────────────────────────────────────────
#  MODULE RUNNERS
# ─────────────────────────────────────────────

def _run_audiocodes(config: dict) -> dict | None:
    """
    Run the AudioCodes collector.
    Returns the data dict on success, None on any failure.
    """
    try:
        log.info("[AudioCodes] Starting collector ...")
        data = ac_module.collect(config)
        log.info("[AudioCodes] Collector complete.")
        return data
    except Exception as exc:
        log.error("[AudioCodes] Collector failed: %s", exc, exc_info=True)
        return None


def _run_genesys(config: dict) -> dict | None:
    """
    Run the Genesys Cloud collector.
    Returns the data dict on success, None on any failure.
    """
    try:
        log.info("[Genesys] Starting collector ...")
        data = gc_module.collect(config)
        log.info("[Genesys] Collector complete.")
        return data
    except Exception as exc:
        log.error("[Genesys] Collector failed: %s", exc, exc_info=True)
        return None


def _run_stats(config: dict) -> dict | None:
    """
    Run the Stats Collector export check.
    Returns the data dict on success, None on any failure.
    """
    try:
        log.info("[Stats] Starting collector ...")
        data = sc_module.collect(config)
        log.info("[Stats] Collector complete.")
        return data
    except Exception as exc:
        log.error("[Stats] Collector failed: %s", exc, exc_info=True)
        return None


# ─────────────────────────────────────────────
#  HEALTH CHECK REPORT
# ─────────────────────────────────────────────


def _build_oos_trunks(trunks: list) -> list:
    """
    Deduplicate out-of-service trunks by name.
    Each trunk appears once with a comma-separated list of offline edges.
    """
    from collections import defaultdict
    trunk_edges: dict = defaultdict(list)
    for t in trunks:
        if t.get("severity") == "critical":
            name      = t.get("name", "")
            edge_name = t.get("edge_name", "")
            if edge_name and edge_name not in trunk_edges[name]:
                trunk_edges[name].append(edge_name)
    return [
        {
            "name":   name,
            "edges":  ", ".join(edges) if edges else "—",
        }
        for name, edges in sorted(trunk_edges.items())
    ]


def run(config_path: str = DEFAULT_CONFIG) -> None:
    """
    Execute one full health check run.
    Called directly by the Windows Service on each scheduled trigger,
    and by main() when run from the command line.
    Raises on config errors; all other failures are logged and handled.
    """
    report_ts = datetime.now(tz=timezone.utc)

    log.info("=" * 60)
    log.info("Contact Centre Health Check — starting")
    log.info("Report timestamp : %s", report_ts.strftime("%Y-%m-%d %H:%M:%S UTC"))
    log.info("=" * 60)

    # ── 1. Load config ────────────────────────────────────
    config  = load_config(config_path)
    handler = _configure_logging(config, report_ts)
    try:
        _run_health_check(config, report_ts)
    finally:
        _release_log_handler(handler)


def _run_health_check(config: dict, report_ts: datetime) -> None:
    """Body of the health check run (logging already configured)."""
    # ── 2. Run collectors concurrently ───────────────────
    sc_enabled = sc_module.is_enabled(config)
    log.info("[1/3] Running collectors (AudioCodes + Genesys%s) in parallel ...",
             " + Stats Collectors" if sc_enabled else "")
    t_start = datetime.now()

    ac_data: dict | None = None
    gc_data: dict | None = None
    sc_data: dict | None = None

    with ThreadPoolExecutor(max_workers=3) as pool:
        futures = {
            pool.submit(_run_audiocodes, config): "AudioCodes",
            pool.submit(_run_genesys,    config): "Genesys",
        }
        if sc_enabled:
            futures[pool.submit(_run_stats, config)] = "Stats"
        for future in as_completed(futures):
            module_name = futures[future]
            result = future.result()          # exceptions already caught inside runners
            if module_name == "AudioCodes":
                ac_data = result
            elif module_name == "Genesys":
                gc_data = result
            else:
                sc_data = result

    elapsed = (datetime.now() - t_start).total_seconds()
    ac_ok   = ac_data is not None
    gc_ok   = gc_data is not None
    sc_ok   = sc_data is not None
    log.info(
        "Collectors done in %.1fs — AudioCodes: %s  |  Genesys: %s  |  Stats: %s",
        elapsed,
        "OK" if ac_ok else "FAILED",
        "OK" if gc_ok else "FAILED",
        ("OK" if sc_ok else "FAILED") if sc_enabled else "DISABLED",
    )

    # module name → data (None = failed). Disabled modules are left out.
    module_results = {
        "AudioCodes":    ac_data,
        "Genesys Cloud": gc_data,
    }
    if sc_enabled:
        module_results["Stats Collectors"] = sc_data

    if all(v is None for v in module_results.values()):
        log.error("All collectors failed — aborting PDF generation.")
        email_sender.send(config, None, module_results)
        log.info("Failure notification email sent.")
        return

    # ── 3. Build merged PDF ───────────────────────────────
    log.info("[2/3] Building merged PDF report ...")
    try:
        pdf_path = pdf_merger.build(config, ac_data, gc_data, report_ts,
                                    sc_data=sc_data, sc_enabled=sc_enabled)
        log.info("PDF report saved: %s", pdf_path)
    except Exception as exc:
        log.error("PDF generation failed: %s", exc, exc_info=True)
        pdf_path = None

    # ── 4. Send email ─────────────────────────────────────
    log.info("[3/3] Sending email ...")

    # Build summary dicts for the HTML email body
    ac_summary = None
    if ac_data:
        active_results  = ac_data.get("active_results",  [])
        history_results = ac_data.get("history_results", [])
        unreachable     = [d for d in active_results if d.get("status") != "connected"]
        device_country  = config.get("audiocodes", {}).get("deviceCountry", {})

        # Build one entry per device that has at least one Critical alarm
        _sev_fields = ("severity", "Severity", "alarmSeverity", "AlarmSeverity",
                       "level", "Level", "alarmLevel")
        critical_devices = []
        for d in active_results:
            critical = [
                a for a in d.get("alarms", [])
                if str(next((a[f] for f in _sev_fields if f in a), "")).lower() == "critical"
            ]
            if critical:
                # Pick the first critical alarm description as the representative
                worst = critical[0]
                critical_devices.append({
                    "ip":      d["ip"],
                    "country": device_country.get(d["ip"], ""),
                    "alarm":   str(worst.get("description", ""))[:80],
                })

        ac_summary = {
            "total":            len(active_results),
            "reachable":        len(active_results) - len(unreachable),
            "active_alarms":    sum(len(d["alarms"]) for d in active_results),
            "historical_alarms":sum(len(d["alarms"]) for d in history_results),
            "history_days":     config.get("audiocodes", {}).get("historyDays", 7),
            "critical_devices": critical_devices,
            "unreachable_devices": [
                {
                    "ip":      d["ip"],
                    "country": device_country.get(d["ip"], ""),
                    "reason":  str(d.get("error") or d.get("status") or ""),
                }
                for d in unreachable
            ],
        }

    gc_summary = None
    if gc_data:
        edges  = gc_data.get("edges",  [])
        trunks = gc_data.get("trunks", [])
        gc_summary = {
            "total_edges":     len(edges),
            "connected_edges": sum(1 for e in edges  if e.get("severity") == "ok"),
            "trunks_total":    len(trunks),
            "trunks_up":       sum(1 for t in trunks if t.get("severity") == "ok"),
            "trunks_down":     sum(1 for t in trunks if t.get("severity") == "critical"),
            "offline_edges":   [
                {
                    "name":          e.get("name", ""),
                    "online_status": e.get("online_status") or "UNKNOWN",
                }
                for e in edges if e.get("severity") == "critical"
            ],
            "oos_trunks": _build_oos_trunks(trunks),
        }

    sc_summary = sc_module.summarise(sc_data) if sc_data else None

    email_sender.send(config, pdf_path, module_results, ac_summary, gc_summary,
                      sc_summary)

    # ── Summary ───────────────────────────────────────────
    total_elapsed = (datetime.now() - t_start).total_seconds()
    log.info("=" * 60)
    log.info("Health Check complete in %.1fs", total_elapsed)
    log.info("  AudioCodes   : %s", "OK"     if ac_ok   else "FAILED")
    log.info("  Genesys      : %s", "OK"     if gc_ok   else "FAILED")
    if sc_enabled:
        log.info("  Stats        : %s", (f"{sc_summary['ok']}/{sc_summary['total']} OK"
                                         if sc_summary else "FAILED"))
    log.info("  PDF report   : %s", pdf_path if pdf_path else "NOT GENERATED")
    log.info("=" * 60)


# ─────────────────────────────────────────────
#  USER LOGIN REPORT
# ─────────────────────────────────────────────

def run_user_login_report(config_path: str = DEFAULT_CONFIG) -> None:
    """
    Execute one Genesys Cloud User Login Report run.
    Called by the Windows Service on its own daily trigger, and by
    main() with --report user-login.
    Raises on config errors; all other failures are logged, and a
    failure email is sent so a silent miss is never possible.
    """
    report_ts = datetime.now(tz=timezone.utc)
    config    = load_config(config_path)
    handler   = _configure_logging(config, report_ts, prefix="userlogin")
    t_start   = datetime.now()

    try:
        log.info("=" * 60)
        log.info("Genesys Cloud User Login Report — starting")
        log.info("=" * 60)

        # ── 1. Collect ────────────────────────────────────
        try:
            data = ul_module.collect(config)
        except (Exception, SystemExit) as exc:   # authenticate() calls sys.exit on failure
            reason = str(exc) if not isinstance(exc, SystemExit) else \
                     "Genesys Cloud authentication failed — see log"
            log.error("[UserLogin] Collection failed: %s", reason, exc_info=True)
            email_sender.send_user_login_report(config, None, None, None, error=reason)
            return

        # ── 2. Build outputs (independent — one can fail alone) ──
        pdf_path = csv_path = None
        try:
            pdf_path = ul_module.build_pdf(config, data)
        except Exception as exc:
            log.error("[UserLogin] PDF generation failed: %s", exc, exc_info=True)
        try:
            csv_path = ul_module.build_csv(config, data)
        except Exception as exc:
            log.error("[UserLogin] CSV generation failed: %s", exc, exc_info=True)

        # ── 3. Email ──────────────────────────────────────
        summary = {
            "total":          data["total"],
            "recent":         data["recent"],
            "inactive_count": len(data["inactive"]),
            "never_count":    len(data["never"]),
            "days":           data["days"],
            "expand_suspect": data["expand_suspect"],
        }
        email_sender.send_user_login_report(config, pdf_path, csv_path, summary)

        log.info("=" * 60)
        log.info("User Login Report complete in %.1fs",
                 (datetime.now() - t_start).total_seconds())
        log.info("  Flagged   : %d inactive  |  %d never",
                 summary["inactive_count"], summary["never_count"])
        log.info("  PDF       : %s", pdf_path or "NOT GENERATED")
        log.info("  CSV       : %s", csv_path or "NOT GENERATED")
        log.info("=" * 60)
    finally:
        _release_log_handler(handler)


# ─────────────────────────────────────────────
#  CLI
# ─────────────────────────────────────────────

def main() -> None:
    """CLI entry point — parses arguments and runs the chosen report."""
    parser = argparse.ArgumentParser(
        description="Altron Contact Centre Health Check"
    )
    parser.add_argument(
        "--config", "-c",
        default=DEFAULT_CONFIG,
        help=f"Path to YAML config file (default: {DEFAULT_CONFIG})",
    )
    parser.add_argument(
        "--report", "-r",
        choices=["healthcheck", "user-login"],
        default="healthcheck",
        help="Which report to run (default: healthcheck)",
    )
    args = parser.parse_args()
    try:
        if args.report == "user-login":
            run_user_login_report(args.config)
        else:
            run(args.config)
    except RuntimeError as exc:
        log.error("Fatal error: %s", exc)
        sys.exit(1)


if __name__ == "__main__":
    main()

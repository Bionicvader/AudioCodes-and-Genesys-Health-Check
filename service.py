"""
service.py
──────────
Windows Service wrapper for the Altron Contact Centre Health Check.

Registers as 'AltronHealthCheck' in services.msc.
Reads schedule.times from healthcheck_config.yaml on every run so
time changes take effect without restarting the service.

Also schedules the daily Genesys Cloud User Login Report from the
userLoginReport: section (enabled + time).

Installation (run as Administrator):
    python install_service.py

Manual service control:
    sc start AltronHealthCheck
    sc stop  AltronHealthCheck

Uninstall (run as Administrator):
    python install_service.py --remove
"""

from __future__ import annotations

import logging
import os
import sys
import threading
import time
from pathlib import Path

import schedule
import win32event
import win32service
import win32serviceutil
import yaml

# ── Resolve paths relative to this file so the service works regardless
#    of the working directory Windows sets when starting it.
SERVICE_DIR  = Path(__file__).parent.resolve()
CONFIG_FILE  = SERVICE_DIR / "healthcheck_config.yaml"
DEFAULT_TIMES = ["06:00", "18:00"]

log = logging.getLogger("healthcheck.service")


# ─────────────────────────────────────────────
#  LOGGING SETUP FOR SERVICE CONTEXT
# ─────────────────────────────────────────────

def _setup_service_logging(log_dir: str, level: int) -> None:
    """
    Configure a rotating file handler scoped ONLY to the service logger
    (healthcheck.service). This keeps app-run log messages out of the
    service log — each health check run writes to its own timestamped
    file via main._configure_logging(), which adds handlers to the root
    logger separately.
    """
    os.makedirs(log_dir, exist_ok=True)
    log_file = os.path.join(log_dir, "healthcheck_service.log")

    from logging.handlers import RotatingFileHandler
    handler = RotatingFileHandler(
        log_file, maxBytes=5 * 1024 * 1024, backupCount=5, encoding="utf-8"
    )
    handler.setLevel(level)
    handler.setFormatter(logging.Formatter(
        fmt="%(asctime)s  %(levelname)-8s  %(name)s  %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    ))

    # Attach ONLY to the service logger, not the root logger
    svc_logger = logging.getLogger("healthcheck.service")
    svc_logger.setLevel(level)
    svc_logger.propagate = False   # prevent bubbling up to root logger

    # Avoid duplicate handlers on service restart
    for h in svc_logger.handlers[:]:
        svc_logger.removeHandler(h)

    svc_logger.addHandler(handler)

    # Also add a stdout handler so service start/stop is visible in SCM
    stdout_handler = logging.StreamHandler()
    stdout_handler.setLevel(level)
    stdout_handler.setFormatter(logging.Formatter(
        fmt="%(asctime)s  %(levelname)-8s  %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    ))
    svc_logger.addHandler(stdout_handler)

    log.info("Service log: %s", log_file)


# ─────────────────────────────────────────────
#  SCHEDULE MANAGEMENT
# ─────────────────────────────────────────────

def _load_times() -> list[str]:
    """Read schedule.times from the YAML config. Falls back to defaults on error."""
    try:
        with open(CONFIG_FILE, encoding="utf-8") as f:
            cfg = yaml.safe_load(f)
        times = cfg.get("schedule", {}).get("times", DEFAULT_TIMES)
        log.info("Schedule times loaded: %s", times)
        return [str(t).strip() for t in times]
    except Exception as exc:
        log.error("Failed to load schedule times: %s — using defaults %s",
                  exc, DEFAULT_TIMES)
        return DEFAULT_TIMES


def _run_health_check() -> None:
    """
    Execute one health check run, re-reading config first so schedule
    changes take effect on the next trigger without restarting the service.
    """
    log.info("── Scheduled health check starting ──")
    try:
        # Ensure service directory is on sys.path
        if str(SERVICE_DIR) not in sys.path:
            sys.path.insert(0, str(SERVICE_DIR))

        # Re-read schedule times for the next cycle
        _register_jobs()

        log.info("Importing main module from: %s", SERVICE_DIR)
        from main import run  # noqa: PLC0415
        log.info("Running health check ...")
        run(str(CONFIG_FILE))
        log.info("── Scheduled health check complete ──")

    except ImportError as exc:
        log.error("Import error — check that all dependencies are installed "
                  "and that main.py is in %s: %s", SERVICE_DIR, exc, exc_info=True)
    except RuntimeError as exc:
        log.error("Config error during health check run: %s", exc, exc_info=True)
    except Exception as exc:
        log.error("Unexpected error during health check run: %s", exc, exc_info=True)


def _load_user_login_time() -> str | None:
    """
    Read userLoginReport.enabled / .time from the YAML config.
    Returns the HH:MM time if the report is enabled, otherwise None.
    """
    try:
        with open(CONFIG_FILE, encoding="utf-8") as f:
            cfg = yaml.safe_load(f)
        rpt = cfg.get("userLoginReport", {}) or {}
        if not rpt.get("enabled", False):
            log.info("User login report disabled in config.")
            return None
        t = str(rpt.get("time", "06:00")).strip()
        log.info("User login report time loaded: %s", t)
        return t
    except Exception as exc:
        log.error("Failed to load userLoginReport config: %s — report not scheduled", exc)
        return None


def _run_user_login_report() -> None:
    """Execute one Genesys Cloud User Login Report run."""
    log.info("── Scheduled user login report starting ──")
    try:
        if str(SERVICE_DIR) not in sys.path:
            sys.path.insert(0, str(SERVICE_DIR))
        from main import run_user_login_report  # noqa: PLC0415
        run_user_login_report(str(CONFIG_FILE))
        log.info("── Scheduled user login report complete ──")
    except ImportError as exc:
        log.error("Import error — check that all dependencies are installed "
                  "and that main.py is in %s: %s", SERVICE_DIR, exc, exc_info=True)
    except RuntimeError as exc:
        log.error("Config error during user login report: %s", exc, exc_info=True)
    except (Exception, SystemExit) as exc:
        # SystemExit: genesys authenticate() exits on failure; never let it
        # escape into the service main loop and stop the service.
        log.error("Unexpected error during user login report: %s", exc, exc_info=True)


def _register_jobs() -> None:
    """
    Clear all scheduled jobs and re-register from current config.
    Registers the health check times AND the user login report, because
    schedule.clear() wipes every job — both must be re-added together.
    Jobs due at the same minute run sequentially in the service loop,
    so the two reports never overlap.
    """
    schedule.clear()
    for t in _load_times():
        schedule.every().day.at(t).do(_run_health_check)
        log.info("Scheduled daily health check at %s", t)

    ul_time = _load_user_login_time()
    if ul_time:
        schedule.every().day.at(ul_time).do(_run_user_login_report)
        log.info("Scheduled daily user login report at %s", ul_time)


# ─────────────────────────────────────────────
#  NETWORK READINESS CHECK
# ─────────────────────────────────────────────

def _wait_for_network(max_wait: int = 120, poll_interval: int = 5) -> None:
    """
    Block until the network and SSL stack appear ready, or until
    max_wait seconds have elapsed.

    Strategy:
      1. Read the first device IP from config (if available) and attempt a
         TCP connect to port 443.  A successful TCP connect — even before
         HTTPS is tried — means the stack is up.
      2. If no device is configured, fall back to a fixed 30-second sleep
         which covers the vast majority of Windows boot scenarios.

    This prevents the mass SSL-handshake failures that occur when the
    service fires concurrent HTTPS requests before the TLS stack is ready.
    """
    import socket

    probe_ip = None
    try:
        with open(CONFIG_FILE, encoding="utf-8") as f:
            cfg = yaml.safe_load(f)
        devices = cfg.get("audiocodes", {}).get("devices", [])
        if devices:
            probe_ip = str(devices[0]).strip()
    except Exception:
        pass

    if not probe_ip:
        log.info("No probe device found — waiting 30s for network stack to initialise ...")
        time.sleep(30)
        log.info("Network wait complete.")
        return

    log.info("Waiting for network readiness (probing %s:443, max %ds) ...", probe_ip, max_wait)
    deadline = time.monotonic() + max_wait
    attempt  = 0
    while time.monotonic() < deadline:
        attempt += 1
        try:
            with socket.create_connection((probe_ip, 443), timeout=5):
                log.info("Network ready after %d probe(s) — TCP connect to %s:443 succeeded.",
                         attempt, probe_ip)
                # Extra 2s buffer so the TLS/SSL context finishes loading
                time.sleep(2)
                return
        except OSError:
            remaining = max(0, int(deadline - time.monotonic()))
            log.info("Network not ready yet (attempt %d) — retrying in %ds "
                     "(%ds remaining) ...", attempt, poll_interval, remaining)
            time.sleep(poll_interval)

    log.warning("Network readiness timeout after %ds — proceeding anyway. "
                "Some SSL handshake failures may occur.", max_wait)


# ─────────────────────────────────────────────
#  WINDOWS SERVICE CLASS
# ─────────────────────────────────────────────

class AltronHealthCheckService(win32serviceutil.ServiceFramework):
    _svc_name_         = "AltronHealthCheck"
    _svc_display_name_ = "Altron Health Check"
    _svc_description_  = (
        "Polls AudioCodes and Genesys Cloud infrastructure at scheduled times "
        "and emails a combined PDF health check report."
    )

    def __init__(self, args):
        super().__init__(args)
        self._stop_event = win32event.CreateEvent(None, 0, 0, None)
        self._running    = True

    def SvcStop(self):
        log.info("Service stop requested.")
        self.ReportServiceStatus(win32service.SERVICE_STOP_PENDING)
        self._running = False
        win32event.SetEvent(self._stop_event)

    def SvcDoRun(self):
        """Main service loop — runs until SvcStop() is called."""
        # ── Bootstrap logging ────────────────────────────────
        try:
            with open(CONFIG_FILE, encoding="utf-8") as f:
                cfg = yaml.safe_load(f)
            reporting = cfg.get("reporting", {})
            log_dir   = reporting.get("logDir",   str(SERVICE_DIR / "logs"))
            raw_level = reporting.get("logLevel", "INFO").upper()
            level     = getattr(logging, raw_level, logging.INFO)
        except Exception:
            log_dir, level = str(SERVICE_DIR / "logs"), logging.INFO

        _setup_service_logging(log_dir, level)
        log.info("=" * 56)
        log.info("AltronHealthCheck service starting")
        log.info("Service dir : %s", SERVICE_DIR)
        log.info("Config file : %s", CONFIG_FILE)
        log.info("=" * 56)

        # ── Register scheduled jobs ──────────────────────────
        try:
            _register_jobs()
        except Exception as exc:
            log.error("Failed to register scheduled jobs: %s", exc, exc_info=True)
            self.ReportServiceStatus(win32service.SERVICE_STOPPED)
            return

        # ── Wait for network + SSL stack to be fully ready ──────
        # When Windows starts the service automatically (boot or crash
        # recovery), the TCP/SSL stack may not be fully initialised yet.
        # Firing 10+ concurrent HTTPS connections too early causes SSL
        # handshake failures that do not occur when the service is
        # restarted manually (where the stack is already warm).
        # We probe a known device IP until we get any TCP response, or
        # fall back to a fixed delay if no devices are configured.
        _wait_for_network()

        # ── Run immediately on start if a scheduled time has already
        #    passed today — prevents missing runs when service starts
        #    after the scheduled time.
        log.info("Checking if a scheduled time has already passed today ...")
        try:
            from datetime import datetime as dt
            now = dt.now()

            def _passed(t: str) -> bool:
                h, m = map(int, t.split(":"))
                return now.replace(hour=h, minute=m, second=0, microsecond=0) <= now

            catch_up = []
            passed_hc = next((t for t in _load_times() if _passed(t)), None)
            if passed_hc:
                log.info("Health check time %s has already passed today — "
                         "running immediately.", passed_hc)
                catch_up.append(_run_health_check)

            ul_time = _load_user_login_time()
            if ul_time and _passed(ul_time):
                log.info("User login report time %s has already passed today — "
                         "running immediately.", ul_time)
                catch_up.append(_run_user_login_report)

            if catch_up:
                # One thread, run in order — the reports must not overlap,
                # otherwise their per-run log files would interleave.
                def _run_catch_up():
                    for job in catch_up:
                        job()
                threading.Thread(target=_run_catch_up, daemon=True).start()
        except Exception as exc:
            log.error("Error during startup run check: %s", exc, exc_info=True)

        # ── Main loop ────────────────────────────────────────
        log.info("Service running — waiting for scheduled triggers ...")
        while self._running:
            try:
                schedule.run_pending()
            except Exception as exc:
                log.error("Error in schedule.run_pending(): %s", exc, exc_info=True)
            # Sleep in 1-second increments so SvcStop() is responsive
            for _ in range(30):
                if not self._running:
                    break
                time.sleep(1)

        log.info("AltronHealthCheck service stopped.")


# ─────────────────────────────────────────────
#  ENTRY POINT
# ─────────────────────────────────────────────

if __name__ == "__main__":
    win32serviceutil.HandleCommandLine(AltronHealthCheckService)

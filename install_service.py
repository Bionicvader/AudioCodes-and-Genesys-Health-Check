"""
install_service.py
──────────────────
One-run installer for the AltronHealthCheck Windows Service.
Must be run from an elevated (Administrator) command prompt.

Usage:
    python install_service.py           # install and start
    python install_service.py --remove  # stop and uninstall
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path

# ─────────────────────────────────────────────
#  CONSTANTS
# ─────────────────────────────────────────────

SERVICE_NAME  = "AltronHealthCheck"
SERVICE_FILE  = Path(__file__).parent.resolve() / "service.py"
REQUIRED_PKGS = ["pywin32", "schedule", "pyyaml", "requests",
                  "reportlab", "PureCloudPlatformClientV2"]


# ─────────────────────────────────────────────
#  HELPERS
# ─────────────────────────────────────────────

def run(cmd: list[str], check: bool = True) -> subprocess.CompletedProcess:
    """Run a subprocess command, printing it first."""
    print(f"  > {' '.join(cmd)}")
    return subprocess.run(cmd, capture_output=True, text=True,
                          check=check)


def check_admin() -> None:
    try:
        import ctypes
        if not ctypes.windll.shell32.IsUserAnAdmin():
            print("[ERROR] This script must be run as Administrator.")
            print("        Right-click your terminal → 'Run as administrator'.")
            sys.exit(1)
    except AttributeError:
        print("[WARNING] Cannot verify Administrator privileges.")


def check_prerequisites() -> None:
    """Verify all required packages are installed, install missing ones."""
    print("\nChecking prerequisites ...")
    missing = []
    for pkg in REQUIRED_PKGS:
        try:
            __import__(pkg.replace("-", "_").split("[")[0].lower()
                       .replace("purecloud", "PureCloudPlatformClientV2"
                       .lower()).replace("purecloudplatformclientv2", "PureCloudPlatformClientV2"[:5].lower()))
        except ImportError:
            missing.append(pkg)

    # Simpler import check
    missing = []
    checks = {
        "pywin32":                    "win32serviceutil",
        "schedule":                   "schedule",
        "pyyaml":                     "yaml",
        "requests":                   "requests",
        "reportlab":                  "reportlab",
        "PureCloudPlatformClientV2":  "PureCloudPlatformClientV2",
    }
    for pkg, import_name in checks.items():
        try:
            __import__(import_name)
        except ImportError:
            missing.append(pkg)

    if missing:
        print(f"  Installing missing packages: {', '.join(missing)}")
        result = subprocess.run(
            [sys.executable, "-m", "pip", "install"] + missing,
            capture_output=True, text=True
        )
        if result.returncode != 0:
            print(f"  [ERROR] pip install failed:\n{result.stderr}")
            sys.exit(1)
        print("  ✓  All packages installed.")
    else:
        print("  ✓  All prerequisites satisfied.")

    # pywin32 post-install step — required after first install
    try:
        import win32serviceutil  # noqa: F401
    except ImportError:
        print("\n  pywin32 was just installed.")
        print("  Running post-install script ...")
        scripts_dir = Path(sys.executable).parent / "Scripts"
        post_install = scripts_dir / "pywin32_postinstall.py"
        if post_install.exists():
            subprocess.run([sys.executable, str(post_install), "-install"],
                           check=False)
        print("  ✓  pywin32 post-install complete.")
        print("\n  [ACTION REQUIRED] Please restart this script now.")
        print("  pywin32 requires a fresh Python process after installation.")
        sys.exit(0)


def service_exists() -> bool:
    result = subprocess.run(
        ["sc", "query", SERVICE_NAME],
        capture_output=True, text=True
    )
    return result.returncode == 0


def service_is_running() -> bool:
    result = subprocess.run(
        ["sc", "query", SERVICE_NAME],
        capture_output=True, text=True
    )
    return "RUNNING" in result.stdout


def stop_service() -> None:
    if service_is_running():
        print(f"\n  Stopping service '{SERVICE_NAME}' ...")
        subprocess.run(["sc", "stop", SERVICE_NAME],
                       capture_output=True, text=True)
        import time
        for _ in range(15):
            time.sleep(1)
            if not service_is_running():
                print("  ✓  Service stopped.")
                return
        print("  [WARNING] Service did not stop within 15s — continuing anyway.")


def configure_recovery() -> None:
    """
    Set failure recovery: restart on 1st, 2nd, and subsequent failures.
    Reset failure count after 1 day of successful operation.
    """
    print("\n  Configuring failure recovery ...")
    result = subprocess.run([
        "sc", "failure", SERVICE_NAME,
        "reset=", "86400",          # Reset failure count after 1 day
        "actions=", "restart/5000/restart/10000/restart/30000"
        # restart after 5s, 10s, 30s on 1st/2nd/3rd+ failure
    ], capture_output=True, text=True)
    if result.returncode == 0:
        print("  ✓  Recovery configured — auto-restart on failure.")
    else:
        print(f"  [WARNING] Could not configure recovery: {result.stderr.strip()}")


# ─────────────────────────────────────────────
#  INSTALL / REMOVE
# ─────────────────────────────────────────────

def install() -> None:
    print("\n" + "=" * 56)
    print("  Installing AltronHealthCheck Windows Service")
    print("=" * 56)

    check_prerequisites()

    # Import here — will work now that prerequisites are confirmed
    import win32serviceutil  # noqa: F401

    # Stop and remove existing service if present
    if service_exists():
        print(f"\n  Existing service found — reinstalling ...")
        stop_service()
        print(f"  Removing old service ...")
        result = subprocess.run(
            [sys.executable, str(SERVICE_FILE), "remove"],
            capture_output=True, text=True
        )
        if result.returncode != 0:
            print(f"  [WARNING] Remove returned: {result.stderr.strip()}")

    # Install the service
    print(f"\n  Registering service '{SERVICE_NAME}' ...")
    result = subprocess.run(
        [sys.executable, str(SERVICE_FILE), "--startup", "auto", "install"],
        capture_output=True, text=True,
        cwd=str(SERVICE_FILE.parent)
    )
    if result.returncode != 0:
        print(f"  [ERROR] Service installation failed:")
        print(f"  {result.stdout.strip()}")
        print(f"  {result.stderr.strip()}")
        sys.exit(1)
    print(f"  ✓  Service registered.")

    # Configure failure recovery
    configure_recovery()

    # Start the service
    print(f"\n  Starting service ...")
    result = subprocess.run(
        ["sc", "start", SERVICE_NAME],
        capture_output=True, text=True
    )
    if result.returncode != 0:
        print(f"  [ERROR] Failed to start service: {result.stderr.strip()}")
        print(f"  Check the service log for details.")
        sys.exit(1)

    import time
    time.sleep(3)
    running = service_is_running()

    print()
    print("=" * 56)
    if running:
        print(f"  ✓  AltronHealthCheck service is RUNNING")
    else:
        print(f"  ⚠  Service installed but may still be starting.")
        print(f"     Check: sc query {SERVICE_NAME}")
    print()
    print("  Verify in services.msc:")
    print("    Win+R → services.msc → AltronHealthCheck")
    print()
    print("  Service log location (from your config):")
    print("    reporting.logDir\\healthcheck_service.log")
    print()
    print("  To trigger an immediate test run:")
    print(f"    sc stop  {SERVICE_NAME}")
    print(f"    sc start {SERVICE_NAME}")
    print("=" * 56)


def remove() -> None:
    print("\n" + "=" * 56)
    print("  Removing AltronHealthCheck Windows Service")
    print("=" * 56)

    if not service_exists():
        print(f"\n  Service '{SERVICE_NAME}' is not installed — nothing to do.")
        return

    stop_service()

    print(f"\n  Unregistering service ...")
    result = subprocess.run(
        [sys.executable, str(SERVICE_FILE), "remove"],
        capture_output=True, text=True,
        cwd=str(SERVICE_FILE.parent)
    )
    if result.returncode == 0 or not service_exists():
        print(f"  ✓  Service '{SERVICE_NAME}' removed.")
    else:
        print(f"  [ERROR] {result.stderr.strip() or result.stdout.strip()}")
        sys.exit(1)

    print("=" * 56)


# ─────────────────────────────────────────────
#  ENTRY POINT
# ─────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Install or remove the AltronHealthCheck Windows Service"
    )
    parser.add_argument("--remove", action="store_true",
                        help="Stop and uninstall the service")
    args = parser.parse_args()

    check_admin()

    if args.remove:
        remove()
    else:
        install()


if __name__ == "__main__":
    main()

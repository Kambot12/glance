#!/usr/bin/env python3
"""Install / remove the LaunchAgent that starts glance at login.

Points at the .app bundle's executable rather than at python directly, so the
process keeps the bundle's code identity and therefore its Screen Recording
grant. Pointing launchd at .venv/bin/python would run under a different
identity and macOS would ask for permission again (and attribute it to
something confusing).

A LaunchAgent (~/Library/LaunchAgents, per-user) is the right scope: glance
captures *this user's* screen, needs their keychain-free config, and must run
in their GUI session. A LaunchDaemon runs as root with no GUI session and
cannot do any of that.
"""
from __future__ import annotations

import argparse
import plistlib
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LABEL = "com.glance.app"
PLIST = Path.home() / "Library" / "LaunchAgents" / f"{LABEL}.plist"


def write_plist(program: Path, keep_alive: bool) -> None:
    PLIST.parent.mkdir(parents=True, exist_ok=True)
    plist = {
        "Label": LABEL,
        "ProgramArguments": [str(program)],
        "RunAtLoad": True,
        # KeepAlive restarts it if it exits. Off by default: a user who quits
        # from the menu bar means it, and a relaunch loop would be maddening.
        "KeepAlive": keep_alive,
        "ProcessType": "Interactive",
        "StandardOutPath": str(Path.home() / "Library" / "Logs" / "glance.log"),
        "StandardErrorPath": str(Path.home() / "Library" / "Logs" / "glance.log"),
    }
    with open(PLIST, "wb") as fh:
        plistlib.dump(plist, fh)


def bootstrap() -> int:
    uid = subprocess.run(["id", "-u"], capture_output=True, text=True).stdout.strip()
    subprocess.run(["launchctl", "bootout", f"gui/{uid}/{LABEL}"], capture_output=True)
    r = subprocess.run(
        ["launchctl", "bootstrap", f"gui/{uid}", str(PLIST)],
        capture_output=True, text=True,
    )
    if r.returncode != 0:
        print(f"launchctl bootstrap failed: {r.stderr.strip()}", file=sys.stderr)
        return 1
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("action", choices=["install", "uninstall", "status"])
    ap.add_argument(
        "--app", default=str(ROOT / "dist" / "glance.app"),
        help="the bundle built by scripts/build_app.py",
    )
    ap.add_argument("--keep-alive", action="store_true", help="restart if it exits")
    a = ap.parse_args()

    uid = subprocess.run(["id", "-u"], capture_output=True, text=True).stdout.strip()

    if a.action == "status":
        r = subprocess.run(
            ["launchctl", "print", f"gui/{uid}/{LABEL}"], capture_output=True, text=True
        )
        print("installed" if PLIST.exists() else "not installed", f"({PLIST})")
        print("loaded" if r.returncode == 0 else "not loaded")
        return 0

    if a.action == "uninstall":
        subprocess.run(["launchctl", "bootout", f"gui/{uid}/{LABEL}"], capture_output=True)
        if PLIST.exists():
            PLIST.unlink()
        print(f"removed {PLIST}")
        return 0

    program = Path(a.app) / "Contents" / "MacOS" / "glance"
    if not program.exists():
        print(
            f"error: {program} not found.\n"
            f"Build the bundle first:  .venv/bin/python scripts/build_app.py",
            file=sys.stderr,
        )
        return 2

    write_plist(program, a.keep_alive)
    rc = bootstrap()
    print(f"installed {PLIST}")
    print(f"  runs: {program}")
    print(f"  logs: {Path.home() / 'Library' / 'Logs' / 'glance.log'}")
    print("\nglance will start at login. Remove it with:")
    print("  .venv/bin/python scripts/launch_agent.py uninstall")
    return rc


if __name__ == "__main__":
    raise SystemExit(main())

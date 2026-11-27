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
#: glance needs a model endpoint to be useful, and Ollama does not install a
#: LaunchAgent of its own - it is started by Ollama.app, by hand. Without this
#: glance comes back after a reboot and every capture fails at the model step.
#: Named under com.glance so it is obvious who installed it and what removes it.
OLLAMA_LABEL = "com.glance.ollama-serve"
AGENTS = Path.home() / "Library" / "LaunchAgents"
PLIST = AGENTS / f"{LABEL}.plist"
OLLAMA_PLIST = AGENTS / f"{OLLAMA_LABEL}.plist"


def find_ollama() -> Path | None:
    for candidate in (
        Path("/usr/local/bin/ollama"),
        Path("/opt/homebrew/bin/ollama"),
        Path("/Applications/Ollama.app/Contents/Resources/ollama"),
    ):
        if candidate.exists():
            return candidate
    return None


def write_ollama_plist(program: Path, keep_alive: bool) -> None:
    AGENTS.mkdir(parents=True, exist_ok=True)
    log = Path.home() / "Library" / "Logs" / "ollama-serve.log"
    plist = {
        "Label": OLLAMA_LABEL,
        # Stand down if something is already serving. Ollama.app starts a
        # server of its own, and two of them race for the port: whichever
        # loses exits with "address already in use" and, with KeepAlive on,
        # respawns into the same failure. This makes the agent a no-op when
        # the endpoint is already up, so the two can coexist in any order.
        "ProgramArguments": [
            "/bin/sh", "-c",
            f'/usr/sbin/lsof -nP -iTCP:11434 -sTCP:LISTEN >/dev/null 2>&1 '
            f'|| exec "{program}" serve',
        ],
        "RunAtLoad": True,
        # Off by default. Ollama.app starts a server of its own, and if the
        # port is already taken `ollama serve` exits immediately - with
        # KeepAlive on, launchd would respawn it in a tight throttled loop.
        "KeepAlive": keep_alive,
        "ProcessType": "Background",
        "EnvironmentVariables": {
            "HOME": str(Path.home()),
            "PATH": "/usr/local/bin:/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin:/sbin",
        },
        "StandardOutPath": str(log),
        "StandardErrorPath": str(log),
    }
    with open(OLLAMA_PLIST, "wb") as fh:
        plistlib.dump(plist, fh)


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


def _uid() -> str:
    return subprocess.run(["id", "-u"], capture_output=True, text=True).stdout.strip()


def boot(label: str, plist: Path) -> int:
    uid = _uid()
    subprocess.run(["launchctl", "bootout", f"gui/{uid}/{label}"], capture_output=True)
    r = subprocess.run(
        ["launchctl", "bootstrap", f"gui/{uid}", str(plist)],
        capture_output=True, text=True,
    )
    if r.returncode != 0:
        print(f"launchctl bootstrap failed for {label}: {r.stderr.strip()}",
              file=sys.stderr)
        return 1
    return 0


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


def ollama_main(a) -> int:
    uid = _uid()
    if a.action == "status":
        r = subprocess.run(["launchctl", "print", f"gui/{uid}/{OLLAMA_LABEL}"],
                           capture_output=True, text=True)
        print("installed" if OLLAMA_PLIST.exists() else "not installed",
              f"({OLLAMA_PLIST})")
        print("loaded" if r.returncode == 0 else "not loaded")
        return 0

    if a.action == "uninstall":
        subprocess.run(["launchctl", "bootout", f"gui/{uid}/{OLLAMA_LABEL}"],
                       capture_output=True)
        if OLLAMA_PLIST.exists():
            OLLAMA_PLIST.unlink()
        print(f"removed {OLLAMA_PLIST}")
        return 0

    program = find_ollama()
    if program is None:
        print("error: ollama not found. Install it from https://ollama.com, "
              "then run this again.", file=sys.stderr)
        return 2
    write_ollama_plist(program, a.keep_alive)
    rc = boot(OLLAMA_LABEL, OLLAMA_PLIST)
    print(f"installed {OLLAMA_PLIST}")
    print(f"  runs: {program} serve")
    print(f"  logs: {Path.home() / 'Library' / 'Logs' / 'ollama-serve.log'}")
    print("\nThe model endpoint will be up at login, so glance works after a "
          "reboot without opening Ollama.app.")
    return rc


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("action", choices=["install", "uninstall", "status"])
    ap.add_argument(
        "--app", default=str(ROOT / "dist" / "glance.app"),
        help="the bundle built by scripts/build_app.py",
    )
    ap.add_argument("--keep-alive", action="store_true", help="restart if it exits")
    ap.add_argument(
        "--service", choices=["glance", "ollama"], default="glance",
        help="glance itself, or the Ollama server it talks to",
    )
    a = ap.parse_args()

    if a.service == "ollama":
        return ollama_main(a)

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

#!/usr/bin/env python3
"""Build a local glance.app bundle.

Why a locally built bundle rather than a downloaded one
-------------------------------------------------------
macOS attaches Screen Recording permission to the *bundle that launches
python*, not to python. Run from a terminal and the grant belongs to
Terminal/VS Code, which is why `glance` inherits whatever the editor was
given. A bundle gets its own entry in System Settings, which is what a real
install should look like.

Building on the user's own machine also sidesteps Gatekeeper entirely:
quarantine (`com.apple.quarantine`) is set by the *downloader*, so an app you
built locally is never quarantined and needs no Developer ID, no notarization
and no $99/yr account. Shipping a prebuilt .app or .dmg from GitHub Releases
is the case that needs signing + notarization; see INSTALL.md.

The bundle is deliberately thin: it does not vendor Python or the
dependencies, it points at the project's own .venv. That keeps the build
instant and the app honest about what it runs.
"""
from __future__ import annotations

import argparse
import os
import plistlib
import shutil
import stat
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BUNDLE_ID = "com.glance.app"

LAUNCHER = """\
#!/bin/sh
# Bundle launcher. Keeps the bundle's identity as the responsible process so
# Screen Recording is granted to glance.app rather than to a terminal.
#
# The cd matters: launchd starts an agent with / as the working directory, so
# a relative "config.yaml" resolves to /config.yaml and glance exits with
# "no config at config.yaml". Only the packaged path hits this - running from
# a terminal always happens to be in the project directory.
cd "{root}" || exit 1
# launchd execs this script directly rather than going through
# LaunchServices, so __CFBundleIdentifier is NOT set in that path. Export the
# identity ourselves so glance knows it is bundled and can give permission
# instructions that name glance.app instead of the user's terminal.
GLANCE_BUNDLE_ID="{bundle_id}"
export GLANCE_BUNDLE_ID
exec "{python}" "{main}" {args}
"""


def build(dest: Path, args_line: str, sign: bool = True) -> Path:
    python = ROOT / ".venv" / "bin" / "python"
    main_py = ROOT / "main.py"
    if not python.exists():
        sys.exit(f"error: {python} not found; run `uv sync --extra hud` first")

    app = dest / "glance.app"
    if app.exists():
        shutil.rmtree(app)
    macos = app / "Contents" / "MacOS"
    resources = app / "Contents" / "Resources"
    macos.mkdir(parents=True)
    resources.mkdir(parents=True)

    info = {
        "CFBundleName": "glance",
        "CFBundleDisplayName": "glance",
        "CFBundleIdentifier": BUNDLE_ID,
        "CFBundleVersion": "0.1.0",
        "CFBundleShortVersionString": "0.1.0",
        "CFBundleExecutable": "glance",
        "CFBundlePackageType": "APPL",
        "NSHighResolutionCapable": True,
        # Agent app: no Dock icon and no menu bar. The HUD is the whole UI, and
        # its right-click menu is how you quit.
        "LSUIElement": True,
        "LSMinimumSystemVersion": "14.0",
        # Shown in the permission prompts, so say why in the user's words.
        "NSCameraUsageDescription": "glance does not use the camera.",
    }
    with open(app / "Contents" / "Info.plist", "wb") as fh:
        plistlib.dump(info, fh)

    launcher = macos / "glance"
    launcher.write_text(
        LAUNCHER.format(python=python, main=main_py, args=args_line, root=ROOT,
                        bundle_id=BUNDLE_ID)
    )
    launcher.chmod(launcher.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)

    if sign:
        # Ad-hoc signing is free and needs no account. It gives the bundle a
        # stable code identity, without which macOS can forget the Screen
        # Recording grant every time the bundle changes.
        r = subprocess.run(
            ["codesign", "--force", "--deep", "--sign", "-", str(app)],
            capture_output=True, text=True,
        )
        if r.returncode != 0:
            print(f"warning: ad-hoc codesign failed: {r.stderr.strip()}", file=sys.stderr)
    return app


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--dest", default=str(ROOT / "dist"), help="output directory")
    ap.add_argument(
        "--args", default="--hud --processor openai",
        help="arguments the bundle passes to main.py",
    )
    ap.add_argument("--no-sign", action="store_true")
    a = ap.parse_args()

    dest = Path(a.dest)
    dest.mkdir(parents=True, exist_ok=True)
    app = build(dest, a.args, sign=not a.no_sign)

    print(f"built {app}")
    print()
    print("First run (grants permission to glance.app, not to your terminal):")
    print(f"  open {app}")
    print()
    print("macOS will ask for Screen Recording the first time it captures.")
    print("If it does not, add it by hand:")
    print("  System Settings -> Privacy & Security -> Screen & System Audio Recording")
    print(f"  then drag in {app}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

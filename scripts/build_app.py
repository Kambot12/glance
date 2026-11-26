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

The interpreter has to live INSIDE the bundle
---------------------------------------------
The first version of this script wrote a shell launcher that did
`exec /path/to/.venv/bin/python main.py`. That looks fine and does not work:
`exec` REPLACES the process image, so the running binary was
/Library/Frameworks/Python.framework/.../python3.13 - outside glance.app
entirely. TCC attributes permission to the code that is actually running, so
granting Screen Recording to glance.app had no effect at all; the app kept
reporting the permission as missing no matter how often it was granted.

The fix is to copy the interpreter into Contents/MacOS/ so the running image
is inside the bundle, and to rebuild the venv layout around it:

    Contents/MacOS/glance      sh launcher, execs the interpreter beside it
    Contents/MacOS/python      a COPY of the real interpreter
    Contents/MacOS/pyvenv.cfg  isolates it from system site-packages

site-packages is supplied through PYTHONPATH rather than a symlink. Linking
Contents/lib at the project's .venv/lib works at runtime but makes the
signature invalid - `codesign --verify` rejects it with "invalid destination
for symbolic link in bundle", because the link leaves the bundle. An invalid
signature means no stable code identity, which is the whole reason the
interpreter was moved inside the bundle in the first place.

The bundle stays thin: it copies one binary and symlinks the rest, so it does
not vendor the dependencies and the build is instant.
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
# Bundle launcher.
#
# It execs the interpreter COPIED NEXT TO THIS SCRIPT, not the project's
# .venv/bin/python. exec replaces the process image, so whichever binary is
# named here is what macOS sees running - and TCC will only attribute Screen
# Recording to glance.app if that binary lives inside glance.app.
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
# site-packages comes in by PYTHONPATH. A Contents/lib symlink to the
# project's .venv would also work at runtime but invalidates the signature,
# because the link points outside the bundle.
PYTHONPATH="{site_packages}"
export PYTHONPATH
HERE=$(dirname "$0")
exec "$HERE/python" "{main}" {args}
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

    # The interpreter itself, copied in so the running image is inside the
    # bundle. Resolve the symlink: .venv/bin/python points at the framework.
    real_python = python.resolve()
    shutil.copy2(real_python, macos / "python")
    (macos / "python").chmod(0o755)

    # Rebuild the venv around the copy. Python reads pyvenv.cfg beside the
    # executable and sets sys.prefix to the PARENT directory, so lib belongs
    # at Contents/lib.
    venv_cfg = ROOT / ".venv" / "pyvenv.cfg"
    home = "/Library/Frameworks/Python.framework/Versions/3.13/bin"
    version = "3.13.14"
    if venv_cfg.exists():
        for line in venv_cfg.read_text().splitlines():
            if line.startswith("home ="):
                home = line.split("=", 1)[1].strip()
            elif line.startswith("version_info ="):
                version = line.split("=", 1)[1].strip()
    (macos / "pyvenv.cfg").write_text(
        f"home = {home}\ninclude-system-site-packages = false\n"
        f"version_info = {version}\n"
    )

    site_packages = sorted((ROOT / ".venv" / "lib").glob("python*/site-packages"))
    if not site_packages:
        sys.exit("error: no site-packages in .venv; run `uv sync --extra hud --extra sck`")

    launcher = macos / "glance"
    launcher.write_text(
        LAUNCHER.format(main=main_py, args=args_line, root=ROOT,
                        bundle_id=BUNDLE_ID, site_packages=site_packages[-1])
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

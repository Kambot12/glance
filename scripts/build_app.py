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

The main executable has to BE the running image
----------------------------------------------
macOS attributes Screen Recording to the code that is actually running, so it
is not enough for glance.app to launch python - the running image must live
inside the bundle. Two simpler designs were tried and both failed:

  * A shell launcher doing `exec .venv/bin/python main.py`. exec replaces the
    process image, so the running binary was the system Python framework,
    outside the bundle. Granting the permission to glance.app did nothing at
    all, through both LaunchServices and launchd.

  * Copying bin/python3.13 into the bundle. On a framework build that binary
    is a stub which immediately re-execs Python.app/Contents/MacOS/Python,
    so the real image was outside the bundle before Qt was even imported -
    while sys.executable still reported the bundle path, which is exactly
    what made the first fix look like it had worked. Copying Python.app's own
    binary instead is SIGKILLed (exit 137): its signature does not survive
    being moved.

So the bundle ships a small compiled launcher (scripts/launcher.c) that
embeds CPython through Py_BytesMain. Nothing re-execs, the image stays inside
glance.app, and the binary is signed as part of the bundle - which is what
gives it a stable TCC identity.

site-packages arrives through PYTHONPATH rather than a Contents/lib symlink
into the project's .venv. The symlink works at runtime but makes the
signature invalid ("invalid destination for symbolic link in bundle"), and an
invalid signature defeats the whole point.

The bundle stays thin: one small binary, no vendored dependencies.
"""
from __future__ import annotations

import argparse
import os
import plistlib
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BUNDLE_ID = "com.glance.app"

LAUNCHER_SRC = Path(__file__).resolve().parent / "launcher.c"


def python_config() -> Path:
    """The python-config for the interpreter the venv was built from."""
    base = Path(sys.executable).resolve().parent
    for name in (f"python{sys.version_info.major}.{sys.version_info.minor}-config",
                 "python3-config"):
        candidate = base / name
        if candidate.exists():
            return candidate
    sys.exit(
        "error: python-config not found next to {}.\n"
        "A framework Python (python.org or `uv python install`) is needed to "
        "build the launcher.".format(base)
    )


def compile_launcher(dest: Path, args_line: str, site_packages: Path) -> None:
    """Compile scripts/launcher.c into the bundle's main executable.

    Values are baked in at compile time rather than read at runtime: there is
    no shell to set them, and a launcher that reads a config file would just
    reintroduce a path that can go stale.
    """
    if not shutil.which("clang"):
        sys.exit(
            "error: clang not found. Install the Command Line Tools:\n"
            "  xcode-select --install"
        )
    cfg = python_config()
    includes = subprocess.run([str(cfg), "--includes"], capture_output=True,
                              text=True).stdout.split()
    ldflags = subprocess.run([str(cfg), "--ldflags", "--embed"],
                             capture_output=True, text=True).stdout.split()
    if not ldflags:  # older python-config without --embed
        ldflags = subprocess.run([str(cfg), "--ldflags"], capture_output=True,
                                 text=True).stdout.split()
    cmd = [
        "clang", "-Os", "-o", str(dest), str(LAUNCHER_SRC),
        f'-DGLANCE_ROOT="{ROOT}"',
        f'-DGLANCE_SCRIPT="{ROOT / "main.py"}"',
        f'-DGLANCE_ARGS="{args_line}"',
        f'-DGLANCE_PYTHONPATH="{site_packages}"',
        f'-DGLANCE_BUNDLE_ID="{BUNDLE_ID}"',
        *includes, *ldflags,
    ]
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        sys.exit(f"error: could not build the launcher:\n{r.stderr}")
    dest.chmod(0o755)


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

    site_packages = sorted((ROOT / ".venv" / "lib").glob("python*/site-packages"))
    if not site_packages:
        sys.exit("error: no site-packages in .venv; run `uv sync --extra hud --extra sck`")

    # pyvenv.cfg beside the executable keeps the embedded interpreter off the
    # system site-packages.
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

    compile_launcher(macos / "glance", args_line, site_packages[-1])

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

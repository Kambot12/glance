"""Pre-flight report: everything that must be true for the real path to work.

Run this before blaming the code. Exits 0 when the real path is ready, 1 when
something is missing, and prints the exact fix for each failure.
"""
from __future__ import annotations

import platform
import socket
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

OK, WARN, BAD = "  ok  ", " warn ", " FAIL "


class Report:
    def __init__(self) -> None:
        self.failed = 0

    def row(self, status: str, label: str, detail: str = "", fix: str = "") -> None:
        print(f"[{status}] {label:34s} {detail}")
        if fix:
            for line in fix.splitlines():
                print(f"         -> {line}")
        if status == BAD:
            self.failed += 1


def check_python(r: Report) -> None:
    v = sys.version_info
    ver = f"{v.major}.{v.minor}.{v.micro}"
    if (3, 11) <= (v.major, v.minor) < (3, 15):
        r.row(OK, "python", ver)
    else:
        r.row(BAD, "python", ver, "onnxruntime ships no wheel past cp314; use 3.13")


def check_imports(r: Report) -> None:
    for name, extra in [
        ("fastapi", None), ("uvicorn", None), ("openai", None), ("PIL", None),
        ("imagehash", None), ("mss", None), ("pynput", None), ("qrcode", None),
        ("rapidocr", "ocr"), ("onnxruntime", "ocr"), ("PyQt6", "hud"),
    ]:
        try:
            __import__(name)
            r.row(OK, f"import {name}")
        except ImportError:
            if extra:
                r.row(WARN, f"import {name}", f"optional ({extra} extra)",
                      f"uv sync --extra {extra}")
            else:
                r.row(BAD, f"import {name}", "missing", "uv sync")


def check_permissions(r: Report) -> None:
    if platform.system() != "Darwin":
        r.row(WARN, "macOS permissions", f"not applicable on {platform.system()}")
        return
    from glance.platform_mac import (
        accessibility_granted,
        input_monitoring_granted,
        screen_recording_granted,
    )

    if screen_recording_granted():
        r.row(OK, "screen recording", "granted")
    else:
        r.row(
            BAD, "screen recording", "DENIED",
            "System Settings -> Privacy & Security -> Screen & System Audio\n"
            "Recording -> enable your terminal / VS Code (the app that LAUNCHES\n"
            "python, not python), then fully quit and reopen it.",
        )
    ax = accessibility_granted()
    if ax:
        r.row(OK, "accessibility (hotkeys)", "granted")
    elif ax is False:
        r.row(WARN, "accessibility (hotkeys)", "denied",
              "System Settings -> Privacy & Security -> Accessibility\n"
              "(or run with --no-hotkeys)")
    else:
        r.row(WARN, "accessibility (hotkeys)", "could not determine")

    # A separate grant from Accessibility. Without it the listener starts
    # without error and never receives a keystroke, so it has to be reported
    # on its own rather than folded into the Accessibility row.
    im = input_monitoring_granted()
    if im:
        r.row(OK, "input monitoring (hotkeys)", "granted")
    elif im is False:
        r.row(WARN, "input monitoring (hotkeys)", "denied",
              "System Settings -> Privacy & Security -> Input Monitoring\n"
              "-> enable your terminal / VS Code, then fully quit and reopen it.\n"
              "Without it hotkeys stay silent even with Accessibility granted\n"
              "(or run with --no-hotkeys).")
    else:
        r.row(WARN, "input monitoring (hotkeys)", "could not determine")


def check_window_titles(r: Report) -> None:
    if platform.system() != "Darwin":
        return
    from glance.platform_mac import list_windows

    wins = list_windows()
    # None means kCGWindowName was unreadable; "" means a genuinely untitled
    # window (a menubar strip, a toolbar). Only None blocks the denylist, and
    # privacy.PrivacyGuard.check draws exactly this distinction.
    readable = sum(1 for w in wins if w.title is not None)
    if not wins:
        r.row(WARN, "window enumeration", "no windows visible")
    elif readable == len(wins):
        r.row(OK, "window titles readable", f"{readable}/{len(wins)}")
    else:
        r.row(
            WARN, "window titles readable", f"{readable}/{len(wins)}",
            "kCGWindowName needs Screen Recording. Until granted, the title\n"
            "denylist cannot be evaluated and fail-closed will skip captures.",
        )


def check_config(r: Report) -> None:
    from glance.config import ConfigError, load_config

    path = ROOT / "config.yaml"
    if not path.exists():
        r.row(BAD, "config.yaml", "missing", "cp config.example.yaml config.yaml")
        return
    try:
        cfg = load_config(path)
    except ConfigError as e:
        r.row(BAD, "config.yaml", "invalid", str(e).splitlines()[0])
        return
    r.row(OK, "config.yaml", f"{len(cfg.modes)} modes, ocr={cfg.ocr.mode}")
    if cfg.provider.api_key_env:
        import os

        if os.environ.get(cfg.provider.api_key_env):
            r.row(OK, f"${cfg.provider.api_key_env}", "set")
        else:
            r.row(BAD, f"${cfg.provider.api_key_env}", "not set",
                  f"add {cfg.provider.api_key_env}=... to .env")
    return cfg


def check_endpoint(r: Report, cfg) -> None:
    if cfg is None:
        return
    import json
    import urllib.error
    import urllib.request

    base = cfg.provider.base_url.rstrip("/")
    root = base[: -len("/v1")] if base.endswith("/v1") else base
    try:
        with urllib.request.urlopen(root + "/api/tags", timeout=4) as resp:
            models = [m["name"] for m in json.load(resp).get("models", [])]
    except (urllib.error.URLError, OSError, ValueError):
        # Not Ollama, or not reachable. A cloud endpoint is fine; just say so.
        r.row(WARN, "provider endpoint", f"{base} not probed",
              "a non-Ollama endpoint cannot be checked without spending a call")
        return
    r.row(OK, "ollama reachable", root)
    if cfg.provider.model in models:
        r.row(OK, f"model {cfg.provider.model}", "present")
    else:
        r.row(BAD, f"model {cfg.provider.model}", "not pulled",
              f"ollama pull {cfg.provider.model}\n"
              f"available: {', '.join(models) or 'none'}")


def check_port(r: Report, cfg) -> None:
    if cfg is None:
        return
    s = socket.socket()
    try:
        s.bind(("127.0.0.1", cfg.server.port))
        r.row(OK, f"port {cfg.server.port}", "free")
    except OSError:
        r.row(WARN, f"port {cfg.server.port}", "in use", "pass --port <n>")
    finally:
        s.close()


def main() -> int:
    print(f"glance doctor - {platform.system()} {platform.mac_ver()[0] or ''} "
          f"{platform.machine()}\n")
    r = Report()
    check_python(r)
    check_imports(r)
    check_permissions(r)
    check_window_titles(r)
    cfg = check_config(r)
    check_endpoint(r, cfg)
    check_port(r, cfg)

    print()
    if r.failed:
        print(f"{r.failed} blocking problem(s). The headless path still works:")
        print("  main.py --source file:assets/sample.png --processor mock --no-hotkeys")
        return 1
    print("ready: hotkey -> screen capture -> model -> web view should work.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

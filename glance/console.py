"""Console output and logging policy.

Logging rules from the spec: no image bytes, no full model responses by
default. Frame.__repr__ prints dimensions only, and the orchestrator logs
chunk/char counts rather than text.
"""
from __future__ import annotations

import logging
import socket


def setup_logging(verbose: bool = False) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)-14s %(message)s",
        datefmt="%H:%M:%S",
    )
    for noisy in ("uvicorn.error", "uvicorn.access", "httpx", "openai", "httpcore"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def local_ip() -> str:
    """Best-effort LAN address. No packet is actually sent."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("10.255.255.255", 1))
        return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        s.close()


def session_url(host: str, port: int, token: str) -> str:
    # Token in the fragment: never sent to the server, so it stays out of logs.
    return f"http://{host}:{port}/#t={token}"


def print_qr(url: str) -> None:
    try:
        import qrcode  # noqa: PLC0415
    except ImportError:
        print("  (install qrcode to print a scannable code)")
        return
    qr = qrcode.QRCode(border=1)
    qr.add_data(url)
    qr.print_ascii(invert=True)


def banner(
    *,
    url: str,
    endpoint: str,
    source: str,
    lan: bool,
    modes: list[str],
    debug_save: bool,
) -> None:
    line = "-" * 62
    print(line)
    print("  glance")
    print(line)
    print(f"  source    : {source}")
    print(f"  endpoint  : {endpoint}")
    print(f"  modes     : {', '.join(modes)}")
    print(f"  bind      : {'0.0.0.0 (LAN, token required)' if lan else '127.0.0.1 (local only)'}")
    print(f"  web view  : {url}")
    if debug_save:
        print("  WARNING   : --debug-save is on; frames are being written to disk")
    print(line)
    if lan:
        print("  Scan to open on another device on this network:")
        print_qr(url)
        print(line)

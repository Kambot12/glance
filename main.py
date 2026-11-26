#!/usr/bin/env python3
"""glance - hotkey -> screen capture -> vision model -> streaming local web view.

Thread layout (see README):
  main thread     asyncio (or Qt, once the HUD lands in step 4)
  pynput thread   GlobalHotKeys listener
  capture worker  single-thread executor, thread-local mss handle
"""
from __future__ import annotations

import argparse
import asyncio
import contextlib
import logging
import secrets
import sys
from pathlib import Path

import uvicorn
from dotenv import load_dotenv

from glance.capture import build_source
from glance.config import ConfigError, load_config
from glance.console import banner, local_ip, session_url, setup_logging
from glance.hotkeys import HotkeyListener
from glance.models import Event
from glance.ocr import build_engine
from glance.orchestrator import Orchestrator
from glance.platform_mac import preflight, warnings_
from glance.privacy import PrivacyGuard
from glance.server.app import Hub, create_app
from glance.session import SessionState
from glance.vision import build_processor

log = logging.getLogger("glance")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(prog="main.py", description=__doc__.splitlines()[0])
    p.add_argument("--config", default="config.yaml")
    p.add_argument(
        "--source",
        default="screen",
        help="'screen' (mss), 'sck' (ScreenCaptureKit; can exclude the HUD from "
             "its own capture) or 'file:<path.png>' (headless, no permissions)",
    )
    p.add_argument(
        "--processor", default="openai", choices=["openai", "mock"],
        help="'mock' runs the whole pipeline with no model and no API key",
    )
    p.add_argument("--lan", action="store_true", help="bind 0.0.0.0; token still required")
    p.add_argument("--port", type=int, default=None, help="override config server.port")
    p.add_argument("--no-hotkeys", action="store_true")
    p.add_argument("--watch", action="store_true", help="force watch mode on")
    p.add_argument(
        "--debug-save", action="store_true",
        help="write captured frames to ./debug_frames (off by default; frames are "
             "otherwise memory-only)",
    )
    p.add_argument("--hud", action="store_true", help="Qt HUD overlay (step 4, not yet built)")
    p.add_argument("-v", "--verbose", action="store_true")
    return p.parse_args(argv)


async def run_app(cfg, args) -> int:
    hub = Hub()
    inbox: asyncio.Queue[Event] = asyncio.Queue()
    token = secrets.token_urlsafe(32)

    port = args.port or cfg.server.port
    lan = args.lan or cfg.server.lan
    host = "0.0.0.0" if lan else "127.0.0.1"
    display_host = local_ip() if lan else "127.0.0.1"

    allowed_origins = {f"http://127.0.0.1:{port}", f"http://localhost:{port}"}
    if lan:
        allowed_origins.add(f"http://{display_host}:{port}")

    try:
        source = build_source(
            args.source, display=cfg.capture.display, max_edge=cfg.capture.max_edge
        )
        processor = build_processor(
            args.processor, cfg.provider, jpeg_quality=cfg.capture.jpeg_quality
        )
    except (ValueError, FileNotFoundError, RuntimeError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 2

    state = SessionState(cfg.history.max_exchanges)
    guard = PrivacyGuard(cfg.privacy)
    ocr = build_engine(cfg.ocr.mode, cfg.ocr.min_confidence)
    orch = Orchestrator(
        cfg, source, processor, guard, state, hub, inbox,
        ocr=ocr,
        debug_save=Path("debug_frames") if args.debug_save else None,
    )

    app = create_app(
        hub=hub,
        inbox=inbox,
        token=token,
        allowed_origins=allowed_origins,
        mode_names=[m.name for m in cfg.modes],
    )
    server = uvicorn.Server(
        uvicorn.Config(app, host=host, port=port, log_level="warning", access_log=False)
    )

    url = session_url(display_host, port, token)
    banner(
        url=url,
        endpoint=processor.endpoint,
        source=f"{args.source} ({source.name})",
        lan=lan,
        modes=[m.name for m in cfg.modes],
        debug_save=args.debug_save,
    )

    listener = None
    if not args.no_hotkeys:
        listener = HotkeyListener(cfg, orch.submit_from_thread)
        if not listener.start():
            listener = None
            log.warning("running without hotkeys; use the buttons in the web view")

    orch.bind_loop(asyncio.get_running_loop())
    tasks = [
        asyncio.create_task(server.serve(), name="uvicorn"),
        asyncio.create_task(orch.run(), name="orchestrator"),
    ]
    try:
        done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_EXCEPTION)
        for t in done:
            if t.exception():
                raise t.exception()
    except (KeyboardInterrupt, asyncio.CancelledError):
        pass
    finally:
        if listener:
            listener.stop()
        for t in tasks:
            t.cancel()
        for t in tasks:
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await t
        source.close()
        with contextlib.suppress(Exception):
            await processor.aclose()
    return 0


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    setup_logging(args.verbose)
    load_dotenv()

    if args.hud:
        print(
            "error: --hud is not implemented yet. It lands with the "
            "ScreenCaptureKit backend (step 4), because excluding the HUD from "
            "captures needs SCContentFilter.",
            file=sys.stderr,
        )
        return 2

    try:
        cfg = load_config(args.config)
    except ConfigError as e:
        print(f"config error: {e}", file=sys.stderr)
        return 2

    if args.watch:
        cfg.watch.enabled = True

    problems = preflight(require_capture=args.source in ("screen", "sck"))
    if problems:
        for p in problems:
            print(f"\n{p}", file=sys.stderr)
        print("Run scripts/doctor.py for a full readiness report.", file=sys.stderr)
        return 3

    if not args.no_hotkeys:
        for w in warnings_():
            log.warning(w)

    try:
        return asyncio.run(run_app(cfg, args))
    except KeyboardInterrupt:
        print("\nbye")
        return 0


if __name__ == "__main__":
    raise SystemExit(main())

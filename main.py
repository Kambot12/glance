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
import signal
import threading
import sys
from pathlib import Path

import uvicorn
from dotenv import load_dotenv

from glance.capture import build_source
from glance.config import ConfigError, load_config
from glance.console import banner, local_ip, session_url, setup_logging
from glance.hotkeys import HotkeyListener, prime_keycode_context
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
    p.add_argument(
        "--hud", action="store_true",
        help="floating always-on-top control panel (needs the 'hud' extra). "
             "Implies the ScreenCaptureKit backend so the panel is excluded "
             "from its own captures",
    )
    p.add_argument("-v", "--verbose", action="store_true")
    return p.parse_args(argv)


async def run_app(cfg, args, hud=None, bridge=None, submitter=None) -> int:
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
            args.source,
            display=cfg.capture.display,
            max_edge=cfg.capture.max_edge,
            # Read live: the HUD's window id only exists once the panel is on
            # screen, and changes if it is ever recreated.
            exclude_window_ids=(
                (lambda: [hud.window_id] if hud and hud.window_id else [])
                if hud is not None
                else None
            ),
        )
        processor = build_processor(
            args.processor, cfg.provider, jpeg_quality=cfg.capture.jpeg_quality
        )
    except (ValueError, FileNotFoundError, RuntimeError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 2

    if bridge is not None:
        # Emitting a Qt signal is the whole sink: it returns immediately and
        # Qt moves the payload to the GUI thread.
        hub.add_sink(bridge.payload.emit)

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
    if submitter is not None:
        submitter.bind(orch.submit_from_thread)
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


def run_hud(cfg, args) -> int:
    """Qt on the main thread, asyncio on a worker.

    AppKit insists on owning thread 0, so QApplication.exec() cannot be moved.
    asyncio therefore runs the whole app on a worker thread and the two sides
    talk only through Qt signals (asyncio -> GUI) and
    Orchestrator.submit_from_thread (GUI -> asyncio), both of which are
    thread-safe by design.
    """
    try:
        from PyQt6.QtCore import QTimer  # noqa: PLC0415
        from PyQt6.QtWidgets import QApplication  # noqa: PLC0415
    except ImportError:
        print(
            "error: --hud needs PyQt6.\n  uv sync --extra hud",
            file=sys.stderr,
        )
        return 2

    from glance.hud import Bridge, Hud, LateSubmitter, build_tray  # noqa: PLC0415
    from glance import singleton  # noqa: PLC0415

    # Hand over to a glance that is already running rather than competing with
    # it for the port. This is what makes clicking glance.app do something:
    # the existing instance shows its panel and this process exits.
    existing = singleton.live_pid()
    if existing is not None:
        if singleton.poke(existing):
            print(f"glance is already running (pid {existing}); showing its panel")
            return 0
        print(
            f"glance appears to be running (pid {existing}) but did not respond.\n"
            "Quit it from the menu bar, or remove "
            f"{singleton.pidfile()} if it is stale.",
            file=sys.stderr,
        )
        return 1

    app = QApplication(sys.argv[:1])
    # Hiding the panel with its × must not terminate the app; the hotkeys and
    # the web view keep working without it.
    app.setQuitOnLastWindowClosed(False)

    # Must happen on the main thread, before the listener starts on the
    # asyncio thread: pynput's listener would otherwise call Carbon Text
    # Services off-main, which HIToolbox turns into a SIGTRAP once Qt's run
    # loop is up. See hotkeys.prime_keycode_context.
    prime_keycode_context()

    submitter = LateSubmitter()
    hud = Hud(
        [(m.name, m.hotkey) for m in cfg.modes],
        submitter.submit,
        opacity=cfg.hud.opacity,
        opacity_active=cfg.hud.opacity_active,
        toggle_hotkey=cfg.hud.toggle_hotkey,
    )
    bridge = Bridge()
    bridge.payload.connect(hud.on_payload)
    bridge.done.connect(app.quit)
    hud.realise()
    hud.start_keep_on_top(cfg.hud.keep_on_top_s)
    # Held on `app` so it is not garbage collected; a dropped tray icon simply
    # vanishes from the menu bar.
    app._glance_tray = build_tray(hud, app)

    box: dict = {"rc": 0}

    def runner() -> None:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        box["loop"] = loop
        task = loop.create_task(
            run_app(cfg, args, hud=hud, bridge=bridge, submitter=submitter)
        )
        box["task"] = task
        try:
            box["rc"] = loop.run_until_complete(task)
        except asyncio.CancelledError:
            box["rc"] = 0
        except OSError as e:
            # Almost always "address already in use": another glance is still
            # running. Without this the server dies, Qt keeps the process
            # alive, and the user is left with a panel that silently does
            # nothing.
            log.error(
                "could not start the server: %s\n"
                "Another glance is probably already running - quit it from the "
                "menu bar, or start this one with --port <other>.", e,
            )
            box["rc"] = 1
        except Exception as e:  # noqa: BLE001 - last line before the thread dies
            log.error("%s: %s", type(e).__name__, e)
            box["rc"] = 1
        finally:
            with contextlib.suppress(Exception):
                loop.close()
            bridge.done.emit()

    thread = threading.Thread(target=runner, name="glance-asyncio", daemon=True)
    thread.start()

    def shutdown() -> None:
        loop, task = box.get("loop"), box.get("task")
        if loop is None or task is None or task.done() or loop.is_closed():
            # The asyncio side may already be gone - it closes its own loop
            # when run_app returns or fails - and call_soon_threadsafe on a
            # closed loop raises, turning a clean quit into a traceback.
            return
        loop.call_soon_threadsafe(task.cancel)

    app.aboutToQuit.connect(shutdown)

    # Qt's run loop does not service Python signal handlers, so a plain
    # SIGTERM (launchctl bootout, pkill) was being ignored and stale instances
    # stayed alive holding the port. A timer gives the interpreter a chance to
    # run the handler.
    def _on_term(_signum, _frame) -> None:
        log.info("terminating on signal")
        app.quit()

    def _on_reopen(_signum, _frame) -> None:
        """A second launch asking us to surface."""
        log.info("another launch asked for the panel")
        hud.reveal()

    def _on_activated(state) -> None:
        """Clicking glance.app when it is already running.

        LaunchServices does not start a second process in that case - it just
        activates this one - so the handover path never runs and, with the
        panel hidden and no Dock icon, nothing visible happened at all.
        Treating activation as "show me" is what the click means.
        """
        from PyQt6.QtCore import Qt as _Qt  # noqa: PLC0415

        if state == _Qt.ApplicationState.ApplicationActive and not hud.isVisible():
            log.info("activated; showing the panel")
            hud.reveal()

    app.applicationStateChanged.connect(_on_activated)

    signal.signal(signal.SIGTERM, _on_term)
    signal.signal(signal.SIGINT, _on_term)
    signal.signal(signal.SIGUSR1, _on_reopen)
    singleton.claim()
    wake = QTimer()
    wake.start(250)
    wake.timeout.connect(lambda: None)

    app.exec()
    singleton.release()
    thread.join(timeout=5.0)
    return box["rc"]


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    setup_logging(args.verbose)
    load_dotenv()

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

    if args.hud:
        # mss cannot leave a window out of a capture, so an mss-backed HUD
        # would photograph itself. Prefer ScreenCaptureKit and say so.
        if args.source == "screen":
            try:
                from glance.capture.sck_source import _import_sck  # noqa: PLC0415

                _import_sck()
                args.source = "sck"
                log.info("--hud: using the ScreenCaptureKit backend so the "
                         "panel can be excluded from its own captures")
            except RuntimeError as e:
                log.warning(
                    "--hud on the mss backend: the panel WILL appear in every "
                    "capture, because mss cannot exclude a window. %s",
                    str(e).splitlines()[0],
                )
        try:
            return run_hud(cfg, args)
        except KeyboardInterrupt:
            print("\nbye")
            return 0

    try:
        return asyncio.run(run_app(cfg, args))
    except KeyboardInterrupt:
        print("\nbye")
        return 0


if __name__ == "__main__":
    raise SystemExit(main())

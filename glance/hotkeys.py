"""Global hotkeys via pynput.

pynput's GlobalHotKeys only fires on the combinations registered here; it is
not a keylogger and nothing else is recorded or logged. The listener owns its
own thread (and its own CFRunLoop on macOS); callbacks hand work to the asyncio
loop through Orchestrator.submit_from_thread.
"""
from __future__ import annotations

import logging
import time

from glance.config import AppConfig
from glance.models import Event

log = logging.getLogger("glance.hotkeys")

#: How long to watch a freshly started listener thread before trusting it.
LISTENER_GRACE_S = 0.35

ACCESSIBILITY_HELP = """\
Hotkeys could not be registered. On macOS, grant your terminal / VS Code:
  System Settings -> Privacy & Security -> Accessibility
  System Settings -> Privacy & Security -> Input Monitoring
then fully quit and reopen it. Run with --no-hotkeys to skip hotkeys entirely.
"""


class HotkeyListener:
    def __init__(self, cfg: AppConfig, submit) -> None:
        self.cfg = cfg
        self.submit = submit
        self._listener = None

    def start(self) -> bool:
        try:
            from pynput import keyboard  # noqa: PLC0415
        except ImportError:
            log.error("pynput not installed; run with --no-hotkeys")
            return False

        mapping = {
            m.hotkey: self._make(Event(kind="mode", mode=m.name, manual=True))
            for m in self.cfg.modes
        }
        mapping[self.cfg.pause_hotkey] = self._make(Event(kind="pause"))
        try:
            self._listener = keyboard.GlobalHotKeys(mapping)
            self._listener.start()
        except Exception as e:
            log.error("%s (%s: %s)", ACCESSIBILITY_HELP, type(e).__name__, e)
            self._listener = None
            return False

        # start() only spawns the thread; the event tap is built inside it, so a
        # failure there never reaches this frame and start() would otherwise
        # report success for a listener that is already dead. listener.wait() is
        # not an option: pynput's first act in _run is AXIsProcessTrusted(),
        # ahead of any _mark_ready(), so a raise there leaves wait() blocked
        # forever. Poll for liveness instead.
        if not self._live_within(LISTENER_GRACE_S):
            log.error(
                "hotkey listener thread stopped on startup; continuing without "
                "hotkeys. Use the web view buttons, or --no-hotkeys to silence "
                "this.\n%s",
                ACCESSIBILITY_HELP,
            )
            self._listener = None
            return False

        for m in self.cfg.modes:
            log.info("hotkey %s -> %s", m.hotkey, m.name)
        log.info("hotkey %s -> pause/resume", self.cfg.pause_hotkey)
        return True

    def _live_within(self, grace_s: float) -> bool:
        """True if the listener thread is still alive after a startup grace."""
        deadline = time.monotonic() + grace_s
        while time.monotonic() < deadline:
            if not self._listener.is_alive():
                return False
            time.sleep(0.02)
        return self._listener.is_alive()

    def _make(self, event: Event):
        def _cb() -> None:
            self.submit(event)

        return _cb

    def stop(self) -> None:
        if self._listener is not None:
            self._listener.stop()
            self._listener = None

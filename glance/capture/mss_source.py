"""mss-backed screen capture.

mss handles are per-thread and not thread-safe, so the instance is created
lazily inside whichever thread calls grab() and kept thread-local. The
orchestrator pins capture to a single-worker executor, so in practice that is
one handle for the process lifetime.

Note: on macOS mss goes through CGDisplayCreateImage, deprecated since macOS 14.
ScreenCaptureKitSource is the planned replacement (it can also exclude the HUD
window without hiding it); this stays the portable backend.
"""
from __future__ import annotations

import threading

from PIL import Image

from glance.capture.base import prepare
from glance.models import Frame, Region


class MSSSource:
    name = "screen"
    captures_screen = True

    def __init__(self, display: int = 1, max_edge: int = 1600):
        self.display = display
        self.max_edge = max_edge
        self._local = threading.local()

    def _sct(self):
        sct = getattr(self._local, "sct", None)
        if sct is None:
            import mss  # noqa: PLC0415

            sct = mss.mss()
            self._local.sct = sct
        return sct

    def grab(self, region: Region | None = None) -> Frame:
        sct = self._sct()
        monitors = sct.monitors
        if self.display >= len(monitors):
            raise IndexError(
                f"display {self.display} not found; {len(monitors) - 1} connected"
            )
        mon = monitors[self.display]
        shot = sct.grab(mon)
        img = Image.frombytes("RGB", shot.size, shot.bgra, "raw", "BGRX")
        if img.width == 0 or img.height == 0:
            raise RuntimeError(
                "capture returned an empty frame; Screen Recording permission is "
                "probably missing or was revoked"
            )
        return prepare(img, region, self.max_edge, self.display)

    def close(self) -> None:
        sct = getattr(self._local, "sct", None)
        if sct is not None:
            sct.close()
            self._local.sct = None

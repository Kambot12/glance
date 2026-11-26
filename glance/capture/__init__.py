from __future__ import annotations

from glance.capture.base import CaptureSource, prepare
from glance.capture.file_source import FileCaptureSource

__all__ = ["CaptureSource", "FileCaptureSource", "build_source", "prepare"]


def build_source(
    spec: str,
    *,
    display: int = 1,
    max_edge: int = 1600,
    exclude_window_ids=None,
) -> CaptureSource:
    """'screen' | 'sck' | 'file:<path>' -> a CaptureSource.

    'screen' is the portable mss backend. 'sck' is ScreenCaptureKit, which is
    the only one that can leave the HUD out of its own capture; it takes
    `exclude_window_ids`, a callable returning CGWindowIDs to omit.
    """
    if spec == "screen":
        from glance.capture.mss_source import MSSSource

        return MSSSource(display=display, max_edge=max_edge)
    if spec == "sck":
        from glance.capture.sck_source import ScreenCaptureKitSource

        return ScreenCaptureKitSource(
            display=display, max_edge=max_edge, exclude_window_ids=exclude_window_ids
        )
    if spec.startswith("file:"):
        return FileCaptureSource(spec[5:], max_edge=max_edge, display=display)
    raise ValueError(
        f"unknown source {spec!r}; expected 'screen', 'sck' or 'file:<path>'"
    )

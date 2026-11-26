"""ScreenCaptureKit-backed capture, with window exclusion.

Why this exists alongside MSSSource
-----------------------------------
mss goes through CGDisplayCreateImage, deprecated since macOS 14, and it has no
way to leave a window out of the shot. That matters for the HUD: an always-on-
top panel would photograph itself on every capture and the model would spend
its context describing glance's own UI back to the user.

SCContentFilter(display:excludingWindows:) is the only macOS API that removes a
window from a capture *without hiding it*, so there is no show/hide race
against the compositor and no flicker. Measured here: exclusion costs nothing
(~68 ms per shot), and the excluded window's rectangle is demonstrably absent
from the resulting image.

Threading
---------
ScreenCaptureKit's APIs are async and call back on their own dispatch queues.
grab() blocks on a threading.Event until the callback lands, which is safe
because the orchestrator runs capture on a dedicated single-worker executor and
never on the thread owning a run loop. Blocking the main thread here would
deadlock once the Qt HUD owns it, hence the explicit guard in _await.
"""
from __future__ import annotations

import threading
from collections.abc import Callable, Iterable

from PIL import Image

from glance.capture.base import prepare
from glance.models import Frame, Region

#: How long to wait on a ScreenCaptureKit callback before giving up.
CALLBACK_TIMEOUT_S = 15.0

MIN_MACOS = (14, 0)

UNAVAILABLE = """\
ScreenCaptureKit is unavailable: {why}

It needs macOS 14+ and the pyobjc bindings:
  uv sync --extra sck

Use the portable backend instead:
  --source screen
"""


def _import_sck():
    try:
        import ScreenCaptureKit as SCK  # noqa: PLC0415
    except ImportError as e:
        raise RuntimeError(UNAVAILABLE.format(why=f"pyobjc bindings missing ({e})")) from e
    if not hasattr(SCK, "SCScreenshotManager"):
        raise RuntimeError(
            UNAVAILABLE.format(why="SCScreenshotManager needs macOS 14 or newer")
        )
    return SCK


def _await(fn, what: str, timeout: float = CALLBACK_TIMEOUT_S):
    """Run an async ScreenCaptureKit call and block until its callback fires."""
    if threading.current_thread() is threading.main_thread():
        # Not fatal today, but it becomes a deadlock the moment Qt owns the main
        # thread and this blocks the run loop that would deliver the callback.
        import warnings  # noqa: PLC0415

        warnings.warn(
            f"{what} is being awaited on the main thread; run capture on the "
            "capture executor instead",
            RuntimeWarning,
            stacklevel=3,
        )
    box: dict = {}
    done = threading.Event()

    def handler(result, error) -> None:
        # This runs inside an ObjC block on a dispatch queue. A Python
        # exception escaping here, or any non-None return, becomes an uncaught
        # NSException and aborts the *process* - not just the capture. So it
        # catches everything and hands it back through the box.
        try:
            box["result"], box["error"] = result, error
        except BaseException as exc:  # noqa: BLE001 - must not escape the block
            box["raised"] = exc
        finally:
            done.set()
        return None

    fn(handler)
    if not done.wait(timeout):
        raise RuntimeError(f"{what} did not call back within {timeout:.0f}s")
    if box.get("raised") is not None:
        raise RuntimeError(f"{what} callback failed: {box['raised']}") from box["raised"]
    if box.get("error") is not None:
        raise RuntimeError(f"{what} failed: {box['error']}")
    return box.get("result")


def _cgimage_to_pil(cgimage) -> Image.Image:
    """CGImage -> PIL, via a known-layout RGBA bitmap context.

    Drawing into a context we allocated ourselves avoids depending on the
    source image's bytes-per-row padding or colour space.
    """
    import Quartz  # noqa: PLC0415

    w = Quartz.CGImageGetWidth(cgimage)
    h = Quartz.CGImageGetHeight(cgimage)
    if w == 0 or h == 0:
        raise RuntimeError(
            "capture returned an empty frame; Screen Recording permission is "
            "probably missing or was revoked"
        )
    space = Quartz.CGColorSpaceCreateDeviceRGB()
    ctx = Quartz.CGBitmapContextCreate(
        None, w, h, 8, w * 4, space,
        Quartz.kCGImageAlphaPremultipliedLast | Quartz.kCGBitmapByteOrder32Big,
    )
    if ctx is None:
        raise RuntimeError("could not allocate a bitmap context for the capture")
    Quartz.CGContextDrawImage(ctx, Quartz.CGRectMake(0, 0, w, h), cgimage)
    data = Quartz.CGBitmapContextGetData(ctx)
    return Image.frombuffer(
        "RGBA", (w, h), data.as_buffer(w * h * 4), "raw", "RGBA", 0, 1
    ).convert("RGB")


def _backing_scale(display_id: int) -> float:
    """Pixels per point, so a Retina display is captured at native resolution."""
    import Quartz  # noqa: PLC0415

    try:
        mode = Quartz.CGDisplayCopyDisplayMode(display_id)
        pt_w = Quartz.CGDisplayModeGetWidth(mode)
        px_w = Quartz.CGDisplayModeGetPixelWidth(mode)
        if pt_w and px_w:
            return max(1.0, px_w / pt_w)
    except Exception:
        pass
    return 1.0


class ScreenCaptureKitSource:
    """Full-display capture that can leave chosen windows out of the shot.

    :param exclude_window_ids: called once per grab, returning the CGWindowIDs
        to omit. It is a callable rather than a fixed list because the HUD's
        window id only exists after the window is realised, and changes if the
        panel is recreated.
    """

    name = "sck"
    captures_screen = True

    def __init__(
        self,
        display: int = 1,
        max_edge: int = 1600,
        exclude_window_ids: Callable[[], Iterable[int]] | None = None,
        show_cursor: bool = False,
    ):
        self.display = display
        self.max_edge = max_edge
        self._exclude = exclude_window_ids or (lambda: ())
        self.show_cursor = show_cursor
        self._sck = _import_sck()

    def _shareable(self):
        content = _await(
            self._sck.SCShareableContent.getShareableContentWithCompletionHandler_,
            "SCShareableContent",
        )
        if content is None:
            raise RuntimeError("SCShareableContent returned nothing")
        return content

    def _pick_display(self, content):
        displays = list(content.displays())
        if not displays:
            raise RuntimeError(
                "no capturable displays; Screen Recording permission is probably "
                "missing or was revoked"
            )
        # `display` is 1-based to match mss's monitor numbering.
        idx = self.display - 1
        if idx < 0 or idx >= len(displays):
            raise IndexError(
                f"display {self.display} not found; {len(displays)} connected"
            )
        return displays[idx]

    def grab(self, region: Region | None = None) -> Frame:
        content = self._shareable()
        disp = self._pick_display(content)

        wanted = set(self._exclude())
        excluded = [w for w in content.windows() if w.windowID() in wanted] if wanted else []

        filt = self._sck.SCContentFilter.alloc().initWithDisplay_excludingWindows_(
            disp, excluded
        )
        scale = _backing_scale(disp.displayID())
        cfg = self._sck.SCStreamConfiguration.alloc().init()
        cfg.setWidth_(int(disp.width() * scale))
        cfg.setHeight_(int(disp.height() * scale))
        if hasattr(cfg, "setShowsCursor_"):
            cfg.setShowsCursor_(bool(self.show_cursor))

        cgimage = _await(
            lambda h: self._sck.SCScreenshotManager
            .captureImageWithFilter_configuration_completionHandler_(filt, cfg, h),
            "SCScreenshotManager.captureImage",
        )
        if cgimage is None:
            raise RuntimeError("capture returned no image")
        return prepare(_cgimage_to_pil(cgimage), region, self.max_edge, self.display)

    def close(self) -> None:
        # Nothing to release: each grab builds its own filter and configuration.
        return None

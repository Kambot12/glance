"""Windows specifics for the HUD and capture.

STATUS: written against the documented Win32 API, NOT yet run on Windows.
Everything here is behind a platform guard so it cannot affect macOS, and the
HUD degrades to "visible in its own captures" if any call fails.

The asymmetry that shapes this module
-------------------------------------
macOS and Windows put capture exclusion on opposite sides of the relationship:

  macOS    the CAPTURER names windows to leave out
           SCContentFilter(display:excludingWindows:) - per capture, so the
           HUD is absent from glance's frames and present everywhere else.

  Windows  the WINDOW opts itself out of being captured
           SetWindowDisplayAffinity(hwnd, WDA_EXCLUDEFROMCAPTURE) - a property
           of the window, honoured by every capture path on the system.

That difference is not cosmetic and it is worth being explicit about: on
Windows there is no way to hide the panel from glance alone. Excluding it from
glance's captures also hides it from Zoom, Teams, OBS and any screenshot. The
alternative - hiding the window around each capture - flickers and races the
compositor, which is exactly what the macOS path was built to avoid.

WDA_EXCLUDEFROMCAPTURE needs Windows 10 version 2004 (build 19041) or newer.
On older builds the call succeeds with WDA_MONITOR instead, which blacks the
window out rather than removing it, so we check and report.
"""
from __future__ import annotations

import logging
import platform

log = logging.getLogger("glance.platform_win")

IS_WINDOWS = platform.system() == "Windows"

WDA_NONE = 0x00
WDA_MONITOR = 0x01
WDA_EXCLUDEFROMCAPTURE = 0x11  # Windows 10 2004+

#: SetWindowPos flags for "stay on top without stealing focus".
HWND_TOPMOST = -1
SWP_NOMOVE = 0x0002
SWP_NOSIZE = 0x0001
SWP_NOACTIVATE = 0x0010


def _user32():
    import ctypes  # noqa: PLC0415

    return ctypes.windll.user32


def supports_exclude_from_capture() -> bool:
    """WDA_EXCLUDEFROMCAPTURE needs build 19041+."""
    if not IS_WINDOWS:
        return False
    try:
        build = int(platform.version().split(".")[-1])
    except (ValueError, IndexError):
        return False
    return build >= 19041


def exclude_from_capture(hwnd: int) -> bool:
    """Make this window invisible to every screen-capture path on the system.

    Returns True only if real exclusion was applied. On pre-2004 builds this
    returns False rather than silently falling back to WDA_MONITOR, which
    blacks the window out on screen instead of removing it from the capture -
    a visibly different and much worse outcome.
    """
    if not IS_WINDOWS:
        return False
    if not supports_exclude_from_capture():
        log.warning(
            "WDA_EXCLUDEFROMCAPTURE needs Windows 10 2004 (build 19041) or "
            "newer; the panel will appear in captures"
        )
        return False
    try:
        ok = bool(_user32().SetWindowDisplayAffinity(int(hwnd), WDA_EXCLUDEFROMCAPTURE))
        if not ok:
            import ctypes  # noqa: PLC0415

            log.warning(
                "SetWindowDisplayAffinity failed (GetLastError=%d)",
                ctypes.get_last_error(),
            )
        return ok
    except Exception as e:  # pragma: no cover - needs Windows
        log.warning("could not exclude the panel from capture: %s: %s",
                    type(e).__name__, e)
        return False


def set_topmost(hwnd: int) -> bool:
    """Keep the panel above other windows without taking focus.

    Windows has no equivalent of NSWindowCollectionBehaviorCanJoinAllSpaces:
    a window belongs to the virtual desktop it was created on, and pinning it
    across desktops needs the undocumented IVirtualDesktopManagerInternal COM
    interface, whose GUIDs change between Windows builds. Documented as a
    limitation rather than chased.
    """
    if not IS_WINDOWS:
        return False
    try:
        return bool(
            _user32().SetWindowPos(
                int(hwnd), HWND_TOPMOST, 0, 0, 0, 0,
                SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE,
            )
        )
    except Exception as e:  # pragma: no cover - needs Windows
        log.warning("could not pin the panel on top: %s: %s", type(e).__name__, e)
        return False

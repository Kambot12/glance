"""macOS specifics: permission preflight and window enumeration.

Measured behaviour on macOS 27.2 that shapes this module:
  * CGPreflightScreenCaptureAccess() queries without showing a dialog.
  * kCGWindowOwnerName is always readable.
  * kCGWindowName (the title) is only readable WITH Screen Recording permission,
    so a title-based denylist silently degrades without it. We therefore treat an
    unreadable title as unknown and let PrivacyConfig decide (default: skip).
"""
from __future__ import annotations

import platform
from dataclasses import dataclass

IS_MAC = platform.system() == "Darwin"


@dataclass(frozen=True)
class WindowInfo:
    owner: str | None
    title: str | None
    bounds: tuple[int, int, int, int]  # x, y, w, h
    window_id: int


def _quartz():
    try:
        import Quartz  # noqa: PLC0415
    except ImportError as e:  # pragma: no cover - only on a broken install
        raise RuntimeError(
            "pyobjc Quartz bindings missing; reinstall with: uv sync"
        ) from e
    return Quartz


def screen_recording_granted() -> bool | None:
    """True/False on macOS, None where the concept does not apply."""
    if not IS_MAC:
        return None
    return bool(_quartz().CGPreflightScreenCaptureAccess())


def accessibility_granted() -> bool | None:
    """Whether this process may post/observe input events (needed by pynput)."""
    if not IS_MAC:
        return None
    try:
        from ApplicationServices import AXIsProcessTrusted  # noqa: PLC0415
    except ImportError:
        return None
    return bool(AXIsProcessTrusted())


def input_monitoring_granted() -> bool | None:
    """Whether this process may receive key events from a CGEventTap.

    Distinct from Accessibility: AXIsProcessTrusted() can return True while
    kTCCServiceListenEvent is still denied, in which case pynput's listener
    starts cleanly and then simply never sees a keystroke. pyobjc has no
    binding for IOHIDCheckAccess, so it is called through ctypes.

    Returns True/False, or None where the answer cannot be determined.
    """
    if not IS_MAC:
        return None
    import ctypes  # noqa: PLC0415
    import ctypes.util  # noqa: PLC0415

    try:
        path = (
            ctypes.util.find_library("IOKit")
            or "/System/Library/Frameworks/IOKit.framework/IOKit"
        )
        iokit = ctypes.cdll.LoadLibrary(path)
        iokit.IOHIDCheckAccess.restype = ctypes.c_int
        iokit.IOHIDCheckAccess.argtypes = [ctypes.c_uint32]
        # kIOHIDRequestTypeListenEvent = 1; 0 == kIOHIDAccessTypeGranted
        return iokit.IOHIDCheckAccess(1) == 0
    except (OSError, AttributeError):
        return None


def request_screen_recording() -> None:
    """Explicitly trigger the system dialog. Only call on user opt-in."""
    if IS_MAC:
        _quartz().CGRequestScreenCaptureAccess()


def running_in_bundle() -> bool:
    """True when launched as glance.app rather than from a terminal.

    Two launch paths, two signals. LaunchServices (`open glance.app`) exports
    __CFBundleIdentifier; launchd execs the bundle's script directly and does
    NOT, so the launcher exports GLANCE_BUNDLE_ID itself. Checking only the
    first silently gave terminal instructions to a bundled app - measured.

    This changes the advice we give, because the Screen Recording grant
    follows whichever identity actually asked for it.
    """
    import os  # noqa: PLC0415

    return bool(
        os.environ.get("GLANCE_BUNDLE_ID") or os.environ.get("__CFBundleIdentifier")
    )


_HELP_TERMINAL = """\
Screen Recording permission is not granted.

Grant it to the app that LAUNCHES python (your terminal or VS Code), not to
python itself:

  System Settings -> Privacy & Security -> Screen & System Audio Recording
  -> enable the entry for your terminal / VS Code, then fully quit and reopen it.

Hotkeys additionally need:
  System Settings -> Privacy & Security -> Accessibility (or Input Monitoring)

Until then, run the headless path:
  python main.py --source file:assets/sample.png --processor mock
"""

_HELP_BUNDLE = """\
Screen Recording permission is not granted to glance.app.

A bundle has its own identity, so a grant you gave your terminal or editor
does NOT carry over - glance.app has to be allowed in its own right:

  System Settings -> Privacy & Security -> Screen & System Audio Recording
  -> enable "glance" (drag in glance.app if it is not listed)

Hotkeys additionally need:
  System Settings -> Privacy & Security -> Accessibility
  System Settings -> Privacy & Security -> Input Monitoring

Then quit glance from the menu bar and start it again; macOS only re-reads
the grant on launch.
"""


def permission_help() -> str:
    """The right instructions for how this process was actually launched."""
    return _HELP_BUNDLE if running_in_bundle() else _HELP_TERMINAL


def list_windows() -> list[WindowInfo]:
    """On-screen windows, excluding desktop furniture."""
    if not IS_MAC:
        return []
    Q = _quartz()
    raw = (
        Q.CGWindowListCopyWindowInfo(
            Q.kCGWindowListOptionOnScreenOnly | Q.kCGWindowListExcludeDesktopElements,
            Q.kCGNullWindowID,
        )
        or []
    )
    out: list[WindowInfo] = []
    for w in raw:
        b = w.get("kCGWindowBounds") or {}
        out.append(
            WindowInfo(
                owner=w.get("kCGWindowOwnerName"),
                title=w.get("kCGWindowName"),
                bounds=(
                    int(b.get("X", 0)),
                    int(b.get("Y", 0)),
                    int(b.get("Width", 0)),
                    int(b.get("Height", 0)),
                ),
                window_id=int(w.get("kCGWindowNumber", 0)),
            )
        )
    return out


def preflight(require_capture: bool) -> list[str]:
    """Return human-readable problems; empty list means good to go."""
    problems: list[str] = []
    system = platform.system()
    if system == "Linux":
        import os

        if os.environ.get("XDG_SESSION_TYPE") == "wayland" or os.environ.get(
            "WAYLAND_DISPLAY"
        ):
            problems.append(
                "Wayland session detected. mss and pynput need X11; "
                "log into an X11 session, or use --source file:<png>."
            )
    if require_capture and system == "Darwin" and not screen_recording_granted():
        problems.append(permission_help())
    return problems


def warnings_() -> list[str]:
    """Non-fatal notes. Hotkeys are optional, so a missing grant only warns."""
    out: list[str] = []
    if IS_MAC and accessibility_granted() is False:
        out.append(
            "Accessibility is not granted, so hotkeys will not register. "
            "System Settings -> Privacy & Security -> Accessibility, then "
            "restart this app. (--no-hotkeys skips them.)"
        )
    return out

"""One running glance, and a way to poke it.

Clicking glance.app while it is already running did nothing visible: macOS
either activated the existing process - which has no Dock icon and no window
to bring forward if the panel is hidden - or started a second one that died
on "address already in use". Either way the user got nothing.

So a second launch now hands over to the first instead of competing with it:
it sends SIGUSR1 and exits, and the running instance shows its panel. A
signal is the right channel here because it needs no port, no token and no
network - the HTTP server is session-token gated, which a fresh process has
no way to satisfy.
"""
from __future__ import annotations

import errno
import json
import logging
import os
import signal
from pathlib import Path

log = logging.getLogger("glance.singleton")


def runtime_dir() -> Path:
    """Per-user, survives logout, not world-writable like /tmp."""
    base = Path.home() / "Library" / "Application Support" / "glance"
    base.mkdir(parents=True, exist_ok=True)
    return base


def pidfile() -> Path:
    return runtime_dir() / "glance.pid"


def _executable_of(pid: int) -> str | None:
    """The path of the binary running as `pid`, or None."""
    try:
        import ctypes  # noqa: PLC0415

        libproc = ctypes.CDLL("/usr/lib/libproc.dylib")
        buf = ctypes.create_string_buffer(4096)
        if libproc.proc_pidpath(ctypes.c_int(pid), buf, 4096) > 0:
            return buf.value.decode()
    except Exception:
        pass
    return None


def live_pid() -> int | None:
    """The PID of a running glance, or None.

    A PID on its own is not enough to go on. The pidfile outlives an unclean
    shutdown - a reboot, a SIGKILL - and PIDs are reused, so after a restart
    the recorded number can belong to something else entirely. Trusting it
    had two consequences, both bad: glance would decide it was already
    running and exit, so nothing started at login; and it would send SIGUSR1
    to whatever now held that PID, whose default disposition is to terminate.

    So the identity of the process is checked, not just its existence.
    """
    path = pidfile()
    try:
        raw = path.read_text().strip()
    except OSError:
        return None
    if not raw:
        return None

    recorded_exe = None
    try:  # current format: JSON. Older files held a bare number.
        data = json.loads(raw)
        pid = int(data["pid"])
        recorded_exe = data.get("exe")
    except (ValueError, KeyError, TypeError):
        try:
            pid = int(raw)
        except ValueError:
            return None

    if pid <= 0 or pid == os.getpid():
        return None
    try:
        os.kill(pid, 0)
    except OSError as e:
        if e.errno == errno.EPERM:
            # Alive but owned by another user, so it is not our instance and
            # we could not signal it anyway.
            return None
        return None  # ESRCH and anything else: treat as stale

    running_exe = _executable_of(pid)
    if recorded_exe and running_exe and running_exe != recorded_exe:
        log.info("pidfile points at %s, not glance; treating it as stale", running_exe)
        return None
    if recorded_exe is None and running_exe and "glance" not in running_exe.lower():
        # Legacy bare-number pidfile: fall back to a name check.
        log.info("stale legacy pidfile pointing at %s", running_exe)
        return None
    return pid


def claim() -> None:
    """Record this process as the running glance.

    The executable path is stored alongside the PID so a later reader can
    tell our process from whatever inherits the number after a reboot.
    """
    try:
        pidfile().write_text(json.dumps({
            "pid": os.getpid(),
            "exe": _executable_of(os.getpid()),
        }) + "\n")
    except OSError as e:
        log.warning("could not write %s: %s", pidfile(), e)


def release() -> None:
    try:
        path = pidfile()
        if not path.exists():
            return
        raw = path.read_text().strip()
        try:
            mine = int(json.loads(raw)["pid"]) == os.getpid()
        except (ValueError, KeyError, TypeError):
            mine = raw == str(os.getpid())
        if mine:
            path.unlink()
    except OSError:
        pass


def poke(pid: int) -> bool:
    """Ask the running instance to show its panel."""
    try:
        os.kill(pid, signal.SIGUSR1)
        return True
    except OSError as e:
        log.warning("could not signal glance (pid %d): %s", pid, e)
        return False

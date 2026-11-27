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


def live_pid() -> int | None:
    """The PID of a running glance, or None.

    A stale pidfile is common - power loss, SIGKILL - so the PID is probed
    with signal 0 rather than trusted.
    """
    path = pidfile()
    try:
        pid = int(path.read_text().strip())
    except (OSError, ValueError):
        return None
    if pid <= 0 or pid == os.getpid():
        return None
    try:
        os.kill(pid, 0)
    except OSError as e:
        if e.errno == errno.ESRCH:  # no such process: stale file
            return None
        if e.errno == errno.EPERM:  # exists but owned by someone else
            return None
        return None
    return pid


def claim() -> None:
    """Record this process as the running glance."""
    try:
        pidfile().write_text(f"{os.getpid()}\n")
    except OSError as e:
        log.warning("could not write %s: %s", pidfile(), e)


def release() -> None:
    try:
        path = pidfile()
        if path.exists() and path.read_text().strip() == str(os.getpid()):
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

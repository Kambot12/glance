"""Handing over to an already-running glance.

Clicking glance.app while it was running did nothing: macOS either activated
a process with no Dock icon and no visible window, or started a second one
that died on "address already in use".
"""
import os
import signal

import pytest

from glance import singleton


@pytest.fixture(autouse=True)
def _tmp_runtime(tmp_path, monkeypatch):
    monkeypatch.setattr(singleton, "runtime_dir", lambda: tmp_path)
    yield


def test_no_pidfile_means_no_instance():
    assert singleton.live_pid() is None


def test_claim_then_release_removes_the_file():
    singleton.claim()
    assert singleton.pidfile().exists()
    singleton.release()
    assert not singleton.pidfile().exists()


def test_our_own_pid_is_not_reported_as_another_instance():
    """Otherwise a process would refuse to start because of itself."""
    singleton.claim()
    assert singleton.live_pid() is None


def test_a_stale_pidfile_is_ignored():
    """Power loss or SIGKILL leaves the file behind; it must not wedge start-up."""
    # PID 1 exists but is launchd, not us; use an implausible one instead
    singleton.pidfile().write_text("999999\n")
    assert singleton.live_pid() is None


def test_garbage_pidfile_is_ignored():
    singleton.pidfile().write_text("not-a-pid\n")
    assert singleton.live_pid() is None


def test_empty_pidfile_is_ignored():
    singleton.pidfile().write_text("")
    assert singleton.live_pid() is None


def test_a_real_other_process_is_detected(monkeypatch):
    # a live pid that is not ours: the parent shell
    other = os.getppid()
    singleton.pidfile().write_text(f"{other}\n")
    assert singleton.live_pid() == other


def test_poke_sends_sigusr1(monkeypatch):
    sent = {}

    def fake_kill(pid, sig):
        sent["pid"], sent["sig"] = pid, sig

    monkeypatch.setattr(os, "kill", fake_kill)
    assert singleton.poke(4321) is True
    assert sent == {"pid": 4321, "sig": signal.SIGUSR1}


def test_poke_reports_failure(monkeypatch):
    def fake_kill(pid, sig):
        raise ProcessLookupError()

    monkeypatch.setattr(os, "kill", fake_kill)
    assert singleton.poke(4321) is False


def test_release_leaves_another_instances_file_alone():
    """Releasing must not delete a pidfile a different glance owns."""
    singleton.pidfile().write_text("999999\n")
    singleton.release()
    assert singleton.pidfile().exists()

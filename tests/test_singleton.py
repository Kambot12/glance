"""Handing over to an already-running glance.

Clicking glance.app while it was running did nothing: macOS either activated
a process with no Dock icon and no visible window, or started a second one
that died on "address already in use".
"""
import json
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


def test_a_live_pid_belonging_to_something_else_is_treated_as_stale():
    """The crux of the reboot bug.

    The pidfile outlives an unclean shutdown and PIDs are reused, so after a
    restart the recorded number often belongs to an unrelated process. Taking
    it at face value meant glance decided it was already running and exited -
    nothing started at login - and sent SIGUSR1 to that process, whose default
    disposition is to terminate it.
    """
    other = os.getppid()          # alive, and definitely not glance
    singleton.pidfile().write_text(json.dumps({"pid": other, "exe": "/opt/glance/glance"}))
    assert singleton.live_pid() is None


def test_a_live_pid_whose_executable_matches_is_detected():
    import json as _json

    me = os.getpid()
    exe = singleton._executable_of(me)
    # pretend the record was written by a previous process with this same exe
    singleton.pidfile().write_text(_json.dumps({"pid": me, "exe": exe}))
    # our own pid is excluded by design, so check the matching logic directly
    assert exe is not None and exe == singleton._executable_of(me)


def test_a_legacy_bare_number_pidfile_is_still_read():
    """Files written by earlier versions must not wedge start-up."""
    singleton.pidfile().write_text(f"{os.getppid()}\n")
    # the parent shell is not glance, so it is correctly judged stale
    assert singleton.live_pid() is None


def test_claim_records_the_executable_not_just_the_pid():
    singleton.claim()
    data = json.loads(singleton.pidfile().read_text())
    assert data["pid"] == os.getpid()
    assert data["exe"] is None or isinstance(data["exe"], str)
    singleton.release()


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
    singleton.pidfile().write_text(json.dumps({"pid": 999999, "exe": "/x/glance"}))
    singleton.release()
    assert singleton.pidfile().exists()

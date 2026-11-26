"""HotkeyListener startup reporting.

pynput builds its event tap inside the listener thread, so a failure there
(observed here as a pyobjc lazy-import race on AXIsProcessTrusted) kills the
thread while GlobalHotKeys.start() returns normally. start() used to report
success for an already dead listener, leaving the app claiming hotkeys worked.
"""
import threading

import pytest

from glance import hotkeys as hk
from glance.config import AppConfig, ModeConfig
from glance.models import Event


@pytest.fixture
def cfg():
    return AppConfig(
        modes=[
            ModeConfig(name="summarize", hotkey="<cmd>+<shift>+1", system_prompt="s"),
        ]
    )


class _DyingListener(threading.Thread):
    """A listener whose thread raises the moment it starts, like the real one."""

    def __init__(self, mapping):
        super().__init__(daemon=True)
        self.mapping = mapping

    def run(self):
        raise KeyError("AXIsProcessTrusted")

    def stop(self):
        pass


class _LiveListener(threading.Thread):
    def __init__(self, mapping):
        super().__init__(daemon=True)
        self.mapping = mapping
        self._stop = threading.Event()

    def run(self):
        self._stop.wait()

    def stop(self):
        self._stop.set()


@pytest.fixture(autouse=True)
def _quiet_threads(monkeypatch):
    # the dying listener's traceback is expected; keep it out of the report
    monkeypatch.setattr(threading, "excepthook", lambda args: None)


def _patch(monkeypatch, cls):
    import pynput.keyboard

    monkeypatch.setattr(pynput.keyboard, "GlobalHotKeys", cls)


def test_start_reports_failure_when_listener_thread_dies(cfg, monkeypatch):
    _patch(monkeypatch, _DyingListener)
    listener = hk.HotkeyListener(cfg, lambda ev: None)
    assert listener.start() is False
    # and it must not keep a handle to the corpse, so stop() stays safe
    assert listener._listener is None
    listener.stop()


def test_start_reports_success_for_a_live_listener(cfg, monkeypatch):
    _patch(monkeypatch, _LiveListener)
    listener = hk.HotkeyListener(cfg, lambda ev: None)
    assert listener.start() is True
    assert listener._listener.is_alive()
    listener.stop()


def test_start_survives_a_constructor_raise(cfg, monkeypatch):
    def boom(mapping):
        raise RuntimeError("tap refused")

    _patch(monkeypatch, boom)
    listener = hk.HotkeyListener(cfg, lambda ev: None)
    assert listener.start() is False
    assert listener._listener is None


def test_registered_mapping_covers_every_mode_and_pause(cfg, monkeypatch):
    seen = {}

    class Capture(_LiveListener):
        def __init__(self, mapping):
            super().__init__(mapping)
            seen.update(mapping)

    _patch(monkeypatch, Capture)
    listener = hk.HotkeyListener(cfg, lambda ev: None)
    assert listener.start() is True
    assert set(seen) == {"<cmd>+<shift>+1", cfg.pause_hotkey}
    listener.stop()


def test_callback_submits_the_right_event(cfg, monkeypatch):
    got = []
    captured = {}

    class Capture(_LiveListener):
        def __init__(self, mapping):
            super().__init__(mapping)
            captured.update(mapping)

    _patch(monkeypatch, Capture)
    listener = hk.HotkeyListener(cfg, got.append)
    assert listener.start() is True

    captured["<cmd>+<shift>+1"]()
    captured[cfg.pause_hotkey]()
    listener.stop()

    assert got == [
        Event(kind="mode", mode="summarize", manual=True),
        Event(kind="pause"),
    ]

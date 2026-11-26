"""HUD logic, headless.

Qt runs under the 'offscreen' platform plugin so these exercise the real
widgets without needing a window server. The native always-on-top behaviour
and capture exclusion cannot be asserted here - they need a live compositor -
and were verified by measurement instead (see README, step 4).
"""
import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

pytest.importorskip("PyQt6", reason="HUD needs the 'hud' extra")

from glance.hud import Hud, LateSubmitter, _pretty  # noqa: E402
from glance.models import Event  # noqa: E402


@pytest.fixture(scope="module")
def qapp():
    from PyQt6.QtWidgets import QApplication

    app = QApplication.instance() or QApplication([])
    yield app


@pytest.fixture
def hud(qapp):
    sent = []
    w = Hud([("summarize", "<cmd>+<shift>+1"), ("explain_code", "<cmd>+<shift>+2")],
            sent.append)
    w._sent = sent
    yield w
    w.deleteLater()


# ------------------------------------------------------- LateSubmitter ----
def test_late_submitter_drops_events_before_the_orchestrator_exists():
    """The HUD is built before the Orchestrator, so early clicks must not crash."""
    s = LateSubmitter()
    s.submit(Event(kind="mode", mode="summarize"))  # must not raise


def test_late_submitter_forwards_once_bound():
    got = []
    s = LateSubmitter()
    s.bind(got.append)
    ev = Event(kind="mode", mode="summarize")
    s.submit(ev)
    assert got == [ev]


# ------------------------------------------------------------ triggers ----
def test_mode_button_submits_a_manual_event_tagged_hud(hud):
    hud.trigger("summarize")
    assert hud._sent == [
        Event(kind="mode", mode="summarize", manual=True, origin="hud")
    ]


def test_followup_submits_the_typed_text_and_clears_the_box(hud):
    hud.ask.setText("  what is the error rate?  ")
    hud._send_followup()
    assert hud._sent == [
        Event(kind="followup", text="what is the error rate?", origin="hud")
    ]
    assert hud.ask.text() == ""


def test_empty_followup_is_ignored(hud):
    hud.ask.setText("   ")
    hud._send_followup()
    assert hud._sent == []


# ------------------------------------------------------------- payloads ---
def test_chunks_accumulate_into_the_body(hud):
    hud.on_payload({"type": "start", "label": "summarize"})
    hud.on_payload({"type": "chunk", "text": "hello "})
    hud.on_payload({"type": "chunk", "text": "world"})
    assert hud.body.text() == "hello world"
    assert hud.status.text() == "summarize"


def test_start_clears_the_previous_response(hud):
    hud.on_payload({"type": "start", "label": "a"})
    hud.on_payload({"type": "chunk", "text": "first"})
    hud.on_payload({"type": "start", "label": "b"})
    assert hud.body.text() == ""


def test_end_returns_the_status_to_ready(hud):
    hud.on_payload({"type": "start", "label": "summarize"})
    hud.on_payload({"type": "chunk", "text": "x"})
    hud.on_payload({"type": "end"})
    assert hud.status.text() == "ready"


def test_chunk_without_a_start_still_renders(hud):
    """A reconnecting view can meet a stream already in progress."""
    hud.on_payload({"type": "chunk", "text": "mid-stream"})
    assert hud.body.text() == "mid-stream"


@pytest.mark.parametrize("kind", ["error", "blocked"])
def test_errors_and_blocks_are_shown_verbatim(hud, kind):
    hud.on_payload({"type": kind, "text": "Capture skipped - 1Password"})
    assert hud.body.text() == "Capture skipped - 1Password"
    assert hud.status.text() == kind


def test_status_payload_updates_only_the_status(hud):
    hud.on_payload({"type": "chunk", "text": "body text"})
    hud.on_payload({"type": "status", "text": "paused"})
    assert hud.status.text() == "paused"
    assert hud.body.text() == "body text"


def test_unknown_payload_type_is_ignored(hud):
    hud.on_payload({"type": "something-new"})  # must not raise


# -------------------------------------------------------------- display ---
def test_window_id_is_none_until_realised(hud):
    assert hud.window_id is None


@pytest.mark.parametrize(
    "raw,pretty",
    [
        ("<cmd>+<shift>+1", "⌘⇧1"),
        ("<ctrl>+<alt>+g", "⌃⌥g"),
        ("<cmd>+<shift>+0", "⌘⇧0"),
    ],
)
def test_hotkeys_render_as_mac_symbols(raw, pretty):
    assert _pretty(raw) == pretty

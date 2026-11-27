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
    w = Hud(
        [("summarize", "<cmd>+<shift>+1"), ("solve", "<cmd>+<shift>+2")],
        sent.append,
        opacity=0.28,
        opacity_active=0.97,
        toggle_hotkey="<cmd>+<shift>+.",
    )
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
    hud.show()
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


# ------------------------------------------------------------- position ---
def test_panel_cannot_be_dragged_off_the_top_of_the_screen(hud, qapp):
    """Observed for real at y=-58: unreachable, with no Dock icon to recover."""
    from PyQt6.QtCore import QPoint
    from PyQt6.QtWidgets import QApplication

    area = QApplication.primaryScreen().availableGeometry()
    assert hud._clamp(QPoint(400, -500)).y() >= area.top()


def test_panel_cannot_be_dragged_off_the_bottom_or_sides(hud, qapp):
    from PyQt6.QtCore import QPoint
    from PyQt6.QtWidgets import QApplication

    area = QApplication.primaryScreen().availableGeometry()
    assert hud._clamp(QPoint(99999, 99999)).x() <= area.right()
    assert hud._clamp(QPoint(99999, 99999)).y() <= area.bottom()
    # a sliver must stay grabbable on the left too
    assert hud._clamp(QPoint(-99999, 300)).x() > area.left() - hud.width()


def test_a_position_already_on_screen_is_left_alone(hud, qapp):
    from PyQt6.QtCore import QPoint
    from PyQt6.QtWidgets import QApplication

    area = QApplication.primaryScreen().availableGeometry()
    inside = QPoint(area.left() + 100, area.top() + 100)
    assert hud._clamp(inside) == inside


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


# -------------------------------------------------------------- opacity ---
def test_panel_rests_faint(hud):
    """It floats over what the user is actually reading."""
    assert hud.windowOpacity() == pytest.approx(0.28, abs=0.01)


def test_pointing_at_it_makes_it_solid(hud):
    hud._hovered = True
    hud._refresh_opacity()
    assert hud.windowOpacity() == pytest.approx(0.97, abs=0.01)
    hud._hovered = False
    hud._refresh_opacity()
    assert hud.windowOpacity() == pytest.approx(0.28, abs=0.01)


def test_a_streaming_answer_makes_it_readable_without_the_mouse(hud):
    """A response you cannot read is no use, so streaming raises it too."""
    hud.on_payload({"type": "start", "label": "solve"})
    assert hud.windowOpacity() == pytest.approx(0.97, abs=0.01)
    hud.on_payload({"type": "end"})
    assert hud.windowOpacity() == pytest.approx(0.28, abs=0.01)


def test_it_stays_solid_after_streaming_while_still_hovered(hud):
    hud._hovered = True
    hud.on_payload({"type": "start", "label": "solve"})
    hud.on_payload({"type": "end"})
    assert hud.windowOpacity() == pytest.approx(0.97, abs=0.01)


# --------------------------------------------------------------- toggle ---
def test_panel_payload_toggles_visibility(hud):
    hud.show()
    assert hud.isVisible()
    hud.on_payload({"type": "panel", "action": "toggle"})
    assert not hud.isVisible()
    hud.on_payload({"type": "panel", "action": "toggle"})
    assert hud.isVisible()


def test_toggle_visible_returns_the_new_state(hud):
    hud.show()
    assert hud.toggle_visible() is False
    assert hud.toggle_visible() is True


def test_a_toggle_payload_does_not_disturb_the_response(hud):
    hud.on_payload({"type": "chunk", "text": "an answer"})
    hud.on_payload({"type": "panel", "action": "toggle"})
    assert hud.body.text() == "an answer"


# ------------------------------------ answers that land behind a hidden panel --
def test_an_answer_that_finishes_while_hidden_is_kept_and_flagged(hud):
    """Hiding must not cost the user the answer.

    The stream keeps running regardless - verified end to end - but the panel
    used to drop straight back to resting opacity when it ended, so a finished
    answer sat there at 28% and read as "nothing happened".
    """
    hud.show()
    hud.on_payload({"type": "start", "label": "solve"})
    hud.on_payload({"type": "chunk", "text": "The answer "})
    hud._hide_by_request()

    hud.on_payload({"type": "chunk", "text": "is 42."})
    hud.on_payload({"type": "end"})

    assert hud.body.text() == "The answer is 42."      # nothing lost
    assert hud._unseen is True
    assert hud.status.text() == "answer ready"


def test_revealing_shows_an_unseen_answer_at_full_strength(hud):
    hud.show()
    hud.on_payload({"type": "start", "label": "solve"})
    hud.on_payload({"type": "chunk", "text": "42"})
    hud._hide_by_request()
    hud.on_payload({"type": "end"})

    hud.reveal()
    assert hud._unseen is False
    assert hud.status.text() == "ready"
    assert hud.windowOpacity() == pytest.approx(0.97, abs=0.01)
    assert hud.body.text() == "42"


def test_a_finished_answer_stays_readable_instead_of_fading_at_once(hud):
    hud.show()
    hud.on_payload({"type": "start", "label": "solve"})
    hud.on_payload({"type": "chunk", "text": "42"})
    hud.on_payload({"type": "end"})
    # still solid: the hold timer is running
    assert hud.windowOpacity() == pytest.approx(0.97, abs=0.01)


def test_hold_can_be_turned_off(qapp):
    w = Hud([("solve", "<cmd>+<shift>+2")], lambda e: None,
            opacity=0.28, opacity_active=0.97, hold_after_answer_s=0)
    w.show()
    w.on_payload({"type": "start", "label": "solve"})
    w.on_payload({"type": "chunk", "text": "42"})
    w.on_payload({"type": "end"})
    assert w.windowOpacity() == pytest.approx(0.28, abs=0.01)
    w.deleteLater()


def test_an_empty_stream_does_not_claim_an_answer_is_waiting(hud):
    hud.show()
    hud.on_payload({"type": "start", "label": "solve"})
    hud._hide_by_request()
    hud.on_payload({"type": "end"})
    assert hud._unseen is False
    assert hud.status.text() == "ready"


def test_hovering_counts_as_having_seen_it(hud):
    """Pointing at the panel is the user reading it, so the flag clears."""
    from PyQt6.QtCore import QPointF
    from PyQt6.QtGui import QEnterEvent

    hud.show()
    hud.on_payload({"type": "start", "label": "solve"})
    hud.on_payload({"type": "chunk", "text": "42"})
    hud._hide_by_request()
    hud.on_payload({"type": "end"})
    assert hud._unseen is True

    p = QPointF(10.0, 10.0)
    hud.enterEvent(QEnterEvent(p, p, p))
    assert hud._unseen is False
    assert hud._hovered is True

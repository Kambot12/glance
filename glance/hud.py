"""Always-on-top control panel.

Why Qt owns the main thread
---------------------------
On macOS, AppKit requires its run loop on thread 0, so QApplication.exec() must
run on the main thread. asyncio therefore moves to a worker thread whenever the
HUD is active (see main.py). Every call from the asyncio side into the HUD goes
through a Qt signal, which is delivered on the GUI thread by Qt's queued
connection; nothing touches a widget directly from another thread.

Why the HUD is excluded rather than hidden
------------------------------------------
An always-on-top panel sits in front of whatever the user is looking at, so a
naive capture photographs the panel and the model spends its context describing
glance's own UI. Hiding the window for the duration of a capture is the usual
workaround and it is visibly bad: it flickers, and it races the compositor.
Instead the HUD publishes its CGWindowID and ScreenCaptureKit leaves it out of
the shot while it stays on screen. Measured: 181,625 magenta HUD pixels in a
plain capture, 0 with the id excluded.
"""
from __future__ import annotations

import logging
import platform

from PyQt6.QtCore import QObject, Qt, QPoint, pyqtSignal
from PyQt6.QtGui import QFont
from PyQt6.QtWidgets import (
    QApplication,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from glance.models import Event

log = logging.getLogger("glance.hud")

IS_MAC = platform.system() == "Darwin"

STYLE = """
#panel     { background: rgba(18,18,22,236); border: 1px solid rgba(255,255,255,34);
             border-radius: 14px; }
#title     { color: #e8e8ef; font-weight: 700; letter-spacing: 1.2px; }
#status    { color: #8f8fa6; }
#body      { color: #e4e4ee; }
#empty     { color: #7a7a90; }
QPushButton#mode {
    background: rgba(255,255,255,20); color: #e8e8ef; border: 0;
    border-radius: 8px; padding: 6px 12px;
}
QPushButton#mode:hover   { background: rgba(255,255,255,42); }
QPushButton#mode:pressed { background: rgba(120,140,255,110); }
QPushButton#close {
    background: transparent; color: #8f8fa6; border: 0; font-size: 18px;
}
QPushButton#close:hover { color: #ff6b6b; }
QLineEdit {
    background: rgba(255,255,255,16); color: #e8e8ef;
    border: 1px solid rgba(255,255,255,28); border-radius: 8px; padding: 6px 10px;
}
QLineEdit:focus { border-color: rgba(120,140,255,160); }
QScrollArea { background: transparent; border: 0; }
"""


def apply_native_window_behaviour(widget: QWidget) -> int | None:
    """Float above everything, on every Space, including over fullscreen apps.

    Returns the window's CGWindowID so the capture source can exclude it, or
    None off macOS. Qt's own WindowStaysOnTopHint is not enough: it keeps the
    window above other *Qt* windows but still below the menu bar, and it does
    not follow the user across Spaces or over a fullscreen app.
    """
    if not IS_MAC:
        return None
    try:
        import objc  # noqa: PLC0415
        from AppKit import (  # noqa: PLC0415
            NSWindowCollectionBehaviorCanJoinAllSpaces,
            NSWindowCollectionBehaviorFullScreenAuxiliary,
            NSWindowCollectionBehaviorStationary,
            NSStatusWindowLevel,
        )

        view = objc.objc_object(c_void_p=int(widget.winId()))
        nswindow = view.window()
        nswindow.setCollectionBehavior_(
            NSWindowCollectionBehaviorCanJoinAllSpaces
            | NSWindowCollectionBehaviorStationary
            | NSWindowCollectionBehaviorFullScreenAuxiliary
        )
        nswindow.setLevel_(NSStatusWindowLevel)
        # Keep the panel from dimming when another app takes focus.
        nswindow.setHidesOnDeactivate_(False)
        return int(nswindow.windowNumber())
    except Exception as e:  # pragma: no cover - depends on a live window server
        log.warning("could not apply native window behaviour: %s: %s", type(e).__name__, e)
        return None


class Bridge(QObject):
    """Carries payloads from the asyncio thread onto the GUI thread.

    Qt delivers a signal emitted from a non-GUI thread through its event loop
    (a queued connection), which is the only supported way to touch widgets
    from another thread.
    """

    payload = pyqtSignal(dict)
    #: emitted when the asyncio side has finished, so Qt can leave exec()
    done = pyqtSignal()


class LateSubmitter:
    """A submit callable the HUD can hold before the Orchestrator exists.

    The HUD is built on the main thread first (Qt needs it), while the
    Orchestrator is created later on the asyncio thread; this closes that gap
    without handing the HUD a half-built object.
    """

    def __init__(self) -> None:
        self._fn = None

    def bind(self, fn) -> None:
        self._fn = fn

    def submit(self, event: Event) -> None:
        if self._fn is None:
            log.warning("dropping %s: the orchestrator is not running yet", event.kind)
            return
        self._fn(event)


class Hud(QWidget):
    """The floating control panel.

    :param modes: (name, hotkey) for each configured mode.
    :param submit: thread-safe event submitter, i.e. Orchestrator.submit_from_thread.
    """

    def __init__(self, modes: list[tuple[str, str]], submit, width: int = 560):
        super().__init__()
        self._submit = submit
        self._streaming = False
        self._drag_from: QPoint | None = None
        self.window_id: int | None = None

        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.Tool
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setStyleSheet(STYLE)

        panel = QWidget(objectName="panel")
        outer = QVBoxLayout(self)
        outer.setContentsMargins(10, 10, 10, 10)
        outer.addWidget(panel)
        col = QVBoxLayout(panel)
        col.setContentsMargins(14, 10, 14, 12)
        col.setSpacing(8)

        # --- header (also the drag handle) ---
        head = QHBoxLayout()
        title = QLabel("GLANCE", objectName="title")
        title.setFont(QFont(title.font().family(), 11))
        self.status = QLabel("ready", objectName="status")
        close = QPushButton("×", objectName="close")
        close.setFixedWidth(26)
        close.clicked.connect(self.hide)
        close.setToolTip("Hide the panel (hotkeys keep working)")
        head.addWidget(title)
        head.addSpacing(10)
        head.addWidget(self.status)
        head.addStretch(1)
        head.addWidget(close)
        col.addLayout(head)

        # --- mode buttons ---
        row = QHBoxLayout()
        row.setSpacing(6)
        for name, hotkey in modes:
            b = QPushButton(name.replace("_", " "), objectName="mode")
            b.setToolTip(f"{name}  ({_pretty(hotkey)})")
            b.clicked.connect(lambda _, n=name: self.trigger(n))
            row.addWidget(b)
        row.addStretch(1)
        col.addLayout(row)

        # --- response body ---
        self.body = QLabel("press a mode, or its hotkey", objectName="empty")
        self.body.setWordWrap(True)
        self.body.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.body.setAlignment(Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignLeft)
        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setWidget(self.body)
        self.scroll.setFixedHeight(150)
        self.scroll.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff
        )
        col.addWidget(self.scroll)

        # --- follow-up ---
        self.ask = QLineEdit(placeholderText="ask a follow-up about the last capture…")
        self.ask.returnPressed.connect(self._send_followup)
        col.addWidget(self.ask)

        self.setFixedWidth(width)
        self.adjustSize()

    # ------------------------------------------------------------ actions --
    def trigger(self, mode: str) -> None:
        self._submit(Event(kind="mode", mode=mode, manual=True, origin="hud"))

    def _send_followup(self) -> None:
        text = self.ask.text().strip()
        if not text:
            return
        self.ask.clear()
        self._submit(Event(kind="followup", text=text, origin="hud"))

    # ------------------------------------------------------------ payload --
    def on_payload(self, msg: dict) -> None:
        """Called on the GUI thread via Bridge.payload."""
        kind = msg.get("type")
        if kind == "start":
            self._streaming = True
            self.body.setObjectName("body")
            self._restyle()
            self.body.setText("")
            self.status.setText(str(msg.get("label", "working")))
        elif kind == "chunk":
            if not self._streaming:
                self.body.setText("")
                self._streaming = True
            self.body.setText(self.body.text() + str(msg.get("text", "")))
            bar = self.scroll.verticalScrollBar()
            bar.setValue(bar.maximum())
        elif kind == "end":
            self._streaming = False
            self.status.setText("ready")
        elif kind in ("error", "blocked"):
            self._streaming = False
            self.status.setText(kind)
            self.body.setObjectName("empty")
            self._restyle()
            self.body.setText(str(msg.get("text", kind)))
        elif kind == "status":
            self.status.setText(str(msg.get("text", "")))

    def _restyle(self) -> None:
        self.body.style().unpolish(self.body)
        self.body.style().polish(self.body)

    # ----------------------------------------------------------- dragging --
    def mousePressEvent(self, e) -> None:
        if e.button() == Qt.MouseButton.LeftButton:
            self._drag_from = e.globalPosition().toPoint() - self.frameGeometry().topLeft()

    def mouseMoveEvent(self, e) -> None:
        if self._drag_from is not None and e.buttons() & Qt.MouseButton.LeftButton:
            self.move(e.globalPosition().toPoint() - self._drag_from)

    def mouseReleaseEvent(self, e) -> None:
        self._drag_from = None

    # ----------------------------------------------------------- placement --
    def place_top_centre(self) -> None:
        screen = QApplication.primaryScreen()
        if screen is None:
            return
        area = screen.availableGeometry()
        self.move(area.center().x() - self.width() // 2, area.top() + 8)

    def realise(self) -> int | None:
        """Show the panel and claim its native behaviour + window id."""
        self.show()
        self.place_top_centre()
        QApplication.processEvents()  # the NSWindow only exists once shown
        self.window_id = apply_native_window_behaviour(self)
        if self.window_id is None:
            log.warning(
                "no window id: the HUD cannot be excluded from its own captures "
                "and will appear in every frame sent to the model"
            )
        else:
            log.info("hud window id %d (excluded from captures)", self.window_id)
        return self.window_id


def _pretty(hotkey: str) -> str:
    """'<cmd>+<shift>+1' -> '⌘⇧1'."""
    out = hotkey
    for frm, to in (
        ("<cmd>+", "⌘"), ("<shift>+", "⇧"), ("<ctrl>+", "⌃"),
        ("<alt>+", "⌥"), ("+", ""),
    ):
        out = out.replace(frm, to)
    return out

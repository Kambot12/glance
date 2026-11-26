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
import os
import platform

from PyQt6.QtCore import QObject, QPoint, Qt, pyqtSignal
from PyQt6.QtGui import QAction, QColor, QFont, QIcon, QPainter, QPixmap
from PyQt6.QtWidgets import (
    QApplication,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMenu,
    QPushButton,
    QScrollArea,
    QSystemTrayIcon,
    QVBoxLayout,
    QWidget,
)

from glance.models import Event

log = logging.getLogger("glance.hud")

IS_MAC = platform.system() == "Darwin"

#: NSStatusWindowLevel. Floating over another app's fullscreen Space turned
#: out NOT to be a level problem - 25, 101 and 1000 all failed under the
#: default Regular activation policy, and all three work under Accessory (see
#: become_accessory_app). So the level stays at the polite value: higher
#: levels would also cover system alerts and the screen saver for no gain.
#: Overridable with GLANCE_HUD_LEVEL.
WINDOW_LEVEL = 25

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


def become_accessory_app() -> bool:
    """Drop to NSApplicationActivationPolicyAccessory.

    Measured: a Regular-policy app's window will not appear over another app's
    fullscreen Space no matter what level it is given - 25, 101 and 1000 all
    failed. The window's Space membership is decided by the *application*, and
    only an accessory (LSUIElement) app floats across fullscreen Spaces. It
    also removes the Dock icon, which is what an always-on panel wants anyway;
    the bundle sets LSUIElement for the same reason.
    """
    if not IS_MAC:
        return False
    try:
        from AppKit import NSApp, NSApplicationActivationPolicyAccessory  # noqa: PLC0415

        app = NSApp()
        if app is None:
            return False
        return bool(app.setActivationPolicy_(NSApplicationActivationPolicyAccessory))
    except Exception as e:  # pragma: no cover
        log.warning("could not become an accessory app: %s: %s", type(e).__name__, e)
        return False


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
        import os  # noqa: PLC0415

        import objc  # noqa: PLC0415
        from AppKit import (  # noqa: PLC0415
            NSWindowCollectionBehaviorCanJoinAllSpaces,
            NSWindowCollectionBehaviorFullScreenAuxiliary,
            NSWindowCollectionBehaviorIgnoresCycle,
            NSWindowCollectionBehaviorStationary,
        )

        view = objc.objc_object(c_void_p=int(widget.winId()))
        nswindow = view.window()
        nswindow.setCollectionBehavior_(
            NSWindowCollectionBehaviorCanJoinAllSpaces
            | NSWindowCollectionBehaviorStationary
            | NSWindowCollectionBehaviorFullScreenAuxiliary
            | NSWindowCollectionBehaviorIgnoresCycle
        )
        nswindow.setLevel_(int(os.environ.get("GLANCE_HUD_LEVEL", WINDOW_LEVEL)))
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
        self._modes = list(modes)
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

    # --------------------------------------------------------- menu ------
    def contextMenuEvent(self, e) -> None:
        self._menu().exec(e.globalPos())

    def _menu(self) -> QMenu:
        """Shared by the panel's right-click and the menu-bar item.

        A bundled glance runs as LSUIElement: no Dock icon and no menu bar, so
        without this there is no way to quit and no way to get the panel back
        once it is hidden.
        """
        m = QMenu(self)
        if self.isVisible():
            m.addAction("Hide panel", self.hide)
        else:
            m.addAction("Show panel", self.reveal)
        m.addSeparator()
        for name, hotkey in self._modes:
            act = QAction(f"{name.replace('_', ' ')}\t{_pretty(hotkey)}", m)
            act.triggered.connect(lambda _, n=name: self.trigger(n))
            m.addAction(act)
        m.addSeparator()
        m.addAction("Reset position", self._reset_position)
        m.addAction("Quit glance", QApplication.quit)
        return m

    def _reset_position(self) -> None:
        self.reveal()
        self.place_top_centre()

    def reveal(self) -> None:
        self.show()
        self.raise_()
        # Re-apply: a hidden window can be torn down and rebuilt by the window
        # server, which invalidates the id the capture source excludes.
        new_id = apply_native_window_behaviour(self)
        if new_id and new_id != self.window_id:
            log.info("hud window id changed %s -> %s", self.window_id, new_id)
            self.window_id = new_id

    # ----------------------------------------------------------- dragging --
    def mousePressEvent(self, e) -> None:
        if e.button() == Qt.MouseButton.LeftButton:
            self._drag_from = e.globalPosition().toPoint() - self.frameGeometry().topLeft()

    def mouseMoveEvent(self, e) -> None:
        if self._drag_from is not None and e.buttons() & Qt.MouseButton.LeftButton:
            self.move(self._clamp(e.globalPosition().toPoint() - self._drag_from))

    def _clamp(self, pos: QPoint) -> QPoint:
        """Keep the panel on screen.

        A frameless always-on-top window has no title bar for the window
        manager to catch, so without this it can be dragged above the menu bar
        and becomes unreachable - no way to grab it, and on a bundled build no
        Dock icon either. Observed for real at y=-58.
        """
        screen = self.screen() or QApplication.primaryScreen()
        if screen is None:
            return pos
        area = screen.availableGeometry()
        margin = 40  # always leave this much of the panel grabbable
        x = max(area.left() - self.width() + margin,
                min(pos.x(), area.right() - margin))
        y = max(area.top(), min(pos.y(), area.bottom() - margin))
        return QPoint(x, y)

    def mouseReleaseEvent(self, e) -> None:
        self._drag_from = None

    # ----------------------------------------------------------- placement --
    def place_top_centre(self) -> None:
        screen = QApplication.primaryScreen()
        if screen is None:
            return
        area = screen.availableGeometry()
        self.move(self._clamp(QPoint(
            area.center().x() - self.width() // 2, area.top() + 8
        )))

    def realise(self) -> int | None:
        """Show the panel and claim its native behaviour + window id."""
        # Must happen before the window is shown: the Space a window joins is
        # decided when it is created, from the app's activation policy.
        if os.environ.get("GLANCE_HUD_POLICY", "accessory") == "accessory":
            become_accessory_app()
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


def _tray_icon() -> QIcon:
    """A small glyph drawn in code, so the repo ships no binary asset."""
    pm = QPixmap(22, 22)
    pm.fill(QColor(0, 0, 0, 0))
    p = QPainter(pm)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    p.setPen(QColor(255, 255, 255, 235))
    p.drawEllipse(3, 5, 16, 12)      # an eye outline: glance
    p.setBrush(QColor(255, 255, 255, 235))
    p.drawEllipse(9, 8, 5, 5)
    p.end()
    pm.setDevicePixelRatio(2.0)
    return QIcon(pm)


def build_tray(hud: Hud, app) -> QSystemTrayIcon | None:
    """Menu-bar item: the only affordance a bundled glance has.

    With LSUIElement set there is no Dock icon and no menu bar, so this is how
    the panel comes back after it is hidden, and how the app quits.
    """
    if not QSystemTrayIcon.isSystemTrayAvailable():
        log.warning("no system tray available; quit with Ctrl-C or Activity Monitor")
        return None
    tray = QSystemTrayIcon(_tray_icon(), app)
    tray.setToolTip("glance")

    def refresh() -> None:
        # The menu is rebuilt so Show/Hide reflects the current state; the old
        # one is a child of the HUD and would otherwise accumulate on every
        # click.
        previous = tray.contextMenu()
        tray.setContextMenu(hud._menu())
        if previous is not None:
            previous.deleteLater()

    refresh()
    tray.activated.connect(lambda _reason: refresh())
    tray.show()
    return tray


def _pretty(hotkey: str) -> str:
    """'<cmd>+<shift>+1' -> '⌘⇧1'."""
    out = hotkey
    for frm, to in (
        ("<cmd>+", "⌘"), ("<shift>+", "⇧"), ("<ctrl>+", "⌃"),
        ("<alt>+", "⌥"), ("+", ""),
    ):
        out = out.replace(frm, to)
    return out

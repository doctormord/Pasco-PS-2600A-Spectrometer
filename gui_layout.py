"""
gui_layout.py — Window geometry and stable-layout helpers (desktop GUI).

Rules implemented here (calibration dialog and main window):
  • The initial size derives from QScreen.availableGeometry() of the screen the
    parent window is on (at most ~90 % of it), centred in the available area.
  • A remembered geometry is clamped to the CURRENT screen before use, so a
    window never opens partly behind the taskbar or above the top edge.
  • Content changes never resize the window: status lines are single-line and
    elided (full text as tooltip, copyable), result areas have a fixed height
    and scroll internally, and whole pages sit in scroll areas.
"""

from __future__ import annotations

from PyQt6.QtCore import QEvent, QRect, Qt, QTimer
from PyQt6.QtGui import QAction, QGuiApplication, QKeySequence, QPainter
from PyQt6.QtWidgets import (
    QFrame, QLabel, QScrollArea, QSizePolicy, QStyle, QTextBrowser, QWidget,
)

# Smallest size that still fits a 1366×768 screen with a taskbar and a title
# bar (available height ≈ 728 px, minus ≈ 32 px title bar at 100 % scaling).
MIN_DIALOG_W = 760
MIN_DIALOG_H = 560
SCREEN_FRACTION = 0.90


# ── pure geometry (unit-testable without a display) ───────────────────────
def clamp_rect(rect, avail, min_w: int = 0, min_h: int = 0,
               frame_top: int = 0, frame_side: int = 0) -> tuple[int, int, int, int]:
    """Clamp a client rect (x, y, w, h) into the available area `avail`
    (x, y, w, h). `frame_top` / `frame_side` are the window-frame margins (title
    bar, borders) that lie OUTSIDE the client rect; they are kept on-screen too.
    The minimum size wins over the screen size only if the screen is smaller
    than the minimum (then the window is placed at the top-left)."""
    x, y, w, h = (int(v) for v in rect)
    ax, ay, aw, ah = (int(v) for v in avail)
    max_w = max(1, aw - 2 * frame_side)
    max_h = max(1, ah - frame_top - frame_side)
    w = max(min(w, max_w), min(min_w, max_w))
    h = max(min(h, max_h), min(min_h, max_h))
    x = min(max(x, ax + frame_side), ax + aw - frame_side - w)
    y = min(max(y, ay + frame_top), ay + ah - frame_side - h)
    x = max(x, ax + frame_side)
    y = max(y, ay + frame_top)
    return x, y, w, h


def initial_rect(avail, *, fraction: float = SCREEN_FRACTION,
                 max_w: int | None = None, max_h: int | None = None,
                 min_w: int = 0, min_h: int = 0,
                 frame_top: int = 0, frame_side: int = 0) -> tuple[int, int, int, int]:
    """Centred client rect of `fraction` of the available area (capped)."""
    ax, ay, aw, ah = (int(v) for v in avail)
    w = int(aw * fraction) - 2 * frame_side
    h = int(ah * fraction) - frame_top - frame_side
    if max_w:
        w = min(w, int(max_w))
    if max_h:
        h = min(h, int(max_h))
    x = ax + (aw - w) // 2
    y = ay + frame_top + (ah - frame_top - frame_side - h) // 2
    return clamp_rect((x, y, w, h), avail, min_w, min_h, frame_top, frame_side)


# ── Qt glue ───────────────────────────────────────────────────────────────
def _rect_tuple(r: QRect) -> tuple[int, int, int, int]:
    return r.x(), r.y(), r.width(), r.height()


def screen_for(widget: QWidget | None):
    """Screen of `widget` (or of its window), else the primary screen."""
    scr = None
    if widget is not None:
        try:
            scr = widget.window().screen() if widget.window() is not None else widget.screen()
        except Exception:                                    # noqa: BLE001
            scr = None
    return scr or QGuiApplication.primaryScreen()


def frame_margins_estimate(widget: QWidget) -> tuple[int, int]:
    """(title-bar height, side border) before the window is shown — the real
    frame is only known after show(); fix_onscreen() corrects afterwards."""
    st = widget.style()
    title = st.pixelMetric(QStyle.PixelMetric.PM_TitleBarHeight) if st else 30
    return max(int(title), 30), 1


def place_window(widget: QWidget, anchor: QWidget | None, saved=None, *,
                 min_w: int = MIN_DIALOG_W, min_h: int = MIN_DIALOG_H,
                 max_w: int | None = None, max_h: int | None = None) -> None:
    """Set the minimum size and the initial / restored geometry of a top-level
    window (call before show()). A saved geometry is restored on the screen it
    lies on, if that screen still exists; otherwise (and for a first open) the
    screen of `anchor` / the primary screen is used."""
    scr = None
    if isinstance(saved, (list, tuple)) and len(saved) == 4:
        try:
            scr = QGuiApplication.screenAt(QRect(*[int(v) for v in saved]).center())
        except (TypeError, ValueError):
            scr = None
    if scr is None:
        scr = screen_for(anchor if anchor is not None else widget)
    avail = _rect_tuple(scr.availableGeometry()) if scr is not None else (0, 0, 1366, 728)
    ft, fs = frame_margins_estimate(widget)
    # Never demand more than the screen offers (tiny / heavily scaled screens).
    mw = min(min_w, avail[2] - 2 * fs)
    mh = min(min_h, avail[3] - ft - fs)
    widget.setMinimumSize(max(1, mw), max(1, mh))
    rect = None
    if isinstance(saved, (list, tuple)) and len(saved) == 4:
        try:
            rect = clamp_rect([int(v) for v in saved], avail, mw, mh, ft, fs)
        except (TypeError, ValueError):
            rect = None
    if rect is None:
        rect = initial_rect(avail, max_w=max_w, max_h=max_h, min_w=mw, min_h=mh,
                            frame_top=ft, frame_side=fs)
    widget.setGeometry(*rect)


def fix_onscreen(widget: QWidget) -> None:
    """After show(): move/shrink so the real FRAME (incl. title bar) lies inside
    the available area. One-time correction on open, not a reaction to
    content changes."""
    scr = screen_for(widget)
    if scr is None or widget.isMaximized() or widget.isFullScreen():
        return
    avail = scr.availableGeometry()
    fg, g = widget.frameGeometry(), widget.geometry()
    ft = max(0, g.top() - fg.top())
    fs = max(0, g.left() - fg.left())
    rect = clamp_rect(_rect_tuple(g), _rect_tuple(avail),
                      widget.minimumWidth(), widget.minimumHeight(), ft, fs)
    if rect != _rect_tuple(g):
        widget.setGeometry(*rect)


def schedule_fix_onscreen(widget: QWidget) -> None:
    # The call is dropped if the widget was destroyed in the meantime.
    from PyQt6 import sip

    def run():
        if not sip.isdeleted(widget):
            fix_onscreen(widget)
    QTimer.singleShot(0, run)


def geometry_to_config(widget: QWidget) -> list[int] | None:
    if widget.isMaximized() or widget.isFullScreen():
        g = widget.normalGeometry()
    else:
        g = widget.geometry()
    if g.width() <= 0 or g.height() <= 0:
        return None
    return [g.x(), g.y(), g.width(), g.height()]


# ── stable widgets ────────────────────────────────────────────────────────
class ElidedLabel(QLabel):
    """Single-line label of fixed height. Long text (paths, device names) is
    elided when painted; the full text is the tooltip and is copied by
    Ctrl+C / the context menu. Its size hint never widens the window."""

    def __init__(self, text: str = "", parent=None,
                 mode: Qt.TextElideMode = Qt.TextElideMode.ElideRight):
        super().__init__(parent)
        self._full = ""
        self._mode = mode
        self.setTextFormat(Qt.TextFormat.PlainText)
        self.setWordWrap(False)
        self.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Fixed)
        self.setFixedHeight(self.fontMetrics().height() + 6)
        self.setFocusPolicy(Qt.FocusPolicy.ClickFocus)
        self.setContextMenuPolicy(Qt.ContextMenuPolicy.ActionsContextMenu)
        act = QAction("Copy", self)
        act.setShortcut(QKeySequence.StandardKey.Copy)
        act.setShortcutContext(Qt.ShortcutContext.WidgetShortcut)
        act.triggered.connect(self.copy_text)
        self.addAction(act)
        self.setText(text)

    def setText(self, text) -> None:                       # noqa: N802 (Qt API)
        self._full = "" if text is None else str(text)
        super().setText(self._full)
        self.setToolTip(self._full)
        self.update()

    def text(self) -> str:                                  # noqa: D401
        return self._full

    def copy_text(self) -> None:
        cb = QGuiApplication.clipboard()
        if cb is not None:
            cb.setText(self._full)

    def minimumSizeHint(self):                              # noqa: N802
        s = super().minimumSizeHint()
        s.setWidth(20)
        return s

    def sizeHint(self):                                     # noqa: N802
        s = super().sizeHint()
        s.setWidth(min(s.width(), 200))
        return s

    def paintEvent(self, ev):                               # noqa: N802
        p = QPainter(self)
        r = self.contentsRect()
        txt = self.fontMetrics().elidedText(self._full, self._mode, r.width())
        p.setPen(self.palette().color(self.foregroundRole()))
        p.drawText(r, int(self.alignment() | Qt.AlignmentFlag.AlignVCenter), txt)


class FixedTextPane(QTextBrowser):
    """Read-only rich-text area of a fixed number of lines. Longer content
    scrolls inside the pane; the text stays selectable and copyable."""

    def __init__(self, lines: int = 3, parent=None):
        super().__init__(parent)
        self.setReadOnly(True)
        self.setOpenLinks(False)
        self.setFrameShape(QFrame.Shape.NoFrame)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self.setLineWrapMode(QTextBrowser.LineWrapMode.WidgetWidth)
        self.setObjectName("fixedTextPane")
        self.document().setDocumentMargin(2)
        self._lines = max(1, int(lines))
        self._update_height()
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)

    def _update_height(self) -> None:
        # Depends on the font the stylesheet applies (known only after polish).
        fw = 2 * self.frameWidth()
        self.setFixedHeight(self.fontMetrics().lineSpacing() * self._lines + 10 + fw)

    def changeEvent(self, ev):                              # noqa: N802
        super().changeEvent(ev)
        if ev.type() in (QEvent.Type.FontChange, QEvent.Type.StyleChange):
            self._update_height()

    def setText(self, text) -> None:                        # noqa: N802
        t = "" if text is None else str(text)
        if Qt.mightBeRichText(t):
            self.setHtml(t)
        else:
            self.setPlainText(t)
        self.setToolTip("")

    def text(self) -> str:
        return self.toPlainText()

    def minimumSizeHint(self):                              # noqa: N802
        s = super().minimumSizeHint()
        s.setWidth(40)
        return s


def scroll_page(page: QWidget, object_name: str = "calTabPage") -> QScrollArea:
    """Wrap a tab page in a frameless, resizable scroll area so its minimum
    size never forces the window larger than the screen."""
    sa = QScrollArea()
    sa.setObjectName(object_name + "Scroll")
    sa.setWidgetResizable(True)
    sa.setFrameShape(QFrame.Shape.NoFrame)
    sa.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
    sa.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
    page.setObjectName(object_name)
    sa.setWidget(page)
    sa.page = page
    return sa

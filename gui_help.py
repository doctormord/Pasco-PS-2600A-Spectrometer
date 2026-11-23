"""gui_help.py — delayed context tooltip for Qt widgets (moved out of
spectrometer_core.py so that module stays Qt-free)."""
from __future__ import annotations

from PyQt6.QtCore import QEvent, QObject, QTimer
from PyQt6.QtWidgets import QToolTip


class ContextHelp(QObject):
    """Installs a 2-second delayed tooltip on any QWidget."""

    def __init__(self, widget, text: str, delay_ms: int = 2000):
        super().__init__(widget)
        self._widget = widget
        self._text   = text
        self._timer  = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.setInterval(delay_ms)
        self._timer.timeout.connect(self._show)
        widget.installEventFilter(self)

    def eventFilter(self, obj, event) -> bool:
        t = event.type()
        if t == QEvent.Type.Enter:
            self._timer.start()
        elif t in (QEvent.Type.Leave, QEvent.Type.MouseButtonPress):
            self._timer.stop()
            QToolTip.hideText()
        return False

    def _show(self) -> None:
        if self._widget.underMouse():
            pos = self._widget.mapToGlobal(self._widget.rect().bottomLeft())
            QToolTip.showText(pos, self._text, self._widget)


def add_help(widget, text: str, delay_ms: int = 2000):
    """Attach a delayed context tooltip to a widget. Returns widget."""
    ContextHelp(widget, text, delay_ms)
    return widget

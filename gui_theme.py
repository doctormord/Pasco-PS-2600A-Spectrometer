"""
Design tokens, dark/light stylesheets, and custom widgets shared by the
dashboard and the CIE tab.

Exposes:
- DarkPalette / LightPalette classes + Theme alias (rebound by apply_theme)
- DARK_STYLESHEET / LIGHT_STYLESHEET strings
- ToggleSwitch, ConnDot, WavelengthFillItem
- make_stat_cell / update_stat_cell, make_row, make_chip, make_hsep / make_vsep
- wavelength_to_color
"""

from __future__ import annotations
import numpy as np

from PyQt6.QtCore import Qt, QRectF, QPointF
from PyQt6.QtGui import (
    QColor, QPainter, QPainterPath, QPen, QBrush, QLinearGradient,
)
from PyQt6.QtWidgets import (
    QPushButton, QWidget, QLabel, QFrame, QHBoxLayout, QVBoxLayout,
)
import pyqtgraph as pg


# ════════════════════════════════════════════════════════════
# Palettes
# ════════════════════════════════════════════════════════════
class DarkPalette:
    BG0 = "#21242b"; BG1 = "#282b33"; BG2 = "#2e3139"; BG3 = "#363943"
    BORDER1 = "#3a3e49"; BORDER2 = "#484c58"
    FG1 = "#eef0f3"; FG2 = "#b9bcc4"; FG3 = "#80848e"; FG4 = "#5a5d66"
    ACCENT = "#5ee0d6"; ACCENT_GLOW = "#5ee0d659"
    ACCENT_BTN_FG = "#143733"; ACCENT_HOVER = "#6ee9df"
    DANGER = "#e36b5c"; DANGER_BORDER = "#623630"; DANGER_BG_HOVER = "#3a221d"
    WARN = "#d8b46a"; WARN_BG = "#3a2f1c"; WARN_BORDER = "#5a4928"
    OK = "#7bd3a5"; OK_BG = "#1f3a2c"; OK_BORDER = "#2e5740"
    MEASURE_A = "#cba6f7"; MEASURE_B = "#d8b46a"


class LightPalette:
    BG0 = "#f4f5f7"; BG1 = "#ffffff"; BG2 = "#eef0f3"; BG3 = "#e3e6ea"
    BORDER1 = "#d8dce1"; BORDER2 = "#b9bfc7"
    FG1 = "#1a1d24"; FG2 = "#3a3f48"; FG3 = "#6c7280"; FG4 = "#9aa0a8"
    ACCENT = "#0d9488"; ACCENT_GLOW = "#0d948859"
    ACCENT_BTN_FG = "#ffffff"; ACCENT_HOVER = "#0fa89b"
    DANGER = "#c0392b"; DANGER_BORDER = "#e3b3ab"; DANGER_BG_HOVER = "#fce8e4"
    WARN = "#a07816"; WARN_BG = "#fbf2dd"; WARN_BORDER = "#e8d39f"
    OK = "#15803d"; OK_BG = "#dcf5e7"; OK_BORDER = "#a4dfba"
    MEASURE_A = "#7c3aed"; MEASURE_B = "#b45309"


FONT_SANS = "Inter, 'Segoe UI', Helvetica, Arial, sans-serif"
FONT_MONO = "'JetBrains Mono', 'Cascadia Mono', Consolas, 'SF Mono', monospace"
for _P in (DarkPalette, LightPalette):
    _P.FONT_SANS = FONT_SANS
    _P.FONT_MONO = FONT_MONO


# Mutable global pointing at the active palette. Rebound by apply_theme.
Theme = DarkPalette


def set_active_palette(palette):
    """Called by DashboardWindow.apply_theme(). Rebinds the module-level alias."""
    global Theme
    Theme = palette


# ════════════════════════════════════════════════════════════
# Stylesheet generator (palette → QSS string)
# ════════════════════════════════════════════════════════════
def build_stylesheet(p) -> str:
    return f"""
* {{ font-family: {FONT_SANS}; color: {p.FG1}; font-size: 13px; }}
QMainWindow, QWidget#central, QWidget#sidebar {{ background: {p.BG0}; }}
QWidget#sidebar {{ background: {p.BG1}; border-left: 1px solid {p.BORDER1}; }}

QWidget#titlebar {{
    background: {p.BG1};
    border-bottom: 1px solid {p.BORDER1};
}}
QLabel#titleText {{ color: {p.FG2}; font-size: 12px; }}

QWidget#toolbar {{
    background: {p.BG0};
    border-bottom: 1px solid {p.BORDER1};
}}

QLabel#sectionHeader {{
    color: {p.FG3};
    font-size: 10px;
    font-weight: 500;
    letter-spacing: 2px;
}}

QLabel#readoutLbl {{
    color: {p.FG3};
    font-size: 9px;
    letter-spacing: 1.5px;
    font-weight: 500;
}}
QLabel#readoutVal       {{ color: {p.FG1};    font-family: {FONT_MONO};
                          font-size: 17px; font-weight: 500; }}
QLabel#readoutValAccent {{ color: {p.ACCENT}; font-family: {FONT_MONO};
                          font-size: 17px; font-weight: 500; }}

QPushButton {{
    background: {p.BG2};
    border: 1px solid {p.BORDER1};
    border-radius: 7px;
    color: {p.FG1};
    font-weight: 500;
    padding: 0 14px;
    min-height: 30px;
}}
QPushButton:hover    {{ background: {p.BG3}; }}
QPushButton:disabled {{ color: {p.FG4}; background: {p.BG1}; }}

/* Tiny toggle switch — must escape the generic QPushButton chrome */
QPushButton#toggleSwitch {{
    background: transparent;
    border: none;
    border-radius: 0;
    min-height: 20px;
    min-width: 36px;
    max-height: 20px;
    max-width: 36px;
    padding: 0;
}}
QPushButton#toggleSwitch:hover {{ background: transparent; }}

QPushButton#ghostBtn {{
    background: transparent;
    border: 1px solid transparent;
    color: {p.FG2};
}}
QPushButton#ghostBtn:hover {{ background: {p.BG2}; color: {p.FG1}; }}

QPushButton#dangerBtn {{
    color: {p.DANGER};
    border-color: {p.DANGER_BORDER};
}}
QPushButton#dangerBtn:hover {{ background: {p.DANGER_BG_HOVER}; }}

/* Title-bar pill row — 26 px tall, matches DARK / LIGHT / EXIT */
QPushButton#exitBtn {{
    background: {p.DANGER};
    color: #ffffff;
    border: none;
    font-weight: 600;
    min-height: 26px;
    max-height: 26px;
    padding: 0 14px;
    border-radius: 6px;
    font-size: 11px;
    letter-spacing: 1px;
}}
QPushButton#exitBtn:hover {{ background: {p.ACCENT_HOVER}; color: {p.ACCENT_BTN_FG}; }}

QLabel#livePill {{
    border-radius: 6px;
    min-height: 26px;
    max-height: 26px;
    padding: 0 12px;
    font-family: {FONT_MONO};
    font-size: 11px;
    qproperty-alignment: AlignCenter;
}}
QLabel#livePillIdle {{
    background: {p.BG2};
    border: 1px solid {p.BORDER1};
    color: {p.FG3};
    border-radius: 6px;
    min-height: 26px;
    max-height: 26px;
    padding: 0 12px;
    font-family: {FONT_MONO};
    font-size: 11px;
    qproperty-alignment: AlignCenter;
}}
QLabel#livePillOk {{
    background: {p.OK_BG};
    border: 1px solid {p.OK_BORDER};
    color: {p.OK};
    border-radius: 6px;
    min-height: 26px;
    max-height: 26px;
    padding: 0 12px;
    font-family: {FONT_MONO};
    font-size: 11px;
    qproperty-alignment: AlignCenter;
}}

QPushButton#primaryBtn {{
    background: {p.ACCENT};
    color: {p.ACCENT_BTN_FG};
    border: none;
    font-weight: 700;
}}
QPushButton#primaryBtn:hover {{ background: {p.ACCENT_HOVER}; }}

QPushButton#deviceSelect {{
    text-align: left;
    padding: 0 12px;
    min-width: 280px;
}}

QPushButton#goBtn {{
    background: {p.ACCENT};
    color: {p.ACCENT_BTN_FG};
    border: none;
    border-radius: 9px;
    font-weight: 700;
    font-size: 13px;
    letter-spacing: 2px;
    min-height: 50px;
    padding: 0 18px;
}}
QPushButton#goBtn:hover {{ background: {p.ACCENT_HOVER}; }}
QPushButton#goBtnPaused {{
    background: {p.BG2};
    color: {p.FG1};
    border: 1px solid {p.BORDER2};
    border-radius: 9px;
    font-weight: 700;
    font-size: 13px;
    letter-spacing: 2px;
    min-height: 50px;
    padding: 0 18px;
}}
QPushButton#goBtnPaused:hover {{ background: {p.BG3}; }}

/* Title-bar segmented theme toggle */
QPushButton#themeSegOn  {{ background: {p.BG3}; color: {p.FG1};
                           border: 1px solid {p.BORDER2};
                           min-height: 26px; max-height: 26px;
                           padding: 0 12px; border-radius: 6px;
                           font-size: 11px; letter-spacing: 1px; }}
QPushButton#themeSegOff {{ background: transparent; color: {p.FG3};
                           border: 1px solid transparent;
                           min-height: 26px; max-height: 26px;
                           padding: 0 12px; border-radius: 6px;
                           font-size: 11px; letter-spacing: 1px; }}
QPushButton#themeSegOff:hover {{ color: {p.FG1}; }}

/* Toolbar checkable quick-action buttons (Peaks / Measure) */
QPushButton#toolToggleOff {{
    background: {p.BG2};
    color: {p.FG2};
    border: 1px solid {p.BORDER1};
    border-radius: 7px;
    min-height: 30px;
    padding: 0 12px;
    font-weight: 500;
    font-size: 11px;
    letter-spacing: 1px;
}}
QPushButton#toolToggleOff:hover {{ background: {p.BG3}; color: {p.FG1}; }}
QPushButton#toolToggleOn {{
    background: {p.ACCENT_GLOW};
    color: {p.ACCENT};
    border: 1px solid {p.ACCENT};
    border-radius: 7px;
    min-height: 30px;
    padding: 0 12px;
    font-weight: 600;
    font-size: 11px;
    letter-spacing: 1px;
}}
QPushButton#toolToggleOn:hover {{ background: {p.ACCENT_GLOW}; }}

QFrame#statCell {{
    background: {p.BG2};
    border: 1px solid {p.BORDER1};
    border-radius: 7px;
}}
QLabel#statLbl  {{ color: {p.FG3}; font-size: 9px;
                   letter-spacing: 1.4px; font-weight: 500; }}
QLabel#statVal  {{ color: {p.FG1}; font-family: {FONT_MONO};
                   font-size: 13px; font-weight: 500; }}

QLabel#rowLbl {{ color: {p.FG2}; font-size: 12px; }}

QAbstractSpinBox {{
    background: {p.BG2};
    border: 1px solid {p.BORDER1};
    border-radius: 5px;
    color: {p.FG1};
    font-family: {FONT_MONO};
    font-size: 12px;
    font-weight: 500;
    padding: 2px 6px;
    min-height: 24px;
    selection-background-color: {p.ACCENT_GLOW};
}}
QAbstractSpinBox:hover    {{ border-color: {p.BORDER2}; }}
QAbstractSpinBox:disabled {{ color: {p.FG4}; }}
QAbstractSpinBox::up-button, QAbstractSpinBox::down-button {{
    background: {p.BG2};
    border-left: 1px solid {p.BORDER1};
    width: 14px;
}}
QAbstractSpinBox::up-button:hover,
QAbstractSpinBox::down-button:hover {{ background: {p.BG3}; }}
QAbstractSpinBox::up-arrow {{
    image: none;
    border-left: 3px solid transparent;
    border-right: 3px solid transparent;
    border-bottom: 4px solid {p.FG3};
    width: 0; height: 0;
}}
QAbstractSpinBox::down-arrow {{
    image: none;
    border-left: 3px solid transparent;
    border-right: 3px solid transparent;
    border-top: 4px solid {p.FG3};
    width: 0; height: 0;
}}

QComboBox {{
    background: {p.BG2};
    border: 1px solid {p.BORDER1};
    border-radius: 5px;
    color: {p.FG1};
    padding: 2px 6px 2px 10px;
    min-height: 24px;
    font-weight: 500;
    font-size: 12px;
}}
QComboBox:hover    {{ border-color: {p.BORDER2}; }}
QComboBox:disabled {{ color: {p.FG4}; }}
QComboBox::drop-down {{ border: none; width: 18px; }}
QComboBox::down-arrow {{
    image: none;
    border-left: 4px solid transparent;
    border-right: 4px solid transparent;
    border-top: 5px solid {p.FG3};
    margin-right: 4px;
}}
QComboBox QAbstractItemView {{
    background: {p.BG2};
    border: 1px solid {p.BORDER2};
    selection-background-color: {p.BG3};
    color: {p.FG1};
    padding: 4px;
    outline: 0;
}}

QTabWidget::pane {{
    border: none;
    border-top: 1px solid {p.BORDER1};
    background: {p.BG0};
}}
QTabBar {{ background: {p.BG0}; }}
QTabBar::tab {{
    background: transparent;
    color: {p.FG3};
    font-weight: 500;
    font-size: 12px;
    padding: 8px 16px;
    border: none;
    border-bottom: 2px solid transparent;
}}
QTabBar::tab:hover     {{ color: {p.FG1}; }}
QTabBar::tab:selected  {{ color: {p.FG1};
                          border-bottom: 2px solid {p.ACCENT}; }}

QWidget#statusbar {{
    background: {p.BG1};
    border-top: 1px solid {p.BORDER1};
}}
QLabel#statusKey {{ color: {p.FG3}; font-family: {FONT_MONO}; font-size: 11px; }}
QLabel#statusVal {{ color: {p.FG1}; font-family: {FONT_MONO};
                    font-size: 11px; font-weight: 500; }}

QLabel#sensorChip {{
    border-radius: 8px;
    padding: 4px 12px;
    font-family: {FONT_MONO};
    font-size: 10px;
    min-height: 18px;
}}

QScrollArea, QScrollArea > QWidget > QWidget {{ background: {p.BG1}; }}
QScrollBar:vertical {{ background: {p.BG1}; width: 10px; margin: 0; border: none; }}
QScrollBar::handle:vertical {{ background: {p.BORDER2}; border-radius: 4px;
                               min-height: 30px; }}
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{ height: 0; }}
QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical {{ background: transparent; }}

QToolTip {{
    background: {p.BG2};
    color: {p.FG1};
    border: 1px solid {p.ACCENT};
    padding: 6px;
    font-size: 12px;
}}
QMessageBox {{ background: {p.BG1}; color: {p.FG1}; }}
"""


DARK_STYLESHEET  = build_stylesheet(DarkPalette)
LIGHT_STYLESHEET = build_stylesheet(LightPalette)


# ════════════════════════════════════════════════════════════
# Helpers
# ════════════════════════════════════════════════════════════
def wavelength_to_color(nm: float) -> QColor:
    """Approximate visible-spectrum colour for a wavelength in nm."""
    if nm < 380 or nm > 780:
        return QColor(90, 90, 115)
    if   380 <= nm < 440: r, g, b = -(nm - 440) / 60, 0, 1
    elif 440 <= nm < 490: r, g, b = 0, (nm - 440) / 50, 1
    elif 490 <= nm < 510: r, g, b = 0, 1, -(nm - 510) / 20
    elif 510 <= nm < 580: r, g, b = (nm - 510) / 70, 1, 0
    elif 580 <= nm < 645: r, g, b = 1, -(nm - 645) / 65, 0
    else:                  r, g, b = 1, 0, 0
    factor = 1.0
    if nm < 420:   factor = 0.3 + 0.7 * (nm - 380) / 40
    elif nm > 700: factor = 0.3 + 0.7 * (780 - nm) / 80
    r = max(0.0, r) * factor
    g = max(0.0, g) * factor
    b = max(0.0, b) * factor
    return QColor(int(255 * r ** 0.8),
                  int(255 * g ** 0.8),
                  int(255 * b ** 0.8))


# ════════════════════════════════════════════════════════════
# Custom widgets
# ════════════════════════════════════════════════════════════
class WavelengthFillItem(pg.GraphicsObject):
    """
    Paints a wavelength-mapped gradient fill UNDER the spectrum curve.

    Why this exists: drawing the fill via `pg.PlotCurveItem(brush=QBrush(gradient))`
    with `ObjectBoundingMode` produces repeating rainbow bars because each fill
    sub-segment is rebound to its own bounding box. This item draws the gradient
    ONCE in data coordinates, clipped to the closed curve→baseline path.
    """
    def __init__(self, parent=None):
        super().__init__(parent)
        self._x = np.array([])
        self._y = np.array([])
        self._alpha = 0.55

    def setData(self, x, y):
        self._x = np.asarray(x, dtype=float)
        self._y = np.asarray(y, dtype=float)
        self.prepareGeometryChange()
        self.update()

    def setAlpha(self, a: float):
        self._alpha = max(0.0, min(1.0, a))
        self.update()

    def boundingRect(self):
        if self._x.size == 0:
            return QRectF(0, 0, 1, 1)
        x0 = float(self._x[0]); x1 = float(self._x[-1])
        y0 = float(min(0.0, np.min(self._y)))
        y1 = float(max(1.0, np.max(self._y)))
        return QRectF(x0, y0, max(1.0, x1 - x0), max(1.0, y1 - y0))

    def paint(self, painter, option, widget=None):
        if self._x.size < 2:
            return
        x0, x1 = float(self._x[0]), float(self._x[-1])
        path = QPainterPath()
        path.moveTo(x0, 0.0)
        for xv, yv in zip(self._x, self._y):
            path.lineTo(float(xv), float(yv))
        path.lineTo(x1, 0.0)
        path.closeSubpath()
        grad = QLinearGradient(x0, 0.0, x1, 0.0)
        N = 48
        for i in range(N + 1):
            t = i / N
            c = wavelength_to_color(x0 + t * (x1 - x0))
            c.setAlphaF(self._alpha)
            grad.setColorAt(t, c)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QBrush(grad))
        painter.drawPath(path)


class ToggleSwitch(QPushButton):
    """iOS-style checkable toggle. Emits .toggled(bool)."""
    def __init__(self, checked: bool = False, parent=None):
        super().__init__(parent)
        self.setObjectName("toggleSwitch")
        self.setCheckable(True)
        self.setChecked(checked)
        self.setFixedSize(36, 20)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.toggled.connect(lambda _: self.update())

    def paintEvent(self, ev):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        on = self.isChecked()
        track  = QColor(Theme.ACCENT_GLOW) if on else QColor(Theme.BG3)
        border = QColor(Theme.ACCENT)      if on else QColor(Theme.BORDER2)
        p.setBrush(track)
        p.setPen(QPen(border, 1))
        r = self.rect().adjusted(0, 0, -1, -1)
        p.drawRoundedRect(r, 10, 10)
        knob = QColor(Theme.ACCENT) if on else QColor(Theme.FG2)
        p.setBrush(knob)
        p.setPen(Qt.PenStyle.NoPen)
        x = 18 if on else 2
        p.drawEllipse(x, 2, 16, 16)


class ConnDot(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedSize(18, 18)
        self._on = False

    def setOn(self, on: bool):
        if on != self._on:
            self._on = on
            self.update()

    def paintEvent(self, ev):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        cx, cy = 9, 9
        p.setPen(Qt.PenStyle.NoPen)
        if self._on:
            p.setBrush(QColor(123, 211, 165, 60))
            p.drawEllipse(QPointF(cx, cy), 9, 9)
            p.setBrush(QColor(Theme.OK))
        else:
            p.setBrush(QColor(Theme.FG4))
        p.drawEllipse(QPointF(cx, cy), 4, 4)


def make_stat_cell(label: str, value: str, unit: str = "") -> QFrame:
    cell = QFrame()
    cell.setObjectName("statCell")
    lay = QVBoxLayout(cell)
    lay.setContentsMargins(10, 7, 10, 7)
    lay.setSpacing(2)
    lbl = QLabel(label.upper())
    lbl.setObjectName("statLbl")
    val = QLabel()
    val.setObjectName("statVal")
    val.setTextFormat(Qt.TextFormat.RichText)
    unit_html = (f' <span style="color:{Theme.FG3};font-size:10px;"> {unit}</span>'
                 if unit else "")
    val.setText(f"{value}{unit_html}")
    lay.addWidget(lbl); lay.addWidget(val)
    cell._value_label = val
    cell._unit_html = unit_html
    return cell


def update_stat_cell(cell: QFrame, value: str):
    cell._value_label.setText(f"{value}{cell._unit_html}")


def make_row(label: str, control, sub: str | None = None) -> QWidget:
    w = QWidget()
    h = QHBoxLayout(w)
    h.setContentsMargins(0, 4, 0, 4)
    h.setSpacing(8)
    text = label
    if sub:
        text = (f'{label} <span style="color:{Theme.FG4};font-size:11px;">'
                f' {sub}</span>')
    lbl = QLabel(text)
    lbl.setTextFormat(Qt.TextFormat.RichText)
    lbl.setObjectName("rowLbl")
    lbl.setMinimumHeight(28)
    h.addWidget(lbl, 1)
    if isinstance(control, (list, tuple)):
        sub_lay = QHBoxLayout(); sub_lay.setSpacing(6)
        for c in control:
            sub_lay.addWidget(c)
        h.addLayout(sub_lay)
    else:
        h.addWidget(control)
    return w


def make_chip(text: str, object_name: str = "chip") -> QLabel:
    lbl = QLabel(text)
    lbl.setObjectName(object_name)
    return lbl


def make_hsep() -> QFrame:
    s = QFrame()
    s.setFixedHeight(1)
    s.setStyleSheet(f"background:{Theme.BORDER1};")
    return s


def make_vsep(height: int = 14) -> QFrame:
    s = QFrame()
    s.setFixedSize(1, height)
    s.setStyleSheet(f"background:{Theme.BORDER2};")
    return s

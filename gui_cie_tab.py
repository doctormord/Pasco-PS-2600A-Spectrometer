"""
CIE 1931 chromaticity / CCT / CRI tab.

Shows on the left: the CIE 1931 xy chromaticity diagram with the spectrum-
locus horseshoe, the Planckian locus, isothermal lines at common CCTs, and
a marker for the live measurement.

On the right: a stack of large numeric readouts (CCT, Duv, x, y, CRI Ra)
plus a bar chart of the eight CRI test-color shifts.
"""

from __future__ import annotations
import numpy as np

from PyQt6.QtCore import Qt
from PyQt6.QtGui import QColor, QPen, QBrush, QFont
from PyQt6.QtWidgets import (
    QWidget, QHBoxLayout, QVBoxLayout, QLabel, QFrame, QGridLayout,
)
import pyqtgraph as pg

from gui_theme import (
    FONT_MONO, FONT_SANS, make_chip, make_stat_cell, update_stat_cell,
    make_hsep,
)
import gui_theme as gt
import color_science as cs


def _T():
    """Always read the current palette (rebinds during theme switches)."""
    return gt.Theme


def _xy_to_srgb(x: float, y: float) -> tuple[float, float, float]:
    """Convert CIE 1931 xy to clipped sRGB (for diagram fill)."""
    if y <= 0:
        return 0.0, 0.0, 0.0
    Y = 1.0
    X = (x / y) * Y
    Z = ((1.0 - x - y) / y) * Y
    # sRGB primaries (D65 reference white)
    r =  3.2406 * X - 1.5372 * Y - 0.4986 * Z
    g = -0.9689 * X + 1.8758 * Y + 0.0415 * Z
    b =  0.0557 * X - 0.2040 * Y + 1.0570 * Z
    rgb = np.array([r, g, b])
    rgb = np.clip(rgb, 0.0, None)
    m = rgb.max()
    if m > 1.0:
        rgb = rgb / m
    # gamma-encode
    rgb = np.where(rgb <= 0.0031308,
                   12.92 * rgb,
                   1.055 * rgb ** (1.0 / 2.4) - 0.055)
    rgb = np.clip(rgb, 0.0, 1.0)
    return float(rgb[0]), float(rgb[1]), float(rgb[2])


def _build_horseshoe_fill_image(size: int = 220) -> np.ndarray:
    """Generate an RGBA image (size×size) of the colorful chromaticity gamut."""
    from matplotlib.path import Path  # Avoid hard dep — fall back below.
    locus = cs.spectrum_locus_xy()  # (N,2)
    path = Path(locus)

    img = np.zeros((size, size, 4), dtype=np.uint8)
    xs = np.linspace(0.0, 0.8, size)
    ys = np.linspace(0.0, 0.9, size)
    grid = np.stack(np.meshgrid(xs, ys), axis=-1).reshape(-1, 2)
    mask = path.contains_points(grid).reshape(size, size)
    for j in range(size):
        for i in range(size):
            if not mask[j, i]:
                continue
            r, g, b = _xy_to_srgb(xs[i], ys[j])
            img[j, i] = (int(r * 255), int(g * 255), int(b * 255), 230)
    return img


def _build_horseshoe_fill_fallback(size: int = 220) -> np.ndarray:
    """Vectorised polygon test (numpy only — no matplotlib required)."""
    locus = cs.spectrum_locus_xy()
    xs = np.linspace(0.0, 0.8, size)
    ys = np.linspace(0.0, 0.9, size)
    # Build mask via ray casting against the polygon edges
    poly = locus
    n = len(poly)
    PX, PY = np.meshgrid(xs, ys)            # (size, size)
    inside = np.zeros_like(PX, dtype=bool)
    j = n - 1
    for i in range(n):
        yi, yj = poly[i, 1], poly[j, 1]
        xi, xj = poly[i, 0], poly[j, 0]
        cond_a = (yi > PY) != (yj > PY)
        cond_b = PX < (xj - xi) * (PY - yi) / (yj - yi + 1e-12) + xi
        inside ^= cond_a & cond_b
        j = i
    # Build the colour grid — only where inside
    img = np.zeros((size, size, 4), dtype=np.uint8)
    idx = np.argwhere(inside)
    for j_idx, i_idx in idx:
        r, g, b = _xy_to_srgb(xs[i_idx], ys[j_idx])
        img[j_idx, i_idx] = (int(r * 255), int(g * 255), int(b * 255), 230)
    return img


def build_horseshoe_fill_image(size: int = 220) -> np.ndarray:
    try:
        return _build_horseshoe_fill_image(size)
    except Exception:
        return _build_horseshoe_fill_fallback(size)


# ════════════════════════════════════════════════════════════
# CIE tab widget
# ════════════════════════════════════════════════════════════
class CIETab(QWidget):
    """
    Owns the CIE 1931 chromaticity plot + the numeric readouts.

    Call .update_measurement(wavelengths, intensities) whenever a fresh
    spectrum arrives. The widget cheaply throttles updates internally.
    """

    UPDATE_EVERY_N_FRAMES = 5     # CRI math is expensive; throttle it

    def __init__(self, parent=None):
        super().__init__(parent)
        self._frame_count = 0
        self._last_xy = (None, None)
        self._build_ui()

    # ── UI assembly ─────────────────────────────────────────
    def _build_ui(self) -> None:
        root = QHBoxLayout(self)
        root.setContentsMargins(14, 14, 14, 14)
        root.setSpacing(14)

        # ── LEFT: CIE 1931 plot ──
        self.plot = pg.PlotWidget()
        self.plot.setBackground(_T().BG0)
        self.plot.setAspectLocked(True)
        self.plot.setXRange(0.0, 0.8, padding=0)
        self.plot.setYRange(0.0, 0.9, padding=0)
        self.plot.showGrid(x=True, y=True, alpha=0.10)
        for axis_name in ("bottom", "left"):
            ax = self.plot.getAxis(axis_name)
            ax.setPen(pg.mkPen(QColor(_T().BORDER2), width=1))
            ax.setTextPen(pg.mkPen(QColor(_T().FG3)))
            font = QFont("JetBrains Mono"); font.setPointSize(9)
            ax.setTickFont(font)
        self.plot.setLabel("bottom", "x", color=_T().FG3)
        self.plot.setLabel("left",   "y", color=_T().FG3)
        self.plot.setMouseEnabled(False, False)
        self.plot.hideButtons()

        # Coloured gamut fill (rendered once)
        fill_img = build_horseshoe_fill_image(220)
        self._fill_item = pg.ImageItem(fill_img, axisOrder='row-major')
        # Image domain x:[0,0.8], y:[0,0.9] — transform pixels→data
        from PyQt6.QtGui import QTransform
        tr = QTransform()
        tr.scale(0.8 / fill_img.shape[1], 0.9 / fill_img.shape[0])
        self._fill_item.setTransform(tr)
        self.plot.addItem(self._fill_item)

        # Horseshoe outline
        locus = cs.spectrum_locus_xy()
        self._locus_curve = self.plot.plot(
            locus[:, 0], locus[:, 1],
            pen=pg.mkPen(_T().FG1, width=1.5))

        # Wavelength labels along the locus (every 50 nm)
        self._locus_labels = []
        for wl in (380, 460, 480, 500, 520, 540, 560, 580, 600, 620, 650, 700):
            i = int((wl - 380) / 5)
            if i >= len(locus) - 1:
                continue
            t = pg.TextItem(anchor=(0.5, 1.2))
            t.setHtml(
                f'<span style="color:{_T().FG2};font-family:{FONT_MONO};'
                f'font-size:9pt;">{wl}</span>')
            t.setPos(float(locus[i, 0]), float(locus[i, 1]))
            self.plot.addItem(t, ignoreBounds=True)
            self._locus_labels.append(t)

        # Planckian locus
        pl = cs.planckian_locus_xy(1500.0, 20000.0, 70)
        self._planck_curve = self.plot.plot(
            pl[:, 0], pl[:, 1],
            pen=pg.mkPen(_T().WARN, width=1.6, style=Qt.PenStyle.DashLine))

        # Isothermal markers + labels at common CCTs
        self._iso_markers = []
        for T in (2700, 3000, 4000, 5000, 6500, 10000):
            spd = cs.planckian_spd(T)
            X, Y, Z = cs.spectrum_to_xyz(cs.CIE_WL_5, spd)
            x, y = cs.xyz_to_xy(X, Y, Z)
            dot = pg.ScatterPlotItem(
                size=6, brush=pg.mkBrush(_T().WARN),
                pen=pg.mkPen(_T().BG0, width=1.5), symbol="o")
            dot.setData([x], [y])
            self.plot.addItem(dot)
            lbl = pg.TextItem(anchor=(0.0, 0.5))
            lbl.setHtml(
                f'<span style="color:{_T().WARN};font-family:{FONT_MONO};'
                f'font-size:8.5pt;"> {T}K</span>')
            lbl.setPos(x, y)
            self.plot.addItem(lbl, ignoreBounds=True)
            self._iso_markers.append((dot, lbl))

        # Measurement point (large accent marker)
        self._measure_point = pg.ScatterPlotItem(
            size=14, brush=pg.mkBrush(_T().ACCENT),
            pen=pg.mkPen(_T().BG0, width=2), symbol="o")
        self.plot.addItem(self._measure_point)
        self._measure_label = pg.TextItem(anchor=(0.0, 1.4))
        self.plot.addItem(self._measure_label, ignoreBounds=True)

        root.addWidget(self.plot, 1)

        # ── RIGHT: readouts ──
        right = QWidget()
        right.setFixedWidth(330)
        rv = QVBoxLayout(right)
        rv.setContentsMargins(0, 0, 0, 0)
        rv.setSpacing(10)

        # Big CCT card
        self.card_cct = self._make_big_card("CCT", "—", "K")
        rv.addWidget(self.card_cct)

        # Row of two small cards: Duv + Ra
        small_row = QHBoxLayout()
        small_row.setSpacing(8)
        self.card_duv = self._make_big_card("Duv", "—", "", small=True)
        self.card_ra  = self._make_big_card("CRI Ra", "—", "", small=True)
        small_row.addWidget(self.card_duv)
        small_row.addWidget(self.card_ra)
        rv.addLayout(small_row)

        # xy cards
        xy_row = QHBoxLayout()
        xy_row.setSpacing(8)
        self.card_x = self._make_big_card("x", "—", "", small=True)
        self.card_y = self._make_big_card("y", "—", "", small=True)
        xy_row.addWidget(self.card_x)
        xy_row.addWidget(self.card_y)
        rv.addLayout(xy_row)

        rv.addWidget(make_hsep())

        # R1..R8 bar plot
        hdr = QLabel("INDIVIDUAL TEST COLORS  R1…R8")
        hdr.setObjectName("sectionHeader")
        rv.addWidget(hdr)

        self.ri_plot = pg.PlotWidget()
        self.ri_plot.setBackground(_T().BG1)
        self.ri_plot.setFixedHeight(170)
        self.ri_plot.setMouseEnabled(False, False)
        self.ri_plot.hideButtons()
        self.ri_plot.showGrid(y=True, alpha=0.10)
        self.ri_plot.setXRange(0.4, 8.6, padding=0)
        self.ri_plot.setYRange(0, 100, padding=0)
        for axis_name in ("bottom", "left"):
            ax = self.ri_plot.getAxis(axis_name)
            ax.setPen(pg.mkPen(QColor(_T().BORDER2), width=1))
            ax.setTextPen(pg.mkPen(QColor(_T().FG3)))
        # X tick labels
        ax_b = self.ri_plot.getAxis("bottom")
        ax_b.setTicks([[(i + 1, f"R{i+1}") for i in range(8)]])
        self._ri_bar = pg.BarGraphItem(
            x=list(range(1, 9)), height=[0] * 8, width=0.7,
            brush=pg.mkBrush(_T().ACCENT), pen=pg.mkPen(_T().BG0, width=0))
        self.ri_plot.addItem(self._ri_bar)
        # 100% reference dotted line
        ref = pg.InfiniteLine(angle=0, pos=100,
                              pen=pg.mkPen(_T().FG3, style=Qt.PenStyle.DotLine))
        self.ri_plot.addItem(ref)
        rv.addWidget(self.ri_plot)

        # Helper note
        note = QLabel(
            'CIE 1931 2° observer · CRI Ra per CIE 13.3-1995\n'
            'Reference: Planckian < 5000 K, D-illuminant ≥ 5000 K')
        note.setStyleSheet(f"color:{_T().FG4};font-size:10px;line-height:1.4;")
        note.setWordWrap(True)
        rv.addWidget(note)
        rv.addStretch(1)

        root.addWidget(right)

    def _make_big_card(self, label: str, value: str, unit: str,
                       small: bool = False) -> QFrame:
        cell = QFrame()
        cell.setObjectName("statCell")
        v = QVBoxLayout(cell)
        v.setContentsMargins(14, 10, 14, 12)
        v.setSpacing(4)
        lbl = QLabel(label.upper())
        lbl.setObjectName("statLbl")
        v.addWidget(lbl)
        val = QLabel()
        val.setTextFormat(Qt.TextFormat.RichText)
        size = 18 if small else 30
        val.setStyleSheet(
            f"color:{_T().FG1};font-family:{FONT_MONO};"
            f"font-size:{size}px;font-weight:600;")
        unit_html = (f' <span style="color:{_T().FG3};font-size:11px;">'
                     f' {unit}</span>') if unit else ""
        val.setText(f"{value}{unit_html}")
        v.addWidget(val)
        cell._val = val
        cell._unit_html = unit_html
        return cell

    @staticmethod
    def _set_card(cell: QFrame, value: str) -> None:
        cell._val.setText(f"{value}{cell._unit_html}")

    # ── public API ──────────────────────────────────────────
    def update_measurement(self, wavelengths_nm: np.ndarray,
                           intensities: np.ndarray) -> None:
        """Called by DashboardWindow whenever a fresh frame arrives."""
        self._frame_count += 1
        if self._frame_count % self.UPDATE_EVERY_N_FRAMES != 0:
            return

        try:
            X, Y, Z = cs.spectrum_to_xyz(wavelengths_nm, intensities)
            if Y <= 0:
                self._show_no_data()
                return
            x, y = cs.xyz_to_xy(X, Y, Z)
            cct = cs.cct_mccamy(x, y)
            duv = cs.duv_from_xy(x, y)
        except Exception:
            self._show_no_data()
            return

        # Update CIE plot marker
        self._measure_point.setData([x], [y])
        if np.isfinite(cct):
            cct_text = f"{cct:.0f} K  ·  Duv {duv:+.4f}"
        else:
            cct_text = "—"
        self._measure_label.setHtml(
            f'<span style="color:{_T().ACCENT};font-family:{FONT_MONO};'
            f'font-size:9.5pt;font-weight:600;">  {cct_text}</span>')
        self._measure_label.setPos(x, y)

        # Numeric cards
        self._set_card(self.card_cct, f"{cct:.0f}" if np.isfinite(cct) else "—")
        self._set_card(self.card_duv, f"{duv:+.4f}" if np.isfinite(duv) else "—")
        self._set_card(self.card_x,   f"{x:.4f}")
        self._set_card(self.card_y,   f"{y:.4f}")

        # CRI (heavier — only compute every 2nd update for headroom)
        try:
            Ra, Ri, _ = cs.cri_ra(wavelengths_nm, intensities)
        except Exception:
            Ra, Ri = float('nan'), [float('nan')] * 8

        self._set_card(self.card_ra,
                       f"{Ra:.1f}" if np.isfinite(Ra) else "—")
        # Update bars (clip at [0, 100] for display; show -100 as zero)
        bar_heights = [max(0.0, min(100.0, r)) if np.isfinite(r) else 0.0
                       for r in Ri]
        bar_colors = [self._ra_color_for(r) for r in Ri]
        self._ri_bar.setOpts(height=bar_heights, brushes=bar_colors)
        self._last_xy = (x, y)

    def _show_no_data(self) -> None:
        for c in (self.card_cct, self.card_duv, self.card_x,
                  self.card_y, self.card_ra):
            self._set_card(c, "—")
        self._measure_point.clear()
        self._measure_label.setHtml("")

    @staticmethod
    def _ra_color_for(ri: float) -> QBrush:
        if not np.isfinite(ri):
            return pg.mkBrush(_T().FG4)
        if ri >= 90:   return pg.mkBrush(_T().OK)
        if ri >= 80:   return pg.mkBrush(_T().ACCENT)
        if ri >= 60:   return pg.mkBrush(_T().WARN)
        return pg.mkBrush(_T().DANGER)

    def apply_theme(self) -> None:
        """Repaint plot chrome after a theme switch."""
        self.plot.setBackground(_T().BG0)
        self.ri_plot.setBackground(_T().BG1)
        for axis_name in ("bottom", "left"):
            for plot in (self.plot, self.ri_plot):
                ax = plot.getAxis(axis_name)
                ax.setPen(pg.mkPen(QColor(_T().BORDER2), width=1))
                ax.setTextPen(pg.mkPen(QColor(_T().FG3)))
        self._locus_curve.setPen(pg.mkPen(_T().FG1, width=1.5))
        self._planck_curve.setPen(pg.mkPen(_T().WARN, width=1.6,
                                           style=Qt.PenStyle.DashLine))
        self._measure_point.setBrush(pg.mkBrush(_T().ACCENT))
        self._measure_point.setPen(pg.mkPen(_T().BG0, width=2))

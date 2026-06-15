"""
Color tab — comprehensive light-source characterisation.

Three nested sub-tabs:
  · Overview        big cards + small previews
  · CIE 1931        chromaticity diagram + numerics
  · Color Rendering radar + bar chart + R1..R15 table
"""

from __future__ import annotations
import os
import time
import colorsys
import numpy as np

from PyQt6.QtCore import Qt, QObject, QThread, pyqtSignal
from PyQt6.QtGui import QColor, QPen, QBrush, QFont, QTransform
from PyQt6.QtWidgets import (
    QWidget, QHBoxLayout, QVBoxLayout, QLabel, QFrame, QGridLayout,
    QTabWidget, QPushButton, QFileDialog, QMessageBox, QApplication,
)
import pyqtgraph as pg

from gui_theme import (
    FONT_MONO, FONT_SANS, make_chip, make_stat_cell, update_stat_cell,
    make_hsep, make_vsep,
)
import gui_theme as gt
import color_science as cs


def _T():
    """Always read the current palette (rebinds during theme switches)."""
    return gt.Theme


# ════════════════════════════════════════════════════════════
# CIE 1931 gamut fill image (colourful horseshoe)
# ════════════════════════════════════════════════════════════
def _xy_to_srgb(x: float, y: float) -> tuple[float, float, float]:
    """CIE xy → clipped sRGB triplet (used for the diagram fill)."""
    if y <= 0:
        return 0.0, 0.0, 0.0
    Y = 1.0
    X = (x / y) * Y
    Z = ((1.0 - x - y) / y) * Y
    r =  3.2406 * X - 1.5372 * Y - 0.4986 * Z
    g = -0.9689 * X + 1.8758 * Y + 0.0415 * Z
    b =  0.0557 * X - 0.2040 * Y + 1.0570 * Z
    rgb = np.array([r, g, b])
    rgb = np.clip(rgb, 0.0, None)
    m = rgb.max()
    if m > 1.0:
        rgb = rgb / m
    rgb = np.where(rgb <= 0.0031308,
                   12.92 * rgb,
                   1.055 * rgb ** (1.0 / 2.4) - 0.055)
    return tuple(float(v) for v in np.clip(rgb, 0.0, 1.0))


def _build_horseshoe_fill_image(size: int = 220) -> np.ndarray:
    try:
        from matplotlib.path import Path
        locus = cs.spectrum_locus_xy()
        path = Path(locus)
        xs = np.linspace(0.0, 0.8, size)
        ys = np.linspace(0.0, 0.9, size)
        grid = np.stack(np.meshgrid(xs, ys), axis=-1).reshape(-1, 2)
        mask = path.contains_points(grid).reshape(size, size)
    except Exception:
        # Vectorised fallback
        locus = cs.spectrum_locus_xy()
        xs = np.linspace(0.0, 0.8, size)
        ys = np.linspace(0.0, 0.9, size)
        PX, PY = np.meshgrid(xs, ys)
        mask = np.zeros_like(PX, dtype=bool)
        n = len(locus)
        j = n - 1
        for i in range(n):
            yi, yj = locus[i, 1], locus[j, 1]
            xi, xj = locus[i, 0], locus[j, 0]
            cond_a = (yi > PY) != (yj > PY)
            cond_b = PX < (xj - xi) * (PY - yi) / (yj - yi + 1e-12) + xi
            mask ^= cond_a & cond_b
            j = i

    img = np.zeros((size, size, 4), dtype=np.uint8)
    idx = np.argwhere(mask)
    for j_idx, i_idx in idx:
        r, g, b = _xy_to_srgb(xs[i_idx], ys[j_idx])
        img[j_idx, i_idx] = (int(r * 255), int(g * 255), int(b * 255), 230)
    return img


# ════════════════════════════════════════════════════════════
# Visual constants for the CRI bar / radar
# ════════════════════════════════════════════════════════════
# Approximate display colour for each TCS — used by the bar chart.
TCS_DISPLAY_COLORS = [
    "#E8B89B",   # R1  light greyish red
    "#D9C982",   # R2  dark greyish yellow
    "#C7D068",   # R3  strong yellow green
    "#8FBE7C",   # R4  moderate yellowish green
    "#7BBFAE",   # R5  light bluish green
    "#7AA3D4",   # R6  light blue
    "#9F8DCB",   # R7  light violet
    "#C77AB1",   # R8  light reddish purple
    "#C13F3A",   # R9  strong red
    "#D8C04A",   # R10 strong yellow
    "#4FA864",   # R11 strong green
    "#2F4FB8",   # R12 strong blue
    "#E2B89A",   # R13 complexion (Caucasian)
    "#7E8F50",   # R14 leaf
    "#D9B8A0",   # R15 complexion (Asian)
]


# ════════════════════════════════════════════════════════════
# Helpers
# ════════════════════════════════════════════════════════════
def _make_metric_card(label: str, default_value: str = "—",
                      unit: str = "", *, value_size: int = 28,
                      min_width: int = 0) -> QFrame:
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
    val.setStyleSheet(
        f"color:{_T().FG1};font-family:{FONT_MONO};"
        f"font-size:{value_size}px;font-weight:600;")
    unit_html = (f' <span style="color:{_T().FG3};font-size:11px;font-weight:400;">'
                 f' {unit}</span>') if unit else ""
    val.setText(f"{default_value}{unit_html}")
    v.addWidget(val)
    cell._val = val
    cell._unit_html = unit_html
    if min_width:
        cell.setMinimumWidth(min_width)
    return cell


def _set_card(cell: QFrame, value: str) -> None:
    cell._val.setText(f"{value}{cell._unit_html}")


def _section_header(text: str) -> QLabel:
    lbl = QLabel(text.upper())
    lbl.setObjectName("sectionHeader")
    return lbl


def _ri_brush(ri: float) -> QBrush:
    """Traffic-light colour mapping for individual Ri scores."""
    if not np.isfinite(ri):
        return pg.mkBrush(_T().FG4)
    if ri >= 90: return pg.mkBrush(_T().OK)
    if ri >= 80: return pg.mkBrush(_T().ACCENT)
    if ri >= 60: return pg.mkBrush(_T().WARN)
    return pg.mkBrush(_T().DANGER)


def _sample_hue_brushes(n: int) -> list:
    """Distinct per-sample colours for the 99 TM-30 colour-evaluation samples:
    a continuous hue ramp (red → magenta) so every bar gets its own colour
    instead of sharing one of the 16 hue-bin colours."""
    out = []
    for i in range(n):
        h = (i / max(n - 1, 1)) * 0.83          # 0..0.83 → red round to magenta
        r, g, b = colorsys.hsv_to_rgb(h, 0.62, 0.97)
        out.append(pg.mkBrush(QColor(int(r * 255), int(g * 255), int(b * 255))))
    return out


# ════════════════════════════════════════════════════════════
# Sub-tab 1: OVERVIEW
# ════════════════════════════════════════════════════════════
class _OverviewPage(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        root = QVBoxLayout(self)
        root.setContentsMargins(14, 14, 14, 14)
        root.setSpacing(12)

        # Top: four big headline cards
        grid = QGridLayout()
        grid.setSpacing(10)
        self.card_cct = _make_metric_card("CCT", "—", "K", value_size=34)
        self.card_lux = _make_metric_card("Illuminance", "—", "lx", value_size=34)
        self.card_lux.setToolTip(
            "Requires absolute radiometric calibration.\n"
            "Set radiometric_calibration_factor (W/m²/nm per ADC count)\n"
            "in spectrometer_config.json to enable this readout.\n"
            "All other metrics are shape-based and do not need calibration."
        )
        self.card_duv = _make_metric_card("Duv", "—", "",   value_size=28)
        self.card_ra  = _make_metric_card("CRI Ra", "—", "", value_size=34)
        grid.addWidget(self.card_cct, 0, 0)
        grid.addWidget(self.card_lux, 0, 1)
        grid.addWidget(self.card_duv, 0, 2)
        grid.addWidget(self.card_ra,  0, 3)
        root.addLayout(grid)

        # Second row: secondary metrics
        grid2 = QGridLayout()
        grid2.setSpacing(8)
        self.card_fc        = _make_metric_card("Foot-candle", "—", "fc", value_size=18)
        self.card_sp        = _make_metric_card("S / P ratio", "—", "",   value_size=18)
        self.card_sdcm      = _make_metric_card("SDCM",        "—", "",   value_size=18)
        self.card_peak      = _make_metric_card("Peak λ",      "—", "nm", value_size=18)
        self.card_dom       = _make_metric_card("Dominant λ",  "—", "nm", value_size=18)
        self.card_purity    = _make_metric_card("Purity",      "—", "%",  value_size=18)
        for i, c in enumerate((self.card_fc, self.card_sp, self.card_sdcm,
                               self.card_peak, self.card_dom, self.card_purity)):
            grid2.addWidget(c, 0, i)
        root.addLayout(grid2)

        # Third row: more spectral descriptors
        grid3 = QGridLayout()
        grid3.setSpacing(8)
        self.card_fwhm      = _make_metric_card("FWHM",         "—", "nm", value_size=18)
        self.card_centroid  = _make_metric_card("Centroid λ",   "—", "nm", value_size=18)
        self.card_central   = _make_metric_card("Central λ",    "—", "nm", value_size=18)
        self.card_red       = _make_metric_card("Red band",     "—", "%",  value_size=18)
        self.card_green     = _make_metric_card("Green band",   "—", "%",  value_size=18)
        self.card_blue      = _make_metric_card("Blue band",    "—", "%",  value_size=18)
        for i, c in enumerate((self.card_fwhm, self.card_centroid, self.card_central,
                               self.card_red, self.card_green, self.card_blue)):
            grid3.addWidget(c, 0, i)
        root.addLayout(grid3)

        root.addWidget(make_hsep())

        # Bottom row: colour-space coordinates table
        coord_w = QWidget()
        ch = QHBoxLayout(coord_w)
        ch.setContentsMargins(0, 0, 0, 0); ch.setSpacing(10)
        ch.addWidget(_section_header("Color-space coordinates"))
        ch.addStretch(1)
        coord_w.setMaximumHeight(28)
        root.addWidget(coord_w)

        coord_grid = QGridLayout()
        coord_grid.setSpacing(6)
        self.card_x   = _make_metric_card("x  (CIE 1931)", "—", "", value_size=18)
        self.card_y   = _make_metric_card("y  (CIE 1931)", "—", "", value_size=18)
        self.card_u60 = _make_metric_card("u  (CIE 1960)", "—", "", value_size=18)
        self.card_v60 = _make_metric_card("v  (CIE 1960)", "—", "", value_size=18)
        self.card_u76 = _make_metric_card("u' (CIE 1976)", "—", "", value_size=18)
        self.card_v76 = _make_metric_card("v' (CIE 1976)", "—", "", value_size=18)
        for i, c in enumerate((self.card_x, self.card_y, self.card_u60,
                               self.card_v60, self.card_u76, self.card_v76)):
            coord_grid.addWidget(c, 0, i)
        root.addLayout(coord_grid)

        root.addStretch(1)

    # Public update method
    def apply(self, m: dict, calibrated: bool = False) -> None:
        f = lambda v, fmt="{:.0f}": fmt.format(v) if np.isfinite(v) else "—"
        _set_card(self.card_cct,  f(m["cct"]))
        # Illuminance and foot-candles require an absolute radiometric
        # calibration factor (W/m²/nm per ADC count). Without it the number
        # is ADC-count integrals × 683 — physically meaningless. Show "—"
        # until the user sets radiometric_calibration_factor in config.
        if calibrated and np.isfinite(m["lux"]):
            _set_card(self.card_lux, f(m["lux"], "{:.1f}"))
            _set_card(self.card_fc,  f(m["fc"],  "{:.2f}"))
        else:
            _set_card(self.card_lux, "—")
            _set_card(self.card_fc,  "—")
        _set_card(self.card_sp,   f(m["sp_ratio"], "{:.3f}"))
        _set_card(self.card_sdcm, f(m["sdcm"], "{:.2f}"))
        _set_card(self.card_peak, f(m["peak_nm"], "{:.1f}"))
        dom = m["dominant_nm"]
        if np.isfinite(dom):
            txt = (f"{abs(dom):.1f}c" if dom < 0 else f"{dom:.1f}")
        else:
            txt = "—"
        _set_card(self.card_dom, txt)
        _set_card(self.card_purity, f(m["purity_pct"], "{:.1f}"))
        _set_card(self.card_fwhm,     f(m["fwhm_nm"], "{:.1f}"))
        _set_card(self.card_centroid, f(m["centroid_nm"], "{:.1f}"))
        _set_card(self.card_central,  f(m["central_nm"],  "{:.1f}"))
        _set_card(self.card_red,   f(m["red_pct"],   "{:.1f}"))
        _set_card(self.card_green, f(m["green_pct"], "{:.1f}"))
        _set_card(self.card_blue,  f(m["blue_pct"],  "{:.1f}"))
        _set_card(self.card_x,   f(m["x"],   "{:.4f}"))
        _set_card(self.card_y,   f(m["y"],   "{:.4f}"))
        _set_card(self.card_u60, f(m["u60"], "{:.4f}"))
        _set_card(self.card_v60, f(m["v60"], "{:.4f}"))
        _set_card(self.card_u76, f(m["u76"], "{:.4f}"))
        _set_card(self.card_v76, f(m["v76"], "{:.4f}"))


# ════════════════════════════════════════════════════════════
# Sub-tab 2: CIE 1931 chromaticity
# ════════════════════════════════════════════════════════════
class _ChromaticityPage(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        root = QHBoxLayout(self)
        root.setContentsMargins(14, 14, 14, 14); root.setSpacing(14)

        # Plot
        self.plot = pg.PlotWidget()
        self.plot.setBackground(_T().BG0)
        self.plot.setAspectLocked(True)
        self.plot.setXRange(0.0, 0.8, padding=0)
        self.plot.setYRange(0.0, 0.9, padding=0)
        self.plot.showGrid(x=True, y=True, alpha=0.10)
        for axis in ("bottom", "left"):
            ax = self.plot.getAxis(axis)
            ax.setPen(pg.mkPen(QColor(_T().BORDER2), width=1))
            ax.setTextPen(pg.mkPen(QColor(_T().FG3)))
        self.plot.setLabel("bottom", "x", color=_T().FG3)
        self.plot.setLabel("left",   "y", color=_T().FG3)
        self.plot.setMouseEnabled(False, False)
        self.plot.hideButtons()

        # Colourful gamut fill
        fill_img = _build_horseshoe_fill_image(220)
        self._fill_item = pg.ImageItem(fill_img, axisOrder='row-major')
        tr = QTransform()
        tr.scale(0.8 / fill_img.shape[1], 0.9 / fill_img.shape[0])
        self._fill_item.setTransform(tr)
        self.plot.addItem(self._fill_item)

        # Horseshoe outline
        locus = cs.spectrum_locus_xy()
        self._locus_curve = self.plot.plot(
            locus[:, 0], locus[:, 1], pen=pg.mkPen(_T().FG1, width=1.5))

        # Wavelength labels along the locus
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

        # Planckian locus + iso markers
        pl = cs.planckian_locus_xy(1500.0, 20000.0, 70)
        self._planck_curve = self.plot.plot(
            pl[:, 0], pl[:, 1],
            pen=pg.mkPen(_T().WARN, width=1.6, style=Qt.PenStyle.DashLine))

        self._iso_items = []
        for T_K in (2700, 3000, 4000, 5000, 6500, 10000):
            spd = cs.planckian_spd(T_K)
            X, Y, Z = cs.spectrum_to_xyz(cs.CIE_WL_5, spd)
            ix, iy = cs.xyz_to_xy(X, Y, Z)
            dot = pg.ScatterPlotItem(
                size=6, brush=pg.mkBrush(_T().WARN),
                pen=pg.mkPen(_T().BG0, width=1.5), symbol="o")
            dot.setData([ix], [iy])
            self.plot.addItem(dot)
            lbl = pg.TextItem(anchor=(0.0, 0.5))
            lbl.setHtml(
                f'<span style="color:{_T().WARN};font-family:{FONT_MONO};'
                f'font-size:8.5pt;"> {T_K}K</span>')
            lbl.setPos(ix, iy)
            self.plot.addItem(lbl, ignoreBounds=True)
            self._iso_items.append((dot, lbl))

        # Measurement marker
        self._measure_point = pg.ScatterPlotItem(
            size=14, brush=pg.mkBrush(_T().ACCENT),
            pen=pg.mkPen(_T().BG0, width=2), symbol="o")
        self.plot.addItem(self._measure_point)
        self._measure_label = pg.TextItem(anchor=(0.0, 1.4))
        self.plot.addItem(self._measure_label, ignoreBounds=True)

        # MacAdam-style tolerance ellipse around target CCT — a simple visual hint
        self._ellipse_curve = self.plot.plot(
            pen=pg.mkPen(_T().MEASURE_A, width=1.2, style=Qt.PenStyle.DashLine))
        self._ellipse_curve.setVisible(False)

        root.addWidget(self.plot, 1)

        # Numerics panel
        side = QWidget()
        side.setFixedWidth(260)
        sv = QVBoxLayout(side)
        sv.setContentsMargins(0, 0, 0, 0); sv.setSpacing(8)
        sv.addWidget(_section_header("Chromaticity"))
        self.card_x   = _make_metric_card("x", "—", "", value_size=18)
        self.card_y   = _make_metric_card("y", "—", "", value_size=18)
        self.card_u76 = _make_metric_card("u'", "—", "", value_size=18)
        self.card_v76 = _make_metric_card("v'", "—", "", value_size=18)
        self.card_duv  = _make_metric_card("Duv", "—", "", value_size=18)
        self.card_sdcm = _make_metric_card("SDCM", "—", "", value_size=18)
        self.card_cct  = _make_metric_card("CCT", "—", "K", value_size=22)
        for c in (self.card_cct, self.card_duv, self.card_sdcm,
                  self.card_x, self.card_y, self.card_u76, self.card_v76):
            sv.addWidget(c)
        sv.addStretch(1)
        note = QLabel("Dashed ellipse: 5-SDCM MacAdam tolerance\n"
                      "around the target Planckian point.")
        note.setStyleSheet(f"color:{_T().FG4};font-size:10px;line-height:1.4;")
        note.setWordWrap(True)
        sv.addWidget(note)
        root.addWidget(side)

    def apply(self, m: dict) -> None:
        # Marker + label
        x, y = m["x"], m["y"]
        cct, duv = m["cct"], m["duv"]
        if np.isfinite(x) and np.isfinite(y):
            self._measure_point.setData([x], [y])
            cct_txt = f"{cct:.0f} K · Duv {duv:+.4f}" if np.isfinite(cct) else "—"
            self._measure_label.setHtml(
                f'<span style="color:{_T().ACCENT};font-family:{FONT_MONO};'
                f'font-size:9.5pt;font-weight:600;">  {cct_txt}</span>')
            self._measure_label.setPos(x, y)
        else:
            self._measure_point.clear()
            self._measure_label.setHtml("")

        # Ellipse — drawn around the *target* Planckian point at the test CCT
        if np.isfinite(cct):
            u0, v0 = cs.planckian_uv(cct)
            # Convert (u0,v0) → (x0,y0) using inverse of CIE 1960 transform
            # Inverse: x = 3u / (2u - 8v + 4), y = 2v / (2u - 8v + 4)
            den = 2.0 * u0 - 8.0 * v0 + 4.0
            if abs(den) > 1e-9:
                xc = 3.0 * u0 / den
                yc = 2.0 * v0 / den
                # 5 SDCM ellipse — roughly a small circle in u'v', mapped through Jacobian.
                # Simple approximation: 5 SDCM ≈ 0.0055 in u'v' → small ellipse in xy.
                # Build a parametric ellipse in u'v' then convert back.
                theta = np.linspace(0, 2 * np.pi, 80)
                u_c, v_c = cs.xy_to_uv76(xc, yc)
                r = 0.0055
                u_e = u_c + r * np.cos(theta)
                v_e = v_c + (r * 0.6) * np.sin(theta)
                # u',v' → x,y via inverse: u'=4u/(d_60), v'=9v/(d_60) so u_60=u'/1, v_60=v'/1.5
                # Direct mapping x,y from u',v':
                x_e, y_e = [], []
                for uu, vv in zip(u_e, v_e):
                    d2 = 6.0 * uu - 16.0 * vv + 12.0
                    if abs(d2) < 1e-9:
                        x_e.append(np.nan); y_e.append(np.nan); continue
                    x_e.append(9.0 * uu / d2)
                    y_e.append(4.0 * vv / d2)
                self._ellipse_curve.setData(np.array(x_e), np.array(y_e))
                self._ellipse_curve.setVisible(True)
            else:
                self._ellipse_curve.setVisible(False)
        else:
            self._ellipse_curve.setVisible(False)

        f = lambda v, fmt="{:.4f}": fmt.format(v) if np.isfinite(v) else "—"
        _set_card(self.card_x,   f(x))
        _set_card(self.card_y,   f(y))
        _set_card(self.card_u76, f(m["u76"]))
        _set_card(self.card_v76, f(m["v76"]))
        _set_card(self.card_cct,  f(cct, "{:.0f}"))
        _set_card(self.card_duv,  f(duv, "{:+.4f}"))
        _set_card(self.card_sdcm, f(m["sdcm"], "{:.2f}"))

    def apply_theme(self) -> None:
        self.plot.setBackground(_T().BG0)
        for axis in ("bottom", "left"):
            ax = self.plot.getAxis(axis)
            ax.setPen(pg.mkPen(QColor(_T().BORDER2), width=1))
            ax.setTextPen(pg.mkPen(QColor(_T().FG3)))
        self._locus_curve.setPen(pg.mkPen(_T().FG1, width=1.5))
        self._planck_curve.setPen(pg.mkPen(_T().WARN, width=1.6,
                                           style=Qt.PenStyle.DashLine))
        self._measure_point.setBrush(pg.mkBrush(_T().ACCENT))
        self._measure_point.setPen(pg.mkPen(_T().BG0, width=2))
        self._ellipse_curve.setPen(pg.mkPen(_T().MEASURE_A, width=1.2,
                                            style=Qt.PenStyle.DashLine))


# ════════════════════════════════════════════════════════════
# Sub-tab 3: Color Rendering (radar + bars + table)
# ════════════════════════════════════════════════════════════
class _TcsPatchPanel(QWidget):
    """Ref/Test colour-patch grid for the 15 CIE 13.3 TCS plus the source white.
    Top row = reference appearance, bottom row = test appearance, both
    chromatically adapted to D65 so the visible difference is the pure colour-
    rendering shift. Fed by ``update_swatches`` with the dict from
    ``color_science.cri_tcs_swatches`` (carried in measure_all['tcs_swatches'])."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._n = 16                       # 15 TCS + source
        g = QGridLayout(self)
        g.setContentsMargins(0, 0, 0, 0)
        g.setHorizontalSpacing(2); g.setVerticalSpacing(2)
        for c in range(self._n):
            g.setColumnStretch(c + 1, 1)

        def _row_label(text):
            l = QLabel(text)
            l.setStyleSheet(f"color:{_T().FG3};font-size:9.5px;")
            l.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
            return l

        g.addWidget(_row_label("Ref"), 0, 0)
        g.addWidget(_row_label("Test"), 1, 0)

        def _cell():
            f = QFrame()
            f.setMinimumHeight(20)
            f.setFrameShape(QFrame.Shape.NoFrame)
            f.setStyleSheet("background:#222;border-radius:2px;")
            return f

        self._ref_cells, self._test_cells, self._labels = [], [], []
        for c in range(self._n):
            rc, tc = _cell(), _cell()
            self._ref_cells.append(rc); self._test_cells.append(tc)
            g.addWidget(rc, 0, c + 1); g.addWidget(tc, 1, c + 1)
            lab = QLabel("")
            lab.setStyleSheet(f"color:{_T().FG3};font-family:{FONT_MONO};font-size:8px;")
            lab.setAlignment(Qt.AlignmentFlag.AlignCenter)
            self._labels.append(lab)
            g.addWidget(lab, 2, c + 1)

    def clear(self) -> None:
        for c in range(self._n):
            self._ref_cells[c].setStyleSheet("background:#222;border-radius:2px;")
            self._test_cells[c].setStyleSheet("background:#222;border-radius:2px;")
            self._labels[c].setText("")
            self._ref_cells[c].setToolTip("")

    def update_swatches(self, sw: "dict | None") -> None:
        if not sw:
            self.clear(); return
        ref = sw.get("ref", []); test = sw.get("test", [])
        labels = sw.get("labels", []); dE = sw.get("dE", [])
        n_tcs = min(len(ref), len(test), len(labels))
        for i in range(self._n):
            if i < n_tcs:
                hr, ht, name = ref[i], test[i], labels[i]
                d = dE[i] if i < len(dE) else None
                tip = f"{name}  ΔE*ab = {d:.1f}" if d is not None else name
                self._labels[i].setText(name.replace("TCS", ""))
            elif i == n_tcs:                              # source-white column
                hr = sw.get("source_ref", "#222"); ht = sw.get("source_test", "#222")
                tip = "Source white (Ref vs Test tint)"
                self._labels[i].setText("Src")
            else:
                self._ref_cells[i].setStyleSheet("background:#222;border-radius:2px;")
                self._test_cells[i].setStyleSheet("background:#222;border-radius:2px;")
                self._labels[i].setText(""); continue
            self._ref_cells[i].setStyleSheet(f"background:{hr};border-radius:2px;")
            self._test_cells[i].setStyleSheet(f"background:{ht};border-radius:2px;")
            self._ref_cells[i].setToolTip(tip); self._test_cells[i].setToolTip(tip)

    def apply_theme(self) -> None:
        for lab in self._labels:
            lab.setStyleSheet(f"color:{_T().FG3};font-family:{FONT_MONO};font-size:8px;")


class _ColorRenderingPage(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        root = QHBoxLayout(self)
        root.setContentsMargins(14, 14, 14, 14); root.setSpacing(14)

        # ── Left: radar chart ──
        self.radar = pg.PlotWidget()
        self.radar.setBackground(_T().BG0)
        self.radar.setAspectLocked(True)
        self.radar.setMouseEnabled(False, False)
        self.radar.hideButtons()
        self.radar.setXRange(-1.25, 1.25, padding=0)
        self.radar.setYRange(-1.25, 1.25, padding=0)
        for axis in ("bottom", "left"):
            self.radar.getAxis(axis).setStyle(showValues=False)
            self.radar.getAxis(axis).setPen(pg.mkPen(None))
        self.radar.showGrid(False, False)

        # Concentric rings at 20/40/60/80/100
        self._ring_curves = []
        theta_full = np.linspace(0, 2 * np.pi, 100)
        for level in (20, 40, 60, 80, 100):
            r = level / 100.0
            ring = self.radar.plot(
                r * np.cos(theta_full), r * np.sin(theta_full),
                pen=pg.mkPen(_T().BORDER1, width=1, style=Qt.PenStyle.DotLine))
            self._ring_curves.append((ring, level))
        # Reference-100 ring
        self._ring_100 = self.radar.plot(
            np.cos(theta_full), np.sin(theta_full),
            pen=pg.mkPen(_T().WARN, width=1.5, style=Qt.PenStyle.DashLine))
        # Spokes
        self._spoke_curves = []
        self._spoke_labels = []
        N = 15
        for i in range(N):
            a = np.pi / 2 - i * 2 * np.pi / N    # start from top, clockwise
            self.radar.plot([0, np.cos(a)], [0, np.sin(a)],
                            pen=pg.mkPen(_T().BORDER1, width=1))
            lbl = pg.TextItem(anchor=(0.5, 0.5))
            lbl.setHtml(
                f'<span style="color:{_T().FG2};font-family:{FONT_MONO};'
                f'font-size:9.5pt;">R{i + 1}</span>')
            lbl.setPos(1.13 * np.cos(a), 1.13 * np.sin(a))
            self.radar.addItem(lbl, ignoreBounds=True)
            self._spoke_labels.append(lbl)

        # Measurement polygon
        self._radar_fill = pg.PlotCurveItem(
            pen=pg.mkPen(_T().ACCENT, width=1.5),
            fillLevel=0, brush=pg.mkBrush(QColor(94, 224, 214, 60)))
        self.radar.addItem(self._radar_fill)
        self._radar_dots = pg.ScatterPlotItem(
            size=7, brush=pg.mkBrush(_T().ACCENT),
            pen=pg.mkPen(_T().BG0, width=1), symbol="o")
        self.radar.addItem(self._radar_dots)

        radar_label = QLabel("CRI Ra · R1…R15")
        radar_label.setObjectName("sectionHeader")
        radar_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        radar_label.setStyleSheet(f"color:{_T().FG2};font-size:11px;"
                                   f"letter-spacing:2px;padding:0 0 4px 0;")

        radar_wrap = QWidget()
        rl = QVBoxLayout(radar_wrap); rl.setContentsMargins(0, 0, 0, 0)
        rl.setSpacing(4)
        rl.addWidget(radar_label)
        rl.addWidget(self.radar, 1)
        self.card_ra_big = _make_metric_card("Ra", "—", "", value_size=38)
        self.card_ra_big.setMaximumHeight(80)
        rl.addWidget(self.card_ra_big)

        root.addWidget(radar_wrap, 1)

        # ── Right: bar chart + table ──
        right = QWidget()
        rv = QVBoxLayout(right)
        rv.setContentsMargins(0, 0, 0, 0); rv.setSpacing(6)
        rv.addWidget(_section_header("Per-sample fidelity"))

        self.bars_plot = pg.PlotWidget()
        self.bars_plot.setBackground(_T().BG0)
        self.bars_plot.setMouseEnabled(False, False)
        self.bars_plot.hideButtons()
        self.bars_plot.setXRange(-25, 105, padding=0)
        self.bars_plot.setYRange(0.5, 15.5, padding=0.02)
        # Y-axis: R1 at top
        ax_left = self.bars_plot.getAxis("left")
        ax_left.setTicks([[(15 - i, f"R{i + 1}") for i in range(15)]])
        ax_left.setPen(pg.mkPen(_T().BORDER2, width=1))
        ax_left.setTextPen(pg.mkPen(_T().FG3))
        ax_bot = self.bars_plot.getAxis("bottom")
        ax_bot.setPen(pg.mkPen(_T().BORDER2, width=1))
        ax_bot.setTextPen(pg.mkPen(_T().FG3))
        self.bars_plot.showGrid(x=True, alpha=0.10)
        # 100% reference line
        ref = pg.InfiniteLine(angle=90, pos=100,
                              pen=pg.mkPen(_T().WARN, style=Qt.PenStyle.DashLine, width=1))
        self.bars_plot.addItem(ref)
        # bars + labels
        self._bar_items: list[pg.BarGraphItem] = []
        self._bar_value_labels: list[pg.TextItem] = []
        for i in range(15):
            bar = pg.BarGraphItem(
                x0=[0], y=[15 - i], height=0.72, width=[0],
                brush=pg.mkBrush(QColor(TCS_DISPLAY_COLORS[i])),
                pen=pg.mkPen(None))
            self.bars_plot.addItem(bar)
            self._bar_items.append(bar)
            lbl = pg.TextItem(anchor=(0.0, 0.5))
            lbl.setHtml(
                f'<span style="color:{_T().FG2};font-family:{FONT_MONO};'
                f'font-size:9pt;">—</span>')
            lbl.setPos(0, 15 - i)
            self.bars_plot.addItem(lbl, ignoreBounds=True)
            self._bar_value_labels.append(lbl)
        rv.addWidget(self.bars_plot, 1)

        # R9 highlight box
        self.card_r9 = _make_metric_card("R9  (saturated red)", "—", "",
                                          value_size=20, min_width=160)
        rv.addWidget(self.card_r9)

        rv.addWidget(make_hsep())
        legend = QLabel(
            f'<span style="color:{_T().OK};">●</span> Ra ≥ 90 · excellent  '
            f'&nbsp; <span style="color:{_T().ACCENT};">●</span> 80-90 · good  '
            f'&nbsp; <span style="color:{_T().WARN};">●</span> 60-80 · fair  '
            f'&nbsp; <span style="color:{_T().DANGER};">●</span> &lt;60 · poor')
        legend.setStyleSheet(f"color:{_T().FG3};font-size:10.5px;")
        rv.addWidget(legend)

        # Ref/Test colour-patch panel (reference vs. measured appearance)
        rv.addWidget(make_hsep())
        rv.addWidget(_section_header("Reference vs. test appearance"))
        self.patch_panel = _TcsPatchPanel()
        rv.addWidget(self.patch_panel)

        root.addWidget(right, 1)

    def apply(self, m: dict) -> None:
        Ri = m["Ri"]
        Ra = m["Ra"]
        # Big Ra card
        _set_card(self.card_ra_big, f"{Ra:.1f}" if np.isfinite(Ra) else "—")
        # R9 highlight
        r9 = Ri[8] if len(Ri) > 8 else float('nan')
        _set_card(self.card_r9, f"{r9:.1f}" if np.isfinite(r9) else "—")

        # Radar polygon
        N = 15
        rs = [max(0.0, min(1.2, (Ri[i] if np.isfinite(Ri[i]) else 0) / 100.0))
              for i in range(N)]
        xs, ys = [], []
        for i, r in enumerate(rs):
            a = np.pi / 2 - i * 2 * np.pi / N
            xs.append(r * np.cos(a)); ys.append(r * np.sin(a))
        xs.append(xs[0]); ys.append(ys[0])     # close polygon
        self._radar_fill.setData(np.array(xs), np.array(ys))
        self._radar_dots.setData(xs[:-1], ys[:-1])

        # Bars
        for i, (bar, lbl) in enumerate(
                zip(self._bar_items, self._bar_value_labels)):
            r = Ri[i] if i < len(Ri) else float('nan')
            w = max(-25.0, min(100.0, r)) if np.isfinite(r) else 0.0
            bar.setOpts(x0=[0], width=[w],
                        brush=pg.mkBrush(QColor(TCS_DISPLAY_COLORS[i])))
            txt = f"{r:.1f}" if np.isfinite(r) else "—"
            lbl.setHtml(
                f'<span style="color:{_T().FG1};font-family:{FONT_MONO};'
                f'font-size:9pt;">  {txt}</span>')
            lbl.setPos(max(0.0, w), 15 - i)

        # Ref/Test colour patches
        self.patch_panel.update_swatches(m.get("tcs_swatches"))

    def apply_theme(self) -> None:
        for plot in (self.radar, self.bars_plot):
            plot.setBackground(_T().BG0)
            for axis in ("bottom", "left"):
                ax = plot.getAxis(axis)
                ax.setPen(pg.mkPen(_T().BORDER2, width=1))
                ax.setTextPen(pg.mkPen(_T().FG3))
        self._ring_100.setPen(pg.mkPen(_T().WARN, width=1.5,
                                       style=Qt.PenStyle.DashLine))
        self._radar_fill.setPen(pg.mkPen(_T().ACCENT, width=1.5))
        self._radar_dots.setBrush(pg.mkBrush(_T().ACCENT))
        self.patch_panel.apply_theme()


# ════════════════════════════════════════════════════════════
# Top-level Color tab — hosts the three pages
# ════════════════════════════════════════════════════════════
class _TM30Page(QWidget):
    """TM-30-18 + CQS: metric cards, colour-vector graphic, 99-sample fidelity
    bars and per-hue-bin chroma shift. Fed throttled by CIETab."""

    def __init__(self, parent=None):
        super().__init__(parent)
        root = QVBoxLayout(self)
        root.setContentsMargins(12, 12, 12, 12); root.setSpacing(10)

        # Cards
        cardrow = QGridLayout(); cardrow.setHorizontalSpacing(8); cardrow.setVerticalSpacing(8)
        self._cards = {}
        labels = ["TM-30 Rf", "TM-30 Rg", "CQS Qa", "CQS Qf", "CQS Qg", "CCT", "Duv"]
        for i, lab in enumerate(labels):
            cell = make_stat_cell(lab, "—")
            self._cards[lab] = cell
            cardrow.addWidget(cell, 0, i)
        cw = QWidget(); cw.setLayout(cardrow)
        root.addWidget(cw)

        graphs = QHBoxLayout(); graphs.setSpacing(12)

        # Colour-vector graphic
        self.cvg = pg.PlotWidget()
        self.cvg.setBackground(_T().BG0); self.cvg.setAspectLocked(True)
        self.cvg.setMouseEnabled(False, False); self.cvg.hideButtons()
        self.cvg.setXRange(-1.4, 1.4, padding=0); self.cvg.setYRange(-1.4, 1.4, padding=0)
        for ax in ("left", "bottom"):
            self.cvg.getAxis(ax).setStyle(showValues=False)
            self.cvg.getAxis(ax).setPen(pg.mkPen(None))
        th = np.linspace(0, 2 * np.pi, 200)
        for r, col in ((1.0, _T().FG3), (0.8, _T().BORDER2), (1.2, _T().BORDER2)):
            self.cvg.plot(r * np.cos(th), r * np.sin(th),
                          pen=pg.mkPen(QColor(col), width=1))
        self.cvg.plot([-1.3, 1.3], [0, 0], pen=pg.mkPen(QColor(_T().BORDER2), width=1))
        self.cvg.plot([0, 0], [-1.3, 1.3], pen=pg.mkPen(QColor(_T().BORDER2), width=1))
        self._cvg_test = self.cvg.plot(pen=pg.mkPen(QColor("#e0314a"), width=2))
        self._cvg_dots = pg.ScatterPlotItem(size=7, pen=pg.mkPen(None))
        self.cvg.addItem(self._cvg_dots)
        self._cvg_arrows = []
        for j in range(16):
            c = self.cvg.plot([], [], pen=pg.mkPen(QColor(cs.TM30_BIN_COLORS[j]), width=1.4))
            self._cvg_arrows.append(c)
        cvg_wrap = QWidget(); cl = QVBoxLayout(cvg_wrap); cl.setContentsMargins(0, 0, 0, 0)
        cap = QLabel("Colour vector graphic (16 hue bins)")
        cap.setStyleSheet(f"color:{_T().FG3};font-size:11px;")
        cl.addWidget(cap); cl.addWidget(self.cvg, 1)
        graphs.addWidget(cvg_wrap, 1)

        # Right column: 99-sample fidelity bars + per-bin chroma shift
        right = QWidget(); rv = QVBoxLayout(right); rv.setContentsMargins(0, 0, 0, 0); rv.setSpacing(10)
        self.samples = pg.PlotWidget()
        self.samples.setBackground(_T().BG0); self.samples.setMouseEnabled(False, False)
        self.samples.hideButtons(); self.samples.setYRange(0, 110, padding=0)
        self.samples.setLabel("left", "Rf,i", color=_T().FG3)
        cap2 = QLabel("Fidelity of all 99 colour samples")
        cap2.setStyleSheet(f"color:{_T().FG3};font-size:11px;")
        self._samples_bars = None
        rv.addWidget(cap2); rv.addWidget(self.samples, 1)

        self.bins = pg.PlotWidget()
        self.bins.setBackground(_T().BG0); self.bins.setMouseEnabled(False, False)
        self.bins.hideButtons(); self.bins.setYRange(-25, 25, padding=0)
        self.bins.setLabel("left", "Rcs,hj (%)", color=_T().FG3)
        cap3 = QLabel("Local chroma shift per hue bin  (+ = more saturated)")
        cap3.setStyleSheet(f"color:{_T().FG3};font-size:11px;")
        self._bins_bars = None
        rv.addWidget(cap3); rv.addWidget(self.bins, 1)
        graphs.addWidget(right, 1)

        root.addLayout(graphs, 1)

        self.hint = QLabel("")
        self.hint.setStyleSheet(f"color:{_T().FG3};font-size:11px;")
        self.hint.setWordWrap(True)
        root.addWidget(self.hint)
        if not cs.tm30_available():
            self.hint.setText("TM-30 / CQS needs the optional 'colour-science' "
                              "package. Install it (pip install colour-science) to "
                              "enable this tab.")

    def apply(self, tm: dict | None, cq: dict | None) -> None:
        if tm is None:
            return
        self.hint.setText("")
        nf = lambda v, d=0: "—" if (v is None or not np.isfinite(v)) else f"{v:.{d}f}"
        cq = cq or {}
        update_stat_cell(self._cards["TM-30 Rf"], nf(tm.get("Rf")))
        update_stat_cell(self._cards["TM-30 Rg"], nf(tm.get("Rg")))
        update_stat_cell(self._cards["CQS Qa"], nf(cq.get("Qa")))
        update_stat_cell(self._cards["CQS Qf"], nf(cq.get("Qf")))
        update_stat_cell(self._cards["CQS Qg"], nf(cq.get("Qg")))
        update_stat_cell(self._cards["CCT"], nf(tm.get("CCT")) + " K")
        duv = tm.get("Duv")
        update_stat_cell(self._cards["Duv"],
                         "—" if (duv is None or not np.isfinite(duv)) else f"{duv:+.4f}")

        # Colour-vector graphic (normalise each bin by its reference radius)
        ref = np.asarray(tm.get("avg_ref") or [], dtype=float)
        test = np.asarray(tm.get("avg_test") or [], dtype=float)
        if ref.size and test.size:
            rref = np.hypot(ref[:, 0], ref[:, 1]); rref[rref == 0] = 1.0
            refu = ref / rref[:, None]; testu = test / rref[:, None]
            tx = np.append(testu[:, 0], testu[0, 0]); ty = np.append(testu[:, 1], testu[0, 1])
            self._cvg_test.setData(tx, ty)
            self._cvg_dots.setData(
                testu[:, 0], testu[:, 1],
                brush=[pg.mkBrush(QColor(cs.TM30_BIN_COLORS[j % 16])) for j in range(len(testu))])
            for j in range(16):
                if j < len(refu):
                    self._cvg_arrows[j].setData([refu[j, 0], testu[j, 0]],
                                                [refu[j, 1], testu[j, 1]])
                else:
                    self._cvg_arrows[j].setData([], [])

        # 99-sample fidelity bars — coloured with each CES sample's true
        # (reference) colour when available, else a synthetic hue ramp.
        Rs = np.asarray(tm.get("Rs") or [], dtype=float)
        bins = tm.get("bins") or []
        if self._samples_bars is not None:
            self.samples.removeItem(self._samples_bars)
        if Rs.size:
            hexes = tm.get("Rs_hex")
            if hexes and len(hexes) == len(Rs):
                brushes = [pg.mkBrush(QColor(h)) for h in hexes]
            else:
                brushes = _sample_hue_brushes(len(Rs))
            self._samples_bars = pg.BarGraphItem(
                x=np.arange(len(Rs)), height=Rs, width=1.0, brushes=brushes, pen=pg.mkPen(None))
            self.samples.addItem(self._samples_bars)

        # Per-bin chroma shift
        rcs = np.asarray(tm.get("Rcshj") or [], dtype=float)
        if self._bins_bars is not None:
            self.bins.removeItem(self._bins_bars)
        if rcs.size:
            brushes = [pg.mkBrush(QColor(cs.TM30_BIN_COLORS[j % 16])) for j in range(len(rcs))]
            self._bins_bars = pg.BarGraphItem(
                x=np.arange(len(rcs)) + 1, height=rcs, width=0.8, brushes=brushes, pen=pg.mkPen(None))
            self.bins.addItem(self._bins_bars)

    def apply_theme(self) -> None:
        for w in (self.cvg, self.samples, self.bins):
            w.setBackground(_T().BG0)


class _Tm30Worker(QObject):
    """Runs the heavy TM-30-18 + CQS computation off the GUI thread. Lives in a
    QThread; `compute` is invoked via a queued signal and emits `done` back to
    the GUI thread. Receives copies of the spectrum so the GUI may overwrite its
    buffers freely."""
    done = pyqtSignal(object, object)   # (tm30 dict|None, cqs dict|None)

    def compute(self, wl, inten):
        try:
            tm = cs.tm30_metrics(wl, inten)
            cq = cs.cqs_metrics(wl, inten)
        except Exception:
            tm, cq = None, None
        self.done.emit(tm, cq)


class CIETab(QWidget):
    UPDATE_EVERY_N_FRAMES = 5
    TM30_THROTTLE_S = 1.2        # wall-clock throttle for TM-30 (matches the web)
    _tm30_request = pyqtSignal(object, object)   # (wl, inten) → worker thread

    def __init__(self, parent=None):
        super().__init__(parent)
        self._frame_count = 0
        self._latest = None          # (wavelengths, intensities) last frame
        self._calib = 0.0            # lux calibration factor in use
        self._tm30_last = 0.0        # monotonic time of last TM-30 dispatch
        self.report_meta_provider = None   # set by the dashboard; () -> dict

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0); root.setSpacing(0)

        root.addWidget(self._build_export_bar())

        self._inner_tabs = QTabWidget()
        self._inner_tabs.setDocumentMode(True)
        self.overview_page    = _OverviewPage()
        self.chrom_page       = _ChromaticityPage()
        self.rendering_page   = _ColorRenderingPage()
        self.tm30_page        = _TM30Page()
        self._inner_tabs.addTab(self.overview_page,  "Overview")
        self._inner_tabs.addTab(self.chrom_page,     "CIE 1931")
        self._inner_tabs.addTab(self.rendering_page, "Color Rendering")
        self._inner_tabs.addTab(self.tm30_page,      "TM-30 / CQS")
        self._tm30_count = 0
        # Switching to the TM-30 sub-tab should compute promptly, not after the
        # next throttle window — reset the throttle so the next frame dispatches.
        self._inner_tabs.currentChanged.connect(lambda *_: setattr(self, "_tm30_last", 0.0))
        root.addWidget(self._inner_tabs, 1)

        # TM-30 / CQS runs on a background thread so the heavy compute (~0.1 s,
        # more on a Pi) never blocks the GUI. We dispatch via a queued signal and
        # apply the result back on the GUI thread; a busy flag drops requests
        # while one is in flight so they can't pile up.
        self._tm30_busy = False
        self._tm30_thread = QThread()
        # OpenBLAS' LAPACK routines (dgesv/dgetrf_parallel, reached via
        # colour-science → numpy.linalg) need far more stack than a QThread's
        # small default (~512 KB on macOS), which overflowed the stack guard page
        # and crashed with SIGBUS "thread stack size exceeded". Give the worker a
        # main-thread-sized stack. Must be set before start().
        self._tm30_thread.setStackSize(16 * 1024 * 1024)   # 16 MB
        self._tm30_worker = _Tm30Worker()
        self._tm30_worker.moveToThread(self._tm30_thread)
        self._tm30_request.connect(self._tm30_worker.compute)   # → worker thread
        self._tm30_worker.done.connect(self._on_tm30_done)      # → GUI thread
        self._tm30_thread.start()
        app = QApplication.instance()
        if app is not None:
            app.aboutToQuit.connect(self._stop_tm30_thread)

    def _stop_tm30_thread(self) -> None:
        if self._tm30_thread.isRunning():
            self._tm30_thread.quit()
            self._tm30_thread.wait(1500)

    def _on_tm30_done(self, tm, cq) -> None:
        self._tm30_busy = False
        try:
            self.tm30_page.apply(tm, cq)
        except Exception:
            pass

    def _build_export_bar(self) -> QWidget:
        w = QWidget(); w.setObjectName("scopeOverlayBar")
        h = QHBoxLayout(w)
        h.setContentsMargins(10, 6, 10, 6); h.setSpacing(8)
        lbl = QLabel("Colour / CRI report & raw data"); lbl.setObjectName("readoutLbl")
        h.addWidget(lbl)
        h.addStretch(1)
        self.btn_report_pdf = QPushButton("⤓  PDF"); self.btn_report_pdf.setObjectName("ghostBtn")
        self.btn_report_pdf.clicked.connect(lambda: self._save_report("pdf"))
        self.btn_report_pdf.setToolTip(
            "Save a colour/CRI report (SPD, R1–R15 bars, full metrics table, "
            "white background) as PDF.")
        self.btn_report_png = QPushButton("⤓  PNG"); self.btn_report_png.setObjectName("ghostBtn")
        self.btn_report_png.clicked.connect(lambda: self._save_report("png"))
        self.btn_report_csv = QPushButton("⤓  CSV"); self.btn_report_csv.setObjectName("ghostBtn")
        self.btn_report_csv.clicked.connect(self._save_csv)
        self.btn_report_csv.setToolTip("Export all colour metrics as raw data (CSV).")
        self.lbl_export = QLabel(""); self.lbl_export.setObjectName("readoutLbl")
        for b in (self.btn_report_pdf, self.btn_report_png, self.btn_report_csv):
            h.addWidget(b)
        h.addWidget(self.lbl_export)
        return w

    def _save_report(self, fmt: str) -> None:
        if self._latest is None:
            QMessageBox.information(self, "Colour report", "No spectrum yet.")
            return
        default = f"color_report.{fmt}"
        flt = "PDF document (*.pdf)" if fmt == "pdf" else "PNG image (*.png)"
        path, _ = QFileDialog.getSaveFileName(self, "Save colour / CRI report",
                                              default, flt)
        if not path:
            return
        if not path.lower().endswith("." + fmt):
            path += "." + fmt
        wl, inten = self._latest
        meta = self.report_meta_provider() if callable(self.report_meta_provider) else None
        try:
            cs.render_color_report(path, wl, inten, fmt=fmt,
                                   lux_calibration=self._calib, meta=meta)
            self.lbl_export.setText(f"Saved {os.path.basename(path)}")
        except ImportError:
            QMessageBox.warning(self, "Colour report",
                                "matplotlib is not installed — install it to export "
                                "PDF/PNG reports, or use CSV instead.")
        except Exception as e:
            QMessageBox.warning(self, "Colour report", f"Export failed:\n{e}")

    def _save_csv(self) -> None:
        if self._latest is None:
            QMessageBox.information(self, "Colour CSV", "No spectrum yet.")
            return
        path, _ = QFileDialog.getSaveFileName(self, "Save colour metrics (CSV)",
                                              "color_metrics.csv", "CSV file (*.csv)")
        if not path:
            return
        if not path.lower().endswith(".csv"):
            path += ".csv"
        wl, inten = self._latest
        try:
            import csv
            rows = cs.color_report_rows(wl, inten, self._calib)
            with open(path, "w", newline="", encoding="utf-8") as fh:
                wr = csv.writer(fh)
                wr.writerow(["Metric", "Value"])
                wr.writerows(rows)
            self.lbl_export.setText(f"Saved {os.path.basename(path)}")
        except Exception as e:
            QMessageBox.warning(self, "Colour CSV", f"Export failed:\n{e}")

    def update_measurement(self, wavelengths_nm: np.ndarray,
                           intensities: np.ndarray) -> None:
        self._latest = (np.asarray(wavelengths_nm, dtype=float),
                        np.asarray(intensities, dtype=float))
        self._frame_count += 1

        # TM-30 / CQS — dispatch to the worker thread (never blocks the GUI).
        # Throttled by WALL-CLOCK time (like the web client), NOT by frame count:
        # at the demo's slow ~1.2 Hz a frame-count throttle meant ~16 s between
        # updates, while the web refreshed every ~1.2 s. Send COPIES so the GUI
        # may overwrite its buffers while the worker runs.
        now = time.monotonic()
        if (self._inner_tabs.currentWidget() is self.tm30_page
                and not self._tm30_busy
                and cs.tm30_available()
                and now - self._tm30_last >= self.TM30_THROTTLE_S):
            self._tm30_busy = True
            self._tm30_last = now
            self._tm30_request.emit(
                np.asarray(wavelengths_nm, dtype=float).copy(),
                np.asarray(intensities, dtype=float).copy())

        # Overview / chromaticity / rendering pages — heavier to redraw, so keep
        # the frame-count throttle (the demo's low rate is fine for these).
        if self._frame_count % self.UPDATE_EVERY_N_FRAMES != 0:
            return
        try:
            from app_config import Config
            calib = float(Config.get("radiometric_calibration_factor", 0.0))
            self._calib = calib
            m = cs.measure_all(wavelengths_nm, intensities,
                               lux_calibration=calib)
        except Exception:
            return
        self.overview_page.apply(m, calibrated=(calib > 0.0))
        self.chrom_page.apply(m)
        self.rendering_page.apply(m)

    def apply_theme(self) -> None:
        self.chrom_page.apply_theme()
        self.rendering_page.apply_theme()
        self.tm30_page.apply_theme()

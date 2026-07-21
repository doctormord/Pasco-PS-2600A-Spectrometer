"""
DashboardWindow — main GUI shell for the PS-2600A spectrometer.

Layout:
  ┌──────────────────────────────────────────────────────────────┐
  │ Title bar      [Live]  [DARK] [LIGHT] [EXIT]                 │
  ├──────────────────────────────────────────────────────────────┤
  │ Toolbar  [●][device ▾] [Scan] [Connect] [PEAKS][MEASURE]  …  │
  │                          live readout (Pixel / nm / ADC)     │
  ├──────────────────────────────────────────────────┬───────────┤
  │ Tabs:  Scope · Heatmap · Color (CCT / CRI)       │ Sidebar   │
  │                                                  │  GO       │
  │                  ── plot ──                      │  stats    │
  │                                                  │  Acq      │
  │                                                  │  Display  │
  │                                                  │  Process  │
  │                                                  │  Export   │
  ├──────────────────────────────────────────────────┴───────────┤
  │ Status bar:  Integration / Dark / Bias / Rate / Status  Chip │
  └──────────────────────────────────────────────────────────────┘
"""

from __future__ import annotations
import os
import csv
import time
from collections import deque
from datetime import datetime

import numpy as np
from scipy.signal import savgol_filter, find_peaks

from PyQt6.QtCore import Qt, QTimer, QMetaObject, Q_ARG
from PyQt6.QtCore import pyqtSlot
from PyQt6.QtGui import (
    QTransform, QColor, QFont, QPen, QBrush, QGuiApplication,
)
from PyQt6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QPushButton, QLabel, QSpinBox, QDoubleSpinBox, QMessageBox, QComboBox,
    QTabWidget, QFrame, QScrollArea, QToolTip, QFileDialog, QCheckBox, QSlider,
    QLineEdit,
)
import pyqtgraph as pg

import gui_theme as theme
from gui_theme import (
    DarkPalette, LightPalette, DARK_STYLESHEET, LIGHT_STYLESHEET, build_stylesheet,
    set_active_palette, FONT_MONO,
    ToggleSwitch, ConnDot, WavelengthFillItem,
    make_stat_cell, update_stat_cell, make_row, make_chip,
    make_hsep, make_vsep,
)
from gui_cie_tab import CIETab
from gui_filter_tab import FilterTab
from app_config import Config

from _device_pasco import (
    PASCO_WAVELENGTH_ARRAY       as _default_wavelength_array,
    PASCO_PIXEL_COUNT            as _default_pixel_count,
    PASCO_SPECTRAL_RESPONSE_GAIN as _default_response_gain,
)
from calibration_utils import build_response_gain as _build_response_gain
import calibration_profiles as calprof
# PASCO-specific constants — imported from the driver, not from core
from _device_pasco import (
    PASCO_WAVELENGTH_ARRAY       as wavelength_array,
    PASCO_PIXEL_COUNT            as PIXEL_COUNT,
    PASCO_START_INTEGRATION_US   as START_INTEGRATION_TIME_US,
    PASCO_MIN_INTEGRATION_US     as MIN_INTEGRATION_TIME_US,
    PASCO_MAX_INTEGRATION_US     as MAX_INTEGRATION_TIME_US,
    PASCO_ADC_SATURATION         as ADC_SATURATION_THRESHOLD,
    PASCO_SPECTRAL_RESPONSE_GAIN as SPECTRAL_RESPONSE_GAIN,
    PASCO_OB_CORRECTION_SLOPE    as OB_CORRECTION_SLOPE,
    PASCO_OB_CORRECTION_OFFSET   as OB_CORRECTION_OFFSET,
    PASCO_OB_TEMP_REFERENCE_ADC  as OB_TEMP_REFERENCE_ADC,
    PASCO_DARK_BIAS_ADC          as DEFAULT_DARK_BIAS_ADC,
    PASCO_DARK_RATE_ADC_PER_SEC  as DEFAULT_DARK_RATE_ADC_PER_SEC,
)
from spectrometer_core import (
    Y_AXIS_BOTTOM_MARGIN_ADC,
    HEATMAP_HISTORY_SIZE, REFERENCE_LIBRARY_FILENAME,
    DARK_MODE_OPTICAL_BLACK, DARK_MODE_PARAMETRIC,
    ensure_reference_library_exists, add_help,
)
import device_manager as dm
from fusion import SpectrumFusion, DEFAULTS as FUSION_DEFAULTS
import processing
from autosave import HeatmapAutosaveWriter
import export_paths


# Shorthand
def T():
    """Return the *current* palette (alias rebinds during theme switch)."""
    return theme.Theme


# Number of fading "afterglow" trail curves for the scope persistence overlay.
SCOPE_PERSIST_TRAILS = 6


def _load_two_column_csv(path: str):
    """Tolerant 2-column (wavelength, intensity) CSV loader for the scope CSV
    background overlay. Accepts ';' ',' tab or whitespace delimiters, optional
    header/comment (#) lines, and 3-column Pixel;λ;ADC dumps (uses the last two
    columns). Mirrors the web client's parseTwoColCsv. Returns (xs, ys) as
    ascending-sorted float ndarrays."""
    with open(path, "r", encoding="utf-8", errors="replace") as fh:
        lines = [ln.strip() for ln in fh
                 if ln.strip() and not ln.strip().startswith("#")]
    if not lines:
        raise ValueError("empty file")
    sample = lines[0]
    if ";" in sample:
        delim = ";"
    elif "\t" in sample:
        delim = "\t"
    elif "," in sample:
        delim = ","
    else:
        delim = None                       # split on any run of whitespace
    start = 1 if any(ch.isalpha() for ch in sample) else 0
    xs, ys = [], []
    for ln in lines[start:]:
        parts = ln.split() if delim is None else ln.split(delim)
        if len(parts) < 2:
            continue
        try:
            w = float(parts[-2]); v = float(parts[-1])
        except ValueError:
            continue
        xs.append(w); ys.append(v)
    if not xs:
        raise ValueError("no numeric rows found")
    x = np.asarray(xs, dtype=float)
    y = np.asarray(ys, dtype=float)
    order = np.argsort(x)                   # np.interp needs ascending x
    return x[order], y[order]


def _hm_scrub_advance(active: bool, index: int, size: int) -> int:
    """After one ring-buffer push (np.roll(1, axis=1) + insert at column 0),
    return the new 'frames behind live' index that keeps pointing at the SAME
    absolute frame. Rolling shifts every existing column from i to i+1, so
    advancing the tracked index by 1 keeps it aimed at that frame — DVR-style
    rewind: scrub back, recording continues, the frame on screen doesn't drift.
    Clamped at size-1 (the frame ages out of the buffer, we stay on the oldest
    still available rather than jumping to unrelated data). Inactive → 0 (live)."""
    if not active:
        return 0
    return min(index + 1, max(size - 1, 0))


class DashboardWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Spectrum Analyzer — PS-2600A")
        self.resize(1480, 900)

        # ── runtime state ──
        # --- DYNAMIC ARRAYS ---
        self.active_wavelengths   = _default_wavelength_array
        self.active_pixel_count   = _default_pixel_count
        self.active_response_gain = _default_response_gain
        
        self.spectrum_history = deque(maxlen=Config.get("frames_to_average", 1))
        self.current_averaged_pixels = np.zeros(self.active_pixel_count)
        # Dark-corrected spectrum BEFORE response compensation — the signal the
        # calibration wizards capture (response correction must be off when
        # *measuring* the instrument response). Updated each frame.
        self._pixels_pre_response = np.zeros(self.active_pixel_count)
        self.hardware_thread = None

        # Spectrum fusion stage (long + short → adaptive temporal stream).
        # Sits between the driver's frame callback and process_new_spectrum.
        self._fusion = SpectrumFusion(emit_callback=self._fused_emit)
        self._apply_fusion_config()

        self.is_dark_correction_enabled = Config.get("dark_correction", False)
        self.is_despeckle_enabled       = Config.get("hot_pixel_filter", False)
        self.is_spatial_smoothing_enabled = Config.get("spatial_smoothing", False)
        self.is_response_comp_enabled   = Config.get("spectral_response_compensation", False)
        self.is_auto_y_axis_enabled     = Config.get("auto_y", True)
        self.is_peak_finding_enabled    = Config.get("show_peaks", False)
        self.is_measure_mode_enabled    = Config.get("measure_mode", False)

        # Scope overlay cluster — ephemeral display state (not persisted), mirrors
        # the web client: CSV background, freeze snapshot, difference, peak-hold
        # envelope, persistence/afterglow. All overlays draw on the scope plot and
        # share its ADC y-axis.
        self.persist_n         = SCOPE_PERSIST_TRAILS
        self.overlay_diff      = False
        self.overlay_peak_hold = False
        self.overlay_persist   = False
        self.freeze_xy         = None    # (wavelengths, intensities) snapshot
        self.csvbg_xy          = None    # (wavelengths, intensities) from a file
        self.peak_hold_env     = None    # per-pixel running max (np.ndarray)
        self.persist_ring      = []      # recent live frames, newest first
        self.prev_live         = None    # previous displayed frame

        self.dark_correction_mode = Config.get("dark_mode", DARK_MODE_OPTICAL_BLACK)
        self.latest_ob_mean = 0.0
        self.latest_integration_us = START_INTEGRATION_TIME_US

        # FPS rolling window
        self._fps_history = deque(maxlen=20)
        self._last_frame_time = None

        # CIE tab needs an interpolated 380-780nm slice
        self._cie_mask = (self.active_wavelengths >= 380) & (self.active_wavelengths <= 780)

        # Heatmap setup
        self.heatmap_linear_waves = np.linspace(
            self.active_wavelengths[0], self.active_wavelengths[-1], self.active_pixel_count)
        # Rolling time-lapse buffer. Length is user-configurable (steps of 10);
        # measurement cadence can be decoupled from the frame rate for long runs.
        self.heatmap_size = max(10, (int(Config.get("heatmap_buffer_size", HEATMAP_HISTORY_SIZE)) // 10) * 10)
        self._heatmap_measure_ms = max(0, int(Config.get("heatmap_measure_interval_ms", 0)))
        self._heatmap_filled = 0                 # valid columns written so far
        self._heatmap_last_push = 0.0            # monotonic time of last logged frame
        self._current_fps = 0.0                  # for the buffer-span estimate
        self._autosave_writer = None             # HeatmapAutosaveWriter | None
        self.heatmap_buffer = np.zeros((self.active_pixel_count, self.heatmap_size))
        # Parallel per-column timestamp buffer (monotonic seconds), rolled the
        # same way as heatmap_buffer, for the "T-x.xs ago" scrub readout.
        self.heatmap_times = np.full(self.heatmap_size, np.nan)
        # Rewind/scrub state. index is "frames behind live" (0 = live/newest).
        # While active, the index is advanced by 1 on every push (see
        # _hm_scrub_advance) so it keeps pointing at the SAME absolute frame as
        # the ring buffer keeps rolling underneath — a DVR-style rewind rather
        # than a fixed slot that would silently start showing different data.
        self._hm_scrub_active = False
        self._hm_scrub_index = 0
        # "Peek": pinned at frame 0 (= newest) while otherwise idle/live — lets
        # you keep the current spectrum visible in this panel without leaving
        # live mode. Toggled by clicking Live while already live; any real
        # scrub (dragging the slider, or explicitly going live) clears it.
        self._hm_peek = False

        # Initialise device registry (discovers available backends)
        dm.init_registry()

        # Reference library
        self.reference_library: dict[str, np.ndarray] = {}
        ensure_reference_library_exists()
        self._load_reference_library()

        # Build UI
        self._build_ui()
        self.refresh_device_list()

        # Apply initial theme
        self.apply_theme(Config.get("theme", "dark"))

    # ════════════════════════════════════════════════════════════
    # Reference library loader (unchanged from original)
    # ════════════════════════════════════════════════════════════
    def _load_reference_library(self) -> None:
        if not os.path.exists(REFERENCE_LIBRARY_FILENAME):
            return
        try:
            with open(REFERENCE_LIBRARY_FILENAME, "r") as fp:
                reader = csv.reader(fp, delimiter=";")
                headers = next(reader)
                csv_waves = []
                csv_columns = {n: [] for n in headers[1:]}
                for row in reader:
                    if not row or len(row) < 2:
                        continue
                    try:
                        csv_waves.append(float(row[0]))
                        for j, name in enumerate(headers[1:]):
                            csv_columns[name].append(
                                float(row[j + 1]) if j + 1 < len(row) else 0.0)
                    except ValueError:
                        continue
            for name in headers[1:]:
                self.reference_library[name] = np.interp(
                    self.active_wavelengths, csv_waves, csv_columns[name],
                    left=0.0, right=0.0)
        except Exception as e:
            print(f"[ref-lib] error: {e}")

    # ════════════════════════════════════════════════════════════
    # UI ASSEMBLY
    # ════════════════════════════════════════════════════════════
    def _build_ui(self) -> None:
        central = QWidget()
        central.setObjectName("central")
        self.setCentralWidget(central)
        root = QVBoxLayout(central)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        root.addWidget(self._build_titlebar())
        root.addWidget(self._build_toolbar())

        body = QWidget()
        body.setObjectName("central")
        body_h = QHBoxLayout(body)
        body_h.setContentsMargins(0, 0, 0, 0)
        body_h.setSpacing(0)
        body_h.addWidget(self._build_tabs(), 1)
        body_h.addWidget(self._build_sidebar())
        root.addWidget(body, 1)

        root.addWidget(self._build_statusbar())

        # Apply initial enabled/disabled state
        self.control_widget.setEnabled(False)
        self.header_controls.setEnabled(False)
        self._install_tooltips()
        self._sync_peak_measure_buttons()

    # ──────────────── TITLE BAR ────────────────
    def _build_titlebar(self) -> QWidget:
        w = QWidget()
        w.setObjectName("titlebar")
        w.setFixedHeight(48)
        h = QHBoxLayout(w)
        h.setContentsMargins(16, 8, 12, 8)
        h.setSpacing(8)

        title = QLabel(
            f'<span style="color:{T().FG1};font-weight:600;">'
            f'Spectrum Analyzer</span>'
            f'<span style="color:{T().FG3};">  ·  PS‑2600A  ·  Lab Dashboard</span>')
        title.setObjectName("titleText")
        title.setTextFormat(Qt.TextFormat.RichText)
        h.addWidget(title)
        h.addStretch(1)

        # Live pill (matches DARK/LIGHT/EXIT visually)
        self.live_pill = QLabel("● Idle")
        self.live_pill.setObjectName("livePillIdle")
        self.live_pill.setFixedWidth(82)
        h.addWidget(self.live_pill)

        # Theme segmented toggle
        self.button_theme_dark  = QPushButton("DARK")
        self.button_theme_light = QPushButton("LIGHT")
        self.button_theme_dark.setObjectName("themeSegOn")
        self.button_theme_light.setObjectName("themeSegOff")
        self.button_theme_dark.clicked.connect(lambda: self.apply_theme("dark"))
        self.button_theme_light.clicked.connect(lambda: self.apply_theme("light"))
        h.addSpacing(6)
        h.addWidget(self.button_theme_dark)
        h.addWidget(self.button_theme_light)

        # Exit
        self.button_exit = QPushButton("EXIT")
        self.button_exit.setObjectName("exitBtn")
        self.button_exit.clicked.connect(self.close)
        h.addSpacing(6)
        h.addWidget(self.button_exit)
        return w

    def _restyle_live_pill(self, connected: bool, fps: float = 0.0) -> None:
        if connected:
            self.live_pill.setObjectName("livePillOk")
            self.live_pill.setText(f"● {fps:.1f} Hz" if fps > 0 else "● Live")
        else:
            self.live_pill.setObjectName("livePillIdle")
            self.live_pill.setText("● Idle")
        self.live_pill.style().unpolish(self.live_pill)
        self.live_pill.style().polish(self.live_pill)

    # ──────────────── TOOLBAR ────────────────
    def _build_toolbar(self) -> QWidget:
        w = QWidget()
        w.setObjectName("toolbar")
        w.setFixedHeight(60)
        h = QHBoxLayout(w)
        h.setContentsMargins(14, 0, 14, 0)
        h.setSpacing(10)

        self.conn_dot = ConnDot()
        h.addWidget(self.conn_dot)

        # Backend (device type) selector — populated from device_manager.REGISTRY
        self.combo_backend = QComboBox()
        self.combo_backend.setObjectName("deviceSelect")
        self.combo_backend.setMinimumWidth(180)
        self.combo_backend.setMinimumHeight(32)
        for name in dm.list_backends():
            self.combo_backend.addItem(name)
        self.combo_backend.currentTextChanged.connect(self._on_backend_changed)
        h.addWidget(self.combo_backend)

        self.combo_devices = QComboBox()
        self.combo_devices.setObjectName("deviceSelect")
        self.combo_devices.setMinimumWidth(280)
        self.combo_devices.setMinimumHeight(32)
        h.addWidget(self.combo_devices)

        self.button_scan = QPushButton("Scan USB")
        self.button_scan.setObjectName("ghostBtn")
        self.button_scan.clicked.connect(self.refresh_device_list)
        h.addWidget(self.button_scan)

        self.button_connect = QPushButton("Connect")
        self.button_connect.setObjectName("primaryBtn")
        self.button_connect.setMinimumWidth(110)
        self.button_connect.clicked.connect(self.toggle_connection)
        h.addWidget(self.button_connect)

        h.addSpacing(10)

        # Quick-access toggle buttons (moved from sidebar)
        self.btn_peaks_toolbar = QPushButton("● PEAKS")
        self.btn_peaks_toolbar.setCheckable(True)
        self.btn_peaks_toolbar.setChecked(self.is_peak_finding_enabled)
        self.btn_peaks_toolbar.clicked.connect(self._on_toolbar_peaks_toggled)

        self.btn_measure_toolbar = QPushButton("⇿ MEASURE")
        self.btn_measure_toolbar.setCheckable(True)
        self.btn_measure_toolbar.setChecked(self.is_measure_mode_enabled)
        self.btn_measure_toolbar.clicked.connect(self._on_toolbar_measure_toggled)

        h.addWidget(self.btn_peaks_toolbar)
        h.addWidget(self.btn_measure_toolbar)

        h.addStretch(1)

        # Live readout. Fixed min widths sized for the worst case (INTENSITY up
        # to 5 digits + "ADC") so the row doesn't shift as values change digit
        # count — e.g. 0 -> 20000 used to widen the box and push WAVELENGTH left.
        self.readout_pix = self._make_readout("PIXEL",      "—", min_w=64)
        self.readout_wl  = self._make_readout("WAVELENGTH", "—", unit="nm", accent=True, min_w=88)
        self.readout_int = self._make_readout("INTENSITY",  "—", unit="ADC", min_w=92)
        h.addWidget(self.readout_pix); h.addSpacing(18)
        h.addWidget(self.readout_wl);  h.addSpacing(18)
        h.addWidget(self.readout_int)
        return w

    def _make_readout(self, label, value, unit="", accent=False, min_w=76) -> QWidget:
        w = QWidget()
        w.setMinimumWidth(min_w)
        v = QVBoxLayout(w)
        v.setContentsMargins(0, 0, 0, 0); v.setSpacing(2)
        lbl = QLabel(label); lbl.setObjectName("readoutLbl")
        v.addWidget(lbl)
        val = QLabel()
        val.setObjectName("readoutValAccent" if accent else "readoutVal")
        val.setTextFormat(Qt.TextFormat.RichText)
        unit_html = (f' <span style="color:{T().FG3};font-family:{FONT_MONO};'
                     f'font-size:10px;"> {unit}</span>') if unit else ""
        val.setText(f"{value}{unit_html}")
        v.addWidget(val)
        w._val = val
        w._unit_html = unit_html
        return w

    def _set_readout(self, w, value):
        w._val.setText(f"{value}{w._unit_html}")

    def _sync_peak_measure_buttons(self) -> None:
        """Update the QPushButton objectName so the QSS picks the right colour."""
        self.btn_peaks_toolbar.setObjectName(
            "toolToggleOn" if self.is_peak_finding_enabled else "toolToggleOff")
        self.btn_measure_toolbar.setObjectName(
            "toolToggleOn" if self.is_measure_mode_enabled else "toolToggleOff")
        for b in (self.btn_peaks_toolbar, self.btn_measure_toolbar):
            b.style().unpolish(b); b.style().polish(b)

    # ──────────────── TABS ────────────────
    def _build_tabs(self) -> QWidget:
        outer = QWidget(); outer.setObjectName("central")
        ol = QVBoxLayout(outer)
        ol.setContentsMargins(0, 0, 0, 0); ol.setSpacing(0)

        self.tabs = QTabWidget()
        self.tabs.setDocumentMode(True)

        # ── Scope plot ──
        pg.setConfigOptions(antialias=True)
        self.plot_canvas = pg.PlotWidget()
        self._style_plot(self.plot_canvas, "Wavelength", "nm", "Intensity", "ADC")
        self.plot_canvas.setXRange(
            Config.get("x_min_nm", 380), Config.get("x_max_nm", 1050), padding=0)
        self.plot_canvas.setYRange(
            Y_AXIS_BOTTOM_MARGIN_ADC,
            Config.get("y_max_adc", 4000),
            padding=0)

        # Gradient fill UNDER the curve
        self.spectrum_fill = WavelengthFillItem()
        self.plot_canvas.addItem(self.spectrum_fill)

        # Spectrum curve
        self.spectrum_curve = pg.PlotCurveItem(
            pen=pg.mkPen(QColor(T().FG1), width=1.4))
        self.plot_canvas.addItem(self.spectrum_curve)

        # Reference overlay
        self.reference_curve = self.plot_canvas.plot(
            pen=pg.mkPen(QColor(T().WARN), width=1.5, style=Qt.PenStyle.DashLine))
        self.reference_curve.setVisible(False)

        # ── Out-of-spec shading ────────────────────────────────────────────
        # Two translucent-red bands mark where the device still returns data but
        # the manufacturer no longer guarantees it (left of spec-min, right of
        # spec-max). Filled to the plot edges (±1e9) and pinned behind the data;
        # a thin brighter line marks the exact spec boundary. Positioned by
        # _update_spec_overlay() from the connected device's spec_range_nm.
        _spec_brush = pg.mkBrush(230, 60, 60, 10)
        _spec_pen   = pg.mkPen(230, 60, 60, 80, width=1)
        self.spec_region_lo = pg.LinearRegionItem(
            values=(-1e9, -1e9), movable=False, brush=_spec_brush,
            pen=pg.mkPen(None))
        self.spec_region_hi = pg.LinearRegionItem(
            values=(1e9, 1e9), movable=False, brush=_spec_brush,
            pen=pg.mkPen(None))
        # Boundary lines at the spec edges.
        self.spec_line_lo = pg.InfiniteLine(angle=90, movable=False, pen=_spec_pen)
        self.spec_line_hi = pg.InfiniteLine(angle=90, movable=False, pen=_spec_pen)
        for _it in (self.spec_region_lo, self.spec_region_hi,
                    self.spec_line_lo, self.spec_line_hi):
            _it.setZValue(-100)          # behind spectrum, fill, overlays
            _it.setVisible(False)
            self.plot_canvas.addItem(_it, ignoreBounds=True)
        # Keep the bands reaching the plot edges as the user zooms/pans.
        self.plot_canvas.getViewBox().sigXRangeChanged.connect(
            lambda *a: self._update_spec_overlay())

        # ── Overlay cluster curves (CSV bg / freeze / diff / peak-hold /
        # persistence). All start hidden; driven per frame in _redraw_overlays().
        # Persistence trails sit BEHIND the live curve (negative Z), the rest on
        # top like the reference overlay.
        self.persist_curves = []
        _ac = QColor(T().ACCENT)
        for k in range(self.persist_n):
            a = int(150 * (1 - k / self.persist_n))      # fading alpha 150 → ~25
            c = pg.PlotCurveItem(
                pen=pg.mkPen(QColor(_ac.red(), _ac.green(), _ac.blue(), a), width=1))
            c.setZValue(-10 - k)
            c.setVisible(False)
            self.plot_canvas.addItem(c)
            self.persist_curves.append(c)

        self.csvbg_curve = self.plot_canvas.plot(
            pen=pg.mkPen(QColor("#38bdf8"), width=1.5, style=Qt.PenStyle.DashLine))
        self.csvbg_curve.setVisible(False)
        self.freeze_curve = self.plot_canvas.plot(
            pen=pg.mkPen(QColor("#9aa0a6"), width=1.5))
        self.freeze_curve.setVisible(False)
        self.diff_curve = self.plot_canvas.plot(
            pen=pg.mkPen(QColor("#e879f9"), width=1.5))
        self.diff_curve.setVisible(False)
        self.env_curve = self.plot_canvas.plot(
            pen=pg.mkPen(QColor("#facc15"), width=1))
        self.env_curve.setVisible(False)

        # Peaks
        self.scatter_peaks = pg.ScatterPlotItem(
            size=10, brush=pg.mkBrush(T().ACCENT),
            pen=pg.mkPen(T().BG0, width=2), symbol="o")
        self.plot_canvas.addItem(self.scatter_peaks)
        self.peak_labels: list[pg.TextItem] = []

        # Measure overlay
        self.measure_label = pg.TextItem(anchor=(0.5, 0.0))
        self.measure_label.setVisible(False)
        self.plot_canvas.addItem(self.measure_label, ignoreBounds=True)

        cursor_pen = pg.mkPen(QColor(T().ACCENT), style=Qt.PenStyle.DashLine, width=1)
        self.cursor_vline = pg.InfiniteLine(angle=90, movable=False, pen=cursor_pen)
        self.cursor_hline = pg.InfiniteLine(angle=0,  movable=False, pen=cursor_pen)
        self.plot_canvas.addItem(self.cursor_vline, ignoreBounds=True)
        self.plot_canvas.addItem(self.cursor_hline, ignoreBounds=True)

        self.measure_line_a = pg.InfiniteLine(
            angle=90, movable=True,
            pen=pg.mkPen(T().MEASURE_A, width=2, style=Qt.PenStyle.DashLine))
        self.measure_line_b = pg.InfiniteLine(
            angle=90, movable=True,
            pen=pg.mkPen(T().MEASURE_B, width=2, style=Qt.PenStyle.DashLine))
        self.measure_line_a.setPos(Config.get("measure_a_nm", 500.0))
        self.measure_line_b.setPos(Config.get("measure_b_nm", 600.0))
        self.measure_line_a.setVisible(self.is_measure_mode_enabled)
        self.measure_line_b.setVisible(self.is_measure_mode_enabled)
        self.plot_canvas.addItem(self.measure_line_a, ignoreBounds=True)
        self.plot_canvas.addItem(self.measure_line_b, ignoreBounds=True)
        self.measure_line_a.sigPositionChanged.connect(self.update_measurement_display)
        self.measure_line_b.sigPositionChanged.connect(self.update_measurement_display)

        self.mouse_proxy = pg.SignalProxy(
            self.plot_canvas.scene().sigMouseMoved,
            rateLimit=30, slot=self.handle_mouse_movement)

        # Wrap the scope plot with an overlay toolbar above it.
        scope_tab = QWidget()
        scope_v = QVBoxLayout(scope_tab)
        scope_v.setContentsMargins(0, 0, 0, 0)
        scope_v.setSpacing(0)
        scope_v.addWidget(self._build_scope_overlay_bar())
        scope_v.addWidget(self.plot_canvas, 1)
        self.tabs.addTab(scope_tab, "Scope")

        # ── Heatmap ──
        self.heatmap_canvas = pg.PlotWidget()
        self._style_plot(self.heatmap_canvas, "Wavelength", "nm", "Frame history", "")
        self.heatmap_canvas.setXRange(
            Config.get("x_min_nm", 380), Config.get("x_max_nm", 1050), padding=0)
        self.heatmap_canvas.setYRange(0, self.heatmap_size, padding=0)

        self.image_item = pg.ImageItem()
        self.image_item.setLookupTable(pg.colormap.get("inferno").getLookupTable())
        self.heatmap_canvas.addItem(self.image_item)
        self._rebuild_image_transform()

        hm_pen = pg.mkPen(QColor(T().ACCENT), style=Qt.PenStyle.DashLine, width=1)
        self.heatmap_cursor_vline = pg.InfiniteLine(angle=90, movable=False, pen=hm_pen)
        self.heatmap_cursor_hline = pg.InfiniteLine(angle=0,  movable=False, pen=hm_pen)
        self.heatmap_canvas.addItem(self.heatmap_cursor_vline, ignoreBounds=True)
        self.heatmap_canvas.addItem(self.heatmap_cursor_hline, ignoreBounds=True)
        self.heatmap_mouse_proxy = pg.SignalProxy(
            self.heatmap_canvas.scene().sigMouseMoved,
            rateLimit=30, slot=self.handle_heatmap_mouse_movement)

        # Controls row — one line, same look as the scope overlay bar:
        # Clear far left, buffer/rate/span in the middle, autosave far right.
        self.heatmap_tab = QWidget()
        _hmv = QVBoxLayout(self.heatmap_tab)
        _hmv.setContentsMargins(0, 0, 0, 0); _hmv.setSpacing(0)

        _bar_w = QWidget(); _bar_w.setObjectName("scopeOverlayBar")
        _bar = QHBoxLayout(_bar_w)
        _bar.setContentsMargins(10, 6, 10, 6); _bar.setSpacing(8)

        self.btn_hm_clear = QPushButton("⟲  Clear")
        self.btn_hm_clear.setObjectName("ghostBtn")
        self.btn_hm_clear.setToolTip("Empty the time-lapse buffer and start over.")
        self.btn_hm_clear.clicked.connect(self.clear_heatmap)
        _bar.addWidget(self.btn_hm_clear)
        _bar.addWidget(make_vsep())

        _bar.addWidget(QLabel("Buffer"))
        self.spin_hm_size = self._spin_int(self.heatmap_size, 10, 100000, 10, 80)
        self.spin_hm_size.valueChanged.connect(self.set_heatmap_size)
        _bar.addWidget(self.spin_hm_size)
        _bar.addWidget(QLabel("frames"))

        _bar.addSpacing(6)
        _bar.addWidget(QLabel("Log every"))
        self.spin_hm_rate = self._spin_int(self._heatmap_measure_ms, 0, 3_600_000, 10, 80)
        self.spin_hm_rate.setToolTip("0 = every frame. Otherwise log one frame per "
                                     "N ms (decouples the time-lapse from the frame "
                                     "rate for long runs). The live scope is unaffected.")
        self.spin_hm_rate.valueChanged.connect(self._on_heatmap_rate)
        _bar.addWidget(self.spin_hm_rate)
        _bar.addWidget(QLabel("ms"))

        self.lbl_hm_span = QLabel("Span —")
        self.lbl_hm_span.setObjectName("readoutLbl")
        self.lbl_hm_span.setToolTip("Approximate time the buffer spans = frames × "
                                    "the effective interval (the larger of 'Log "
                                    "every' and the current frame period).")
        _bar.addWidget(self.lbl_hm_span)

        _bar.addStretch(1)

        self.chk_hm_autosave = QCheckBox("Autosave to disk")
        self.chk_hm_autosave.setToolTip(
            "Stream every logged frame to a CSV file as it's captured (survives "
            "a crash — flushed every few frames, fsynced periodically). "
            "Independent of the buffer above, so a long run is captured in full "
            "even though only the last N frames are shown on screen.")
        self.chk_hm_autosave.setChecked(bool(Config.get("heatmap_autosave_enabled", False)))
        self.chk_hm_autosave.toggled.connect(self._on_autosave_toggled)
        _bar.addWidget(self.chk_hm_autosave)

        self.lbl_hm_autosave = QLabel("")
        self.lbl_hm_autosave.setObjectName("readoutLbl")
        _bar.addWidget(self.lbl_hm_autosave)

        _hmv.addWidget(_bar_w)

        # ── Rewind / scrub bar ──
        _scrub_w = QWidget(); _scrub_w.setObjectName("scopeOverlayBar")
        _sbar = QHBoxLayout(_scrub_w)
        _sbar.setContentsMargins(10, 6, 10, 6); _sbar.setSpacing(8)

        self.btn_hm_live = QPushButton("●  Live")
        self.btn_hm_live.setToolTip(
            "Click while live to pin the current spectrum here without leaving "
            "live mode (click again to unpin). While scrubbing history, click "
            "to return to live.")
        self.btn_hm_live.clicked.connect(self._hm_on_live_clicked)
        _sbar.addWidget(self.btn_hm_live)

        self.btn_hm_export_frame = QPushButton("Export frame")
        self.btn_hm_export_frame.setToolTip(
            "Save just the currently viewed frame as a CSV (wavelength, "
            "intensity) — instead of exporting the whole buffer. Available "
            "while paused/peeking at a specific frame.")
        self.btn_hm_export_frame.clicked.connect(self._hm_export_frame_csv)
        self.btn_hm_export_frame.setEnabled(False)
        _sbar.addWidget(self.btn_hm_export_frame)
        self._hm_style_export_button(False)

        self.slider_hm_scrub = QSlider(Qt.Orientation.Horizontal)
        self.slider_hm_scrub.setRange(0, 0)
        self.slider_hm_scrub.setValue(0)
        self.slider_hm_scrub.setToolTip(
            "Rewind through the time-lapse buffer. Recording continues in the "
            "background while you look at a past frame — drag back to 'Live' "
            "(or click the button) to resume following in real time.")
        self.slider_hm_scrub.valueChanged.connect(self._hm_on_scrub)
        self.slider_hm_scrub.sliderReleased.connect(
            lambda: self.slider_hm_scrub.setMaximum(max(0, self._heatmap_filled - 1)))
        _sbar.addWidget(self.slider_hm_scrub, 1)

        self.lbl_hm_scrub = QLabel("● LIVE")
        self.lbl_hm_scrub.setObjectName("readoutLbl")
        # Fixed width reserved for the longest status string so the slider's
        # right edge stays put (no "jumping" as the frame text changes length).
        self.lbl_hm_scrub.setFixedWidth(180)
        _sbar.addWidget(self.lbl_hm_scrub)
        _hmv.addWidget(_scrub_w)
        self._hm_style_live_button("live")

        # Spectrum of the scrubbed frame — only shown while rewound; the live
        # scope already covers the "Live" case, so this stays hidden then.
        self.plot_hm_scrub = pg.PlotWidget()
        self._style_plot(self.plot_hm_scrub, "Wavelength", "nm", "Intensity", "ADC")
        self.plot_hm_scrub.setFixedHeight(160)
        self.curve_hm_scrub = self.plot_hm_scrub.plot(
            pen=pg.mkPen(QColor(T().ACCENT), width=1.5))
        # Same wavelength axis as the heatmap image below (setXLink keeps pan/
        # zoom in lockstep).
        self.plot_hm_scrub.setXLink(self.heatmap_canvas)
        # Keep the bottom axis's tick marks + grid (so this plot still shows
        # gridlines at the round wavelengths, e.g. 400/500/600) but hide its
        # tick NUMBERS — the heatmap's axis below already labels them, and once
        # both plots' left-axis gutters are pinned to the same width (below),
        # the numbers would just duplicate. (showAxis(False) would have hidden
        # the grid too, which was the actual missing-gridlines bug — this way
        # only the label text goes, not the ticks.)
        bottom_axis = self.plot_hm_scrub.getPlotItem().getAxis("bottom")
        bottom_axis.setStyle(showValues=False)
        bottom_axis.setHeight(6)
        # Pin BOTH plots' left-axis gutter to the same fixed pixel width. Without
        # this, pyqtgraph sizes each axis to its own tick-label text (5-digit
        # "30000" above vs 3-digit "900" below), which shifts the two viewboxes'
        # left edges relative to each other by those few pixels — the "1px
        # Versatz" between the crosshair up top and the boundary below, and the
        # left border not lining up either. A shared fixed width makes the two
        # plotted areas start at the exact same x, so pan/zoom stays pixel-exact.
        for _plot in (self.plot_hm_scrub, self.heatmap_canvas):
            _plot.getPlotItem().getAxis("left").setWidth(58)
        scrub_pen = pg.mkPen(QColor(T().ACCENT), style=Qt.PenStyle.DashLine, width=1)
        self.scrub_cursor_vline = pg.InfiniteLine(angle=90, movable=False, pen=scrub_pen)
        self.plot_hm_scrub.addItem(self.scrub_cursor_vline, ignoreBounds=True)
        self.scrub_mouse_proxy = pg.SignalProxy(
            self.plot_hm_scrub.scene().sigMouseMoved,
            rateLimit=30, slot=self.handle_scrub_mouse_movement)
        self.plot_hm_scrub.setVisible(False)
        _hmv.addWidget(self.plot_hm_scrub)

        _hmv.addWidget(self.heatmap_canvas, 1)
        self.tabs.addTab(self.heatmap_tab, "Time-Lapse Heatmap")

        # ── Color tab ──
        self.cie_tab = CIETab()
        self.tabs.addTab(self.cie_tab, "Color · CCT / CRI")

        # ── Filter characterization tab ──
        self.filter_tab = FilterTab()
        self.tabs.addTab(self.filter_tab, "Filter")

        # Let both export tabs stamp reports with the live acquisition parameters.
        self.cie_tab.report_meta_provider = self.report_meta
        self.filter_tab.report_meta_provider = self.report_meta

        ol.addWidget(self.tabs, 1)
        return outer

    def _style_plot(self, plot, xlabel, x_units, ylabel, y_units) -> None:
        plot.setBackground(T().BG0)
        plot.showGrid(x=True, y=True, alpha=0.10)
        for axis_name in ("bottom", "left"):
            ax = plot.getAxis(axis_name)
            ax.setPen(pg.mkPen(QColor(T().BORDER2), width=1))
            ax.setTextPen(pg.mkPen(QColor(T().FG3)))
            font = QFont("JetBrains Mono"); font.setPointSize(9)
            ax.setTickFont(font)
            ax.enableAutoSIPrefix(False)
        plot.setLabel("bottom", xlabel, units=x_units, color=T().FG3, **{"font-size":"10px"})
        plot.setLabel("left",   ylabel, units=y_units, color=T().FG3, **{"font-size":"10px"})

    def _build_scope_overlay_bar(self) -> QWidget:
        """Toolbar above the scope plot: freeze / diff / peak-hold / persist and
        a CSV background loader. Mirrors the web client's scope overlay bar."""
        w = QWidget(); w.setObjectName("scopeOverlayBar")
        h = QHBoxLayout(w)
        h.setContentsMargins(10, 6, 10, 6); h.setSpacing(8)

        self.btn_freeze = QPushButton("❄  Freeze")
        self.btn_freeze.setCheckable(True)
        self.btn_freeze.clicked.connect(self.toggle_freeze)
        self.btn_freeze.setToolTip(
            "Freeze the current trace as a grey snapshot (absolute ADC). "
            "Click again to clear.")

        self.btn_diff = QPushButton("Δ  Diff")
        self.btn_diff.setCheckable(True)
        self.btn_diff.clicked.connect(self.toggle_diff)
        self.btn_diff.setToolTip(
            "Show live − frozen snapshot (magenta, signed ADC). "
            "Freeze a reference first.")

        self.btn_peak_hold = QPushButton("⎍  Peak-hold")
        self.btn_peak_hold.setCheckable(True)
        self.btn_peak_hold.clicked.connect(self.toggle_peak_hold)
        self.btn_peak_hold.setToolTip(
            "Peak-hold envelope (yellow): per-pixel running maximum, like an "
            "equalizer peak meter. Toggle to re-arm.")

        self.btn_persist = QPushButton("≈  Persist")
        self.btn_persist.setCheckable(True)
        self.btn_persist.clicked.connect(self.toggle_persist)
        self.btn_persist.setToolTip(
            "Persistence / afterglow: fading echoes of the last few frames, "
            "oscilloscope-style.")

        self.btn_csvbg = QPushButton("CSV bg…")
        self.btn_csvbg.setObjectName("ghostBtn")
        self.btn_csvbg.clicked.connect(self.load_csv_background)
        self.btn_csvbg.setToolTip(
            "Load a 2-column (wavelength, intensity) CSV as a cyan background "
            "reference, normalised to the current Y max.")

        self.btn_csvbg_clear = QPushButton("✕")
        self.btn_csvbg_clear.setObjectName("ghostBtn")
        self.btn_csvbg_clear.setFixedWidth(34)
        self.btn_csvbg_clear.clicked.connect(self.clear_csv_background)
        self.btn_csvbg_clear.setToolTip("Clear the CSV background reference")
        self.btn_csvbg_clear.setVisible(False)

        self.lbl_csvbg = QLabel(""); self.lbl_csvbg.setObjectName("readoutLbl")

        for b in (self.btn_freeze, self.btn_diff, self.btn_peak_hold, self.btn_persist):
            h.addWidget(b)
        h.addWidget(make_vsep())
        h.addWidget(self.btn_csvbg)
        h.addWidget(self.btn_csvbg_clear)
        h.addWidget(self.lbl_csvbg)
        h.addStretch(1)
        self._sync_overlay_buttons()
        return w

    # ──────────────── SIDEBAR ────────────────
    def _build_sidebar(self) -> QWidget:
        outer = QWidget()
        outer.setObjectName("sidebar")
        outer.setFixedWidth(360)
        ol = QVBoxLayout(outer)
        ol.setContentsMargins(0, 0, 0, 0); ol.setSpacing(0)

        # ── Fixed header (does NOT scroll) ──
        header = QWidget(); header.setObjectName("sidebarInner")
        hl = QVBoxLayout(header)
        hl.setContentsMargins(14, 14, 14, 8); hl.setSpacing(8)

        self.button_pause = QPushButton("▶  START")
        self.button_pause.setObjectName("goBtnPaused")
        self.button_pause.setEnabled(False)          # disabled until connected
        self.button_pause.clicked.connect(self.toggle_measurement_pause)
        hl.addWidget(self.button_pause)

        # Export row — takes the place of the old stat chips (Frames/s moves to
        # the status bar; Exposure/Dark are already there).
        self.button_save_csv  = self._make_save_button("Save CSV", "csv")
        self.button_save_csv.clicked.connect(self.export_data_csv)
        self.button_save_png  = self._make_save_button("Save PNG", "png")
        self.button_save_png.clicked.connect(self.export_plot_png)
        self.button_copy_clip = self._make_save_button("Copy", "copy")
        self.button_copy_clip.setToolTip("Copy the current spectrum to the clipboard")
        self.button_copy_clip.clicked.connect(self.copy_spectrum_to_clipboard)
        save_row = QHBoxLayout(); save_row.setSpacing(6)
        for b in (self.button_save_csv, self.button_save_png, self.button_copy_clip):
            save_row.addWidget(b)
        hl.addLayout(save_row)

        # Fixed acquisition controls (stay visible while everything else scrolls).
        self.header_controls = QWidget(); self.header_controls.setObjectName("sidebarInner")
        self._fixed_acq = QVBoxLayout(self.header_controls)
        self._fixed_acq.setContentsMargins(0, 4, 0, 0); self._fixed_acq.setSpacing(2)
        hl.addWidget(self.header_controls)

        ol.addWidget(header)
        ol.addWidget(make_hsep())

        # ── Scrolling controls ──
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        # Always reserve the scrollbar's width (even when content fits) so
        # opening a section that needs scrolling doesn't shift every row left —
        # the width is constant whether or not the bar is actually needed.
        scroll.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOn)
        ol.addWidget(scroll, 1)

        self.control_widget = QWidget()
        self.control_widget.setObjectName("sidebarInner")
        scroll.setWidget(self.control_widget)
        v = QVBoxLayout(self.control_widget)
        v.setContentsMargins(0, 0, 0, 0); v.setSpacing(0)

        # ─── Acquisition ───
        self.spinbox_averaging = self._spin_int(
            Config.get("frames_to_average", 1), 1, 100, 1, 96)
        self.spinbox_averaging.valueChanged.connect(self.update_averaging_frames)

        self.spinbox_exposure = QDoubleSpinBox()
        # Wide default range; the connected device's real bounds are applied in
        # connect_device() from backend.min_integration_us / max_integration_us.
        self.spinbox_exposure.setRange(0.1, 10000.0)
        self.spinbox_exposure.setDecimals(2)
        self.spinbox_exposure.setValue(Config.get("exposure_ms",
                                                  START_INTEGRATION_TIME_US / 1000.0))
        self.spinbox_exposure.setKeyboardTracking(False)
        self.spinbox_exposure.setFixedWidth(96)
        self.spinbox_exposure.setAlignment(Qt.AlignmentFlag.AlignRight)
        self.spinbox_exposure.valueChanged.connect(self.apply_manual_exposure)

        self.button_exposure_reset = QPushButton("20 ms")
        self.button_exposure_reset.setObjectName("goBtn")
        self.button_exposure_reset.setFixedHeight(28)
        self.button_exposure_reset.setMinimumWidth(60)
        self.button_exposure_reset.setMaximumWidth(76)
        self.button_exposure_reset.setStyleSheet(
            "min-height: 24px; font-size: 11px; letter-spacing: 0px; "
            "padding: 0 8px; border-radius: 5px;"
        )
        self.button_exposure_reset.setToolTip("Reset integration time to 20 ms")
        self.button_exposure_reset.clicked.connect(
            lambda: self.spinbox_exposure.setValue(20.0)
        )

        self.button_auto_exposure = ToggleSwitch(Config.get("auto_exposure", False))
        self.button_auto_exposure.toggled.connect(self.toggle_auto_exposure)

        # Fast Preview (dual-rate) controls
        self.button_fast_preview = ToggleSwitch(Config.get("fast_preview_enabled", False))
        self.button_fast_preview.toggled.connect(self.toggle_fast_preview)

        self.spinbox_fp_short_pct = self._spin_int(
            Config.get("fast_preview_short_pct", 10), 1, 50)
        self.spinbox_fp_short_pct.setSuffix(" %")
        self.spinbox_fp_short_pct.valueChanged.connect(
            lambda v: (Config.set("fast_preview_short_pct", v),
                       setattr(self.hardware_thread, "fast_preview_short_pct", v)
                       if self.hardware_thread else None))

        # ── Spectrum fusion controls ────────────────────────────────────
        self.button_fusion = ToggleSwitch(Config.get("fusion_enabled", False))
        self.button_fusion.toggled.connect(self._on_fusion_toggle)

        self.spinbox_fusion_smoothing = self._spin_int(
            int(round(Config.get("fusion_smoothing", 0.85) * 100)), 0, 99)
        self.spinbox_fusion_smoothing.setSuffix(" %")
        self.spinbox_fusion_smoothing.valueChanged.connect(
            lambda v: self._set_fusion("fusion_smoothing", v / 100.0))

        self.spinbox_fusion_sigma = QDoubleSpinBox()
        self.spinbox_fusion_sigma.setRange(0.5, 20.0)
        self.spinbox_fusion_sigma.setSingleStep(0.5)
        self.spinbox_fusion_sigma.setDecimals(1)
        self.spinbox_fusion_sigma.setValue(Config.get("fusion_change_sigma", 4.0))
        self.spinbox_fusion_sigma.setFixedWidth(96)
        self.spinbox_fusion_sigma.setAlignment(Qt.AlignmentFlag.AlignRight)
        self.spinbox_fusion_sigma.setSuffix(" σ")
        self.spinbox_fusion_sigma.valueChanged.connect(
            lambda v: self._set_fusion("fusion_change_sigma", float(v)))

        self.spinbox_fusion_rate = self._spin_int(
            int(Config.get("fusion_emit_hz", 0.0)), 0, 120)
        self.spinbox_fusion_rate.setSuffix(" Hz")
        self.spinbox_fusion_rate.setSpecialValueText("Auto")
        self.spinbox_fusion_rate.valueChanged.connect(
            lambda v: self._set_fusion("fusion_emit_hz", float(v)))

        self.combo_reference = QComboBox()
        self.combo_reference.addItem("None")
        for key in self.reference_library.keys():
            self.combo_reference.addItem(key)
        self.combo_reference.setFixedWidth(170)
        self.combo_reference.currentTextChanged.connect(self.apply_reference_overlay)
        target = Config.get("reference_library", "None")
        idx = self.combo_reference.findText(target)
        if idx >= 0:
            self.combo_reference.setCurrentIndex(idx)

        # Fixed (non-scrolling) acquisition controls, in the header.
        for _lbl, _sub, _ctl in [
            ("Exposure",      "ms", [self.button_exposure_reset, self.spinbox_exposure]),
            ("Auto exposure", None, self.button_auto_exposure),
            ("Fast preview",  None, self.button_fast_preview),
            ("Short frame",   "%",  self.spinbox_fp_short_pct),
        ]:
            self._fixed_acq.addWidget(make_row(_lbl, _ctl, sub=_sub))

        # Scrolling sections start here (collapsible).
        _sec_acquisition = self._collapsible("Acquisition", [
            ("Frames to average", None, self.spinbox_averaging),
            ("Reference library", None, self.combo_reference),
        ])

        _sec_smoothing = self._collapsible("Smoothing", [
            ("Enable smoothing",   None, self.button_fusion),
            ("Smoothing",       "%",  self.spinbox_fusion_smoothing),
            ("Peak sensitivity", None, self.spinbox_fusion_sigma),
            ("Output rate",     None, self.spinbox_fusion_rate),
        ], collapsed=True)

        # ─── Display ───
        self.spinbox_x_min = self._spin_int(Config.get("x_min_nm", 380), 100, 1500, 10, 72)
        self.spinbox_x_max = self._spin_int(Config.get("x_max_nm", 1050), 100, 1500, 10, 72)
        self.spinbox_x_min.setKeyboardTracking(False)
        self.spinbox_x_max.setKeyboardTracking(False)
        self.spinbox_x_min.valueChanged.connect(self.apply_axes_limits)
        self.spinbox_x_max.valueChanged.connect(self.apply_axes_limits)

        self.button_auto_y_axis = ToggleSwitch(self.is_auto_y_axis_enabled)
        self.button_auto_y_axis.toggled.connect(self.toggle_auto_y_axis)

        self.spinbox_y_max = self._spin_int(Config.get("y_max_adc", 4000),
                                            10, 65000, 100, 96)
        self.spinbox_y_max.setKeyboardTracking(False)
        self.spinbox_y_max.valueChanged.connect(self.apply_manual_y_axis)

        self.button_y_snap = QPushButton("▲")
        self.button_y_snap.setObjectName("goBtn")
        self.button_y_snap.setFixedHeight(28)
        self.button_y_snap.setFixedWidth(32)
        self.button_y_snap.setStyleSheet(
            "min-height: 24px; max-height: 28px; font-size: 11px; "
            "letter-spacing: 0px; padding: 0 4px; border-radius: 5px;"
        )
        self.button_y_snap.setToolTip(
            "Snap Y max to the current frame peak\n(also disables Auto-scale Y)")
        self.button_y_snap.clicked.connect(self._snap_y_to_peak)

        _sec_display = self._collapsible("Display", [
            ("Wavelength range", "nm", [self.spinbox_x_min, self.spinbox_x_max]),
            ("Auto-scale Y",     None, self.button_auto_y_axis),
            ("Y max",            "ADC", [self.button_y_snap, self.spinbox_y_max]),
        ])

        # ─── Processing ───
        self.button_dark_correct = ToggleSwitch(self.is_dark_correction_enabled)
        self.button_dark_correct.toggled.connect(self.toggle_dark_correction)

        self.combo_dark_mode = QComboBox()
        self.combo_dark_mode.addItem("Optical Black (live)", DARK_MODE_OPTICAL_BLACK)
        self.combo_dark_mode.addItem("Parametric (Bias+Rate)", DARK_MODE_PARAMETRIC)
        self.combo_dark_mode.setFixedWidth(190)
        target_dm = Config.get("dark_mode", DARK_MODE_OPTICAL_BLACK)
        for i in range(self.combo_dark_mode.count()):
            if self.combo_dark_mode.itemData(i) == target_dm:
                self.combo_dark_mode.setCurrentIndex(i); break
        self.combo_dark_mode.currentIndexChanged.connect(self.apply_dark_mode)

        self.spinbox_dark_bias = QDoubleSpinBox()
        self.spinbox_dark_bias.setRange(0.0, 1000.0)
        self.spinbox_dark_bias.setValue(Config.get("dark_bias_adc", DEFAULT_DARK_BIAS_ADC))
        self.spinbox_dark_bias.setFixedWidth(96)
        self.spinbox_dark_bias.setAlignment(Qt.AlignmentFlag.AlignRight)
        self.spinbox_dark_bias.valueChanged.connect(
            lambda v: Config.set("dark_bias_adc", v))

        self.spinbox_dark_rate = QDoubleSpinBox()
        self.spinbox_dark_rate.setRange(0.0, 1000.0)
        self.spinbox_dark_rate.setValue(Config.get("dark_rate_adc_per_s",
                                                   DEFAULT_DARK_RATE_ADC_PER_SEC))
        self.spinbox_dark_rate.setFixedWidth(96)
        self.spinbox_dark_rate.setAlignment(Qt.AlignmentFlag.AlignRight)
        self.spinbox_dark_rate.valueChanged.connect(
            lambda v: Config.set("dark_rate_adc_per_s", v))

        self.button_despeckle = ToggleSwitch(self.is_despeckle_enabled)
        self.button_despeckle.toggled.connect(self.toggle_despeckle_filter)

        self.spinbox_despeckle_width = self._spin_int(
            Config.get("smoothing_width", 5), 3, 15, 2, 72)
        self.spinbox_despeckle_width.valueChanged.connect(
            lambda v: Config.set("smoothing_width", v))

        self.button_spatial_smooth = ToggleSwitch(self.is_spatial_smoothing_enabled)
        self.button_spatial_smooth.toggled.connect(self.toggle_spatial_smoothing)

        self.spinbox_spatial_width = self._spin_int(
            Config.get("spatial_smoothing_width", 5), 3, 51, 2, 72)
        self.spinbox_spatial_width.valueChanged.connect(
            lambda v: Config.set("spatial_smoothing_width", v))

        self.combo_response = QComboBox()
        self.combo_response.setObjectName("deviceSelect")
        self.combo_response.setFixedWidth(150)
        self.combo_response.addItem(calprof.SEL_NONE)
        self.combo_response.currentTextChanged.connect(self._on_response_selection)

        # Delete the selected saved profile (parity with the web client's
        # "Manage profile" delete). Disabled for None / Device default.
        self.button_response_delete = QPushButton("✕")
        self.button_response_delete.setObjectName("ghostBtn")
        self.button_response_delete.setFixedWidth(34)
        self.button_response_delete.setEnabled(False)
        self.button_response_delete.setToolTip(
            "Delete the selected response-correction profile for this device.")
        self.button_response_delete.clicked.connect(self._on_response_delete)

        self.button_calibrate = QPushButton("Calibration…")
        self.button_calibrate.clicked.connect(self.open_calibration_dialog)

        _sec_processing = self._collapsible("Processing", [
            ("Dark correction",       None,  self.button_dark_correct),
            ("Dark mode",             None,  self.combo_dark_mode),
            ("Bias offset",          "ADC",  self.spinbox_dark_bias),
            ("Rate",               "ADC/s",  self.spinbox_dark_rate),
            ("Hot-pixel filter",      None,  self.button_despeckle),
            ("Smoothing width",       "px",  self.spinbox_despeckle_width),
            ("Spatial smoothing",     None,  self.button_spatial_smooth),
            ("Spatial width",         "px",  self.spinbox_spatial_width),
            ("Response correction",   None,  [self.combo_response, self.button_response_delete]),
            ("Calibrate device",      None,  self.button_calibrate),
        ])

        # ─── Peak detection ───
        self.spinbox_sg_window = self._spin_int(
            Config.get("peak_savgol_window", 11), 5, 101, 2, 72)
        self.spinbox_sg_window.valueChanged.connect(self._on_peak_param_changed)

        self.spinbox_sg_order = self._spin_int(
            Config.get("peak_savgol_order", 3), 1, 6, 1, 60)
        self.spinbox_sg_order.valueChanged.connect(self._on_peak_param_changed)

        self.spinbox_prominence = QDoubleSpinBox()
        self.spinbox_prominence.setRange(0.0, 5000.0)
        self.spinbox_prominence.setSingleStep(10.0)
        self.spinbox_prominence.setValue(Config.get("peak_prominence", 50.0))
        self.spinbox_prominence.setFixedWidth(96)
        self.spinbox_prominence.setAlignment(Qt.AlignmentFlag.AlignRight)
        self.spinbox_prominence.valueChanged.connect(self._on_peak_param_changed)

        self.spinbox_min_dist = self._spin_int(
            Config.get("peak_min_distance_px", 40), 1, 500, 5, 72)
        self.spinbox_min_dist.valueChanged.connect(self._on_peak_param_changed)

        self.spinbox_peak_count = self._spin_int(
            Config.get("peak_max_count", 3), 1, 12, 1, 60)
        self.spinbox_peak_count.valueChanged.connect(self._on_peak_param_changed)

        _sec_peaks = self._collapsible("Peak detection", [
            ("Savitzky-Golay window", "px", self.spinbox_sg_window),
            ("Savitzky-Golay order",  None, self.spinbox_sg_order),
            ("Prominence min",       "ADC", self.spinbox_prominence),
            ("Min peak distance",     "px", self.spinbox_min_dist),
            ("Max peak count",        None, self.spinbox_peak_count),
        ], collapsed=True)

        _sec_data = self._build_data_section()

        # Display, Acquisition, Smoothing, Processing, Peak detection, Data.
        for _sec in (_sec_display, _sec_acquisition, _sec_smoothing,
                    _sec_processing, _sec_peaks, _sec_data):
            v.addLayout(_sec)

        v.addStretch(1)

        self.apply_dark_mode()
        return outer

    def _spin_int(self, value, lo, hi, step=1, width=96) -> QSpinBox:
        sb = QSpinBox()
        sb.setRange(lo, hi); sb.setSingleStep(step); sb.setValue(value)
        sb.setFixedWidth(width); sb.setAlignment(Qt.AlignmentFlag.AlignRight)
        return sb

    def _section(self, title, rows):
        v = QVBoxLayout()
        v.setContentsMargins(14, 14, 14, 12); v.setSpacing(2)
        hdr = QLabel(title.upper()); hdr.setObjectName("sectionHeader")
        v.addWidget(hdr); v.addSpacing(4)
        for label, sub, control in rows:
            v.addWidget(make_row(label, control, sub=sub))
        v.addSpacing(6)
        v.addWidget(make_hsep())
        w = QWidget(); w.setObjectName("sidebarInner"); w.setLayout(v)
        outer = QVBoxLayout(); outer.setContentsMargins(0, 0, 0, 0)
        outer.addWidget(w)
        return outer

    def _build_data_section(self):
        """Central export settings: directory (applies to every save in every
        tab — CSV, PNG, reports, autosave) plus the filename template."""
        box = QWidget(); box.setObjectName("sidebarInner")
        bl = QVBoxLayout(box); bl.setContentsMargins(0, 0, 0, 0); bl.setSpacing(0)
        hdr = QPushButton(); hdr.setObjectName("sectionHeader")
        hdr.setCheckable(True); hdr.setChecked(True)
        hdr.setCursor(Qt.CursorShape.PointingHandCursor)
        hdr.setStyleSheet("QPushButton { text-align:left; border:none; "
                          "background:transparent; padding:12px 14px 4px 14px; }")
        hdr.setText("▾  DATA")
        content = QWidget(); content.setObjectName("sidebarInner")
        cv = QVBoxLayout(content); cv.setContentsMargins(14, 0, 14, 12); cv.setSpacing(6)

        self.btn_export_dir = QPushButton("Choose export folder…")
        self.btn_export_dir.clicked.connect(self._choose_export_dir)
        cv.addWidget(self.btn_export_dir)

        self.lbl_export_status = QLabel()
        self.lbl_export_status.setObjectName("readoutLbl")
        self.lbl_export_status.setWordWrap(True)
        cv.addWidget(self.lbl_export_status)

        _fn_lbl = QLabel("Filename"); _fn_lbl.setObjectName("rowLbl")
        cv.addWidget(_fn_lbl)
        self.edit_export_template = QLineEdit(
            Config.get("export_filename_template", export_paths.DEFAULT_TEMPLATE))
        self.edit_export_template.setToolTip(export_paths.TOKEN_HELP)
        # Live "debug" preview on every keystroke; persist on commit.
        self.edit_export_template.textChanged.connect(self._on_export_template_live)
        self.edit_export_template.editingFinished.connect(self._on_export_template)
        cv.addWidget(self.edit_export_template)

        tok = QLabel(export_paths.TOKEN_HELP)
        tok.setObjectName("readoutLbl"); tok.setWordWrap(True)
        cv.addWidget(tok)

        self.lbl_export_preview = QLabel()
        self.lbl_export_preview.setObjectName("readoutLbl"); self.lbl_export_preview.setWordWrap(True)
        cv.addWidget(self.lbl_export_preview)

        bl.addWidget(hdr); bl.addWidget(content); bl.addWidget(make_hsep())
        content.setVisible(True)
        hdr.toggled.connect(lambda c: (content.setVisible(c),
                                       hdr.setText(("▾  " if c else "▸  ") + "DATA")))
        self._refresh_export_status()
        outer = QVBoxLayout(); outer.setContentsMargins(0, 0, 0, 0)
        outer.addWidget(box)
        return outer

    def _choose_export_dir(self) -> None:
        cur = export_paths.configured_dir()
        d = QFileDialog.getExistingDirectory(self, "Export folder", cur)
        if d:
            Config.set("export_dir", d)
            self._refresh_export_status()

    def _on_export_template(self) -> None:
        Config.set("export_filename_template",
                   self.edit_export_template.text().strip()
                   or export_paths.DEFAULT_TEMPLATE)
        self._refresh_export_status()

    def _on_export_template_live(self, text: str) -> None:
        """Preview the exact filename for whatever is currently typed (a live
        'debug' of the template) without committing it to config until the edit
        finishes."""
        tmpl = text.strip() or export_paths.DEFAULT_TEMPLATE
        try:
            saved = Config.get("export_filename_template", export_paths.DEFAULT_TEMPLATE)
            Config.set("export_filename_template", tmpl)
            preview = export_paths.build_filename("spectrum", "csv", self.export_meta())
            Config.set("export_filename_template", saved)   # don't persist while typing
            self.lbl_export_preview.setText(f"e.g. {preview}")
        except Exception:
            self.lbl_export_preview.setText("")

    def _refresh_export_status(self) -> None:
        """Show the effective export folder (and any fallback error) + a live
        filename preview."""
        d, err = export_paths.ensure_export_dir()
        self.lbl_export_status.setText(("⚠ " + err) if err else f"Folder: {d}")
        try:
            preview = export_paths.build_filename("spectrum", "csv", self.export_meta())
            self.lbl_export_preview.setText(f"e.g. {preview}")
        except Exception:
            self.lbl_export_preview.setText("")

    def _collapsible(self, title, rows, collapsed=False):
        """Titled section whose body can be folded away to shorten the sidebar
        scroll. Header click toggles; returns a layout (like _section)."""
        box = QWidget(); box.setObjectName("sidebarInner")
        bl = QVBoxLayout(box); bl.setContentsMargins(0, 0, 0, 0); bl.setSpacing(0)
        hdr = QPushButton(); hdr.setObjectName("sectionHeader")
        hdr.setCheckable(True); hdr.setChecked(not collapsed)
        hdr.setCursor(Qt.CursorShape.PointingHandCursor)
        hdr.setStyleSheet(
            "QPushButton { text-align:left; border:none; background:transparent; "
            "padding:12px 14px 4px 14px; }")

        def _fmt(open_):
            return ("▾  " if open_ else "▸  ") + title.upper()
        hdr.setText(_fmt(not collapsed))

        content = QWidget(); content.setObjectName("sidebarInner")
        cv = QVBoxLayout(content); cv.setContentsMargins(14, 0, 14, 12); cv.setSpacing(2)
        for label, sub, control in rows:
            cv.addWidget(make_row(label, control, sub=sub))
        content.setVisible(not collapsed)

        def _toggle(checked):
            content.setVisible(checked); hdr.setText(_fmt(checked))
        hdr.toggled.connect(_toggle)

        bl.addWidget(hdr); bl.addWidget(content); bl.addWidget(make_hsep())
        outer = QVBoxLayout(); outer.setContentsMargins(0, 0, 0, 0)
        outer.addWidget(box)
        return outer

    def _make_save_button(self, text, kind):
        """Flat, subtly tinted export button for the fixed header."""
        colors = {   # fg, border, bg, hover-bg
            "csv":  ("#cbb96a", "#5a5230", "rgba(203,185,106,0.10)", "rgba(203,185,106,0.20)"),
            "png":  ("#b49ad6", "#4c4066", "rgba(180,154,214,0.10)", "rgba(180,154,214,0.20)"),
            "copy": ("#8fc79a", "#3c5a42", "rgba(143,199,154,0.10)", "rgba(143,199,154,0.20)"),
        }
        fg, br, bg, bgh = colors.get(kind, colors["csv"])
        b = QPushButton(text)
        b.setFixedHeight(30)
        b.setStyleSheet(
            f"QPushButton {{ color:{fg}; border:1px solid {br}; background:{bg}; "
            f"border-radius:6px; padding:4px 8px; font-size:12px; }}"
            f"QPushButton:hover {{ background:{bgh}; }}"
            f"QPushButton:disabled {{ color:#666; border-color:#333; background:transparent; }}")
        return b

    # ──────────────── STATUS BAR ────────────────
    def _build_statusbar(self) -> QWidget:
        w = QWidget(); w.setObjectName("statusbar")
        w.setFixedHeight(34)
        h = QHBoxLayout(w)
        h.setContentsMargins(14, 4, 14, 4); h.setSpacing(20)

        def kv(key, val):
            wrap = QWidget()
            wl = QHBoxLayout(wrap)
            wl.setContentsMargins(0, 0, 0, 0); wl.setSpacing(6)
            kl = QLabel(key); kl.setObjectName("statusKey")
            vl = QLabel(val); vl.setObjectName("statusVal")
            wl.addWidget(kl); wl.addWidget(vl)
            return wrap, vl

        wrap0, self.status_fps         = kv("Frames / s", "—")
        wrap1, self.status_integration = kv("Integration", "—")
        wrap2, self.status_dark        = kv("Dark", "—")
        wrap3, self.status_bias        = kv("Bias",
            f"{Config.get('dark_bias_adc', DEFAULT_DARK_BIAS_ADC):.2f} ADC")
        wrap4, self.status_rate        = kv("Rate",
            f"{Config.get('dark_rate_adc_per_s', DEFAULT_DARK_RATE_ADC_PER_SEC):.2f} ADC/s")
        wrap5, self.status_conn        = kv("Status", "Disconnected")
        for wrap in (wrap0, wrap1, wrap2, wrap3, wrap4, wrap5):
            h.addWidget(wrap); h.addWidget(make_vsep())
        h.addStretch(1)

        self.chip_sensor_temp = QLabel("Sensor: —")
        self.chip_sensor_temp.setObjectName("sensorChip")
        self._restyle_sensor_chip("neutral", "Sensor: —")
        h.addWidget(self.chip_sensor_temp)
        h.addWidget(make_vsep())
        try:
            from version import app_version
            _ver = app_version()
        except Exception:
            _ver = "unknown"
        self.lbl_version = QLabel(f"v{_ver}")
        self.lbl_version.setObjectName("statusKey")
        self.lbl_version.setToolTip("Build version (last commit / newest file date)")
        h.addWidget(self.lbl_version)
        return w

    def _restyle_sensor_chip(self, kind: str, text: str) -> None:
        """kind ∈ {'neutral', 'ok', 'warn', 'danger'}."""
        p = T()
        if kind == "ok":
            fg, bg, br = p.OK, p.OK_BG, p.OK_BORDER
        elif kind == "warn":
            fg, bg, br = p.WARN, p.WARN_BG, p.WARN_BORDER
        elif kind == "danger":
            fg, bg, br = p.DANGER, "#3a221d", p.DANGER_BORDER
        else:
            fg, bg, br = p.FG3, p.BG2, p.BORDER1
        self.chip_sensor_temp.setStyleSheet(
            f"color:{fg};background:{bg};border:1px solid {br};"
            f"border-radius:8px;padding:4px 12px;"
            f"font-family:{FONT_MONO};font-size:10px;")
        self.chip_sensor_temp.setText(text)

    # ──────────────── TOOLTIPS ────────────────
    def _install_tooltips(self) -> None:
        add_help(self.button_scan, "Scan the USB bus for connected PASCO PS-2600A.")
        add_help(self.combo_devices, "Detected spectrometers — pick one and Connect.")
        add_help(self.button_connect, "Connect to the selected device and start acquisition.")
        add_help(self.button_pause, "Pause/resume live acquisition.")
        add_help(self.button_save_csv, "Export the averaged spectrum (or heatmap) to CSV.")
        add_help(self.button_save_png, "Save a screenshot of the active plot.")
        add_help(self.button_copy_clip, "Copy the current spectrum as TSV to the clipboard.")
        add_help(self.spinbox_averaging, "Number of consecutive frames to average. 1–100.")
        add_help(self.spinbox_exposure, "Integration time in ms (1 – 2500 ms linear regime).")
        add_help(self.button_exposure_reset,
                 "Reset integration time to 20 ms.\n"
                 "Useful after auto-exposure has driven the value to maximum.")
        add_help(self.button_auto_exposure, "Automatically adjust exposure to keep the strongest peak near target.")
        add_help(self.button_fast_preview,
                 "Fast Preview: shortens the exposure to the 'Short frame %' of the\n"
                 "main integration time, reads frames as fast as the device allows,\n"
                 "and scales the signal back up to normal-exposure levels (dark\n"
                 "current handled per frame). Faster, noisier live trace.\n"
                 "Disable when using Auto Exposure.")
        add_help(self.spinbox_fp_short_pct,
                 "Short-frame exposure as a percentage of the main integration time.\n"
                 "E.g. 10% of 1000 ms = 100 ms frames, scaled up ×10.\n"
                 "Lower = faster refresh but more noise per frame.")
        add_help(self.button_fusion,
                 "Smoothing: softens the per-frame noise of the live trace over TIME,\n"
                 "never across wavelength — peaks keep their exact shape and height.\n"
                 "On a steady signal you get a clean trace; when the signal changes,\n"
                 "the display reacts within one frame. Works with or without Fast\n"
                 "Preview.")
        add_help(self.spinbox_fusion_smoothing,
                 "How strongly steady parts of the spectrum are smoothed over time.\n"
                 "Higher = cleaner, more stable trace (closer to long-exposure noise),\n"
                 "but a touch more lag on slow changes.\n"
                 "This only smooths over TIME, never across wavelength — peaks keep\n"
                 "their exact shape and height. Typical: 80–95%.")
        add_help(self.spinbox_fusion_sigma,
                 "Peak sensitivity: how far a pixel must jump (in noise multiples)\n"
                 "before smoothing backs off and the change is shown immediately.\n"
                 "Lower = reacts to smaller changes (more responsive, slightly noisier).\n"
                 "Higher = only large changes break through (smoother, calmer).\n"
                 "A new or rising peak always appears at full height. Typical: 3–5 σ.")
        add_help(self.spinbox_fusion_rate,
                 "How often the trace is pushed to the display.\n"
                 "Auto (0) derives the rate from the frame cadence, so it scales\n"
                 "with your settings and the device (a fast spectrometer updates faster).\n"
                 "Set a fixed value to force a specific refresh rate.")
        add_help(self.combo_reference, "Overlay a reference spectrum (gas lamps, fluorescents, LEDs).")
        add_help(self.button_auto_y_axis, "Auto-scale Y to the tallest visible peak.")
        add_help(self.button_dark_correct, "Enable dark-current subtraction.")
        add_help(self.combo_dark_mode, "Optical Black: per-frame correction.\nParametric: Bias + Rate · t.")
        add_help(self.button_despeckle, "Sliding-median filter to suppress hot pixels.")
        add_help(self.button_spatial_smooth,
                 "Moving-average over a window of pixels along the wavelength\n"
                 "axis. De-noises the trace, including the fixed-pattern structure\n"
                 "(PRNU / etaloning) that temporal smoothing cannot remove.\n"
                 "Wider window = smoother trace but broader, lower peaks\n"
                 "(resolution loss). Keep the width well below the narrowest\n"
                 "peak's FWHM.")
        add_help(self.combo_response,
                 "Per-pixel spectral response correction for this device.\n"
                 "None = no correction (raw counts). Device default = the\n"
                 "driver's built-in sensitivity table (if any). Or pick a saved\n"
                 "profile measured for a specific fibre/optical setup. Gains are\n"
                 "capped to keep edge noise from exploding.")
        add_help(self.button_calibrate,
                 "Open the calibration wizards: fit pixel → wavelength from a\n"
                 "line lamp (Cd / Hg / Ne / Ar …), or build a response-correction\n"
                 "profile from a broadband lamp vs a reference.")
        add_help(self.btn_peaks_toolbar,
                 "Find and label dominant peaks using Savitzky-Golay smoothing\n"
                 "plus prominence-based filtering.")
        add_help(self.btn_measure_toolbar,
                 "Place two draggable cursors and read Δλ / Δy between them.")
        add_help(self.spinbox_sg_window,
                 "Savitzky-Golay window length (odd integer).\n"
                 "Wider windows smooth more but blur narrow peaks.")
        add_help(self.spinbox_sg_order,
                 "Savitzky-Golay polynomial order. Must be < window length.")
        add_help(self.spinbox_prominence,
                 "Minimum prominence (ADC counts) for a peak to be reported.\n"
                 "Higher = stricter, rejects shoulders.")
        add_help(self.spinbox_min_dist,
                 "Minimum distance between peaks (in pixels).")
        add_help(self.spinbox_peak_count,
                 "Maximum number of peaks to display, sorted by prominence.")

    # ════════════════════════════════════════════════════════════
    # CONNECTION
    # ════════════════════════════════════════════════════════════
    def _on_backend_changed(self, name: str) -> None:
        """Repopulate the device dropdown when the backend changes."""
        Config.set("selected_backend", name)
        self.refresh_device_list()
        if hasattr(self, "combo_response"):
            self._refresh_response_combo()

    def refresh_device_list(self) -> None:
        backend_name = self.combo_backend.currentText()
        cls = dm.get_backend_class(backend_name)
        self.combo_devices.clear()
        if cls is None:
            self.combo_devices.addItem("No backend available")
            self.button_connect.setEnabled(False)
            return
        try:
            devices = cls.scan()
        except Exception as e:
            self.combo_devices.addItem(f"Scan error: {e}")
            self.button_connect.setEnabled(False)
            return
        if not devices:
            self.combo_devices.addItem(f"No {backend_name} found")
            self.button_connect.setEnabled(False)
        else:
            for d in devices:
                # Show a short label but store full path as item data
                label = d if len(d) <= 40 else f"…{d[-37:]}"
                self.combo_devices.addItem(label, d)
            self.button_connect.setEnabled(True)

    def toggle_connection(self) -> None:
        if self.hardware_thread is not None:
            self.disconnect_device()
        else:
            self.connect_device()

    def connect_device(self) -> None:
        backend_name = self.combo_backend.currentText()
        cls = dm.get_backend_class(backend_name)
        if cls is None:
            QMessageBox.critical(self, "Error", f"Backend '{backend_name}' not available.")
            return
        device_id = self.combo_devices.currentData() or self.combo_devices.currentText()
        if not device_id or device_id.startswith("No ") or device_id.startswith("Scan"):
            return
        try:
            backend = cls(
                on_frame           = self._on_frame_callback,
                on_auto_exp        = self._on_auto_exp_callback,
                on_connection_lost = self._on_connection_lost_callback,
            )
            # Apply device-specific settings BEFORE connect() so that
            # the calibration file load inside connect() can use them.
            if hasattr(backend, "wl_offset_nm"):
                backend.wl_offset_nm = float(Config.get("lr2t_wl_offset_nm", 0.0))
            if hasattr(backend, "flip_pixels"):
                backend.flip_pixels = bool(Config.get("lr2t_flip_pixels", True))

            backend.connect(device_id)

            # Size the exposure control to this device's integration bounds
            # (per-device; HDX can be pushed below 1 ms via ocean_min_integration_us).
            lo_ms = getattr(backend, "min_integration_us", 1000) / 1000.0
            hi_ms = getattr(backend, "max_integration_us", 10_000_000) / 1000.0
            self.spinbox_exposure.blockSignals(True)
            self.spinbox_exposure.setRange(lo_ms, hi_ms)
            self.spinbox_exposure.blockSignals(False)

            # Push current config to the backend
            ms = self.spinbox_exposure.value()
            us = int(ms * 1000)
            backend.current_integration_time_us = us
            backend.set_integration_time_us(us)
            backend.is_auto_exposure_active    = self.button_auto_exposure.isChecked()
            backend.fast_preview_enabled       = self.button_fast_preview.isChecked()
            backend.fast_preview_short_pct     = self.spinbox_fp_short_pct.value()

            # ─────────────────────────────────────────────────────────────
            # IMPORTANT ORDERING: prepare the wavelength axis, clear stale
            # averaging history, and arm the fusion stage BEFORE start().
            # A fast device (HDX at ~6 ms) can deliver its first frame almost
            # instantly; if start() ran first the frame could reach
            # process_new_spectrum while active_wavelengths / spectrum_history
            # still held the previous device's pixel count → np.mean() over
            # mixed-size arrays crashes. PASCO masked this with its slow first
            # frame. Everything below is set up first; start() is last.
            # ─────────────────────────────────────────────────────────────

            # Update active wavelength axis to match the connected device.
            wl = backend.wavelength_array
            if len(wl) != self.active_pixel_count or not np.array_equal(wl, self.active_wavelengths):
                self.active_wavelengths   = wl
                self.active_pixel_count   = len(wl)
                self.active_response_gain = np.ones(len(wl))
                self.current_averaged_pixels = np.zeros(self.active_pixel_count)
                self._pixels_pre_response    = np.zeros(self.active_pixel_count)
                # Rebuild heatmap buffer for new pixel count
                self.heatmap_buffer = np.zeros(
                    (self.active_pixel_count, self.heatmap_size))
                self.heatmap_times = np.full(self.heatmap_size, np.nan)
                self._heatmap_filled = 0
                self._hm_go_live()
                self.slider_hm_scrub.setRange(0, 0)
                self.heatmap_linear_waves = np.linspace(
                    self.active_wavelengths[0], self.active_wavelengths[-1],
                    self.active_pixel_count)
                self._cie_mask = ((self.active_wavelengths >= 380) &
                                  (self.active_wavelengths <= 780))
                self._rebuild_image_transform()
                self._load_reference_library()

            self.hardware_thread = backend
            # Populate the response-correction dropdown for this device and
            # apply its persisted selection (None / Device default / profile).
            self._refresh_response_combo()
            self.apply_response_correction()
            self.spectrum_history.clear()
            # Arm the fusion stage fresh for this device (timer running, but it
            # only emits once frames arrive).
            self._apply_fusion_config()
            self._fusion.reset()
            self._fusion.start()

            # Everything is ready — NOW start acquisition.
            backend.start()
            # Re-push exposure — SpectrometerAcquisition.__init__ sends
            # PASCO_START_INTEGRATION_US to hardware; override it now.
            backend.set_integration_time_us(us)


            self._is_paused = False
            self._set_pause_button_state(False)
            self.button_pause.setEnabled(True)

            self.button_connect.setText("Disconnect")
            self.button_connect.setObjectName("dangerBtn")
            self.button_connect.style().unpolish(self.button_connect)
            self.button_connect.style().polish(self.button_connect)
            self.conn_dot.setOn(True)
            self._restyle_live_pill(True)
            self.status_conn.setText("Connected")
            self.combo_devices.setEnabled(False)
            self.combo_backend.setEnabled(False)
            self.button_scan.setEnabled(False)
            self.control_widget.setEnabled(True)
            self.header_controls.setEnabled(True)
            self._update_spec_overlay()
        except Exception as e:
            QMessageBox.critical(self, "Connection Error", str(e))

    def disconnect_device(self) -> None:
        self._fusion.stop()
        if self.hardware_thread:
            self._deliberate_disconnect = True
            self.hardware_thread.disconnect()
            self.hardware_thread = None
        self._update_spec_overlay()
        self.button_connect.setText("Connect")
        self.button_connect.setObjectName("primaryBtn")
        self.button_connect.style().unpolish(self.button_connect)
        self.button_connect.style().polish(self.button_connect)
        self.conn_dot.setOn(False)
        self._restyle_live_pill(False)
        self.status_conn.setText("Disconnected")
        self.combo_devices.setEnabled(True)
        self.combo_backend.setEnabled(True)
        self.button_scan.setEnabled(True)
        self.control_widget.setEnabled(False)
        self.header_controls.setEnabled(False)
        self._is_paused = False
        self._set_pause_button_state(False)
        self.button_pause.setEnabled(False)
        self._clear_peak_labels()
        self.measure_label.setVisible(False)
        self.scatter_peaks.clear()
        self._set_readout(self.readout_pix, "—")
        self._set_readout(self.readout_wl,  "—")
        self._set_readout(self.readout_int, "—")

    def _on_frame_callback(self, pixels: np.ndarray, ob_mean: float,
                           integration_us: int, kind: str = "standard",
                           ratio: float = 1.0) -> None:
        """Called from the acquisition thread. Feed the fusion stage; the
        fusion emitter (or pass-through when disabled) calls _fused_emit,
        which marshals the result to the GUI thread."""
        self._fusion.submit(pixels, ob_mean, integration_us, kind, ratio)

    def _fused_emit(self, pixels: np.ndarray, dark: float,
                    integration_us: int) -> None:
        """Fusion output sink (may run on the fusion emit-timer thread).
        Marshal to the GUI thread for rendering."""
        QMetaObject.invokeMethod(
            self, "_on_frame_gui",
            Qt.ConnectionType.QueuedConnection,
            Q_ARG("PyQt_PyObject", pixels),
            Q_ARG(float, dark),
            Q_ARG(int, integration_us),
        )

    @pyqtSlot("PyQt_PyObject", float, int)
    def _on_frame_gui(self, pixels: np.ndarray, ob_mean: float, integration_us: int) -> None:
        self.process_new_spectrum(pixels, ob_mean, integration_us)

    def _on_auto_exp_callback(self, integration_ms: float) -> None:
        QMetaObject.invokeMethod(
            self, "_on_auto_exp_gui",
            Qt.ConnectionType.QueuedConnection,
            Q_ARG(float, integration_ms),
        )

    @pyqtSlot(float)
    def _on_auto_exp_gui(self, integration_ms: float) -> None:
        self.handle_auto_exposure_update(integration_ms)

    def _on_connection_lost_callback(self) -> None:
        QMetaObject.invokeMethod(
            self, "_on_connection_lost_gui",
            Qt.ConnectionType.QueuedConnection,
        )

    @pyqtSlot()
    def _on_connection_lost_gui(self) -> None:
        self.handle_connection_lost()

    def handle_connection_lost(self) -> None:
        if getattr(self, "_deliberate_disconnect", False):
            self._deliberate_disconnect = False
            return   # user-initiated — no warning
        self.disconnect_device()
        QMessageBox.warning(self, "Connection Lost",
                            "The device was unplugged or stopped responding.")

    # ════════════════════════════════════════════════════════════
    # MOUSE → live readout
    # ════════════════════════════════════════════════════════════
    def export_meta(self) -> dict:
        """Token fields for export_paths (device / serial / wavelength span)."""
        return export_paths.meta_from_report(self.report_meta(),
                                             getattr(self, "active_wavelengths", None))

    def report_meta(self) -> dict:
        """Acquisition parameters stamped onto colour/filter reports. Missing
        values are dropped by the renderer."""
        from datetime import datetime
        b = self.hardware_thread
        us = getattr(b, "current_integration_time_us", None)
        px = getattr(self, "current_averaged_pixels", None)
        peak = float(np.max(px)) if (px is not None and len(px)) else None
        ob = getattr(self, "latest_ob_mean", None)
        return {
            "Spectrometer":    self.combo_backend.currentText() or (type(b).__name__ if b else None),
            "Serial":          getattr(b, "serial", None),
            "Date/time":       datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "Exposure":        (f"{us/1000:.1f} ms" if us else None),
            "Dark / baseline": (f"{ob:.0f} ADC" if ob is not None else None),
            "Peak ADC":        (f"{peak:.0f}" if peak is not None else None),
        }

    def handle_mouse_movement(self, ev) -> None:
        pos = ev[0]
        if not self.plot_canvas.sceneBoundingRect().contains(pos):
            return
        view_pt = self.plot_canvas.getPlotItem().vb.mapSceneToView(pos)
        idx = (np.abs(self.active_wavelengths - view_pt.x())).argmin()
        # Moving an InfiniteLine forces a full ViewBox repaint; at 30 Hz over a
        # big curve on a laptop iGPU that starves the acquisition thread (GIL/
        # CPU) and glitches the LR-2T. Skip the repaint when the nearest pixel
        # hasn't changed (mouse still within one pixel column).
        if idx == getattr(self, "_last_cursor_idx", -1):
            return
        self._last_cursor_idx = idx
        sx, sy = self.active_wavelengths[idx], self.current_averaged_pixels[idx]
        self.cursor_vline.setPos(sx); self.cursor_hline.setPos(sy)
        self._set_readout(self.readout_pix, str(idx))
        self._set_readout(self.readout_wl,  f"{sx:.1f}")
        self._set_readout(self.readout_int, f"{sy:.0f}")

    def handle_heatmap_mouse_movement(self, ev) -> None:
        pos = ev[0]
        if not self.heatmap_canvas.sceneBoundingRect().contains(pos):
            return
        view_pt = self.heatmap_canvas.getPlotItem().vb.mapSceneToView(pos)
        lin_idx = (np.abs(self.heatmap_linear_waves - view_pt.x())).argmin()
        frame_index = int(np.clip(round(view_pt.y()), 0, self.heatmap_size - 1))
        key = (lin_idx, frame_index)
        if key == getattr(self, "_last_hm_cursor", None):
            return
        self._last_hm_cursor = key
        sx = self.heatmap_linear_waves[lin_idx]
        hw_pixel = (np.abs(self.active_wavelengths - sx)).argmin()
        intensity = self.heatmap_buffer[lin_idx, frame_index]
        self.heatmap_cursor_vline.setPos(sx)
        self.heatmap_cursor_hline.setPos(frame_index)
        self.scrub_cursor_vline.setPos(sx)     # keep both crosshairs in lockstep
        self._set_readout(self.readout_pix, f"~{hw_pixel}")
        self._set_readout(self.readout_wl,  f"{sx:.1f}")
        self._set_readout(self.readout_int, f"{intensity:.0f}")

    def handle_scrub_mouse_movement(self, ev) -> None:
        """Hovering the scrubbed-frame spectrum (top) sweeps the SAME vertical
        crosshair across the heatmap image (bottom) too — the two plots share
        the wavelength axis (setXLink), so a scene x maps to the same
        wavelength in both."""
        pos = ev[0]
        if not self.plot_hm_scrub.sceneBoundingRect().contains(pos):
            return
        view_pt = self.plot_hm_scrub.getPlotItem().vb.mapSceneToView(pos)
        lin_idx = (np.abs(self.heatmap_linear_waves - view_pt.x())).argmin()
        if lin_idx == getattr(self, "_last_scrub_cursor", -1):
            return
        self._last_scrub_cursor = lin_idx
        sx = self.heatmap_linear_waves[lin_idx]
        self.scrub_cursor_vline.setPos(sx)
        self.heatmap_cursor_vline.setPos(sx)
        y = self.curve_hm_scrub.yData[lin_idx] if self.curve_hm_scrub.yData is not None else 0.0
        hw_pixel = (np.abs(self.active_wavelengths - sx)).argmin()
        self._set_readout(self.readout_pix, f"~{hw_pixel}")
        self._set_readout(self.readout_wl,  f"{sx:.1f}")
        self._set_readout(self.readout_int, f"{y:.0f}")

    # ════════════════════════════════════════════════════════════
    # CONTROL HANDLERS
    # ════════════════════════════════════════════════════════════
    def toggle_measurement_pause(self) -> None:
        """Toggle between running (PAUSE shown) and paused (START shown)."""
        paused = not getattr(self, "_is_paused", False)
        self._is_paused = paused
        if self.hardware_thread:
            self.hardware_thread.is_measurement_paused = paused
        self._set_pause_button_state(paused)

    def is_measurement_paused(self) -> bool:
        """Whether live acquisition is currently paused."""
        return bool(getattr(self, "_is_paused", False))

    def set_measurement_paused(self, paused: bool) -> None:
        """Set the paused state explicitly (used by the calibration wizard to
        resume streaming for a live capture, then restore the prior state).
        No-op if nothing is connected."""
        paused = bool(paused)
        self._is_paused = paused
        if self.hardware_thread:
            self.hardware_thread.is_measurement_paused = paused
        self._set_pause_button_state(paused)

    def _set_pause_button_state(self, paused: bool) -> None:
        if paused:
            self.button_pause.setText("▶  START")
            self.button_pause.setObjectName("goBtnPaused")
        else:
            self.button_pause.setText("■  PAUSE")
            self.button_pause.setObjectName("goBtn")
        self.button_pause.style().unpolish(self.button_pause)
        self.button_pause.style().polish(self.button_pause)

    def update_averaging_frames(self, value: int) -> None:
        self.spectrum_history = deque(self.spectrum_history, maxlen=value)
        Config.set("frames_to_average", int(value))

    def apply_manual_exposure(self, ms: float) -> None:
        Config.set("exposure_ms", float(ms))
        if not self.hardware_thread:
            return
        if self.hardware_thread.is_auto_exposure_active:
            self.hardware_thread.is_auto_exposure_active = False
            self.button_auto_exposure.blockSignals(True)
            self.button_auto_exposure.setChecked(False)
            self.button_auto_exposure.blockSignals(False)
            Config.set("auto_exposure", False)
        us = int(ms * 1000)
        self.hardware_thread.current_integration_time_us = us
        self.hardware_thread.set_integration_time_us(us)
        self.spectrum_history.clear()

    def handle_auto_exposure_update(self, new_ms: float) -> None:
        self.spinbox_exposure.blockSignals(True)
        self.spinbox_exposure.setValue(new_ms)
        self.spinbox_exposure.blockSignals(False)
        Config.set("exposure_ms", float(new_ms))
        self.spectrum_history.clear()

    def toggle_auto_exposure(self, checked: bool) -> None:
        if self.hardware_thread:
            self.hardware_thread.is_auto_exposure_active = checked
        Config.set("auto_exposure", bool(checked))
        self.spectrum_history.clear()

    def toggle_fast_preview(self, checked: bool) -> None:
        Config.set("fast_preview_enabled", bool(checked))
        if self.hardware_thread:
            self.hardware_thread.fast_preview_enabled   = checked
            self.hardware_thread.fast_preview_short_pct = self.spinbox_fp_short_pct.value()
            # If turning off, restore the main integration time immediately
            if not checked:
                us = int(self.spinbox_exposure.value() * 1000)
                self.hardware_thread.set_integration_time_us(us)

    def _on_fusion_toggle(self, checked: bool) -> None:
        Config.set("fusion_enabled", bool(checked))
        self._fusion.configure(fusion_enabled=bool(checked))
        self._fusion.reset()

    def _set_fusion(self, key: str, value) -> None:
        """Persist one fusion parameter and push it into the live engine."""
        Config.set(key, value)
        self._fusion.configure(**{key: value})

    def apply_reference_overlay(self, name: str) -> None:
        Config.set("reference_library", name)
        if name == "None" or name not in self.reference_library:
            self.reference_curve.setVisible(False)
        else:
            self._update_reference_curve_scale(name)
            self.reference_curve.setVisible(True)

    def _rebuild_image_transform(self) -> None:
        """Recompute the ImageItem QTransform so the heatmap x-axis maps
        correctly to wavelength for the currently active pixel grid.
        Must be called whenever ``heatmap_linear_waves`` changes (device switch,
        initial build).
        """
        transform = QTransform()
        transform.translate(self.heatmap_linear_waves[0], 0)
        x_scale = ((self.heatmap_linear_waves[-1] - self.heatmap_linear_waves[0])
                   / max(len(self.heatmap_linear_waves) - 1, 1))
        transform.scale(x_scale, 1.0)
        self.image_item.setTransform(transform)
        self.heatmap_canvas.setXRange(
            self.heatmap_linear_waves[0], self.heatmap_linear_waves[-1], padding=0)

    def _update_reference_curve_scale(self, name: str | None = None) -> None:
        """Scale reference curve to 90% of the current visible Y maximum."""
        if name is None:
            name = self.combo_reference.currentText()
        if name == "None" or name not in self.reference_library:
            return
        # Use the current plot Y range as the scaling target so the reference
        # always fits the displayed spectrum regardless of signal level.
        _, (y_lo, y_hi) = self.plot_canvas.getViewBox().viewRange()
        scale = max(y_hi * 0.9, 50.0)
        scaled = self.reference_library[name] * scale
        self.reference_curve.setData(self.active_wavelengths, scaled)

    # ──────────────── Scope overlay cluster ────────────────
    def _sync_overlay_buttons(self) -> None:
        froze = self.freeze_xy is not None
        self.btn_freeze.setText("✕  Frozen" if froze else "❄  Freeze")
        states = ((self.btn_freeze, froze),
                  (self.btn_diff, self.overlay_diff),
                  (self.btn_peak_hold, self.overlay_peak_hold),
                  (self.btn_persist, self.overlay_persist))
        for b, on in states:
            b.setChecked(on)
            b.setObjectName("toolToggleOn" if on else "toolToggleOff")
            b.style().unpolish(b); b.style().polish(b)
        self.btn_csvbg_clear.setVisible(self.csvbg_xy is not None)

    def toggle_freeze(self) -> None:
        if self.freeze_xy is not None:
            self.freeze_xy = None
            self.overlay_diff = False           # diff needs a freeze
        else:
            live = self.current_averaged_pixels
            if live is not None and len(live) > 0:
                self.freeze_xy = (np.array(self.active_wavelengths, dtype=float),
                                  np.array(live, dtype=float))
        self._sync_overlay_buttons()
        self._redraw_overlays()

    def toggle_diff(self) -> None:
        if not self.overlay_diff and self.freeze_xy is None:
            self._sync_overlay_buttons()        # ignore: nothing to diff against
            return
        self.overlay_diff = not self.overlay_diff
        self._sync_overlay_buttons()
        self._redraw_overlays()

    def toggle_peak_hold(self) -> None:
        self.overlay_peak_hold = not self.overlay_peak_hold
        self.peak_hold_env = None               # (re)arm on any toggle
        self._sync_overlay_buttons()
        self._redraw_overlays()

    def toggle_persist(self) -> None:
        self.overlay_persist = not self.overlay_persist
        if not self.overlay_persist:
            self.persist_ring = []
        self._sync_overlay_buttons()
        self._redraw_overlays()

    def load_csv_background(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self, "Load CSV background reference", "",
            "CSV files (*.csv *.txt);;All files (*)")
        if not path:
            return
        try:
            xs, ys = _load_two_column_csv(path)
        except Exception as e:
            QMessageBox.warning(self, "CSV background",
                                f"Could not parse file:\n{e}")
            return
        self.csvbg_xy = (xs, ys)
        self.lbl_csvbg.setText(f"{os.path.basename(path)} · {len(xs)} pts")
        self._sync_overlay_buttons()
        self._redraw_overlays()

    def clear_csv_background(self) -> None:
        self.csvbg_xy = None
        self.lbl_csvbg.setText("")
        self._sync_overlay_buttons()
        self._redraw_overlays()

    def _advance_overlays(self, live: np.ndarray) -> None:
        """Advance stateful overlays on a NEW frame (peak-hold max, persistence
        ring). Call once per frame, before _redraw_overlays()."""
        n = len(live)
        if self.overlay_peak_hold:
            if self.peak_hold_env is None or len(self.peak_hold_env) != n:
                self.peak_hold_env = np.array(live, dtype=float)
            else:
                np.maximum(self.peak_hold_env, live, out=self.peak_hold_env)
        if (self.overlay_persist and self.prev_live is not None
                and len(self.prev_live) == n):
            self.persist_ring.insert(0, self.prev_live)
            del self.persist_ring[self.persist_n:]
        self.prev_live = np.array(live, dtype=float) if n else None

    def _freeze_aligned(self):
        """The frozen snapshot resampled onto the current wavelength axis (used
        as-is when the pixel count matches, i.e. same device)."""
        if self.freeze_xy is None:
            return None
        fx, fy = self.freeze_xy
        wl = self.active_wavelengths
        if len(fy) == len(wl):
            return fy
        return np.interp(wl, fx, fy)            # device changed → resample

    def _redraw_overlays(self) -> None:
        """Recompute the derived overlay curves from current state. Safe to call
        while paused — does not advance peak-hold / persistence."""
        wl = self.active_wavelengths
        live = self.current_averaged_pixels
        n = len(wl)

        # CSV background — interp onto axis, normalise, scale to 90% of Y max.
        if self.csvbg_xy is not None:
            cx, cy = self.csvbg_xy
            interp = np.interp(wl, cx, cy, left=np.nan, right=np.nan)
            finite = np.isfinite(interp)
            mx = float(np.nanmax(interp)) if np.any(finite) else 0.0
            if mx > 0:
                _, (_, y_hi) = self.plot_canvas.getViewBox().viewRange()
                interp = interp * (max(y_hi * 0.9, 50.0) / mx)
            self.csvbg_curve.setData(wl, interp, connect="finite")
            self.csvbg_curve.setVisible(True)
        else:
            self.csvbg_curve.setVisible(False)

        # Freeze (grey, absolute ADC).
        fz = self._freeze_aligned()
        if fz is not None:
            self.freeze_curve.setData(wl, fz)
            self.freeze_curve.setVisible(True)
        else:
            self.freeze_curve.setVisible(False)

        # Difference live − freeze (magenta, signed).
        if (self.overlay_diff and fz is not None
                and len(fz) == len(live) == n):
            diff = np.asarray(live, dtype=float) - np.asarray(fz, dtype=float)
            self.diff_curve.setData(wl, diff)
            self.diff_curve.setVisible(True)
            # Extend the Y floor so negative differences stay visible.
            dmin = float(np.min(diff))
            if dmin < Y_AXIS_BOTTOM_MARGIN_ADC:
                _, (_, y_hi) = self.plot_canvas.getViewBox().viewRange()
                self.plot_canvas.setYRange(dmin * 1.1, y_hi, padding=0)
        else:
            self.diff_curve.setVisible(False)

        # Peak-hold envelope (yellow).
        if (self.overlay_peak_hold and self.peak_hold_env is not None
                and len(self.peak_hold_env) == n):
            self.env_curve.setData(wl, self.peak_hold_env)
            self.env_curve.setVisible(True)
        else:
            self.env_curve.setVisible(False)

        # Persistence trails (faded teal echoes of recent frames).
        for k, c in enumerate(self.persist_curves):
            if (self.overlay_persist and k < len(self.persist_ring)
                    and len(self.persist_ring[k]) == n):
                c.setData(wl, self.persist_ring[k])
                c.setVisible(True)
            else:
                c.setVisible(False)

    def toggle_dark_correction(self, checked: bool) -> None:
        self.is_dark_correction_enabled = checked
        Config.set("dark_correction", bool(checked))

    def apply_dark_mode(self, *args) -> None:
        self.dark_correction_mode = self.combo_dark_mode.currentData()
        is_param = (self.dark_correction_mode == DARK_MODE_PARAMETRIC)
        self.spinbox_dark_bias.setEnabled(is_param)
        self.spinbox_dark_rate.setEnabled(is_param)
        Config.set("dark_mode", self.dark_correction_mode)

    def toggle_auto_y_axis(self, checked: bool) -> None:
        self.is_auto_y_axis_enabled = checked
        Config.set("auto_y", bool(checked))
        if not checked:
            self.plot_canvas.setYRange(
                Y_AXIS_BOTTOM_MARGIN_ADC, self.spinbox_y_max.value(), padding=0)

    def toggle_despeckle_filter(self, checked: bool) -> None:
        self.is_despeckle_enabled = checked
        Config.set("hot_pixel_filter", bool(checked))

    def toggle_spatial_smoothing(self, checked: bool) -> None:
        self.is_spatial_smoothing_enabled = checked
        Config.set("spatial_smoothing", bool(checked))

    def _device_default_response_table(self):
        """The connected backend's built-in RESPONSE_TABLE, or None."""
        return getattr(self.hardware_thread, "RESPONSE_TABLE", None)

    def _refresh_response_combo(self) -> None:
        """Populate the response-correction dropdown for the connected device
        and restore its persisted selection (without re-triggering apply)."""
        dev = self.combo_backend.currentText()
        has_default = self._device_default_response_table() is not None
        items = calprof.selection_items(dev, has_default)
        sel = calprof.active_selection(dev)
        if sel not in items:
            sel = calprof.SEL_NONE
        self.combo_response.blockSignals(True)
        self.combo_response.clear()
        self.combo_response.addItems(items)
        self.combo_response.setCurrentText(sel)
        self.combo_response.blockSignals(False)
        self._sync_response_delete_enabled()

    def _on_response_selection(self, selection: str) -> None:
        """User picked a correction in the dropdown — persist + apply live."""
        if not selection:
            return
        dev = self.combo_backend.currentText()
        calprof.set_active_selection(dev, selection)
        self.apply_response_correction()
        self._sync_response_delete_enabled()

    def _sync_response_delete_enabled(self) -> None:
        """Enable delete only for a real saved profile (not None / Device
        default)."""
        if not hasattr(self, "button_response_delete"):
            return
        dev = self.combo_backend.currentText()
        sel = self.combo_response.currentText()
        self.button_response_delete.setEnabled(sel in calprof.list_profiles(dev))

    def _on_response_delete(self) -> None:
        """Delete the selected saved response-correction profile for this device
        (mirrors the web client). If the deleted profile was active, fall back to
        None and rebuild the live gain."""
        dev = self.combo_backend.currentText()
        name = self.combo_response.currentText()
        if name not in calprof.list_profiles(dev):
            return
        if QMessageBox.question(
                self, "Delete profile",
                f"Delete response-correction profile \u201c{name}\u201d for {dev}?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No) != QMessageBox.StandardButton.Yes:
            return
        calprof.delete_profile(dev, name)
        if calprof.active_selection(dev) == name:
            calprof.set_active_selection(dev, calprof.SEL_NONE)
        self._refresh_response_combo()   # repopulates + re-syncs delete-enable
        self.apply_response_correction()

    def apply_response_correction(self) -> None:
        """Rebuild active_response_gain from the device's current selection and
        update the enable flag. Used on connect, on dropdown change, and after a
        new profile is saved. None -> ones (no-op)."""
        dev = self.combo_backend.currentText()
        sel = calprof.active_selection(dev)
        gain, eff = calprof.build_gain_for_selection(
            self.active_wavelengths, dev, sel,
            device_default_table=self._device_default_response_table())
        self.active_response_gain = gain
        self.is_response_comp_enabled = (eff != calprof.SEL_NONE)

    def capture_snapshot(self):
        """Return (wavelengths, intensities) of the current dark-corrected,
        response-UNcorrected averaged spectrum — what the calibration wizards
        measure. Copies so the caller can accumulate safely."""
        return (np.array(self.active_wavelengths, dtype=float),
                np.array(self._pixels_pre_response, dtype=float))

    def _update_spec_overlay(self) -> None:
        """Position the out-of-spec shading from the connected device's
        guaranteed range. Hidden when no device is connected or the range is
        unset. Bands fill to the plot edges; boundary lines mark the exact
        spec limits."""
        lo = hi = None
        bk = self.hardware_thread
        if bk is not None:
            try:
                lo, hi = bk.spec_range_nm
            except Exception:
                lo = hi = None
        show_lo = lo is not None
        show_hi = hi is not None
        # Bound the bands to the current view (plus one span of margin) rather
        # than ±1e9. Huge fixed bounds lose float precision when zoomed in very
        # close and the fill can flip to the wrong side; view-relative bounds
        # near the data scale stay stable while still reaching the plot edge.
        try:
            (vx0, vx1), _ = self.plot_canvas.getViewBox().viewRange()
        except Exception:
            vx0, vx1 = 0.0, 2000.0
        pad = max(vx1 - vx0, 1.0)
        if show_lo:
            self.spec_region_lo.setRegion((vx0 - pad, float(lo)))
            self.spec_line_lo.setPos(float(lo))
        if show_hi:
            self.spec_region_hi.setRegion((float(hi), vx1 + pad))
            self.spec_line_hi.setPos(float(hi))
        self.spec_region_lo.setVisible(show_lo)
        self.spec_line_lo.setVisible(show_lo)
        self.spec_region_hi.setVisible(show_hi)
        self.spec_line_hi.setVisible(show_hi)

    def sync_active_axis(self, wl) -> bool:
        """Adopt a new per-pixel wavelength axis (after a live wavelength
        calibration) and keep everything indexed by wavelength consistent with
        it. Crucially this RE-INTERPOLATES the reference library onto the new
        axis, so reference lines stay pinned to their TRUE wavelengths (Hg
        546.1 nm stays at 546.1) instead of sliding along with the measured
        spectrum — otherwise both move together and the fit can't be judged.
        No-op if the axis is unchanged or the pixel count doesn't match.
        Returns True if the axis was actually updated."""
        wl = np.asarray(wl, dtype=float)
        if wl.size != self.active_pixel_count or np.array_equal(
                wl, self.active_wavelengths):
            return False
        self.active_wavelengths = wl
        self.heatmap_linear_waves = np.linspace(
            wl[0], wl[-1], self.active_pixel_count)
        self._cie_mask = ((wl >= 380) & (wl <= 780))
        self._rebuild_image_transform()
        self._load_reference_library()          # reference back onto true nm
        if self.combo_reference.currentText() != "None":
            self._update_reference_curve_scale()
        return True

    def open_calibration_dialog(self) -> None:
        from gui_calibration import CalibrationDialog
        dlg = CalibrationDialog(self)
        dlg.exec()
        # Re-apply: a new profile may have been saved / selected, or the
        # wavelength axis recalibrated.
        self._refresh_response_combo()
        self.apply_response_correction()
        if self.hardware_thread is not None:
            # Keep the reference library pinned to true wavelengths if the axis
            # changed (also handled live during the fit — this covers the final
            # state on close).
            self.sync_active_axis(self.hardware_thread.wavelength_array)

    def _on_toolbar_peaks_toggled(self, checked: bool) -> None:
        self.is_peak_finding_enabled = checked
        Config.set("show_peaks", bool(checked))
        if not checked:
            self.scatter_peaks.clear()
            self._clear_peak_labels()
        self._sync_peak_measure_buttons()

    def _on_toolbar_measure_toggled(self, checked: bool) -> None:
        self.is_measure_mode_enabled = checked
        Config.set("measure_mode", bool(checked))
        self.measure_line_a.setVisible(checked)
        self.measure_line_b.setVisible(checked)
        self.measure_label.setVisible(checked)
        if checked:
            self.update_measurement_display()
        self._sync_peak_measure_buttons()

    def update_measurement_display(self) -> None:
        if not self.is_measure_mode_enabled:
            return
        pa = self.measure_line_a.value()
        pb = self.measure_line_b.value()
        Config.set("measure_a_nm", float(pa))
        Config.set("measure_b_nm", float(pb))
        ia = (np.abs(self.active_wavelengths - pa)).argmin()
        ib = (np.abs(self.active_wavelengths - pb)).argmin()
        va = self.current_averaged_pixels[ia]
        vb = self.current_averaged_pixels[ib]
        dx = abs(self.active_wavelengths[ib] - self.active_wavelengths[ia])
        dy = abs(vb - va)
        self.measure_label.setHtml(
            f'<div style="background:rgba(0,0,0,0.55);padding:3px 8px;'
            f'border:1px solid {T().WARN};border-radius:4px;'
            f'font-family:{FONT_MONO};font-size:11px;color:{T().WARN};">'
            f'A {self.active_wavelengths[ia]:.1f}  ·  B {self.active_wavelengths[ib]:.1f}  ·  '
            f'Δλ {dx:.2f} nm  ·  Δy {dy:.0f} ADC'
            f'</div>')
        x_c = (pa + pb) / 2.0
        view_y_max = self.plot_canvas.getViewBox().viewRange()[1][1]
        self.measure_label.setPos(x_c, view_y_max * 0.97)

    def _clear_peak_labels(self) -> None:
        for t in self.peak_labels:
            self.plot_canvas.removeItem(t)
        self.peak_labels.clear()

    def _on_peak_param_changed(self, *args) -> None:
        Config.update({
            "peak_savgol_window":  int(self.spinbox_sg_window.value()),
            "peak_savgol_order":   int(self.spinbox_sg_order.value()),
            "peak_prominence":     float(self.spinbox_prominence.value()),
            "peak_min_distance_px": int(self.spinbox_min_dist.value()),
            "peak_max_count":      int(self.spinbox_peak_count.value()),
        })

    def _snap_y_to_peak(self) -> None:
        """Set Y max to the current frame peak and disable auto-scale."""
        if self.current_averaged_pixels is not None and len(self.current_averaged_pixels):
            peak = int(np.max(self.current_averaged_pixels))
            peak = max(peak, 10)   # guard: no signal yet / fully dark
            self.spinbox_y_max.setValue(peak)
            # spinbox.valueChanged → apply_manual_y_axis → disables auto-Y + sets range

    def _apply_fusion_config(self) -> None:
        """Push all fusion_* config values into the fusion engine."""
        self._fusion.configure(**{
            k: Config.get(k, FUSION_DEFAULTS[k]) for k in FUSION_DEFAULTS
        })

    def apply_manual_y_axis(self, max_y: int) -> None:
        Config.set("y_max_adc", int(max_y))
        if self.is_auto_y_axis_enabled:
            self.button_auto_y_axis.blockSignals(True)
            self.button_auto_y_axis.setChecked(False)
            self.button_auto_y_axis.blockSignals(False)
            self.is_auto_y_axis_enabled = False
            Config.set("auto_y", False)
        self.plot_canvas.setYRange(Y_AXIS_BOTTOM_MARGIN_ADC, max_y, padding=0)

    def apply_axes_limits(self) -> None:
        xmin = self.spinbox_x_min.value()
        xmax = self.spinbox_x_max.value()
        if xmin >= xmax:
            return
        Config.update({"x_min_nm": int(xmin), "x_max_nm": int(xmax)})
        self.plot_canvas.setXRange(xmin, xmax, padding=0)
        self.heatmap_canvas.setXRange(xmin, xmax, padding=0)

    # ════════════════════════════════════════════════════════════
    # OB / FPS / Sensor temp readout
    # ════════════════════════════════════════════════════════════
    def update_ob_readout(self) -> None:
        ob = self.latest_ob_mean
        integration_ms = self.latest_integration_us / 1000.0
        if ob <= 0.0:
            self.status_dark.setText("— ADC")
            self.status_integration.setText("— ms")
            self._restyle_sensor_chip("neutral", "Sensor: —")
            return
        self.status_dark.setText(f"{ob:.2f} ADC")
        self.status_integration.setText(f"{integration_ms:.1f} ms")

        thermal_part = ob - (DEFAULT_DARK_RATE_ADC_PER_SEC * integration_ms / 1000.0)
        delta = thermal_part - OB_TEMP_REFERENCE_ADC
        if delta < 1.0:
            self._restyle_sensor_chip("ok", "Sensor: cold baseline")
        elif delta < 4.0:
            self._restyle_sensor_chip("warn", f"Sensor: {delta:+.1f} ADC · slightly warm")
        else:
            self._restyle_sensor_chip("danger", f"Sensor: {delta:+.1f} ADC · warm")

    # ════════════════════════════════════════════════════════════
    # TIME-LAPSE HEATMAP
    # ════════════════════════════════════════════════════════════
    def _update_heatmap_span(self) -> None:
        """Show the wall-clock time the buffer spans = frames × effective
        interval. The effective interval is the LARGER of 'Log every' and the
        current frame period — exposure is not subtracted from the log interval;
        if a frame takes longer than the interval, every frame is logged (you
        can't log a frame that hasn't been acquired yet)."""
        if not hasattr(self, "lbl_hm_span"):
            return
        frame_period = (1.0 / self._current_fps) if self._current_fps > 0 else 0.0
        eff = max(self._heatmap_measure_ms / 1000.0, frame_period)
        if eff <= 0.0:
            self.lbl_hm_span.setText("Span —")
            return
        secs = self.heatmap_size * eff
        if secs < 90:
            txt = f"{secs:.0f} s"
        elif secs < 5400:
            txt = f"{secs / 60:.1f} min"
        else:
            txt = f"{secs / 3600:.1f} h"
        self.lbl_hm_span.setText(f"Span ≈ {txt}")

    # ── Autosave (streams every logged frame to disk, independent of the
    # on-screen ring buffer, so a long run isn't limited to its size) ──
    def _on_autosave_toggled(self, checked: bool) -> None:
        Config.set("heatmap_autosave_enabled", bool(checked))
        if checked:
            self._start_autosave()
        else:
            self._stop_autosave()

    def _start_autosave(self) -> None:
        self._stop_autosave()
        path, path_err = export_paths.export_path("timelapse_autosave", "csv",
                                                  self.export_meta())
        try:
            self._autosave_writer = HeatmapAutosaveWriter(
                path, self.heatmap_linear_waves,
                fsync_interval_s=Config.get("heatmap_autosave_fsync_s", 20))
            self.lbl_hm_autosave.setText(f"→ {os.path.basename(path)}")
            if path_err and hasattr(self, "lbl_export_status"):
                self.lbl_export_status.setText(f"⚠ {path_err}")
        except OSError as e:
            self._autosave_writer = None
            self.chk_hm_autosave.setChecked(False)
            QMessageBox.warning(self, "Autosave failed",
                                f"Could not open the autosave file:\n{e}")

    def _stop_autosave(self) -> None:
        if self._autosave_writer is not None:
            self._autosave_writer.close()
            self._autosave_writer = None
        self.lbl_hm_autosave.setText("")

    # ── Rewind / scrub ──
    # Shared box metrics so the Live/Export buttons render exactly the same
    # height as the Buffer spinbox field. The field renders 33px (its up/down
    # subcontrols bump it past the plain box formula), so min-height 27 + 2px
    # padding + 1px border ⇒ 33 to match.
    _HM_BTN_BOX = ("border-radius:5px; padding:2px 12px; min-height:27px; "
                   "font-size:12px; font-weight:500;")

    def _hm_style_live_button(self, mode: str) -> None:
        """mode: 'live' (green, idle) or 'active' (red — peeking at the live
        frame or scrubbing history). The frame-export button always mirrors
        this: exportable exactly when a specific frame is on screen."""
        if mode == "live":
            fg, br, bg, bgh = "#8fc79a", "#3c5a42", "rgba(143,199,154,0.10)", "rgba(143,199,154,0.20)"
        else:
            fg, br, bg, bgh = "#e0958c", "#6b3e38", "rgba(224,149,140,0.12)", "rgba(224,149,140,0.22)"
        self.btn_hm_live.setStyleSheet(
            f"QPushButton {{ color:{fg}; border:1px solid {br}; background:{bg}; "
            f"{self._HM_BTN_BOX} }}"
            f"QPushButton:hover {{ background:{bgh}; }}")
        self._hm_style_export_button(mode == "active")

    def _hm_style_export_button(self, enabled: bool) -> None:
        """Purple-tinted (matches Save PNG) when a specific frame is on screen
        and exportable; muted/disabled while idle-live."""
        self.btn_hm_export_frame.setEnabled(enabled)
        if enabled:
            fg, br, bg, bgh = "#b49ad6", "#4c4066", "rgba(180,154,214,0.10)", "rgba(180,154,214,0.20)"
            self.btn_hm_export_frame.setStyleSheet(
                f"QPushButton {{ color:{fg}; border:1px solid {br}; background:{bg}; "
                f"{self._HM_BTN_BOX} }}"
                f"QPushButton:hover {{ background:{bgh}; }}")
        else:
            # Same box metrics, muted disabled colours (so it stays the same
            # height as the field/Live button rather than falling back to the
            # taller global QPushButton style).
            self.btn_hm_export_frame.setStyleSheet(
                f"QPushButton {{ color:#5c6270; border:1px solid #2f333c; "
                f"background:transparent; {self._HM_BTN_BOX} }}")

    def _hm_export_frame_csv(self) -> None:
        """Export ONLY the currently viewed (scrubbed/peeked) frame, not the
        whole buffer — a single (wavelength, intensity) CSV, same convention
        as the regular single-spectrum export."""
        if not (self._hm_scrub_active or self._hm_peek):
            return
        index = self._hm_scrub_index if self._hm_scrub_active else 0
        if self._heatmap_filled == 0 or index >= self.heatmap_buffer.shape[1]:
            return
        index = min(index, self._heatmap_filled - 1)
        spec = self.heatmap_buffer[:, index]
        meta = self.export_meta()
        filename, path_err = export_paths.export_path(f"frame{index}", "csv", meta)
        try:
            with open(filename, "w", newline="") as f:
                w = csv.writer(f, delimiter=";")
                age = self.heatmap_times[index]
                age_s = (f"{time.monotonic() - age:.1f}" if np.isfinite(age) else "unknown")
                w.writerow([f"# Time-lapse frame {index}"])
                w.writerow([f"# Seconds before export"] if age_s == "unknown"
                          else [f"# Seconds before export;{age_s}"])
                w.writerow(["Wavelength_nm", "ADC_Counts"])
                for wl, v in zip(self.heatmap_linear_waves, spec):
                    w.writerow([f"{wl:.2f}", round(float(v), 2)])
            self._note_export_saved(filename, path_err)
        except OSError as e:
            QMessageBox.warning(self, "Export failed", str(e))

    def _hm_on_live_clicked(self) -> None:
        if self._hm_scrub_active:
            # Was rewound into history — Live always means "back to live".
            self._hm_go_live()
        else:
            # Already idle/live — toggle the pinned peek at the current frame.
            self._hm_peek = not self._hm_peek
            if self._hm_peek:
                self._hm_scrub_render(0)
                self._hm_style_live_button("active")
            else:
                self.plot_hm_scrub.setVisible(False)
                self.lbl_hm_scrub.setText("● LIVE")
                self._hm_style_live_button("live")

    def _hm_on_scrub(self, value: int) -> None:
        """User dragged the slider (or code moved it programmatically for the
        DVR-follow behaviour, which blocks this signal — see the push handler)."""
        if value <= 0:
            self._hm_go_live()
            return
        self._hm_peek = False          # a real historical scrub supersedes peek
        self._hm_scrub_active = True
        self._hm_scrub_index = value
        self._hm_scrub_render(value)
        self._hm_style_live_button("active")

    def _hm_go_live(self) -> None:
        self._hm_scrub_active = False
        self._hm_scrub_index = 0
        self._hm_peek = False
        self.slider_hm_scrub.blockSignals(True)
        self.slider_hm_scrub.setValue(0)
        self.slider_hm_scrub.blockSignals(False)
        self.plot_hm_scrub.setVisible(False)
        self.lbl_hm_scrub.setText("● LIVE")
        self._hm_style_live_button("live")

    def _hm_scrub_render(self, index: int) -> None:
        if index >= self.heatmap_buffer.shape[1] or self._heatmap_filled == 0:
            return
        index = min(index, self._heatmap_filled - 1)
        spec = self.heatmap_buffer[:, index]
        self.curve_hm_scrub.setData(self.heatmap_linear_waves, spec)
        self.plot_hm_scrub.setVisible(True)
        t = self.heatmap_times[index]
        if np.isfinite(t):
            elapsed = time.monotonic() - t
            age_txt = (f"{elapsed:.1f}s ago" if elapsed < 90
                       else f"{elapsed/60:.1f} min ago")
        else:
            age_txt = "unknown age"
        if self._hm_peek and index == 0:
            self.lbl_hm_scrub.setText(f"●  Frame 0 (live) · {age_txt}")
        else:
            frame_txt = "oldest" if index >= self._heatmap_filled - 1 else f"frame {index}"
            self.lbl_hm_scrub.setText(f"⏸ T-{age_txt} · {frame_txt}")
        # Mirror the position on the heatmap image with the existing cursor line.
        self.heatmap_cursor_hline.setPos(index)

    def set_heatmap_size(self, n: int) -> None:
        """Resize the rolling time-lapse buffer (snapped to 10-frame steps).
        Reallocates and clears (per the agreed behaviour) and persists."""
        n = max(10, (int(n) // 10) * 10)
        self.heatmap_size = n
        Config.set("heatmap_buffer_size", n)
        self.heatmap_buffer = np.zeros((self.active_pixel_count, n))
        self.heatmap_times = np.full(n, np.nan)
        self._heatmap_filled = 0
        self._hm_go_live()
        self.slider_hm_scrub.setRange(0, 0)
        self.heatmap_canvas.setYRange(0, n, padding=0)
        self.image_item.setImage(self.heatmap_buffer, autoLevels=False, levels=(0, 10.0))
        self._update_heatmap_span()

    def _on_heatmap_rate(self, ms: int) -> None:
        self._heatmap_measure_ms = max(0, int(ms))
        Config.set("heatmap_measure_interval_ms", self._heatmap_measure_ms)
        self._update_heatmap_span()

    def clear_heatmap(self) -> None:
        """Empty the time-lapse buffer and start over."""
        self.heatmap_buffer[:] = 0.0
        self.heatmap_times[:] = np.nan
        self._heatmap_filled = 0
        self._heatmap_last_push = 0.0
        self._hm_go_live()
        self.slider_hm_scrub.setRange(0, 0)
        self.image_item.setImage(self.heatmap_buffer, autoLevels=False, levels=(0, 10.0))

    # ════════════════════════════════════════════════════════════
    # EXPORT
    # ════════════════════════════════════════════════════════════
    def export_data_csv(self) -> None:
        is_heatmap = (self.tabs.currentWidget() is self.heatmap_tab)
        kind = "heatmap" if is_heatmap else "spectrum"
        filename, path_err = export_paths.export_path(kind, "csv", self.export_meta())
        frame_indices = None
        if is_heatmap:
            # Choose scope so a long logging session isn't dumped by accident.
            box = QMessageBox(self)
            box.setWindowTitle("Export time-lapse")
            box.setIcon(QMessageBox.Icon.Question)
            filled = self._heatmap_filled
            box.setText(f"Export which part of the time-lapse? "
                        f"({filled} frame(s) in the buffer)")
            b_vis = box.addButton("Visible window", QMessageBox.ButtonRole.AcceptRole)
            b_all = box.addButton("Full buffer", QMessageBox.ButtonRole.AcceptRole)
            box.addButton("Cancel", QMessageBox.ButtonRole.RejectRole)
            box.exec()
            clicked = box.clickedButton()
            if clicked not in (b_vis, b_all):
                return
            if clicked is b_vis:
                (_x, (y0, y1)) = self.heatmap_canvas.getViewBox().viewRange()
                lo = max(0, int(np.floor(min(y0, y1))))
                hi = min(filled, int(np.ceil(max(y0, y1))))
                frame_indices = list(range(lo, hi))
            else:
                frame_indices = list(range(filled))
        try:
            with open(filename, "w", newline="") as f:
                w = csv.writer(f, delimiter=";")
                if is_heatmap:
                    w.writerow(["Time_Frame"] +
                               [f"{wl:.2f}" for wl in self.heatmap_linear_waves])
                    for fi in frame_indices:
                        w.writerow([fi] +
                                   [round(v, 2) for v in self.heatmap_buffer[:, fi]])
                else:
                    w.writerow(["# Optical Black mean (ADC)", round(self.latest_ob_mean, 3)])
                    w.writerow(["# Integration time (ms)",
                                round(self.latest_integration_us / 1000.0, 1)])
                    w.writerow(["# Dark correction",
                                "ON" if self.is_dark_correction_enabled else "OFF"])
                    w.writerow(["# Dark mode", self.dark_correction_mode])
                    w.writerow(["# Response compensation",
                                "ON" if self.is_response_comp_enabled else "OFF"])
                    w.writerow(["Pixel_ID", "Wavelength_nm", "ADC_Counts"])
                    for i, (wl, v) in enumerate(
                            zip(self.active_wavelengths, self.current_averaged_pixels)):
                        w.writerow([i, round(wl, 2), round(v, 2)])
            self._note_export_saved(filename, path_err)
        except Exception as e:
            QMessageBox.critical(self, "Error", f"Save CSV failed: {e}")

    def export_plot_png(self) -> None:
        widget = self.tabs.currentWidget()
        if widget is self.heatmap_tab:
            kind, grab_target = "heatmap", self.heatmap_canvas
        elif widget is getattr(self, "cie_tab", None):
            kind, grab_target = "color", widget
        elif widget is getattr(self, "filter_tab", None):
            kind, grab_target = "filter", widget
        else:
            kind, grab_target = "spectrum", widget
        filename, path_err = export_paths.export_path(kind + "_plot", "png",
                                                      self.export_meta())
        try:
            grab_target.grab().save(filename, "PNG")
            self._note_export_saved(filename, path_err)
        except Exception as e:
            QMessageBox.critical(self, "Error", f"Save PNG failed: {e}")

    def _note_export_saved(self, path, path_err=None) -> None:
        """Consistent 'saved' feedback + surface a fallback-dir warning if the
        configured export folder couldn't be used."""
        import os
        msg = f"Saved:\n{path}"
        if path_err:
            msg += f"\n\n⚠ {path_err}"
            if hasattr(self, "lbl_export_status"):
                self.lbl_export_status.setText(f"⚠ {path_err}")
        QMessageBox.information(self, "Saved", msg)

    def copy_spectrum_to_clipboard(self) -> None:
        lines = ["Wavelength_nm\tIntensity_ADC"]
        for wl, v in zip(self.active_wavelengths, self.current_averaged_pixels):
            lines.append(f"{wl:.2f}\t{v:.2f}")
        QGuiApplication.clipboard().setText("\n".join(lines))
        QToolTip.showText(
            self.button_copy_clip.mapToGlobal(self.button_copy_clip.rect().bottomLeft()),
            f"Copied {len(self.active_wavelengths)} samples", self.button_copy_clip,
            self.button_copy_clip.rect(), 1800)

    # ════════════════════════════════════════════════════════════
    # DRAWING / PROCESSING
    # ════════════════════════════════════════════════════════════
    def process_new_spectrum(self, raw_pixels, ob_mean, integration_us) -> None:
        # --- DYNAMIC RESYNC: Adapt if backend pixel count differs from GUI ---
        if len(raw_pixels) != len(self.active_wavelengths):
            if self.hardware_thread:
                self.active_wavelengths = self.hardware_thread.wavelength_array
            else:
                self.active_wavelengths = np.linspace(380, 1050, len(raw_pixels))
                
            self.active_pixel_count = len(self.active_wavelengths)
            self.heatmap_linear_waves = np.linspace(
                self.active_wavelengths[0], self.active_wavelengths[-1], self.active_pixel_count)
            self.heatmap_buffer = np.zeros((self.active_pixel_count, self.heatmap_size))
            self.heatmap_times = np.full(self.heatmap_size, np.nan)
            self._heatmap_filled = 0
            self._hm_go_live()
            self.slider_hm_scrub.setRange(0, 0)
            self._cie_mask = (self.active_wavelengths >= 380) & (self.active_wavelengths <= 780)
            self._rebuild_image_transform()

            self.apply_response_correction()
            
            self._load_reference_library()
            if self.combo_reference.currentText() != "None":
                self._update_reference_curve_scale()

            # Pixel count changed: pixel-count-bound overlay state is stale.
            self.peak_hold_env = None
            self.persist_ring = []
            self.prev_live = None

            self.spectrum_history.clear()
            self.current_averaged_pixels = np.zeros(self.active_pixel_count)


        # FPS
        now = time.perf_counter()
        if self._last_frame_time is not None:
            dt = now - self._last_frame_time
            if dt > 0:
                self._fps_history.append(1.0 / dt)
                if self._fps_history:
                    fps = float(np.mean(self._fps_history))
                    self._current_fps = fps
                    self.status_fps.setText(f"{fps:.1f} Hz")
                    self._update_heatmap_span()
                    if self.hardware_thread is not None:
                        self._restyle_live_pill(True, fps)
        self._last_frame_time = now

        self.spectrum_history.append(raw_pixels)
        # Defensive: if the deque still holds a frame of a different length
        # (device switch race), drop the stale ones rather than crashing in
        # np.mean over a ragged stack.
        if any(len(f) != len(raw_pixels) for f in self.spectrum_history):
            self.spectrum_history.clear()
            self.spectrum_history.append(raw_pixels)
        self.current_averaged_pixels = np.mean(self.spectrum_history, axis=0)
        self.latest_ob_mean = ob_mean
        self.latest_integration_us = integration_us
        self.update_ob_readout()

        # 1. Dark correction
        if self.is_dark_correction_enabled:
            if self.dark_correction_mode == DARK_MODE_OPTICAL_BLACK:
                dark_estimate = OB_CORRECTION_SLOPE * ob_mean + OB_CORRECTION_OFFSET
            else:
                dark_estimate = (self.spinbox_dark_bias.value()
                                 + self.spinbox_dark_rate.value()
                                 * integration_us / 1_000_000.0)
            self.current_averaged_pixels = np.maximum(
                0.0, self.current_averaged_pixels - dark_estimate)

        # 2. Despeckle (median)
        if self.is_despeckle_enabled:
            ws = self.spinbox_despeckle_width.value()
            if ws % 2 == 0: ws += 1
            pad = ws // 2
            padded = np.pad(self.current_averaged_pixels, (pad, pad), mode="edge")
            windows = np.lib.stride_tricks.sliding_window_view(padded, ws)
            self.current_averaged_pixels = np.median(windows, axis=1)

        # 2a. Spatial (pixel-window) smoothing — moving average along λ.
        # Device-agnostic logic lives in processing.py. Off by default; widens
        # peaks as the window grows (resolution trade-off, see tooltip).
        if self.is_spatial_smoothing_enabled:
            self.current_averaged_pixels = processing.apply_spatial_smoothing(
                self.current_averaged_pixels,
                self.spinbox_spatial_width.value())

        # 2b. Spectral response compensation
        # Multiply by the per-pixel inverse-sensitivity gain (from the selected
        # response-correction profile) so the chosen reference shape reads flat.
        # Capture the dark-corrected, pre-correction spectrum first — that is
        # what the calibration wizards measure.
        self._pixels_pre_response = self.current_averaged_pixels.copy()
        if self.is_response_comp_enabled:
            self.current_averaged_pixels = np.clip(
                self.current_averaged_pixels * self.active_response_gain, 0.0, None)

        # 3. Dynamic Y
        current_y_max = self.plot_canvas.getViewBox().viewRange()[1][1]
        ideal_heatmap_max = ADC_SATURATION_THRESHOLD
        if self.is_auto_y_axis_enabled:
            x_lo, x_hi = self.plot_canvas.getViewBox().viewRange()[0]
            vis = (self.active_wavelengths >= x_lo) & (self.active_wavelengths <= x_hi)
            if np.any(vis):
                mv = float(np.max(self.current_averaged_pixels[vis]))
                ideal_y = max(mv * 1.1, 50.0)
                ideal_heatmap_max = ideal_y
                if ideal_y > current_y_max or ideal_y < current_y_max * 0.7:
                    self.plot_canvas.setYRange(
                        Y_AXIS_BOTTOM_MARGIN_ADC, ideal_y, padding=0)
        else:
            ideal_heatmap_max = self.spinbox_y_max.value()

        # 4. Scope
        self.spectrum_curve.setData(self.active_wavelengths, self.current_averaged_pixels)
        self.spectrum_fill.setData(self.active_wavelengths, self.current_averaged_pixels)
        # Rescale reference overlay to match current Y range every frame
        if self.reference_curve.isVisible():
            self._update_reference_curve_scale()
        # Overlay cluster (CSV bg / freeze / diff / peak-hold / persistence)
        self._advance_overlays(self.current_averaged_pixels)
        self._redraw_overlays()

        # 5. Heatmap — optional measurement-rate decimation (decouples the
        # time-lapse cadence from the live frame rate for long runs; the scope
        # above still updates every frame).
        now = time.monotonic()
        interval = self._heatmap_measure_ms / 1000.0
        if interval <= 0.0 or (now - self._heatmap_last_push) >= interval:
            self._heatmap_last_push = now
            linear_spec = np.interp(self.heatmap_linear_waves,
                                    self.active_wavelengths, self.current_averaged_pixels)
            self.heatmap_buffer = np.roll(self.heatmap_buffer, 1, axis=1)
            self.heatmap_buffer[:, 0] = linear_spec
            self.heatmap_times = np.roll(self.heatmap_times, 1)
            self.heatmap_times[0] = now
            self._heatmap_filled = min(self._heatmap_filled + 1, self.heatmap_size)
            self.image_item.setImage(self.heatmap_buffer, autoLevels=False,
                                     levels=(0, max(10.0, ideal_heatmap_max)))
            if self._autosave_writer is not None:
                self._autosave_writer.write_frame(
                    datetime.now().isoformat(timespec="milliseconds"), linear_spec)
            # Keep the rewind slider's range covering the filled history, and —
            # if paused on a past frame — keep it pinned to that SAME frame as
            # the buffer keeps rolling underneath (see _hm_scrub_advance).
            # IMPORTANT: skip all of this while the user has the handle pressed.
            # Changing setMaximum() mid-drag changes the pixel->value mapping
            # under their cursor (same mouse position now means a different
            # value), which is exactly what made the slider "jump" while
            # dragging. Their own drag already renders the right frame in real
            # time via _hm_on_scrub; we just resume housekeeping on release.
            if not self.slider_hm_scrub.isSliderDown():
                self.slider_hm_scrub.setMaximum(max(0, self._heatmap_filled - 1))
                if self._hm_scrub_active:
                    new_idx = _hm_scrub_advance(True, self._hm_scrub_index, self.heatmap_size)
                    self._hm_scrub_index = new_idx
                    self.slider_hm_scrub.blockSignals(True)
                    self.slider_hm_scrub.setValue(new_idx)
                    self.slider_hm_scrub.blockSignals(False)
                    self._hm_scrub_render(new_idx)
                elif self._hm_peek:
                    # Peek stays pinned at "newest" (always column 0 after a
                    # roll — no index advance needed, unlike a historical scrub).
                    self._hm_scrub_render(0)

        # 6. Cursors
        if self.is_measure_mode_enabled:
            self.update_measurement_display()

        # 7. Peaks (Savitzky-Golay + prominence)
        if self.is_peak_finding_enabled:
            self._update_peaks()

        # 8. CIE tab (throttled inside)
        self.cie_tab.update_measurement(self.active_wavelengths, self.current_averaged_pixels)
        self.filter_tab.update_measurement(self.active_wavelengths, self.current_averaged_pixels)

    def _update_peaks(self) -> None:
        y = self.current_averaged_pixels
        ws  = max(5, int(self.spinbox_sg_window.value()))
        if ws % 2 == 0: ws += 1
        order = min(int(self.spinbox_sg_order.value()), ws - 1)
        try:
            y_smooth = savgol_filter(y, window_length=ws, polyorder=order)
        except Exception:
            y_smooth = y

        peak_idx, props = find_peaks(
            y_smooth,
            prominence=float(self.spinbox_prominence.value()),
            distance=int(self.spinbox_min_dist.value()),
            height=float(Config.get("peak_min_height", 15.0)),
        )

        if peak_idx.size == 0:
            self.scatter_peaks.clear()
            self._clear_peak_labels()
            return

        # Sort by prominence descending; trim to max_count; then re-sort by x for labelling
        proms = props["prominences"]
        order_by_prom = np.argsort(proms)[::-1]
        max_n = int(self.spinbox_peak_count.value())
        chosen = peak_idx[order_by_prom[:max_n]]
        chosen.sort()

        px = self.active_wavelengths[chosen]
        py = self.current_averaged_pixels[chosen]
        self.scatter_peaks.setData(px, py)
        self._refresh_peak_labels(px, py)

    def _refresh_peak_labels(self, peak_x, peak_y) -> None:
        self._clear_peak_labels()
        for xv, yv in zip(peak_x, peak_y):
            t = pg.TextItem(anchor=(0.5, 1.4))
            t.setHtml(
                f'<span style="color:{T().FG1};font-family:{FONT_MONO};'
                f'font-size:10pt;font-weight:500;">{xv:.1f} nm</span>')
            t.setPos(float(xv), float(yv))
            self.plot_canvas.addItem(t, ignoreBounds=True)
            self.peak_labels.append(t)

    # ════════════════════════════════════════════════════════════
    # THEME
    # ════════════════════════════════════════════════════════════
    def apply_theme(self, mode: str) -> None:
        palette = LightPalette if mode == "light" else DarkPalette
        set_active_palette(palette)
        # Rebuild stylesheets with current palette (FONT constants already in place)
        QApplication.instance().setStyleSheet(
            build_stylesheet(palette))
        Config.set("theme", mode)

        # Toggle segmented buttons
        self.button_theme_dark.setObjectName(
            "themeSegOn" if mode == "dark"  else "themeSegOff")
        self.button_theme_light.setObjectName(
            "themeSegOn" if mode == "light" else "themeSegOff")
        for b in (self.button_theme_dark, self.button_theme_light):
            b.style().unpolish(b); b.style().polish(b)

        # Restyle plot chrome
        p = T()
        for plot in (self.plot_canvas, self.heatmap_canvas):
            plot.setBackground(p.BG0)
            for axis_name in ("bottom", "left"):
                ax = plot.getAxis(axis_name)
                ax.setPen(pg.mkPen(QColor(p.BORDER2), width=1))
                ax.setTextPen(pg.mkPen(QColor(p.FG3)))
        self.spectrum_curve.setPen(pg.mkPen(QColor(p.FG1), width=1.4))
        self.reference_curve.setPen(pg.mkPen(QColor(p.WARN), width=1.5,
                                             style=Qt.PenStyle.DashLine))
        self.scatter_peaks.setBrush(pg.mkBrush(p.ACCENT))
        self.scatter_peaks.setPen(pg.mkPen(p.BG0, width=2))
        cur_pen = pg.mkPen(QColor(p.ACCENT), style=Qt.PenStyle.DashLine, width=1)
        for ln in (self.cursor_vline, self.cursor_hline,
                   self.heatmap_cursor_vline, self.heatmap_cursor_hline):
            ln.setPen(cur_pen)
        self.measure_line_a.setPen(pg.mkPen(p.MEASURE_A, width=2,
                                            style=Qt.PenStyle.DashLine))
        self.measure_line_b.setPen(pg.mkPen(p.MEASURE_B, width=2,
                                            style=Qt.PenStyle.DashLine))
        self.spectrum_fill.update()
        self.cie_tab.apply_theme()
        self.filter_tab.apply_theme()

        # Refresh live pill + sensor chip with new palette colors
        self._restyle_live_pill(
            self.hardware_thread is not None)
        self.update_ob_readout()
        self._sync_peak_measure_buttons()

        for child in self.findChildren(ToggleSwitch):
            child.update()
        for child in self.findChildren(ConnDot):
            child.update()

    # ════════════════════════════════════════════════════════════
    def closeEvent(self, event) -> None:
        # Save anything that isn't already saved-on-change
        Config.update({
            "exposure_ms":      float(self.spinbox_exposure.value()),
            "frames_to_average": int(self.spinbox_averaging.value()),
            "x_min_nm":         int(self.spinbox_x_min.value()),
            "x_max_nm":         int(self.spinbox_x_max.value()),
            "y_max_adc":        int(self.spinbox_y_max.value()),
        })
        self._stop_autosave()
        self.disconnect_device()
        event.accept()

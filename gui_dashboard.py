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

from PyQt6.QtCore import Qt, QTimer
from PyQt6.QtGui import (
    QTransform, QColor, QFont, QPen, QBrush, QGuiApplication,
)
from PyQt6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QPushButton, QLabel, QSpinBox, QDoubleSpinBox, QMessageBox, QComboBox,
    QTabWidget, QFrame, QScrollArea, QToolTip,
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
from app_config import Config

import spectrometer_core as core
from spectrometer_core import (
    wavelength_array, PIXEL_COUNT, START_INTEGRATION_TIME_US,
    MIN_INTEGRATION_TIME_US, MAX_INTEGRATION_TIME_US,
    Y_AXIS_BOTTOM_MARGIN_ADC, ADC_SATURATION_THRESHOLD,
    HEATMAP_HISTORY_SIZE, REFERENCE_LIBRARY_FILENAME,
    DEFAULT_DARK_BIAS_ADC, DEFAULT_DARK_RATE_ADC_PER_SEC,
    OB_CORRECTION_SLOPE, OB_CORRECTION_OFFSET, OB_TEMP_REFERENCE_ADC,
    DARK_MODE_OPTICAL_BLACK, DARK_MODE_PARAMETRIC,
    SpectrometerHardwareThread, scan_usb_devices,
    connect_usb_device, free_usb_resources, ensure_reference_library_exists,
    add_help,
)


# Shorthand
def T():
    """Return the *current* palette (alias rebinds during theme switch)."""
    return theme.Theme


class DashboardWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Spectrum Analyzer — PS-2600A")
        self.resize(1480, 900)

        # ── runtime state ──
        self.spectrum_history = deque(maxlen=Config.get("frames_to_average", 1))
        self.current_averaged_pixels = np.zeros(PIXEL_COUNT)
        self.hardware_thread = None

        self.is_dark_correction_enabled = Config.get("dark_correction", False)
        self.is_despeckle_enabled       = Config.get("hot_pixel_filter", False)
        self.is_auto_y_axis_enabled     = Config.get("auto_y", True)
        self.is_peak_finding_enabled    = Config.get("show_peaks", False)
        self.is_measure_mode_enabled    = Config.get("measure_mode", False)

        self.dark_correction_mode = Config.get("dark_mode", DARK_MODE_OPTICAL_BLACK)
        self.latest_ob_mean = 0.0
        self.latest_integration_us = START_INTEGRATION_TIME_US

        # FPS rolling window
        self._fps_history = deque(maxlen=20)
        self._last_frame_time = None

        # CIE tab needs an interpolated 380-780nm slice
        self._cie_mask = (wavelength_array >= 380) & (wavelength_array <= 780)

        # Heatmap setup
        self.heatmap_linear_waves = np.linspace(
            wavelength_array[0], wavelength_array[-1], PIXEL_COUNT)
        self.heatmap_buffer = np.zeros((PIXEL_COUNT, HEATMAP_HISTORY_SIZE))

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
                    wavelength_array, csv_waves, csv_columns[name],
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

        # Live readout
        self.readout_pix = self._make_readout("PIXEL",      "—")
        self.readout_wl  = self._make_readout("WAVELENGTH", "—", unit="nm", accent=True)
        self.readout_int = self._make_readout("INTENSITY",  "—", unit="ADC")
        h.addWidget(self.readout_pix); h.addSpacing(18)
        h.addWidget(self.readout_wl);  h.addSpacing(18)
        h.addWidget(self.readout_int)
        return w

    def _make_readout(self, label, value, unit="", accent=False) -> QWidget:
        w = QWidget()
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
        self.plot_canvas.setYRange(Y_AXIS_BOTTOM_MARGIN_ADC, 100, padding=0)

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
            rateLimit=60, slot=self.handle_mouse_movement)

        self.tabs.addTab(self.plot_canvas, "Scope")

        # ── Heatmap ──
        self.heatmap_canvas = pg.PlotWidget()
        self._style_plot(self.heatmap_canvas, "Wavelength", "nm", "Frame history", "")
        self.heatmap_canvas.setXRange(
            Config.get("x_min_nm", 380), Config.get("x_max_nm", 1050), padding=0)
        self.heatmap_canvas.setYRange(0, HEATMAP_HISTORY_SIZE, padding=0)

        self.image_item = pg.ImageItem()
        transform = QTransform()
        transform.translate(self.heatmap_linear_waves[0], 0)
        x_scale = (self.heatmap_linear_waves[-1] - self.heatmap_linear_waves[0]) / PIXEL_COUNT
        transform.scale(x_scale, 1.0)
        self.image_item.setTransform(transform)
        self.image_item.setLookupTable(pg.colormap.get("inferno").getLookupTable())
        self.heatmap_canvas.addItem(self.image_item)

        hm_pen = pg.mkPen(QColor(T().ACCENT), style=Qt.PenStyle.DashLine, width=1)
        self.heatmap_cursor_vline = pg.InfiniteLine(angle=90, movable=False, pen=hm_pen)
        self.heatmap_cursor_hline = pg.InfiniteLine(angle=0,  movable=False, pen=hm_pen)
        self.heatmap_canvas.addItem(self.heatmap_cursor_vline, ignoreBounds=True)
        self.heatmap_canvas.addItem(self.heatmap_cursor_hline, ignoreBounds=True)
        self.heatmap_mouse_proxy = pg.SignalProxy(
            self.heatmap_canvas.scene().sigMouseMoved,
            rateLimit=60, slot=self.handle_heatmap_mouse_movement)
        self.tabs.addTab(self.heatmap_canvas, "Time-Lapse Heatmap")

        # ── Color tab ──
        self.cie_tab = CIETab()
        self.tabs.addTab(self.cie_tab, "Color · CCT / CRI")

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

    # ──────────────── SIDEBAR ────────────────
    def _build_sidebar(self) -> QWidget:
        outer = QWidget()
        outer.setObjectName("sidebar")
        outer.setFixedWidth(360)
        ol = QVBoxLayout(outer)
        ol.setContentsMargins(0, 0, 0, 0); ol.setSpacing(0)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        ol.addWidget(scroll, 1)

        self.control_widget = QWidget()
        self.control_widget.setObjectName("sidebar")
        scroll.setWidget(self.control_widget)
        v = QVBoxLayout(self.control_widget)
        v.setContentsMargins(0, 0, 0, 0); v.setSpacing(0)

        # ─── GO hero ───
        hero = QWidget(); hero.setObjectName("sidebar")
        hl = QVBoxLayout(hero)
        hl.setContentsMargins(14, 14, 14, 14); hl.setSpacing(10)

        self.button_pause = QPushButton("■  PAUSE")
        self.button_pause.setObjectName("goBtn")
        self.button_pause.setCheckable(True)
        self.button_pause.clicked.connect(self.toggle_measurement_pause)
        hl.addWidget(self.button_pause)

        stats = QHBoxLayout(); stats.setSpacing(6)
        self.stat_fps  = make_stat_cell("Frames / s", "—", "Hz")
        self.stat_exp  = make_stat_cell("Exposure",   "—", "ms")
        self.stat_dark = make_stat_cell("Dark lvl",   "—", "ADC")
        for s in (self.stat_fps, self.stat_exp, self.stat_dark):
            stats.addWidget(s)
        hl.addLayout(stats)
        v.addWidget(hero)
        v.addWidget(make_hsep())

        # ─── Acquisition ───
        self.spinbox_averaging = self._spin_int(
            Config.get("frames_to_average", 1), 1, 100, 1, 96)
        self.spinbox_averaging.valueChanged.connect(self.update_averaging_frames)

        self.spinbox_exposure = QDoubleSpinBox()
        self.spinbox_exposure.setRange(MIN_INTEGRATION_TIME_US / 1000.0,
                                       MAX_INTEGRATION_TIME_US / 1000.0)
        self.spinbox_exposure.setDecimals(1)
        self.spinbox_exposure.setValue(Config.get("exposure_ms",
                                                  START_INTEGRATION_TIME_US / 1000.0))
        self.spinbox_exposure.setKeyboardTracking(False)
        self.spinbox_exposure.setFixedWidth(96)
        self.spinbox_exposure.setAlignment(Qt.AlignmentFlag.AlignRight)
        self.spinbox_exposure.valueChanged.connect(self.apply_manual_exposure)

        self.button_auto_exposure = ToggleSwitch(Config.get("auto_exposure", False))
        self.button_auto_exposure.toggled.connect(self.toggle_auto_exposure)

        self.combo_reference = QComboBox()
        self.combo_reference.addItem("None")
        for key in self.reference_library.keys():
            self.combo_reference.addItem(key)
        self.combo_reference.setFixedWidth(170)
        target = Config.get("reference_library", "None")
        idx = self.combo_reference.findText(target)
        if idx >= 0:
            self.combo_reference.setCurrentIndex(idx)
        self.combo_reference.currentTextChanged.connect(self.apply_reference_overlay)

        v.addLayout(self._section("Acquisition · 04", [
            ("Exposure",          "ms", self.spinbox_exposure),
            ("Auto exposure",     None, self.button_auto_exposure),
            ("Frames to average", None, self.spinbox_averaging),
            ("Reference library", None, self.combo_reference),
        ]))

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

        v.addLayout(self._section("Display · 03", [
            ("Wavelength range", "nm", [self.spinbox_x_min, self.spinbox_x_max]),
            ("Auto-scale Y",     None, self.button_auto_y_axis),
            ("Y max",            "ADC", self.spinbox_y_max),
        ]))

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

        v.addLayout(self._section("Processing · 05", [
            ("Dark correction",  None,  self.button_dark_correct),
            ("Dark mode",        None,  self.combo_dark_mode),
            ("Bias offset",      "ADC", self.spinbox_dark_bias),
            ("Rate",           "ADC/s", self.spinbox_dark_rate),
            ("Hot-pixel filter", None,  self.button_despeckle),
            ("Smoothing width",  "px",  self.spinbox_despeckle_width),
        ]))

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

        v.addLayout(self._section("Peak detection · 05", [
            ("Savitzky-Golay window", "px", self.spinbox_sg_window),
            ("Savitzky-Golay order",  None, self.spinbox_sg_order),
            ("Prominence min",       "ADC", self.spinbox_prominence),
            ("Min peak distance",     "px", self.spinbox_min_dist),
            ("Max peak count",        None, self.spinbox_peak_count),
        ]))

        # ─── Export ───
        export_w = QWidget()
        ex_v = QVBoxLayout(export_w)
        ex_v.setContentsMargins(14, 14, 14, 18); ex_v.setSpacing(8)
        hdr = QLabel("EXPORT"); hdr.setObjectName("sectionHeader")
        ex_v.addWidget(hdr)
        row = QHBoxLayout(); row.setSpacing(6)
        self.button_save_csv = QPushButton("Save CSV")
        self.button_save_csv.clicked.connect(self.export_data_csv)
        self.button_save_png = QPushButton("Save PNG")
        self.button_save_png.clicked.connect(self.export_plot_png)
        row.addWidget(self.button_save_csv); row.addWidget(self.button_save_png)
        ex_v.addLayout(row)
        self.button_copy_clip = QPushButton("Copy spectrum to clipboard")
        self.button_copy_clip.clicked.connect(self.copy_spectrum_to_clipboard)
        ex_v.addWidget(self.button_copy_clip)
        v.addWidget(export_w)
        v.addStretch(1)

        self.apply_dark_mode()
        return outer

    def _spin_int(self, value, lo, hi, step, width=96) -> QSpinBox:
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
        w = QWidget(); w.setObjectName("sidebar"); w.setLayout(v)
        outer = QVBoxLayout(); outer.setContentsMargins(0, 0, 0, 0)
        outer.addWidget(w)
        return outer

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

        wrap1, self.status_integration = kv("Integration", "—")
        wrap2, self.status_dark        = kv("Dark", "—")
        wrap3, self.status_bias        = kv("Bias",
            f"{Config.get('dark_bias_adc', DEFAULT_DARK_BIAS_ADC):.2f} ADC")
        wrap4, self.status_rate        = kv("Rate",
            f"{Config.get('dark_rate_adc_per_s', DEFAULT_DARK_RATE_ADC_PER_SEC):.2f} ADC/s")
        wrap5, self.status_conn        = kv("Status", "Disconnected")
        for wrap in (wrap1, wrap2, wrap3, wrap4, wrap5):
            h.addWidget(wrap); h.addWidget(make_vsep())
        h.addStretch(1)

        self.chip_sensor_temp = QLabel("Sensor: —")
        self.chip_sensor_temp.setObjectName("sensorChip")
        self._restyle_sensor_chip("neutral", "Sensor: —")
        h.addWidget(self.chip_sensor_temp)
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
        add_help(self.button_auto_exposure, "Automatically adjust exposure to keep the strongest peak near target.")
        add_help(self.combo_reference, "Overlay a reference spectrum (gas lamps, fluorescents, LEDs).")
        add_help(self.button_auto_y_axis, "Auto-scale Y to the tallest visible peak.")
        add_help(self.button_dark_correct, "Enable dark-current subtraction.")
        add_help(self.combo_dark_mode, "Optical Black: per-frame correction.\nParametric: Bias + Rate · t.")
        add_help(self.button_despeckle, "Sliding-median filter to suppress hot pixels.")
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
    def refresh_device_list(self) -> None:
        self.combo_devices.clear()
        devices = scan_usb_devices()
        if not devices:
            self.combo_devices.addItem("No PASCO PS-2600A found")
            self.button_connect.setEnabled(False)
        else:
            for d in devices:
                self.combo_devices.addItem(f"PS-2600A  ({d[-20:]})", d)
            self.button_connect.setEnabled(True)

    def toggle_connection(self) -> None:
        if self.hardware_thread is not None and self.hardware_thread.isRunning():
            self.disconnect_device()
        else:
            self.connect_device()

    def connect_device(self) -> None:
        device_path = self.combo_devices.currentData()
        if not device_path:
            return
        try:
            connect_usb_device(device_path)
            self.hardware_thread = SpectrometerHardwareThread()
            self.hardware_thread.signal_new_spectrum_data.connect(self.process_new_spectrum)
            self.hardware_thread.signal_auto_exposure_adjusted.connect(self.handle_auto_exposure_update)
            self.hardware_thread.signal_connection_lost.connect(self.handle_connection_lost)
            # Push current exposure (from Config) to hardware so it reflects what the user sees
            ms = self.spinbox_exposure.value()
            self.hardware_thread.current_integration_time_us = int(ms * 1000)
            self.hardware_thread.apply_hardware_exposure_time(int(ms * 1000))
            self.hardware_thread.is_auto_exposure_active = self.button_auto_exposure.isChecked()
            self.hardware_thread.start()

            self.button_connect.setText("Disconnect")
            self.button_connect.setObjectName("dangerBtn")
            self.button_connect.style().unpolish(self.button_connect)
            self.button_connect.style().polish(self.button_connect)
            self.conn_dot.setOn(True)
            self._restyle_live_pill(True)
            self.status_conn.setText("Connected")
            self.combo_devices.setEnabled(False)
            self.button_scan.setEnabled(False)
            self.control_widget.setEnabled(True)
            self.spectrum_history.clear()
        except Exception as e:
            QMessageBox.critical(self, "Connection Error", str(e))

    def disconnect_device(self) -> None:
        if self.hardware_thread:
            self.hardware_thread.stop_thread()
            self.hardware_thread = None
        free_usb_resources()
        self.button_connect.setText("Connect")
        self.button_connect.setObjectName("primaryBtn")
        self.button_connect.style().unpolish(self.button_connect)
        self.button_connect.style().polish(self.button_connect)
        self.conn_dot.setOn(False)
        self._restyle_live_pill(False)
        self.status_conn.setText("Disconnected")
        self.combo_devices.setEnabled(True)
        self.button_scan.setEnabled(True)
        self.control_widget.setEnabled(False)
        self._clear_peak_labels()
        self.measure_label.setVisible(False)
        self.scatter_peaks.clear()
        self._set_readout(self.readout_pix, "—")
        self._set_readout(self.readout_wl,  "—")
        self._set_readout(self.readout_int, "—")

    def handle_connection_lost(self) -> None:
        self.disconnect_device()
        QMessageBox.warning(self, "Connection Lost",
                            "The device was unplugged or stopped responding.")

    # ════════════════════════════════════════════════════════════
    # MOUSE → live readout
    # ════════════════════════════════════════════════════════════
    def handle_mouse_movement(self, ev) -> None:
        pos = ev[0]
        if not self.plot_canvas.sceneBoundingRect().contains(pos):
            return
        view_pt = self.plot_canvas.getPlotItem().vb.mapSceneToView(pos)
        idx = (np.abs(wavelength_array - view_pt.x())).argmin()
        sx, sy = wavelength_array[idx], self.current_averaged_pixels[idx]
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
        sx = self.heatmap_linear_waves[lin_idx]
        hw_pixel = (np.abs(wavelength_array - sx)).argmin()
        frame_index = int(np.clip(round(view_pt.y()), 0, HEATMAP_HISTORY_SIZE - 1))
        intensity = self.heatmap_buffer[lin_idx, frame_index]
        self.heatmap_cursor_vline.setPos(sx)
        self.heatmap_cursor_hline.setPos(frame_index)
        self._set_readout(self.readout_pix, f"~{hw_pixel}")
        self._set_readout(self.readout_wl,  f"{sx:.1f}")
        self._set_readout(self.readout_int, f"{intensity:.0f}")

    # ════════════════════════════════════════════════════════════
    # CONTROL HANDLERS
    # ════════════════════════════════════════════════════════════
    def toggle_measurement_pause(self, checked: bool) -> None:
        if self.hardware_thread:
            self.hardware_thread.is_measurement_paused = checked
        if checked:
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
        self.hardware_thread.apply_hardware_exposure_time(us)
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

    def apply_reference_overlay(self, name: str) -> None:
        Config.set("reference_library", name)
        if name == "None" or name not in self.reference_library:
            self.reference_curve.setVisible(False)
        else:
            self.reference_curve.setData(wavelength_array, self.reference_library[name])
            self.reference_curve.setVisible(True)

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
        ia = (np.abs(wavelength_array - pa)).argmin()
        ib = (np.abs(wavelength_array - pb)).argmin()
        va = self.current_averaged_pixels[ia]
        vb = self.current_averaged_pixels[ib]
        dx = abs(wavelength_array[ib] - wavelength_array[ia])
        dy = abs(vb - va)
        self.measure_label.setHtml(
            f'<div style="background:rgba(0,0,0,0.55);padding:3px 8px;'
            f'border:1px solid {T().WARN};border-radius:4px;'
            f'font-family:{FONT_MONO};font-size:11px;color:{T().WARN};">'
            f'A {wavelength_array[ia]:.1f}  ·  B {wavelength_array[ib]:.1f}  ·  '
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
            update_stat_cell(self.stat_dark, "—")
            update_stat_cell(self.stat_exp, f"{integration_ms:.0f}")
            self._restyle_sensor_chip("neutral", "Sensor: —")
            return
        self.status_dark.setText(f"{ob:.2f} ADC")
        self.status_integration.setText(f"{integration_ms:.1f} ms")
        update_stat_cell(self.stat_dark, f"{ob:.1f}")
        update_stat_cell(self.stat_exp,  f"{integration_ms:.0f}")

        thermal_part = ob - (DEFAULT_DARK_RATE_ADC_PER_SEC * integration_ms / 1000.0)
        delta = thermal_part - OB_TEMP_REFERENCE_ADC
        if delta < 1.0:
            self._restyle_sensor_chip("ok", "Sensor: cold baseline")
        elif delta < 4.0:
            self._restyle_sensor_chip("warn", f"Sensor: {delta:+.1f} ADC · slightly warm")
        else:
            self._restyle_sensor_chip("danger", f"Sensor: {delta:+.1f} ADC · warm")

    # ════════════════════════════════════════════════════════════
    # EXPORT
    # ════════════════════════════════════════════════════════════
    def export_data_csv(self) -> None:
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        is_heatmap = (self.tabs.currentWidget() is self.heatmap_canvas)
        prefix = "heatmap_data" if is_heatmap else "spectrum_data"
        filename = f"{prefix}_{ts}.csv"
        try:
            with open(filename, "w", newline="") as f:
                w = csv.writer(f, delimiter=";")
                if is_heatmap:
                    w.writerow(["Time_Frame"] +
                               [f"{wl:.2f}" for wl in self.heatmap_linear_waves])
                    for fi in range(HEATMAP_HISTORY_SIZE):
                        w.writerow([fi] +
                                   [round(v, 2) for v in self.heatmap_buffer[:, fi]])
                else:
                    w.writerow(["# Optical Black mean (ADC)", round(self.latest_ob_mean, 3)])
                    w.writerow(["# Integration time (ms)",
                                round(self.latest_integration_us / 1000.0, 1)])
                    w.writerow(["# Dark correction",
                                "ON" if self.is_dark_correction_enabled else "OFF"])
                    w.writerow(["# Dark mode", self.dark_correction_mode])
                    w.writerow(["Pixel_ID", "Wavelength_nm", "ADC_Counts"])
                    for i, (wl, v) in enumerate(
                            zip(wavelength_array, self.current_averaged_pixels)):
                        w.writerow([i, round(wl, 2), round(v, 2)])
            QMessageBox.information(self, "Success", f"Saved:\n{filename}")
        except Exception as e:
            QMessageBox.critical(self, "Error", f"Save CSV failed: {e}")

    def export_plot_png(self) -> None:
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        widget = self.tabs.currentWidget()
        prefix = {
            self.plot_canvas:    "spectrum_plot",
            self.heatmap_canvas: "heatmap_plot",
        }.get(widget, "cie_plot")
        filename = f"{prefix}_{ts}.png"
        try:
            widget.grab().save(filename, "PNG")
            QMessageBox.information(self, "Success", f"Saved:\n{filename}")
        except Exception as e:
            QMessageBox.critical(self, "Error", f"Save PNG failed: {e}")

    def copy_spectrum_to_clipboard(self) -> None:
        lines = ["Wavelength_nm\tIntensity_ADC"]
        for wl, v in zip(wavelength_array, self.current_averaged_pixels):
            lines.append(f"{wl:.2f}\t{v:.2f}")
        QGuiApplication.clipboard().setText("\n".join(lines))
        QToolTip.showText(
            self.button_copy_clip.mapToGlobal(self.button_copy_clip.rect().bottomLeft()),
            f"Copied {len(wavelength_array)} samples", self.button_copy_clip,
            self.button_copy_clip.rect(), 1800)

    # ════════════════════════════════════════════════════════════
    # DRAWING / PROCESSING
    # ════════════════════════════════════════════════════════════
    def process_new_spectrum(self, raw_pixels, ob_mean, integration_us) -> None:
        # FPS
        now = time.perf_counter()
        if self._last_frame_time is not None:
            dt = now - self._last_frame_time
            if dt > 0:
                self._fps_history.append(1.0 / dt)
                if self._fps_history:
                    fps = float(np.mean(self._fps_history))
                    update_stat_cell(self.stat_fps, f"{fps:.1f}")
                    if self.hardware_thread is not None and self.hardware_thread.isRunning():
                        self._restyle_live_pill(True, fps)
        self._last_frame_time = now

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

        # 3. Dynamic Y
        current_y_max = self.plot_canvas.getViewBox().viewRange()[1][1]
        ideal_heatmap_max = ADC_SATURATION_THRESHOLD
        if self.is_auto_y_axis_enabled:
            x_lo, x_hi = self.plot_canvas.getViewBox().viewRange()[0]
            vis = (wavelength_array >= x_lo) & (wavelength_array <= x_hi)
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
        self.spectrum_curve.setData(wavelength_array, self.current_averaged_pixels)
        self.spectrum_fill.setData(wavelength_array, self.current_averaged_pixels)

        # 5. Heatmap
        linear_spec = np.interp(self.heatmap_linear_waves,
                                wavelength_array, self.current_averaged_pixels)
        self.heatmap_buffer = np.roll(self.heatmap_buffer, 1, axis=1)
        self.heatmap_buffer[:, 0] = linear_spec
        self.image_item.setImage(self.heatmap_buffer, autoLevels=False,
                                 levels=(0, max(10.0, ideal_heatmap_max)))

        # 6. Cursors
        if self.is_measure_mode_enabled:
            self.update_measurement_display()

        # 7. Peaks (Savitzky-Golay + prominence)
        if self.is_peak_finding_enabled:
            self._update_peaks()

        # 8. CIE tab (throttled inside)
        self.cie_tab.update_measurement(wavelength_array, self.current_averaged_pixels)

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

        px = wavelength_array[chosen]
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

        # Refresh live pill + sensor chip with new palette colors
        self._restyle_live_pill(
            self.hardware_thread is not None and self.hardware_thread.isRunning())
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
        self.disconnect_device()
        event.accept()

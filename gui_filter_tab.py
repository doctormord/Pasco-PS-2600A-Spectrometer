"""
Filter tab — optical-filter characterization (LP / SP / BP / notch / ND).

Workflow: with the light source running and NO filter in the beam, press
"Set baseline" to capture the 100% reference. Insert the filter; the tab then
shows live transmission T(λ) = sample / baseline (%) plus the classic metrics
(type, centre wavelength, FWHM, 50% edges, 10–90% edge steepness, passband-avg
transmission, blocking / optical density). "Save report" writes a PDF or PNG
with the plot and a metrics table.

The physics lives in the device-agnostic ``filter_analysis`` module (shared with
the web app); this file is only the Qt/pyqtgraph view.
"""

from __future__ import annotations
import os
import numpy as np

from PyQt6.QtCore import Qt
from PyQt6.QtGui import QColor, QFont
from PyQt6.QtWidgets import (
    QWidget, QHBoxLayout, QVBoxLayout, QLabel, QGridLayout, QPushButton,
    QFileDialog, QMessageBox,
)
import pyqtgraph as pg

from gui_theme import (
    FONT_MONO, make_stat_cell, update_stat_cell, make_hsep, make_vsep,
)
import gui_theme as gt
from filter_analysis import analyze_filter, metrics_rows


def _T():
    return gt.Theme


# Ordered metric labels — must match the keys emitted by metrics_rows() so the
# fixed cell set can be shown/hidden per filter type without rebuilding layouts.
_METRIC_LABELS = [
    "Filter type", "Valid range",
    "Peak transmission", "Peak wavelength",
    "Centre wavelength", "FWHM / bandwidth",
    "Left edge (50%)", "Right edge (50%)",
    "Cut-on (50%)", "Cut-off (50%)",
    "Edge steepness, left (10–90%)", "Edge steepness, right (10–90%)",
    "Passband avg transmission", "Min transmission",
    "Blocking min transmission", "Peak optical density (OD)",
    "OD at centre (ND)", "Avg OD (blocking)",
]


class FilterTab(QWidget):
    UPDATE_EVERY_N_FRAMES = 4

    def __init__(self, parent=None):
        super().__init__(parent)
        self._frame_count = 0
        self._baseline = None          # (wavelengths, intensities) reference
        self._latest = None            # (wavelengths, intensities) last live frame
        self._last_result = None       # last analyze_filter() dict
        self._capture = None           # in-progress baseline averaging state | None
        self.report_meta_provider = None   # set by the dashboard; () -> dict

        root = QHBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        # ── Left: transmission plot ──
        plot_wrap = QWidget()
        pv = QVBoxLayout(plot_wrap)
        pv.setContentsMargins(0, 0, 0, 0); pv.setSpacing(0)
        pv.addWidget(self._build_toolbar())

        self.plot = pg.PlotWidget()
        self._style_plot()
        self.plot.setLabel("bottom", "Wavelength", units="nm",
                           color=_T().FG3, **{"font-size": "10px"})
        self.plot.setLabel("left", "Transmission", units="%",
                           color=_T().FG3, **{"font-size": "10px"})
        self.plot.enableAutoRange(axis=pg.ViewBox.XAxis)
        self.plot.setYRange(0, 110, padding=0)

        self.t_curve = self.plot.plot(pen=pg.mkPen(QColor(_T().ACCENT), width=2))
        self.half_line = pg.InfiniteLine(
            angle=0, pos=50.0, movable=False,
            pen=pg.mkPen(QColor(_T().FG3), width=1, style=Qt.PenStyle.DashLine))
        self.plot.addItem(self.half_line, ignoreBounds=True)
        edge_pen = pg.mkPen(QColor(_T().WARN), width=1, style=Qt.PenStyle.DashLine)
        self.edge_a = pg.InfiniteLine(angle=90, movable=False, pen=edge_pen)
        self.edge_b = pg.InfiniteLine(angle=90, movable=False, pen=edge_pen)
        center_pen = pg.mkPen(QColor(_T().ACCENT), width=1, style=Qt.PenStyle.DotLine)
        self.center_line = pg.InfiniteLine(angle=90, movable=False, pen=center_pen)
        for ln in (self.edge_a, self.edge_b, self.center_line):
            ln.setVisible(False)
            self.plot.addItem(ln, ignoreBounds=True)

        self.hint = pg.TextItem(
            "Set a baseline (light source without filter) to begin.",
            color=_T().FG3, anchor=(0.5, 0.5))
        self.plot.addItem(self.hint, ignoreBounds=True)
        self._position_hint()

        pv.addWidget(self.plot, 1)
        root.addWidget(plot_wrap, 1)

        # ── Right: metrics panel ──
        root.addWidget(make_vsep(height=10))
        side = QWidget(); side.setFixedWidth(330)
        sv = QVBoxLayout(side)
        sv.setContentsMargins(14, 12, 14, 12); sv.setSpacing(8)

        hdr = QLabel("FILTER METRICS"); hdr.setObjectName("statLbl")
        sv.addWidget(hdr)

        grid_wrap = QWidget()
        self.metrics_grid = QGridLayout(grid_wrap)
        self.metrics_grid.setContentsMargins(0, 0, 0, 0)
        self.metrics_grid.setHorizontalSpacing(8)
        self.metrics_grid.setVerticalSpacing(8)
        self._cells: dict[str, object] = {}
        for i, label in enumerate(_METRIC_LABELS):
            cell = make_stat_cell(label, "—")
            cell.setVisible(False)
            self._cells[label] = cell
            self.metrics_grid.addWidget(cell, i // 2, i % 2)
        sv.addWidget(grid_wrap)

        sv.addWidget(make_hsep())
        self.notes = QLabel("")
        self.notes.setWordWrap(True)
        self.notes.setObjectName("rowLbl")
        sv.addWidget(self.notes)
        sv.addStretch(1)
        root.addWidget(side)

    # ── UI builders ──
    def _build_toolbar(self) -> QWidget:
        w = QWidget(); w.setObjectName("scopeOverlayBar")
        h = QHBoxLayout(w)
        h.setContentsMargins(10, 6, 10, 6); h.setSpacing(8)

        self.btn_baseline = QPushButton("◎  Set baseline")
        self.btn_baseline.setObjectName("primaryBtn")
        self.btn_baseline.clicked.connect(self.set_baseline)
        self.btn_baseline.setToolTip(
            "Capture the current spectrum as the 100% reference (light source "
            "with NO filter in the beam).")

        self.btn_clear = QPushButton("Clear")
        self.btn_clear.setObjectName("ghostBtn")
        self.btn_clear.clicked.connect(self.clear_baseline)
        self.btn_clear.setEnabled(False)

        self.btn_report = QPushButton("⤓  Save report")
        self.btn_report.setObjectName("ghostBtn")
        self.btn_report.clicked.connect(self.save_report)
        self.btn_report.setEnabled(False)

        self.btn_csv = QPushButton("⤓  CSV")
        self.btn_csv.setObjectName("ghostBtn")
        self.btn_csv.clicked.connect(self.save_csv)
        self.btn_csv.setEnabled(False)
        self.btn_csv.setToolTip("Export the filter metrics as raw data (CSV).")

        self.lbl_state = QLabel("No baseline")
        self.lbl_state.setObjectName("readoutLbl")

        h.addWidget(self.btn_baseline)
        h.addWidget(self.btn_clear)
        h.addWidget(make_vsep())
        h.addWidget(self.btn_report)
        h.addWidget(self.btn_csv)
        h.addWidget(self.lbl_state)
        h.addStretch(1)
        return w

    def _style_plot(self) -> None:
        self.plot.setBackground(_T().BG0)
        self.plot.showGrid(x=True, y=True, alpha=0.10)
        for axis in ("bottom", "left"):
            ax = self.plot.getAxis(axis)
            ax.setPen(pg.mkPen(QColor(_T().BORDER2), width=1))
            ax.setTextPen(pg.mkPen(QColor(_T().FG3)))
            f = QFont("JetBrains Mono"); f.setPointSize(9)
            ax.setTickFont(f)
            ax.enableAutoSIPrefix(False)

    def _position_hint(self) -> None:
        self.hint.setPos(0.5, 55.0)   # placed in data coords; recentred on range

    # ── Data path ──
    def set_baseline(self) -> None:
        if self._latest is None or len(self._latest[1]) == 0:
            QMessageBox.information(self, "Filter baseline",
                                   "No live spectrum yet — connect a device first.")
            return
        from app_config import Config
        n = max(1, int(Config.get("filter_baseline_frames", 16)))
        if n <= 1:
            wl, inten = self._latest
            self._baseline = (np.array(wl, dtype=float), np.array(inten, dtype=float))
            self._finish_baseline()
            return
        # Average the next N live frames into the reference. The reference is
        # static, so averaging cuts its noise ~√N without costing spectral
        # resolution — and that noise would otherwise enter every later T = s/ref.
        self._capture = {"wl": None, "sum": None, "count": 0, "target": n}
        self.btn_baseline.setEnabled(False)
        self.lbl_state.setText(f"Averaging… 0/{n}")

    def _accumulate_baseline(self, wl, inten) -> None:
        cap = self._capture
        if cap["sum"] is None or len(inten) != len(cap["sum"]):
            cap["wl"] = wl.copy(); cap["sum"] = inten.astype(float).copy(); cap["count"] = 1
        else:
            cap["sum"] += inten; cap["count"] += 1
        self.lbl_state.setText(f"Averaging… {cap['count']}/{cap['target']}")
        if cap["count"] >= cap["target"]:
            self._baseline = (cap["wl"].copy(), cap["sum"] / cap["count"])
            self._capture = None
            self._finish_baseline(averaged=cap["target"])

    def _finish_baseline(self, averaged: int = 1) -> None:
        self.btn_baseline.setEnabled(True)
        self.btn_clear.setEnabled(True)
        self.lbl_state.setText(f"Baseline set (avg {averaged})" if averaged > 1
                               else "Baseline set")
        self.hint.setVisible(False)
        self._frame_count = 0
        self._recompute()

    def clear_baseline(self) -> None:
        self._capture = None
        self.btn_baseline.setEnabled(True)
        self._baseline = None
        self._last_result = None
        self.btn_clear.setEnabled(False)
        self.btn_report.setEnabled(False)
        self.btn_csv.setEnabled(False)
        self.lbl_state.setText("No baseline")
        self.t_curve.setData([], [])
        for ln in (self.edge_a, self.edge_b, self.center_line):
            ln.setVisible(False)
        for cell in self._cells.values():
            cell.setVisible(False)
        self.notes.setText("")
        self.hint.setVisible(True)

    def update_measurement(self, wavelengths_nm, intensities) -> None:
        wl = np.asarray(wavelengths_nm, dtype=float)
        inten = np.asarray(intensities, dtype=float)
        self._latest = (wl, inten)
        if self._capture is not None:
            self._accumulate_baseline(wl, inten)
            return
        if self._baseline is None:
            return
        self._frame_count += 1
        if self._frame_count % self.UPDATE_EVERY_N_FRAMES != 0:
            return
        self._recompute()

    def _recompute(self) -> None:
        if self._baseline is None or self._latest is None:
            return
        bwl, bref = self._baseline
        wl, smp = self._latest
        ref = bref if len(bref) == len(wl) else np.interp(wl, bwl, bref)
        try:
            res = analyze_filter(wl, ref, smp)
        except Exception:
            return
        self._last_result = res
        self.btn_report.setEnabled(True)
        self.btn_csv.setEnabled(True)

        T_pct = res["transmission"] * 100.0
        # Optional DISPLAY-ONLY smoothing. Metrics (edges, FWHM, OD …) stay on the
        # raw transmission in `res`, so smoothing never distorts the numbers — it
        # only calms the drawn curve. Off by default (filter_display_smoothing=0).
        disp = T_pct
        from app_config import Config
        sm = int(Config.get("filter_display_smoothing", 0))
        if sm and sm > 1:
            from filter_analysis import smooth_for_display
            disp = smooth_for_display(T_pct, sm)
        self.t_curve.setData(wl, disp, connect="finite")

        # pyqtgraph's deferred X auto-range can fail to apply when this plot is
        # first populated while its tab is still hidden (it then sticks at the
        # default [0,1] and the curve sits off-screen — the matplotlib report
        # looks fine because it autoscales independently). Fit X when the current
        # view doesn't overlap the data; a user zoom that does overlap is left be.
        if wl.size:
            (x0, x1), _ = self.plot.getViewBox().viewRange()
            lo, hi = float(np.nanmin(wl)), float(np.nanmax(wl))
            if x1 <= lo or x0 >= hi:
                self.plot.setXRange(lo, hi, padding=0.02)

        # Edge / centre guide lines
        edges = [res.get("left_edge_nm"), res.get("right_edge_nm"),
                 res.get("cut_on_nm"), res.get("cut_off_nm")]
        edges = [e for e in edges if e is not None]
        self.edge_a.setVisible(len(edges) >= 1)
        if edges:
            self.edge_a.setPos(edges[0])
        self.edge_b.setVisible(len(edges) >= 2)
        if len(edges) >= 2:
            self.edge_b.setPos(edges[1])
        c = res.get("center_wavelength_nm")
        self.center_line.setVisible(c is not None)
        if c is not None:
            self.center_line.setPos(c)

        self._populate_metrics(res)
        self.notes.setText(res.get("notes", ""))

    def _populate_metrics(self, res: dict) -> None:
        rows = dict(metrics_rows(res))
        for label, cell in self._cells.items():
            if label in rows:
                update_stat_cell(cell, rows[label])
                cell.setVisible(True)
            else:
                cell.setVisible(False)

    # ── Report export ──
    def save_report(self) -> None:
        if self._last_result is None:
            QMessageBox.information(self, "Filter report", "Nothing to export yet.")
            return
        path, _ = QFileDialog.getSaveFileName(
            self, "Save filter report", "filter_report.pdf",
            "PDF document (*.pdf);;PNG image (*.png)")
        if not path:
            return
        try:
            meta = self.report_meta_provider() if callable(self.report_meta_provider) else None
            _write_report_matplotlib(path, self._last_result, meta=meta)
            self.lbl_state.setText(f"Saved {os.path.basename(path)}")
        except ImportError:
            # matplotlib not available → graph PNG via pyqtgraph + metrics CSV.
            try:
                png = os.path.splitext(path)[0] + ".png"
                csv = os.path.splitext(path)[0] + "_metrics.csv"
                _export_plot_png(self.plot, png)
                _write_metrics_csv(csv, self._last_result)
                QMessageBox.information(
                    self, "Filter report",
                    "matplotlib not installed — saved the graph as PNG and the "
                    f"metrics as CSV instead:\n{os.path.basename(png)}\n"
                    f"{os.path.basename(csv)}")
                self.lbl_state.setText(f"Saved {os.path.basename(png)}")
            except Exception as e:
                QMessageBox.warning(self, "Filter report", f"Export failed:\n{e}")
        except Exception as e:
            QMessageBox.warning(self, "Filter report", f"Export failed:\n{e}")

    def save_csv(self) -> None:
        if self._last_result is None:
            QMessageBox.information(self, "Filter CSV", "Nothing to export yet.")
            return
        path, _ = QFileDialog.getSaveFileName(
            self, "Save filter metrics (CSV)", "filter_metrics.csv",
            "CSV file (*.csv)")
        if not path:
            return
        if not path.lower().endswith(".csv"):
            path += ".csv"
        try:
            _write_metrics_csv(path, self._last_result)
            self.lbl_state.setText(f"Saved {os.path.basename(path)}")
        except Exception as e:
            QMessageBox.warning(self, "Filter CSV", f"Export failed:\n{e}")

    def apply_theme(self) -> None:
        self._style_plot()
        self.t_curve.setPen(pg.mkPen(QColor(_T().ACCENT), width=2))
        self.half_line.setPen(pg.mkPen(QColor(_T().FG3), width=1,
                                       style=Qt.PenStyle.DashLine))
        self.hint.setColor(_T().FG3)


# ════════════════════════════════════════════════════════════
# Report helpers (module-level so they can be unit-tested headless)
# ════════════════════════════════════════════════════════════
def _write_report_matplotlib(path: str, res: dict, meta: dict | None = None) -> None:
    """Write a PDF/PNG (chosen by file extension) using the shared report
    renderer in filter_analysis (plot + full metrics table, white background).
    Raises ImportError if matplotlib is unavailable (caller falls back)."""
    from filter_analysis import render_report
    fmt = "png" if path.lower().endswith(".png") else "pdf"
    render_report(path, res, fmt=fmt, meta=meta)


def _export_plot_png(plot_widget, path: str) -> None:
    from pyqtgraph.exporters import ImageExporter
    exporter = ImageExporter(plot_widget.plotItem)
    exporter.export(path)


def _write_metrics_csv(path: str, res: dict) -> None:
    import csv
    with open(path, "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["Metric", "Value"])
        for k, v in metrics_rows(res, full=True):
            w.writerow([k, v])

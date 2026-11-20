"""
gui_calibration.py — Calibration wizards (desktop).

A single modal dialog with five tabs, device-agnostic in their logic
(the maths lives in calibration_profiles.py / calibration_utils.py; this file
is only Qt glue + live capture):

  • Wavelength — measure a line lamp (Cd / Hg / Ne / Ar …), detect peaks,
    assign them to known line wavelengths, fit a degree-3 polynomial, inspect
    residuals AND line coverage (an honest extrapolation warning), then persist
    the coefficients to the device's config key.

  • Response — measure a broadband lamp on the device under test and divide by a
    reference of the SAME lamp (captured live on a reference device, or loaded
    from a CSV) to build a relative spectral-sensitivity table. Saved as a named
    per-device profile and selectable from the main window's dropdown.

Live spectra are pulled from the dashboard's capture_snapshot(), which returns
the dark-corrected, response-UNcorrected averaged spectrum.
"""

from __future__ import annotations

import csv
import os
import threading
import time

import numpy as np
import pyqtgraph as pg

from PyQt6.QtCore import Qt, QTimer
from PyQt6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QGridLayout, QLabel, QPushButton,
    QComboBox, QSpinBox, QLineEdit, QTableWidget, QTableWidgetItem,
    QHeaderView, QTabWidget, QWidget, QMessageBox, QFileDialog, QAbstractItemView,
    QCheckBox, QListWidget, QListWidgetItem, QStackedWidget, QDoubleSpinBox,
    QPlainTextEdit, QFormLayout, QInputDialog, QDialogButtonBox,
)

import calibration_capture as cc
import calibration_profiles as calprof
import calibration_workflow as cw
import reference_sources as rs
import gui_theme as gt


def _T():
    """Current palette (alias rebinds during a theme switch)."""
    return gt.Theme


# Poll interval for live capture. Only genuinely NEW frames are accumulated
# (the dashboard snapshot is sampled faster than the frame rate, so a naive
# poll would average the same frame repeatedly and give no sqrt(N) benefit).
_CAPTURE_INTERVAL_MS = 80


def _csv_write_spectrum(path, wl, inten):
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["wavelength_nm", "intensity"])
        for x, y in zip(wl, inten):
            w.writerow([f"{float(x):.4f}", f"{float(y):.6g}"])


def _csv_read_spectrum(path):
    wl, it = [], []
    with open(path, "r", newline="", encoding="utf-8") as f:
        r = csv.reader(f)
        for row in r:
            if not row or len(row) < 2:
                continue
            try:
                x = float(row[0]); y = float(row[1])
            except ValueError:
                continue  # header / comment
            wl.append(x); it.append(y)
    if len(wl) < 2:
        raise ValueError("CSV needs at least two numeric wavelength,intensity rows.")
    return np.asarray(wl, float), np.asarray(it, float)


class _Capturer:
    """Accumulate N genuinely-new dark-corrected snapshots from the dashboard
    into an average (noise ~/sqrt(N)). Duplicate polls of the same frame are
    skipped, so the result is sqrt(N) independent of the live frame rate. Calls
    done(wl, inten) when N new frames are collected, or on a safety timeout with
    whatever was gathered."""

    def __init__(self, dash, frames, status_label, done):
        self.dash = dash
        self.frames = max(1, int(frames))
        self.status = status_label
        self.done = done
        self._acc = None
        self._wl = None
        self._last = None        # last accumulated frame (for new-frame detection)
        self._n = 0
        self._ticks = 0
        # Safety: stop even if frames stall. Generous so a slow device (long
        # exposure) still reaches N before timing out.
        self._max_ticks = max(self.frames * 30, 200)
        self._timer = QTimer()
        self._timer.setInterval(_CAPTURE_INTERVAL_MS)
        self._timer.timeout.connect(self._tick)

    def start(self):
        self._acc = None
        self._wl = None
        self._last = None
        self._n = 0
        self._ticks = 0
        self._timer.start()

    def _tick(self):
        self._ticks += 1
        wl, inten = self.dash.capture_snapshot()
        is_new = (self._last is None) or (not np.array_equal(inten, self._last))
        if is_new and float(np.max(inten)) > 0.0:
            if self._acc is None or self._acc.shape != inten.shape:
                self._acc = np.zeros_like(inten)
            self._acc += inten
            self._wl = wl
            self._last = inten
            self._n += 1
            if self.status:
                self.status.setText(f"Averaging… {self._n}/{self.frames}")
        if self._n >= self.frames or self._ticks >= self._max_ticks:
            self._timer.stop()
            if self._n == 0:
                # Nothing arrived — hand back the latest snapshot so the caller
                # can warn about a dark/idle device.
                self.done(np.asarray(wl, float), np.asarray(inten, float))
            else:
                self.done(np.asarray(self._wl, float), self._acc / self._n)


class CalibrationDialog(QDialog):
    def __init__(self, dashboard):
        super().__init__(dashboard)
        self.dash = dashboard
        self.setWindowTitle("Device calibration")
        self.setMinimumSize(820, 600)
        # The app stylesheet themes backgrounds by objectName (#central, #sidebar
        # …); a bare QDialog and its unnamed tab-page QWidgets have none, so they
        # fall back to the light system palette. Give them a scoped dark
        # background — selectors are limited to QDialog and #calTabPage so button
        # / combo / table styling is left untouched.
        self.setStyleSheet(
            f"QDialog {{ background-color: {_T().BG1}; }}"
            f"QWidget#calTabPage {{ background-color: {_T().BG1}; }}")

        # captured data
        self._wl_capture = None          # (wl, inten) of the line lamp
        self._wl_peaks = None            # cached sub-pixel peaks of the capture
        self._resume_after_capture = False  # re-pause the device after a capture
        self._wl_pairs = []              # [(pixel, known_nm)]
        # Snapshot the axis as it is on open, so Close (without Save) discards any
        # live preview and a rejected fit can be rolled back — no broken live view.
        _bk = self._backend()
        self._wl_open_snap = _bk.wl_calibration_snapshot() if _bk is not None else None
        self._wl_saved = False
        self._wl_last_fit = None         # dict from calibrate_from_lines
        self._ref = None                 # (wl, inten) response reference
        self._meas = None                # (wl, inten) response measurement
        self._resp_table = None          # computed Nx2 [[lambda, S]]

        root = QVBoxLayout(self)
        self.tabs = QTabWidget()
        self._busy = False
        self.tab_wizard = _SpectralWizardTab(self)
        self.tab_profiles = _ProfilesTab(self)
        self.tab_lamps = _LampsTab(self)
        self.tabs.addTab(self._build_wavelength_tab(), "Wavelength")
        self.tabs.addTab(self.tab_wizard, "Spectral calibration")
        self.tabs.addTab(self.tab_profiles, "Profiles")
        self.tabs.addTab(self.tab_lamps, "Lamps")
        self.tabs.addTab(self._build_response_tab(), "Response (legacy)")
        self.tabs.currentChanged.connect(self._on_tab_changed)
        root.addWidget(self.tabs, 1)

        self.btn_close = QPushButton("Close")
        self.btn_close.clicked.connect(self.accept)
        row = QHBoxLayout(); row.addStretch(1); row.addWidget(self.btn_close)
        root.addLayout(row)

    # ── busy handling (a wizard step runs in a worker thread) ────────────
    def set_busy(self, busy: bool) -> None:
        self._busy = bool(busy)
        self.btn_close.setEnabled(not busy)
        for i in range(self.tabs.count()):
            if self.tabs.widget(i) is not self.tab_wizard:
                self.tabs.setTabEnabled(i, not busy)

    def done(self, r):
        if self._busy:
            return
        super().done(r)

    def _on_tab_changed(self, _i):
        w = self.tabs.currentWidget()
        for t in (self.tab_wizard, self.tab_profiles, self.tab_lamps):
            if w is t and hasattr(t, "refresh"):
                t.refresh()

    def notify_profiles_changed(self) -> None:
        fn = getattr(self.dash, "on_calibration_profiles_changed", None)
        if callable(fn):
            try:
                fn()
            except Exception as e:
                print(f"[calib] on_calibration_profiles_changed failed: {e}")
        for t in (self.tab_wizard, self.tab_profiles):
            t.refresh()

    # ── helpers ──────────────────────────────────────────────────────────
    def _device_name(self) -> str:
        return self.dash.combo_backend.currentText()

    def _backend(self):
        return self.dash.hardware_thread

    def _is_streaming(self) -> bool:
        return self._backend() is not None

    def _capture_frames(self) -> int:
        from app_config import Config
        return max(1, int(Config.get("calibration_capture_frames", 16)))

    def _resume_for_capture(self) -> None:
        """A live capture needs fresh frames; if the user left acquisition
        paused, resume it now and remember to re-pause when the capture ends."""
        dash = self.dash
        if hasattr(dash, "is_measurement_paused") and dash.is_measurement_paused():
            self._resume_after_capture = True
            dash.set_measurement_paused(False)
        else:
            self._resume_after_capture = False

    def _restore_pause_after_capture(self) -> None:
        if self._resume_after_capture:
            self._resume_after_capture = False
            if hasattr(self.dash, "set_measurement_paused"):
                self.dash.set_measurement_paused(True)

    def _style_cal_plot(self, plot, default_x=(300.0, 1050.0)):
        """Match the app chrome and fix the empty-plot axis.

        An empty pyqtgraph view shows a bare 0..1 range on BOTH axes until data
        is plotted — that is what the old black plot's "0.1 … 1.0" ticks were,
        NOT a real wavelength axis and NOT a CCD-stepping artefact. Here we give
        the plot the themed background + axes, a small top/right margin so the
        top tick label is not clipped, and a sensible nm default range for the
        empty state (the connected device's span, or a wide fallback). Captures
        call plot.autoRange() afterwards to frame the actual data."""
        plot.setBackground(_T().BG0)
        plot.getPlotItem().setContentsMargins(6, 10, 14, 6)
        for axis in ("bottom", "left"):
            ax = plot.getAxis(axis)
            ax.setPen(pg.mkPen(_T().BORDER2, width=1))
            ax.setTextPen(pg.mkPen(_T().FG3))
        plot.showGrid(x=True, y=True, alpha=0.12)
        wl = getattr(self.dash, "active_wavelengths", None)
        if wl is not None and len(wl) > 1:
            lo, hi = float(wl[0]), float(wl[-1])
        else:
            lo, hi = default_x
        plot.setXRange(lo, hi, padding=0.02)

    def _style_cal_table(self, table):
        """Dark-theme the pixel↔λ table. The global stylesheet doesn't reach
        QTableWidget here, so its viewport and header render bare white in dark
        mode — style them explicitly from the palette (header sections, corner
        button, grid, selection and the inline cell editor)."""
        t = _T()
        table.setAlternatingRowColors(True)
        table.setStyleSheet(f"""
            QTableWidget {{
                background: {t.BG1};
                alternate-background-color: {t.BG2};
                color: {t.FG1};
                gridline-color: {t.BORDER1};
                border: 1px solid {t.BORDER1};
                border-radius: 6px;
                selection-background-color: {t.BG3};
                selection-color: {t.FG1};
            }}
            QTableWidget::item {{ padding: 2px 6px; }}
            QTableWidget::item:selected {{ background: {t.BG3}; color: {t.FG1}; }}
            QTableWidget QLineEdit {{
                background: {t.BG3}; color: {t.FG1};
                border: 1px solid {t.ACCENT}; selection-background-color: {t.ACCENT};
            }}
            QHeaderView::section {{
                background: {t.BG2};
                color: {t.FG3};
                padding: 4px 8px;
                border: none;
                border-bottom: 1px solid {t.BORDER1};
                border-right: 1px solid {t.BORDER1};
                font-weight: 500;
            }}
            QTableCornerButton::section {{
                background: {t.BG2};
                border: none;
                border-bottom: 1px solid {t.BORDER1};
            }}
        """)

    # ══════════════════════════════════════════════════════════════════════
    #  WAVELENGTH TAB
    # ══════════════════════════════════════════════════════════════════════
    def _build_wavelength_tab(self) -> QWidget:
        w = QWidget(); w.setObjectName("calTabPage"); v = QVBoxLayout(w)

        intro = QLabel(
            "Measure a discharge lamp, assign detected peaks to known lines, "
            "then fit pixel → wavelength. Use lamps that cover the whole range "
            "(combine e.g. Hg + Ne + Ar) — a fit only constrains the span its "
            "lines cover; outside it the axis is extrapolated.")
        intro.setWordWrap(True)
        v.addWidget(intro)

        top = QHBoxLayout()
        top.addWidget(QLabel("Lamp:"))
        self.cmb_lamp = QComboBox()
        self.cmb_lamp.addItems(list(calprof.CALIBRATION_LINES.keys()))
        # Selecting a lamp re-assigns lines against the last capture (no re-measure).
        self.cmb_lamp.currentIndexChanged.connect(self._wl_rematch)
        top.addWidget(self.cmb_lamp)
        top.addSpacing(12)
        top.addWidget(QLabel("Degree:"))
        self.spn_degree = QSpinBox(); self.spn_degree.setRange(2, 3); self.spn_degree.setValue(3)
        top.addWidget(self.spn_degree)
        top.addSpacing(12)
        # Multi-lamp: when checked, a Capture or a lamp switch ADDS its matched
        # lines to the table instead of replacing it — so you can build a
        # full-span fit from several lamps (Cd+Hg, then Ne/Ar) sequentially.
        self.chk_accum = QCheckBox("Add to existing (multi-lamp)")
        self.chk_accum.setToolTip(
            "Off: each capture / lamp replaces the line table (single lamp).\n"
            "On: each capture or lamp switch appends its lines, so you can\n"
            "combine several lamps into one full-range fit. Pixel positions are\n"
            "stable across a lamp swap, so mixing lamps in one fit is correct.")
        top.addWidget(self.chk_accum)
        top.addStretch(1)
        self.btn_wl_capture = QPushButton("Capture && detect peaks")
        self.btn_wl_capture.clicked.connect(self._wl_capture_clicked)
        top.addWidget(self.btn_wl_capture)
        self.btn_wl_clear = QPushButton("Clear")
        self.btn_wl_clear.setToolTip("Remove all assigned lines and the current "
                                     "capture — start the lamp selection over.")
        self.btn_wl_clear.clicked.connect(self._wl_clear_clicked)
        top.addWidget(self.btn_wl_clear)
        v.addLayout(top)

        self.plot_wl = pg.PlotWidget()
        self.plot_wl.setLabel("bottom", "Wavelength", units="nm")
        self.plot_wl.setLabel("left", "Intensity", units="ADC")
        self.plot_wl.setMinimumHeight(180)
        self._style_cal_plot(self.plot_wl)
        v.addWidget(self.plot_wl, 1)

        mid = QHBoxLayout()
        self.tbl_wl = QTableWidget(0, 2)
        self.tbl_wl.setHorizontalHeaderLabels(["Pixel", "Known λ (nm)"])
        self.tbl_wl.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self.tbl_wl.setEditTriggers(QAbstractItemView.EditTrigger.AllEditTriggers)
        self._style_cal_table(self.tbl_wl)
        mid.addWidget(self.tbl_wl, 1)

        btns = QVBoxLayout()
        b_rm = QPushButton("Remove row"); b_rm.clicked.connect(self._wl_remove_row)
        b_add = QPushButton("Add row");   b_add.clicked.connect(lambda: self._wl_add_row(0.0, 0.0))
        self.btn_wl_fit = QPushButton("Fit && apply"); self.btn_wl_fit.clicked.connect(self._wl_fit_clicked)
        self.chk_wl_confirm = QCheckBox("Back up && confirm")
        self.chk_wl_confirm.setToolTip(
            "Required before saving. Writes a timestamped backup of the current "
            "calibration (old_calib_data_<datetime>.json, next to the config) "
            "before the new one overwrites it.")
        self.btn_wl_save = QPushButton("Save to device"); self.btn_wl_save.clicked.connect(self._wl_save_clicked)
        self.btn_wl_save.setEnabled(False)
        for b in (b_add, b_rm, self.btn_wl_fit):
            btns.addWidget(b)
        btns.addWidget(self.chk_wl_confirm)
        btns.addWidget(self.btn_wl_save)
        btns.addStretch(1)
        mid.addLayout(btns)
        v.addLayout(mid, 1)

        self.lbl_wl_result = QLabel("No fit yet.")
        self.lbl_wl_result.setWordWrap(True)
        self.lbl_wl_result.setTextFormat(Qt.TextFormat.RichText)
        v.addWidget(self.lbl_wl_result)
        return w

    def _wl_add_row(self, pixel, nm):
        r = self.tbl_wl.rowCount()
        self.tbl_wl.insertRow(r)
        self.tbl_wl.setItem(r, 0, QTableWidgetItem(f"{float(pixel):.2f}"))
        self.tbl_wl.setItem(r, 1, QTableWidgetItem(f"{float(nm):.2f}"))

    def _wl_remove_row(self):
        r = self.tbl_wl.currentRow()
        if r >= 0:
            self.tbl_wl.removeRow(r)

    def _wl_table_pairs(self):
        pairs = []
        for r in range(self.tbl_wl.rowCount()):
            try:
                px = float(self.tbl_wl.item(r, 0).text())
                nm = float(self.tbl_wl.item(r, 1).text())
            except (AttributeError, ValueError):
                continue
            pairs.append((px, nm))
        return pairs

    def _wl_capture_clicked(self):
        if not self._is_streaming():
            QMessageBox.warning(self, "Not connected",
                                "Connect and start a device first.")
            return
        self.btn_wl_capture.setEnabled(False)
        self.btn_wl_capture.setText("Capturing…")
        self._resume_for_capture()
        self._cap = _Capturer(self.dash, self._capture_frames(), self.lbl_wl_result,
                              self._wl_capture_done)
        self._cap.start()

    def _wl_capture_done(self, wl, inten):
        self.btn_wl_capture.setEnabled(True)
        self.btn_wl_capture.setText("Capture && detect peaks")
        self._restore_pause_after_capture()
        self._wl_capture = (wl, inten)
        if float(np.max(inten)) < 1.0:
            self._wl_peaks = None
            self.lbl_wl_result.setText(
                "<b>Captured signal is ~0.</b> Is the lamp on and the exposure set?")
            return
        # detect peaks once; keep them so switching the lamp dropdown can
        # re-assign against the SAME capture without re-measuring.
        self._wl_peaks = calprof.detect_peaks(inten, min_distance_px=10)
        self._wl_rematch()

    def _wl_merge_pairs(self, pairs, dedup_px: float = 4.0):
        """Add (pixel, nm) pairs to the table, skipping any whose pixel is within
        dedup_px of a row already present (so re-matching the same lamp, or an
        overlapping line from another lamp, doesn't duplicate a row). Returns the
        number actually added."""
        existing = [p for p, _ in self._wl_table_pairs()]
        added = 0
        for px, nm in pairs:
            if any(abs(px - e) <= dedup_px for e in existing):
                continue
            self._wl_add_row(px, nm)
            existing.append(px)
            added += 1
        return added

    def _wl_rematch(self):
        """(Re)assign the selected lamp's known lines to the peaks of the last
        capture. Wired to the lamp dropdown so picking a lamp pulls that lamp's
        lines. With 'Add to existing' checked, the matches are APPENDED (build a
        multi-lamp fit); otherwise they REPLACE the table (single lamp)."""
        if self._wl_capture is None or not getattr(self, "_wl_peaks", None):
            return
        wl, _inten = self._wl_capture
        peaks = self._wl_peaks
        lamp = self.cmb_lamp.currentText()
        known = [k for k in calprof.CALIBRATION_LINES[lamp]
                 if wl[0] <= k <= wl[-1]]
        pairs = calprof.auto_match_lines(peaks, wl, known, tol_nm=8.0)
        accum = self.chk_accum.isChecked()
        if accum:
            added = self._wl_merge_pairs(pairs)
        else:
            self.tbl_wl.setRowCount(0)
            for px, nm in pairs:
                self._wl_add_row(px, nm)
            added = len(pairs)
        self._wl_redraw_plot(peaks)
        total = self.tbl_wl.rowCount()
        dropped = len(known) - len(pairs)
        note = (f", {dropped} skipped (no clean peak)" if dropped > 0 else "")
        verb = f"added {added}" if accum else f"assigned {len(pairs)}"
        self.lbl_wl_result.setText(
            f"Detected {len(peaks)} peaks; {verb} of {len(known)} {lamp} "
            f"lines in range{note}. Table now holds {total} line(s). "
            + ("Switch lamp / recapture with another lamp to add more, then Fit. "
               if accum else
               "Tick 'Add to existing' to combine several lamps (Cd+Hg, then "
               "Ne/Ar for the red/NIR end) into one full-span fit. ")
            + "Edit the table if needed, then Fit.")

    def _wl_clear_clicked(self):
        """Reset the line table and the current capture so the user can start
        the lamp selection over (e.g. after picking the wrong lamp)."""
        self.tbl_wl.setRowCount(0)
        self._wl_capture = None
        self._wl_peaks = None
        self._wl_last_fit = None
        self.btn_wl_save.setEnabled(False)
        self.plot_wl.clear()
        self.lbl_wl_result.setText("Cleared. Select a lamp and capture again.")

    def closeEvent(self, ev):
        """Closing without Save discards any live preview: roll the device axis
        back to how it was on open so the live view is never left in a modified
        (or broken) state. Save persists; Close cancels. Ignored while a
        calibration step runs in its worker thread."""
        if self._busy:
            ev.ignore()
            return
        try:
            bk = self._backend()
            if (bk is not None and not getattr(self, "_wl_saved", False)
                    and self._wl_open_snap is not None):
                bk.wl_calibration_restore(self._wl_open_snap)
        except Exception:
            pass
        super().closeEvent(ev)

    def _wl_redraw_plot(self, peaks=None, wl_fit=None):
        self.plot_wl.clear()
        if self._wl_capture is None:
            return
        wl, inten = self._wl_capture
        self.plot_wl.plot(wl, inten, pen=pg.mkPen((120, 180, 255), width=1))
        # assigned lines as vertical markers
        for _, nm in self._wl_table_pairs():
            self.plot_wl.addItem(pg.InfiniteLine(
                pos=nm, angle=90, pen=pg.mkPen((255, 170, 60), style=Qt.PenStyle.DashLine)))
        self.plot_wl.autoRange()

    def _wl_fit_clicked(self):
        backend = self._backend()
        pairs = self._wl_table_pairs()
        deg = self.spn_degree.value()
        if backend is None:
            QMessageBox.warning(self, "Not connected", "Connect a device first.")
            return
        if len(pairs) < deg + 1:
            QMessageBox.warning(self, "Too few lines",
                                f"A degree-{deg} fit needs at least {deg + 1} "
                                f"assigned lines (have {len(pairs)}). Add lines "
                                f"or lower the degree.")
            return
        pxs = [p for p, _ in pairs]
        nms = [n for _, n in pairs]
        # Snapshot so we can roll back if the fit is bad.
        snap = backend.wl_calibration_snapshot()
        try:
            res = backend.calibrate_from_lines(nms, pixel_positions=pxs, degree=deg)
        except Exception as e:
            QMessageBox.critical(self, "Fit failed", str(e))
            return
        if not isinstance(res, dict) or "coeffs" not in res:
            QMessageBox.information(
                self, "Not supported",
                "This device does not support wavelength calibration "
                "(its axis is fixed/virtual).")
            return

        # ── Sanity guard ──────────────────────────────────────────────────
        # A degree-3 fit through mis-assigned lines (wrong lamp, stray peak) can
        # produce a wildly wrong axis — non-monotonic or with coefficients that
        # flip the start wavelength to something absurd (e.g. +200 → −150 nm).
        # Applying that breaks the live view. Validate first; roll back if bad.
        wl_new = np.asarray(backend.wavelength_array, dtype=float)
        max_res = float(res["max_error"])
        monotonic = bool(np.all(np.diff(wl_new) > 0))
        in_band = (120.0 < wl_new[0] < 1350.0) and (200.0 < wl_new[-1] < 1400.0)
        if (not monotonic) or (not in_band) or (max_res > 8.0):
            backend.wl_calibration_restore(snap)
            if hasattr(self.dash, "sync_active_axis"):
                self.dash.sync_active_axis(backend.wavelength_array)
            self._wl_last_fit = None
            self.btn_wl_save.setEnabled(False)
            reasons = []
            if not monotonic:
                reasons.append("the wavelength axis comes out non-monotonic")
            if not in_band:
                reasons.append(f"the axis runs {wl_new[0]:.0f}–{wl_new[-1]:.0f} nm "
                               f"(outside a plausible range)")
            if max_res > 8.0:
                reasons.append(f"the max residual is {max_res:.1f} nm")
            self.lbl_wl_result.setText(
                "<b style='color:#ff6060'>Fit rejected</b> — " + "; ".join(reasons)
                + ". Almost always a mis-assigned line (wrong lamp, or a stray "
                "peak matched to the wrong wavelength). The previous axis was "
                "kept. Fix the table — remove bad rows or pick the right lamp — "
                "then Fit again.")
            self._wl_redraw_plot()
            return

        self._wl_last_fit = res
        self.btn_wl_save.setEnabled(True)

        # Live preview on the main scope. Route through sync_active_axis so the
        # reference library is re-interpolated onto the new axis in the SAME
        # step — otherwise the reference lines slide along with the spectrum in
        # the background and the fit looks wrong even when it's right.
        if hasattr(self.dash, "sync_active_axis"):
            self.dash.sync_active_axis(wl_new)
        else:
            self.dash.active_wavelengths = wl_new
        self._wl_capture = (wl_new, self._wl_capture[1])  # re-plot on new axis
        self._wl_redraw_plot()

        line_lo, line_hi = min(nms), max(nms)
        dev_lo, dev_hi = float(wl_new[0]), float(wl_new[-1])
        rms = float(np.sqrt(np.mean(np.square(res["residuals"]))))
        warn = ""
        # warn if lines leave large unconstrained spans at either end
        if line_lo - dev_lo > 60 or dev_hi - line_hi > 60:
            warn = (f"<br><span style='color:#ff8c3c'><b>Coverage warning:</b> "
                    f"lines span {line_lo:.0f}–{line_hi:.0f} nm but the device "
                    f"covers {dev_lo:.0f}–{dev_hi:.0f} nm. The axis is "
                    f"<b>extrapolated</b> outside the line span — small residuals "
                    f"do not guarantee accuracy there. Add lines from other "
                    f"lamps for full coverage.</span>")
        elif rms > 0.5:
            warn = (f"<br><span style='color:#ff8c3c'>RMS {rms:.2f} nm is a bit "
                    f"high — check for a mis-assigned line before saving.</span>")
        self.lbl_wl_result.setText(
            f"<b>Fit (degree {self.spn_degree.value()}, {len(nms)} lines):</b> "
            f"max residual {max_res:.3f} nm, RMS {rms:.3f} nm. "
            f"Applied live. {warn}")

    def _backup_calibration(self) -> str | None:
        """Write a timestamped snapshot of the current calibration-related config
        keys next to the config file, so an overwrite can always be rolled back.
        Returns the backup path (or None on failure)."""
        import os, json, datetime
        from app_config import Config
        keys = ["pasco_wl_poly_coeffs", "lr2t_wl_poly_coeffs", "ocean_wl_poly_coeffs",
                "lr2t_wl_offset_nm", "ocean_wl_offset_nm", "ocean_use_device_wavelength"]
        snap = {k: Config.get(k, None) for k in keys}
        ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        snap["_backed_up_at"] = ts
        snap["_note"] = "Calibration snapshot taken before the wizard overwrote it."
        try:
            cfg_dir = os.path.dirname(os.path.abspath(Config.path)) or "."
            path = os.path.join(cfg_dir, f"old_calib_data_{ts}.json")
            with open(path, "w", encoding="utf-8") as f:
                json.dump(snap, f, indent=2, sort_keys=True)
            return path
        except Exception as e:
            QMessageBox.warning(self, "Backup failed",
                                f"Could not write the calibration backup: {e}\n"
                                f"Nothing was saved.")
            return None

    def _wl_save_clicked(self):
        if not self._wl_last_fit:
            return
        if not self.chk_wl_confirm.isChecked():
            QMessageBox.warning(
                self, "Confirmation required",
                "Tick 'Back up & confirm' first. Saving overwrites the current "
                "wavelength calibration in the config — the checkbox makes sure "
                "a timestamped backup is written before that happens.")
            return
        dev = self._device_name()
        key = {
            "PASCO PS-2600A": "pasco_wl_poly_coeffs",
            "ASEQ / Lasertrack LR-2T": "lr2t_wl_poly_coeffs",
            "Ocean HDX-UV-VIS": "ocean_wl_poly_coeffs",
        }.get(dev)
        if key is None:
            QMessageBox.information(
                self, "Not persistable",
                f"No wavelength-coefficient config key for '{dev}'. "
                f"The fit is applied for this session but not saved.")
            return
        # Back up BEFORE overwriting; abort if the backup couldn't be written.
        backup_path = self._backup_calibration()
        if backup_path is None:
            return
        from app_config import Config
        Config.set(key, list(self._wl_last_fit["coeffs"]))
        self._wl_saved = True
        self.chk_wl_confirm.setChecked(False)   # require a fresh tick next time
        extra = ""
        # The Ocean HDX defaults to its on-device factory axis; saving a
        # calibration means "use mine now", so flip it to the config poly.
        if dev == "Ocean HDX-UV-VIS":
            Config.set("ocean_use_device_wavelength", False)
            extra = ("<br>Switched 'ocean_use_device_wavelength' → false so "
                     "this calibration is used instead of the device's factory "
                     "axis. Set it back to true to revert to the device axis.")
        import os
        QMessageBox.information(
            self, "Saved",
            f"Wavelength calibration saved to '{key}'. It will load "
            f"automatically on the next connect.<br>Backup of the previous "
            f"calibration: <b>{os.path.basename(backup_path)}</b>.{extra}")

    # ══════════════════════════════════════════════════════════════════════
    #  RESPONSE TAB
    # ══════════════════════════════════════════════════════════════════════
    def _build_response_tab(self) -> QWidget:
        w = QWidget(); w.setObjectName("calTabPage"); v = QVBoxLayout(w)

        intro = QLabel(
            "<b>Legacy method.</b> New calibrations: please use the "
            "<b>Spectral calibration</b> tab. "
            "Build a per-wavelength sensitivity correction. Measure a SMOOTH, "
            "BROADBAND lamp (halogen, white LED — never a line lamp) on the "
            "device being calibrated, and provide a reference of the SAME lamp "
            "(captured on a reference device, or loaded from a CSV). "
            "S(λ) = measured_norm / reference_norm; the saved profile is the "
            "relative sensitivity (inverted to a gain when applied). It corrects "
            "the device to the reference shape — not to an absolute scale.")
        intro.setWordWrap(True)
        v.addWidget(intro)

        grid = QGridLayout()
        grid.addWidget(QLabel("<b>Reference</b> (target shape)"), 0, 0)
        self.lbl_ref = QLabel("none")
        grid.addWidget(self.lbl_ref, 0, 1, 1, 3)
        b_ref_cap = QPushButton("Capture live"); b_ref_cap.clicked.connect(self._ref_capture)
        b_ref_load = QPushButton("Load CSV…");   b_ref_load.clicked.connect(self._ref_load)
        b_ref_save = QPushButton("Save CSV…");   b_ref_save.clicked.connect(self._ref_save)
        grid.addWidget(b_ref_cap, 1, 0); grid.addWidget(b_ref_load, 1, 1); grid.addWidget(b_ref_save, 1, 2)

        grid.addWidget(QLabel("<b>Measurement</b> (device under test)"), 2, 0)
        self.lbl_meas = QLabel("none")
        grid.addWidget(self.lbl_meas, 2, 1, 1, 3)
        b_meas_cap = QPushButton("Capture live"); b_meas_cap.clicked.connect(self._meas_capture)
        grid.addWidget(b_meas_cap, 3, 0)
        v.addLayout(grid)

        self.plot_resp = pg.PlotWidget()
        self.plot_resp.setLabel("bottom", "Wavelength", units="nm")
        self.plot_resp.setLabel("left", "Sensitivity / gain")
        self.plot_resp.addLegend()
        self.plot_resp.setMinimumHeight(200)
        self._style_cal_plot(self.plot_resp)
        v.addWidget(self.plot_resp, 1)

        bottom = QHBoxLayout()
        self.btn_compute = QPushButton("Compute correction")
        self.btn_compute.clicked.connect(self._compute_response)
        bottom.addWidget(self.btn_compute)
        bottom.addSpacing(16)
        bottom.addWidget(QLabel("Profile name:"))
        self.edit_name = QLineEdit(); self.edit_name.setPlaceholderText("e.g. 600µm UV-VIS fibre")
        bottom.addWidget(self.edit_name, 1)
        self.btn_save_profile = QPushButton("Save profile")
        self.btn_save_profile.clicked.connect(self._save_profile)
        self.btn_save_profile.setEnabled(False)
        bottom.addWidget(self.btn_save_profile)
        v.addLayout(bottom)

        self.lbl_resp_status = QLabel("")
        self.lbl_resp_status.setWordWrap(True)
        v.addWidget(self.lbl_resp_status)
        return w

    def _ref_capture(self):
        if not self._is_streaming():
            QMessageBox.warning(self, "Not connected", "Connect a device first."); return
        self._resume_for_capture()
        self._cap = _Capturer(self.dash, self._capture_frames(), self.lbl_resp_status,
                              lambda wl, it: self._set_ref(wl, it, "live"))
        self._cap.start()

    def _set_ref(self, wl, inten, src):
        if src == "live":
            self._restore_pause_after_capture()
        self._ref = (wl, inten)
        self.lbl_ref.setText(f"{src}: {len(wl)} pts, {wl[0]:.0f}–{wl[-1]:.0f} nm, "
                             f"peak {float(np.max(inten)):.0f}")
        self._redraw_response()

    def _ref_load(self):
        path, _ = QFileDialog.getOpenFileName(self, "Load reference CSV", "", "CSV (*.csv)")
        if not path:
            return
        try:
            wl, it = _csv_read_spectrum(path)
        except Exception as e:
            QMessageBox.critical(self, "Load failed", str(e)); return
        self._set_ref(wl, it, "file")

    def _ref_save(self):
        if self._ref is None:
            QMessageBox.warning(self, "No reference", "Capture a reference first."); return
        path, _ = QFileDialog.getSaveFileName(self, "Save reference CSV", "reference.csv", "CSV (*.csv)")
        if not path:
            return
        _csv_write_spectrum(path, *self._ref)
        self.lbl_resp_status.setText(f"Reference saved to {path}")

    def _meas_capture(self):
        if not self._is_streaming():
            QMessageBox.warning(self, "Not connected", "Connect a device first."); return
        self._resume_for_capture()
        self._cap = _Capturer(self.dash, self._capture_frames(), self.lbl_resp_status,
                              self._set_meas)
        self._cap.start()

    def _set_meas(self, wl, inten):
        self._restore_pause_after_capture()
        self._meas = (wl, inten)
        self.lbl_meas.setText(f"{self._device_name()}: {len(wl)} pts, "
                              f"{wl[0]:.0f}–{wl[-1]:.0f} nm, peak {float(np.max(inten)):.0f}")
        self._redraw_response()

    def _redraw_response(self):
        self.plot_resp.clear()
        if self._ref is not None:
            wl, it = self._ref
            self.plot_resp.plot(wl, it / max(np.max(it), 1e-9),
                                pen=pg.mkPen((120, 200, 120), width=1), name="reference (norm)")
        if self._meas is not None:
            wl, it = self._meas
            self.plot_resp.plot(wl, it / max(np.max(it), 1e-9),
                                pen=pg.mkPen((120, 180, 255), width=1), name="measured (norm)")
        if self._resp_table is not None:
            self.plot_resp.plot(self._resp_table[:, 0], self._resp_table[:, 1],
                                pen=pg.mkPen((255, 170, 60), width=2), name="sensitivity S")
        if (self._ref is not None or self._meas is not None
                or self._resp_table is not None):
            self.plot_resp.autoRange()

    def _compute_response(self):
        if self._ref is None or self._meas is None:
            QMessageBox.warning(self, "Missing data",
                                "Capture both a reference and a measurement first."); return
        try:
            table, info = calprof.compute_response_table(
                self._meas[0], self._meas[1], self._ref[0], self._ref[1])
        except Exception as e:
            QMessageBox.critical(self, "Compute failed", str(e)); return
        self._resp_table = table
        self.btn_save_profile.setEnabled(True)
        self._redraw_response()
        warn = info.get("warning")
        if warn:
            self.lbl_resp_status.setText(
                f"<span style='color:#ff8c3c'><b>Coverage warning:</b> {warn}</span>")
            QMessageBox.warning(self, "Banded / narrow source", warn)
        else:
            self.lbl_resp_status.setText(
                f"Computed sensitivity over {info['n_points']} points "
                f"({info['wl_min_nm']:.0f}–{info['wl_max_nm']:.0f} nm, "
                f"{info['coverage_frac']*100:.0f}% coverage). "
                f"Name and save it, then select it in the main window.")

    def _save_profile(self):
        if self._resp_table is None:
            return
        name = self.edit_name.text().strip()
        if not name:
            QMessageBox.warning(self, "Name required", "Enter a profile name."); return
        dev = self._device_name()
        meta = {
            "wl_min_nm": float(self._resp_table[:, 0].min()),
            "wl_max_nm": float(self._resp_table[:, 0].max()),
        }
        prof = calprof.save_profile(dev, name, self._resp_table, meta)
        calprof.set_active_selection(dev, prof["id"])
        self.notify_profiles_changed()
        QMessageBox.information(
            self, "Saved",
            f"Profile '{name}' saved for {dev} and selected. Close this dialog "
            f"to apply it.")


# ══════════════════════════════════════════════════════════════════════════
#  Shared helpers for the v2 tabs
# ══════════════════════════════════════════════════════════════════════════

_YES = (QMessageBox.StandardButton.Yes, QMessageBox.StandardButton.Ok)


def _ask(parent, title: str, text: str, *, ok_cancel: bool = False) -> bool:
    """Blocking question; True on Yes/OK."""
    btns = (QMessageBox.StandardButton.Ok | QMessageBox.StandardButton.Cancel
            if ok_cancel else
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
    return QMessageBox.question(parent, title, text, btns) in _YES


def _fmt_range(lo, hi) -> str:
    try:
        return f"{float(lo):.0f}–{float(hi):.0f} nm"
    except (TypeError, ValueError):
        return "—"


def _backend_serial(backend):
    s = getattr(backend, "serial", None) if backend is not None else None
    return str(s) if s else None


def _backend_fp(backend):
    try:
        return backend.wl_fingerprint() if backend is not None else None
    except Exception:
        return None


class _Fail(Exception):
    """A wizard step failed (message shown to the user)."""


class _Abort(Exception):
    """User cancelled."""


class _GenRunner:
    """Drives a generator whose yielded callables run in a worker thread.

    The generator itself (and everything in it that touches Qt) runs in the
    GUI thread. `result = yield fn` runs fn() in a threading.Thread; the
    result (or the exception, thrown into the generator) is picked up by a
    100 ms QTimer poll. Workers never touch Qt objects."""

    def __init__(self, gen, on_done, on_tick=None):
        self.gen = gen
        self.on_done = on_done
        self.on_tick = on_tick
        self._box = None
        self._timer = QTimer()
        self._timer.setInterval(100)
        self._timer.timeout.connect(self._poll)

    def start(self):
        self._advance(None, None)

    def _advance(self, val, exc):
        try:
            fn = self.gen.throw(exc) if exc is not None else self.gen.send(val)
        except StopIteration:
            self.on_done(None)
            return
        except Exception as e:                      # not handled by the generator
            self.on_done(e)
            return
        box = {}

        def run():
            try:
                box["result"] = fn()
            except BaseException as e:             # noqa: BLE001 – handed to the GUI
                box["error"] = e
            finally:
                box["done"] = True

        self._box = box
        threading.Thread(target=run, daemon=True).start()
        self._timer.start()

    def _poll(self):
        box = self._box
        if not box or not box.get("done"):
            if self.on_tick:
                self.on_tick()
            return
        self._timer.stop()
        self._box = None
        if "error" in box:
            self._advance(None, box["error"])
        else:
            self._advance(box.get("result"), None)


def _shade_invalid(plot, wl, valid, max_regions: int = 40):
    """Grey out the masked wavelength runs (valid == False)."""
    wl = np.asarray(wl, float)
    valid = np.asarray(valid, bool)
    if wl.size < 2:
        return
    d = float(np.median(np.diff(wl))) / 2.0
    runs, start = [], None
    for i, ok in enumerate(valid):
        if not ok and start is None:
            start = i
        if ok and start is not None:
            runs.append((start, i - 1)); start = None
    if start is not None:
        runs.append((start, valid.size - 1))
    if len(runs) > max_regions:                       # keep the two outer ones
        runs = [r for r in runs if r[0] == 0 or r[1] == valid.size - 1]
    for a, b in runs:
        reg = pg.LinearRegionItem(values=(wl[a] - d, wl[b] + d), movable=False,
                                  brush=pg.mkBrush(128, 128, 128, 70),
                                  pen=pg.mkPen(None))
        reg.setZValue(-10)
        plot.addItem(reg)


def _norm(a, valid=None):
    a = np.asarray(a, float)
    ref = a[np.asarray(valid, bool)] if valid is not None else a
    ref = ref[np.isfinite(ref)]
    m = float(np.max(np.abs(ref))) if ref.size else 0.0
    return a / m if m > 0 else a


# ══════════════════════════════════════════════════════════════════════════
#  SPECTRAL CALIBRATION TAB (wizard)
# ══════════════════════════════════════════════════════════════════════════

_LAMP_STEPS = ["Selection", "Wavelength check", "Integration time", "Dark spectra",
               "Calibration + stability", "Integration cross check", "Compute",
               "Save + protocol"]
_DERIV_STEPS = ["Selection", "Integration time", "Known optics (dark + light)",
                "New optics (dark + light)", "Compute", "Save"]
_STATE_TEXT = {"waiting": "waiting", "running": "running", "ok": "OK",
               "warning": "warning", "error": "ERROR"}


class _SpectralWizardTab(QWidget):
    def __init__(self, dlg):
        super().__init__()
        self.setObjectName("calTabPage")
        self.dlg = dlg
        self.running = False
        self._cancel = False
        self._note = ""
        self._runner = None
        self._wf = None
        self._dw = None
        self._resumed = False
        self._step_names: list[str] = []
        self.step_states: dict[int, tuple] = {}
        self.last_error: str | None = None
        self.last_status: str = ""
        self.protocol_paths: list[str] = []
        self.protocol_message: str = ""
        self._build()
        self.refresh()

    # ── UI ───────────────────────────────────────────────────────────────
    def _build(self):
        v = QVBoxLayout(self)
        intro = QLabel(
            "Fixed sequence, identical for every device: select, wavelength check, "
            "integration time, dark, calibration spectrum (+ stability), "
            "integration-time cross check, compute, save. The new profile is "
            "activated and a protocol (PDF/CSV) is written.")
        intro.setWordWrap(True)
        v.addWidget(intro)

        top = QHBoxLayout()
        top.addWidget(QLabel("Mode:"))
        self.cmb_mode = QComboBox()
        self.cmb_mode.addItems(["Calibrate against a reference lamp",
                                "Derive from other optics"])
        self.cmb_mode.currentIndexChanged.connect(self._mode_changed)
        top.addWidget(self.cmb_mode)
        top.addStretch(1)
        v.addLayout(top)

        self.stack = QStackedWidget()
        self.stack.addWidget(self._build_lamp_form())
        self.stack.addWidget(self._build_deriv_form())
        v.addWidget(self.stack)

        g = QHBoxLayout()
        g.addWidget(QLabel("Integration time:"))
        self.cmb_itime = QComboBox()
        self.cmb_itime.addItems(["Auto (target level)", "Fixed"])
        self.cmb_itime.currentIndexChanged.connect(self._itime_mode_changed)
        g.addWidget(self.cmb_itime)
        self.spn_ms = QDoubleSpinBox()
        self.spn_ms.setRange(0.01, 60000.0); self.spn_ms.setDecimals(2)
        self.spn_ms.setValue(100.0); self.spn_ms.setSuffix(" ms")
        self.spn_ms.setEnabled(False)
        g.addWidget(self.spn_ms)
        g.addSpacing(12)
        g.addWidget(QLabel("Frames per capture:"))
        self.spn_frames = QSpinBox(); self.spn_frames.setRange(2, 1000)
        self.spn_frames.setValue(cw.WorkflowConfig.from_config().n_frames)
        g.addWidget(self.spn_frames)
        g.addStretch(1)
        self.lbl_itime = QLabel("")
        g.addWidget(self.lbl_itime)
        v.addLayout(g)

        mid = QHBoxLayout()
        self.lst_steps = QListWidget()
        self.lst_steps.setMaximumWidth(330)
        self.lst_steps.setMinimumHeight(170)
        mid.addWidget(self.lst_steps)
        plots = QVBoxLayout()
        self.plot_in = pg.PlotWidget()
        self.plot_in.setLabel("bottom", "Wavelength", units="nm")
        self.plot_in.setLabel("left", "normalised")
        self.plot_in.addLegend()
        self.plot_in.setMinimumHeight(110)
        self.dlg._style_cal_plot(self.plot_in)
        self.plot_out = pg.PlotWidget()
        self.plot_out.setLabel("bottom", "Wavelength", units="nm")
        self.plot_out.setLabel("left", "S / K")
        self.plot_out.addLegend()
        self.plot_out.setMinimumHeight(110)
        self.dlg._style_cal_plot(self.plot_out)
        plots.addWidget(self.plot_in, 1)
        plots.addWidget(self.plot_out, 1)
        mid.addLayout(plots, 1)
        v.addLayout(mid, 1)

        self.lbl_result = QLabel("")
        self.lbl_result.setWordWrap(True)
        self.lbl_result.setTextFormat(Qt.TextFormat.RichText)
        v.addWidget(self.lbl_result)
        self.lbl_status = QLabel("Ready.")
        self.lbl_status.setWordWrap(True)
        v.addWidget(self.lbl_status)

        row = QHBoxLayout()
        row.addStretch(1)
        self.btn_cancel = QPushButton("Cancel")
        self.btn_cancel.setEnabled(False)
        self.btn_cancel.setToolTip("Stops after the running step.")
        self.btn_cancel.clicked.connect(self._cancel_clicked)
        self.btn_start = QPushButton("Start")
        self.btn_start.clicked.connect(self.start)
        row.addWidget(self.btn_cancel)
        row.addWidget(self.btn_start)
        v.addLayout(row)
        self._set_steps(_LAMP_STEPS)

    def _build_lamp_form(self) -> QWidget:
        w = QWidget(); f = QFormLayout(w); f.setContentsMargins(0, 0, 0, 0)
        self.lbl_device = QLabel("")
        f.addRow("Device:", self.lbl_device)
        self.cmb_optics = QComboBox(); self.cmb_optics.setEditable(True)
        self.cmb_optics.lineEdit().setPlaceholderText("Optics configuration name")
        self.cmb_optics.activated.connect(self._optics_picked)
        f.addRow("Optics configuration:", self.cmb_optics)
        self.edit_notes = QLineEdit(); self.edit_notes.setPlaceholderText("Notes")
        f.addRow("Notes:", self.edit_notes)
        self.cmb_input = QComboBox(); self.cmb_input.addItems(list(calprof.INPUT_OPTICS))
        self.cmb_input.currentIndexChanged.connect(self._input_changed)
        f.addRow("Input optic:", self.cmb_input)
        self.cmb_ref = QComboBox()
        self.cmb_ref.currentIndexChanged.connect(self._ref_changed)
        f.addRow("Reference source:", self.cmb_ref)
        self.lbl_ref_info = QLabel("")
        self.lbl_ref_info.setWordWrap(True)
        self.lbl_ref_info.setTextFormat(Qt.TextFormat.RichText)
        f.addRow("", self.lbl_ref_info)
        self.cmb_type = QComboBox()
        self.cmb_type.addItems([calprof.TYPE_RELATIVE, calprof.TYPE_ABSOLUTE])
        f.addRow("Type:", self.cmb_type)
        return w

    def _build_deriv_form(self) -> QWidget:
        w = QWidget(); f = QFormLayout(w); f.setContentsMargins(0, 0, 0, 0)
        hint = QLabel("Measures the same diffuse, extended source with the known "
                      "and the new optics: S_new = S_known · C_new / C_known.")
        hint.setWordWrap(True)
        f.addRow(hint)
        self.cmb_known = QComboBox()
        f.addRow("Known profile:", self.cmb_known)
        self.edit_new_name = QLineEdit()
        self.edit_new_name.setPlaceholderText("Name of the NEW optics configuration")
        f.addRow("New optics name:", self.edit_new_name)
        self.cmb_new_input = QComboBox()
        self.cmb_new_input.addItems(list(calprof.INPUT_OPTICS))
        f.addRow("New input optic:", self.cmb_new_input)
        self.edit_new_notes = QLineEdit()
        f.addRow("Notes:", self.edit_new_notes)
        return w

    # ── refresh / small slots ────────────────────────────────────────────
    def _device(self):
        be = self.dlg._backend()
        return self.dlg._device_name(), be, _backend_serial(be)

    def refresh(self):
        if self.running:
            return
        name, be, serial = self._device()
        fp = _backend_fp(be)
        self.lbl_device.setText(
            f"{name} · S/N {serial or '—'}"
            + ("" if be is not None else "  (no device connected)")
            + ("  ⚠ no serial number: profiles are NOT bound to this unit"
               if be is not None and not serial else ""))
        cur = self.cmb_optics.currentText()
        profs = calprof.profiles_for_device(name, serial, fp, include_old=True)
        names = sorted({(p.get("optics") or {}).get("name", "") for p in profs} - {""})
        self.cmb_optics.blockSignals(True)
        self.cmb_optics.clear(); self.cmb_optics.addItems(names)
        self.cmb_optics.setEditText(cur)
        self.cmb_optics.blockSignals(False)
        cur_id = self.cmb_ref.currentData()
        try:
            srcs = rs.ReferenceLibrary().list()
        except Exception as e:                                # noqa: BLE001
            srcs = []
            self.lbl_ref_info.setText(f"<span style='color:#ff6060'>{e}</span>")
        self.cmb_ref.blockSignals(True)
        self.cmb_ref.clear()
        for s in srcs:
            self.cmb_ref.addItem(f"{s.name} ({s.serial})" if s.serial else s.name, s.id)
        i = self.cmb_ref.findData(cur_id) if cur_id is not None else -1
        if i >= 0:
            self.cmb_ref.setCurrentIndex(i)
        self.cmb_ref.blockSignals(False)
        cur_known = self.cmb_known.currentData()
        self.cmb_known.clear()
        import calibration_workflow as _cw
        for p in calprof.profiles_for_device(name, serial, fp,
                                             nl_active=calprof.device_nl_active(be)):
            # only v2 profiles valid on this unit with the current λ calibration
            if be is not None and not _cw.derivation_source_ok(p, be, name)[0]:
                continue
            self.cmb_known.addItem(calprof.profile_label(p), p["id"])
        i = self.cmb_known.findData(cur_known)
        if i >= 0:
            self.cmb_known.setCurrentIndex(i)
        self._input_changed()

    def _optics_picked(self, _i):
        name, be, serial = self._device()
        n = self.cmb_optics.currentText()
        for p in calprof.profiles_for_device(name, serial, _backend_fp(be),
                                             include_old=True):
            o = p.get("optics") or {}
            if o.get("name") == n:
                j = self.cmb_input.findText(o.get("input", ""))
                if j >= 0:
                    self.cmb_input.setCurrentIndex(j)
                self.edit_notes.setText(o.get("notes", ""))
                break

    def _input_changed(self, *_):
        self._ref_changed()

    def _sync_type_enabled(self, src):
        """Absolute only with a defined entrance area (input != bare) AND a
        reference with an absolute irradiance unit."""
        bare = self.cmb_input.currentText() == "bare"
        rel_ref = src is not None and not calprof.is_absolute_reference_unit(src.unit)
        allow_abs = not bare and not rel_ref
        item = self.cmb_type.model().item(1)
        if item is not None:
            item.setEnabled(allow_abs)
        if not allow_abs and self.cmb_type.currentText() == calprof.TYPE_ABSOLUTE:
            self.cmb_type.setCurrentIndex(0)

    def _ref_changed(self, *_):
        sid = self.cmb_ref.currentData()
        try:
            src = rs.ReferenceLibrary().get(sid) if sid else None
        except Exception:                                     # noqa: BLE001
            src = None
        self._sync_type_enabled(src)
        if src is None:
            self.lbl_ref_info.setText("")
            return
        txt = (f"Unit: {src.unit} · valid {_fmt_range(src.valid_min_nm, src.valid_max_nm)}"
               f" · intended optic: {src.input_optic or 'not specified'}")
        if getattr(src, "builtin", False):
            txt += ("<br><span style='color:#ff8c3c'><b>Synthetic demo source</b> — "
                    "only meaningful with the Demo backend, not a real lamp.</span>")
        if not calprof.is_absolute_reference_unit(src.unit):
            txt += "<br>Relative unit → only relative calibration possible."
        inp = self.cmb_input.currentText()
        if src.input_optic and src.input_optic != inp:
            txt += (f"<br><span style='color:#ff8c3c'><b>Warning:</b> this source "
                    f"is meant for input optic '{src.input_optic}', but '{inp}' is "
                    f"selected.</span>")
        self.lbl_ref_info.setText(txt)

    def _itime_mode_changed(self, *_):
        self.spn_ms.setEnabled(self.cmb_itime.currentIndex() == 1)

    def _mode_changed(self, i):
        if self.running:
            return
        self.stack.setCurrentIndex(i)
        self._set_steps(_LAMP_STEPS if i == 0 else _DERIV_STEPS)

    # ── step list ────────────────────────────────────────────────────────
    def _set_steps(self, names):
        self._step_names = list(names)
        self.step_states = {}
        self.lst_steps.clear()
        for _ in names:
            self.lst_steps.addItem(QListWidgetItem())
        for i in range(len(names)):
            self._state(i, "waiting")

    def _state(self, i, state, detail=""):
        it = self.lst_steps.item(i)
        if it is None:
            return
        self.step_states[i] = (state, detail)
        txt = f"{self._step_names[i]}: {_STATE_TEXT[state]}"
        if detail:
            txt += f" – {detail}"
        it.setText(txt)
        it.setToolTip(txt)
        t = _T()
        col = {"error": "#ff6060", "warning": "#ff8c3c",
               "waiting": t.FG3, "running": t.ACCENT}.get(state, t.FG1)
        from PyQt6.QtGui import QColor
        it.setForeground(QColor(col))

    # ── run control ──────────────────────────────────────────────────────
    def _cancel_clicked(self):
        self._cancel = True
        self.btn_cancel.setEnabled(False)
        self.lbl_status.setText("Cancelling after the running step…")

    def _checkpoint(self):
        if self._cancel:
            raise _Abort("cancelled by user")

    def _tick(self):
        if self._note:
            self.lbl_status.setText(self._note)

    def _worker_sleep(self, seconds):
        """Replacement for time.sleep in the worker: updates a plain string the
        GUI poll displays (no Qt access here)."""
        end = time.monotonic() + max(0.0, float(seconds))
        while True:
            rem = end - time.monotonic()
            if rem <= 0:
                return
            self._note = f"Stability check: waiting {rem:.0f} s before the second capture…"
            time.sleep(min(0.1, rem))

    def _begin(self, gen):
        self.running = True
        self._cancel = False
        self.last_error = None
        self.last_status = ""
        self.protocol_paths = []
        self.protocol_message = ""
        self.dlg.set_busy(True)
        self.btn_start.setEnabled(False)
        self.btn_cancel.setEnabled(True)
        self.cmb_mode.setEnabled(False)
        self.dlg._resume_for_capture()
        self._resumed = True
        self._runner = _GenRunner(gen, self._end, self._tick)
        self._runner.start()

    def _end(self, exc):
        if exc is not None:
            self.last_error = str(exc)
            self.last_status = "error"
            self.lbl_status.setText(f"Unexpected error: {exc}")
        self.running = False
        self._note = ""
        if self._resumed:
            self.dlg._restore_pause_after_capture()
            self._resumed = False
        self.dlg.set_busy(False)
        self.btn_start.setEnabled(True)
        self.btn_cancel.setEnabled(False)
        self.cmb_mode.setEnabled(True)

    def start(self):
        if self.running:
            return
        name, be, serial = self._device()
        if be is None:
            QMessageBox.warning(self, "Not connected",
                                "Connect and start a device first.")
            return
        self.refresh()
        self._set_steps(_LAMP_STEPS if self.cmb_mode.currentIndex() == 0 else _DERIV_STEPS)
        self.lbl_result.setText("")
        self.lbl_itime.setText("")
        self.plot_in.clear(); self.plot_out.clear()
        cfg = cw.WorkflowConfig.from_config()
        cfg.n_frames = int(self.spn_frames.value())
        us = (None if self.cmb_itime.currentIndex() == 0
              else int(round(self.spn_ms.value() * 1000.0)))
        try:
            if self.cmb_mode.currentIndex() == 0:
                src = rs.ReferenceLibrary().get(self.cmb_ref.currentData() or "")
                if src is None:
                    raise ValueError("Select a reference source.")
                self._wf = cw.CalibrationWorkflow(
                    be, name, kind=self.cmb_type.currentText(),
                    optics={"name": self.cmb_optics.currentText().strip(),
                            "input": self.cmb_input.currentText(),
                            "notes": self.edit_notes.text().strip()},
                    reference=src, cfg=cfg)
                gen = self._gen_lamp(self._wf, us)
            else:
                pid = self.cmb_known.currentData()
                if not pid:
                    raise ValueError("Select a known profile.")
                self._dw = cw.DerivationWorkflow(
                    be, name, known_profile_id=pid,
                    optics={"name": self.edit_new_name.text().strip(),
                            "input": self.cmb_new_input.currentText(),
                            "notes": self.edit_new_notes.text().strip()},
                    cfg=cfg)
                gen = self._gen_deriv(self._dw, us)
        except (ValueError, KeyError) as e:
            QMessageBox.warning(self, "Selection incomplete", str(e))
            return
        self._begin(gen)

    # ── generators ───────────────────────────────────────────────────────
    def _fail_msg(self, step, e):
        self._note = ""
        if isinstance(e, _Abort):
            self._state(step, "warning", str(e))
            self.last_status = "cancelled"
            self.lbl_status.setText("Cancelled.")
            return
        self.last_error = str(e)
        self.last_status = "error"
        self._state(step, "error", str(e))
        self.lbl_status.setText(f"Stopped: {e}")
        QMessageBox.critical(self, "Calibration stopped", str(e))

    def _gen_lamp(self, wf, us):
        step = 0
        try:
            self._state(0, "ok", f"{wf.optics['name']} · {wf.optics['input']} · {wf.kind}")
            # 2 wavelength check
            step = 1; self._checkpoint(); self._state(1, "running")
            self.lbl_status.setText("Checking the wavelength calibration…")
            chk = yield wf.check_wavelength
            fp = chk.get("fingerprint", "")
            msgs = "; ".join(chk.get("messages", []))
            if not chk.get("ok"):
                raise _Fail(msgs or "Wavelength axis unusable.")
            self._state(1, "warning" if msgs else "ok", f"fingerprint {fp}")
            self.lbl_result.setText(f"Fingerprint: <code>{fp}</code>"
                                    + (f"<br>{msgs}" if msgs else ""))
            # 3 integration time
            step = 2; self._checkpoint(); self._state(2, "running")
            self.lbl_status.setText("Setting the integration time…")
            info = yield (lambda: wf.set_integration(us, auto=us is None))
            half = info.get("half_us")
            self.lbl_itime.setText(
                f"t_int = {info['integration_us']/1000:.2f} ms"
                + (f", half = {half/1000:.2f} ms" if half else ", no half value"))
            self._state(2, "ok" if info.get("converged", True) else "warning",
                        self.lbl_itime.text())
            # 4 dark
            step = 3; self._checkpoint(); self._state(3, "running")
            if wf.needs_user_cover:
                if not _ask(self, "Dark spectrum",
                            "Block the light path / cover the lamp now.\n\n"
                            "The dark spectra are captured next.", ok_cancel=True):
                    raise _Abort("cancelled at the dark prompt")
            self.lbl_status.setText("Capturing dark spectra…")
            dk = yield wf.capture_darks
            if wf.needs_user_cover:
                if not _ask(self, "Release the light path",
                            "Release the light path / uncover the lamp now.\n\n"
                            "The calibration spectrum is captured next.",
                            ok_cancel=True):
                    raise _Abort("cancelled at the release prompt")
            self._state(3, "ok", f"peak {dk['dark_peak']:.0f} counts")
            # 5 calibration + stability
            step = 4; self._checkpoint(); self._state(4, "running")
            self.lbl_status.setText("Capturing the calibration spectrum…")
            cal = yield (lambda: wf.capture_calibration(sleep=self._worker_sleep))
            self._note = ""
            st = cal["stability"]["p95_pct"]
            self._state(4, "ok" if cal["pass"] else "warning", f"stability {st:.2f} %")
            # 6 cross check
            step = 5; self._checkpoint(); self._state(5, "running")
            self.lbl_status.setText("Integration-time cross check…")
            cx = yield wf.crosscheck_integration
            if cx.get("skipped"):
                self._state(5, "warning", "skipped")
            else:
                self._state(5, "ok" if cx["pass"] else "warning",
                            f"{cx['crosscheck']['p95_pct']:.2f} %")
            # 7 compute
            step = 6; self._checkpoint(); self._state(6, "running")
            res = yield wf.compute
            self._show_lamp_result(wf, res)
            self._state(6, "ok", f"valid {_fmt_range(*res['valid_range_nm'])}")
            # 8 save
            step = 7; self._checkpoint()
            if any(wf.checks.get(k) is False for k in ("stability_pass", "itime_pass")):
                if not _ask(self, "Checks failed",
                            "The stability and/or integration-time cross check "
                            "failed.\n\nSave the profile anyway?"):
                    self._state(7, "warning", "not saved (checks failed)")
                    self.last_status = "not_saved"
                    self.lbl_status.setText("Not saved.")
                    return
            self._state(7, "running")
            saved = yield wf.save
            calprof.set_active_selection(wf.backend_name, saved[0]["id"])
            self.dlg.notify_profiles_changed()
            self._write_protocol(wf)
            self._state(7, "ok" if self.protocol_paths else "warning",
                        f"profile {saved[0]['id']} v{saved[0]['version']} saved and active"
                        + ("" if self.protocol_paths else "; protocol not written"))
            self.last_status = "saved"
            self.lbl_status.setText(
                f"Saved profile {saved[0]['id']} (v{saved[0]['version']}) and activated "
                f"it. " + (self.protocol_message or ""))
        except Exception as e:                                # noqa: BLE001
            self._fail_msg(step, e)

    def _show_lamp_result(self, wf, res):
        wl, valid = res["wl"], res["valid"]
        self.plot_in.clear()
        self.plot_in.plot(wl, _norm(res["C_cal"], valid), name="C_cal (norm)",
                          pen=pg.mkPen((120, 180, 255), width=1))
        self.plot_in.plot(wl, _norm(res["E"], valid), name="E_ref (norm)",
                          pen=pg.mkPen((120, 200, 120), width=1))
        _shade_invalid(self.plot_in, wl, valid)
        self.plot_in.autoRange()
        self.plot_out.clear()
        absolute = wf.kind == calprof.TYPE_ABSOLUTE
        y = res["K"] if absolute else res["S"]
        self.plot_out.setLabel("left", "K" if absolute else "S")
        self.plot_out.plot(wl, y, name="K" if absolute else "S", connect="finite",
                           pen=pg.mkPen((255, 170, 60), width=2))
        _shade_invalid(self.plot_out, wl, valid)
        self.plot_out.autoRange()
        c = wf.checks

        def chk(name, pct, ok):
            if pct is None:
                return f"{name}: skipped"
            col = "#3cb371" if ok else "#ff6060"
            return (f"{name}: {pct:.2f} % — <span style='color:{col}'>"
                    f"{'passed' if ok else 'NOT passed'}</span>")
        txt = [f"Valid range: {_fmt_range(*res['valid_range_nm'])}"
               f" · gain span {res['gain_span']:.1f}×",
               chk("Stability", c.get("stability_pct"), c.get("stability_pass")),
               chk("Integration cross check", c.get("itime_crosscheck_pct"),
                   c.get("itime_pass"))]
        if wf.warnings:
            txt.append("<span style='color:#ff8c3c'>" + "<br>".join(wf.warnings) + "</span>")
        self.lbl_result.setText("<br>".join(txt))

    def _write_protocol(self, wf):
        try:
            import calibration_report as crp
        except ImportError as e:
            self.protocol_message = f"Protocol not written (calibration_report: {e})."
            QMessageBox.warning(self, "Protocol", self.protocol_message)
            return
        try:
            import export_paths
            rec = wf.record()
            d, err = export_paths.ensure_export_dir()
            base = crp.default_protocol_basename(rec)
            paths = crp.write_protocol(rec, os.path.join(d, base + ".pdf"),
                                       os.path.join(d, base + ".csv"))
            self.protocol_paths = list(paths or [])
            self.protocol_message = ("Protocol: " + ", ".join(self.protocol_paths)
                                     + (f" ({err})" if err else ""))
        except Exception as e:                                # noqa: BLE001
            self.protocol_message = f"Protocol not written: {e}"
            QMessageBox.warning(self, "Protocol", self.protocol_message)

    # derivation ----------------------------------------------------------
    def _capture_pair(self, dw, which, cover):
        """Dark + light for `which` (generator fragment), with the cover /
        uncover prompts between them on devices without a shutter. Uses the
        split DerivationWorkflow steps (no capture/rate logic here)."""
        if cover and not _ask(self, "Dark spectrum",
                              "Block the light path / cover the source now.\n\n"
                              "The dark spectrum is captured next.", ok_cancel=True):
            raise _Abort("cancelled at the dark prompt")
        yield (lambda: dw.capture_dark_for(which))
        if cover and not _ask(self, "Release the light path",
                              "Release the light path / uncover the source now.\n\n"
                              "The light spectrum is captured next.", ok_cancel=True):
            raise _Abort("cancelled at the light prompt")
        yield (lambda: dw.capture_light_for(which))

    def _gen_deriv(self, dw, us):
        step = 0
        cover = not getattr(dw.backend, "SUPPORTS_SHUTTER", False)
        try:
            self._state(0, "ok", f"{(dw.known.get('optics') or {}).get('name')} → "
                                 f"{dw.optics['name']} ({dw.optics['input']})")
            step = 1; self._checkpoint(); self._state(1, "running")
            self.lbl_status.setText("Setting the integration time…")
            t = yield (lambda: dw.set_integration(us))
            self.lbl_itime.setText(f"t_int = {t/1000:.2f} ms")
            self._state(1, "ok", self.lbl_itime.text())
            step = 2; self._checkpoint(); self._state(2, "running")
            QMessageBox.information(
                self, "Known optics",
                "Known optics mounted, diffuse extended source.\n\nThe capture of "
                "the known optics starts after you confirm.")
            self.lbl_status.setText("Capturing with the known optics…")
            yield from self._capture_pair(dw, "known", cover)
            self._state(2, "ok")
            step = 3; self._checkpoint(); self._state(3, "running")
            if not _ask(self, "Swap optics",
                        "Swap the optics now: mount the NEW optics on the same "
                        "diffuse, extended source (same position, same lamp).",
                        ok_cancel=True):
                raise _Abort("cancelled at the optics swap")
            self.lbl_status.setText("Capturing with the new optics…")
            yield from self._capture_pair(dw, "new", cover)
            self._state(3, "ok")
            step = 4; self._checkpoint(); self._state(4, "running")
            res = yield dw.compute
            self._show_deriv_result(dw, res)
            self._state(4, "ok", f"valid {_fmt_range(*res['valid_range_nm'])}")
            step = 5; self._checkpoint(); self._state(5, "running")
            p = yield dw.save
            calprof.set_active_selection(dw.backend_name, p["id"])
            self.dlg.notify_profiles_changed()
            self._state(5, "ok", f"profile {p['id']} v{p['version']} saved and active")
            self.last_status = "saved"
            self.lbl_status.setText(f"Saved profile {p['id']} (v{p['version']}) and "
                                    f"activated it.")
        except Exception as e:                                # noqa: BLE001
            self._fail_msg(step, e)

    def _show_deriv_result(self, dw, res):
        wl, valid = res["wl"], res["valid"]
        self.plot_in.clear()
        self.plot_in.plot(wl, _norm(res["C_known"], valid), name="C_known (norm)",
                          pen=pg.mkPen((120, 200, 120), width=1))
        self.plot_in.plot(wl, _norm(res["C_new"], valid), name="C_new (norm)",
                          pen=pg.mkPen((120, 180, 255), width=1))
        _shade_invalid(self.plot_in, wl, valid)
        self.plot_in.autoRange()
        self.plot_out.clear()
        self.plot_out.setLabel("left", "S")
        self.plot_out.plot(wl, res["S"], name="S (new optics)", connect="finite",
                           pen=pg.mkPen((255, 170, 60), width=2))
        _shade_invalid(self.plot_out, wl, valid)
        self.plot_out.autoRange()
        self.lbl_result.setText(f"Valid range: {_fmt_range(*res['valid_range_nm'])}"
                                f" · gain span {res['gain_span']:.1f}×")


# ══════════════════════════════════════════════════════════════════════════
#  PROFILES TAB
# ══════════════════════════════════════════════════════════════════════════

class _ProfilesTab(QWidget):
    COLS = ["Profile", "Status", "Valid range", "Unit"]

    def __init__(self, dlg):
        super().__init__()
        self.setObjectName("calTabPage")
        self.dlg = dlg
        v = QVBoxLayout(self)
        top = QHBoxLayout()
        self.lbl_dev = QLabel("")
        top.addWidget(self.lbl_dev, 1)
        self.chk_all = QCheckBox("All versions")
        self.chk_all.toggled.connect(lambda _=None: self.refresh())
        top.addWidget(self.chk_all)
        v.addLayout(top)
        self.table = QTableWidget(0, len(self.COLS))
        self.table.setHorizontalHeaderLabels(self.COLS)
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        dlg._style_cal_table(self.table)
        v.addWidget(self.table, 1)
        row = QHBoxLayout()
        for text, fn in (("Activate", self.activate), ("Export…", self.export),
                         ("Import…", self.import_), ("Delete", self.delete),
                         ("Copy profile to this device…", self.copy_to_device)):
            b = QPushButton(text)
            b.clicked.connect(lambda _=False, f=fn: f())
            row.addWidget(b)
        row.addStretch(1)
        v.addLayout(row)
        self.lbl_status = QLabel("")
        self.lbl_status.setWordWrap(True)
        v.addWidget(self.lbl_status)
        self.refresh()

    def _device(self):
        be = self.dlg._backend()
        return self.dlg._device_name(), be, _backend_serial(be)

    def refresh(self):
        name, be, serial = self._device()
        fp = _backend_fp(be)
        self.lbl_dev.setText(f"Device: {name} · S/N {serial or '—'}")
        nl = calprof.device_nl_active(be)
        profs = calprof.profiles_for_device(name, serial, fp, nl_active=nl,
                                            include_old=self.chk_all.isChecked())
        active = calprof.active_selection(name)
        self.table.setRowCount(len(profs))
        for r, p in enumerate(profs):
            st = calprof.profile_status(p, name, serial, fp, nl)
            label = calprof.profile_label(p) + ("  [active]" if p.get("id") == active else "")
            vr = p.get("valid_range_nm") or (None, None)
            vals = [label, st["state"], _fmt_range(*vr), str(p.get("unit", ""))]
            for c, t in enumerate(vals):
                it = QTableWidgetItem(t)
                it.setToolTip(st["message"] or t)
                if c == 0:
                    it.setData(Qt.ItemDataRole.UserRole, p["id"])
                self.table.setItem(r, c, it)

    def selected_id(self):
        r = self.table.currentRow()
        it = self.table.item(r, 0) if r >= 0 else None
        return it.data(Qt.ItemDataRole.UserRole) if it else None

    def _need_selection(self):
        pid = self.selected_id()
        if pid is None:
            QMessageBox.information(self, "Profiles", "Select a profile first.")
        return pid

    def activate(self):
        pid = self._need_selection()
        if pid is None:
            return
        name, be, serial = self._device()
        p = calprof.get_profile_by_id(pid)
        if p is None:
            return
        fp = _backend_fp(be)
        nl = calprof.device_nl_active(be)
        st = calprof.profile_status(p, name, serial, fp, nl)
        if st["state"] in calprof.CONFIRM_STATES:
            if not _ask(self, "Calibration conditions changed",
                        st["message"].split(" (Applying")[0]
                        + ". Applying it may give wrong results.\n\nActivate anyway?"):
                return
            calprof.confirm_fingerprint_override(pid, fp, nl)
        calprof.set_active_selection(name, pid)
        self.lbl_status.setText(f"Activated: {calprof.profile_label(p)}")
        self.dlg.notify_profiles_changed()

    def export(self):
        pid = self._need_selection()
        if pid is None:
            return
        path, _ = QFileDialog.getSaveFileName(self, "Export profile", f"profile_{pid}.json",
                                              "JSON (*.json)")
        if not path:
            return
        try:
            calprof.export_profile(pid, path)
            self.lbl_status.setText(f"Exported to {path}")
        except Exception as e:                                # noqa: BLE001
            QMessageBox.critical(self, "Export failed", str(e))

    def import_(self):
        path, _ = QFileDialog.getOpenFileName(self, "Import profile", "", "JSON (*.json)")
        if not path:
            return
        try:
            p = calprof.import_profile(path)
        except Exception as e:                                # noqa: BLE001
            QMessageBox.critical(self, "Import failed", str(e))
            return
        self.lbl_status.setText(f"Imported: {calprof.profile_label(p)}")
        self.dlg.notify_profiles_changed()

    def delete(self):
        pid = self._need_selection()
        if pid is None:
            return
        p = calprof.get_profile_by_id(pid)
        if not _ask(self, "Delete profile",
                    f"Delete '{calprof.profile_label(p) if p else pid}'?"):
            return
        calprof.delete_profile_id(pid)
        self.dlg.notify_profiles_changed()

    def copy_to_device(self):
        name, be, serial = self._device()
        if be is None or not serial:
            QMessageBox.warning(self, "Copy profile",
                                "Connect a device with a known serial number first.")
            return
        cands = calprof.profiles_other_units(name, serial)
        if not cands:
            QMessageBox.information(self, "Copy profile",
                                    "No profiles of other units of this device type.")
            return
        labels = [f"S/N {(p.get('device') or {}).get('serial')} · {calprof.profile_label(p)}"
                  for p in cands]
        sel, ok = QInputDialog.getItem(self, "Copy profile to this device",
                                       "Profile:", labels, 0, False)
        if not ok:
            return
        src = cands[labels.index(sel)]
        try:
            p = calprof.copy_profile_to_device(src["id"], serial, name)
        except Exception as e:                                # noqa: BLE001
            QMessageBox.critical(self, "Copy failed", str(e))
            return
        self.lbl_status.setText(f"Copied as {calprof.profile_label(p)}")
        self.dlg.notify_profiles_changed()


# ══════════════════════════════════════════════════════════════════════════
#  LAMPS TAB (reference library)
# ══════════════════════════════════════════════════════════════════════════

class _LampImportDialog(QDialog):
    def __init__(self, parent, path):
        super().__init__(parent)
        self.setWindowTitle("Import reference lamp")
        wl, _vals = rs.parse_reference_file(path)
        self.dmin, self.dmax = float(wl[0]), float(wl[-1])
        f = QFormLayout(self)
        f.addRow(QLabel(f"File: {os.path.basename(path)} "
                        f"(data {self.dmin:.0f}–{self.dmax:.0f} nm)"))
        self.edit_name = QLineEdit(os.path.splitext(os.path.basename(path))[0])
        self.edit_serial = QLineEdit()
        self.cmb_unit = QComboBox(); self.cmb_unit.addItems(list(rs.KNOWN_UNITS))
        self.spn_lo = QDoubleSpinBox(); self.spn_hi = QDoubleSpinBox()
        for s, val in ((self.spn_lo, self.dmin), (self.spn_hi, self.dmax)):
            s.setRange(0.0, 20000.0); s.setDecimals(1); s.setSuffix(" nm"); s.setValue(val)
        self.cmb_optic = QComboBox(); self.cmb_optic.addItems([""] + list(rs.INPUT_OPTICS))
        self.edit_notes = QLineEdit()
        f.addRow("Name:", self.edit_name)
        f.addRow("Serial number:", self.edit_serial)
        f.addRow("Unit:", self.cmb_unit)
        f.addRow("Valid from:", self.spn_lo)
        f.addRow("Valid to:", self.spn_hi)
        hint = QLabel("Take the validity limits from the certificate "
                      "(e.g. HAL-CAL: 380 nm lower limit).")
        hint.setWordWrap(True)
        f.addRow(hint)
        f.addRow("Input optic:", self.cmb_optic)
        f.addRow("Notes:", self.edit_notes)
        bb = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok
                              | QDialogButtonBox.StandardButton.Cancel)
        bb.accepted.connect(self.accept); bb.rejected.connect(self.reject)
        f.addRow(bb)

    def values(self) -> dict:
        return dict(name=self.edit_name.text().strip(),
                    serial=self.edit_serial.text().strip(),
                    unit=self.cmb_unit.currentText(),
                    valid_min_nm=self.spn_lo.value(), valid_max_nm=self.spn_hi.value(),
                    input_optic=self.cmb_optic.currentText(),
                    notes=self.edit_notes.text().strip())


class _LampsTab(QWidget):
    COLS = ["Name", "Serial", "Unit", "Valid range", "Optic", "Built-in"]

    def __init__(self, dlg):
        super().__init__()
        self.setObjectName("calTabPage")
        self.dlg = dlg
        v = QVBoxLayout(self)
        self.table = QTableWidget(0, len(self.COLS))
        self.table.setHorizontalHeaderLabels(self.COLS)
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setMaximumHeight(180)
        self.table.itemSelectionChanged.connect(self._plot_selected)
        dlg._style_cal_table(self.table)
        v.addWidget(self.table)
        row = QHBoxLayout()
        b = QPushButton("Import…"); b.clicked.connect(lambda _=False: self.import_clicked())
        row.addWidget(b)
        b = QPushButton("Remove"); b.clicked.connect(lambda _=False: self.remove_clicked())
        row.addWidget(b)
        row.addStretch(1)
        v.addLayout(row)
        self.plot = pg.PlotWidget()
        self.plot.setLabel("bottom", "Wavelength", units="nm")
        self.plot.setLabel("left", "Irradiance")
        self.plot.setMinimumHeight(160)
        dlg._style_cal_plot(self.plot)
        v.addWidget(self.plot, 1)
        self.lbl_status = QLabel("")
        self.lbl_status.setWordWrap(True)
        v.addWidget(self.lbl_status)
        self.refresh()

    def refresh(self):
        try:
            srcs = rs.ReferenceLibrary().list()
        except Exception as e:                                # noqa: BLE001
            srcs = []
            self.lbl_status.setText(f"Library unreadable: {e}")
        self.table.setRowCount(len(srcs))
        for r, s in enumerate(srcs):
            vals = [s.name, s.serial, s.unit, _fmt_range(s.valid_min_nm, s.valid_max_nm),
                    s.input_optic or "—", "yes" if s.builtin else ""]
            for c, t in enumerate(vals):
                it = QTableWidgetItem(t)
                if c == 0:
                    it.setData(Qt.ItemDataRole.UserRole, s.id)
                    it.setData(Qt.ItemDataRole.UserRole + 1, bool(s.builtin))
                self.table.setItem(r, c, it)

    def selected(self):
        r = self.table.currentRow()
        it = self.table.item(r, 0) if r >= 0 else None
        if it is None:
            return None, False
        return it.data(Qt.ItemDataRole.UserRole), bool(it.data(Qt.ItemDataRole.UserRole + 1))

    def _plot_selected(self):
        sid, _b = self.selected()
        self.plot.clear()
        if not sid:
            return
        try:
            s = rs.ReferenceLibrary().get(sid)
        except Exception:                                     # noqa: BLE001
            return
        if s is None:
            return
        self.plot.setLabel("left", f"Irradiance ({s.unit})")
        self.plot.plot(s.wl, s.values, pen=pg.mkPen((255, 170, 60), width=2))
        for lo, hi in ((float(s.wl[0]) - 1.0, s.valid_min_nm),
                       (s.valid_max_nm, float(s.wl[-1]) + 1.0)):
            if hi > lo:
                reg = pg.LinearRegionItem(values=(lo, hi), movable=False,
                                          brush=pg.mkBrush(128, 128, 128, 70),
                                          pen=pg.mkPen(None))
                reg.setZValue(-10)
                self.plot.addItem(reg)
        self.plot.autoRange()

    def import_clicked(self):
        path, _ = QFileDialog.getOpenFileName(
            self, "Import reference lamp", "",
            "Lamp files (*.lmp *.csv *.txt);;All files (*)")
        if not path:
            return
        try:
            dlg = _LampImportDialog(self, path)
        except Exception as e:                                # noqa: BLE001
            QMessageBox.critical(self, "Import failed", str(e))
            return
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return
        self.do_import(path, **dlg.values())

    def do_import(self, path, **kw):
        try:
            src = rs.ReferenceLibrary().import_file(path, **kw)
        except Exception as e:                                # noqa: BLE001
            QMessageBox.critical(self, "Import failed", str(e))
            return None
        self.lbl_status.setText(f"Imported '{src.name}'.")
        self.refresh()
        self.dlg.tab_wizard.refresh()
        return src

    def remove_clicked(self):
        sid, builtin = self.selected()
        if not sid:
            QMessageBox.information(self, "Lamps", "Select a lamp first.")
            return
        if builtin:
            QMessageBox.information(self, "Lamps", "Built-in sources cannot be removed.")
            return
        if not _ask(self, "Remove lamp", "Remove this reference source?"):
            return
        try:
            rs.ReferenceLibrary().remove(sid)
        except Exception as e:                                # noqa: BLE001
            QMessageBox.critical(self, "Remove failed", str(e))
            return
        self.refresh()
        self.plot.clear()
        self.dlg.tab_wizard.refresh()

"""
gui_calibration.py — Calibration wizards (desktop).

A single modal dialog with two tabs, both device-agnostic in their logic
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

import numpy as np
import pyqtgraph as pg

from PyQt6.QtCore import Qt, QTimer
from PyQt6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QGridLayout, QLabel, QPushButton,
    QComboBox, QSpinBox, QLineEdit, QTableWidget, QTableWidgetItem,
    QHeaderView, QTabWidget, QWidget, QMessageBox, QFileDialog, QAbstractItemView,
)

import calibration_profiles as calprof


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

        # captured data
        self._wl_capture = None          # (wl, inten) of the line lamp
        self._wl_pairs = []              # [(pixel, known_nm)]
        self._wl_last_fit = None         # dict from calibrate_from_lines
        self._ref = None                 # (wl, inten) response reference
        self._meas = None                # (wl, inten) response measurement
        self._resp_table = None          # computed Nx2 [[lambda, S]]

        root = QVBoxLayout(self)
        self.tabs = QTabWidget()
        self.tabs.addTab(self._build_wavelength_tab(), "Wavelength")
        self.tabs.addTab(self._build_response_tab(), "Response")
        root.addWidget(self.tabs, 1)

        btn = QPushButton("Close")
        btn.clicked.connect(self.accept)
        row = QHBoxLayout(); row.addStretch(1); row.addWidget(btn)
        root.addLayout(row)

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

    # ══════════════════════════════════════════════════════════════════════
    #  WAVELENGTH TAB
    # ══════════════════════════════════════════════════════════════════════
    def _build_wavelength_tab(self) -> QWidget:
        w = QWidget(); v = QVBoxLayout(w)

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
        top.addWidget(self.cmb_lamp)
        top.addSpacing(12)
        top.addWidget(QLabel("Degree:"))
        self.spn_degree = QSpinBox(); self.spn_degree.setRange(2, 3); self.spn_degree.setValue(3)
        top.addWidget(self.spn_degree)
        top.addStretch(1)
        self.btn_wl_capture = QPushButton("Capture && detect peaks")
        self.btn_wl_capture.clicked.connect(self._wl_capture_clicked)
        top.addWidget(self.btn_wl_capture)
        v.addLayout(top)

        self.plot_wl = pg.PlotWidget()
        self.plot_wl.setLabel("bottom", "Wavelength", units="nm")
        self.plot_wl.setLabel("left", "Intensity", units="ADC")
        self.plot_wl.setMinimumHeight(180)
        v.addWidget(self.plot_wl, 1)

        mid = QHBoxLayout()
        self.tbl_wl = QTableWidget(0, 2)
        self.tbl_wl.setHorizontalHeaderLabels(["Pixel", "Known λ (nm)"])
        self.tbl_wl.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self.tbl_wl.setEditTriggers(QAbstractItemView.EditTrigger.AllEditTriggers)
        mid.addWidget(self.tbl_wl, 1)

        btns = QVBoxLayout()
        b_rm = QPushButton("Remove row"); b_rm.clicked.connect(self._wl_remove_row)
        b_add = QPushButton("Add row");   b_add.clicked.connect(lambda: self._wl_add_row(0.0, 0.0))
        self.btn_wl_fit = QPushButton("Fit && apply"); self.btn_wl_fit.clicked.connect(self._wl_fit_clicked)
        self.btn_wl_save = QPushButton("Save to device"); self.btn_wl_save.clicked.connect(self._wl_save_clicked)
        self.btn_wl_save.setEnabled(False)
        for b in (b_add, b_rm, self.btn_wl_fit, self.btn_wl_save):
            btns.addWidget(b)
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
        self._cap = _Capturer(self.dash, self._capture_frames(), self.lbl_wl_result,
                              self._wl_capture_done)
        self._cap.start()

    def _wl_capture_done(self, wl, inten):
        self.btn_wl_capture.setEnabled(True)
        self.btn_wl_capture.setText("Capture && detect peaks")
        self._wl_capture = (wl, inten)
        if float(np.max(inten)) < 1.0:
            self.lbl_wl_result.setText(
                "<b>Captured signal is ~0.</b> Is the lamp on and the exposure set?")
            return
        # detect peaks in pixel space
        peaks = calprof.detect_peaks(inten, min_distance_px=10)
        lamp = self.cmb_lamp.currentText()
        known = [k for k in calprof.CALIBRATION_LINES[lamp]
                 if wl[0] <= k <= wl[-1]]
        pairs = calprof.auto_match_lines(peaks, wl, known, tol_nm=6.0)
        self.tbl_wl.setRowCount(0)
        for px, nm in pairs:
            self._wl_add_row(px, nm)
        self._wl_redraw_plot(peaks)
        self.lbl_wl_result.setText(
            f"Detected {len(peaks)} peaks, auto-assigned {len(pairs)} of "
            f"{len(known)} {lamp} lines in range. Check/edit the table, then Fit.")

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
        self._wl_last_fit = res
        self.btn_wl_save.setEnabled(True)

        # Honest quality report: residuals AND coverage / extrapolation.
        wl_new = backend.wavelength_array
        self.dash.active_wavelengths = wl_new  # live preview on the main scope
        self._wl_capture = (wl_new, self._wl_capture[1])  # re-plot on new axis
        self._wl_redraw_plot()

        line_lo, line_hi = min(nms), max(nms)
        dev_lo, dev_hi = float(wl_new[0]), float(wl_new[-1])
        max_res = res["max_error"]
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
        self.lbl_wl_result.setText(
            f"<b>Fit (degree {self.spn_degree.value()}, {len(nms)} lines):</b> "
            f"max residual {max_res:.3f} nm, RMS {rms:.3f} nm. "
            f"Applied live. {warn}")

    def _wl_save_clicked(self):
        if not self._wl_last_fit:
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
        from app_config import Config
        Config.set(key, list(self._wl_last_fit["coeffs"]))
        QMessageBox.information(
            self, "Saved",
            f"Wavelength calibration saved to '{key}'. It will load "
            f"automatically on the next connect.")

    # ══════════════════════════════════════════════════════════════════════
    #  RESPONSE TAB
    # ══════════════════════════════════════════════════════════════════════
    def _build_response_tab(self) -> QWidget:
        w = QWidget(); v = QVBoxLayout(w)

        intro = QLabel(
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
        self._cap = _Capturer(self.dash, self._capture_frames(), self.lbl_resp_status,
                              lambda wl, it: self._set_ref(wl, it, "live"))
        self._cap.start()

    def _set_ref(self, wl, inten, src):
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
        self._cap = _Capturer(self.dash, self._capture_frames(), self.lbl_resp_status,
                              self._set_meas)
        self._cap.start()

    def _set_meas(self, wl, inten):
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
        calprof.save_profile(dev, name, self._resp_table, meta)
        calprof.set_active_selection(dev, name)
        QMessageBox.information(
            self, "Saved",
            f"Profile '{name}' saved for {dev} and selected. Close this dialog "
            f"to apply it.")

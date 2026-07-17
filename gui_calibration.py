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
    QCheckBox,
)

import calibration_profiles as calprof
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
        (or broken) state. Save persists; Close cancels."""
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
        self._wl_saved = True
        extra = ""
        # The Ocean HDX defaults to its on-device factory axis; saving a
        # calibration means "use mine now", so flip it to the config poly.
        if dev == "Ocean HDX-UV-VIS":
            Config.set("ocean_use_device_wavelength", False)
            extra = ("<br>Switched 'ocean_use_device_wavelength' → false so "
                     "this calibration is used instead of the device's factory "
                     "axis. Set it back to true to revert to the device axis.")
        QMessageBox.information(
            self, "Saved",
            f"Wavelength calibration saved to '{key}'. It will load "
            f"automatically on the next connect.{extra}")

    # ══════════════════════════════════════════════════════════════════════
    #  RESPONSE TAB
    # ══════════════════════════════════════════════════════════════════════
    def _build_response_tab(self) -> QWidget:
        w = QWidget(); w.setObjectName("calTabPage"); v = QVBoxLayout(w)

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
        calprof.save_profile(dev, name, self._resp_table, meta)
        calprof.set_active_selection(dev, name)
        QMessageBox.information(
            self, "Saved",
            f"Profile '{name}' saved for {dev} and selected. Close this dialog "
            f"to apply it.")

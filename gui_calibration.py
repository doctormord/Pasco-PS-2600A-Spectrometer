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

from PyQt6.QtCore import Qt, QTimer, QUrl
from PyQt6.QtGui import QColor, QDesktopServices
from PyQt6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QGridLayout, QLabel, QPushButton,
    QComboBox, QSpinBox, QLineEdit, QTableWidget, QTableWidgetItem,
    QHeaderView, QTabWidget, QWidget, QMessageBox, QFileDialog, QAbstractItemView,
    QCheckBox, QListWidget, QListWidgetItem, QStackedWidget, QDoubleSpinBox,
    QFormLayout, QInputDialog, QDialogButtonBox, QSplitter, QSizePolicy,
)

import calibration_capture as cc
import calibration_profiles as calprof
import calibration_workflow as cw
import reference_sources as rs
import wavelength_calibration as wlcal
import gui_theme as gt
import gui_tables as gtab
from engine import LOCAL as _LOCAL
from gui_layout import (
    ElidedLabel, FixedTextPane, scroll_page, place_window, schedule_fix_onscreen,
    geometry_to_config,
)


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
        self.setSizeGripEnabled(True)
        # Styling comes exclusively from the central theme (gui_theme); the
        # geometry from the screen the main window is on (gui_layout).
        from app_config import Config
        place_window(self, dashboard, Config.get("calibration_dialog_geometry", None))
        self._placed_onscreen = False

        # captured data
        self._resume_after_capture = False  # re-pause the device after a capture
        # Wavelength session (shared Qt-free logic). It snapshots the axis as it
        # is on open, so Close (without Save) discards any live preview and a
        # rejected fit can be rolled back — no broken live view.
        self._wl_sess = None
        self._wl_session()
        self._ref = None                 # (wl, inten) response reference
        self._meas = None                # (wl, inten) response measurement
        self._resp_table = None          # computed Nx2 [[lambda, S]]

        root = QVBoxLayout(self)
        self.tabs = QTabWidget()
        self._busy = False
        self.tab_wizard = _SpectralWizardTab(self)
        self.tab_profiles = _ProfilesTab(self)
        self.tab_lamps = _LampsTab(self)
        from gui_linearity import LinearityTab
        self.tab_linearity = LinearityTab(self)
        # Every tab page sits in a scroll area: its minimum size can never force
        # the dialog larger than the window / screen.
        self._pages = {}
        for page, title in ((self._build_wavelength_tab(), "Wavelength"),
                            (self.tab_wizard, "Spectral calibration"),
                            (self.tab_profiles, "Profiles"),
                            (self.tab_lamps, "Lamps"),
                            (self.tab_linearity, "Linearity"),
                            (self._build_response_tab(), "Response (legacy)")):
            sa = page if getattr(page, "SELF_SCROLLING", False) else scroll_page(page)
            self._pages[id(sa)] = page
            self.tabs.addTab(sa, title)
        self.tabs.currentChanged.connect(self._on_tab_changed)
        root.addWidget(self.tabs, 1)

        self.btn_close = QPushButton("Close")
        self.btn_close.clicked.connect(self.accept)
        row = QHBoxLayout(); row.addStretch(1); row.addWidget(self.btn_close)
        root.addLayout(row)

    def page_at(self, i: int):
        """Tab page (inside its scroll area) at index i."""
        return self._pages.get(id(self.tabs.widget(i)))

    # ── geometry ─────────────────────────────────────────────────────────
    def showEvent(self, ev):                                 # noqa: N802
        super().showEvent(ev)
        if not self._placed_onscreen:
            self._placed_onscreen = True
            schedule_fix_onscreen(self)          # once, after the real frame exists

    def _save_geometry(self) -> None:
        try:
            g = geometry_to_config(self)
            if g is not None and self.isVisible():
                from app_config import Config
                Config.set("calibration_dialog_geometry", g)
        except Exception as e:                                # noqa: BLE001
            print(f"[calib] saving the dialog geometry failed: {e}")

    # ── busy handling (a wizard step runs in a worker thread) ────────────
    def set_busy(self, busy: bool, keep=None) -> None:
        """Lock the dialog while a run is active; `keep` is the tab that
        stays enabled (the running one; default: the spectral wizard)."""
        self._busy = bool(busy)
        keep = keep if keep is not None else self.tab_wizard
        self.btn_close.setEnabled(not busy)
        for i in range(self.tabs.count()):
            if self.page_at(i) is not keep:
                self.tabs.setTabEnabled(i, not busy)

    def done(self, r):
        if self._busy:
            return
        self._save_geometry()
        self.tab_wizard.restore_demo_spectrum()
        super().done(r)

    def _on_tab_changed(self, i):
        w = self.page_at(i)
        for t in (self.tab_wizard, self.tab_profiles, self.tab_lamps,
                  self.tab_linearity):
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

    def notify_scope_reference_changed(self) -> None:
        """The scope reference selection changed (verification sets it)."""
        fn = getattr(self.dash, "sync_scope_reference", None)
        if callable(fn):
            try:
                fn()
            except Exception as e:                            # noqa: BLE001
                print(f"[calib] sync_scope_reference failed: {e}")

    def engine(self):
        """The core engine behind the dashboard (None for stub dashboards)."""
        return getattr(self.dash, "engine", None)

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
            ax.enableAutoSIPrefix(False)      # always "nm", never "knm" / "Mnm"
        plot.showGrid(x=True, y=True, alpha=0.12)
        wl = getattr(self.dash, "active_wavelengths", None)
        if wl is not None and len(wl) > 1:
            lo, hi = float(wl[0]), float(wl[-1])
        else:
            lo, hi = default_x
        plot.setXRange(lo, hi, padding=0.02)

    @staticmethod
    def _style_cal_table(table):
        """Pixel ↔ λ input table: colours come from the central theme; only
        behaviour is set here (no row-number column, whole-row selection)."""
        table.setAlternatingRowColors(True)
        table.verticalHeader().setVisible(False)
        table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        table.setCornerButtonEnabled(False)

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
        v.addLayout(top)
        top = QHBoxLayout()
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

        self.lbl_wl_result = FixedTextPane(4)
        self.lbl_wl_result.setText("No fit yet.")
        v.addWidget(self.lbl_wl_result)
        return w

    def _wl_session(self):
        """The WavelengthSession of the current backend (created on first use)."""
        bk = self._backend()
        if bk is None:
            return None
        s = self._wl_sess
        if s is None or s.backend is not bk:
            s = self._wl_sess = wlcal.WavelengthSession(bk, self._device_name())
        return s

    def _wl_fill_table(self, pairs):
        self.tbl_wl.setRowCount(0)
        for px, nm in pairs:
            self._wl_add_row(px, nm)

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
        sess = self._wl_session()
        if sess is None:
            return
        sess.set_capture(wl, inten)
        if not sess.has_signal:
            self.lbl_wl_result.setText(
                "<b>No signal captured.</b> Check the lamp and the integration time.")
            return
        # detect peaks once; keep them so switching the lamp dropdown can
        # re-assign against the SAME capture without re-measuring.
        sess.detect_peaks()
        self._wl_rematch()

    def _wl_rematch(self):
        """(Re)assign the selected lamp's known lines to the peaks of the last
        capture. Wired to the lamp dropdown so picking a lamp pulls that lamp's
        lines. With 'Add to existing' checked, the matches are APPENDED (build a
        multi-lamp fit); otherwise they REPLACE the table (single lamp)."""
        sess = self._wl_session()
        if sess is None or sess.capture is None or not sess.peaks:
            return
        peaks = sess.peaks
        lamp = self.cmb_lamp.currentText()
        accum = self.chk_accum.isChecked()
        info = sess.match(lamp, accumulate=accum)
        self._wl_fill_table(sess.pairs)
        self._wl_redraw_plot(peaks)
        total = info["n_total"]
        dropped = info["n_skipped"]
        note = (f", {dropped} skipped (no clean peak)" if dropped > 0 else "")
        verb = (f"added {info['n_added']}" if accum
                else f"assigned {info['n_assigned']}")
        self.lbl_wl_result.setText(
            f"Detected {len(peaks)} peaks; {verb} of {info['n_known']} {lamp} "
            f"lines in range{note}. Table now holds {total} line(s). "
            + ("Capture further lamps to add lines, then fit. "
               if accum else
               "Enable 'Add to existing' to combine several lamps (e.g. Cd+Hg and "
               "Ne/Ar for the red/NIR range) into one fit. ")
            + "Check the assignments, then fit.")

    def _wl_clear_clicked(self):
        """Reset the line table and the current capture so the user can start
        the lamp selection over (e.g. after picking the wrong lamp)."""
        self.tbl_wl.setRowCount(0)
        sess = self._wl_session()
        if sess is not None:
            sess.clear()
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
            if self._wl_sess is not None:
                self._wl_sess.discard()
        except Exception:
            pass
        super().closeEvent(ev)

    def _wl_redraw_plot(self, peaks=None, wl_fit=None):
        self.plot_wl.clear()
        sess = self._wl_session()
        if sess is None or sess.capture is None:
            return
        wl, inten = sess.capture
        self.plot_wl.plot(wl, inten, pen=pg.mkPen((120, 180, 255), width=1))
        # assigned lines as vertical markers
        for _, nm in self._wl_table_pairs():
            self.plot_wl.addItem(pg.InfiniteLine(
                pos=nm, angle=90, pen=pg.mkPen((255, 170, 60), style=Qt.PenStyle.DashLine)))
        self.plot_wl.autoRange()

    def _wl_fit_clicked(self):
        sess = self._wl_session()
        deg = self.spn_degree.value()
        if sess is None:
            QMessageBox.warning(self, "Not connected", "Connect a device first.")
            return
        sess.set_pairs(self._wl_table_pairs())
        if len(sess.pairs) < deg + 1:
            QMessageBox.warning(self, "Too few lines",
                                f"A degree-{deg} fit needs at least {deg + 1} "
                                f"assigned lines (have {len(sess.pairs)}). Add lines "
                                f"or lower the degree.")
            return
        try:
            res = sess.fit(deg)
        except wlcal.NotSupported:
            QMessageBox.information(
                self, "Not supported",
                "This device does not support wavelength calibration "
                "(its axis is fixed/virtual).")
            return
        except Exception as e:
            QMessageBox.critical(self, "Fit failed", str(e))
            return
        wl_new = res["wavelengths"]
        if not res["accepted"]:
            # Plausibility guard rejected the fit; the session already rolled the
            # device back. Re-sync the display to the restored axis.
            if hasattr(self.dash, "sync_active_axis"):
                self.dash.sync_active_axis(wl_new)
            self.btn_wl_save.setEnabled(False)
            self.lbl_wl_result.setText(
                f"<b style='color:{_T().DANGER}'>Fit rejected</b> — "
                + "; ".join(res["reasons"])
                + ". Typical cause: a mis-assigned line (wrong lamp or a peak "
                "matched to the wrong wavelength). The previous axis is kept. "
                "Correct the line table, then fit again.")
            self._wl_redraw_plot()
            return

        self.btn_wl_save.setEnabled(True)
        # Live preview on the main scope. Route through sync_active_axis so the
        # reference library is re-interpolated onto the new axis in the SAME
        # step — otherwise the reference lines slide along with the spectrum in
        # the background and the fit looks wrong even when it's right.
        if hasattr(self.dash, "sync_active_axis"):
            self.dash.sync_active_axis(wl_new)
        else:
            self.dash.active_wavelengths = wl_new
        self._wl_redraw_plot()

        warn = ""
        if res["coverage_warning"]:
            lo, hi = res["line_range"]
            dlo, dhi = res["device_range"]
            warn = (f"<br><span style='color:{_T().WARN}'><b>Coverage warning:</b> "
                    f"lines span {lo:.0f}–{hi:.0f} nm but the device "
                    f"covers {dlo:.0f}–{dhi:.0f} nm. The axis is "
                    f"<b>extrapolated</b> outside the line span — small residuals "
                    f"do not guarantee accuracy there. Add lines from other "
                    f"lamps for full coverage.</span>")
        elif res["rms_warning"]:
            warn = (f"<br><span style='color:{_T().WARN}'>RMS {res['rms']:.2f} nm is a bit "
                    f"high — check for a mis-assigned line before saving.</span>")
        self.lbl_wl_result.setText(
            f"<b>Fit (degree {deg}, {res['n_lines']} lines):</b> "
            f"max residual {res['max_error']:.3f} nm, RMS {res['rms']:.3f} nm. "
            f"Applied live. {warn}")

    def _wl_save_clicked(self):
        sess = self._wl_session()
        if sess is None or not sess.last_fit:
            return
        if not self.chk_wl_confirm.isChecked():
            QMessageBox.warning(
                self, "Confirmation required",
                "Enable 'Back up & confirm' first. Saving replaces the current "
                "wavelength calibration; a timestamped backup is written before.")
            return
        if sess.config_key() is None:
            QMessageBox.information(
                self, "Not persistable",
                f"No wavelength-coefficient config key for '{sess.backend_name}'. "
                f"The fit is applied for this session but not saved.")
            return
        try:
            out = sess.save(True)
        except wlcal.WavelengthError as e:
            QMessageBox.warning(self, "Backup failed", str(e))
            return
        self.chk_wl_confirm.setChecked(False)   # require a fresh tick next time
        extra = f"<br>{out['extra_note']}" if out["extra_note"] else ""
        import os
        QMessageBox.information(
            self, "Saved",
            f"Wavelength calibration saved to '{out['key']}'. It will load "
            f"automatically on the next connect.<br>Backup of the previous "
            f"calibration: <b>{os.path.basename(out['backup_path'])}</b>.{extra}")

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
        self.lbl_ref = ElidedLabel("none")
        grid.addWidget(self.lbl_ref, 0, 1, 1, 3)
        b_ref_cap = QPushButton("Capture live"); b_ref_cap.clicked.connect(self._ref_capture)
        b_ref_load = QPushButton("Load CSV…");   b_ref_load.clicked.connect(self._ref_load)
        b_ref_save = QPushButton("Save CSV…");   b_ref_save.clicked.connect(self._ref_save)
        grid.addWidget(b_ref_cap, 1, 0); grid.addWidget(b_ref_load, 1, 1); grid.addWidget(b_ref_save, 1, 2)

        grid.addWidget(QLabel("<b>Measurement</b> (device under test)"), 2, 0)
        self.lbl_meas = ElidedLabel("none")
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

        self.lbl_resp_status = FixedTextPane(3)
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
                f"<span style='color:{_T().WARN}'><b>Coverage warning:</b> {warn}</span>")
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


def _shade_masked(plot, wl, segments):
    """Grey out everything outside the valid segments — including masked gaps
    INSIDE the outer valid range (they are never reported as valid)."""
    wl = np.asarray(wl, float)
    if wl.size < 2:
        return
    d = float(np.median(np.diff(wl))) / 2.0
    lo_all, hi_all = float(wl[0]) - d, float(wl[-1]) + d
    edges, cur = [], lo_all
    for lo, hi in (segments or []):
        if lo - d > cur:
            edges.append((cur, lo - d))
        cur = max(cur, hi + d)
    if cur < hi_all:
        edges.append((cur, hi_all))
    for a, b in edges:
        reg = pg.LinearRegionItem(values=(a, b), movable=False,
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

_LAMP_STEPS = ["Selection", "Wavelength check", "Integration time", "Dark before",
               "Calibration + repeat", "t/2 capture", "Dark after + checks", "Compute",
               "Save", "Verification"]
_DERIV_KEYS = ["selection"] + [k for k, _ in cw.DERIVATION_STEPS] + ["protocol"]
_DERIV_STEPS = (["Selection"] + [label for _, label in cw.DERIVATION_STEPS]
                + ["Protocol"])
_DERIV_IX = {k: i for i, k in enumerate(_DERIV_KEYS)}
_STATE_TEXT = {"waiting": "waiting", "running": "running", "ok": "OK",
               "warning": "warning", "error": "ERROR", "failed": "FAILED"}
# Leading symbol: the state stays recognisable without colour.
_STATE_MARK = {"waiting": "○", "running": "▶", "ok": "✓", "warning": "!",
               "error": "✗", "failed": "✗"}


def _state_color(state: str) -> str:
    """Theme token per step state (contrast ≥ 4.5:1 on BG1/BG2, both themes)."""
    t = _T()
    return {"error": t.DANGER, "failed": t.DANGER, "warning": t.WARN,
            "ok": t.OK, "running": t.ACCENT}.get(state, t.FG2)


def _span(color_token: str, html: str) -> str:
    return f"<span style='color:{color_token}'>{html}</span>"


class _ResultPane(FixedTextPane):
    """Result text with its own space in the plot splitter: never fixed to a
    height the plots could squeeze, at least `lines` lines tall, longer content
    scrolls inside the pane (the top line is always the first one shown)."""

    def __init__(self, lines: int = 6, parent=None):
        super().__init__(lines, parent)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)

    def _update_height(self) -> None:
        fw = 2 * self.frameWidth()
        self.setMinimumHeight(self.fontMetrics().lineSpacing() * self._lines + 10 + fw)
        self.setMaximumHeight(16777215)


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
        self._demo_restore = None            # (backend, previous spectrum name)
        self._step_names: list[str] = []
        self.step_states: dict[int, tuple] = {}
        self.last_error: str | None = None
        self.last_status: str = ""
        self.protocol_paths: list[str] = []
        self.protocol_message: str = ""
        self._build()
        self.refresh()

    # ── UI ───────────────────────────────────────────────────────────────
    # The wizard manages its own scrolling: the body scrolls, the footer with
    # status, protocol and Start / Cancel is always visible.
    SELF_SCROLLING = True

    def _build(self):
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        body = QWidget()
        body.setObjectName("calTabPage")
        v = QVBoxLayout(body)
        v.setContentsMargins(8, 8, 8, 4)

        # Left: selection, acquisition settings and step list (one column);
        # right: plots + result. User-resizable, never widens the window.
        self.split = QSplitter(Qt.Orientation.Horizontal)
        self.split.setChildrenCollapsible(False)
        left = QWidget()
        lv = QVBoxLayout(left)
        lv.setContentsMargins(0, 0, 6, 0)
        lv.setSpacing(6)

        self.cmb_mode = QComboBox()
        self.cmb_mode.addItems(["Calibrate against a reference lamp",
                                "Derive from other optics"])
        self.cmb_mode.setToolTip("Calibration mode")
        self.cmb_mode.currentIndexChanged.connect(self._mode_changed)
        lv.addWidget(self.cmb_mode)

        self.stack = QStackedWidget()
        self.stack.addWidget(self._build_lamp_form())
        self.stack.addWidget(self._build_deriv_form())
        lv.addWidget(self.stack)

        g = QGridLayout()
        g.setHorizontalSpacing(6)
        self.lbl_itime_cap = QLabel("Integration:")
        g.addWidget(self.lbl_itime_cap, 0, 0)
        self.cmb_itime = QComboBox()
        self.cmb_itime.addItems(["Auto", "Fixed"])
        self.cmb_itime.setToolTip("Auto: exposure to the configured target band "
                                  "(calibration_target_frac_lo/hi, limited by "
                                  "saturation_warn_frac). Fixed: the value on the right.")
        self.cmb_itime.currentIndexChanged.connect(self._itime_mode_changed)
        g.addWidget(self.cmb_itime, 0, 1)
        self.spn_ms = QDoubleSpinBox()
        self.spn_ms.setRange(0.01, 60000.0); self.spn_ms.setDecimals(2)
        self.spn_ms.setValue(100.0); self.spn_ms.setSuffix(" ms")
        self.spn_ms.setEnabled(False)
        g.addWidget(self.spn_ms, 0, 2)
        g.addWidget(QLabel("Frames:"), 1, 0)
        self.spn_frames = QSpinBox(); self.spn_frames.setRange(2, 1000)
        self.spn_frames.setToolTip("Frames averaged per capture")
        self.spn_frames.setValue(cw.WorkflowConfig.from_config().n_frames)
        g.addWidget(self.spn_frames, 1, 1)
        self.lbl_itime = ElidedLabel("")
        g.addWidget(self.lbl_itime, 1, 2)
        g.setColumnStretch(2, 1)
        lv.addLayout(g)

        self.lst_steps = QListWidget()
        self.lst_steps.setObjectName("calStepList")
        self.lst_steps.setWordWrap(True)
        self.lst_steps.setTextElideMode(Qt.TextElideMode.ElideNone)
        self.lst_steps.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.lst_steps.setResizeMode(QListWidget.ResizeMode.Adjust)
        self.lst_steps.setUniformItemSizes(False)
        self.lst_steps.setSelectionMode(QAbstractItemView.SelectionMode.NoSelection)
        self.lst_steps.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.lst_steps.setMinimumHeight(100)
        lv.addWidget(self.lst_steps, 1)
        left.setMinimumWidth(300)
        self.split.addWidget(left)

        right = QWidget()
        rv = QVBoxLayout(right)
        rv.setContentsMargins(6, 0, 0, 0)
        rv.setSpacing(6)
        plots = QSplitter(Qt.Orientation.Vertical)
        plots.setChildrenCollapsible(False)
        self.plot_in = pg.PlotWidget()
        self.plot_in.setLabel("bottom", "Wavelength", units="nm")
        self.plot_in.setLabel("left", "normalised")
        self.plot_in.addLegend()
        self.plot_in.setMinimumHeight(90)
        self.dlg._style_cal_plot(self.plot_in)
        self.plot_out = pg.PlotWidget()
        self.plot_out.setLabel("bottom", "Wavelength", units="nm")
        self.plot_out.setLabel("left", "S / K")
        self.plot_out.addLegend()
        self.plot_out.setMinimumHeight(90)
        self.dlg._style_cal_plot(self.plot_out)
        self._reset_plots()
        plots.addWidget(self.plot_in)
        plots.addWidget(self.plot_out)
        # The result pane is the third pane of the splitter: it keeps its own
        # space (minimum height of 6 lines) and is never covered by the plots.
        self.lbl_result = _ResultPane(6)
        plots.addWidget(self.lbl_result)
        plots.setStretchFactor(0, 2)
        plots.setStretchFactor(1, 2)
        plots.setStretchFactor(2, 2)
        plots.setSizes([200, 200, 200])
        self.plots_split = plots
        rv.addWidget(plots, 1)
        self.split.addWidget(right)
        self.split.setStretchFactor(0, 0)
        self.split.setStretchFactor(1, 1)
        self.split.setSizes([380, 800])
        v.addWidget(self.split, 1)

        self.body_scroll = scroll_page(body)
        outer.addWidget(self.body_scroll, 1)

        # Footer: always visible.
        foot = QWidget()
        foot.setObjectName("calTabPage")
        fh = QHBoxLayout(foot)
        fh.setContentsMargins(8, 4, 8, 6)
        info = QVBoxLayout()
        info.setSpacing(2)
        self.lbl_status = ElidedLabel("Ready.")
        info.addWidget(self.lbl_status)
        prow = QHBoxLayout()
        prow.setSpacing(6)
        prow.addWidget(QLabel("Protocol:"))
        self.lbl_protocol = ElidedLabel("—", mode=Qt.TextElideMode.ElideMiddle)
        prow.addWidget(self.lbl_protocol, 1)
        info.addLayout(prow)
        fh.addLayout(info, 1)
        self.btn_open_folder = QPushButton("Open folder")
        self.btn_open_folder.setEnabled(False)
        self.btn_open_folder.clicked.connect(self._open_protocol_folder)
        fh.addWidget(self.btn_open_folder)
        fh.addSpacing(12)
        self.btn_cancel = QPushButton("Cancel")
        self.btn_cancel.setEnabled(False)
        self.btn_cancel.setToolTip("Stops after the running step.")
        self.btn_cancel.clicked.connect(self._cancel_clicked)
        self.btn_start = QPushButton("Start")
        self.btn_start.setObjectName("primaryBtn")
        self.btn_start.setToolTip("Runs the fixed sequence. A protocol (PDF/CSV) is "
                                  "written for every completed computation.")
        self.btn_start.clicked.connect(self.start)
        fh.addWidget(self.btn_cancel)
        fh.addWidget(self.btn_start)
        outer.addWidget(foot)
        self._set_steps(_LAMP_STEPS)

    def _reset_plots(self):
        """Empty plots with the device's wavelength span and a 0..1 y range
        (no stale autorange from a previous run)."""
        wl = getattr(self.dlg.dash, "active_wavelengths", None)
        lo, hi = ((float(wl[0]), float(wl[-1])) if wl is not None and len(wl) > 1
                  else (300.0, 1050.0))
        for p in (self.plot_in, self.plot_out):
            p.clear()
            p.setXRange(lo, hi, padding=0.02)
            p.setYRange(0.0, 1.0, padding=0.05)

    @staticmethod
    def _form() -> tuple[QWidget, QFormLayout]:
        w = QWidget()
        f = QFormLayout(w)
        f.setContentsMargins(0, 0, 0, 0)
        f.setHorizontalSpacing(8)
        f.setVerticalSpacing(6)
        f.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
        f.setRowWrapPolicy(QFormLayout.RowWrapPolicy.DontWrapRows)
        return w, f

    @staticmethod
    def _narrow_combo(cmb: QComboBox, chars: int = 12) -> QComboBox:
        cmb.setSizeAdjustPolicy(
            QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        cmb.setMinimumContentsLength(chars)
        return cmb

    def _build_lamp_form(self) -> QWidget:
        w, f = self._form()
        self.lbl_device = ElidedLabel("")
        f.addRow("Device:", self.lbl_device)
        self.cmb_optics = self._narrow_combo(QComboBox())
        self.cmb_optics.setEditable(True)
        self.cmb_optics.lineEdit().setPlaceholderText("Optics configuration name")
        self.cmb_optics.setToolTip("Optics configuration (name of the profile series)")
        self.cmb_optics.activated.connect(self._optics_picked)
        f.addRow("Optics:", self.cmb_optics)
        self.edit_notes = QLineEdit(); self.edit_notes.setPlaceholderText("Notes")
        f.addRow("Notes:", self.edit_notes)
        self.cmb_input = QComboBox(); self.cmb_input.addItems(list(calprof.INPUT_OPTICS))
        self.cmb_input.setToolTip("Input optic: cosine corrector, bare fibre or sphere")
        self.cmb_input.currentIndexChanged.connect(self._input_changed)
        self.cmb_type = QComboBox()
        self.cmb_type.addItems([calprof.TYPE_RELATIVE, calprof.TYPE_ABSOLUTE])
        row = QHBoxLayout(); row.setSpacing(6)
        row.addWidget(self.cmb_input, 1)
        row.addWidget(QLabel("Type:"))
        row.addWidget(self.cmb_type, 1)
        f.addRow("Input:", row)
        self.cmb_ref = self._narrow_combo(QComboBox())
        self.cmb_ref.currentIndexChanged.connect(self._ref_changed)
        f.addRow("Reference:", self.cmb_ref)
        # Space for the source notes is reserved permanently, so showing /
        # hiding a warning never moves the rest of the dialog. Full width.
        self.lbl_ref_info = FixedTextPane(3)
        f.addRow(self.lbl_ref_info)
        return w

    def _itime_row(self, which: str):
        """Auto / fixed integration time of one optics (own value per side)."""
        cmb = QComboBox()
        cmb.addItems(["Auto", "Fixed"])
        cmb.setToolTip("Auto: exposure to the configured target band with this "
                       "optics mounted and the source on. Fixed: the value on the right.")
        spn = QDoubleSpinBox()
        spn.setRange(0.01, 60000.0); spn.setDecimals(2)
        spn.setValue(100.0); spn.setSuffix(" ms")
        spn.setEnabled(False)
        cmb.currentIndexChanged.connect(
            lambda _i=0, c=cmb, s=spn: s.setEnabled(c.currentIndex() == 1))
        row = QHBoxLayout(); row.setSpacing(6)
        row.addWidget(cmb)
        row.addWidget(spn, 1)
        return cmb, spn, row

    def _build_deriv_form(self) -> QWidget:
        w, f = self._form()
        hint = QLabel("Same source with the known and the new optics: "
                      "S_new = S_known · C_new / C_known. Each optics has its own "
                      "integration time and dark spectra.")
        hint.setWordWrap(True)
        f.addRow(hint)
        self.lbl_source_hint = QLabel(f"<b>{cw.DERIVATION_SOURCE_HINT}</b>")
        self.lbl_source_hint.setWordWrap(True)
        self.lbl_source_hint.setTextFormat(Qt.TextFormat.RichText)
        f.addRow(self.lbl_source_hint)
        self.cmb_known = self._narrow_combo(QComboBox())
        f.addRow("Known:", self.cmb_known)
        self.edit_new_name = QLineEdit()
        self.edit_new_name.setPlaceholderText("Name of the NEW optics configuration")
        f.addRow("New optics:", self.edit_new_name)
        self.cmb_new_input = QComboBox()
        self.cmb_new_input.addItems(list(calprof.INPUT_OPTICS))
        f.addRow("Input:", self.cmb_new_input)
        self.edit_new_notes = QLineEdit()
        self.edit_new_notes.setPlaceholderText("Notes")
        f.addRow("Notes:", self.edit_new_notes)
        self.cmb_itime_known, self.spn_ms_known, row = self._itime_row("known")
        f.addRow("Integration (known):", row)
        self.cmb_itime_new, self.spn_ms_new, row = self._itime_row("new")
        f.addRow("Integration (new):", row)
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
            + ("" if be is not None else " · no device connected")
            + (" · no serial number: profiles are not bound to this unit"
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
            self.lbl_ref_info.setText(_span(_T().DANGER, str(e)))
        self.cmb_ref.blockSignals(True)
        self.cmb_ref.clear()
        for s in srcs:
            label = f"{s.name} ({s.serial})" if s.serial else s.name
            self.cmb_ref.addItem(label, s.id)
            self.cmb_ref.setItemData(self.cmb_ref.count() - 1, label,
                                     Qt.ItemDataRole.ToolTipRole)
        i = self.cmb_ref.findData(cur_id) if cur_id is not None else -1
        if i >= 0:
            self.cmb_ref.setCurrentIndex(i)
        self.cmb_ref.blockSignals(False)
        cur_known = self.cmb_known.currentData()
        self.cmb_known.clear()
        for p in calprof.profiles_for_device(name, serial, fp,
                                             nl_active=calprof.device_nl_active(be)):
            # only v2 profiles valid on this unit with the current λ calibration
            if be is not None and not cw.derivation_source_ok(p, be, name)[0]:
                continue
            if calprof.is_implausible(p):          # never derive from a failed profile
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

    def _backend_is_demo(self, be=None) -> bool:
        be = self.dlg._backend() if be is None else be
        return be is not None and callable(getattr(be, "set_spectrum", None))

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
        lines = [f"Unit: {src.unit} · valid {_fmt_range(src.valid_min_nm, src.valid_max_nm)}"
                 f" · intended optic: {src.input_optic or 'not specified'}"]
        if getattr(src, "builtin", False):
            if self._backend_is_demo() and src.id in rs.DEMO_SPECTRUM_FOR_SOURCE:
                lines.append("Synthetic demo source. The Demo backend simulates this "
                             "lamp for the duration of the run.")
            else:
                lines.append(_span(_T().WARN, "<b>Synthetic demo source</b> — valid "
                                   "only with the Demo backend, not for a real lamp."))
        if not calprof.is_absolute_reference_unit(src.unit):
            lines.append("Relative unit: only a relative calibration is possible.")
        inp = self.cmb_input.currentText()
        if src.input_optic and src.input_optic != inp:
            lines.append(_span(_T().WARN, f"<b>Warning:</b> source specified for input "
                               f"optic '{src.input_optic}', selected: '{inp}'."))
        self.lbl_ref_info.setText("<br>".join(lines))

    def _itime_mode_changed(self, *_):
        self.spn_ms.setEnabled(self.cmb_itime.currentIndex() == 1)

    def _mode_changed(self, i):
        if self.running:
            return
        self.stack.setCurrentIndex(i)
        for wdg in (self.lbl_itime_cap, self.cmb_itime, self.spn_ms):
            wdg.setVisible(i == 0)               # derivation: one time per optics
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
        txt = f"{_STATE_MARK[state]} {self._step_names[i]}: {_STATE_TEXT[state]}"
        if detail:
            txt += f" – {detail}"
        it.setText(txt)
        it.setToolTip(txt)
        it.setForeground(QColor(_state_color(state)))

    # ── protocol location ────────────────────────────────────────────────
    def _set_protocol_display(self):
        if self.protocol_paths:
            self.lbl_protocol.setText("; ".join(self.protocol_paths))
            self.btn_open_folder.setEnabled(True)
        else:
            self.lbl_protocol.setText(self.protocol_message or "—")
            self.btn_open_folder.setEnabled(False)

    def _open_protocol_folder(self):
        if self.protocol_paths:
            QDesktopServices.openUrl(QUrl.fromLocalFile(
                os.path.dirname(self.protocol_paths[0])))

    # ── demo backend: simulate the selected demo source during a run ─────
    def _apply_demo_spectrum(self, be, src) -> str | None:
        name = rs.DEMO_SPECTRUM_FOR_SOURCE.get(getattr(src, "id", None))
        if not name or not self._backend_is_demo(be):
            return None
        prev = be.set_spectrum(name)
        if self._demo_restore is None:           # keep the ORIGINAL spectrum
            self._demo_restore = (be, prev)
        return name

    def restore_demo_spectrum(self) -> None:
        if self._demo_restore is None:
            return
        be, prev = self._demo_restore
        self._demo_restore = None
        try:
            be.set_spectrum(prev)
        except Exception as e:                                # noqa: BLE001
            print(f"[calib] restoring the demo spectrum failed: {e}")

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
            self._note = f"Stability check: second capture in {rem:.0f} s…"
            time.sleep(min(0.1, rem))

    def _begin(self, gen):
        self.running = True
        self._cancel = False
        self.last_error = None
        self.last_status = ""
        self.protocol_paths = []
        self.protocol_message = ""
        self._set_protocol_display()
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
        self.restore_demo_spectrum()
        if self._resumed:
            self.dlg._restore_pause_after_capture()
            self._resumed = False
        self.dlg.set_busy(False)
        self.btn_start.setEnabled(True)
        self.btn_cancel.setEnabled(False)
        self.cmb_mode.setEnabled(True)
        self._set_protocol_display()

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
        self._reset_plots()
        cfg = cw.WorkflowConfig.from_config()
        cfg.n_frames = int(self.spn_frames.value())
        def fixed_us(cmb, spn):
            return None if cmb.currentIndex() == 0 else int(round(spn.value() * 1000.0))
        us = fixed_us(self.cmb_itime, self.spn_ms)
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
                demo = self._apply_demo_spectrum(be, src)
                if demo:
                    self._wf.warnings.append(
                        f"Demo backend: simulated source set to '{demo}' "
                        f"(matching reference '{src.name}') for this run.")
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
                gen = self._gen_deriv(
                    self._dw, fixed_us(self.cmb_itime_known, self.spn_ms_known),
                    fixed_us(self.cmb_itime_new, self.spn_ms_new))
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

    def _confirm_implausible(self, failures, other_failed=()) -> bool:
        """Saving a result that failed the plausibility check needs an explicit
        decision; the default button is 'No'."""
        failures = list(failures) + [f"{c} check failed" for c in other_failed]
        box = QMessageBox(self)
        box.setIcon(QMessageBox.Icon.Warning)
        box.setWindowTitle("Plausibility check failed")
        box.setText("The calibration result failed the plausibility check:")
        box.setInformativeText(
            "\n".join(f"• {f}" for f in failures)
            + "\n\nThe calibration source must be smooth and broadband. "
              "Save the profile anyway? It will be marked as not plausible "
              "and will not be activated.")
        box.setStandardButtons(QMessageBox.StandardButton.Yes
                               | QMessageBox.StandardButton.No)
        box.setDefaultButton(QMessageBox.StandardButton.No)
        return self._exec_box(box) == QMessageBox.StandardButton.Yes

    def _exec_box(self, box):
        """Separate hook so tests can answer the dialog."""
        return QMessageBox.StandardButton(box.exec())

    def _engine(self):
        """The dashboard's engine (Qt-free core), or None (stub dashboards)."""
        return getattr(self.dlg.dash, "engine", None)

    def _gen_lamp(self, wf, us):
        step = 0
        try:
            self._state(0, "ok", f"{wf.optics['name']} · {wf.optics['input']} · {wf.kind}")
            # 1 wavelength check
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
            # 2 integration time
            step = 2; self._checkpoint(); self._state(2, "running")
            self.lbl_status.setText("Setting the integration time…")
            info = yield (lambda: wf.set_integration(us, auto=us is None))
            half = info.get("half_us")
            self.lbl_itime.setText(
                f"t_int = {info['integration_us']/1000:.2f} ms"
                + (f", half = {half/1000:.2f} ms" if half else ", no half value"))
            self._state(2, "ok" if info.get("converged", True) else "warning",
                        self.lbl_itime.text())
            # 3 darks BEFORE
            step = 3; self._checkpoint(); self._state(3, "running")
            if wf.needs_user_cover:
                if not _ask(self, "Dark spectrum",
                            "Block the light path / cover the lamp.\n\n"
                            "The dark spectra are captured next.", ok_cancel=True):
                    raise _Abort("cancelled at the dark prompt")
            self.lbl_status.setText("Capturing dark spectra (before)…")
            dk = yield wf.capture_darks
            if wf.needs_user_cover:
                if not _ask(self, "Release the light path",
                            "Release the light path / uncover the lamp.\n\n"
                            "The calibration spectrum is captured next.",
                            ok_cancel=True):
                    raise _Abort("cancelled at the release prompt")
            self._state(3, "ok", f"peak {dk['dark_peak']:.0f} counts")
            # 4 calibration + repeat (provisional stability)
            step = 4; self._checkpoint(); self._state(4, "running")
            self.lbl_status.setText("Capturing the calibration spectrum…")
            cal = yield (lambda: wf.capture_calibration(sleep=self._worker_sleep))
            self._note = ""
            st = cal["stability"]["p95_pct"]
            self._state(4, "ok" if cal["pass"] else "warning",
                        f"stability {st:.2f} % (provisional)")
            # 5 t/2 capture (provisional cross check)
            step = 5; self._checkpoint(); self._state(5, "running")
            self.lbl_status.setText("Integration-time cross check (t/2)…")
            cx = yield wf.crosscheck_integration
            if cx.get("skipped"):
                self._state(5, "warning", "skipped")
            else:
                self._state(5, "ok" if cx["pass"] else "warning",
                            f"{cx['crosscheck']['p95_pct']:.2f} % (provisional)")
            # 6 darks AFTER + final checks
            step = 6; self._checkpoint(); self._state(6, "running")
            if wf.needs_user_cover:
                if not _ask(self, "Dark spectrum (after)",
                            "Block the light path / cover the lamp again.\n\n"
                            "The dark spectra AFTER the light captures are taken next.",
                            ok_cancel=True):
                    raise _Abort("cancelled at the second dark prompt")
            self.lbl_status.setText("Capturing dark spectra (after)…")
            ev = yield wf.capture_darks_after
            if wf.needs_user_cover:
                if not _ask(self, "Release the light path",
                            "Release the light path / uncover the lamp (for the "
                            "verification measurement).", ok_cancel=True):
                    raise _Abort("cancelled at the release prompt")
            self._final_check_states(wf, ev)
            # 7 compute + plausibility
            step = 7; self._checkpoint(); self._state(7, "running")
            try:
                res = yield wf.compute
            except ValueError as e:                       # no valid pixel at all
                self._state(7, "failed", str(e))
                self._state(8, "failed", "not saved")
                self.last_status = "failed"
                self.lbl_result.setText(_span(_T().DANGER, f"<b>FAILED</b>: {e}"))
                yield from self._write_protocol(wf)
                self.lbl_status.setText("Calibration failed: no valid pixels, "
                                        "no profile saved.")
                return
            self._show_lamp_result(wf, res)
            segs = calprof.format_segments(res.get("segments_nm"), 3)
            if wf.plausible:
                self._state(7, "ok", f"valid {segs}")
            else:
                self._state(7, "failed", "plausibility check failed: "
                            + "; ".join(wf.plausibility["failures"]))
            # 8 save
            step = 8; self._checkpoint()
            activate = True
            if not wf.plausible:
                other = [n for k, n in (("stability_pass", "Stability"),
                                        ("itime_pass", "Integration-time cross"))
                         if wf.checks.get(k) is False]
                if not self._confirm_implausible(wf.plausibility["failures"], other):
                    self._state(8, "failed", "not saved (plausibility check failed)")
                    self.last_status = "failed"
                    yield from self._write_protocol(wf)
                    self.lbl_status.setText("Calibration failed: result not plausible, "
                                            "no profile saved.")
                    return
                activate = False
            elif any(wf.checks.get(k) is False for k in ("stability_pass", "itime_pass")):
                if not _ask(self, "Checks failed",
                            "The stability and/or integration-time cross check "
                            "failed.\n\nSave the profile anyway?"):
                    self._state(8, "warning", "not saved (checks failed)")
                    self.last_status = "not_saved"
                    yield from self._write_protocol(wf)
                    self.lbl_status.setText("Not saved.")
                    return
            self._state(8, "running")
            saved = yield (lambda: wf.save(allow_implausible=not activate))
            if activate:
                calprof.set_active_selection(wf.backend_name, saved[0]["id"])
            self.dlg.notify_profiles_changed()
            pid = f"profile {saved[0]['id']} v{saved[0]['version']}"
            self._state(8, "ok" if activate else "warning",
                        f"{pid} saved and active" if activate
                        else f"{pid} saved as NOT PLAUSIBLE, not activated")
            # 9 verification: the reference is measured through the live
            # pipeline WITH the new profile (never aborts the run).
            step = 9; self._checkpoint(); self._state(9, "running")
            engine = self._engine()
            if engine is None:
                self._state(9, "warning", "no engine available")
                wf.warnings.append("Verification skipped: no engine available.")
            else:
                self.lbl_status.setText("Verification: measuring the reference source…")
                ver = None
                try:
                    ver = yield (lambda: engine.verify_profile(
                        saved[0]["id"], wf.reference.id, owner=_LOCAL))
                except Exception as e:                        # noqa: BLE001
                    self._state(9, "warning", f"failed: {e}")
                    wf.warnings.append(f"Verification failed: {e}")
                if ver is not None:
                    wf.verification = ver
                    o = ver["overall"]
                    self._state(9, "ok" if ver.get("pass") else "warning",
                                f"rms {o['rms_pct']:.2f} %, max {o['max_abs_pct']:.2f} %")
                    self.dlg.notify_scope_reference_changed()
            self._show_lamp_result(wf, res)               # now incl. verification
            yield from self._write_protocol(wf)           # protocol AFTER verification
            if activate:
                if not self.protocol_paths:
                    self._state(8, "warning", f"{pid} saved and active; protocol "
                                              "not written")
                self.last_status = "saved"
                self.lbl_status.setText(f"Saved and activated {pid}.")
            else:
                self.last_status = "saved_implausible"
                self.lbl_status.setText(f"Saved {pid} marked as not plausible; "
                                        f"not activated.")
        except Exception as e:                                # noqa: BLE001
            self._fail_msg(step, e)

    def _final_check_states(self, wf, ev):
        """Step 4/5/6 states from the FINAL checks (after the darks AFTER);
        the values shown earlier were provisional."""
        c = wf.checks
        if c.get("stability_pct") is not None:
            self._state(4, "ok" if c.get("stability_pass") else "warning",
                        f"stability {c['stability_pct']:.2f} %"
                        + (f", {c.get('stability_fail_pixels')} px masked"
                           if c.get("stability_fail_pixels") else ""))
        if c.get("itime_crosscheck_pct") is not None:
            self._state(5, "ok" if c.get("itime_pass") else "warning",
                        f"{c['itime_crosscheck_pct']:.2f} %"
                        + (f", {c.get('itime_fail_pixels')} px masked"
                           if c.get("itime_fail_pixels") else ""))
        drift = list(wf.dark_drift or [])
        warn = any(d.get("warn") for d in drift)
        txt = ", ".join(f"{d['integration_us']/1000:.1f} ms: {d['mean_counts']:+.1f} cts"
                        for d in drift) or "no drift data"
        self._state(6, "warning" if warn else "ok", f"dark drift {txt}")

    def _result_text(self, res, plausibility, extra=()):
        t = _T()
        lines = [f"Valid segments: {calprof.format_segments(res.get('segments_nm'), 4)}"
                 f" · gain span {res['gain_span']:.1f}×"]
        if plausibility is not None:
            m, lim = plausibility["metrics"], plausibility["limits"]
            verdict = (_span(t.OK, "passed") if plausibility["pass"]
                       else _span(t.DANGER, "<b>FAILED</b>"))
            lines.append(
                f"Plausibility: {verdict} — coverage {m['coverage']*100:.0f} % "
                f"(≥ {lim['min_coverage']*100:.0f} %), "
                f"roughness {m['roughness']*100:.1f} % (≤ {lim['max_roughness']*100:.1f} %), "
                f"isolated {m['isolated_frac']*100:.0f} % "
                f"(≤ {lim['max_isolated_frac']*100:.0f} %)")
            for f in plausibility["failures"]:
                lines.append(_span(t.DANGER, f"• {f}"))
            for h in plausibility.get("hints") or []:
                lines.append(_span(t.WARN, f"Hint: {h}"))
        lines.extend(extra)
        return "<br>".join(lines)

    def _check_lines(self, wf):
        """Stability / cross check / dark drift lines (percentage, verdict,
        masked pixels, cause with magnitude)."""
        t = _T()
        c = wf.checks

        def chk(name, pct, ok, nfail, cause):
            if pct is None:
                return f"{name}: skipped"
            s = (f"{name}: {pct:.2f} % — "
                 + _span(t.OK if ok else t.DANGER, "passed" if ok else "NOT passed")
                 + f" · {int(nfail or 0)} px masked")
            if cause:
                s += f"<br>&nbsp;&nbsp;Cause: {cause}"
            return s
        out = [chk("Stability", c.get("stability_pct"), c.get("stability_pass"),
                   c.get("stability_fail_pixels"), c.get("stability_cause")),
               chk("Integration cross check (t/2)", c.get("itime_crosscheck_pct"),
                   c.get("itime_pass"), c.get("itime_fail_pixels"),
                   c.get("itime_cause"))]
        for d in wf.dark_drift or []:
            s = (f"Dark drift at {d['integration_us']/1000:.2f} ms: "
                 f"{d['mean_counts']:+.1f} counts ({d.get('pct_of_peak', 0.0):+.2f} % "
                 f"of the peak)")
            out.append(_span(t.WARN, s + " — warning") if d.get("warn") else s)
        return out

    def _show_lamp_result(self, wf, res):
        wl, valid = res["wl"], res["valid"]
        segs = calprof.valid_segments(wl, valid, 0.0)     # shade every masked gap
        self.plot_in.clear()
        self.plot_in.plot(wl, _norm(res["C_cal"], valid), name="C_cal (norm)",
                          pen=pg.mkPen((120, 180, 255), width=1))
        self.plot_in.plot(wl, _norm(res["E"], valid), name="E_ref (norm)",
                          pen=pg.mkPen((120, 200, 120), width=1))
        _shade_masked(self.plot_in, wl, segs)
        self.plot_in.autoRange()
        self.plot_out.clear()
        absolute = wf.kind == calprof.TYPE_ABSOLUTE
        y = res["K"] if absolute else res["S"]
        self.plot_out.setLabel("left", "K" if absolute else "S")
        self.plot_out.plot(wl, y, name="K" if absolute else "S", connect="finite",
                           pen=pg.mkPen((255, 170, 60), width=2))
        _shade_masked(self.plot_out, wl, segs)
        self.plot_out.autoRange()
        t = _T()
        extra = self._check_lines(wf)
        ver = getattr(wf, "verification", None)
        if ver:
            vl = cw.format_verification(ver)
            ok = bool(ver.get("pass"))
            extra.append(_span(t.OK if ok else t.WARN, f"<b>{vl[0]}</b>"))
            extra.extend("<code>" + ln.replace(" ", "&nbsp;") + "</code>" for ln in vl[1:])
        other = [w for w in wf.warnings if not w.startswith("Plausibility check failed")]
        if other:
            extra.append(_span(t.WARN, "<br>".join(other)))
        self.lbl_result.setText(self._result_text(res, wf.plausibility, extra))

    def _write_protocol(self, wf, derivation: bool = False):
        """Generator fragment: the record is built here (GUI thread), the PDF /
        CSV are written in the worker thread (matplotlib is slow)."""
        try:
            import calibration_report as crp
        except ImportError as e:
            self.protocol_message = f"Protocol not written (calibration_report: {e})."
            QMessageBox.warning(self, "Protocol", self.protocol_message)
            self._set_protocol_display()
            return
        try:
            import export_paths
            rec = wf.record()
            d, note = export_paths.calibration_protocol_dir()
            base = (crp.default_derivation_basename(rec) if derivation
                    else crp.default_protocol_basename(rec))
            write = crp.write_derivation_protocol if derivation else crp.write_protocol
            pdf, csv_ = os.path.join(d, base + ".pdf"), os.path.join(d, base + ".csv")
            self.lbl_status.setText("Writing the protocol…")
            paths = yield (lambda: write(rec, pdf, csv_))
            self.protocol_paths = list(paths or [])
            self.protocol_message = note or ""
        except Exception as e:                                # noqa: BLE001
            self.protocol_paths = []
            self.protocol_message = f"Protocol not written: {e}"
            QMessageBox.warning(self, "Protocol", self.protocol_message)
        self._set_protocol_display()

    # derivation ----------------------------------------------------------
    def _deriv_prompt(self, title: str, text: str) -> None:
        if not _ask(self, title, text, ok_cancel=True):
            raise _Abort(f"cancelled at the prompt '{title}'")

    def _deriv_cover(self, dw, what: str) -> None:
        """Dark prompt (devices without a shutter only)."""
        if dw.needs_user_cover:
            self._deriv_prompt("Dark spectrum",
                               "Block the light path / cover the source.\n\n"
                               f"The dark spectrum ({what}) is captured next.")

    def _deriv_uncover(self, dw, what: str) -> None:
        if dw.needs_user_cover:
            self._deriv_prompt("Release the light path",
                               "Release the light path / uncover the source.\n\n"
                               f"The {what} is captured next.")

    @staticmethod
    def _side_label(which: str) -> str:
        return "known optics" if which == cw.SIDE_KNOWN else "new optics"

    def _deriv_itime_text(self, dw) -> str:
        """One line: integration time and level per side."""
        parts = []
        for s, nm in ((cw.SIDE_KNOWN, "Known"), (cw.SIDE_NEW, "New")):
            t = dw.itime.get(s)
            if t is None:
                continue
            ex = dw.exposure.get(s) or {}
            light = dw.lights.get(s)
            txt = f"{nm}: {t / 1000:.3f} ms"
            if ex.get("auto") is not None:
                txt += " (auto)" if ex.get("auto") else " (fixed)"
            if light is not None:
                txt += f", {100 * light.peak_frac:.0f} % FS"
            parts.append(txt)
        return " · ".join(parts)

    def _deriv_peak_state(self, dw, which: str) -> tuple[str, str]:
        light = dw.lights.get(which)
        if light is None:
            return "ok", ""
        high = light.peak_frac >= dw.cfg.saturation_warn_frac
        return ("warning" if high else "ok"), f"{100 * light.peak_frac:.0f} % FS"

    def _gen_deriv(self, dw, us_known, us_new=None):
        I = _DERIV_IX                                          # noqa: N806
        step = 0

        def begin(key):
            nonlocal step
            step = I[key]
            self._checkpoint()
            self._state(step, "running")

        def itime_step(which, us, key):
            begin(key)
            self.lbl_status.setText(f"Setting the integration time ({self._side_label(which)})…")
            info = yield (lambda: dw.set_integration(which, us))
            self.lbl_itime.setText(self._deriv_itime_text(dw))
            t = info["integration_us"]
            txt = f"{t / 1000:.3f} ms, " + ("auto exposure" if info["auto"] else "fixed")
            if info.get("peak_frac") is not None:
                txt += f", {100 * info['peak_frac']:.0f} % FS"
            if info["auto"] and not info.get("converged", True):
                txt += ", target band not reached"
            self._state(step, "ok" if info.get("converged", True) else "warning", txt)

        def dark_step(which, key, phase="before"):
            begin(key)
            what = f"{self._side_label(which)}" + (", after" if phase == "after" else "")
            self._deriv_cover(dw, what)
            self.lbl_status.setText(f"Capturing the dark spectrum ({what})…")
            d = yield (lambda: dw.capture_dark_for(which, phase))
            self._state(step, "ok", f"{d.integration_us / 1000:.3f} ms, "
                                    f"peak {d.peak_counts:.0f} counts")

        def light_step(which, key, uncover_what):
            begin(key)
            self._deriv_uncover(dw, uncover_what)
            side = cw.SIDE_KNOWN if which == cw.SIDE_REPEAT else which
            self.lbl_status.setText(f"Capturing the light spectrum ({self._side_label(side)}"
                                    + (", repeat)…" if which == cw.SIDE_REPEAT else ")…"))
            yield (lambda: dw.capture_light_for(which))
            self.lbl_itime.setText(self._deriv_itime_text(dw))
            st, txt = self._deriv_peak_state(dw, which)
            self._state(step, st, txt)

        try:
            self._state(0, "ok", f"{(dw.known.get('optics') or {}).get('name')} → "
                                 f"{dw.optics['name']} ({dw.optics['input']})")
            # known optics
            begin("mount_known")
            self._deriv_prompt(
                "Known optics",
                f"{cw.DERIVATION_SOURCE_HINT}\n\n"
                "Mount the KNOWN optics on the source and keep the light path "
                "open. The integration time of this optics is set next.")
            self._state(step, "ok")
            yield from itime_step(cw.SIDE_KNOWN, us_known, "integration_known")
            yield from dark_step(cw.SIDE_KNOWN, "dark_known")
            yield from light_step(cw.SIDE_KNOWN, "light_known", "light spectrum")
            # new optics
            begin("swap_optics")
            self._deriv_prompt(
                "New optics",
                "Mount the NEW optics on the same source (same position, same "
                "power) and keep the light path open. The integration time of "
                "this optics is set next.")
            self._state(step, "ok")
            yield from itime_step(cw.SIDE_NEW, us_new, "integration_new")
            yield from dark_step(cw.SIDE_NEW, "dark_new")
            yield from light_step(cw.SIDE_NEW, "light_new", "light spectrum")
            yield from dark_step(cw.SIDE_NEW, "dark_new_after", "after")
            # known optics again (source drift)
            begin("swap_back")
            self._deriv_prompt(
                "Known optics again",
                "Mount the KNOWN optics again on the same source (same position, "
                "same power) and keep the light path open. It is measured once "
                "more to detect source drift.")
            self._state(step, "ok")
            yield from light_step(cw.SIDE_REPEAT, "repeat_known", "repeat spectrum")
            yield from dark_step(cw.SIDE_KNOWN, "dark_known_after", "after")
            # compute
            begin("compute")
            self.lbl_status.setText("Computing S_new…")
            try:
                res = yield dw.compute
            except ValueError as e:                           # no valid pixel at all
                self._state(step, "failed", str(e))
                self._state(I["save"], "failed", "not saved")
                self.last_status = "failed"
                self.lbl_result.setText(_span(_T().DANGER, f"<b>FAILED</b>: {e}"))
                yield from self._write_protocol(dw, derivation=True)
                self.lbl_status.setText("Derivation failed: no valid pixels, "
                                        "no profile saved.")
                return
            self._show_deriv_result(dw, res)
            self._deriv_check_states(dw)
            ds = dw.drift_status() if dw.drift_failed else None
            if ds:
                # drift is the primary status, shown before "not plausible"
                self._state(step, "failed", ds["message"])
            elif dw.plausible:
                self._state(step, "ok",
                            f"valid {calprof.format_segments(res.get('segments_nm'), 3)}")
            else:
                self._state(step, "failed", "plausibility check failed: "
                            + "; ".join(dw.plausibility["failures"]))
            # save
            step = I["save"]
            self._checkpoint()
            activate = True
            if ds and not _ask(self, "Drift check failed",
                               ds["message"] + "\n\nThe source drifted between the "
                               "measurements; the result is not reliable.\n"
                               "Save the profile anyway?"):
                self._state(step, "failed", "not saved (" + ds["message"] + ")")
                self.last_status = "drift_check_failed"
                yield from self._write_protocol(dw, derivation=True)
                self.lbl_status.setText(ds["message"] + " — no profile saved.")
                return
            if not dw.plausible:
                other = [c for c in self._deriv_failed_checks(dw)
                         if not (ds and c == "Source drift")]
                if not self._confirm_implausible(dw.plausibility["failures"], other):
                    self._state(step, "failed", "not saved (plausibility check failed)")
                    self.last_status = "failed"
                    yield from self._write_protocol(dw, derivation=True)
                    self.lbl_status.setText("Derivation failed: result not plausible, "
                                            "no profile saved.")
                    return
                activate = False
            elif not dw.checks_passed:
                if not _ask(self, "Checks failed",
                            "The source drift check and/or a consistency check "
                            "failed:\n" + "\n".join(f"• {c}" for c in
                                                    self._deriv_failed_checks(dw))
                            + "\n\nSave the profile anyway?"):
                    self._state(step, "warning", "not saved (checks failed)")
                    self.last_status = "not_saved"
                    yield from self._write_protocol(dw, derivation=True)
                    self.lbl_status.setText("Not saved.")
                    return
            self._state(step, "running")
            p = yield (lambda: dw.save(allow_implausible=not activate))
            if activate:
                calprof.set_active_selection(dw.backend_name, p["id"])
            self.dlg.notify_profiles_changed()
            pid = f"profile {p['id']} v{p['version']}"
            self._show_deriv_result(dw, res)
            yield from self._write_protocol(dw, derivation=True)
            if activate:
                self._state(step, "ok" if self.protocol_paths else "warning",
                            f"{pid} saved and active"
                            + ("" if self.protocol_paths else "; protocol not written"))
                self.last_status = "saved"
                self.lbl_status.setText(f"Saved and activated {pid}.")
            else:
                self._state(step, "warning", f"{pid} saved as NOT PLAUSIBLE, not activated")
                self.last_status = "saved_implausible"
                self.lbl_status.setText(f"Saved {pid} marked as not plausible; "
                                        f"not activated.")
            self._state(I["protocol"], "ok" if self.protocol_paths else "warning",
                        "written" if self.protocol_paths else "not written")
        except Exception as e:                                # noqa: BLE001
            self._fail_msg(step, e)

    @staticmethod
    def _deriv_failed_checks(dw) -> list[str]:
        c = dw.checks
        out = []
        if c.get("drift_pass") is False:
            out.append("Source drift")
        if c.get("itime_match_known") is False or c.get("itime_match_new") is False:
            out.append("Dark and light at the same integration time")
        if c.get("linearity_consistent") is False:
            out.append("Linearity correction unchanged")
        return out

    def _deriv_check_states(self, dw) -> None:
        """States of the repeat / dark-after steps from the FINAL checks."""
        c = dw.checks
        I = _DERIV_IX                                          # noqa: N806
        if c.get("drift_pct") is not None:
            self._state(I["repeat_known"], "ok" if c.get("drift_pass") else "warning",
                        f"drift {c['drift_pct']:.2f} % (limit "
                        f"{c['drift_threshold_pct']:.2f} %)"
                        + (f", {c['drift_fail_pixels']} px masked"
                           if c.get("drift_fail_pixels") else ""))
        else:
            self._state(I["repeat_known"], "warning", "drift not evaluated")
        for d in dw.dark_drift or []:
            key = "dark_known_after" if d["side"] == cw.SIDE_KNOWN else "dark_new_after"
            self._state(I[key], "warning" if d.get("warn") else "ok",
                        f"dark drift {d['mean_counts']:+.1f} counts")

    def _deriv_lines(self, dw) -> list[str]:
        """Per optics: integration time, level, linearity correction; source
        drift; dark drift (no empty or 'nan' values)."""
        t = _T()
        rec = dw.record()
        out = []
        for s, nm in ((cw.SIDE_KNOWN, "Known optics"), (cw.SIDE_NEW, "New optics")):
            sd = rec["sides"][s]
            if sd["integration_us"] is None:
                continue
            line = f"{nm}: t_int {sd['integration_us'] / 1000:.3f} ms"
            if sd.get("auto_exposure") is not None:
                line += " (auto)" if sd["auto_exposure"] else " (fixed)"
            if sd.get("peak_frac") is not None:
                line += f" · level {100 * sd['peak_frac']:.1f} % FS"
            if sd.get("peak_frac_repeat") is not None:
                line += f" (repeat {100 * sd['peak_frac_repeat']:.1f} %)"
            line += f" · linearity correction: {sd.get('linearity_text') or 'n/a'}"
            if sd.get("itime_match") is False:
                line += " · " + _span(t.DANGER, "dark/light integration time differs")
            out.append(line)
        dr = rec.get("drift")
        if dr:
            ok = bool(dr.get("pass"))
            mean = dr.get("mean_pct")
            out.append(
                f"Source drift: p95 {dr['p95_pct']:.2f} %"
                + (f", mean {mean:+.2f} %" if mean is not None else "")
                + f", limit {dr['threshold_pct']:.2f} % — "
                + _span(t.OK if ok else t.DANGER, "passed" if ok else "NOT passed")
                + f" · {int(dr.get('n_fail_pixels') or 0)} px masked"
                + (f"<br>&nbsp;&nbsp;Cause: {dr['cause']}" if dr.get("cause") else ""))
        else:
            out.append("Source drift: not checked")
        for d in rec.get("dark_drift") or []:
            s = (f"Dark drift ({'known' if d['side'] == cw.SIDE_KNOWN else 'new'} optics, "
                 f"{d['integration_us'] / 1000:.2f} ms): {d['mean_counts']:+.1f} counts "
                 f"({d.get('pct_of_peak', 0.0):+.2f} % of the peak)")
            out.append(_span(t.WARN, s + " — warning") if d.get("warn") else s)
        return out

    def _show_deriv_result(self, dw, res):
        wl, valid = res["wl"], res["valid"]
        segs = calprof.valid_segments(wl, valid, 0.0)     # shade every masked gap
        self.plot_in.clear()
        self.plot_in.plot(wl, _norm(res["C_known"], valid), name="C_known (norm)",
                          pen=pg.mkPen((120, 200, 120), width=1))
        if dw.rate_repeat is not None:
            self.plot_in.plot(wl, _norm(dw.rate_repeat.rate, valid),
                              name="C_known repeat (norm)",
                              pen=pg.mkPen((200, 200, 120), width=1,
                                           style=Qt.PenStyle.DashLine))
        self.plot_in.plot(wl, _norm(res["C_new"], valid), name="C_new (norm)",
                          pen=pg.mkPen((120, 180, 255), width=1))
        _shade_masked(self.plot_in, wl, segs)
        self.plot_in.autoRange()
        self.plot_out.clear()
        self.plot_out.setLabel("left", "S")
        self.plot_out.plot(wl, res["S"], name="S (new optics)", connect="finite",
                           pen=pg.mkPen((255, 170, 60), width=2))
        _shade_masked(self.plot_out, wl, segs)
        self.plot_out.autoRange()
        t = _T()
        extra = self._deriv_lines(dw)
        ds = dw.drift_status() if dw.drift_failed else None
        if ds:
            extra.insert(0, _span(t.DANGER, f"<b>{ds['message']}</b>"))
        other = [w for w in dw.warnings if not w.startswith("Plausibility check failed")]
        if other:
            extra.append(_span(t.WARN, "<br>".join(other)))
        self.lbl_result.setText(self._result_text(res, dw.plausibility, extra))


# ══════════════════════════════════════════════════════════════════════════
#  PROFILES TAB
# ══════════════════════════════════════════════════════════════════════════

class _ProfilesTab(QWidget):
    COLS = ["Profile", "Status", "Valid range", "Unit", "Created"]

    def __init__(self, dlg):
        super().__init__()
        self.setObjectName("calTabPage")
        self.dlg = dlg
        v = QVBoxLayout(self)
        top = QHBoxLayout()
        self.lbl_dev = ElidedLabel("")
        top.addWidget(self.lbl_dev, 1)
        self.chk_all = QCheckBox("All versions")
        self.chk_all.toggled.connect(lambda _=None: self.refresh())
        top.addWidget(self.chk_all)
        v.addLayout(top)
        self.table = QTableWidget(0, len(self.COLS))
        self.table.setHorizontalHeaderLabels(self.COLS)
        gtab.configure_readonly_table(self.table, stretch_col=0,
                                      sort_col=self.COLS.index("Created"),
                                      sort_order=Qt.SortOrder.DescendingOrder,
                                      min_stretch_px=220)
        self.table.setMinimumHeight(140)
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
        self.lbl_status = ElidedLabel("", mode=Qt.TextElideMode.ElideMiddle)
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
        rows = []
        for p in profs:
            st = calprof.profile_status(p, name, serial, fp, nl)
            label = calprof.profile_label(p) + ("  [active]" if p.get("id") == active else "")
            segs = p.get("valid_segments_nm")
            vr = p.get("valid_range_nm") or (None, None)
            rng = calprof.format_segments(segs, 2) if segs else _fmt_range(*vr)
            created = str(p.get("created", ""))
            state = st["state"] + (" · not plausible" if calprof.is_implausible(p) else "")
            lr = (calprof.legacy_valid_range(p)
                  if p.get("apply") == calprof.APPLY_LEGACY_GAIN else None)
            if lr:
                state += f" · legacy, masked outside {lr[0]:.0f}–{lr[1]:.0f} nm"
            cells = [label, state, rng, str(p.get("unit", "")),
                     created[:16].replace("T", " ")]
            tip = "\n".join(x for x in (label, st["message"],
                                          f"Valid: {calprof.format_segments(segs, 20)}"
                                          if segs else "") if x)
            rows.append({"cells": cells,
                         "sort": [label.lower(), state, (vr[0] or 0.0), cells[3], created],
                         "tooltip": tip,
                         "data": {0: {Qt.ItemDataRole.UserRole: p["id"]}}})
        gtab.fill_table(self.table, rows)

    def selected_id(self):
        return gtab.selected_row_data(self.table)

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
        if calprof.is_implausible(p):
            fails = (p.get("plausibility") or {}).get("failures") or []
            if not _ask(self, "Profile not plausible",
                        "This profile failed the plausibility check:\n"
                        + "\n".join(f"• {f}" for f in fails)
                        + "\n\nActivate it anyway?"):
                return
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
        # Library order (built-in sources first); no click-sorting so the
        # built-ins stay on top.
        gtab.configure_readonly_table(self.table, stretch_col=0, sort_col=None,
                                      min_stretch_px=200)
        self.table.setMinimumHeight(110)
        self.table.setMaximumHeight(200)
        self.table.itemSelectionChanged.connect(self._plot_selected)
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
        self.lbl_status = ElidedLabel("", mode=Qt.TextElideMode.ElideMiddle)
        v.addWidget(self.lbl_status)
        self.refresh()

    def refresh(self):
        try:
            srcs = rs.ReferenceLibrary().list()
        except Exception as e:                                # noqa: BLE001
            srcs = []
            self.lbl_status.setText(f"Library unreadable: {e}")
        rows = []
        for s in srcs:
            cells = [s.name, s.serial, s.unit, _fmt_range(s.valid_min_nm, s.valid_max_nm),
                     s.input_optic or "—", "yes" if s.builtin else ""]
            rows.append({"cells": cells,
                         "data": {0: {Qt.ItemDataRole.UserRole: s.id,
                                      Qt.ItemDataRole.UserRole + 1: bool(s.builtin)}}})
        gtab.fill_table(self.table, rows)

    def selected(self):
        sid = gtab.selected_row_data(self.table)
        if sid is None:
            return None, False
        return sid, bool(gtab.selected_row_data(self.table, Qt.ItemDataRole.UserRole + 1))

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

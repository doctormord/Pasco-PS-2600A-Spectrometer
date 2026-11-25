"""
gui_linearity.py — "Linearity" tab of the calibration dialog (desktop).

Pure Qt glue: the measurement, fit and store live in calibration_linearity.py,
activation / protocol in engine.py. The run uses the same generator + worker
thread pattern as the spectral wizard (gui_calibration._GenRunner).

Per connected unit (backend + serial) the tab shows the state of the linearity
correction (none / active / off / factory), the stored records, and runs a new
linearity calibration. On a device with a factory linearisation (e.g. Ocean
HDX) only a CHECK measurement is made; no correction is applied.
"""

from __future__ import annotations

import os
import time

import numpy as np
import pyqtgraph as pg

from PyQt6.QtCore import Qt, QUrl
from PyQt6.QtWidgets import (
    QAbstractItemView, QHBoxLayout, QLabel, QMessageBox, QPushButton, QSplitter,
    QTableWidget, QVBoxLayout, QWidget,
)
from PyQt6.QtGui import QDesktopServices

import calibration_linearity as cl
import gui_tables as gtab
import gui_theme as gt
from gui_layout import ElidedLabel, FixedTextPane, scroll_page


def _T():
    return gt.Theme


def _span(token: str, html: str) -> str:
    return f"<span style='color:{token}'>{html}</span>"


def _fmt_range(r) -> str:
    try:
        return f"{float(r[0]):.0f}–{float(r[1]):.0f} counts"
    except (TypeError, ValueError, IndexError):
        return "—"


_TABLE_COLS = ["ID", "Created", "Mode", "Max dev. [%]", "RMS before → after [%]",
               "Valid range", "Active"]
_STEP_COLS = ["Role", "t [ms]", "Peak [% FS]", "Dev. raw [%]", "Dev. corr. [%]"]

INFO_TEXT = ("Use a stable broadband source (e.g. a halogen / tungsten lamp). The run "
             "measures 6–10 integration times with a raw peak of about 10–90 % of "
             "full scale. Do not touch the fibre or the lamp during the run.")
FACTORY_TEXT = ("This device linearises its readings itself (factory linearisation). "
                "Only a CHECK measurement is made; no correction is applied.")


class LinearityTab(QWidget):
    SELF_SCROLLING = True

    def __init__(self, dlg):
        super().__init__()
        self.setObjectName("calTabPage")
        self.dlg = dlg
        self.running = False
        self._cancel = False
        self._note = ""
        self._runner = None
        self._wf = None
        self._resumed = False
        self.last_error: str | None = None
        self.last_status: str = ""
        self.protocol_paths: list[str] = []
        self.protocol_message: str = ""
        self._build()
        self.refresh()

    # ── UI ───────────────────────────────────────────────────────────────
    def _build(self):
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)
        body = QWidget()
        body.setObjectName("calTabPage")
        v = QVBoxLayout(body)
        v.setContentsMargins(8, 8, 8, 4)

        self.split = QSplitter(Qt.Orientation.Horizontal)
        self.split.setChildrenCollapsible(False)

        # left: device, state, info, records, buttons
        left = QWidget()
        lv = QVBoxLayout(left)
        lv.setContentsMargins(0, 0, 6, 0)
        lv.setSpacing(6)
        self.lbl_device = ElidedLabel("")
        lv.addWidget(self.lbl_device)
        self.lbl_state = FixedTextPane(3)
        lv.addWidget(self.lbl_state)
        self.lbl_info = FixedTextPane(4)
        lv.addWidget(self.lbl_info)
        self.table = QTableWidget(0, len(_TABLE_COLS))
        self.table.setHorizontalHeaderLabels(_TABLE_COLS)
        self.table.setMinimumHeight(110)
        gtab.configure_readonly_table(self.table, stretch_col=0, sort_col=1,
                                      min_stretch_px=90)
        self.table.itemSelectionChanged.connect(self._sync_buttons)
        lv.addWidget(self.table, 1)
        row = QHBoxLayout()
        row.setSpacing(6)
        self.btn_activate = QPushButton("Activate")
        self.btn_activate.setToolTip("Apply the selected stored linearity correction.")
        self.btn_activate.clicked.connect(self.activate)
        self.btn_off = QPushButton("Switch off")
        self.btn_off.setToolTip("Stop applying the linearity correction "
                                "(the records stay stored).")
        self.btn_off.clicked.connect(self.switch_off)
        row.addWidget(self.btn_activate)
        row.addWidget(self.btn_off)
        row.addStretch(1)
        lv.addLayout(row)
        left.setMinimumWidth(320)
        self.split.addWidget(left)

        # right: deviation curve, step table, result
        right = QWidget()
        rv = QVBoxLayout(right)
        rv.setContentsMargins(6, 0, 0, 0)
        rv.setSpacing(6)
        vs = QSplitter(Qt.Orientation.Vertical)
        vs.setChildrenCollapsible(False)
        self.plot = pg.PlotWidget()
        self.plot.setLabel("bottom", "Signal", units="counts")
        self.plot.setLabel("left", "Deviation from linearity", units="%")
        self.plot.addLegend()
        self.plot.setMinimumHeight(110)
        self._style_plot()
        vs.addWidget(self.plot)
        self.tbl_steps = QTableWidget(0, len(_STEP_COLS))
        self.tbl_steps.setHorizontalHeaderLabels(_STEP_COLS)
        self.tbl_steps.setMinimumHeight(90)
        gtab.configure_readonly_table(self.tbl_steps, stretch_col=0, sort_col=None,
                                      min_stretch_px=60)
        vs.addWidget(self.tbl_steps)
        rv.addWidget(vs, 1)
        self.lbl_result = FixedTextPane(6)
        rv.addWidget(self.lbl_result)
        self.split.addWidget(right)
        self.split.setStretchFactor(0, 0)
        self.split.setStretchFactor(1, 1)
        self.split.setSizes([420, 760])
        v.addWidget(self.split, 1)
        outer.addWidget(scroll_page(body), 1)

        # footer (always visible)
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
        self.btn_run = QPushButton("Run linearity calibration")
        self.btn_run.setObjectName("primaryBtn")
        self.btn_run.clicked.connect(self.start)
        fh.addWidget(self.btn_cancel)
        fh.addWidget(self.btn_run)
        outer.addWidget(foot)

    def _style_plot(self):
        self.plot.setBackground(_T().BG0)
        self.plot.getPlotItem().setContentsMargins(6, 10, 14, 6)
        for axis in ("bottom", "left"):
            ax = self.plot.getAxis(axis)
            ax.setPen(pg.mkPen(_T().BORDER2, width=1))
            ax.setTextPen(pg.mkPen(_T().FG3))
            ax.enableAutoSIPrefix(False)
        self.plot.showGrid(x=True, y=True, alpha=0.12)

    # ── device / engine ──────────────────────────────────────────────────
    def _engine(self):
        return self.dlg.engine()

    def _device(self):
        import gui_calibration as gc
        be = self.dlg._backend()
        eng = self._engine()
        name = getattr(eng, "device_name", None) or self.dlg._device_name()
        return name, be, gc._backend_serial(be)

    def _linearity_info(self) -> dict:
        eng = self._engine()
        return dict(getattr(eng, "linearity_info", None) or {"state": cl.STATE_NONE})

    # ── refresh ──────────────────────────────────────────────────────────
    def refresh(self):
        if self.running:
            return
        name, be, serial = self._device()
        self.lbl_device.setText(
            f"{name} · S/N {serial or '—'}"
            + ("" if be is not None else " · no device connected")
            + (" · no serial number: records are not bound to this unit"
               if be is not None and not serial else ""))
        info = self._linearity_info()
        st = info.get("state", cl.STATE_NONE)
        t = _T()
        if be is None:
            txt = "No device connected."
        elif st == cl.STATE_ACTIVE:
            txt = (_span(t.OK, "<b>Linearity correction: active</b>")
                   + f"<br>Record {info.get('id')} · created "
                     f"{str(info.get('created') or '')[:19]}<br>Valid range "
                     f"{_fmt_range(info.get('valid_range_counts'))}")
        elif st == cl.STATE_FACTORY:
            txt = (_span(t.ACCENT, "<b>Factory linearisation (device)</b>")
                   + "<br>The device linearises itself; no software correction "
                     "is applied.")
        elif st == cl.STATE_OFF:
            txt = (_span(t.WARN, "<b>Linearity correction: off</b>")
                   + "<br>Stored records exist; select one and press Activate.")
        else:
            txt = "<b>Linearity correction: none</b><br>No record stored for this unit."
        self.lbl_state.setText(txt)
        self.lbl_info.setText(FACTORY_TEXT if st == cl.STATE_FACTORY else INFO_TEXT)
        self.btn_run.setText("Run check measurement" if st == cl.STATE_FACTORY
                             else "Run linearity calibration")
        rows = []
        active = info.get("id") if st == cl.STATE_ACTIVE else None
        recs = cl.entries_for(name, serial) if be is not None else []
        for e in recs:
            s = cl.entry_summary(e) or {}
            created = str(s.get("created") or "")
            rb, ra = s.get("rms_before_pct"), s.get("rms_after_pct")
            md = s.get("max_deviation_pct")
            is_active = s.get("id") == active
            rows.append({
                "cells": [s.get("id") or "", created[:19], s.get("mode") or "",
                          "—" if md is None else f"{md:.2f}",
                          "—" if rb is None or ra is None else f"{rb:.3f} → {ra:.3f}",
                          _fmt_range(s.get("valid_range_counts")),
                          "yes" if is_active else ""],
                "sort": [None, created, None, md, None, None, int(is_active)],
                "data": {0: {Qt.ItemDataRole.UserRole: s.get("id"),
                             Qt.ItemDataRole.UserRole + 1: s.get("mode")}},
            })
        gtab.fill_table(self.table, rows)
        self._sync_buttons()

    def _selected(self):
        return (gtab.selected_row_data(self.table),
                gtab.selected_row_data(self.table, Qt.ItemDataRole.UserRole + 1))

    def _sync_buttons(self):
        busy = self.running
        have_engine = self._engine() is not None and self.dlg._backend() is not None
        eid, mode = self._selected()
        st = self._linearity_info().get("state")
        self.btn_activate.setEnabled(bool(
            not busy and have_engine and eid and mode == cl.MODE_CORRECTION
            and st != cl.STATE_FACTORY))
        self.btn_off.setEnabled(bool(not busy and have_engine and st == cl.STATE_ACTIVE))
        self.btn_run.setEnabled(bool(not busy and have_engine))
        self.btn_cancel.setEnabled(busy and not self._cancel)

    # ── activate / switch off ────────────────────────────────────────────
    def _select(self, eid):
        eng = self._engine()
        if eng is None:
            return
        try:
            eng.select_linearity(eid, owner=_owner())
        except Exception as e:                                # noqa: BLE001
            QMessageBox.warning(self, "Linearity", str(e))
            return
        self.dlg.notify_profiles_changed()
        self.refresh()
        self.lbl_status.setText("Linearity correction activated." if eid
                                else "Linearity correction switched off.")

    def activate(self):
        eid, _mode = self._selected()
        if eid:
            self._select(eid)

    def switch_off(self):
        self._select(None)

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

    # ── run control ──────────────────────────────────────────────────────
    def _cancel_clicked(self):
        self._cancel = True
        self.btn_cancel.setEnabled(False)
        self.lbl_status.setText("Cancelling after the running step…")

    def _checkpoint(self):
        if self._cancel:
            import gui_calibration as gc
            raise gc._Abort("cancelled by user")

    def _tick(self):
        if self._note:
            self.lbl_status.setText(self._note)

    def _progress(self, frac, msg):
        """Called from the worker thread: only a plain string is set."""
        self._note = f"{msg} ({100 * float(frac):.0f} %)"

    def _worker_sleep(self, seconds):
        end = time.monotonic() + max(0.0, float(seconds))
        while True:
            rem = end - time.monotonic()
            if rem <= 0:
                return
            time.sleep(min(0.1, rem))

    def _begin(self, gen):
        import gui_calibration as gc
        self.running = True
        self._cancel = False
        self.last_error = None
        self.last_status = ""
        self.protocol_paths = []
        self.protocol_message = ""
        self._set_protocol_display()
        self.dlg.set_busy(True, keep=self)
        self.btn_activate.setEnabled(False)
        self.btn_off.setEnabled(False)
        self.btn_run.setEnabled(False)
        self.btn_cancel.setEnabled(True)
        self.dlg._resume_for_capture()
        self._resumed = True
        self._runner = gc._GenRunner(gen, self._end, self._tick)
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
        self.dlg.set_busy(False, keep=self)
        self._set_protocol_display()
        self.refresh()

    def start(self):
        if self.running:
            return
        import calibration_linearity as cl_
        name, be, _serial = self._device()
        if be is None or self._engine() is None:
            QMessageBox.warning(self, "Not connected",
                                "Connect and start a device first.")
            return
        self.lbl_result.setText("")
        self.tbl_steps.setRowCount(0)
        self.plot.clear()
        self._wf = cl_.LinearityWorkflow(be, name)
        if self._wf.mode == cl_.MODE_CHECK:
            self.lbl_info.setText(FACTORY_TEXT)
        self._begin(self._gen(self._wf))

    # ── generator ────────────────────────────────────────────────────────
    def _ask(self, title, text) -> bool:
        import gui_calibration as gc
        return gc._ask(self, title, text, ok_cancel=True)

    def _gen(self, wf):
        import gui_calibration as gc
        eng = self._engine()
        check_only = wf.mode == cl.MODE_CHECK
        try:
            self.lbl_status.setText("Probing the source…")
            self._checkpoint()
            plan = yield (lambda: wf.plan(None))
            self.lbl_result.setText(
                f"Planned {len(plan['times_us'])} steps: "
                + ", ".join(f"{t/1000:.2f}" for t in plan["times_us"]) + " ms; "
                f"reference {plan['ref_us']/1000:.2f} ms."
                + ("<br>Check measurement only: no correction is applied."
                   if check_only else ""))
            cover = wf.needs_user_cover
            self._checkpoint()
            if cover and not self._ask(
                    "Dark spectra",
                    "Block the light path / cover the input.\n\n"
                    "Dark spectra at all integration times are captured next."):
                raise gc._Abort("cancelled at the dark prompt")
            self.lbl_status.setText("Capturing dark spectra (before)…")
            yield (lambda: wf.capture_darks("before", progress=self._progress))
            if cover and not self._ask(
                    "Release the light path",
                    "Release the light path / uncover the input.\n\n"
                    "The light steps are captured next. Keep the source and the "
                    "fibre untouched."):
                raise gc._Abort("cancelled at the release prompt")
            self.lbl_status.setText("Capturing the light steps…")
            yield (lambda: wf.capture_steps(sleep=self._worker_sleep,
                                            progress=self._progress,
                                            check=self._checkpoint))
            self._checkpoint()
            if cover and not self._ask(
                    "Dark spectra (after)",
                    "Block the light path / cover the input again.\n\n"
                    "The dark spectra AFTER the steps are captured next."):
                raise gc._Abort("cancelled at the second dark prompt")
            self.lbl_status.setText("Capturing dark spectra (after)…")
            yield (lambda: wf.capture_darks("after", progress=self._progress))
            if cover and not self._ask(
                    "Release the light path",
                    "Release the light path / uncover the input."):
                raise gc._Abort("cancelled at the release prompt")
            self._note = ""
            self.lbl_status.setText("Computing the correction…")
            try:
                yield wf.compute
            except ValueError as e:
                self.last_status = "failed"
                self.last_error = str(e)
                self.lbl_result.setText(_span(_T().DANGER, f"<b>FAILED</b>: {e}"))
                yield from self._protocol(wf)
                self.lbl_status.setText("Linearity run failed; nothing saved.")
                return
            self._show_result(wf)
            self.lbl_status.setText("Saving…")
            yield (lambda: wf.save(activate=True))
            if eng is not None:
                yield eng.reload_linearity
            self.dlg.notify_profiles_changed()
            yield from self._protocol(wf)
            self.last_status = "saved" if not check_only else "check_saved"
            self.lbl_status.setText(
                "Check measurement saved; no correction applied." if check_only
                else f"Saved and activated linearity record {wf.saved['id']}.")
        except Exception as e:                                # noqa: BLE001
            self._note = ""
            if isinstance(e, gc._Abort):
                self.last_status = "cancelled"
                self.lbl_status.setText("Cancelled.")
            else:
                self.last_error = str(e)
                self.last_status = "error"
                self.lbl_status.setText(f"Stopped: {e}")
                QMessageBox.critical(self, "Linearity calibration stopped", str(e))

    def _protocol(self, wf):
        eng = self._engine()
        self.lbl_status.setText("Writing the protocol…")
        try:
            paths, note = yield (lambda: eng._write_linearity_protocol(wf))
        except Exception as e:                                # noqa: BLE001
            paths, note = [], f"Protocol not written: {e}"
        self.protocol_paths = list(paths or [])
        self.protocol_message = note or ""
        self._set_protocol_display()

    # ── result display ───────────────────────────────────────────────────
    def _show_result(self, wf):
        t = _T()
        m = wf.model or {}
        self.plot.clear()
        curve = np.asarray(m.get("deviation_curve") or [], float)
        if curve.ndim == 2 and curve.shape[0] > 1:
            self.plot.plot(curve[:, 0], curve[:, 1], name="deviation h(N) − 1",
                           pen=pg.mkPen((255, 170, 60), width=2))
            lo = (m.get("valid_range_counts") or [None])[0]
            if lo is not None:
                reg = pg.LinearRegionItem(values=(0.0, float(lo)), movable=False,
                                          brush=pg.mkBrush(128, 128, 128, 70),
                                          pen=pg.mkPen(None))
                reg.setZValue(-10)
                self.plot.addItem(reg)
            self.plot.autoRange()
        rows = []
        for r in wf.steps_table or []:
            def f(key, fmt):
                v = r.get(key)
                return "—" if v is None else format(v, fmt)
            rows.append({"cells": [r.get("role", ""), f"{r['t_us']/1000:.3f}",
                                   f("peak_pct_fs", ".1f"), f("dev_raw_pct", "+.2f"),
                                   f("dev_corr_pct", "+.2f")]})
        gtab.fill_table(self.tbl_steps, rows)
        lines = []
        if wf.mode == cl.MODE_CHECK:
            lines.append(_span(t.ACCENT, "<b>Check measurement only</b> — the device "
                                         "linearises itself; no correction applied."))
        lines.append(f"Max deviation {m.get('max_deviation_pct', 0.0):.2f} % · "
                     f"rms before {m.get('rms_before_pct', 0.0):.3f} % → after "
                     f"{m.get('rms_after_pct', 0.0):.3f} % · valid "
                     f"{_fmt_range(m.get('valid_range_counts'))}")
        if wf.lamp:
            lines.append(f"Lamp drift {wf.lamp.get('drift_pct', 0.0):.2f} %")
        for d in wf.drift or []:
            lines.append(f"Dark drift at {d['integration_us']/1000:.2f} ms: "
                         f"{d['mean_counts']:+.1f} counts")
        for w in wf.warnings:
            lines.append(_span(t.WARN, w))
        self.lbl_result.setText("<br>".join(lines))


def _owner():
    from engine import LOCAL
    return LOCAL

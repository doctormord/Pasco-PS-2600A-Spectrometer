"""GUI tests for the hardware-test findings 3-6 (offscreen, Demo backend):
spectral wizard order incl. dark after + verification, Linearity tab, scope
reference overlay."""
from __future__ import annotations

import os
import time
import types

import numpy as np
import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PyQt6")
pytest.importorskip("pyqtgraph")

import pyqtgraph as pg  # noqa: E402
from PyQt6.QtWidgets import QApplication, QComboBox, QMessageBox  # noqa: E402

import calibration_linearity as cl  # noqa: E402
import calibration_profiles as cp  # noqa: E402
import calibration_workflow as cw  # noqa: E402
import reference_sources as rs  # noqa: E402
from test_gui_calibration import FakeDashboard, BACKEND  # noqa: E402


# qapp: the session-wide QApplication from tests/conftest.py


@pytest.fixture
def msgbox(monkeypatch):
    Std = QMessageBox.StandardButton
    monkeypatch.setattr(QMessageBox, "question", staticmethod(lambda *a, **k: Std.Yes))
    for n in ("information", "warning", "critical"):
        monkeypatch.setattr(QMessageBox, n, staticmethod(lambda *a, **k: Std.Ok))


class EngineDashboard(FakeDashboard):
    """Stub dashboard on top of a REAL engine (Demo backend)."""

    def __init__(self, engine):
        super().__init__(engine.acquisition)
        self.engine = engine
        self.scope_synced = 0

    def sync_scope_reference(self):
        self.scope_synced += 1


def _wait(app, tab, timeout=120.0):
    t0 = time.monotonic()
    while tab.running and time.monotonic() - t0 < timeout:
        app.processEvents()
        time.sleep(0.01)
    assert not tab.running, "run did not finish in time"


def _engine(cfg_data, isolated_config):
    from engine import Engine
    isolated_config._data.update(cfg_data)
    e = Engine()
    e.connect(BACKEND)
    t0 = time.time()
    while e.last_frame is None and time.time() - t0 < 10:
        time.sleep(0.02)
    assert e.last_frame is not None
    return e


# ── 1. spectral wizard: new order, dark after, verification ─────────────────
def test_wizard_dark_after_verification_and_text(qapp, msgbox, isolated_config,
                                                 monkeypatch):
    import gui_calibration as gc
    e = _engine({"demo_spectrum": "cal_lamp", "demo_sensitivity": "synthetic",
                 "demo_fullscale_ms": 100.0, "calibration_stability_delay_s": 0.2,
                 "calibration_capture_frames": 8}, isolated_config)
    try:
        # Pretend a device without a shutter: all four cover / uncover prompts
        # must be asked in the new order.
        monkeypatch.setattr(cw.CalibrationWorkflow, "needs_user_cover",
                            property(lambda self: True))
        asked = []
        monkeypatch.setattr(gc, "_ask", lambda parent, title, text, **k:
                            (asked.append((title, text)), True)[1])
        dash = EngineDashboard(e)
        dlg = gc.CalibrationDialog(dash)
        w = dlg.tab_wizard
        assert w._step_names == ["Selection", "Wavelength check", "Integration time",
                                 "Dark before", "Calibration + repeat", "t/2 capture",
                                 "Dark after + checks", "Compute", "Save",
                                 "Verification"]
        w.cmb_optics.setEditText("Demo fibre")
        w.cmb_input.setCurrentText("bare")
        w.cmb_ref.setCurrentIndex(w.cmb_ref.findData(rs.DEMO_SOURCE_ID))
        w.start()
        _wait(qapp, w)
        assert w.last_error is None, w.last_error
        assert w.last_status == "saved"
        assert [t for t, _ in asked] == ["Dark spectrum", "Release the light path",
                                         "Dark spectrum (after)",
                                         "Release the light path"]
        assert "AFTER the light captures" in asked[2][1]
        assert "verification measurement" in asked[3][1]
        wf = w._wf
        assert wf.dark_after is not None and wf.dark_drift
        assert wf.verification is not None
        assert w.step_states[9][0] in ("ok", "warning")
        assert "rms" in w.step_states[9][1]
        assert all(s[0] in ("ok", "warning") for s in w.step_states.values())
        assert e.last_verification["profile_id"] == wf.saved[0]["id"]
        assert dash.scope_synced >= 1
        # result text: checks with percentage / pixels / cause, dark drift,
        # verification lines
        txt = w.lbl_result.text()
        assert "Stability:" in txt and "px masked" in txt and "Cause:" in txt
        assert "Integration cross check (t/2):" in txt
        assert "Dark drift at" in txt
        assert "Verification (normalised" in txt
        assert wf.checks["stability_cause"] in txt
        # the protocol is written AFTER the verification (it contains it)
        assert w.protocol_paths
        assert wf.record()["verification"] is not None
        dlg.close()
    finally:
        e.close()


def test_wizard_verification_failure_does_not_abort(qapp, msgbox, isolated_config,
                                                    monkeypatch):
    import gui_calibration as gc
    e = _engine({"demo_spectrum": "cal_lamp", "demo_sensitivity": "synthetic",
                 "demo_fullscale_ms": 100.0, "calibration_stability_delay_s": 0.1,
                 "calibration_capture_frames": 6}, isolated_config)
    try:
        def boom(*a, **k):
            raise RuntimeError("no light")
        monkeypatch.setattr(e, "verify_profile", boom)
        dlg = gc.CalibrationDialog(EngineDashboard(e))
        w = dlg.tab_wizard
        w.cmb_optics.setEditText("Fibre")
        w.cmb_input.setCurrentText("bare")
        w.cmb_ref.setCurrentIndex(w.cmb_ref.findData(rs.DEMO_SOURCE_ID))
        w.start()
        _wait(qapp, w)
        assert w.last_status == "saved" and w.last_error is None
        assert w.step_states[9][0] == "warning"
        assert any("Verification failed" in x for x in w._wf.warnings)
        assert w.protocol_paths
        dlg.close()
    finally:
        e.close()


# ── 2. Linearity tab ────────────────────────────────────────────────────────
def test_linearity_tab_run_activates_correction(qapp, msgbox, isolated_config):
    import gui_calibration as gc
    e = _engine({"demo_spectrum": "tungsten", "demo_fullscale_ms": 100.0,
                 "demo_nonlinearity": 0.03, "linearity_steps": 6,
                 "linearity_capture_frames": 6}, isolated_config)
    try:
        dlg = gc.CalibrationDialog(EngineDashboard(e))
        titles = [dlg.tabs.tabText(i) for i in range(dlg.tabs.count())]
        assert "Linearity" in titles
        t = dlg.tab_linearity
        dlg.tabs.setCurrentIndex(titles.index("Linearity"))
        assert t.table.rowCount() == 0
        assert "none" in t.lbl_state.text()
        assert "stable broadband source" in t.lbl_info.text()
        assert t.btn_run.isEnabled() and not t.btn_activate.isEnabled()
        t.start()
        assert t.running and not dlg.btn_close.isEnabled()
        _wait(qapp, t)
        assert t.last_error is None, t.last_error
        assert t.last_status == "saved"
        assert e.linearity_info["state"] == cl.STATE_ACTIVE
        assert t.table.rowCount() == 1 and t.table.item(0, 6).text() == "yes"
        assert "active" in t.lbl_state.text()
        assert t.tbl_steps.rowCount() >= 6
        txt = t.lbl_result.text()
        assert "Max deviation" in txt and "rms before" in txt and "Lamp drift" in txt
        assert t.plot.getPlotItem().listDataItems()          # deviation curve
        assert t.protocol_paths and t.btn_open_folder.isEnabled()
        assert dlg.btn_close.isEnabled()
        # switch off / activate again
        t.switch_off()
        assert e.linearity_info["state"] == cl.STATE_OFF
        assert t.table.item(0, 6).text() == ""
        t.table.selectRow(0)
        assert t.btn_activate.isEnabled()
        t.activate()
        assert e.linearity_info["state"] == cl.STATE_ACTIVE
        dlg.close()
    finally:
        e.close()


def test_linearity_tab_factory_mode_text(qapp, msgbox, monkeypatch):
    import gui_calibration as gc
    import gui_linearity as gl

    class Stub:
        serial = "HDX1"
        nl_correction_active = True
        SUPPORTS_SHUTTER = True

    eng = types.SimpleNamespace(device_name=BACKEND,
                                linearity_info={"state": cl.STATE_FACTORY})
    dash = FakeDashboard()
    dash.engine = eng
    dlg = gc.CalibrationDialog(dash)
    dash.hardware_thread = Stub()          # the connected unit appears afterwards
    t = dlg.tab_linearity
    t.refresh()
    assert "Factory linearisation" in t.lbl_state.text()
    assert "CHECK measurement" in t.lbl_info.text()
    assert "no correction is applied" in t.lbl_info.text()
    assert t.btn_run.text() == "Run check measurement"
    assert not t.btn_activate.isEnabled() and not t.btn_off.isEnabled()
    # starting builds a check-mode workflow (the run itself is not executed here)
    started = []
    monkeypatch.setattr(gl.LinearityTab, "_begin", lambda self, gen: started.append(gen))
    t.start()
    assert started and t._wf.mode == cl.MODE_CHECK
    assert "CHECK measurement" in t.lbl_info.text()
    dlg.close()


def test_linearity_tab_without_device(qapp, msgbox):
    import gui_calibration as gc
    dlg = gc.CalibrationDialog(FakeDashboard())
    t = dlg.tab_linearity
    assert "No device connected" in t.lbl_state.text()
    assert not t.btn_run.isEnabled()
    dlg.close()


# ── 3. scope reference overlay ──────────────────────────────────────────────
class _ScopeStub:
    """The real overlay methods of the dashboard on a light stand-in (a full
    DashboardWindow leaves Qt state behind in the test process)."""

    def __init__(self, engine):
        import gui_dashboard as gd
        self.engine = engine
        self.scope_ref_curve = pg.PlotCurveItem()
        self.scope_ref_curve.setVisible(False)
        self.combo_scope_ref = QComboBox()
        for n in ("_fill_scope_reference_combo", "sync_scope_reference",
                  "toggle_scope_reference", "_scope_reference_picked",
                  "_update_scope_reference"):
            setattr(self, n, types.MethodType(getattr(gd.DashboardWindow, n), self))
        self._fill_scope_reference_combo()
        self.combo_scope_ref.currentIndexChanged.connect(self._scope_reference_picked)


def test_scope_reference_overlay_toggle(qapp, isolated_config):
    e = _engine({"demo_spectrum": "cal_lamp", "demo_fullscale_ms": 100.0,
                 "scope_reference_id": rs.DEMO_SOURCE_ID}, isolated_config)
    try:
        s = _ScopeStub(e)
        assert s.combo_scope_ref.count() >= 1
        assert s.combo_scope_ref.currentData() == rs.DEMO_SOURCE_ID   # default from config
        s._update_scope_reference(e.last_frame)
        assert not s.scope_ref_curve.isVisible()                      # overlay off
        s.toggle_scope_reference(True)
        assert isolated_config.get("scope_reference_overlay") is True
        assert s.scope_ref_curve.isVisible()
        x, y = s.scope_ref_curve.getData()
        assert len(x) == len(e.last_frame.wavelengths) and np.isfinite(y).any()
        assert np.nanmax(y) > 0
        # new frame → curve follows the displayed values
        s._update_scope_reference(e.last_frame)
        assert s.scope_ref_curve.isVisible()
        # selection is persisted; unknown id hides the curve
        s.combo_scope_ref.setCurrentIndex(s.combo_scope_ref.findData(rs.DEMO_SOURCE_ID))
        assert isolated_config.get("scope_reference_id") == rs.DEMO_SOURCE_ID
        isolated_config._data["scope_reference_id"] = "nope"
        s.sync_scope_reference()
        s.combo_scope_ref.blockSignals(True)
        s.combo_scope_ref.addItem("ghost", "nope")
        s.combo_scope_ref.setCurrentIndex(s.combo_scope_ref.count() - 1)
        s.combo_scope_ref.blockSignals(False)
        s._update_scope_reference(e.last_frame)
        assert not s.scope_ref_curve.isVisible()
        s.combo_scope_ref.setCurrentIndex(0)
        s.toggle_scope_reference(False)
        assert not s.scope_ref_curve.isVisible()
        assert isolated_config.get("scope_reference_overlay") is False
    finally:
        e.close()


def test_dashboard_has_overlay_controls():
    """Wiring of the real dashboard (source check; see the note in
    test_calibration_ui_regressions on constructing DashboardWindow)."""
    import inspect
    import gui_dashboard
    src = inspect.getsource(gui_dashboard.DashboardWindow)
    assert "self.button_scope_ref = ToggleSwitch" in src
    assert '"Reference overlay"' in src
    assert "self.engine.reference_overlay(rid, ev.wavelengths, vals, unit)" in src
    assert "self._update_scope_reference(ev)" in src

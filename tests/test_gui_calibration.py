"""GUI tests for the calibration dialog (offscreen). Skipped without PyQt6."""
from __future__ import annotations

import os
import time

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PyQt6")
pytest.importorskip("pyqtgraph")

from PyQt6.QtWidgets import QApplication, QMessageBox, QWidget  # noqa: E402

import calibration_profiles as cp  # noqa: E402
import reference_sources as rs  # noqa: E402

BACKEND = "Demo (virtual)"


# qapp: the session-wide QApplication from tests/conftest.py


class _Combo:
    def __init__(self, text):
        self._t = text

    def currentText(self):
        return self._t


class FakeDashboard(QWidget):
    def __init__(self, backend=None):
        super().__init__()
        self.hardware_thread = backend
        self.combo_backend = _Combo(BACKEND)
        self.paused = False
        self.changed = 0
        self.active_wavelengths = None

    def is_measurement_paused(self):
        return self.paused

    def set_measurement_paused(self, v):
        self.paused = bool(v)

    def sync_active_axis(self, wl):
        self.active_wavelengths = wl

    def on_calibration_profiles_changed(self):
        self.changed += 1


@pytest.fixture
def msgbox(monkeypatch):
    Std = QMessageBox.StandardButton
    monkeypatch.setattr(QMessageBox, "question", staticmethod(lambda *a, **k: Std.Yes))
    for n in ("information", "warning", "critical"):
        monkeypatch.setattr(QMessageBox, n, staticmethod(lambda *a, **k: Std.Ok))


def _dialog(dash):
    import gui_calibration as gc
    return gc.CalibrationDialog(dash)


def test_dialog_has_all_tabs(qapp, msgbox):
    dlg = _dialog(FakeDashboard())
    titles = [dlg.tabs.tabText(i) for i in range(dlg.tabs.count())]
    assert titles == ["Wavelength", "Spectral calibration", "Profiles", "Lamps",
                      "Linearity", "Response (legacy)"]
    w = dlg.tab_wizard
    # absolute is disabled for a bare fibre
    w.cmb_input.setCurrentText("bare")
    assert not w.cmb_type.model().item(1).isEnabled()
    w.cmb_input.setCurrentText("CC")
    assert w.cmb_type.model().item(1).isEnabled()
    dlg.close()


def test_start_without_device_does_not_run(qapp, msgbox):
    dlg = _dialog(FakeDashboard())
    dlg.tab_wizard.start()
    assert not dlg.tab_wizard.running


def test_lamps_tab_import_remove(qapp, msgbox, lmp_path):
    dash = FakeDashboard()
    dlg = _dialog(dash)
    t = dlg.tab_lamps
    n0 = t.table.rowCount()
    assert n0 >= 1 and t.table.item(0, 5).text() == "yes"
    src = t.do_import(lmp_path, name="HAL-CAL test", serial="X1", unit=rs.UNIT_UW_CM2_NM,
                      valid_min_nm=380.0, valid_max_nm=None, input_optic="CC", notes="")
    assert src is not None and t.table.rowCount() == n0 + 1
    # wizard source list refreshed
    assert dlg.tab_wizard.cmb_ref.findData(src.id) >= 0
    t.table.selectRow(n0)
    assert t.selected()[0] == src.id
    t.remove_clicked()
    assert t.table.rowCount() == n0
    # built-in cannot be removed
    t.table.selectRow(0)
    t.remove_clicked()
    assert t.table.rowCount() == n0


def _profile():
    import calibration_profiles as calprof
    import numpy as np
    wl = np.linspace(400, 800, 50)
    return calprof.save_profile(BACKEND, "legacy one", np.column_stack([wl, np.ones(50)]),
                                {"wl_min_nm": 400.0, "wl_max_nm": 800.0})


def test_profiles_tab_activate_and_delete(qapp, msgbox):
    dash = FakeDashboard()
    dlg = _dialog(dash)
    p = _profile()
    t = dlg.tab_profiles
    t.refresh()
    assert t.table.rowCount() == 1
    t.table.selectRow(0)
    t.activate()
    assert cp.active_selection(BACKEND) == p["id"]
    assert dash.changed >= 1
    t.delete()
    assert t.table.rowCount() == 0


@pytest.fixture
def demo(isolated_config, tmp_path):
    from _device_demo import DemoSpectrometer
    isolated_config._data.update({
        "demo_spectrum": "cal_lamp", "demo_sensitivity": "synthetic",
        "demo_fullscale_ms": 100.0, "calibration_stability_delay_s": 0.2,
        "calibration_capture_frames": 8, "export_dir": str(tmp_path / "exports")})
    b = DemoSpectrometer(on_frame=lambda *a: None)
    b.connect("Demo Spectrometer")
    b.set_integration_time_us(20_000)
    b.start()
    yield b
    b.disconnect()


def _wait(app, tab, timeout=120.0):
    t0 = time.monotonic()
    while tab.running and time.monotonic() - t0 < timeout:
        app.processEvents()
        time.sleep(0.01)
    assert not tab.running, "wizard did not finish in time"


def test_wizard_full_run_saves_and_activates_profile(qapp, msgbox, demo):
    dash = FakeDashboard(demo)
    dlg = _dialog(dash)
    w = dlg.tab_wizard
    w.cmb_optics.setEditText("Demo fibre")
    w.cmb_input.setCurrentText("bare")
    w.cmb_ref.setCurrentIndex(w.cmb_ref.findData(rs.DEMO_SOURCE_ID))
    assert w.spn_frames.value() == 8
    w.start()
    assert w.running and not dlg.btn_close.isEnabled()
    dlg.close()                                   # ignored while a step runs
    dlg.reject()
    assert dlg._busy
    _wait(qapp, w)
    assert w.last_error is None, w.last_error
    assert w.last_status == "saved"
    assert all(s[0] in ("ok", "warning") for s in w.step_states.values())
    profs = cp.profiles_for_device(BACKEND, demo.serial, demo.wl_fingerprint())
    assert len(profs) == 1 and profs[0]["optics"]["name"] == "Demo fibre"
    assert cp.active_selection(BACKEND) == profs[0]["id"]
    assert dash.changed >= 1
    assert dlg.btn_close.isEnabled()
    # the profile shows up in the Profiles tab
    dlg.tab_profiles.refresh()
    assert dlg.tab_profiles.table.rowCount() == 1


def test_wizard_derivation_run(qapp, msgbox, demo):
    from calibration_workflow import CalibrationWorkflow, WorkflowConfig
    wf = CalibrationWorkflow(demo, BACKEND, kind="relative",
                             optics={"name": "CC", "input": "CC"},
                             reference=rs.ReferenceLibrary().get(rs.DEMO_SOURCE_ID),
                             cfg=WorkflowConfig(n_frames=8, stability_delay_s=0.2))
    wf.run_all()
    dash = FakeDashboard(demo)
    dlg = _dialog(dash)
    w = dlg.tab_wizard
    w.cmb_mode.setCurrentIndex(1)
    w.refresh()
    assert w.cmb_known.count() == 1
    w.edit_new_name.setText("bare from CC")
    w.cmb_new_input.setCurrentText("bare")
    # own integration time per optics (fixed here to keep the run short)
    w.cmb_itime_known.setCurrentIndex(1)
    w.spn_ms_known.setValue(wf.integration_us / 1000.0)
    w.cmb_itime_new.setCurrentIndex(1)
    w.spn_ms_new.setValue(wf.integration_us / 1000.0)
    w.start()
    _wait(qapp, w)
    assert w.last_error is None, w.last_error
    assert w.last_status == "saved"
    names = {(p["optics"]["name"]) for p in cp.profiles_for_device(
        BACKEND, demo.serial, demo.wl_fingerprint())}
    assert "bare from CC" in names

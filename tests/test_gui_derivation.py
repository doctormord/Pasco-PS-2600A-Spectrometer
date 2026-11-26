"""GUI tests of the derivation wizard (offscreen): own integration time per
optics, step order, prompts without shutter, result lines, protocol, status
logic of a failed drift check."""
from __future__ import annotations

import glob
import os
import time

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PyQt6")
pytest.importorskip("pyqtgraph")

from PyQt6.QtWidgets import QApplication, QMessageBox, QWidget  # noqa: E402

import calibration_profiles as cp  # noqa: E402
import calibration_workflow as cw  # noqa: E402
import reference_sources as rs  # noqa: E402

BACKEND = "Demo (virtual)"
Std = QMessageBox.StandardButton


@pytest.fixture(scope="module")
def qapp():
    yield QApplication.instance() or QApplication([])


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
def demo(isolated_config, tmp_path):
    from _device_demo import DemoSpectrometer
    isolated_config._data.update({
        "demo_spectrum": "cal_lamp", "demo_sensitivity": "synthetic",
        "demo_fullscale_ms": 1.0, "demo_min_integration_us": 10,
        "demo_max_integration_us": 2_000_000, "calibration_stability_delay_s": 0.1,
        "calibration_capture_frames": 6, "export_dir": str(tmp_path / "exports")})
    b = DemoSpectrometer(on_frame=lambda *a: None)
    b.connect("Demo Spectrometer")
    b.set_integration_time_us(800)
    b.start()
    yield b
    b.disconnect()


@pytest.fixture
def prompts(monkeypatch, demo):
    """Answers every question with Yes/OK and records the titles. Plays the
    user at a device without shutter: covers on 'Dark spectrum', releases
    on 'Release the light path'; `hooks` maps a title to a callable (or to a
    standard button to answer with)."""
    log: list[str] = []
    hooks: dict = {}
    state = {"cover": False}

    def question(parent, title, text, *a, **k):
        log.append(title)
        if state["cover"]:
            demo._shutter_closed = title == "Dark spectrum"
        h = hooks.get(title)
        if isinstance(h, Std):
            return h
        if callable(h):
            h()
        return Std.Yes

    monkeypatch.setattr(QMessageBox, "question", staticmethod(question))
    for n in ("information", "warning", "critical"):
        monkeypatch.setattr(QMessageBox, n, staticmethod(lambda *a, **k: Std.Ok))
    log_obj = type("P", (), {})()
    log_obj.titles, log_obj.hooks, log_obj.state = log, hooks, state
    return log_obj


def _dialog(dash):
    import gui_calibration as gc
    return gc.CalibrationDialog(dash)


def _wait(app, tab, timeout=120.0):
    t0 = time.monotonic()
    while tab.running and time.monotonic() - t0 < timeout:
        app.processEvents()
        time.sleep(0.01)
    assert not tab.running, "wizard did not finish in time"


def _calibrate_known(demo):
    wf = cw.CalibrationWorkflow(
        demo, BACKEND, kind="relative", optics={"name": "CC", "input": "CC"},
        reference=rs.ReferenceLibrary().get(rs.DEMO_SOURCE_ID),
        cfg=cw.WorkflowConfig(n_frames=6, stability_delay_s=0.1))
    wf.run_all()
    return wf


def _open_wizard(qapp, demo):
    dlg = _dialog(FakeDashboard(demo))
    w = dlg.tab_wizard
    w.cmb_mode.setCurrentIndex(1)
    w.refresh()
    assert w.cmb_known.count() == 1
    w.edit_new_name.setText("bare from CC")
    w.cmb_new_input.setCurrentText("bare")
    return dlg, w


def _log_states(w):
    events: list[tuple[int, str]] = []
    orig = w._state

    def spy(i, state, detail=""):
        events.append((i, state))
        orig(i, state, detail)
    w._state = spy
    return events


def test_form_has_separate_integration_times_and_source_hint(qapp, demo, prompts):
    _calibrate_known(demo)
    dlg, w = _open_wizard(qapp, demo)
    assert w.lbl_source_hint.text().count(cw.DERIVATION_SOURCE_HINT) == 1
    assert w.lbl_source_hint.wordWrap()
    assert not w.spn_ms_known.isEnabled() and not w.spn_ms_new.isEnabled()
    w.cmb_itime_known.setCurrentIndex(1)
    assert w.spn_ms_known.isEnabled() and not w.spn_ms_new.isEnabled()
    w.cmb_itime_new.setCurrentIndex(1)
    assert w.spn_ms_new.isEnabled()
    # the shared integration row belongs to the lamp mode only
    assert w.cmb_itime.isHidden() and w.spn_ms.isHidden()
    assert w._step_names[1:-1] == [label for _k, label in cw.DERIVATION_STEPS]
    w.cmb_mode.setCurrentIndex(0)
    assert not w.cmb_itime.isHidden()
    dlg.close()


def test_wizard_runs_with_separate_integration_times(qapp, demo, prompts):
    _calibrate_known(demo)
    dlg, w = _open_wizard(qapp, demo)
    w.cmb_itime_known.setCurrentIndex(1); w.spn_ms_known.setValue(0.8)
    w.cmb_itime_new.setCurrentIndex(1); w.spn_ms_new.setValue(0.4)
    events = _log_states(w)
    w.start()
    _wait(qapp, w)
    assert w.last_error is None, w.last_error
    assert w.last_status == "saved"
    # steps run in the order of the step list
    running = [i for i, s in events if s == "running"]
    assert running == sorted(running) and len(running) == len(set(running))
    assert w._step_names[1:-1] == [label for _k, label in cw.DERIVATION_STEPS]
    assert all(s[0] in ("ok", "warning") for s in w.step_states.values())
    # shutter device: no cover / release prompts, only the first note
    assert prompts.titles == ["Known optics", "New optics", "Known optics again"]
    # result lines: both integration times, drift, linearity correction
    text = w.lbl_result.toPlainText()
    assert "Known optics: t_int 0.800 ms (fixed)" in text
    assert "New optics: t_int 0.400 ms (fixed)" in text
    assert "% FS" in text and "linearity correction:" in text
    assert "Source drift: p95" in text and "limit 2.00 %" in text and "passed" in text
    assert "nan" not in text.lower()
    assert w.lbl_itime.text().startswith("Known: 0.800 ms")
    # profile saved, active, protocol written (PDF + CSV)
    names = {p["optics"]["name"] for p in cp.profiles_for_device(
        BACKEND, demo.serial, demo.wl_fingerprint())}
    assert "bare from CC" in names
    assert len(w.protocol_paths) == 2
    assert all(os.path.exists(p) and "derivation_" in os.path.basename(p)
               for p in w.protocol_paths)
    csv_ = next(p for p in w.protocol_paths if p.endswith(".csv"))
    head = open(csv_, encoding="utf-8").read()
    assert "# t_int_known_us;800" in head and "# t_int_new_us;400" in head
    dlg.close()


def test_auto_exposure_per_side_and_prompts_without_shutter(qapp, demo, prompts,
                                                            monkeypatch):
    _calibrate_known(demo)
    monkeypatch.setattr(type(demo), "SUPPORTS_SHUTTER", False)
    prompts.state["cover"] = True                  # the 'user' covers / releases
    dlg, w = _open_wizard(qapp, demo)              # both sides Auto
    w.start()
    _wait(qapp, w)
    assert w.last_error is None, w.last_error
    assert w.last_status in ("saved", "saved_implausible", "not_saved")
    t = prompts.titles
    assert t.count("Dark spectrum") == 4           # known, new, new after, known after
    assert t.count("Release the light path") == 3  # known, new, repeat light
    # order of the prompts follows the steps
    assert t.index("Known optics") < t.index("Dark spectrum") < t.index("New optics")
    assert t.index("New optics") < t.index("Known optics again")
    assert "(auto)" in w.lbl_result.toPlainText()
    dlg.close()


def test_failed_drift_check_asks_and_does_not_save_on_no(qapp, demo, prompts,
                                                         monkeypatch):
    """GUI path of a failed drift check with a plausible result. The drift
    detection itself is covered in test_derivation_workflow.py; here the
    verdict is forced (a real drift masks a noise-dependent number of pixels
    and may make the result implausible, which is a different dialog)."""
    _calibrate_known(demo)
    orig = cw.DerivationWorkflow.evaluate_drift

    def failing_drift(self):
        d = orig(self)
        d["pass"] = False
        self.checks["drift_pass"] = False
        return d
    monkeypatch.setattr(cw.DerivationWorkflow, "evaluate_drift", failing_drift)
    dlg, w = _open_wizard(qapp, demo)
    w.cmb_itime_known.setCurrentIndex(1); w.spn_ms_known.setValue(0.8)
    w.cmb_itime_new.setCurrentIndex(1); w.spn_ms_new.setValue(0.8)
    prompts.hooks["Checks failed"] = Std.No
    w._exec_box = lambda box: Std.No          # never block on a modal box
    n_before = len(cp.profiles_for_device(BACKEND, demo.serial, demo.wl_fingerprint()))
    try:
        w.start()
        _wait(qapp, w)
    finally:
        demo.source_scale = 1.0
    assert w.last_error is None, w.last_error
    assert "Checks failed" in prompts.titles
    assert w.last_status == "not_saved"
    assert len(cp.profiles_for_device(BACKEND, demo.serial, demo.wl_fingerprint())) == n_before
    text = w.lbl_result.toPlainText()
    assert "NOT passed" in text
    # the protocol is written for the failed run, too
    assert len(w.protocol_paths) == 2
    csv_ = next(p for p in w.protocol_paths if p.endswith(".csv"))
    assert "# drift_pass;no" in open(csv_, encoding="utf-8").read()
    dlg.close()


def test_saturation_shows_factual_message(qapp, demo, prompts, monkeypatch):
    _calibrate_known(demo)
    dlg, w = _open_wizard(qapp, demo)
    w.cmb_itime_known.setCurrentIndex(1); w.spn_ms_known.setValue(1.8)   # > full scale
    shown = []
    monkeypatch.setattr(QMessageBox, "critical",
                        staticmethod(lambda *a, **k: shown.append(a[2]) or Std.Ok))
    w.start()
    _wait(qapp, w)
    assert w.last_status == "error"
    assert shown and "saturated" in shown[0] and "known optics" in shown[0]
    assert "profile" not in w.lbl_status.text().lower() or "Stopped" in w.lbl_status.text()
    dlg.close()

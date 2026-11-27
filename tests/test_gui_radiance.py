"""GUI tests for the radiance block (offscreen, Demo backend): admissibility in
the spectral-calibration wizard, the sphere characterisation mode, lamp import
with radiance units and the colour tab (luminance in cd/m²)."""
from __future__ import annotations

import os
import time

import numpy as np
import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PyQt6")
pytest.importorskip("pyqtgraph")

from PyQt6.QtWidgets import QMessageBox  # noqa: E402

import calibration_profiles as cp  # noqa: E402
import calibration_sphere as csph  # noqa: E402
import calibration_workflow as cw  # noqa: E402
import color_science as cs  # noqa: E402
import reference_sources as rs  # noqa: E402
from test_gui_calibration import FakeDashboard  # noqa: E402

BACKEND = "Demo (virtual)"
Std = QMessageBox.StandardButton
R_MM = 7.0
D0_TRUE = 0.3
K_SPHERE = 0.8


# qapp: the session-wide QApplication from tests/conftest.py


@pytest.fixture
def demo(isolated_config, tmp_path):
    from _device_demo import DemoSpectrometer
    isolated_config._data.update({
        "demo_spectrum": "cal_lamp", "demo_sensitivity": "synthetic",
        "demo_fullscale_ms": 100.0, "selected_backend": BACKEND,
        "calibration_stability_delay_s": 0.1,
        "export_dir": str(tmp_path / "exports")})
    b = DemoSpectrometer(on_frame=lambda *a: None)
    b.connect("Demo Spectrometer")
    b.set_integration_time_us(20_000)
    b.start()
    yield b
    b.source_scale = 1.0
    b.disconnect()


@pytest.fixture
def boxes(monkeypatch):
    """Answers every question with Yes (`answers` maps a title to a button or
    a callable) and records warnings / questions."""
    log = type("L", (), {})()
    log.questions, log.warnings, log.answers = [], [], {}

    def question(parent, title, text, *a, **k):
        log.questions.append(title)
        h = log.answers.get(title)
        if callable(h):
            h = h()
        return h if isinstance(h, Std) else Std.Yes

    def warning(parent, title, text, *a, **k):
        log.warnings.append(text)
        return Std.Ok

    monkeypatch.setattr(QMessageBox, "question", staticmethod(question))
    monkeypatch.setattr(QMessageBox, "warning", staticmethod(warning))
    for n in ("information", "critical"):
        monkeypatch.setattr(QMessageBox, n, staticmethod(lambda *a, **k: Std.Ok))
    return log


def _dialog(dash):
    import gui_calibration as gc
    return gc.CalibrationDialog(dash)


def _wait(app, tab, timeout=180.0):
    t0 = time.monotonic()
    while tab.running and time.monotonic() - t0 < timeout:
        app.processEvents()
        time.sleep(0.01)
    assert not tab.running, "wizard did not finish in time"


def _radiance_source(dlg, lmp_path, name="Rad ref"):
    src = dlg.tab_lamps.do_import(lmp_path, name=name, serial="R1",
                                  unit=rs.UNIT_UW_CM2_SR_NM, valid_min_nm=380.0,
                                  valid_max_nm=None, input_optic="objective", notes="")
    assert src is not None
    return src


# ── wizard: admissibility ────────────────────────────────────────────────

def test_wizard_admissibility_objective_and_quantity(qapp, boxes, lmp_path):
    dlg = _dialog(FakeDashboard())
    w = dlg.tab_wizard
    rad = _radiance_source(dlg, lmp_path)
    for cmb in (w.cmb_input, w.cmb_new_input):
        assert cmb.findText("objective") >= 0
    absolute = w.cmb_type.model().item(1)

    # irradiance reference + objective: absolute refused, reason in the info pane
    w.cmb_ref.setCurrentIndex(w.cmb_ref.findData(rs.DEMO_SOURCE_ID))
    w.cmb_input.setCurrentText("objective")
    assert not absolute.isEnabled()
    assert w.cmb_type.currentText() == cp.TYPE_RELATIVE
    assert "Type absolute not available" in w.lbl_ref_info.toPlainText()
    assert cw.RADIANCE_SOURCE_HINT[:30] not in w.lbl_ref_info.toPlainText()
    h0 = w.lbl_ref_info.height()

    # radiance reference + objective: absolute allowed, source hint shown
    w.cmb_ref.setCurrentIndex(w.cmb_ref.findData(rad.id))
    assert absolute.isEnabled()
    info = w.lbl_ref_info.toPlainText()
    assert "radiance" in info and cw.RADIANCE_SOURCE_HINT[:30] in info
    assert "Type absolute not available" not in info
    assert w.lbl_ref_info.height() == h0          # reserved pane: no layout jump
    assert "radiance" in w.cmb_ref.itemText(w.cmb_ref.currentIndex())

    # radiance reference + CC: absolute refused again
    w.cmb_input.setCurrentText("CC")
    assert not absolute.isEnabled()
    assert "Type absolute not available" in w.lbl_ref_info.toPlainText()
    # radiance + bare fibre: allowed
    w.cmb_input.setCurrentText("bare")
    assert absolute.isEnabled()
    dlg.close()


# ── sphere mode ──────────────────────────────────────────────────────────

def test_sphere_mode_form_and_missing_cc_profile(qapp, demo, boxes):
    dlg = _dialog(FakeDashboard(demo))
    w = dlg.tab_wizard
    w.cmb_mode.setCurrentIndex(2)
    assert w.cmb_mode.itemText(2) == "Characterise a sphere (radiance reference)"
    assert w.stack.currentIndex() == 2
    assert w._step_names[1:-1] == [label for _k, label in csph.SPHERE_STEPS]
    assert w.cmb_itime.isHidden() and not w.cmb_itime_sph.isHidden()
    assert w.lbl_sphere_hint.text().count(csph.SPHERE_SOURCE_HINT) == 1
    assert w.spn_sph_edge.value() == pytest.approx(11.0)
    assert w.spn_sph_cc.value() == 0.0
    assert "No active calibration profile" in w.lbl_sphere_cc.text()
    w.edit_sph_name.setText("Sphere")
    w.spn_sph_r.setValue(R_MM)
    w.start()
    assert not w.running
    assert boxes.warnings and "No active calibration profile" in boxes.warnings[-1]
    dlg.close()


def _cc_profile(demo):
    wf = cw.CalibrationWorkflow(demo, BACKEND, kind="absolute",
                                optics={"name": "CC", "input": "CC"},
                                reference=rs.ReferenceLibrary().get(rs.DEMO_SOURCE_ID),
                                cfg=cw.WorkflowConfig(n_frames=12, stability_delay_s=0.1))
    wf.run_all()
    cp.set_active_selection(BACKEND, wf.saved[0]["id"])
    return wf.saved[0]


def test_sphere_run_in_the_wizard(qapp, demo, boxes):
    cc_prof = _cc_profile(demo)
    dlg = _dialog(FakeDashboard(demo))
    w = dlg.tab_wizard
    w.cmb_mode.setCurrentIndex(2)
    w.refresh()
    assert str(cc_prof["id"]) in w.lbl_sphere_cc.text() or "CC" in w.lbl_sphere_cc.text()
    w.edit_sph_name.setText("Test sphere")
    w.edit_sph_serial.setText("S1")
    w.spn_sph_r.setValue(R_MM)
    w.spn_sph_ur.setValue(0.02)
    w.spn_frames.setValue(12)
    w.cmb_itime_sph.setCurrentIndex(1)
    w.spn_ms_sph.setValue(60.0)              # realistic signal: drift check above the noise

    heights = [0.0, 6.0, 16.0]
    asked: list[float] = []
    sc_holder = {}

    def scale_for(h):
        d = csph.port_distance_mm(h, edge_depth_mm=w.spn_sph_edge.value())
        demo.source_scale = K_SPHERE * float(csph.disk_factor(R_MM, d + D0_TRUE))

    def ask_height(index, previous, min_mm):
        assert min_mm == pytest.approx(-11.0)
        h = heights[len(asked)]
        asked.append(h)
        scale_for(h)                              # the 'user' moves the CC
        return h

    w._ask_height = ask_height
    boxes.answers["Further position?"] = lambda: (Std.Yes if len(asked) < len(heights)
                                                  else Std.No)
    boxes.answers["First distance again"] = lambda: scale_for(heights[0]) or Std.Yes
    scale_for(heights[0])
    n_before = len(rs.ReferenceLibrary().list())
    try:
        w.start()
        assert w.running
        _wait(qapp, w)
    finally:
        demo.source_scale = 1.0
    assert w.last_error is None, w.last_error
    assert w.last_status == "saved", (w.last_status, w.lbl_status.text())
    assert asked == heights
    # shutter device: only the three notes, no cover prompts
    assert "Dark spectrum" not in boxes.questions
    assert boxes.questions.count("Further position?") == 1
    assert all(st[0] in ("ok", "warning") for st in w.step_states.values()), w.step_states
    # result: d0, method, consistency, drift, U (k=2), valid range
    text = w.lbl_result.toPlainText()
    for key in ("Offset d0", "plausible", "Method:", "Consistency over 3 distances",
                "Drift (repeat", "Uncertainty U (k = 2)", "Valid range:"):
        assert key in text, key
    assert "nan" not in text.lower()
    assert abs(float(text.split("Offset d0 = ")[1].split(" mm")[0]) - D0_TRUE) < 0.15
    assert len(w.plot_in.getPlotItem().listDataItems()) == 3      # L_i per distance
    assert len(w.plot_out.getPlotItem().listDataItems()) == 1     # L(λ)
    # saved as a radiance reference, lamps tab and wizard list updated
    lib = rs.ReferenceLibrary().list()
    assert len(lib) == n_before + 1
    src = next(s for s in lib if s.name == "Test sphere")
    assert src.quantity == "radiance" and src.unit == rs.UNIT_UW_CM2_SR_NM
    t = dlg.tab_lamps
    names = [t.table.item(r, 0).text() for r in range(t.table.rowCount())]
    row = names.index("Test sphere")
    assert t.table.item(row, t.COLS.index("Quantity")).text() == "radiance"
    tip = t.table.item(row, 0).toolTip()
    assert "Sphere port" in tip and "CC profile" in tip and "Uncertainty" in tip
    assert any("Test sphere" in w.cmb_ref.itemText(i) and "radiance" in w.cmb_ref.itemText(i)
               for i in range(w.cmb_ref.count()))
    # protocol: written when the report module provides it, else a plain note
    import calibration_report as crp
    if hasattr(crp, "write_sphere_protocol"):
        assert len(w.protocol_paths) == 2 and all(os.path.exists(p) for p in w.protocol_paths)
    else:
        assert not w.protocol_paths and "sphere protocol" in w.protocol_message
    # profiles tab shows the quantity
    pt = dlg.tab_profiles
    qcol = pt.COLS.index("Quantity")
    assert any(pt.table.item(r, qcol).text() == "irradiance"
               for r in range(pt.table.rowCount()))
    dlg.close()


def test_sphere_failed_checks_ask_and_do_not_save_on_no(qapp, demo, boxes, monkeypatch):
    _cc_profile(demo)
    dlg = _dialog(FakeDashboard(demo))
    w = dlg.tab_wizard
    w.cmb_mode.setCurrentIndex(2)
    w.edit_sph_name.setText("Bad sphere")
    w.spn_sph_r.setValue(R_MM)
    w.spn_frames.setValue(8)
    w.cmb_itime_sph.setCurrentIndex(1)
    w.spn_ms_sph.setValue(20.0)
    heights = [0.0, 6.0, 16.0]
    asked: list[float] = []

    def ask_height(index, previous, min_mm):
        h = heights[len(asked)]
        asked.append(h)
        d = csph.port_distance_mm(h)
        demo.source_scale = K_SPHERE * float(csph.disk_factor(R_MM, d))
        return h

    w._ask_height = ask_height
    boxes.answers["Further position?"] = lambda: (Std.Yes if len(asked) < 3 else Std.No)
    boxes.answers["First distance again"] = lambda: Std.Yes
    boxes.answers["Checks failed"] = Std.No
    # the repeat sees another source level: drift check fails
    orig = csph.SphereCharacterization.capture_repeat

    def repeat_with_drift(self):
        demo.source_scale *= 1.2
        return orig(self)
    monkeypatch.setattr(csph.SphereCharacterization, "capture_repeat", repeat_with_drift)
    n_before = len(rs.ReferenceLibrary().list())
    try:
        w.start()
        _wait(qapp, w)
    finally:
        demo.source_scale = 1.0
    assert w.last_error is None, w.last_error
    assert "Checks failed" in boxes.questions
    assert w.last_status == "not_saved"
    assert len(rs.ReferenceLibrary().list()) == n_before
    assert "NOT passed" in w.lbl_result.toPlainText()
    dlg.close()


# ── lamps tab: radiance units ────────────────────────────────────────────

def test_lamp_import_with_radiance_unit(qapp, boxes, lmp_path):
    import gui_calibration as gc
    dlg = _dialog(FakeDashboard())
    imp = gc.LampImportDialog(dlg.tab_lamps, lmp_path) if hasattr(gc, "LampImportDialog") \
        else gc._LampImportDialog(dlg.tab_lamps, lmp_path)
    units = [imp.cmb_unit.itemData(i) for i in range(imp.cmb_unit.count())]
    assert rs.UNIT_UW_CM2_SR_NM in units and rs.UNIT_UW_CM2_NM in units
    assert imp.cmb_optic.findText("objective") >= 0
    imp.cmb_unit.setCurrentIndex(units.index(rs.UNIT_UW_CM2_NM))
    assert imp.lbl_quantity.text() == "irradiance"
    imp.cmb_unit.setCurrentIndex(units.index(rs.UNIT_UW_CM2_SR_NM))
    assert imp.lbl_quantity.text() == "radiance"
    assert "sr" in imp.cmb_unit.currentText()
    assert imp.values()["unit"] == rs.UNIT_UW_CM2_SR_NM
    imp.edit_name.setText("Sphere cert")
    imp.spn_lo.setValue(400.0)
    src = dlg.tab_lamps.do_import(lmp_path, **imp.values())
    assert src is not None and src.quantity == "radiance"
    t = dlg.tab_lamps
    names = [t.table.item(r, 0).text() for r in range(t.table.rowCount())]
    assert t.table.item(names.index("Sphere cert"), t.COLS.index("Quantity")).text() == "radiance"
    # built-in demo source stays an irradiance
    assert t.table.item(0, t.COLS.index("Quantity")).text() == "irradiance"
    assert t.table.item(0, 5).text() == "yes"
    t.table.selectRow(names.index("Sphere cert"))
    qapp.processEvents()
    assert t.plot.getPlotItem().getAxis("left").labelText.startswith("Radiance")
    dlg.close()


# ── colour tab ───────────────────────────────────────────────────────────

def test_color_tab_shows_luminance_for_radiance(qapp, isolated_config):
    from gui_cie_tab import CIETab
    tab = CIETab()
    try:
        _check_color_tab(tab)
    finally:
        tab._stop_tm30_thread()               # no QThread may outlive the widget
        tab.close()


def _check_color_tab(tab):
    wl = np.arange(380.0, 781.0, 1.0)
    spd = np.exp(-0.5 * ((wl - 555.0) / 60.0) ** 2) * 1e-3
    lux_card, fc_card = tab.overview_page.card_lux, tab.overview_page.card_fc
    assert lux_card._lbl.text() == "ILLUMINANCE"

    m = cs.measure_all(wl, spd, 1.0, quantity="radiance")
    tab.update_measurement(wl, spd, 1.0, metrics=m, quantity="radiance")
    assert lux_card._lbl.text() == "LUMINANCE"
    assert "cd/m²" in lux_card._val.text() and "lx" not in lux_card._val.text()
    assert f"{m['luminance']:.4g}" in lux_card._val.text()
    assert fc_card._val.text().startswith("—")
    # report rows carry the quantity
    rows = dict(cs.color_report_rows(wl, spd, 1.0, "radiance"))
    assert "Luminance" in rows and "Illuminance" not in rows

    m2 = cs.measure_all(wl, spd, 1.0)
    tab.update_measurement(wl, spd, 1.0, metrics=m2)
    assert lux_card._lbl.text() == "ILLUMINANCE"
    assert " lx" in lux_card._val.text() and "cd/m²" not in lux_card._val.text()
    assert not fc_card._val.text().startswith("—")

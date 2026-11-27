"""Plausibility (broadband) check, valid segments, protocol location and the
demo-source switch of the calibration wizard."""
from __future__ import annotations

import os
import time

import numpy as np
import pytest

import calibration_profiles as cp
import calibration_workflow as cw
import reference_sources as rs

BACKEND = "Demo (virtual)"


# ── valid segments ───────────────────────────────────────────────────────
def test_valid_segments_split_and_bridge():
    wl = np.arange(400.0, 500.0, 1.0)
    v = np.zeros(wl.size, bool)
    v[0:20] = True          # 400–419
    v[21:40] = True         # gap of one pixel (420) → bridged with 2 nm
    v[60:80] = True         # 460–479, gap 440–459 → separate segment
    assert cp.valid_segments(wl, v, 0.0) == [[400.0, 419.0], [421.0, 439.0],
                                             [460.0, 479.0]]
    assert cp.valid_segments(wl, v, 2.0) == [[400.0, 439.0], [460.0, 479.0]]
    assert cp.valid_segments(wl, np.zeros(5, bool)) == []
    assert "+1 more" in cp.format_segments([[1, 2], [3, 4], [5, 6]], 2)


def _result(wl, S, valid, base=None, gain_span=None):
    S = np.where(valid, S, np.nan)
    gs = gain_span if gain_span is not None else float(
        np.nanmax(1 / S[valid]) / np.nanmin(1 / S[valid]))
    return {"wl": wl, "S": S, "valid": valid, "gain_span": gs,
            "masks": {"base": np.ones(wl.size, bool) if base is None else base}}


def test_broadband_check_passes_smooth_continuum():
    wl = np.linspace(380, 780, 800)
    S = np.exp(-0.5 * ((wl - 600) / 160) ** 2)
    r = cp.broadband_check(_result(wl, S, np.ones(wl.size, bool)))
    assert r["pass"], r["failures"]
    assert r["segments_nm"] == [[380.0, 780.0]]
    assert r["metrics"]["coverage"] == pytest.approx(1.0)


def test_broadband_check_fails_line_spectrum():
    wl = np.linspace(380, 780, 800)
    S = np.zeros(wl.size)
    valid = np.zeros(wl.size, bool)
    for c in (585.2, 594.5, 609.6, 640.2, 703.2, 743.9):      # neon-like lines
        m = np.abs(wl - c) < 1.2
        valid |= m
        S[m] = np.exp(-((wl[m] - c) / 0.6) ** 2)
    S = S / S[valid].max()
    valid &= S > 1 / 50
    r = cp.broadband_check(_result(wl, S, valid))
    assert not r["pass"]
    text = " ".join(r["failures"])
    assert "coverage" in text and "isolated" in text
    assert len(r["segments_nm"]) >= 5
    # the outer range is NOT reported as one valid block
    assert r["metrics"]["largest_segment_frac"] < 0.1


def test_limits_from_config(isolated_config):
    isolated_config._data.update({"calibration_min_coverage": 0.9,
                                  "calibration_max_gain_span": 7.0,
                                  "calibration_max_gap_nm": 4.0})
    lim = cp.PlausibilityLimits.from_config()
    assert (lim.min_coverage, lim.max_gain_span, lim.max_gap_nm) == (0.9, 7.0, 4.0)
    cfg = cw.WorkflowConfig.from_config()
    assert cfg.min_coverage == 0.9 and cfg.plausibility_limits().max_gap_nm == 4.0
    wl = np.linspace(380, 780, 400)
    S = np.linspace(1.0, 0.1, wl.size)                       # gain span 10
    r = cp.broadband_check(_result(wl, S, np.ones(wl.size, bool)), lim)
    # hardware test point 1: the gain span is only a hint, never a failure
    assert r["pass"] and any("gain span" in h for h in r["hints"])
    assert not any("gain span" in f for f in r["failures"])
    assert r["limits"]["max_gain_span"] == 7.0


# ── workflow with the demo backend ───────────────────────────────────────
def _demo(isolated_config, tmp_path, spectrum):
    from _device_demo import DemoSpectrometer
    isolated_config._data.update({
        "demo_spectrum": spectrum, "demo_sensitivity": "synthetic",
        "demo_fullscale_ms": 100.0, "export_dir": str(tmp_path / "exports")})
    b = DemoSpectrometer(on_frame=lambda *a: None)
    b.connect("Demo Spectrometer")
    b.start()
    return b


def _wf(b, kind="relative"):
    return cw.CalibrationWorkflow(
        b, BACKEND, kind=kind, optics={"name": "o", "input": "CC"},
        reference=rs.ReferenceLibrary().get(rs.DEMO_SOURCE_ID),
        cfg=cw.WorkflowConfig(n_frames=6, stability_delay_s=0.05))


def test_line_source_fails_and_is_not_saved_without_override(isolated_config, tmp_path):
    b = _demo(isolated_config, tmp_path, "Neon (Ne)")
    try:
        wf = _wf(b, "absolute")
        rec = wf.run_all(save=False)
        assert wf.plausible is False
        assert wf.checks["broadband_pass"] is False
        assert len(rec["valid_segments_nm"]) > 1
        assert rec["plausibility"]["pass"] is False
        assert rec["settings"]["min_coverage"] == pytest.approx(0.5)   # limits in protocol
        with pytest.raises(cw.PlausibilityError):
            wf.save()
        assert cp.all_profiles() == []
        saved = wf.save(allow_implausible=True)
        assert len(saved) == 2                                   # absolute + relative
        for p in saved:
            assert cp.is_implausible(p)
            assert p["plausibility"]["override"] is True
            assert p["checks"]["broadband_pass"] is False
            assert "NOT PLAUSIBLE" in cp.profile_label(p)
            assert len(p["valid_segments_nm"]) > 1
        rec = wf.record()
        assert rec["plausibility"]["override"] is True
    finally:
        b.disconnect()


def test_broadband_lamp_passes(isolated_config, tmp_path):
    b = _demo(isolated_config, tmp_path, "cal_lamp")
    try:
        wf = _wf(b)
        wf.run_all(save=True)
        assert wf.plausible, wf.plausibility["failures"]
        p = wf.saved[0]
        assert not cp.is_implausible(p)
        assert p["plausibility"]["pass"] is True
        assert len(p["valid_segments_nm"]) == 1
    finally:
        b.disconnect()


def test_report_contains_plausibility(isolated_config, tmp_path):
    import calibration_report as crp
    b = _demo(isolated_config, tmp_path, "Neon (Ne)")
    try:
        wf = _wf(b)
        rec = wf.run_all(save=False)
    finally:
        b.disconnect()
    meta = dict(crp._metadata(rec))
    assert meta["plausibility_pass"] in ("no", "False", "0") or "no" in meta["plausibility_pass"]
    assert meta["valid_segments_nm"].count("-") > 1
    assert "min_coverage_pct" in meta and "max_gain_span" in meta
    out = crp.write_protocol(rec, str(tmp_path / "p.pdf"), str(tmp_path / "p.csv"))
    assert len(out) == 2
    assert "plausibility_failure_1" in open(out[0], encoding="utf-8").read()


# ── protocol location ────────────────────────────────────────────────────
def test_protocol_dir_uses_export_subfolder(isolated_config, tmp_path):
    import export_paths
    isolated_config._data["export_dir"] = str(tmp_path / "exp")
    d, note = export_paths.calibration_protocol_dir()
    assert d == str(tmp_path / "exp" / "calibration") and note is None
    assert os.path.isdir(d)


@pytest.mark.parametrize("export_dir", ["", "INSIDE"])
def test_protocol_dir_never_program_folder(isolated_config, tmp_path, monkeypatch, export_dir):
    import export_paths
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "lad"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "lad"))
    monkeypatch.setattr(export_paths, "user_data_dir",
                        lambda: str(tmp_path / "lad" / "PS-2600A"))
    isolated_config._data["export_dir"] = (
        os.path.join(export_paths.program_dir(), "sub") if export_dir == "INSIDE" else "")
    d, note = export_paths.calibration_protocol_dir()
    assert not export_paths._is_inside(d, export_paths.program_dir())
    # hardware test point 15: protocols are runtime data → <data dir>/protocols
    assert d == str(tmp_path / "lad" / "PS-2600A" / "protocols")
    assert (note is not None) == (export_dir == "INSIDE")


def test_gitignore_has_calib_pattern():
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    with open(os.path.join(root, ".gitignore"), encoding="utf-8") as f:
        assert "calib_*" in [ln.strip() for ln in f]


# ── demo backend: runtime spectrum switch ────────────────────────────────
def test_demo_set_spectrum_roundtrip(isolated_config, tmp_path):
    b = _demo(isolated_config, tmp_path, "Neon (Ne)")
    try:
        before = b._shape.copy()
        prev = b.set_spectrum("cal_lamp")
        assert prev == "Neon (Ne)" and b.spectrum_name == "cal_lamp"
        assert not np.allclose(before, b._shape)
        b.set_spectrum(prev)
        assert np.allclose(before, b._shape)
    finally:
        b.disconnect()


# ── GUI: wizard flow with a line source ──────────────────────────────────
pytest.importorskip("PyQt6")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")


# qapp: the session-wide QApplication from tests/conftest.py


def _run_wizard(qapp, b, answer, monkeypatch, *, ref_id=None, patch_switch=False):
    from PyQt6.QtWidgets import QMessageBox
    import gui_calibration as gc
    from tests.test_gui_calibration import FakeDashboard
    Std = QMessageBox.StandardButton
    monkeypatch.setattr(QMessageBox, "question", staticmethod(lambda *a, **k: Std.Yes))
    for n in ("information", "warning", "critical"):
        monkeypatch.setattr(QMessageBox, n, staticmethod(lambda *a, **k: Std.Ok))
    asked = []
    monkeypatch.setattr(gc._SpectralWizardTab, "_exec_box",
                        lambda self, box: (asked.append(box.text()), answer)[1])
    if patch_switch:     # keep the line source although a demo reference is chosen
        monkeypatch.setattr(gc._SpectralWizardTab, "_apply_demo_spectrum",
                            lambda self, be, src: None)
    dash = FakeDashboard(b)
    dlg = gc.CalibrationDialog(dash)
    w = dlg.tab_wizard
    w.cmb_optics.setEditText("Fibre")
    w.cmb_input.setCurrentText("CC")
    w.cmb_ref.setCurrentIndex(w.cmb_ref.findData(ref_id or rs.DEMO_SOURCE_ID))
    w.spn_frames.setValue(6)
    w.start()
    t0 = time.monotonic()
    while w.running and time.monotonic() - t0 < 120:
        qapp.processEvents()
        time.sleep(0.01)
    assert not w.running
    return dlg, w, asked


def test_wizard_line_source_not_saved(qapp, isolated_config, tmp_path, monkeypatch):
    from PyQt6.QtWidgets import QMessageBox
    isolated_config._data["calibration_stability_delay_s"] = 0.05
    b = _demo(isolated_config, tmp_path, "Neon (Ne)")
    try:
        dlg, w, asked = _run_wizard(qapp, b, QMessageBox.StandardButton.No,
                                    monkeypatch, patch_switch=True)
    finally:
        b.disconnect()
    assert asked, "the override question must be asked"
    assert w.last_status == "failed"
    # step list (hardware test): ... Dark after + checks (6), Compute (7), Save (8)
    assert w.step_states[7][0] == "failed" and w.step_states[8][0] == "failed"
    assert cp.all_profiles() == []
    assert cp.active_selection(BACKEND) in (cp.SEL_NONE, "", None)
    # failed run still documented, in <export_dir>/calibration
    assert w.protocol_paths
    assert all(os.path.dirname(p) == str(tmp_path / "exports" / "calibration")
               for p in w.protocol_paths)


def test_wizard_line_source_override_saves_flagged_not_active(qapp, isolated_config,
                                                              tmp_path, monkeypatch):
    from PyQt6.QtWidgets import QMessageBox
    isolated_config._data["calibration_stability_delay_s"] = 0.05
    b = _demo(isolated_config, tmp_path, "Neon (Ne)")
    try:
        dlg, w, asked = _run_wizard(qapp, b, QMessageBox.StandardButton.Yes,
                                    monkeypatch, patch_switch=True)
    finally:
        b.disconnect()
    assert w.last_status == "saved_implausible", w.last_error
    profs = cp.all_profiles()
    assert len(profs) == 1 and cp.is_implausible(profs[0])
    assert cp.active_selection(BACKEND) != profs[0]["id"]


def test_wizard_switches_demo_spectrum_and_restores(qapp, isolated_config, tmp_path,
                                                    monkeypatch):
    from PyQt6.QtWidgets import QMessageBox
    isolated_config._data["calibration_stability_delay_s"] = 0.05
    b = _demo(isolated_config, tmp_path, "Neon (Ne)")
    try:
        dlg, w, asked = _run_wizard(qapp, b, QMessageBox.StandardButton.No, monkeypatch)
        assert b.spectrum_name == "Neon (Ne)"          # restored after the run
    finally:
        b.disconnect()
    assert not asked, "a matching demo source must pass the plausibility check"
    assert w.last_status == "saved", w.last_error
    assert any("Demo backend" in x for x in w._wf.warnings)

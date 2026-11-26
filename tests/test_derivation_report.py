"""Derivation protocol (PDF + CSV) from DerivationWorkflow.record()."""
from __future__ import annotations

import os
from datetime import datetime

import pytest

pytest.importorskip("matplotlib")

import calibration_report as crp  # noqa: E402
import calibration_workflow as cw  # noqa: E402
import reference_sources as rs  # noqa: E402

BACKEND = "Demo (virtual)"


@pytest.fixture
def record(isolated_config):
    from _device_demo import DemoSpectrometer
    isolated_config._data.update({
        "demo_spectrum": "cal_lamp", "demo_sensitivity": "synthetic",
        "demo_fullscale_ms": 1.0, "demo_min_integration_us": 10,
        "demo_max_integration_us": 2_000_000})
    b = DemoSpectrometer(on_frame=lambda *a: None)
    b.connect("Demo Spectrometer")
    b.set_integration_time_us(800)
    b.start()
    try:
        cfg = cw.WorkflowConfig(n_frames=6, stability_delay_s=0.1)
        wf = cw.CalibrationWorkflow(
            b, BACKEND, kind="relative", optics={"name": "CC", "input": "CC"},
            reference=rs.ReferenceLibrary().get(rs.DEMO_SOURCE_ID), cfg=cfg)
        wf.run_all()
        dw = cw.DerivationWorkflow(
            b, BACKEND, known_profile_id=wf.saved[0]["id"],
            optics={"name": "bare", "input": "bare", "notes": "unit test"}, cfg=cfg)

        def swap(which):
            b.source_scale = 1.0 if which == "new" else 0.97   # drift 3 % > limit
        rec = dw.run_all(integration_known_us=800, integration_new_us=400,
                         swap=swap, save=False)
        yield rec
    finally:
        b.disconnect()


def _header(path):
    meta = {}
    with open(path, encoding="utf-8") as f:
        lines = f.read().splitlines()
    for ln in lines:
        if ln.startswith("# ") and ";" in ln:
            k, v = ln[2:].split(";", 1)
            meta[k] = v
    cols = next(ln for ln in lines if not ln.startswith("#"))
    return meta, cols, [ln for ln in lines if not ln.startswith("#")][1:]


def test_protocol_files_and_csv_content(record, tmp_path):
    base = crp.default_derivation_basename(record)
    assert base.startswith("derivation_") and "bare" in base
    pdf, csv_ = str(tmp_path / (base + ".pdf")), str(tmp_path / (base + ".csv"))
    out = crp.write_derivation_protocol(record, pdf, csv_)
    assert sorted(out) == sorted([pdf, csv_])
    assert os.path.getsize(pdf) > 5000 and open(pdf, "rb").read(5) == b"%PDF-"
    raw = open(csv_, "rb").read()
    raw.decode("utf-8")                                   # valid UTF-8
    text = raw.decode("utf-8")
    assert "nan" not in text.lower()  # no "nan" cells
    meta, cols, rows = _header(csv_)
    assert cols == ("Wavelength_nm;C_known_cps;C_known_repeat_cps;C_known_used_cps;"
                    "C_new_cps;S_known;S_new;Valid;Drift_pct;Drift_fail")
    # the rate the ratio uses and its time-interpolation weight are documented
    assert "# drift_interp_weight;" in text
    assert len(rows) == len(record["spectra"]["wl"])
    assert all(len(r.split(";")) == 10 for r in rows)
    # both integration times, both levels, drift, linearity correction per side
    assert meta["t_int_known_us"] == "800" and meta["t_int_new_us"] == "400"
    assert meta["t_int_known_mode"] == "fixed" and meta["t_int_new_mode"] == "fixed"
    for k in ("peak_known_pct_fs", "peak_new_pct_fs", "peak_known_repeat_pct_fs"):
        assert float(meta[k]) > 0
    assert meta["linearity_correction_known"] in ("on", "off", "n/a") \
        or meta["linearity_correction_known"].startswith(("on", "off", "n/a"))
    assert meta["linearity_correction_new"]
    assert meta["dark_light_same_t_int_known"] == "yes"
    assert float(meta["drift_threshold_pct"]) == record["drift"]["threshold_pct"]
    assert meta["drift_pass"] == "no" and int(meta["drift_masked_pixels"]) > 0
    assert meta["known_profile_id"] == record["known_profile"]["id"]
    assert meta["source_hint"].startswith("Source: a diffuse")
    # time stamps carry the time zone
    assert datetime.fromisoformat(meta["started"]).tzinfo is not None
    assert datetime.fromisoformat(meta["finished"]).tzinfo is not None
    # masked values are empty: S_new blank exactly where Valid is 0
    for r in rows:
        c = r.split(";")
        if c[7] == "0":                  # Valid
            assert c[6] == ""                # S_new
        else:
            assert c[6] != ""


def test_protocol_with_missing_data(tmp_path):
    """No repeat, no spectra, None values: still a readable protocol, no 'nan'."""
    rec = {"kind": "derivation_run", "started": "2026-11-26T10:00:00+01:00",
           "device": {"backend": "X", "serial": None},
           "optics": {"name": "n", "input": "bare"}, "known_profile": {},
           "sides": {"known": {"integration_us": 1000, "peak_frac": None},
                     "new": {"integration_us": None}},
           "drift": None, "dark_drift": [], "warnings": ["w"], "spectra": {
               "wl": [400.0, 401.0], "C_known": [1.0, float("nan")], "valid": [True, False],
               "S": [1.0, None]}}
    pdf, csv_ = str(tmp_path / "a.pdf"), str(tmp_path / "a.csv")
    crp.write_derivation_protocol(rec, pdf, csv_)
    text = open(csv_, encoding="utf-8").read()
    assert "nan" not in text.lower()
    meta, _, rows = _header(csv_)
    assert meta["linearity_correction_known"] == "n/a"
    assert rows[1].split(";")[:2] == ["401.0", ""]
    assert os.path.getsize(pdf) > 1000

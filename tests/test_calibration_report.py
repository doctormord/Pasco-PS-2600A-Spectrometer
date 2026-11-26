"""calibration_report: CSV/PDF from a real demo-backend run record."""
from __future__ import annotations

import re

import pytest

import calibration_report as rep
import calibration_workflow as cw
import reference_sources as rs


@pytest.fixture
def record(isolated_config):
    from _device_demo import DemoSpectrometer
    isolated_config._data.update({"demo_spectrum": "cal_lamp",
                                  "demo_sensitivity": "synthetic",
                                  "demo_fullscale_ms": 100.0})
    b = DemoSpectrometer(on_frame=lambda *a: None)
    b.connect("Demo Spectrometer")
    b.set_integration_time_us(20_000)
    b.start()
    try:
        wf = cw.CalibrationWorkflow(
            b, "Demo (virtual)", kind="relative",
            optics={"name": "Demo fibre", "input": "bare"},
            reference=rs.ReferenceLibrary().get(rs.DEMO_SOURCE_ID),
            cfg=cw.WorkflowConfig(n_frames=8, stability_delay_s=0.1))
        rec = wf.run_all()
    finally:
        b.disconnect()
    return rec


def test_basename_chars(record):
    n = rep.default_protocol_basename(record)
    assert re.fullmatch(r"[A-Za-z0-9_-]+", n) and n.startswith("calib_")
    assert re.fullmatch(r"[A-Za-z0-9_-]+", rep.default_protocol_basename({}))
    odd = {"device": {"backend": "a/b:c ü", "serial": "x y"}, "optics": {"name": "ö.."},
           "type": "relative", "finished": "2026-01-02T03:04:05"}
    n = rep.default_protocol_basename(odd)
    assert re.fullmatch(r"[A-Za-z0-9_-]+", n) and n.endswith("20260102_030405")


def test_csv(record, tmp_path):
    p = rep.write_protocol_csv(record, str(tmp_path / "p.csv"))
    lines = open(p, encoding="utf-8").read().splitlines()
    head = [l for l in lines if l.startswith("# ")]
    keys = {l[2:].split(";")[0] for l in head}
    assert {"started", "device", "optics_name", "t_int_us", "averages_N", "valid_range_nm",
            "stability_pct", "itime_pass", "wl_fingerprint"} <= keys
    body = lines[len(head) + 1:]
    assert lines[len(head) + 1 - 1].split(";") == rep.CSV_COLUMNS
    assert len(body) == len(record["spectra"]["wl"])
    # check_fail column appended (deliberate format change: 8 -> 9 columns)
    assert all(len(l.split(";")) == len(rep.CSV_COLUMNS) == 9 for l in body)


def test_pdf(record, tmp_path):
    p = rep.write_protocol_pdf(record, str(tmp_path / "p.pdf"))
    data = open(p, "rb").read()
    assert data.startswith(b"%PDF") and len(data) > 10_000


def test_write_protocol(record, tmp_path):
    out = rep.write_protocol(record, pdf_path=str(tmp_path / "a.pdf"),
                             csv_path=str(tmp_path / "a.csv"))
    assert len(out) == 2
    assert rep.write_protocol(record) == []


@pytest.mark.parametrize("rec", [
    {}, {"spectra": None, "checks": {"itime_pass": None}},
    {"type": "absolute", "warnings": ["w"], "spectra": {"wl": [400.0, 401.0], "C_cal": [1, None]}},
])
def test_incomplete_record(rec, tmp_path):
    out = rep.write_protocol(rec, str(tmp_path / "x.pdf"), str(tmp_path / "x.csv"))
    assert open(out[1], "rb").read(4) == b"%PDF"
    assert open(out[0], encoding="utf-8").read().startswith("# calibration protocol")

"""Hardware-test point 8: one export implementation for desktop and web, UTF-8
with BOM, header with the settings actually applied, masked pixels empty."""
from __future__ import annotations

import time

import numpy as np
import pytest

import calibration_profiles as cp
import engine as eng

BACKEND = "Demo (virtual)"


@pytest.fixture
def conn(isolated_config):
    e = eng.Engine()
    e.connect(BACKEND)
    t0 = time.time()
    while e.last_frame is None and time.time() - t0 < 10:
        time.sleep(0.02)
    yield e
    e.close()


def _meta(text):
    out = {}
    for line in text.splitlines():
        if line.startswith("# "):
            k, _, v = line[2:].partition(";")
            out[k] = v
    return out


def test_spectrum_csv_header_documents_applied_settings(conn):
    text = conn.export_spectrum_csv()
    m = _meta(text)
    for key in ("Spectrometer", "Serial", "Date/time", "Integration time (ms)",
                "Frames averaged", "Smoothing", "Dark mode (applied)",
                "NL correction (device)", "Linearity correction",
                "Calibration profile", "Unit"):
        assert key in m, key
    assert m["Spectrometer"] == BACKEND and m["Serial"] == "DEMO-0001"
    assert m["Date/time"][-6] in "+-" and m["Date/time"][-3] == ":"      # time zone
    assert m["NL correction (device)"] == "n/a" and m["Linearity correction"] == "off"
    assert m["Calibration profile"] == "None" and m["Unit"] == "counts"
    assert "Response compensation" not in m                # no legacy gain active
    hdr = next(l for l in text.splitlines() if l.startswith("Pixel_ID"))
    assert hdr.startswith("Pixel_ID;Wavelength_nm;ADC_Counts") and "Raw_ADC" in hdr


def test_desktop_and_web_share_one_export(conn, isolated_config):
    from fastapi.testclient import TestClient
    import api
    ev = conn.last_frame
    desk = conn.export_spectrum_csv(ev)
    app = api.create_app(conn, api.ServerSettings())
    with TestClient(app) as c:
        r = c.get("/api/export/csv")
    assert r.status_code == 200 and "charset=utf-8" in r.headers["content-type"]
    assert r.content.startswith(b"\xef\xbb\xbf")                    # BOM for Excel
    web = r.content.decode("utf-8-sig")
    strip = lambda t: [l for l in t.splitlines() if not l.startswith(("# Exported",
                                                                       "# Date/time"))]
    # identical layout and keys (the web exports the newest frame)
    assert [l.split(";")[0] for l in strip(desk)][:20] == [l.split(";")[0] for l in strip(web)][:20]
    import inspect
    import gui_dashboard
    src = inspect.getsource(gui_dashboard.DashboardWindow.export_data_csv)
    assert "export_spectrum_csv" in src and "csv.writer" not in src


def test_write_csv_text_utf8_bom(tmp_path):
    p = eng.write_csv_text(str(tmp_path / "x.csv"), "# Unit;µW/(cm²·nm)\n")
    raw = open(p, "rb").read()
    assert raw.startswith(b"\xef\xbb\xbf") and "µW/(cm²·nm)".encode() in raw


def test_masked_pixels_empty_and_documented(conn):
    wl = conn.wavelength_array
    sel = (wl >= 450) & (wl <= 700)
    table = [[float(w), 1.0] for w in wl[sel]]
    p = cp.add_profile({"type": "relative", "device": {"backend": BACKEND, "serial": "DEMO-0001"},
                        "optics": {"name": "x", "input": "bare"},
                        "reference": {"name": "r", "unit": "relative"},
                        "wl_fingerprint": conn.acquisition.wl_fingerprint(),
                        "valid_range_nm": [450.0, 700.0], "table": table,
                        "unit": "relative", "acquisition": {}, "checks": {},
                        "valid_segments_nm": [[450.0, 700.0]]})
    conn.capture_dark(4)
    conn.select_profile(p["id"])
    t0 = time.time()
    while (conn.last_frame is None or conn.last_frame.unit != "relative") and time.time() - t0 < 10:
        time.sleep(0.02)
    text = conn.export_spectrum_csv()
    m = _meta(text)
    assert m["Profile id"] == p["id"] and m["Profile type"] == "relative"
    assert m["Profile version"] == "1" and m["Valid segments (nm)"].startswith("450.0")
    assert "empty cell" in m["Masked pixels"]
    assert m["Dark mode (applied)"].startswith("stored per-pixel")
    assert "Response compensation" not in m
    rows = [l.split(";") for l in text.splitlines()
            if l and not l.startswith(("#", "Pixel_ID"))]
    first = rows[0]                                           # 340 nm → masked
    assert first[2] == "" and first[-1] != ""                 # empty value, raw present


def test_legacy_response_compensation_only_when_active(conn):
    conn.calib.legacy_gain = np.ones(conn.pixel_count)
    try:
        assert "# Response compensation;on" in conn.export_spectrum_csv()
    finally:
        conn.calib.legacy_gain = None


def test_heatmap_and_measurement_exports(conn):
    t0 = time.time()
    while conn.heatmap_filled < 3 and time.time() - t0 < 10:
        time.sleep(0.02)
    h = conn.export_heatmap_csv([0, 1])
    lines = h.splitlines()
    assert any(l.startswith("# Spectrometer;") for l in lines)
    assert sum(1 for l in lines if l and l[0].isdigit()) == 2
    j = conn.submit_measure({"frames": 2, "dark": "none"}, owner="local")
    t0 = time.time()
    while conn.jobs.get(j.id).status not in eng.JOB_FINAL and time.time() - t0 < 20:
        time.sleep(0.02)
    rec = conn.get_measurement(conn.jobs.get(j.id).result["measurement_id"])
    csv = eng.measurement_to_csv(rec)
    m = _meta(csv)
    assert m["Linearity correction"] == "off" and m["NL correction (device)"] == "n/a"

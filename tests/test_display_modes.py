"""Hardware-test points 9–12 (core): display modes and their normalisation,
availability/fallback, exposure level in ADC, readout formatting, API/MCP."""
from __future__ import annotations

import time

import numpy as np
import pytest

import calibration_profiles as cp
import engine as eng
import processing as pr

BACKEND = "Demo (virtual)"


class _Ap:
    def __init__(self, factor, typ="relative"):
        self.factor = np.asarray(factor, float)
        self.type = typ
        fv = self.factor[np.isfinite(self.factor)]
        self.counts_norm = 1.0 / fv.min()


def test_mode_resolution_and_availability():
    assert pr.effective_display_mode("auto", None) == pr.DISPLAY_RAW
    assert pr.effective_display_mode("auto", None, "counts_per_s") == pr.DISPLAY_RATE
    assert pr.effective_display_mode("auto", "relative") == pr.DISPLAY_CORRECTED
    assert pr.effective_display_mode("corrected", None) == pr.DISPLAY_RAW
    assert pr.effective_display_mode("absolute", "relative") == pr.DISPLAY_CORRECTED
    assert pr.effective_display_mode("absolute", "absolute") == pr.DISPLAY_ABSOLUTE
    assert pr.effective_display_mode("raw", "absolute") == pr.DISPLAY_RAW
    assert pr.DISPLAY_ABSOLUTE not in pr.available_display_modes("relative")


def test_corrected_counts_equal_raw_at_max_sensitivity():
    # rate C, relative factor 1/S with S peak 1 at pixel 2
    S = np.array([0.25, 0.5, 1.0, 0.5, np.nan])
    ap = _Ap(1.0 / S)
    rate = np.array([100.0, 200.0, 400.0, 200.0, 50.0])         # net counts/s
    values = rate * ap.factor                                     # pipeline output
    t_us = 50_000
    v, u = pr.display_transform(pr.DISPLAY_CORRECTED, values=values, unit="relative",
                                integration_us=t_us, calibration=ap)
    assert u == pr.UNIT_COUNTS_CORRECTED
    assert v[2] == pytest.approx(rate[2] * t_us / 1e6)            # = raw net counts
    assert np.isnan(v[4])
    r, ru = pr.display_transform(pr.DISPLAY_RATE, values=values, unit="relative",
                                 integration_us=t_us, calibration=ap)
    assert ru == "counts/s" and r[2] == pytest.approx(400.0)
    n, nu = pr.display_transform(pr.DISPLAY_NORMALISED, values=values, unit="relative",
                                 integration_us=t_us, calibration=ap)
    assert nu == pr.UNIT_NORMALISED and np.nanmax(n) == pytest.approx(1.0)
    # absolute: normalised to the smallest K (max sensitivity)
    K = np.array([4.0, 2.0, 1.0, 2.0, np.nan]) * 1e-3
    apa = _Ap(K, "absolute")
    E = rate * K
    v, _ = pr.display_transform(pr.DISPLAY_CORRECTED, values=E, unit="uW/cm2/nm",
                                integration_us=t_us, calibration=apa)
    assert v[2] == pytest.approx(rate[2] * t_us / 1e6)
    a, au = pr.display_transform(pr.DISPLAY_ABSOLUTE, values=E, unit="uW/cm2/nm",
                                 integration_us=t_us, calibration=apa)
    assert au == "uW/cm2/nm" and np.allclose(a[:4], E[:4])
    raw = np.arange(5.0)
    v, u = pr.display_transform(pr.DISPLAY_RAW, values=E, unit="uW/cm2/nm",
                                integration_us=t_us, calibration=apa, raw_counts=raw)
    assert u == "counts" and np.array_equal(v, raw)


def test_no_profile_modes_keep_legacy_counts():
    v = np.array([10.0, 20.0, 40.0])
    out, u = pr.display_transform(pr.DISPLAY_RAW, values=v, unit="counts", integration_us=10_000)
    assert u == "counts" and out is v or np.array_equal(out, v)
    out, u = pr.display_transform(pr.DISPLAY_RATE, values=v, unit="counts", integration_us=10_000)
    assert u == "counts/s" and out[2] == pytest.approx(4000.0)


def test_readout_and_exposure_level():
    assert pr.format_value(float("nan"), "relative") == "–"
    assert pr.format_value(None, "counts") == "–"
    assert pr.format_value(1234.4, "counts") == "1234 counts"
    assert "µW/(cm²·nm)" in pr.format_value(1.5, "uW/cm2/nm")
    ex = pr.exposure_level(np.array([100.0, 30000.0, 64000.0]), full_scale=65535,
                           saturation_level=60000)
    assert ex["peak_pct_fs"] == pytest.approx(100 * 64000 / 65535)
    assert ex["saturated"] and ex["n_saturated"] == 1


@pytest.fixture
def conn(isolated_config):
    e = eng.Engine()
    e.connect(BACKEND)
    t0 = time.time()
    while e.last_frame is None and time.time() - t0 < 10:
        time.sleep(0.02)
    yield e
    e.close()


def _wait(e, pred, timeout=10.0):
    t0 = time.time()
    while time.time() - t0 < timeout:
        ev = e.last_frame
        if ev is not None and pred(ev):
            return ev
        time.sleep(0.02)
    raise AssertionError("condition not reached")


def test_engine_frames_carry_display_and_evaluations_use_corrected(conn):
    ev = conn.last_frame
    assert ev.display_mode == pr.DISPLAY_RAW and ev.display_unit == "counts"
    assert ev.exposure and ev.exposure["peak_pct_fs"] > 0
    conn.configure({"display_mode": "normalised"})
    ev = _wait(conn, lambda e: e.display_mode == pr.DISPLAY_NORMALISED)
    assert np.nanmax(ev.display) == pytest.approx(1.0)
    assert ev.unit == "counts" and np.nanmax(ev.pixels) > 10       # evaluations unchanged
    # heatmap follows the display
    h = conn.heatmap_snapshot()
    assert np.nanmax(h["buffer"][:, 0]) <= 1.0 + 1e-9
    with pytest.raises(eng.EngineError):
        conn.configure({"display_mode": "bogus"})
    st = conn.status()
    assert st["display_mode"] == "normalised" and st["display_mode_effective"] == "normalised"
    assert {m["value"] for m in st["display_modes"]} >= {"auto", "raw", "corrected"}


def test_engine_corrected_mode_with_profile(conn):
    wl = conn.wavelength_array
    sel = (wl >= 450) & (wl <= 700)
    S = np.exp(-0.5 * ((wl[sel] - 560) / 80) ** 2)
    p = cp.add_profile({"type": "relative", "device": {"backend": BACKEND, "serial": "DEMO-0001"},
                        "optics": {"name": "x", "input": "bare"},
                        "reference": {"name": "r", "unit": "relative"},
                        "wl_fingerprint": conn.acquisition.wl_fingerprint(),
                        "valid_range_nm": [450.0, 700.0],
                        "table": [[float(w), float(s)] for w, s in zip(wl[sel], S)],
                        "unit": "relative", "acquisition": {}, "checks": {}})
    conn.capture_dark(4)
    conn.configure({"display_mode": "auto"})
    conn.select_profile(p["id"])
    ev = _wait(conn, lambda e: e.unit == "relative")
    assert ev.display_mode == pr.DISPLAY_CORRECTED
    i = int(np.nanargmax(np.where(sel, np.interp(wl, wl[sel], S), np.nan)))
    net = ev.raw_averaged[i] - np.asarray(conn.calib.stored_dark(ev.integration_us))[i]
    assert ev.display[i] == pytest.approx(net, rel=0.05)           # Corrected = Raw (net)
    assert np.isnan(ev.display[0])                                  # masked stays masked
    conn.configure({"display_mode": "raw"})
    ev = _wait(conn, lambda e: e.display_mode == pr.DISPLAY_RAW)
    assert np.isfinite(ev.display[0]) and ev.unit == "relative"     # raw shows all pixels
    conn.configure({"display_mode": "absolute"})                    # not available
    ev = _wait(conn, lambda e: e.seq > ev.seq)
    assert ev.display_mode == pr.DISPLAY_CORRECTED


def test_api_and_ws_payload_display(conn):
    from fastapi.testclient import TestClient
    import api
    app = api.create_app(conn, api.ServerSettings())
    with TestClient(app) as c:
        r = c.put("/api/v1/acquisition", json={"display_mode": "count_rate"})
        assert r.status_code == 200, r.text
        assert c.get("/api/v1/acquisition").json()["display_mode"] == "count_rate"
        assert c.put("/api/v1/acquisition", json={"display_mode": "x"}).status_code == 422
    ev = _wait(conn, lambda e: e.display_mode == pr.DISPLAY_RATE)
    p = api.build_frame_payload(conn, ev)
    assert p["display_mode"] == "count_rate" and p["display_unit"] == "counts/s"
    assert len(p["display"]) == len(p["intensities"]) and p["exposure"]["peak_pct_fs"] > 0
    assert p["raw"] and p["display_unit_label"] == "counts/s"


def test_mcp_set_acquisition_accepts_display_mode():
    import inspect
    from mcp_server import server
    src = inspect.getsource(server)
    assert "display_mode=display_mode" in src

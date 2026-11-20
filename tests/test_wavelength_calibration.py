"""WavelengthSession (Qt-free wavelength calibration) and the /api/v1/wavelength
group, against a fake backend with a known polynomial axis."""
from __future__ import annotations

import glob
import json
import os
from types import SimpleNamespace

import numpy as np
import pytest
from fastapi.testclient import TestClient

import api_v1
import calibration_profiles as calprof
import engine as eng
import wavelength_calibration as wlcal
from api import ServerSettings, create_app

N = 1024
V1 = "/api/v1/wavelength"
LAMP = "Mercury-Argon (Hg/Ar)"
SHIFT_NM = 3.0


def true_axis() -> np.ndarray:
    i = np.arange(N, dtype=float)
    return 380.0 + 0.40 * i + 1.0e-5 * i * i


class FakeBackend:
    """Fixed starting axis (true axis + SHIFT_NM); calibrate_from_lines fits a
    polynomial (np.polyfit) and applies it, like the real drivers."""

    def __init__(self):
        self._wl = true_axis() + SHIFT_NM
        self.current_integration_time_us = 20_000
        self.fit_calls = 0

    @property
    def wavelength_array(self):
        return self._wl

    def wl_calibration_snapshot(self):
        return {"_wl": np.asarray(self._wl, float).copy()}

    def wl_calibration_restore(self, snap):
        self._wl = np.asarray(snap["_wl"], float).copy()

    def calibrate_from_lines(self, known, pixel_positions=None, degree=3):
        self.fit_calls += 1
        c = np.polyfit(pixel_positions, known, degree)
        res = np.polyval(c, pixel_positions) - np.asarray(known)
        self._wl = np.polyval(c, np.arange(N, dtype=float))
        return {"coeffs": [float(x) for x in c], "residuals": res.tolist(),
                "max_error": float(np.max(np.abs(res)))}


def lamp_capture(lamp=LAMP):
    """(wl, intensity) as the device would show it: peaks at the true pixel of
    each lamp line, axis still carrying the SHIFT_NM start error."""
    ta = true_axis()
    px = np.arange(N, dtype=float)
    y = np.zeros(N)
    for k in wlcal.lamps()[lamp]:
        if ta[0] < k < ta[-1]:
            p = float(np.interp(k, ta, px))
            y += 1000.0 * np.exp(-0.5 * ((px - p) / 2.0) ** 2)
    return ta + SHIFT_NM, y


def ready_session(be=None, name="PASCO PS-2600A"):
    be = be or FakeBackend()
    s = wlcal.WavelengthSession(be, name)
    s.set_capture(*lamp_capture())
    s.detect_peaks()
    s.match(LAMP)
    return be, s


# ── session logic ─────────────────────────────────────────────────────────
def test_lamps_listing():
    lm = wlcal.lamps()
    assert lm == {k: list(v) for k, v in calprof.CALIBRATION_LINES.items()}
    assert LAMP in lm


def test_match_dedup_and_info():
    be = FakeBackend()
    s = wlcal.WavelengthSession(be, "PASCO PS-2600A")
    s.set_capture(*lamp_capture())
    assert len(s.detect_peaks()) >= 6
    info = s.match(LAMP)
    assert info["n_assigned"] == len(s.pairs) >= 4
    assert info["n_known"] > info["n_assigned"] - 1
    assert info["n_skipped"] == info["n_known"] - info["n_assigned"]
    n0 = len(s.pairs)
    again = s.match(LAMP, accumulate=True)           # same capture: all duplicates
    assert again["n_added"] == 0 and len(s.pairs) == n0
    s.match(LAMP, accumulate=False)                  # replaces
    assert len(s.pairs) == n0
    with pytest.raises(wlcal.WavelengthError):
        s.match("nope")


def test_match_requires_peaks():
    s = wlcal.WavelengthSession(FakeBackend(), "x")
    with pytest.raises(wlcal.WavelengthError):
        s.match(LAMP)
    s.set_capture(np.arange(10.0), np.zeros(10))
    assert not s.has_signal and s.detect_peaks() == []


def test_fit_accepted_recovers_axis():
    be, s = ready_session()
    res = s.fit(3)
    assert res["accepted"] and res["reasons"] == []
    assert res["max_error"] < 0.1 and res["rms"] < 0.1
    np.testing.assert_allclose(res["wavelengths"], true_axis(), atol=0.3)
    np.testing.assert_allclose(be.wavelength_array, res["wavelengths"])
    assert s.last_fit and s.last_fit["coeffs"] == res["coeffs"]
    assert "wavelengths" not in s.last_fit
    np.testing.assert_allclose(s.capture[0], res["wavelengths"])   # re-plot axis
    assert res["line_range"][0] < res["line_range"][1]
    assert res["coverage_warning"] is None


def test_fit_too_few_lines():
    be, s = ready_session()
    s.set_pairs(s.pairs[:3])
    with pytest.raises(wlcal.WavelengthError, match="at least 4"):
        s.fit(3)
    assert be.fit_calls == 0


def test_fit_rejected_wrong_line_rolls_back():
    be, s = ready_session()
    before = be.wavelength_array.copy()
    bad = list(s.pairs)
    px, nm = bad[len(bad) // 2]
    bad[len(bad) // 2] = (px, nm + 40.0)             # mis-assigned line
    s.set_pairs(bad)
    res = s.fit(3)
    assert not res["accepted"]
    assert any("max residual" in r for r in res["reasons"])
    assert res["max_error"] > wlcal.MAX_RESIDUAL_NM
    np.testing.assert_array_equal(be.wavelength_array, before)     # rolled back
    np.testing.assert_array_equal(res["wavelengths"], before)
    assert s.last_fit is None
    with pytest.raises(wlcal.WavelengthError):
        s.save(True)


def test_fit_rejected_implausible_axis():
    be, s = ready_session()
    # lines mapped so that the axis ends far outside the plausible band
    s.set_pairs([(10.0, 1500.0), (300.0, 1510.0), (600.0, 1520.0), (900.0, 1530.0)])
    res = s.fit(3)
    assert not res["accepted"]
    assert any("plausible range" in r for r in res["reasons"])
    np.testing.assert_allclose(be.wavelength_array, true_axis() + SHIFT_NM)


def test_fit_not_supported_device():
    class Virtual(FakeBackend):
        def calibrate_from_lines(self, *a, **k):
            return False
    _, s = ready_session(Virtual())
    with pytest.raises(wlcal.NotSupported):
        s.fit(3)


def test_coverage_warning():
    be, s = ready_session()
    s.set_pairs([p for p in s.pairs if p[1] < 600.0])        # only the blue half
    res = s.fit(2)
    assert res["accepted"]
    assert res["coverage_warning"] and "extrapolated" in res["coverage_warning"]


def test_save_requires_confirm_and_key(isolated_config):
    be, s = ready_session()
    s.fit(3)
    with pytest.raises(wlcal.WavelengthError, match="Confirmation"):
        s.save(False)
    assert not s.saved
    assert not glob.glob(os.path.join(os.path.dirname(isolated_config.path), "old_calib_data_*"))
    be2, s2 = ready_session(name="Demo (virtual)")
    s2.fit(3)
    with pytest.raises(wlcal.WavelengthError, match="No wavelength-coefficient config key"):
        s2.save(True)


def test_save_writes_backup_first_then_config(isolated_config):
    isolated_config.set("pasco_wl_poly_coeffs", [1.0, 2.0, 3.0, 4.0])
    be, s = ready_session()
    res = s.fit(3)
    out = s.save(True)
    assert out["key"] == "pasco_wl_poly_coeffs" and out["extra_note"] == ""
    assert os.path.dirname(out["backup_path"]) == os.path.dirname(os.path.abspath(isolated_config.path))
    with open(out["backup_path"], encoding="utf-8") as f:
        bk = json.load(f)
    assert bk["pasco_wl_poly_coeffs"] == [1.0, 2.0, 3.0, 4.0]      # the OLD value
    assert isolated_config.get("pasco_wl_poly_coeffs") == res["coeffs"]
    assert s.saved
    assert s.discard() is None                                     # saved: no rollback


def test_save_ocean_switches_to_config_axis(isolated_config):
    isolated_config.set("ocean_use_device_wavelength", True)
    be, s = ready_session(name="Ocean HDX-UV-VIS")
    s.fit(3)
    out = s.save(True)
    assert out["key"] == "ocean_wl_poly_coeffs"
    assert "ocean_use_device_wavelength" in out["extra_note"]
    assert isolated_config.get("ocean_use_device_wavelength") is False


def test_discard_rolls_back_unsaved():
    be, s = ready_session()
    start = be.wavelength_array.copy()
    s.fit(3)
    assert not np.allclose(be.wavelength_array, start)
    axis = s.discard()
    np.testing.assert_array_equal(axis, start)
    np.testing.assert_array_equal(be.wavelength_array, start)
    assert s.last_fit is None


def test_backup_failure_aborts_save(isolated_config, monkeypatch):
    be, s = ready_session()
    s.fit(3)
    monkeypatch.setattr(wlcal.json, "dump", lambda *a, **k: (_ for _ in ()).throw(OSError("disk")))
    with pytest.raises(wlcal.WavelengthError, match="Nothing was saved"):
        s.save(True)
    assert isolated_config.get("pasco_wl_poly_coeffs") != s.last_fit["coeffs"]
    assert not s.saved


# ── API ───────────────────────────────────────────────────────────────────
@pytest.fixture
def api(isolated_config, monkeypatch):
    monkeypatch.setattr(api_v1, "GROUPS", tuple(dict.fromkeys(api_v1.GROUPS + ("api_v1_wavelength",))))
    e = eng.Engine()
    be = FakeBackend()
    e.pixel_count = N
    e.wavelength_array = be.wavelength_array.copy()
    e.acquisition = be
    e.connected_device_path = "fake"
    e.connected_backend_name = "PASCO PS-2600A"
    wl, y = lamp_capture()

    def fake_capture(ctx, n, t_us, kind="light"):
        cap = SimpleNamespace(mean=y + 100.0, wl=be.wavelength_array.copy(), n_frames=n,
                              integration_us=t_us, n_saturated=0)
        return cap, 100.0
    monkeypatch.setattr(e, "_capture", fake_capture)
    try:
        with TestClient(create_app(e, ServerSettings())) as c:
            c.engine, c.backend = e, be
            yield c
    finally:
        e.acquisition = None
        e.close()


def err(r, status, code):
    assert r.status_code == status, r.text
    assert r.json()["error"]["code"] == code, r.text
    return r.json()["error"]


def run_to_fit(c):
    assert c.post(f"{V1}/session").status_code == 200
    r = c.post(f"{V1}/capture", json={"frames": 4})
    assert r.status_code == 200, r.text
    r = c.post(f"{V1}/match", json={"lamp": LAMP})
    assert r.status_code == 200, r.text
    return r.json()


def test_api_lamps(api):
    r = api.get(f"{V1}/lamps")
    assert r.status_code == 200
    names = [x["name"] for x in r.json()["lamps"]]
    assert LAMP in names


def test_api_no_device_and_no_session(api):
    api.engine.acquisition = None
    err(api.post(f"{V1}/session"), 503, "not_connected")
    api.engine.acquisition = api.backend
    assert api.get(f"{V1}/session").json() == {"active": False}
    err(api.post(f"{V1}/capture", json={}), 409, "conflict")
    err(api.post(f"{V1}/fit", json={}), 409, "conflict")


def test_api_full_flow(api, isolated_config):
    start_axis = api.engine.wavelength_array.copy()
    info = run_to_fit(api)
    assert info["n_assigned"] >= 4 and len(info["pairs"]) == info["n_total"]
    r = api.post(f"{V1}/fit", json={"degree": 3})
    assert r.status_code == 200, r.text
    fit = r.json()
    assert fit["accepted"] and fit["max_error"] < 0.1
    assert "wavelengths" not in fit
    # live preview adopted by the engine
    np.testing.assert_allclose(api.engine.wavelength_array, true_axis(), atol=0.3)
    assert not np.allclose(api.engine.wavelength_array, start_axis)
    st = api.get(f"{V1}/session").json()
    assert st["active"] and st["last_fit"]["accepted"] and not st["saved"]
    r = api.post(f"{V1}/save", json={"confirm": True})
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["key"] == "pasco_wl_poly_coeffs" and os.path.isfile(out["backup_path"])
    assert isolated_config.get("pasco_wl_poly_coeffs") == fit["coeffs"]
    assert api.get(f"{V1}/session").json()["saved"] is True
    # saved: closing keeps the calibration
    r = api.delete(f"{V1}/session")
    assert r.json()["rolled_back"] is False
    np.testing.assert_allclose(api.engine.wavelength_array, true_axis(), atol=0.3)


def test_api_capture_preview_and_peaks(api):
    api.post(f"{V1}/session")
    r = api.post(f"{V1}/capture", json={"frames": 8, "integration_ms": 30})
    assert r.status_code == 200, r.text
    c = r.json()
    assert len(c["preview"]["wavelengths"]) <= 600
    assert len(c["preview"]["values"]) == len(c["preview"]["wavelengths"])
    assert c["has_signal"] and c["n_peaks"] == len(c["peaks"]) >= 6
    assert c["integration_ms"] == 30.0 and c["dark"] == "scalar"
    assert max(c["preview"]["values"]) > 500          # bin-max keeps the lines
    assert {"pixel", "wavelength", "height"} <= set(c["peaks"][0])
    assert min(c["preview"]["values"]) >= 0.0


def test_api_capture_per_pixel_dark(api, monkeypatch):
    api.post(f"{V1}/session")
    dark = np.full(N, 100.0)
    monkeypatch.setattr(api.engine.calib, "dark_for", lambda us: (dark, "stored"))
    c = api.post(f"{V1}/capture", json={"frames": 2}).json()
    assert c["dark"] == "per_pixel"


def test_api_pairs_and_rejected_fit(api):
    run_to_fit(api)
    pairs = api.get(f"{V1}/session").json()["pairs"]
    bad = [list(p) for p in pairs]
    bad[len(bad) // 2][1] += 40.0
    r = api.put(f"{V1}/pairs", json={"pairs": bad})
    assert r.status_code == 200 and r.json()["n_total"] == len(bad)
    before = api.engine.wavelength_array.copy()
    e = err(api.post(f"{V1}/fit", json={"degree": 3}), 422, "fit_rejected")
    assert e["details"]["accepted"] is False and e["details"]["reasons"]
    np.testing.assert_array_equal(api.engine.wavelength_array, before)
    np.testing.assert_array_equal(api.backend.wavelength_array, before)
    # save without an accepted fit
    err(api.post(f"{V1}/save", json={"confirm": True}), 422, "unprocessable")
    err(api.put(f"{V1}/pairs", json={"pairs": [[1.0]]}), 422, "unprocessable")


def test_api_fit_too_few_lines(api):
    run_to_fit(api)
    api.put(f"{V1}/pairs", json={"pairs": [[10, 400], [20, 410]]})
    e = err(api.post(f"{V1}/fit", json={"degree": 3}), 422, "unprocessable")
    assert "at least 4" in e["message"]


def test_api_save_needs_confirm(api, isolated_config):
    run_to_fit(api)
    api.post(f"{V1}/fit", json={})
    err(api.post(f"{V1}/save", json={}), 422, "unprocessable")
    err(api.post(f"{V1}/save", json={"confirm": False}), 422, "unprocessable")
    assert isolated_config.get("pasco_wl_poly_coeffs") != api.get(f"{V1}/session").json()["last_fit"]["coeffs"]


def test_api_delete_rolls_back_live_axis(api):
    start = api.engine.wavelength_array.copy()
    run_to_fit(api)
    assert api.post(f"{V1}/fit", json={}).status_code == 200
    assert not np.allclose(api.engine.wavelength_array, start)
    r = api.delete(f"{V1}/session")
    assert r.status_code == 200 and r.json()["rolled_back"] is True
    np.testing.assert_allclose(api.engine.wavelength_array, start)
    np.testing.assert_allclose(api.backend.wavelength_array, start)
    assert api.get(f"{V1}/session").json() == {"active": False}


def test_api_fit_on_virtual_device(api):
    api.backend.calibrate_from_lines = lambda *a, **k: False
    run_to_fit(api)
    err(api.post(f"{V1}/fit", json={}), 422, "wavelength_not_supported")


def test_api_control_lease_blocks_other_client(api):
    r = api.post("/api/v1/control", json={"action": "acquire"}, headers={"X-Client-Id": "a"})
    assert r.status_code == 200, r.text
    h = {"X-Client-Id": "b"}
    err(api.post(f"{V1}/session", headers=h), 409, "control_busy")
    assert api.post(f"{V1}/session", headers={"X-Client-Id": "a"}).status_code == 200
    err(api.post(f"{V1}/capture", json={}, headers=h), 409, "control_busy")
    err(api.post(f"{V1}/save", json={"confirm": True}, headers=h), 409, "control_busy")
    err(api.delete(f"{V1}/session", headers=h), 409, "control_busy")


def test_api_stale_session_after_device_change(api):
    api.post(f"{V1}/session")
    api.engine.acquisition = FakeBackend()
    assert api.get(f"{V1}/session").json() == {"active": False}


def test_api_demo_device_rejects_fit(isolated_config):
    """The Demo backend has a fixed axis: a session starts but fitting is refused."""
    import time
    e = eng.Engine()
    try:
        with TestClient(create_app(e, ServerSettings())) as c:
            r = c.post("/api/v1/connect", json={"backend": "Demo (virtual)"})
            assert r.status_code == 200, r.text
            t0 = time.monotonic()
            while e.last_frame is None and time.monotonic() - t0 < 15:
                time.sleep(0.05)
            assert c.post(f"{V1}/session").status_code == 200
            c.put(f"{V1}/pairs", json={"pairs": [[10, 400], [200, 450], [500, 500], [900, 600]]})
            err(c.post(f"{V1}/fit", json={"degree": 3}), 422, "wavelength_not_supported")
    finally:
        e.close()

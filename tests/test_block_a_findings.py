"""Findings 21-23: legacy profiles masked outside their range, configurable
test limits (same-t_int tolerance, dark-bright factor), unambiguous drift status."""
from __future__ import annotations

import os
import sys

import numpy as np
import pytest

import calibration_capture as cc
import calibration_profiles as cp
import calibration_report as crp
import calibration_workflow as cw
import processing
from calibration_utils import build_response_gain

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

DEV = "Synthetic dev"


# -- Point 21 -------------------------------------------------------------

def _legacy(isolated_config, lo=450.0, hi=700.0):
    wl_t = np.linspace(400, 800, 41)
    tab = np.column_stack([wl_t, 0.2 + 0.8 * np.exp(-0.5 * ((wl_t - 600) / 80) ** 2)])
    p = cp.save_profile(DEV, "old", tab, {"wl_min_nm": lo, "wl_max_nm": hi})
    return p, tab


def test_legacy_profile_nan_outside_identical_inside(isolated_config):
    p, tab = _legacy(isolated_config)
    wl = np.linspace(340, 850, 1024)
    gain, eff = cp.build_gain_for_selection(wl, DEV, p["id"])
    ref = build_response_gain(wl, tab)
    inside = (wl >= 450) & (wl <= 700)
    assert eff == p["id"]
    assert np.array_equal(gain[inside], ref[inside])
    assert np.isnan(gain[~inside]).all() and not np.isnan(gain[inside]).any()


def test_legacy_open_bounds_use_table_edge(isolated_config):
    p, tab = _legacy(isolated_config)
    reg = cp.load_registry()
    for q in reg["profiles"]:
        if q["id"] == p["id"]:
            q["valid_range_nm"] = [None, None]
    cp.save_registry(reg)
    wl = np.linspace(340, 850, 512)
    gain, _ = cp.build_gain_for_selection(wl, DEV, p["id"])
    out = (wl < 400) | (wl > 800)
    assert np.isnan(gain[out]).all() and np.isfinite(gain[~out]).all()


def test_device_default_unchanged(isolated_config):
    wl = np.linspace(340, 850, 256)
    tab = np.column_stack([np.linspace(400, 800, 9), np.linspace(1, 0.5, 9)])
    gain, _ = cp.build_gain_for_selection(wl, DEV, cp.SEL_DEVICE_DEFAULT, tab)
    assert np.isfinite(gain).all()
    assert np.array_equal(gain, build_response_gain(wl, tab))


def test_runtime_legacy_note_and_process_frame(isolated_config):
    p, _ = _legacy(isolated_config)
    cp.set_active_selection(DEV, p["id"])
    wl = np.linspace(340, 850, 512)
    rt = cp.CalibrationRuntime()
    rt.backend_name, rt.wl = DEV, wl
    rt.refresh()
    st = rt.status
    assert st["legacy_note"] == ("Migrated legacy profile: no calibration outside "
                                 "450–700 nm (masked).")
    assert st["legacy_note"] in st["message"]
    assert st["valid_range_nm"] == [450.0, 700.0]
    pix = np.full(wl.size, 1000.0)
    kw = dict(wavelengths=wl, response_gain=rt.legacy_gain, ob_slope=1.0, ob_offset=0.0,
              response_compensation=True, peak_detection=True)
    out = processing.process_frame(pix, 0.0, 10000, **kw)
    arr = out["pixels"]
    inside = (wl >= 450) & (wl <= 700)
    assert np.isnan(arr[~inside]).all() and np.isfinite(arr[inside]).all()
    # without a profile the result is unchanged
    base = processing.process_frame(pix, 0.0, 10000, wavelengths=wl,
                                    response_gain=np.ones(wl.size), ob_slope=1.0,
                                    ob_offset=0.0)
    assert np.array_equal(base["pixels"], pix)


# -- Point 22 -------------------------------------------------------------

def test_config_defaults_and_itime_tolerance(isolated_config):
    assert isolated_config.get("calibration_itime_match_tol_pct") == 0.5
    assert isolated_config.get("calibration_dark_bright_frac") == 0.5
    assert cc.itime_rel_tol() == pytest.approx(0.005)
    isolated_config._data["calibration_itime_match_tol_pct"] = 0.0
    assert cc.itime_rel_tol() == 0.0
    isolated_config._data["calibration_itime_match_tol_pct"] = 2.0
    assert cc.itime_rel_tol() == pytest.approx(0.02)
    cfg = cw.WorkflowConfig.from_config()
    assert cfg.itime_match_tol_pct == 2.0 and cfg.dark_bright_frac == 0.5


def _cap(t, kind="light", peak=0.5):
    n = 16
    mean = np.full(n, peak * 65535.0)
    return cc.Capture(wl=np.linspace(400, 800, n), mean=mean, std=np.ones(n), n_frames=4,
                      integration_us=int(t), saturated=np.zeros(n, bool),
                      saturation_level=65535.0, full_scale=65535.0, backend="S",
                      serial="X", wl_fingerprint="fp", kind=kind)


def test_itime_tolerance_decides(isolated_config):
    light, dark = _cap(10000), _cap(10040, "dark", 0.1)       # 0.4 % apart
    assert not cc.count_rate(light, dark).warnings              # default 0.5 %: ok
    isolated_config._data["calibration_itime_match_tol_pct"] = 0.0
    assert any("different" in w or "same integration" in w
               for w in cc.count_rate(light, dark).warnings)


def test_same_t_uses_workflow_config():
    for tol, ok in ((0.0, False), (1.0, True)):
        wf = cw.DerivationWorkflow.__new__(cw.DerivationWorkflow)
        wf.cfg = cw.WorkflowConfig(itime_match_tol_pct=tol)
        assert wf._same_t(10000, 10050) is ok


def test_limits_in_settings_and_csv_rows():
    rec = {"settings": {"itime_match_tol_pct": 0.25, "dark_bright_frac": 0.4},
           "started": "2026-01-01T10:00:00+01:00"}
    rows = dict(crp._limit_rows(rec))
    assert rows == {"itime_match_tol_pct": "0.25", "dark_bright_frac": "0.4"}
    txt = crp._limit_text(rec)
    assert "0.25" in txt and "0.4" in txt
    drec = dict(rec, drift_status={
        "message": "Drift check failed: 5.00 % (limit 2.00 %), mean -5.00 %"})
    meta = dict(crp._deriv_metadata(drec))
    assert meta["itime_match_tol_pct"] == "0.25" and meta["dark_bright_frac"] == "0.4"
    assert meta["drift_status"].startswith("Drift check failed: 5.00 %")


# -- Point 23 -------------------------------------------------------------

def test_drift_status_object():
    dw = cw.DerivationWorkflow.__new__(cw.DerivationWorkflow)
    dw.cfg = cw.WorkflowConfig()
    dw.checks = {}
    dw.drift = None
    assert dw.drift_failed is False and dw.drift_status() is None
    dw.drift = {"p95_pct": 5.123, "mean_pct": -4.9, "threshold_pct": 2.0}
    dw.checks = {"drift_pass": False}
    ds = dw.drift_status()
    assert dw.drift_failed and ds["status"] == "drift_check_failed"
    assert ds["message"] == "Drift check failed: 5.12 % (limit 2.00 %), mean -4.90 %"
    assert ds["limit_pct"] == 2.0 and ds["p95_pct"] == 5.123


def test_submit_derivation_reports_drift_first(isolated_config):
    pytest.importorskip("fastapi")
    pytest.importorskip("httpx")
    import test_api_derivation as t
    from fastapi.testclient import TestClient
    import engine as eng
    from api import ServerSettings, create_app
    isolated_config._data.update({
        "calibration_capture_frames": 4, "calibration_stability_delay_s": 0.1,
        "demo_spectrum": "cal_lamp", "demo_sensitivity": "synthetic",
        "demo_fullscale_ms": 1.0, "demo_min_integration_us": 10,
        "demo_max_integration_us": 2_000_000, "demo_read_noise": 2.0})
    e = eng.Engine()
    try:
        with TestClient(create_app(e, ServerSettings())) as client:
            client.engine = e
            assert client.post(f"{t.V1}/connect",
                               json={"backend": t.BACKEND}).status_code == 200
            t._wait(lambda: e.last_frame is not None, what="first frame")
            known = t._known_profile(e)
            # strong drift: the repeat is 10 % darker; the masked range may also
            # fail the plausibility check, the drift is reported first
            jid, j, _, _ = t._run(client, t._body(known), back_scale=0.9)
            res = j["result"]
            assert res["status"] == "drift_check_failed", res
            assert res["profile_ids"] == []
            ds = res["drift_status"]
            assert ds["status"] == "drift_check_failed" and ds["p95_pct"] > ds["limit_pct"]
            assert ds["message"].startswith("Drift check failed: ")
            assert res["drift"]["pass"] is False
            assert res["protocol"], res.get("protocol_message")
    finally:
        e.close()

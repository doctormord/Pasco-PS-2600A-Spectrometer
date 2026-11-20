"""Profiles record acquisition.nl_correction; applying one after the detector
linearisation was switched requires confirmation (like a wavelength change)."""
from __future__ import annotations

import numpy as np
import pytest

import calibration_profiles as cp
import calibration_workflow as cw
import reference_sources as rs

BACKEND = "Demo (virtual)"


@pytest.fixture
def demo(isolated_config, monkeypatch):
    from _device_demo import DemoSpectrometer
    isolated_config._data.update({"demo_spectrum": "cal_lamp",
                                  "demo_sensitivity": "synthetic",
                                  "demo_fullscale_ms": 100.0,
                                  "selected_backend": BACKEND})
    # emulate a device with a switchable linearisation (Ocean-like)
    state = {"nl": True}
    monkeypatch.setattr(DemoSpectrometer, "nl_correction_active",
                        property(lambda self: state["nl"]), raising=False)
    b = DemoSpectrometer(on_frame=lambda *a: None)
    b.connect("Demo Spectrometer")
    b.set_integration_time_us(20_000)
    b.start()
    b._nl_state = state
    yield b
    b.disconnect()


def _run(demo, kind="relative", optic="bare"):
    wf = cw.CalibrationWorkflow(demo, BACKEND, kind=kind,
                                optics={"name": "F", "input": optic},
                                reference=rs.ReferenceLibrary().get(rs.DEMO_SOURCE_ID),
                                cfg=cw.WorkflowConfig(n_frames=6, stability_delay_s=0.05))
    wf.run_all()
    cp.set_active_selection(BACKEND, wf.saved[0]["id"])
    return wf.saved[0]


def _rt(demo):
    rt = cp.CalibrationRuntime()
    rt.configure(BACKEND, demo, demo.wavelength_array)
    return rt


def test_profile_records_nl_and_status_detects_switch(demo):
    p = _run(demo)
    assert p["acquisition"]["nl_correction"] is True
    fp = demo.wl_fingerprint()
    assert cp.profile_status(p, BACKEND, demo.serial, fp, True)["state"] == cp.STATE_OK
    st = cp.profile_status(p, BACKEND, demo.serial, fp, False)
    assert st["state"] == cp.STATE_NL_CHANGED and st["nl_changed"] and not st["wl_changed"]
    assert "linearisation was ON" in st["message"] and "is OFF now" in st["message"]
    # device without linearisation info → not checked
    assert cp.profile_status(p, BACKEND, demo.serial, fp, None)["state"] == cp.STATE_OK
    # both changed → wl_changed state, both causes named
    st2 = cp.profile_status(p, BACKEND, demo.serial, "0" * 16, False)
    assert st2["state"] == cp.STATE_WL_CHANGED and st2["nl_changed"]


def test_runtime_withholds_until_confirmed_for_exactly_this_state(demo):
    p = _run(demo)
    assert _rt(demo).active_v2                           # same state → applied
    demo._nl_state["nl"] = False                         # user switched NL off
    rt = _rt(demo)
    assert rt.status["state"] == cp.STATE_NL_CHANGED
    assert rt.applier is None and rt.status.get("needs_confirmation")
    cp.confirm_override(p["id"], demo)
    rt = _rt(demo)
    assert rt.active_v2
    # back to the calibrated state → ok again without any confirmation
    demo._nl_state["nl"] = True
    rt = _rt(demo)
    assert rt.status["state"] == cp.STATE_OK and rt.active_v2
    # a confirmation for (fp, NL off) does not cover (fp, NL on) mismatches
    assert not cp.fingerprint_override_ok(p["id"], demo.wl_fingerprint(), True)


def test_newest_valid_version_is_default(demo):
    p1 = _run(demo)                                      # NL on
    demo._nl_state["nl"] = False
    p2 = _run(demo)                                      # NL off (newer)
    fp = demo.wl_fingerprint()
    pick = lambda nl: [q["id"] for q in cp.profiles_for_device(BACKEND, demo.serial, fp,
                                                                nl_active=nl)]
    assert pick(False) == [p2["id"]]
    assert pick(True) == [p1["id"]]                      # older but valid wins


def test_derivation_refuses_known_profile_with_other_nl(demo):
    p = _run(demo, kind="relative", optic="CC")
    demo._nl_state["nl"] = False
    ok, why = cw.derivation_source_ok(p, demo, BACKEND)
    assert not ok and "linearisation" in why


def test_web_select_needs_confirmation_on_nl_change(demo):
    from fastapi.testclient import TestClient
    import web_server as ws
    p = _run(demo)
    demo._nl_state["nl"] = False
    ws.state.acquisition = demo
    ws.state.connected_backend_name = BACKEND
    ws.state.calib.configure(BACKEND, demo, demo.wavelength_array)
    try:
        c = TestClient(ws.app)
        r = c.post("/api/calibration/select", json={"selection": p["id"]})
        assert r.status_code == 409 and r.json()["state"] == cp.STATE_NL_CHANGED
        r = c.post("/api/calibration/select", json={"selection": p["id"], "confirm": True})
        assert r.status_code == 200
        assert ws.state.calib.active_v2
    finally:
        ws.state.acquisition = None
        ws.state.connected_backend_name = None
        ws.state.calib = cp.CalibrationRuntime()

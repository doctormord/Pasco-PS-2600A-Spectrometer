"""Engine (Qt-free core) against the Demo backend: life cycle, pipeline identity,
configure(), measurement / series / cancel jobs, control ownership, user
steps, calibration job, connection loss."""
from __future__ import annotations

import re
import time

import numpy as np
import pytest

import engine as eng
import processing
from _device_pasco import PASCO_DARK_BIAS_ADC, PASCO_DARK_RATE_ADC_PER_SEC
from app_config import Config

BACKEND = "Demo (virtual)"


def wait_for(pred, timeout=10.0, step=0.02, what="condition"):
    t0 = time.monotonic()
    while time.monotonic() - t0 < timeout:
        v = pred()
        if v:
            return v
        time.sleep(step)
    raise AssertionError(f"timeout waiting for {what}")


def wait_job(e, job, timeout=30.0):
    """Wait until the job is final (never stops at waiting_for_user)."""
    e.jobs.wait(job.id, timeout=timeout, until_waiting=False)
    assert job.status in eng.JOB_FINAL, f"job stuck in {job.status}: {job.message}"
    return job


@pytest.fixture
def engine(isolated_config):
    isolated_config._data.update({
        "calibration_capture_frames": 4,
        "calibration_stability_delay_s": 0.1,
    })
    e = eng.Engine()
    try:
        yield e
    finally:
        e.close()


@pytest.fixture
def conn(engine):
    engine.connect(BACKEND, owner="local")
    wait_for(lambda: engine.last_frame is not None, what="first frame")
    return engine


@pytest.fixture
def events(engine):
    evs: list = []
    engine.subscribe(evs.append)
    return evs


# ── 1. connect / disconnect ──────────────────────────────────────────────

def test_connect_disconnect(engine, events):
    assert engine.status()["connected"] is False
    info = engine.connect(BACKEND, owner="local")
    assert info["connected"] and info["backend"] == BACKEND
    assert engine.status()["connected"] is True
    conn_ev = [e.data for e in events if e.kind == "connection"]
    assert conn_ev and conn_ev[0]["connected"] is True and conn_ev[0]["reason"] == "connect"

    f1 = wait_for(lambda: engine.last_frame, what="frame")
    wait_for(lambda: engine.last_frame.seq > f1.seq, what="seq increase")
    assert any(e.kind == "frame" for e in events)
    assert engine.status()["pixel_count"] == f1.pixels.size > 0

    engine.disconnect(owner="local")
    assert engine.status()["connected"] is False
    assert engine.pixel_count == eng.DEFAULT_PIXEL_COUNT
    assert engine.current_unit == "counts"
    assert engine.calib.active_v2 is None or not engine.calib.active_v2
    conn_ev = [e.data for e in events if e.kind == "connection"]
    assert conn_ev[-1]["connected"] is False


# ── 2. pipeline identity ─────────────────────────────────────────────────

def test_pipeline_identity(conn):
    e = conn
    e.configure({"dark_correction": True, "hot_pixel_filter": True, "frames_to_average": 1},
                owner="local")
    e.disconnect(owner="local")          # stop the live stream: deterministic state
    e.connect(BACKEND, owner="local")
    wait_for(lambda: e.last_frame is not None, what="frame")
    wl = e.wavelength_array
    rng = np.random.default_rng(1)
    px = 2000.0 + 500.0 * np.sin(np.linspace(0, 6, wl.size)) + rng.normal(0, 5, wl.size)
    px[100] = 60000.0                        # hot pixel
    t_us = 20000
    e.spectrum_history.clear()
    ev = e.ingest_frame(px, 700.0, t_us, notify=False)
    kw = processing.pipeline_kwargs(
        Config.get, e.calib, integration_us=t_us, ob_slope=e.ob_slope,
        ob_offset=e.ob_offset, default_bias=PASCO_DARK_BIAS_ADC,
        default_rate=PASCO_DARK_RATE_ADC_PER_SEC)
    ref = processing.process_frame(px, 700.0, t_us, wavelengths=wl, **kw)
    np.testing.assert_array_equal(ev.pixels, ref["pixels"])
    assert ev.unit == ref["unit"]


# ── 3. configure ─────────────────────────────────────────────────────────

def test_configure(conn, events):
    e = conn
    lo, hi = e.int_bounds_us()
    eff = e.configure({"exposure_ms": 1e6}, owner="local")
    assert eff["exposure_ms"] == hi / 1000.0
    assert e.acquisition.current_integration_time_us == hi
    eff = e.configure({"exposure_ms": 1e-6}, owner="local")
    assert eff["exposure_ms"] == lo / 1000.0
    assert e.acquisition.current_integration_time_us == lo
    assert e.status()["exposure_ms"] == lo / 1000.0

    # manual exposure switches auto exposure off
    e.configure({"auto_exposure": True}, owner="local")
    assert e.acquisition.is_auto_exposure_active is True
    eff = e.configure({"exposure_ms": 30.0}, owner="local")
    assert eff["auto_exposure"] is False
    assert e.acquisition.is_auto_exposure_active is False
    assert e.status()["auto_exposure"] is False

    # averaging
    eff = e.configure({"frames_to_average": 4}, owner="local")
    assert eff["frames_to_average"] == 4 and e.frames_avg == 4
    assert e.spectrum_history.maxlen == 4
    assert e.status()["frames_to_average"] == 4
    assert e.configure({"frames_to_average": 0}, owner="local")["frames_to_average"] == 1

    cfg_ev = [x.data for x in events if x.kind == "config"]
    assert any(d.get("exposure_ms") == 30.0 and d.get("auto_exposure") is False for d in cfg_ev)
    assert any(d.get("frames_to_average") == 4 for d in cfg_ev)


# ── 4. single measurement ────────────────────────────────────────────────

def test_measure_and_dark_modes(conn):
    e = conn
    j = wait_job(e, e.submit_measure({"frames": 3, "dark": "none"}, owner="local"))
    assert j.status == eng.JOB_DONE, j.error
    rec = e.get_measurement(j.result["measurement_id"])
    assert re.search(r"[+-]\d\d:\d\d$", rec["timestamp"])
    assert rec["device"]["serial"] == e.acquisition.serial
    assert rec["acquisition"]["frames"] == 3
    assert rec["unit"] == "counts"
    csv = eng.measurement_to_csv(rec)
    assert "# Measurement id;" in csv and "# Unit;counts" in csv
    lines = csv.splitlines()
    hdr = lines.index("Pixel_ID;Wavelength_nm;ADC_Counts")
    assert len(lines) - hdr - 1 == rec["wavelengths"].size
    js = eng.measurement_to_json(rec)
    assert js["id"] == rec["id"] and len(js["values"]) == rec["wavelengths"].size

    # stored dark without stored spectrum
    j = wait_job(e, e.submit_measure({"frames": 2, "dark": "stored"}, owner="local"))
    assert j.status == eng.JOB_FAILED and j.error["code"] == "unprocessable"

    # capture dark, then stored works
    j = wait_job(e, e.submit_measure({"frames": 2, "dark": "capture"}, owner="local"))
    assert j.status == eng.JOB_DONE, j.error
    j = wait_job(e, e.submit_measure({"frames": 2, "dark": "stored"}, owner="local"))
    assert j.status == eng.JOB_DONE, j.error
    assert e.get_measurement(j.result["measurement_id"])["acquisition"]["dark"] == "stored"

    with pytest.raises(eng.NotFound):
        e.get_measurement("nope")


def test_submit_requires_connection(engine):
    with pytest.raises(eng.NotConnected):
        engine.submit_measure({}, owner="local")


# ── 5. series ────────────────────────────────────────────────────────────

def test_series(conn):
    e = conn
    j = wait_job(e, e.submit_series({"count": 3, "interval_s": 0.2, "frames": 1,
                                     "dark": "none"}, owner="local"))
    assert j.status == eng.JOB_DONE, j.error
    assert len(j.result["measurement_ids"]) == 3
    assert len(j.result["summary"]) == 3
    assert j.progress == 1.0
    for mid in j.result["measurement_ids"]:
        e.get_measurement(mid)
    with pytest.raises(eng.Unprocessable):
        e.submit_series({"count": 0}, owner="local")


# ── 6. cancel ────────────────────────────────────────────────────────────

def test_cancel_series(conn):
    e = conn
    j = e.submit_series({"count": 50, "interval_s": 0.5, "frames": 1, "dark": "none"},
                        owner="local")
    wait_for(lambda: len(e.measurements) >= 1, timeout=20, what="first measurement")
    e.jobs.cancel(j.id)
    wait_job(e, j, timeout=10)
    assert j.status == eng.JOB_CANCELLED
    wait_for(lambda: e.control_info()["owner"] is None, what="control released")


# ── 7. control ownership ─────────────────────────────────────────────────

def test_control_ownership(conn):
    e = conn
    j = e.submit_series({"count": 50, "interval_s": 0.5, "frames": 1, "dark": "none"},
                        owner="remote:a")
    try:
        wait_for(lambda: e.control_info()["owner"] == "remote:a", what="job control")
        with pytest.raises(eng.ControlBusy):
            e.configure({"exposure_ms": 10.0}, owner="local")
        e.configure({"theme": "dark"}, owner="local")        # display key
        assert Config.get("theme") == "dark"
    finally:
        e.jobs.cancel(j.id)
        wait_job(e, j, timeout=10)
    wait_for(lambda: e.control_info()["owner"] is None, what="control released")

    e.acquire_control("remote:a", "test")
    assert e.control_info()["owner"] == "remote:a"
    with pytest.raises(eng.ControlBusy):
        e.connect(BACKEND, owner="local")
    with pytest.raises(eng.ControlBusy):
        e.disconnect(owner="local")
    with pytest.raises(eng.ControlBusy):
        e.acquire_control("local")
    e.release_control("local")                      # not the holder: no effect
    assert e.control_info()["owner"] == "remote:a"
    e.release_control("remote:a")
    assert e.control_info()["owner"] is None
    e.disconnect(owner="local")                     # free again
    e.connect(BACKEND, owner="local")

    e.acquire_control("remote:a")
    info = e.acquire_control("local", force=True)
    assert info["owner"] == "local"


# ── 8. waiting_for_user ──────────────────────────────────────────────────

def _no_shutter(e):
    e.acquisition.SUPPORTS_SHUTTER = False       # instance attribute


def test_waiting_for_user(conn):
    e = conn
    _no_shutter(e)
    j = e.submit_dark({"frames": 3}, owner="local")
    e.jobs.wait(j.id, timeout=10)
    assert j.status == eng.JOB_WAITING and j.prompt["action"] == "cover"
    e.jobs.continue_job(j.id)
    wait_for(lambda: j.status == eng.JOB_WAITING and j.prompt
             and j.prompt["action"] == "uncover", timeout=15, what="uncover prompt")
    e.jobs.continue_job(j.id)
    wait_job(e, j, timeout=15)
    assert j.status == eng.JOB_DONE, j.error
    assert j.result["n_frames"] >= 2
    with pytest.raises(eng.EngineError):
        e.jobs.continue_job(j.id)


def test_cancel_while_waiting(conn):
    e = conn
    _no_shutter(e)
    j = e.submit_dark({"frames": 3}, owner="local")
    e.jobs.wait(j.id, timeout=10)
    assert j.status == eng.JOB_WAITING
    e.jobs.cancel(j.id)
    wait_job(e, j, timeout=10)
    assert j.status == eng.JOB_CANCELLED
    wait_for(lambda: e.control_info()["owner"] is None, what="control released")


# ── 9. calibration job ───────────────────────────────────────────────────

def test_calibration_job(engine, isolated_config):
    import calibration_profiles as cp
    import reference_sources as rs
    isolated_config._data.update({"demo_spectrum": "cal_lamp",
                                  "demo_sensitivity": "synthetic",
                                  "demo_fullscale_ms": 100.0})
    e = engine
    e.connect(BACKEND, owner="local")
    wait_for(lambda: e.last_frame is not None, what="frame")
    j = e.submit_calibration({"kind": "absolute", "reference_id": rs.DEMO_SOURCE_ID,
                              "optics": {"name": "Engine CC", "input": "CC"}},
                             owner="local")
    wait_job(e, j, timeout=90)
    assert j.status == eng.JOB_DONE, j.error
    res = j.result
    assert res["status"] == "saved", res
    assert res["profile_ids"]
    assert e.calib.active_v2
    assert cp.active_selection(e.device_name) == res["profile_ids"][0]
    try:
        import matplotlib  # noqa: F401
    except ImportError:
        pytest.skip("matplotlib missing: protocol part skipped")
    import os
    assert res["protocol"], res.get("protocol_message")
    for p in res["protocol"]:
        assert os.path.exists(p)
    assert {os.path.splitext(p)[1].lower() for p in res["protocol"]} >= {".pdf", ".csv"}


# ── 10. connection loss ──────────────────────────────────────────────────

def test_connection_lost(conn, events):
    e = conn
    e.acquisition.on_connection_lost()
    ev = wait_for(lambda: next((x.data for x in events if x.kind == "connection"
                                and x.data.get("reason") == "lost"), None),
                  timeout=10, what="lost event")
    assert ev["connected"] is False
    assert e.is_connected is False

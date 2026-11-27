"""Radiance chain over /api/v1 with the Demo backend: sphere characterisation job
(/calibration/spheres), radiance reference, absolute radiance calibration of an
objective, quantity/luminance fields in spectrum / status / analysis,
admissibility endpoint and errors."""
from __future__ import annotations

import math
import os
import time

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("httpx")
from fastapi.testclient import TestClient

import calibration_profiles as cp
import calibration_report as crp
import calibration_sphere as S
import calibration_workflow as cw
import engine as eng
import reference_sources as rs
from api import ServerSettings, create_app

BACKEND = "Demo (virtual)"
V1 = "/api/v1"
FINAL = ("done", "failed", "cancelled")
R_MM = 7.0
D0_TRUE = 0.3
K_SPHERE = 0.8
HEIGHTS = [0.0, 6.0, 16.0]


def _wait(pred, timeout=30.0, step=0.05, what="condition"):
    t0 = time.monotonic()
    while time.monotonic() - t0 < timeout:
        v = pred()
        if v:
            return v
        time.sleep(step)
    raise AssertionError(f"timeout waiting for {what}")


@pytest.fixture
def engine(isolated_config):
    isolated_config._data.update({
        "calibration_capture_frames": 12, "calibration_stability_delay_s": 0.1,
        "demo_spectrum": "cal_lamp", "demo_sensitivity": "synthetic",
        "demo_fullscale_ms": 100.0, "selected_backend": BACKEND})
    e = eng.Engine()
    try:
        yield e
    finally:
        e.close()


@pytest.fixture
def c(engine):
    with TestClient(create_app(engine, ServerSettings())) as client:
        client.engine = engine
        assert client.post(f"{V1}/connect", json={"backend": BACKEND}).status_code == 200
        _wait(lambda: engine.last_frame is not None, what="first frame")
        yield client


def _cc_profile(engine) -> dict:
    wf = cw.CalibrationWorkflow(
        engine.acquisition, BACKEND, kind="absolute",
        optics={"name": "CC", "input": "CC"},
        reference=rs.ReferenceLibrary().get(rs.DEMO_SOURCE_ID),
        cfg=cw.WorkflowConfig(n_frames=12, stability_delay_s=0.1))
    wf.run_all()
    cp.set_active_selection(BACKEND, wf.saved[0]["id"])
    engine.rebuild_calibration()
    return wf.saved[0]


def _sphere_body(**kw):
    body = {"sphere": {"name": "Printed sphere", "serial": "S1", "port_radius_mm": R_MM,
                       "u_radius_mm": 0.02, "u_distance_mm": 0.1, "cc_uncertainty_pct": 3.0},
            "heights_mm": HEIGHTS, "frames": 12}
    body.update(kw)
    return body


def _run_sphere(c, body, timeout=240.0):
    b = c.engine.acquisition
    b.SUPPORTS_SHUTTER = False
    r = c.post(f"{V1}/calibration/spheres", json=body)
    assert r.status_code == 202, r.text
    jid = r.json()["job"]["id"]
    heights = body["heights_mm"]
    edge = body["sphere"].get("edge_depth_mm", 11.0)

    def scale(h):
        return K_SPHERE * float(S.disk_factor(R_MM, edge + h + D0_TRUE))
    seen, prompts, pos = [], [], 0
    deadline = time.monotonic() + timeout
    j = {}
    while time.monotonic() < deadline:
        j = c.get(f"{V1}/jobs/{jid}", params={"wait_s": 0.3}).json()
        if j["status"] == "waiting_for_user":
            a = j["prompt"]["action"]
            seen.append(a)
            prompts.append(j["prompt"].get("message", ""))
            if a == "mount":
                b.source_scale = scale(heights[0])
            elif a == "position":
                b.source_scale = scale(heights[pos])
                pos += 1
            elif a == "repeat":
                b.source_scale = scale(heights[0])
            b.set_shutter(a == "cover")
            assert c.post(f"{V1}/jobs/{jid}/continue").status_code == 200
            time.sleep(0.1)
        elif j["status"] in FINAL:
            break
    b.source_scale = 1.0
    b.set_shutter(False)
    return jid, j, seen, prompts


def test_admissibility_endpoint(c):
    r = c.get(f"{V1}/calibration/admissibility")
    assert r.status_code == 200
    d = r.json()
    assert set(d["inputs"]) == {"CC", "bare", "sphere", "objective"}
    assert set(d["quantities"]) == {"irradiance", "radiance"}
    assert len(d["matrix"]) == 16
    row = next(x for x in d["matrix"] if (x["type"], x["quantity"], x["input"])
               == ("absolute", "radiance", "CC"))
    assert row["allowed"] is False and row["reason"]
    row = next(x for x in d["matrix"] if (x["type"], x["quantity"], x["input"])
               == ("absolute", "radiance", "objective"))
    assert row["allowed"] is True


def test_sphere_needs_cc_profile(c):
    r = c.post(f"{V1}/calibration/spheres", json=_sphere_body())
    assert r.status_code == 422, r.text
    assert "calibration profile" in r.json()["error"]["message"]


def test_sphere_validation(c):
    _cc_profile(c.engine)
    r = c.post(f"{V1}/calibration/spheres", json=_sphere_body(heights_mm=[0.0, 5.0]))
    assert r.status_code == 422
    r = c.post(f"{V1}/calibration/spheres", json=_sphere_body(heights_mm=[0.0, 5.0, 5.0]))
    assert r.status_code == 422
    body = _sphere_body()
    body["sphere"]["port_radius_mm"] = 0
    assert c.post(f"{V1}/calibration/spheres", json=body).status_code == 422


def test_full_chain(c):
    cc_prof = _cc_profile(c.engine)
    jid, j, seen, prompts = _run_sphere(c, _sphere_body())
    assert j["status"] == "done", j
    assert seen == ["mount", "cover", "uncover"] + ["position"] * 3 + ["repeat", "cover"]
    assert S.SPHERE_SOURCE_HINT in prompts[0]
    res = j["result"]
    assert res["status"] == "saved", res
    assert res["quantity"] == "radiance" and res["method"]
    assert abs(res["d0_mm"] - D0_TRUE) < 0.1 and res["d0_plausible"] is True
    assert res["consistency"]["pass"] is True and res["consistency"]["bands"]
    assert all(not isinstance(v, list) for bd in res["consistency"]["bands"]
               for v in bd.values())
    assert res["drift"]["pass"] is True
    assert res["uncertainty"]["u_rel_median_pct"] > 0
    assert res["valid_range_nm"] and "warnings" in res and "protocol_message" in res

    # reference list: quantity + short meta
    refs = c.get(f"{V1}/calibration/references").json()
    src = next(r for r in refs if r["id"] == res["source_id"])
    assert src["quantity"] == "radiance" and src["unit"] == "uW/cm2/sr/nm"
    assert src["meta"]["port_radius_mm"] == R_MM
    assert src["meta"]["cc_profile_id"] == cc_prof["id"]
    demo_ref = next(r for r in refs if r["id"] == rs.DEMO_SOURCE_ID)
    assert demo_ref["quantity"] == "irradiance"

    # absolute radiance calibration of an objective against the sphere
    b = c.engine.acquisition
    b.source_scale = 0.05
    r = c.post(f"{V1}/calibration/runs", json={
        "kind": "absolute", "reference_id": res["source_id"], "verify": False,
        "optics": {"name": "Objective 10x", "input": "objective"}})
    assert r.status_code == 202, r.text
    rid = r.json()["job"]["id"]
    b.SUPPORTS_SHUTTER = False
    deadline = time.monotonic() + 240
    while time.monotonic() < deadline:
        jj = c.get(f"{V1}/jobs/{rid}", params={"wait_s": 0.3}).json()
        if jj["status"] == "waiting_for_user":
            b.set_shutter(jj["prompt"]["action"] == "cover")
            c.post(f"{V1}/jobs/{rid}/continue")
            time.sleep(0.1)
        elif jj["status"] in FINAL:
            break
    b.set_shutter(False)
    assert jj["status"] == "done", jj
    rres = jj["result"]
    assert rres["status"] == "saved", rres
    prof = cp.get_profile_by_id(rres["activated"])
    assert prof["quantity"] == "radiance" and prof["unit"] == "uW/cm2/sr/nm per count/s"

    # status / spectrum / analysis carry quantity and luminance
    st = c.get(f"{V1}/status").json()
    assert st["quantity"] == "radiance" and st["calibration"]["quantity"] == "radiance"
    assert c.get(f"{V1}/calibration/status").json()["quantity"] == "radiance"
    _wait(lambda: c.engine.last_frame is not None
          and c.engine.last_frame.unit == "uW/cm2/sr/nm", what="radiance frame")
    sp = c.get(f"{V1}/spectrum/latest").json()
    assert sp["quantity"] == "radiance" and sp["unit"] == "uW/cm2/sr/nm"
    assert sp["luminance_cd_m2"] is None or sp["luminance_cd_m2"] > 0
    an = c.post(f"{V1}/analyze/color", json={}).json()
    assert an["quantity"] == "radiance"
    assert an["luminance_cd_m2"] > 0 and math.isfinite(an["luminance_cd_m2"])
    assert an["metrics"]["lux"] is None

    # protocol download of the sphere job
    if hasattr(crp, "write_sphere_protocol"):
        for fmt in ("pdf", "csv"):
            rr = c.get(f"{V1}/calibration/spheres/{jid}/protocol", params={"format": fmt})
            assert rr.status_code == 200, rr.text
    else:
        assert c.get(f"{V1}/calibration/spheres/{jid}/protocol").status_code == 404
    assert c.get(f"{V1}/calibration/spheres/{rid}/protocol").status_code == 404


def test_run_inadmissible_is_422(c):
    r = c.post(f"{V1}/calibration/runs", json={
        "kind": "absolute", "reference_id": rs.DEMO_SOURCE_ID,
        "optics": {"name": "Obj", "input": "objective"}})
    assert r.status_code == 422, r.text
    assert "irradiance" in r.json()["error"]["message"]
    r = c.post(f"{V1}/calibration/runs", json={
        "kind": "relative", "reference_id": rs.DEMO_SOURCE_ID,
        "optics": {"name": "Obj", "input": "objective"}})
    assert r.status_code == 202, r.text
    c.post(f"{V1}/jobs/{r.json()['job']['id']}/cancel")


def test_reference_import_quantity(c):
    content = "wavelength,value\n" + "\n".join(f"{w},{1.0 + 0.001 * w}" for w in range(400, 801, 10))
    r = c.post(f"{V1}/calibration/references", json={
        "name": "Radiance lamp", "unit": "W/m2/sr/nm", "filename": "l.csv",
        "content": content})
    assert r.status_code == 200, r.text
    assert r.json()["quantity"] == "radiance"
    r = c.post(f"{V1}/calibration/references", json={
        "name": "Bad", "unit": "W/m2/sr/nm", "quantity": "irradiance",
        "filename": "l.csv", "content": content})
    assert r.status_code == 400
    r = c.post(f"{V1}/calibration/references", json={
        "name": "Bad2", "unit": "W/m2/sr/nm", "quantity": "flux",
        "filename": "l.csv", "content": content})
    assert r.status_code == 422


def test_irradiance_spectrum_has_no_luminance(c):
    sp = c.get(f"{V1}/spectrum/latest").json()
    assert sp["quantity"] == "irradiance" and sp["luminance_cd_m2"] is None
    assert c.get(f"{V1}/status").json()["quantity"] == "irradiance"

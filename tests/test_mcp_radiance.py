"""MCP tools for the radiance chain: start_sphere_characterization, quantity /
luminance fields, list_references, optics_input 'objective'."""
from __future__ import annotations

import asyncio
import json
import socket
import time

import pytest

pytest.importorskip("httpx")
pytest.importorskip("mcp")
pytest.importorskip("uvicorn")

import calibration_profiles as cp
import calibration_sphere as S
import calibration_workflow as cw
import reference_sources as rs

BACKEND = "Demo (virtual)"
LIMIT = 8 * 1024
R_MM = 7.0
D0_TRUE = 0.3
K_SPHERE = 0.8
HEIGHTS = [0.0, 6.0, 16.0]


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class Harness:
    def __init__(self, engine, runner, server):
        self.engine, self.runner, self.server = engine, runner, server

    def call(self, name: str, **args):
        from mcp import Client

        async def go():
            async with Client(self.server) as c:
                return await c.call_tool(name, args)

        res = asyncio.run(go())
        text = "".join(getattr(p, "text", "") for p in res.content)
        try:
            data = json.loads(text)
        except ValueError:
            data = None
        return res.is_error, text, data

    def ok(self, name: str, **args) -> dict:
        err, text, data = self.call(name, **args)
        assert not err, f"{name} failed: {text}"
        assert len(text.encode("utf-8")) <= LIMIT, f"{name}: {len(text)} bytes"
        assert isinstance(data, dict), text
        return data


@pytest.fixture
def harness(isolated_config, tmp_path, monkeypatch):
    import api
    import engine as eng
    import server_runner
    isolated_config._data.update({
        "calibration_capture_frames": 12, "calibration_stability_delay_s": 0.1,
        "demo_spectrum": "cal_lamp", "demo_sensitivity": "synthetic",
        "demo_fullscale_ms": 100.0, "selected_backend": BACKEND})
    port = _free_port()
    e = eng.Engine()
    runner = server_runner.ServerRunner(
        e, api.ServerSettings(port=port, token="tt", require_token_local=True))
    runner.start()
    monkeypatch.setenv("SPECTRO_API_URL", f"http://127.0.0.1:{port}")
    monkeypatch.setenv("SPECTRO_API_TOKEN", "tt")
    monkeypatch.setenv("SPECTRO_DATA_DIR", str(tmp_path / "mcpdata"))
    monkeypatch.setenv("SPECTRO_CLIENT_ID", "pytest")
    from mcp_server.server import build_server
    try:
        h = Harness(e, runner, build_server())
        h.ok("connect", backend=BACKEND)
        t0 = time.monotonic()
        while e.last_frame is None and time.monotonic() - t0 < 10:
            time.sleep(0.05)
        assert e.last_frame is not None
        yield h
    finally:
        runner.stop()
        e.close()


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


def test_tool_registered_with_descriptions(harness):
    from mcp import Client

    async def go():
        async with Client(harness.server) as c:
            return (await c.list_tools()).tools

    by = {t.name: t for t in asyncio.run(go())}
    t = by["start_sphere_characterization"]
    assert "RADIANCE" in t.description and "heights_mm" in t.description
    props = t.input_schema["properties"]
    assert {"sphere_name", "port_radius_mm", "heights_mm", "edge_depth_mm",
            "cc_ref_offset_mm", "u_radius_mm", "u_distance_mm",
            "cc_uncertainty_pct"} <= set(props)
    assert props["edge_depth_mm"]["default"] == 11.0
    sc = by["start_calibration"]
    assert "objective" in json.dumps(sc.input_schema["properties"]["optics_input"])
    assert "drift_check_failed" in sc.description
    assert "luminance_cd_m2" in by["analyze_color"].description


def test_list_references_shows_quantity(harness):
    refs = harness.ok("list_references")["references"]
    assert refs and all("quantity" in r and "meta" in r for r in refs)
    assert {r["quantity"] for r in refs} == {"irradiance"}


def test_inadmissible_and_prerequisite_errors(harness):
    err, text, _ = harness.call("start_calibration", reference_id=rs.DEMO_SOURCE_ID,
                                optics_name="Obj", kind="absolute",
                                optics_input="objective")
    assert err and "irradiance" in text
    err, text, _ = harness.call("start_sphere_characterization", sphere_name="S",
                                port_radius_mm=R_MM, heights_mm=HEIGHTS)
    assert err and "calibration profile" in text


def test_sphere_run_and_quantity_fields(harness):
    e = harness.engine
    _cc_profile(e)
    b = e.acquisition
    b.SUPPORTS_SHUTTER = False
    j = harness.ok("start_sphere_characterization", sphere_name="Printed sphere",
                   serial="S1", port_radius_mm=R_MM, heights_mm=HEIGHTS,
                   u_radius_mm=0.02, u_distance_mm=0.1, cc_uncertainty_pct=3.0)["job"]
    jid = j["id"]

    def scale(h):
        return K_SPHERE * float(S.disk_factor(R_MM, 11.0 + h + D0_TRUE))
    pos, seen = 0, []
    deadline = time.monotonic() + 240
    final = None
    while time.monotonic() < deadline:
        st = harness.ok("job_status", job_id=jid)
        job = st.get("job", st)
        if job["status"] == "waiting_for_user":
            a = job["prompt"]["action"]
            seen.append(a)
            assert "continue_job" in job["next"]
            if a == "mount" or a == "repeat":
                b.source_scale = scale(HEIGHTS[0])
            elif a == "position":
                b.source_scale = scale(HEIGHTS[pos])
                pos += 1
            b.set_shutter(a == "cover")
            harness.ok("continue_job", job_id=jid)
            time.sleep(0.1)
        elif job["status"] in ("done", "failed", "cancelled"):
            final = job
            break
        else:
            time.sleep(0.1)
    b.source_scale = 1.0
    b.set_shutter(False)
    assert final and final["status"] == "done", final
    res = final["result"]
    assert res["status"] == "saved" and res["quantity"] == "radiance"
    assert abs(res["d0_mm"] - D0_TRUE) < 0.1
    assert seen[:1] == ["mount"] and seen.count("position") == 3

    refs = harness.ok("list_references")["references"]
    src = next(r for r in refs if r["id"] == res["source_id"])
    assert src["quantity"] == "radiance" and src["unit"] == "uW/cm2/sr/nm"
    assert src["meta"]["port_radius_mm"] == R_MM

    # objective calibration against the sphere: radiance profile active
    b.source_scale = 0.05
    cal = harness.ok("start_calibration", reference_id=res["source_id"],
                     optics_name="Objective", kind="absolute", optics_input="objective")
    cid = cal["job"]["id"]
    deadline = time.monotonic() + 240
    while time.monotonic() < deadline:
        st = harness.ok("job_status", job_id=cid)
        job = st.get("job", st)
        if job["status"] == "waiting_for_user":
            b.set_shutter(job["prompt"]["action"] == "cover")
            harness.ok("continue_job", job_id=cid)
            time.sleep(0.1)
        elif job["status"] in ("done", "failed", "cancelled"):
            break
        else:
            time.sleep(0.1)
    b.set_shutter(False)
    assert job["status"] == "done", job

    cs_ = harness.ok("calibration_status")
    assert cs_["quantity"] == "radiance"
    st = harness.ok("status")
    assert st["quantity"] == "radiance" and st["unit"] == "uW/cm2/sr/nm"
    t0 = time.monotonic()
    while time.monotonic() - t0 < 10 and e.last_frame.unit != "uW/cm2/sr/nm":
        time.sleep(0.1)
    sp = harness.ok("get_spectrum")
    assert sp["quantity"] == "radiance" and sp["unit"] == "uW/cm2/sr/nm"
    col = harness.ok("analyze_color")
    assert col["quantity"] == "radiance" and col["luminance_cd_m2"] > 0
    m = harness.ok("measure", frames=2)
    meas = m.get("measurement", m)
    assert meas["quantity"] == "radiance"
    assert meas["color"]["luminance_cd_m2"] > 0

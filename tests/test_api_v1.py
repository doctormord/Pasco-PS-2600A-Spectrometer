"""/api/v1 automation API against the Demo backend through TestClient:
system, token, device, control lease, measure / series / jobs, user steps,
calibration runs, analysis / reports, data and the ServerRunner thread."""
from __future__ import annotations

import json
import os
import socket
import time
from datetime import datetime

import pytest
from fastapi.testclient import TestClient

import engine as eng
import reference_sources
from api import ServerSettings, create_app

BACKEND = "Demo (virtual)"
V1 = "/api/v1"
TOKEN = "t0k"
AUTH = {"Authorization": f"Bearer {TOKEN}"}
FINAL = ("done", "failed", "cancelled")


def wait_for(pred, timeout=15.0, step=0.03, what="condition"):
    t0 = time.monotonic()
    while time.monotonic() - t0 < timeout:
        v = pred()
        if v:
            return v
        time.sleep(step)
    raise AssertionError(f"timeout waiting for {what}")


def err(r, status, code):
    assert r.status_code == status, r.text
    e = r.json()["error"]
    assert set(e) >= {"code", "message", "details"}
    assert e["code"] == code, e
    return e


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
def client(engine):
    with TestClient(create_app(engine, ServerSettings())) as c:
        c.engine = engine
        yield c


@pytest.fixture
def lan(engine):
    with TestClient(create_app(engine, ServerSettings(host="0.0.0.0", token=TOKEN))) as c:
        c.engine = engine
        yield c


@pytest.fixture
def conn(client):
    r = client.post(f"{V1}/connect", json={"backend": BACKEND})
    assert r.status_code == 200, r.text
    wait_for(lambda: client.engine.last_frame is not None, what="first frame")
    return client


@pytest.fixture
def cal_client(engine, isolated_config):
    isolated_config._data.update({"demo_spectrum": "cal_lamp", "demo_sensitivity": "synthetic",
                                  "demo_fullscale_ms": 100.0})
    with TestClient(create_app(engine, ServerSettings())) as c:
        c.engine = engine
        assert c.post(f"{V1}/connect", json={"backend": BACKEND}).status_code == 200
        wait_for(lambda: engine.last_frame is not None, what="frame")
        yield c


def job_get(c, jid, wait_s=0):
    r = c.get(f"{V1}/jobs/{jid}", params={"wait_s": wait_s})
    assert r.status_code == 200, r.text
    return r.json()


def wait_status(c, jid, statuses, timeout=60.0):
    out = {}

    def ok():
        out.update(job_get(c, jid, 0.5))
        return out["status"] in statuses
    wait_for(ok, timeout=timeout, step=0.05, what=f"job {statuses}")
    return out


def wait_prompt(c, jid, action, timeout=30.0):
    def ok():
        j = job_get(c, jid, 0.3)
        assert j["status"] not in ("failed", "cancelled"), j
        return j["status"] == "waiting_for_user" and (j.get("prompt") or {}).get("action") == action
    wait_for(ok, timeout=timeout, step=0.05, what=f"prompt {action}")


def engine_of(c):
    return c.engine


def no_shutter(c):
    c.engine.acquisition.SUPPORTS_SHUTTER = False


# -- 1. system ---------------------------------------------------------------
def test_system_endpoints(client):
    s = client.get(f"{V1}/status")
    assert s.status_code == 200
    j = s.json()
    for k in ("connected", "control", "jobs", "api_version", "token_required"):
        assert k in j
    assert j["connected"] is False and j["api_version"] == "1" and j["token_required"] is False
    b = client.get(f"{V1}/backends").json()
    assert BACKEND in b["backends"] and "selected" in b
    d = client.get(f"{V1}/devices", params={"backend": BACKEND}).json()
    assert d["backend"] == BACKEND and d["devices"]
    spec = client.get("/openapi.json").json()
    assert f"{V1}/measure" in spec["paths"] and f"{V1}/calibration/runs" in spec["paths"]
    assert client.get("/docs").status_code == 200
    err(client.get(f"{V1}/nope"), 404, "not_found")


# -- 2. token ----------------------------------------------------------------
def test_token_v1(lan):
    assert lan.get(f"{V1}/status").json()["token_required"] is True
    for kw in ({}, {"headers": {"Authorization": "Bearer wrong"}}):
        r = lan.post(f"{V1}/connect", json={"backend": BACKEND}, **kw)
        err(r, 401, "unauthorized")
        assert "bearer" in r.headers.get("www-authenticate", "").lower()
    err(lan.put(f"{V1}/acquisition", json={"exposure_ms": 5}), 401, "unauthorized")
    err(lan.post(f"{V1}/control", json={"action": "acquire"}), 401, "unauthorized")
    r = lan.post(f"{V1}/connect", json={"backend": BACKEND}, headers=AUTH)
    assert r.status_code == 200 and r.json()["connected"] is True
    assert lan.get(f"{V1}/control").status_code == 200
    assert lan.post(f"{V1}/disconnect", headers=AUTH).status_code == 200


def test_token_legacy_and_ws(lan):
    r = lan.post("/api/connect", json={"backend": BACKEND})
    assert r.status_code == 401 and "detail" in r.json() and "error" not in r.json()
    with lan.websocket_connect("/ws/stream") as ws:
        hello = ws.receive_json()
        assert hello["type"] == "hello" and hello["authorized"] is False
        ws.send_json({"type": "set_config", "payload": {}})
        msg = ws.receive_json()
        for _ in range(20):
            if msg.get("type") == "error":
                break
            msg = ws.receive_json()
        assert msg["type"] == "error" and msg["code"] == "unauthorized"
    with lan.websocket_connect(f"/ws/stream?token={TOKEN}") as ws:
        assert ws.receive_json()["authorized"] is True


# -- 3. device ---------------------------------------------------------------
def test_connect_acquisition_autoexposure(client):
    r = client.post(f"{V1}/connect", json={"backend": BACKEND})
    assert r.status_code == 200 and r.json()["connected"] is True
    wait_for(lambda: client.engine.last_frame is not None, what="frame")
    a = client.get(f"{V1}/acquisition").json()
    lo, hi = a["integration_bounds_us"]
    assert lo < hi and "paused" in a

    assert client.put(f"{V1}/acquisition", json={"exposure_ms": 1e9}).status_code == 200
    assert client.engine.status()["integration_us"] <= hi
    assert client.put(f"{V1}/acquisition", json={"exposure_ms": 0.0001}).status_code == 200
    assert client.engine.status()["integration_us"] >= lo
    r = client.put(f"{V1}/acquisition", json={"exposure_ms": 20, "frames_to_average": 2})
    assert r.status_code == 200 and "applied" in r.json() and "status" in r.json()
    assert client.get(f"{V1}/acquisition").json()["frames_to_average"] == 2

    err(client.put(f"{V1}/acquisition", json={}), 400, "bad_request")
    err(client.put(f"{V1}/acquisition", json={"exposure_ms": -3}), 422, "validation_error")
    err(client.put(f"{V1}/acquisition", json={"frames_to_average": 0}), 422, "validation_error")

    client.put(f"{V1}/acquisition", json={"exposure_ms": 5})
    t0 = client.engine.status()["integration_us"]
    r = client.post(f"{V1}/auto-exposure", json={"apply": False})
    assert r.status_code == 200, r.text
    assert r.json()["converged"] is True
    assert client.engine.status()["integration_us"] == t0
    r = client.post(f"{V1}/auto-exposure", json={"apply": True})
    assert r.status_code == 200 and r.json()["integration_us"] > 0
    err(client.post(f"{V1}/auto-exposure", json={"target_frac_lo": 0.9, "target_frac_hi": 0.8}),
        422, "validation_error")

    r = client.post(f"{V1}/disconnect")
    assert r.status_code == 200 and r.json()["connected"] is False


# -- 4. control lease --------------------------------------------------------
def test_control_lease(conn):
    A, B = {"X-Client-Id": "a"}, {"X-Client-Id": "b"}
    r = conn.post(f"{V1}/control", json={"action": "acquire", "reason": "t"}, headers=A)
    assert r.status_code == 200 and r.json()["owner"] == "remote:a"
    assert conn.get(f"{V1}/control").json()["owner"] == "remote:a"
    e = err(conn.put(f"{V1}/acquisition", json={"exposure_ms": 7}, headers=B), 409, "control_busy")
    assert e["details"]["owner"] == "remote:a"
    err(conn.post(f"{V1}/control", json={"action": "acquire"}, headers=B), 409, "control_busy")
    assert conn.put(f"{V1}/acquisition", json={"exposure_ms": 7}, headers=A).status_code == 200
    r = conn.post(f"{V1}/control", json={"action": "acquire", "force": True}, headers=B)
    assert r.status_code == 200 and r.json()["owner"] == "remote:b"
    r = conn.post(f"{V1}/control", json={"action": "release"}, headers=B)
    assert r.status_code == 200 and r.json()["owner"] is None
    assert conn.put(f"{V1}/acquisition", json={"exposure_ms": 8}, headers=A).status_code == 200


# -- 5. measure --------------------------------------------------------------
def test_measure_and_measurements(conn):
    r = conn.post(f"{V1}/measure", json={"frames": 2, "integration_ms": 20, "dark": "capture"})
    assert r.status_code == 200, r.text
    body = r.json()
    m = body["measurement"]
    assert body["job"]["status"] == "done"
    assert "location" in r.headers
    assert m["acquisition"]["frames"] == 2
    assert datetime.fromisoformat(m["timestamp"]).utcoffset() is not None
    assert m["device"].get("serial")
    assert m["unit"] and len(m["wavelengths"]) == len(m["values"]) > 100
    mid = m["id"]

    r = conn.post(f"{V1}/measure", json={"integration_ms": "auto", "frames": 1})
    assert r.status_code == 200, r.text
    mid2 = r.json()["measurement"]["id"]

    lst = conn.get(f"{V1}/measurements").json()
    assert {x["id"] for x in lst} >= {mid, mid2}
    assert len(conn.get(f"{V1}/measurements", params={"limit": 1}).json()) == 1
    g = conn.get(f"{V1}/measurements/{mid}", params={"arrays": "false"}).json()
    assert g["id"] == mid and not g.get("wavelengths") and not g.get("values")
    full = conn.get(f"{V1}/measurements/{mid}").json()
    assert len(full["values"]) == len(m["values"])
    csv = conn.get(f"{V1}/measurements/{mid}/export", params={"format": "csv"})
    assert csv.status_code == 200 and "text/csv" in csv.headers["content-type"]
    assert "attachment" in csv.headers["content-disposition"] and len(csv.text) > 100
    js = conn.get(f"{V1}/measurements/{mid}/export", params={"format": "json"})
    assert js.status_code == 200 and json.loads(js.text)["id"] == mid
    err(conn.get(f"{V1}/measurements/unknown_id"), 404, "not_found")
    err(conn.get(f"{V1}/measurements/unknown_id/export"), 404, "not_found")
    err(conn.get(f"{V1}/measurements/{mid}/export", params={"format": "xml"}),
        422, "validation_error")


def test_measure_dark_stored_missing(conn):
    err(conn.post(f"{V1}/measure", json={"dark": "stored", "integration_ms": 33}),
        422, "unprocessable")
    err(conn.post(f"{V1}/measure", json={"frames": 0}), 422, "validation_error")


def test_measure_not_connected(client):
    err(client.post(f"{V1}/measure", json={}), 503, "not_connected")


# -- 6. series / jobs --------------------------------------------------------
def test_series_and_jobs(conn):
    r = conn.post(f"{V1}/series", json={"count": 3, "interval_s": 0.1, "frames": 1,
                                        "integration_ms": 20})
    assert r.status_code == 202, r.text
    jid = r.json()["job"]["id"]
    assert r.headers["location"].endswith(f"/jobs/{jid}")
    assert any(j["id"] == jid for j in conn.get(f"{V1}/jobs").json())
    j = job_get(conn, jid, 30)
    assert j["status"] == "done", j
    assert len(j["result"]["measurement_ids"]) == 3
    assert all(x["id"] != jid for x in conn.get(f"{V1}/jobs", params={"active": "true"}).json())
    err(conn.get(f"{V1}/jobs/nojob"), 404, "not_found")


def test_series_cancel(conn):
    r = conn.post(f"{V1}/series", json={"count": 500, "interval_s": 1.0, "frames": 1,
                                        "integration_ms": 20})
    assert r.status_code == 202
    jid = r.json()["job"]["id"]
    assert any(j["id"] == jid for j in conn.get(f"{V1}/jobs", params={"active": "true"}).json())
    assert conn.post(f"{V1}/jobs/{jid}/cancel").status_code == 200
    assert wait_status(conn, jid, FINAL, timeout=15)["status"] == "cancelled"


# -- 7. user steps -----------------------------------------------------------
def test_dark_user_steps(conn):
    no_shutter(conn)
    r = conn.post(f"{V1}/dark", json={"wait": False, "frames": 3})
    assert r.status_code == 202, r.text
    jid = r.json()["job"]["id"]
    wait_prompt(conn, jid, "cover")
    assert conn.post(f"{V1}/jobs/{jid}/continue").status_code == 200
    wait_prompt(conn, jid, "uncover")
    assert conn.post(f"{V1}/jobs/{jid}/continue").status_code == 200
    assert wait_status(conn, jid, FINAL)["status"] == "done"
    err(conn.post(f"{V1}/jobs/{jid}/continue"), 400, "bad_request")


# -- 8. calibration run with user steps --------------------------------------
def test_calibration_run_with_user_steps(cal_client):
    c = cal_client
    no_shutter(c)
    A, B = {"X-Client-Id": "a"}, {"X-Client-Id": "b"}
    r = c.post(f"{V1}/calibration/runs", headers=A, json={
        "kind": "absolute", "reference_id": reference_sources.DEMO_SOURCE_ID,
        "optics": {"name": "CC test", "input": "CC"}})
    assert r.status_code == 202, r.text
    jid = r.json()["job"]["id"]
    err(c.put(f"{V1}/acquisition", json={"exposure_ms": 9}, headers=B), 409, "control_busy")
    seen = []
    deadline = time.monotonic() + 80
    j = {}
    while time.monotonic() < deadline:
        j = job_get(c, jid, 0.3)
        if j["status"] == "waiting_for_user":
            seen.append(j["prompt"]["action"])
            # the demo has no real cover: emulate the user blocking the light path
            engine_of(c).acquisition.set_shutter(seen[-1] == "cover")
            assert c.post(f"{V1}/jobs/{jid}/continue", headers=A).status_code == 200
            time.sleep(0.1)
        elif j["status"] in FINAL:
            break
    assert j["status"] == "done", j
    assert "cover" in seen and "uncover" in seen
    assert seen.index("cover") < seen.index("uncover")
    res = j["result"]
    assert res["status"] == "saved" and res["profile_ids"], res
    st = c.get(f"{V1}/calibration/status").json()
    assert st["state"] == "ok" and "CC test" in st["label"] and st["unit"] != "counts", st
    assert res["profile_ids"][0] in json.dumps(c.get(f"{V1}/calibration/profiles").json())
    assert c.get(f"{V1}/calibration/runs/{jid}/protocol",
                 params={"format": "csv"}).status_code == 200
    try:
        import matplotlib  # noqa: F401
    except ImportError:
        pass
    else:
        p = c.get(f"{V1}/calibration/runs/{jid}/protocol", params={"format": "pdf"})
        assert p.status_code == 200 and p.content[:4] == b"%PDF"
    err(c.get(f"{V1}/calibration/runs/nojob/protocol"), 404, "not_found")
    wait_for(lambda: c.get(f"{V1}/control").json()["owner"] is None, what="control released")

    # export -> import round trip with the freshly created profile
    pid = res["profile_ids"][0]
    ex = c.get(f"{V1}/calibration/profiles/{pid}/export")
    assert ex.status_code == 200 and "attachment" in ex.headers["content-disposition"]
    imp = c.post(f"{V1}/calibration/profiles/import", json=ex.json())
    assert imp.status_code == 200, imp.text
    assert imp.json()["ok"] is True and imp.json()["id"]


# -- 9. calibration misc -----------------------------------------------------
def test_calibration_misc(conn):
    assert conn.get(f"{V1}/calibration/profiles").status_code == 200
    assert conn.get(f"{V1}/calibration/profiles", params={"include_old": "true"}).status_code == 200
    refs = conn.get(f"{V1}/calibration/references").json()
    assert reference_sources.DEMO_SOURCE_ID in json.dumps(refs)
    assert conn.get(f"{V1}/calibration/darks").status_code == 200
    assert conn.get(f"{V1}/calibration/status").status_code == 200
    r = conn.post(f"{V1}/calibration/select", json={"selection": "None"})
    assert r.status_code == 200, r.text
    r = conn.post(f"{V1}/calibration/select", json={"selection": "does-not-exist"})
    assert r.status_code == 400, r.text
    assert r.json()["error"]["code"]
    err(conn.post(f"{V1}/calibration/profiles/copy", json={"id": "nope"}), 404, "not_found")
    err(conn.get(f"{V1}/calibration/profiles/nope/export"), 404, "not_found")
    err(conn.post(f"{V1}/calibration/profiles/import", json={"junk": 1}), 400, "bad_request")


# -- 10. analysis / reports --------------------------------------------------
@pytest.fixture
def measurements(conn):
    ids = []
    for ms in (20, 25):
        r = conn.post(f"{V1}/measure", json={"integration_ms": ms, "frames": 1})
        assert r.status_code == 200, r.text
        ids.append(r.json()["measurement"]["id"])
    return conn, ids


def test_analysis(measurements):
    c, (m1, m2) = measurements
    r = c.post(f"{V1}/analyze/color", json={"measurement_id": m1})
    assert r.status_code == 200, r.text
    assert r.json()["source"] == {"measurement_id": m1} and "metrics" in r.json()
    r = c.post(f"{V1}/analyze/color", json={})
    assert r.status_code == 200 and r.json()["source"] == {"live": True}
    err(c.post(f"{V1}/analyze/color", json={"measurement_id": "nope"}), 404, "not_found")
    r = c.post(f"{V1}/analyze/peaks", json={"measurement_id": m1, "min_prominence": 1,
                                            "min_height": 1})
    assert r.status_code == 200 and isinstance(r.json()["peaks"], list)
    r = c.post(f"{V1}/analyze/filter", json={"reference_id": m1, "sample_id": m2})
    assert r.status_code == 200, r.text
    assert r.json()["n_points"] > 0 and r.json()["rows"]
    err(c.post(f"{V1}/analyze/filter", json={"reference_id": m1}), 400, "bad_request")
    r = c.post(f"{V1}/analyze/tm30", json={"measurement_id": m1})
    if r.status_code == 503:
        pytest.skip("colour-science not installed")
    assert r.status_code == 200 and "tm30" in r.json()


def test_reports(measurements):
    c, (m1, m2) = measurements
    r = c.post(f"{V1}/reports", json={"kind": "color", "format": "csv", "measurement_id": m1})
    assert r.status_code == 200 and "text/csv" in r.headers["content-type"]
    r = c.post(f"{V1}/reports", json={"kind": "measurement", "format": "csv",
                                      "measurement_id": m1})
    assert r.status_code == 200 and len(r.text) > 100
    err(c.post(f"{V1}/reports", json={"kind": "measurement", "format": "pdf",
                                      "measurement_id": m1}), 422, "unprocessable")
    err(c.post(f"{V1}/reports", json={"kind": "measurement", "format": "csv"}),
        400, "bad_request")
    r = c.post(f"{V1}/reports", json={"kind": "filter", "format": "csv",
                                      "reference_id": m1, "sample_id": m2})
    assert r.status_code == 200, r.text
    r = c.post(f"{V1}/reports", json={"kind": "color", "format": "csv",
                                      "measurement_id": m1, "save": True})
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["kind"] == "color" and d["format"] == "csv" and os.path.isfile(d["path"])


def test_report_color_pdf(measurements):
    c, (m1, _m2) = measurements
    r = c.post(f"{V1}/reports", json={"kind": "color", "format": "pdf", "measurement_id": m1})
    if r.status_code == 503:
        pytest.skip("matplotlib not installed")
    assert r.status_code == 200 and r.content[:4] == b"%PDF"


# -- 11. data ----------------------------------------------------------------
def test_spectrum_latest(client):
    err(client.get(f"{V1}/spectrum/latest"), 503, "not_connected")
    client.post(f"{V1}/connect", json={"backend": BACKEND})
    wait_for(lambda: client.engine.last_frame is not None, what="frame")
    full = client.get(f"{V1}/spectrum/latest").json()
    assert full["n_points"] == len(full["wavelengths"]) == len(full["values"]) > 100
    r = client.get(f"{V1}/spectrum/latest", params={"max_points": 100}).json()
    assert 0 < r["n_points"] <= 100 and r["downsample"] > 1
    w = client.get(f"{V1}/spectrum/latest", params={"wl_min": 500, "wl_max": 600}).json()
    assert w["wavelengths"] and min(w["wavelengths"]) >= 500 and max(w["wavelengths"]) <= 600
    err(client.get(f"{V1}/spectrum/latest", params={"max_points": 1}), 422, "validation_error")


# -- 12. server thread -------------------------------------------------------
def _free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def test_server_runner(engine):
    import httpx
    from server_runner import ServerRunner, port_free
    port = _free_port()
    runner = ServerRunner(engine, ServerSettings(port=port))
    runner.start()
    try:
        r = httpx.get(f"http://127.0.0.1:{port}/api/v1/status", timeout=5)
        assert r.status_code == 200 and r.json()["api_version"] == "1"
        second = ServerRunner(engine, ServerSettings(port=port))
        with pytest.raises(OSError):
            second.start()
    finally:
        runner.stop()
    assert not runner.running
    wait_for(lambda: port_free("127.0.0.1", port), timeout=5, what="port free")
    with socket.socket() as s:
        s.bind(("127.0.0.1", port))

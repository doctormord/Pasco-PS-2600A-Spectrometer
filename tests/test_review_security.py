"""Regression tests for the review findings (K1, K2, W1, W3, W4, W5)."""
from __future__ import annotations

import time

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient

from api import ServerSettings, create_app
from engine import LOCAL, ControlBusy, Engine

BACKEND = "Demo (virtual)"


@pytest.fixture
def lan(isolated_config):
    isolated_config._data["remote_token"] = "s3cret-token"
    e = Engine()
    c = TestClient(create_app(e, ServerSettings(host="0.0.0.0", token="s3cret-token")))
    yield e, c
    e.close()


@pytest.fixture
def local(isolated_config):
    isolated_config._data["remote_token"] = "s3cret-token"
    e = Engine()
    c = TestClient(create_app(e, ServerSettings()))
    yield e, c
    e.close()


H = {"Authorization": "Bearer s3cret-token"}


def test_token_never_leaks(lan):
    _e, c = lan
    assert "remote_token" not in c.get("/api/config").json()
    r = c.post("/api/config", json={"theme": "dark"}, headers=H)
    assert r.status_code == 200 and "remote_token" not in r.json()
    with c.websocket_connect("/ws/stream") as ws:
        hello = ws.receive_json()
        assert hello["type"] == "hello" and "remote_token" not in hello["config"]


def test_security_keys_not_settable_remotely(local, isolated_config):
    _e, c = local
    for key, val in (("remote_lan", True), ("remote_token", "x"),
                     ("calibration_profiles_file", "C:/evil.json"),
                     ("export_dir", "C:/somewhere")):       # export_dir: token mode only
        r = c.post("/api/config", json={key: val})
        assert r.status_code == 403, (key, r.text)
    assert isolated_config.get("remote_token") == "s3cret-token"
    with c.websocket_connect("/ws/stream") as ws:
        ws.receive_json()
        ws.send_json({"type": "set_config", "payload": {"remote_lan": True}})
        msg = ws.receive_json()
        while msg.get("type") != "error":
            msg = ws.receive_json()
        assert msg["code"] == "forbidden"
    assert isolated_config.get("remote_lan") is False


def test_export_dir_allowed_with_token(lan, isolated_config, tmp_path):
    _e, c = lan
    r = c.post("/api/config", json={"export_dir": str(tmp_path / "x")}, headers=H)
    assert r.status_code == 200


def test_cross_origin_and_rebinding_refused(local):
    _e, c = local
    r = c.post("/api/config", json={"theme": "dark"}, headers={"Origin": "http://evil.example"})
    assert r.status_code == 403 and r.json()["error"]["code"] == "forbidden_origin"
    r = c.post("/api/config", json={"theme": "dark"},
               headers={"Origin": "http://testserver"})
    assert r.status_code == 200
    r = c.get("/api/status", headers={"Host": "evil.example"})
    assert r.status_code == 400
    from starlette.websockets import WebSocketDisconnect
    with pytest.raises(WebSocketDisconnect):
        with c.websocket_connect("/ws/stream", headers={"Origin": "http://evil.example"}) as ws:
            ws.receive_json()


def test_control_checks_heatmap_import_jobs(local):
    e, c = local
    assert c.post("/api/v1/connect", json={"backend": BACKEND}).status_code == 200
    a, b = {"X-Client-Id": "a"}, {"X-Client-Id": "b"}
    assert c.post("/api/v1/control", json={"action": "acquire"}, headers=a).status_code == 200
    assert c.post("/api/v1/heatmap/clear", headers=b).status_code == 409
    assert c.post("/api/v1/heatmap/autosave", json={"active": True}, headers=b).status_code == 409
    assert c.put("/api/v1/heatmap", json={"buffer_size": 20}, headers=b).status_code == 409
    assert c.post("/api/v1/calibration/profiles/import", json={}, headers=b).status_code == 409
    assert c.post("/api/calibration/profile/import", json={}).status_code == 409
    assert c.post("/api/v1/control", json={"action": "release"}, headers=a).status_code == 200
    # jobs: only the owner may continue / cancel remotely
    r = c.post("/api/v1/series", json={"count": 50, "interval_s": 0.5, "frames": 1},
               headers=a)
    jid = r.json()["job"]["id"]
    assert c.post(f"/api/v1/jobs/{jid}/cancel", headers=b).status_code == 409
    assert c.post(f"/api/v1/jobs/{jid}/cancel", headers=a).status_code == 200
    e.jobs.wait(jid, 10, until_waiting=False)
    assert e.jobs.get(jid).status == "cancelled"


def test_local_lease_not_breakable_remotely(local):
    e, c = local
    e.acquire_control(LOCAL, "calibration dialog")
    r = c.post("/api/v1/control", json={"action": "acquire", "force": True},
               headers={"X-Client-Id": "x"})
    assert r.status_code == 409
    r = c.post("/api/v1/control", json={"action": "release", "force": True},
               headers={"X-Client-Id": "x"})
    assert r.status_code == 409
    assert e.control_info()["owner"] == LOCAL
    e.release_control(LOCAL)
    assert e.control_info()["owner"] is None


def test_hub_frame_flag_recovers(local):
    e, c = local
    hub = c.app.state.hub
    hub.loop = None                          # schedule impossible
    from engine import EngineEvent
    hub._on_event(EngineEvent("frame", None))
    assert hub._frame_pending is False


def test_gui_revoke_releases_remote_lease(isolated_config):
    pytest.importorskip("PyQt6")
    import os
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PyQt6.QtWidgets import QApplication
    app = QApplication.instance() or QApplication([])     # keep a reference
    from gui_remote import RemotePanel
    e = Engine()
    p = RemotePanel(e)
    try:
        e.acquire_control("remote:a")
        p.update_control(e.control_info())
        assert p.btn_revoke.isEnabled()
        p.btn_revoke.click()
        assert e.control_info()["owner"] is None
    finally:
        p.deleteLater()
        app.processEvents()
        e.close()


def test_engine_force_rules():
    e = Engine()
    try:
        e.acquire_control(LOCAL)
        with pytest.raises(ControlBusy):
            e.acquire_control("remote:x", force=True)
        e.acquire_control(LOCAL, force=True)
        e.release_control(LOCAL)
        e.acquire_control("remote:x")
        e.acquire_control(LOCAL, force=True)          # desktop may take over
        assert e.control_info()["owner"] == LOCAL
        e.release_control(LOCAL)
        time.sleep(0)
    finally:
        e.close()

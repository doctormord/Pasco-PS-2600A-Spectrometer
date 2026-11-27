import os
import socket

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
import pytest

pytest.importorskip("PyQt6")
from PyQt6.QtWidgets import QApplication, QMessageBox

from app_config import Config
import engine as engine_mod
import gui_remote
from gui_remote import RemotePanel


# qapp: the session-wide QApplication from tests/conftest.py


@pytest.fixture
def eng():
    e = engine_mod.Engine()
    yield e
    e.close()


class FakeRunner:
    instances = []
    fail = False

    def __init__(self, engine, settings):
        self.settings = settings
        self.running = False
        self.starts = self.stops = 0
        self.url = f"http://127.0.0.1:{settings.port}"
        self.local_url = self.url
        FakeRunner.instances.append(self)

    def start(self):
        if FakeRunner.fail:
            raise OSError("port busy")
        self.starts += 1
        self.running = True

    def stop(self):
        self.stops += 1
        self.running = False


@pytest.fixture
def panel(qapp, eng, monkeypatch):
    FakeRunner.instances = []
    FakeRunner.fail = False
    monkeypatch.setattr(QMessageBox, "warning", lambda *a, **k: None)
    monkeypatch.setattr(QMessageBox, "critical", lambda *a, **k: None)
    p = RemotePanel(eng, runner_factory=FakeRunner)
    yield p
    p.shutdown()
    p.deleteLater()


def test_start_stop_and_locking(panel):
    states = []
    panel.server_state_changed.connect(lambda r, u: states.append((r, u)))
    assert panel.runner is None and not Config.get("remote_enabled", False)
    panel.switch.setChecked(True)
    assert panel.runner.running
    assert panel.runner.settings.host == "127.0.0.1"
    assert not panel.spin_port.isEnabled()
    assert not panel.combo_access.isEnabled()
    assert Config.get("remote_enabled") is True
    assert panel.btn_copy_url.isEnabled()
    assert states[-1][0] is True and "127.0.0.1" in states[-1][1]
    panel.switch.setChecked(False)
    assert panel.runner is None
    assert panel.spin_port.isEnabled() and panel.combo_access.isEnabled()
    assert Config.get("remote_enabled") is False
    assert states[-1] == (False, "")
    assert panel.lbl_url.full_text() == "—"


def test_token_created_and_lan_settings(panel):
    panel.combo_access.setCurrentIndex(1)
    panel.spin_port.setValue(8123)
    assert Config.get("remote_lan") is True and Config.get("remote_port") == 8123
    assert not panel.switch_token_local.isEnabled()
    assert panel.start_server()
    s = panel.runner.settings
    assert s.host == "0.0.0.0" and s.port == 8123 and s.require_token
    assert s.token and s.token == Config.get("remote_token")
    assert gui_remote.LAN_HINT in panel.lbl_lan_hint.text()


def test_renew_restarts(panel, monkeypatch):
    panel.start_server()
    old = Config.get("remote_token")
    first = panel.runner
    monkeypatch.setattr(QMessageBox, "question",
                        lambda *a, **k: QMessageBox.StandardButton.Yes)
    panel.renew_token()
    new = Config.get("remote_token")
    assert new and new != old
    assert first.stops == 1
    assert panel.runner is not first and panel.runner.running
    assert panel.runner.settings.token == new


def test_renew_declined(panel, monkeypatch):
    panel.start_server()
    old = Config.get("remote_token")
    monkeypatch.setattr(QMessageBox, "question",
                        lambda *a, **k: QMessageBox.StandardButton.No)
    panel.renew_token()
    assert Config.get("remote_token") == old and panel.runner.stops == 0


def test_cancel_and_revoke(panel, eng, monkeypatch):
    calls = []
    monkeypatch.setattr(eng.jobs, "cancel", lambda jid: calls.append(jid))
    panel.update_control({"owner": "local", "reason": "x", "since": 0,
                          "job_id": "j1", "lease": None})
    assert not panel.btn_cancel.isEnabled()
    panel.update_control({"owner": "agent-1", "reason": "measure", "since": 0,
                          "job_id": "j1", "lease": "agent-1"})
    assert panel.btn_cancel.isEnabled() and panel.btn_revoke.isEnabled()
    assert "agent-1 (measure)" in panel.lbl_control.full_text()
    panel.btn_cancel.click()
    assert calls == ["j1"]
    panel.update_job({"kind": "measure", "status": "waiting_for_user",
                      "progress": 0.5, "prompt": "Place sample"})
    assert "50 %" in panel.lbl_job.full_text() and "Place sample" in panel.lbl_job.full_text()
    panel.btn_revoke.click()
    assert eng.control_info()["lease"] is None
    assert not panel.btn_revoke.isEnabled()


def test_start_failure_resets_switch(panel):
    FakeRunner.fail = True
    panel.switch.setChecked(True)
    assert not panel.switch.isChecked()
    assert panel.runner is None
    assert not Config.get("remote_enabled", False)


def test_autostart(panel):
    panel.autostart_if_enabled()
    assert panel.runner is None
    Config.set("remote_enabled", True)
    panel.autostart_if_enabled()
    assert panel.runner.running and panel.switch.isChecked()


def test_real_runner(qapp, eng):
    import httpx
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    p = RemotePanel(eng)
    p.spin_port.setValue(port)
    try:
        assert p.start_server()
        r = httpx.get(f"http://127.0.0.1:{port}/api/v1/status", timeout=5)
        assert r.status_code == 200
        p.stop_server()
        with socket.socket() as s:
            s.bind(("127.0.0.1", port))
    finally:
        p.shutdown()

"""Phase 2 acceptance (docs/PLAN_api_mcp.md): desktop app with the Demo
backend, Remote access switched on, measuring over REST; the REST live
spectrum equals what the GUI shows (same engine frame)."""
from __future__ import annotations

import os
import socket
import time

import numpy as np
import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PyQt6")
httpx = pytest.importorskip("httpx")

BACKEND = "Demo (virtual)"


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _pump(app, sec):
    end = time.time() + sec
    while time.time() < end:
        app.processEvents()
        time.sleep(0.01)


def test_desktop_remote_measure_matches_gui(isolated_config):
    from PyQt6.QtWidgets import QApplication
    app = QApplication.instance() or QApplication([])
    isolated_config._data.update(remote_port=_free_port(), demo_fullscale_ms=100.0,
                                 dark_correction=True, hot_pixel_filter=True)
    from gui_dashboard import DashboardWindow
    w = DashboardWindow()
    try:
        w.combo_backend.setCurrentText(BACKEND)
        w.refresh_device_list()
        w.connect_device()
        _pump(app, 1.0)
        assert w.engine.is_connected
        assert w.remote_panel.start_server()
        base = w.remote_panel.runner.local_url

        # measure over REST
        r = httpx.post(base + "/api/v1/measure", json={"frames": 2}, timeout=30)
        assert r.status_code == 200, r.text
        m = r.json()["measurement"]
        assert m["device"]["backend"] == BACKEND and m["acquisition"]["frames"] == 2

        # freeze the live view, then compare REST spectrum vs GUI display
        w.set_measurement_paused(True)
        _pump(app, 0.8)
        seq = w.engine.last_frame.seq
        _pump(app, 0.3)
        assert w.engine.last_frame.seq == seq            # really paused
        s = httpx.get(base + "/api/v1/spectrum/latest", timeout=10).json()
        assert s["seq"] == seq
        gui = np.asarray(w.current_averaged_pixels, float)
        rest = np.asarray([np.nan if v is None else v for v in s["values"]], float)
        assert rest.shape == gui.shape
        assert np.allclose(rest, gui, equal_nan=True)

        # a remote lease locks the local controls
        httpx.post(base + "/api/v1/control", json={"action": "acquire"}, timeout=10)
        _pump(app, 0.3)
        assert w._remote_locked and not w.control_widget.isEnabled()
        httpx.post(base + "/api/v1/control", json={"action": "release"}, timeout=10)
        _pump(app, 0.3)
        assert not w._remote_locked
    finally:
        w.close()
        _pump(app, 0.2)

"""Point 18: ONE saturation warning threshold (config key saturation_warn_frac)
for the exposure meter in the desktop and web UI and for the calibration
workflows."""
from __future__ import annotations

import os
import re
import time

import numpy as np
import pytest

import calibration_workflow as cw
import processing

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FS, SAT = 65535.0, 65000.0


def _lvl(pct, **kw):
    raw = np.full(64, 100.0)
    raw[10] = pct / 100.0 * FS
    return processing.exposure_level(raw, full_scale=FS, saturation_level=SAT, **kw)


def test_states_at_the_threshold(isolated_config):
    isolated_config._data["saturation_warn_frac"] = 0.90
    assert _lvl(89.9)["state"] == "ok"
    assert _lvl(90.0)["state"] == "warn"
    assert _lvl(90.0)["warn_pct_fs"] == pytest.approx(90.0)
    assert _lvl(99.0)["state"] == "warn"                 # no second, fixed limit at 98 %
    assert _lvl(99.9)["state"] == "danger"               # at the saturation level
    assert _lvl(50.0, warn_frac=0.4)["state"] == "warn"  # explicit value wins


def test_config_change_takes_effect(isolated_config):
    isolated_config._data["saturation_warn_frac"] = 0.90
    assert _lvl(87.0)["state"] == "ok"
    isolated_config._data["saturation_warn_frac"] = 0.85
    e = _lvl(87.0)
    assert e["state"] == "warn" and e["warn_frac"] == pytest.approx(0.85)
    # out-of-range values are clamped, garbage falls back to the default
    isolated_config._data["saturation_warn_frac"] = 5
    assert processing.saturation_warn_frac() == 1.0
    isolated_config._data["saturation_warn_frac"] = "x"
    assert processing.saturation_warn_frac() == pytest.approx(0.90)


def test_target_frac_is_limited_to_the_warning_threshold():
    cfg = cw.WorkflowConfig(target_frac=(0.70, 0.95), saturation_warn_frac=0.85)
    assert cfg.target_frac[1] <= 0.85 and cfg.target_frac[0] < cfg.target_frac[1]
    cfg = cw.WorkflowConfig(target_frac=(0.70, 0.85), saturation_warn_frac=0.90)
    assert cfg.target_frac == (0.70, 0.85)


def test_app_js_has_no_fixed_warning_threshold():
    with open(os.path.join(ROOT, "static", "app.js"), encoding="utf-8") as f:
        js = f.read()
    body = js.split("function updateExposure()", 1)[1].split("function onFrame", 1)[0]
    assert "ex.state" in body and "warn_pct_fs" in body
    assert not re.search(r"pct\s*>=\s*(9[0-9]|100|8[0-9])\b", body)
    assert "pct >= 90" not in js


def test_gui_dashboard_has_no_fixed_warning_threshold():
    with open(os.path.join(ROOT, "gui_dashboard.py"), encoding="utf-8") as f:
        src = f.read()
    body = src.split("def _update_exposure", 1)[1].split("def _build_raw_axis", 1)[0]
    assert 'exp.get("state")' in body and "warn_pct_fs" in body
    assert "85.0" not in body and "98.0" not in body


# ── Desktop and web show the same state for the same raw peak ────────────────
def _web_state(isolated_config, pct):
    """The web client's decision (static/app.js updateExposure) replayed in
    Python from the frame payload's exposure dict, plus the payload itself
    from a Demo engine frame."""
    ex = _lvl(pct)
    assert set(ex) >= {"state", "warn_pct_fs", "warn_frac"}
    return ex["state"], ex["warn_pct_fs"]


@pytest.fixture(scope="module")
def qapp():
    pytest.importorskip("PyQt6")
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PyQt6.QtWidgets import QApplication
    return QApplication.instance() or QApplication([])


def _desktop_state(pct):
    """Run gui_dashboard._update_exposure on a stub window and read the class."""
    pytest.importorskip("PyQt6")
    pytest.importorskip("pyqtgraph")
    import types
    import gui_dashboard as gd

    class Lbl:
        text = ""
        def setText(self, t): self.text = t
        def setToolTip(self, t): self.tip = t
        def setStyleSheet(self, s): pass
        def setVisible(self, v): pass

    class Bar(Lbl):
        def setValue(self, v): self.v = v

    w = types.SimpleNamespace(lbl_exposure=Lbl(), bar_exposure=Bar(), _exposure_cls=None,
                              _show_exposure=lambda on: None,
                              _update_raw_curve=lambda ev: None)
    ev = types.SimpleNamespace(display_mode="corrected", exposure=_lvl(pct))
    cls_holder = {}
    orig = w.bar_exposure.setStyleSheet
    w.bar_exposure.setStyleSheet = lambda s: cls_holder.setdefault("css", s)
    gd.DashboardWindow._update_exposure(w, ev)
    return w._exposure_cls, w.lbl_exposure.tip


def test_desktop_and_web_agree(qapp, isolated_config):
    for warn, expected in ((0.90, "ok"), (0.85, "warn")):
        isolated_config._data["saturation_warn_frac"] = warn
        web_state, web_pct = _web_state(isolated_config, 87.0)
        desk_state, tip = _desktop_state(87.0)
        assert web_state == desk_state == expected, (warn, web_state, desk_state)
        assert f"{warn * 100:.0f} % FS" in tip
        assert web_pct == pytest.approx(warn * 100)


def test_web_frame_message_carries_state_and_threshold(isolated_config):
    pytest.importorskip("fastapi")
    pytest.importorskip("httpx")
    from fastapi.testclient import TestClient
    import engine as eng
    from api import ServerSettings, create_app
    isolated_config._data.update({"saturation_warn_frac": 0.85, "demo_spectrum": "cal_lamp",
                                  "demo_sensitivity": "synthetic", "demo_fullscale_ms": 100.0})
    e = eng.Engine()
    try:
        with TestClient(create_app(e, ServerSettings())) as c:
            assert c.post("/api/v1/connect", json={"backend": "Demo (virtual)"}).status_code == 200
            t0 = time.time()
            while e.last_frame is None and time.time() - t0 < 8:
                time.sleep(0.1)
            msg = None
            with c.websocket_connect("/ws/stream") as ws:
                t0 = time.time()
                while time.time() - t0 < 8:
                    m = ws.receive_json()
                    if m.get("type") == "frame":
                        msg = m
                        break
            assert msg is not None
            ex = msg["exposure"]
            assert ex["warn_pct_fs"] == pytest.approx(85.0)
            assert ex["state"] in ("ok", "warn", "danger")
            pct = ex["peak_pct_fs"]
            if ex["state"] != "danger":
                assert (ex["state"] == "warn") == (pct >= 85.0)
    finally:
        e.close()

"""Web UI for display modes (hardware test points 9-12): selector, display
payload, exposure bar, raw curve, per-mode limits, readouts. Static checks of
static/ plus TestClient round trips on the Demo backend."""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import time

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("httpx")
from fastapi.testclient import TestClient

import engine as eng
from api import ServerSettings, create_app

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STATIC = os.path.join(ROOT, "static")
V1 = "/api/v1"
BACKEND = "Demo (virtual)"


def _read(name):
    with open(os.path.join(STATIC, name), encoding="utf-8") as f:
        return f.read()


@pytest.mark.skipif(shutil.which("node") is None, reason="node not in PATH")
def test_app_js_syntax():
    r = subprocess.run(["node", "--check", os.path.join(STATIC, "app.js")],
                       capture_output=True, text=True)
    assert r.returncode == 0, r.stderr


def test_html_elements():
    html = _read("index.html")
    for i in ("scope-display-mode", "btn-raw-curve", "exposure-bar", "exposure-text",
              "exposure-fill", "hm-ymax-auto", "hm-ymax", "y-unit-lbl"):
        assert f'id="{i}"' in html, i
    assert "65000" not in html  # no fixed ADC ceiling on the Y max field


def test_scope_plots_display_values_and_keeps_evaluations_on_intensities():
    js = _read("app.js")
    assert "State.scopeData[1] = disp;" in js
    assert "msg.display" in js and "display_unit" in js and "display_modes" in js
    assert "DisplayMode.onFrame(msg)" in js
    # colour / filter / CSV keep the corrected data
    assert "Filter.onFrame(msg.wavelengths, msg.intensities)" in js
    assert "State.lastFrame.intensities" in js


def test_selector_uses_acquisition_endpoint_and_disables_unavailable():
    js = _read("app.js")
    assert "/api/v1/acquisition" in js and "display_mode: v" in js
    assert "o.disabled = m.available === false" in js
    assert "(b.value === 'auto') - (a.value === 'auto')" in js  # Auto first


def test_exposure_bar_and_raw_axis():
    js = _read("app.js")
    for tok in ("peak_pct_fs", "% FS", "n_saturated", "'exposure-bar '", "SI.RAW", "scale: 'raw'",
                "rawRange", "scope_raw_curve"):
        assert tok in js, tok
    css = _read("styles.css")
    assert ".exposure-bar.warn" in css and ".exposure-bar.danger" in css
    assert "var(--danger)" in css.split("Display modes", 1)[1]


def test_per_mode_limits():
    js = _read("app.js")
    for tok in ("y_limits_by_mode", "heatmap_limits_by_mode", "_applyingMode", "HmLimits"):
        assert tok in js, tok
    assert "y_max_adc || 4000" not in js  # no hard-coded ADC fallback for the Y range


def test_readouts_use_dash_for_masked_pixels():
    js = _read("app.js")
    assert "'–'" in js and "fmtUnit" in js
    assert "toFixed(0)}<span class=\"unit\"> ADC" not in js
    assert "Math.round(adc)" not in js


# ── API round trips (Demo) ───────────────────────────────────────────────

@pytest.fixture
def client(isolated_config):
    e = eng.Engine()
    try:
        with TestClient(create_app(e, ServerSettings())) as c:
            c.engine = e
            yield c
    finally:
        e.close()


def _connect(c):
    assert c.post(f"{V1}/connect", json={"backend": BACKEND}).status_code == 200
    t0 = time.time()
    while c.engine.last_frame is None and time.time() - t0 < 8:
        time.sleep(0.1)
    assert c.engine.last_frame is not None


def _frame(c):
    with c.websocket_connect("/ws/stream") as ws:
        t0 = time.time()
        while time.time() - t0 < 8:
            m = ws.receive_json()
            if m.get("type") == "frame":
                return m
    raise AssertionError("no frame")


def test_frame_payload_has_display_fields(client):
    _connect(client)
    m = _frame(client)
    for k in ("display", "display_unit", "display_unit_label", "display_mode",
              "display_modes", "raw", "exposure", "intensities", "unit"):
        assert k in m, k
    assert len(m["display"]) == len(m["intensities"])
    assert any(x["value"] == "auto" for x in m["display_modes"])
    assert all({"value", "label", "available"} <= set(x) for x in m["display_modes"])


def test_put_display_mode_raw(client):
    _connect(client)
    st = client.get(f"{V1}/status").json()
    assert "display_modes" in st and "display_mode_effective" in st
    r = client.put(f"{V1}/acquisition", json={"display_mode": "raw"})
    assert r.status_code == 200, r.text
    t0 = time.time()
    m = _frame(client)
    while m.get("display_mode") != "raw" and time.time() - t0 < 6:
        m = _frame(client)
    assert m["display_mode"] == "raw"
    assert client.get(f"{V1}/status").json()["display_mode"] == "raw"


def test_limit_keys_accepted_via_set_config(client):
    lim = {"raw": {"auto": False, "min": None, "max": 1234}}
    with client.websocket_connect("/ws/stream") as ws:
        ws.send_json({"type": "set_config", "payload": {
            "y_limits_by_mode": lim, "heatmap_limits_by_mode": {"raw": {"auto": False, "max": 999}}}})
        t0 = time.time()
        while time.time() - t0 < 3 and client.engine.cfg.get("y_limits_by_mode") != lim:
            time.sleep(0.05)
    assert client.engine.cfg.get("y_limits_by_mode") == lim
    assert client.engine.cfg.get("heatmap_limits_by_mode") == {"raw": {"auto": False, "max": 999}}

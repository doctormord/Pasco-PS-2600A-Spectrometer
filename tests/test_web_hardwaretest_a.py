"""Web UI for hardware test points 3-6: calibration run result view (checks,
dark drift, hints, verification), linearity panel, scope reference overlay.
Static checks of static/ plus API round trips on the Demo backend."""
from __future__ import annotations

import os
import re
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


# ── static checks ────────────────────────────────────────────────────────

def test_html_elements_exist():
    html = _read("index.html")
    for i in ("btn-refoverlay", "scope-ref-select", "lin-open", "lin-dialog", "lin-state",
              "lin-factory-note", "lin-table", "lin-off", "lin-start", "lin-cancel",
              "lin-result", "lin-int-mode", "lin-steps", "lin-frames"):
        assert f'id="{i}"' in html, i
    assert "Run linearity calibration" in html


def test_modules_defined_and_initialised():
    js = _read("app.js")
    defined = set(re.findall(r"^(?:const|let|var|function|async function)\s+([\w$]+)", js, re.M))
    assert {"Lin", "RefOverlay"} <= defined
    assert "Lin.init();" in js and "RefOverlay.init();" in js


def test_endpoints_referenced():
    js = _read("app.js")
    assert "/api/v1/calibration/linearity" in js
    assert "${L}/runs" in js and "${L}/select" in js
    assert "/protocol?format=" in js
    assert "/api/v1/calibration/verifications" in js
    assert "/api/v1/calibration/verification'" in js
    assert "/api/v1/calibration/reference-overlay" in js
    assert "/api/v1/calibration/references" in js


def test_run_result_view_fields():
    js = _read("app.js")
    for token in ("stability_fail_pixels", "itime_fail_pixels", "stability_cause", "itime_cause",
                  "causes", "dark_drift", "pct_of_peak", "hints", "verification_error",
                  "max_abs_pct", "mean_pct", "rms_pct", "Verify again", "data-verify-again"):
        assert token in js, token


def test_linearity_states_and_factory_note():
    js = _read("app.js")
    for st in ("'active'", "'off'", "'factory'"):
        assert f"case {st}" in js
    for res in ("check_saved", "computed", "saved"):
        assert res in js
    assert "no correction" in js.lower()


def test_overlay_persists_config_keys_and_has_series():
    js = _read("app.js")
    assert "scope_reference_overlay" in js and "scope_reference_id" in js
    assert re.search(r"s\[SI\.SRC\]\s*=", js) and "dash:" in js.split("s[SI.SRC]")[1][:200]
    assert "MIN_GAP_MS" in js


def test_styles_use_theme_variables_only():
    css = _read("styles.css")
    block = css.split("Hardware-test additions", 1)[1]
    assert "var(--" in block
    assert not re.search(r"#[0-9a-fA-F]{3,6}\b", block), "colours must come from theme variables"


# ── API round trips (Demo backend) ───────────────────────────────────────

@pytest.fixture
def client(isolated_config):
    e = eng.Engine()
    try:
        with TestClient(create_app(e, ServerSettings())) as c:
            c.engine = e
            yield c
    finally:
        e.close()


def test_linearity_state_and_select_without_device(client):
    r = client.get(f"{V1}/calibration/linearity")
    assert r.status_code == 200
    j = r.json()
    assert {"device", "serial", "state", "active_id", "entries"} <= set(j)
    assert j["entries"] == []


def test_overlay_404_before_frame_200_after_connect(client):
    assert client.get(f"{V1}/calibration/reference-overlay?reference_id=demo").status_code == 404
    r = client.post(f"{V1}/connect", json={"backend": BACKEND})
    assert r.status_code == 200, r.text
    t0 = time.time()
    while client.engine.last_frame is None and time.time() - t0 < 8:
        time.sleep(0.1)
    assert client.engine.last_frame is not None
    refs = client.get(f"{V1}/calibration/references").json()
    assert refs
    import reference_sources as rs
    rid = rs.DEMO_SOURCE_ID
    r = client.get(f"{V1}/calibration/reference-overlay?reference_id={rid}")
    assert r.status_code == 200, r.text
    j = r.json()
    assert len(j["wavelengths"]) == len(j["values"]) > 0 and "unit" in j
    assert client.get(f"{V1}/calibration/reference-overlay?reference_id=nope").status_code == 404
    lin = client.get(f"{V1}/calibration/linearity").json()
    assert lin["device"] and "state" in lin


def test_overlay_config_keys_accepted(client):
    with client.websocket_connect("/ws/stream") as ws:
        ws.send_json({"type": "set_config",
                      "payload": {"scope_reference_overlay": True, "scope_reference_id": "x"}})
        t0 = time.time()
        while time.time() - t0 < 3:
            if client.engine.cfg.get("scope_reference_overlay") is True:
                break
            time.sleep(0.05)
    assert client.engine.cfg.get("scope_reference_overlay") is True
    assert client.engine.cfg.get("scope_reference_id") == "x"


def test_verification_without_result_is_404(client):
    assert client.get(f"{V1}/calibration/verification").status_code == 404

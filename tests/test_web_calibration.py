"""Web API for spectral calibration v2: /api/calibration/*, unit-aware frame
payload and CSV export. Runs against the Demo backend through TestClient."""
from __future__ import annotations

import json
import time

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("httpx")
from fastapi.testclient import TestClient

import calibration_profiles as cp
import calibration_workflow as cw
import reference_sources as rs
import web_server as ws

BACKEND = "Demo (virtual)"


@pytest.fixture
def client(isolated_config):
    ws._disconnect()
    c = TestClient(ws.app)
    yield c
    ws._disconnect()


def _connect(client, cfg, serial="DEMO-0001"):
    cfg._data.update({"demo_spectrum": "cal_lamp", "demo_sensitivity": "synthetic",
                      "demo_fullscale_ms": 100.0, "demo_serial": serial})
    r = client.post("/api/connect", json={"backend": BACKEND})
    assert r.status_code == 200, r.text


def _make_profile():
    wf = cw.CalibrationWorkflow(
        ws.state.acquisition, BACKEND, kind="absolute",
        optics={"name": "Web CC", "input": "CC"},
        reference=rs.ReferenceLibrary().get(rs.DEMO_SOURCE_ID),
        cfg=cw.WorkflowConfig(n_frames=8, stability_delay_s=0.1, smooth_px=5))
    wf.run_all()
    return wf.saved[0]


def _wait_frame(pred, timeout=8.0):
    t0 = time.time()
    while time.time() - t0 < timeout:
        p = ws._build_frame_payload()
        if pred(p):
            return p
        time.sleep(0.1)
    raise AssertionError("expected frame never arrived")


def test_endpoints_without_device(client):
    r = client.get("/api/calibration/profiles")
    assert r.status_code == 200
    j = r.json()
    assert j["other_units"] == [] and [i["value"] for i in j["items"]] == ["None"]
    assert j["items"][0]["state"] == "builtin"
    assert client.get("/api/calibration/darks").json() == []
    assert client.get("/api/calibration/status").status_code == 200
    refs = client.get("/api/calibration/references").json()
    assert any(x["id"] == rs.DEMO_SOURCE_ID and x["builtin"] for x in refs)
    assert client.get("/api/calibration/profile/export?id=nope").status_code == 404
    assert client.post("/api/calibration/profile/delete", json={}).status_code == 400
    assert client.get("/api/export/csv").status_code == 200
    p = ws._build_frame_payload()
    assert p["unit"] == "counts" and p["unit_label"] == "counts"


def test_v2_profile_lifecycle(client, isolated_config):
    _connect(client, isolated_config)
    # no profile: counts, no nulls, classic CSV header
    p0 = _wait_frame(lambda p: len(p["intensities"]) > 0)
    assert p0["unit"] == "counts" and None not in p0["intensities"]
    csv0 = client.get("/api/export/csv").text
    # header documents the applied settings (hardware test point 8), no legacy
    # "Response compensation" entry without an active legacy gain
    assert "ADC_Counts" in csv0 and "# Unit;counts" in csv0
    assert "Response compensation" not in csv0

    prof = _make_profile()
    j = client.get("/api/calibration/profiles").json()
    assert j["serial"] == "DEMO-0001"
    it = {i["value"]: i for i in j["items"]}
    assert prof["id"] in it and it[prof["id"]]["state"] == cp.STATE_OK
    assert it[prof["id"]]["label"] == cp.profile_label(prof)

    r = client.post("/api/calibration/select", json={"selection": prof["id"]})
    assert r.status_code == 200
    assert r.json()["status"]["state"] == cp.STATE_OK
    lo, hi = prof["valid_range_nm"]
    p = _wait_frame(lambda p: p["unit"] == "uW/cm2/nm")
    assert p["unit_label"] == "µW/(cm²·nm)"
    assert p["valid_range_nm"] == [pytest.approx(lo), pytest.approx(hi)]
    assert p["calibration"]["profile_id"] == prof["id"]
    wl, v = p["wavelengths"], p["intensities"]
    assert any(x is None for x in v)
    assert all(x is None for w, x in zip(wl, v) if w < lo - 1 or w > hi + 1)
    assert any(x is not None and x > 0 for x in v)
    json.dumps(p, allow_nan=False)

    # status + CSV
    assert client.get("/api/calibration/status").json()["unit"] == "uW/cm2/nm"
    csv = client.get("/api/export/csv").text
    assert "# Unit;uW/cm2/nm" in csv and "# Calibration profile;" in csv
    assert "# Valid range (nm);" in csv and "Value_uW/cm2/nm" in csv
    assert ";;" in csv   # masked value = empty cell (Raw_ADC column follows)

    # irradiance unit change refreshes the applier
    client.post("/api/config", json={"irradiance_unit": "W/m2/nm"})
    assert client.get("/api/calibration/status").json()["unit"] == "W/m2/nm"
    client.post("/api/config", json={"irradiance_unit": "uW/cm2/nm"})

    # export / import round trip
    ex = client.get(f"/api/calibration/profile/export?id={prof['id']}")
    assert "attachment" in ex.headers["content-disposition"]
    imp = client.post("/api/calibration/profile/import", json=ex.json())
    assert imp.status_code == 200 and imp.json()["id"] != prof["id"]
    assert client.post("/api/calibration/profile/import", json={"x": 1}).status_code == 400

    # delete the active profile -> falls back to None
    r = client.post("/api/calibration/profile/delete", json={"id": prof["id"]})
    assert r.json()["ok"] and r.json()["selection"] == "None"
    p = _wait_frame(lambda p: p["unit"] == "counts")
    assert None not in p["intensities"]


def test_fingerprint_mismatch_needs_confirmation(client, isolated_config):
    _connect(client, isolated_config)
    prof = _make_profile()
    stored = cp.get_profile_by_id(prof["id"])
    reg = cp.load_registry()
    for q in reg["profiles"]:
        if q["id"] == prof["id"]:
            q["wl_fingerprint"] = "deadbeef-not-matching"
    cp.save_registry(reg)
    r = client.post("/api/calibration/select", json={"selection": prof["id"]})
    assert r.status_code == 409
    assert r.json()["needs_confirmation"] and r.json()["message"]
    r = client.post("/api/calibration/select",
                    json={"selection": prof["id"], "confirm": True})
    assert r.status_code == 200
    assert r.json()["status"]["state"] == cp.STATE_WL_CHANGED
    assert not r.json()["status"].get("needs_confirmation")
    assert stored is not None
    r = client.post("/api/calibration/select", json={"selection": "bogus"})
    assert r.status_code == 400


def test_copy_to_other_serial(client, isolated_config):
    _connect(client, isolated_config, serial="DEMO-0001")
    prof = _make_profile()
    client.post("/api/disconnect")
    _connect(client, isolated_config, serial="DEMO-0002")
    j = client.get("/api/calibration/profiles?include_old=1").json()
    assert j["serial"] == "DEMO-0002"
    assert prof["id"] not in [i["value"] for i in j["items"]]
    assert prof["id"] in [o["id"] for o in j["other_units"]]
    assert j["other_units"][0]["serial"] == "DEMO-0001"
    r = client.post("/api/calibration/profile/copy", json={"id": prof["id"]})
    assert r.status_code == 200
    new_id = r.json()["id"]
    j = client.get("/api/calibration/profiles").json()
    assert new_id in [i["value"] for i in j["items"]]
    assert client.post("/api/calibration/profile/copy",
                       json={"id": "missing"}).status_code == 404

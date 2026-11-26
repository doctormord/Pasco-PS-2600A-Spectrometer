"""Derivation job over /api/v1/calibration/derivations on the Demo backend:
per-side integration times, the complete user-step sequence, source drift
check, protocol download and the legacy integration_ms parameter."""
from __future__ import annotations

import os
import time

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("httpx")
from fastapi.testclient import TestClient

import calibration_report as crp
import calibration_workflow as cw
import engine as eng
import reference_sources as rs
from api import ServerSettings, create_app

BACKEND = "Demo (virtual)"
V1 = "/api/v1"
FINAL = ("done", "failed", "cancelled")
NEW_SCALE = 1e-3                     # the new optics collect 1000x less light

EXPECTED_ACTIONS = ["mount_known", "cover", "uncover", "swap_optics", "cover", "uncover",
                    "cover", "swap_back", "cover", "uncover"]


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
        "calibration_capture_frames": 4, "calibration_stability_delay_s": 0.1,
        "demo_spectrum": "cal_lamp", "demo_sensitivity": "synthetic",
        "demo_fullscale_ms": 1.0, "demo_min_integration_us": 10,
        "demo_max_integration_us": 2_000_000,
        "demo_read_noise": 2.0,
    })
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


def _known_profile(engine) -> str:
    b = engine.acquisition
    wf = cw.CalibrationWorkflow(
        b, BACKEND, kind="relative", optics={"name": "Known CC", "input": "CC"},
        reference=rs.ReferenceLibrary().get(rs.DEMO_SOURCE_ID),
        cfg=cw.WorkflowConfig(n_frames=4, stability_delay_s=0.1, smooth_px=5))
    wf.run_all()
    return wf.saved[0]["id"]


def _run(c, body, *, back_scale=1.0, timeout=240.0):
    """Start the job and play the user: cover / uncover the demo source,
    change its scale at swap_optics (new optics) and swap_back."""
    b = c.engine.acquisition
    b.SUPPORTS_SHUTTER = False                        # user steps cover / uncover
    r = c.post(f"{V1}/calibration/derivations", json=body)
    assert r.status_code == 202, r.text
    jid = r.json()["job"]["id"]
    seen, prompts = [], []
    deadline = time.monotonic() + timeout
    j = {}
    while time.monotonic() < deadline:
        j = c.get(f"{V1}/jobs/{jid}", params={"wait_s": 0.3}).json()
        if j["status"] == "waiting_for_user":
            a = j["prompt"]["action"]
            seen.append(a)
            prompts.append(j["prompt"].get("message", ""))
            if a == "swap_optics":
                b.source_scale = NEW_SCALE
            elif a in ("mount_known", "swap_back"):
                b.source_scale = 1.0 if a == "mount_known" else back_scale
            b.set_shutter(a == "cover")
            assert c.post(f"{V1}/jobs/{jid}/continue").status_code == 200
            time.sleep(0.1)
        elif j["status"] in FINAL:
            break
    b.source_scale = 1.0
    b.set_shutter(False)
    return jid, j, seen, prompts


def _body(known, **kw):
    return {"known_profile_id": known, "optics": {"name": "Bare derived", "input": "bare"},
            **kw}


def test_derivation_job_two_integration_times_saved(c):
    known = _known_profile(c.engine)
    jid, j, seen, prompts = _run(c, _body(known))
    assert j["status"] == "done", j
    assert seen == EXPECTED_ACTIONS
    assert cw.DERIVATION_SOURCE_HINT in prompts[0]
    res = j["result"]
    assert res["status"] == "saved", res
    assert res["activated"] == res["profile_ids"][0]
    ratio = res["integration_new_us"] / res["integration_known_us"]
    assert 300 < ratio < 3000, ratio
    for k in ("peak_frac_known", "peak_frac_new", "peak_frac_repeat"):
        assert 0.3 < res[k] < 0.9, (k, res[k])
    d = res["drift"]
    assert d["pass"] is True and "per_pixel_pct" not in d
    assert d["threshold_pct"] == pytest.approx(2.0)
    assert res["checks"]["drift_pass"] is True
    assert res["valid_segments_nm"] and res["plausibility"]["pass"]
    assert "warnings" in res and "protocol" in res and "protocol_message" in res
    steps = {e.get("step") for e in j.get("log", []) if isinstance(e, dict)}
    assert not steps or steps <= {k for k, _ in cw.DERIVATION_STEPS} | {None}


def test_derivation_protocol_download(c):
    if not hasattr(crp, "write_derivation_protocol"):
        pytest.skip("derivation protocol not available")
    known = _known_profile(c.engine)
    jid, j, _, _ = _run(c, _body(known))
    res = j["result"]
    assert res["status"] == "saved", res
    assert any(p.lower().endswith(".csv") for p in res["protocol"]), res
    r = c.get(f"{V1}/calibration/runs/{jid}/protocol", params={"format": "csv"})
    assert r.status_code == 200, r.text
    assert r.content
    assert c.get(f"{V1}/calibration/runs/{jid}/protocol",
                 params={"format": "pdf"}).status_code in (200, 404)
    assert all(os.path.isfile(p) for p in res["protocol"])


def test_real_drift_is_reported_and_nothing_is_saved(c):
    """A source drift of -10 % between the capture and its repeat fails the
    drift check; the masked pixels leave no usable range, so nothing is saved
    (here the plausibility check reports it, status not_saved_*)."""
    known = _known_profile(c.engine)
    jid, j, seen, _ = _run(c, _body(known), back_scale=0.9)
    res = j["result"]
    assert j["status"] == "done", j
    assert res["status"] in ("not_saved_checks_failed", "not_saved_implausible"), res
    assert res["profile_ids"] == [] and "activated" not in res
    assert res["drift"]["pass"] is False and res["checks"]["drift_pass"] is False
    assert res["drift"]["n_fail_pixels"] > 0
    assert any("drift" in w.lower() for w in res["warnings"])
    assert "per_pixel_pct" not in res["drift"]


def test_failed_checks_status_and_save_on_request(c, monkeypatch):
    """Plausible result but failed checks: not saved, unless
    save_on_failed_checks (then saved and activated)."""
    monkeypatch.setattr(cw.DerivationWorkflow, "checks_passed", property(lambda self: False))
    known = _known_profile(c.engine)
    jid, j, _, _ = _run(c, _body(known))
    res = j["result"]
    assert res["status"] == "not_saved_checks_failed", res
    assert res["profile_ids"] == [] and res["drift"] is not None
    jid2, j2, _, _ = _run(c, _body(known, save_on_failed_checks=True))
    r2 = j2["result"]
    assert r2["status"] == "saved" and r2["profile_ids"], r2


def test_legacy_integration_ms_and_per_side_fixed_values(c):
    known = _known_profile(c.engine)
    # legacy integration_ms: both sides; the dark / light pairs stay consistent
    jid, j, _, _ = _run(c, _body(known, integration_ms=0.7, allow_implausible=True,
                                 save_on_failed_checks=True))
    res = j["result"]
    assert j["status"] == "done", j
    assert res["integration_known_us"] == 700
    assert res["integration_new_us"] == 700
    # side-specific values win over integration_ms
    jid, j, _, _ = _run(c, _body(known, integration_ms=0.7, integration_known_ms=0.8,
                                 integration_new_ms=300.0, allow_implausible=True,
                                 save_on_failed_checks=True))
    res = j["result"]
    assert res["integration_known_us"] == 800
    assert res["integration_new_us"] == 300_000


def test_request_validation(c):
    known = _known_profile(c.engine)
    for bad in ({"integration_known_ms": 0}, {"integration_new_ms": -1},
                {"integration_ms": 0}, {"integration_new_ms": "fast"}):
        r = c.post(f"{V1}/calibration/derivations", json=_body(known, **bad))
        assert r.status_code == 422, (bad, r.text)
    r = c.post(f"{V1}/calibration/derivations", json=_body("nope"))
    assert r.status_code == 404

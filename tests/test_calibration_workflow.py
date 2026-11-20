"""Phase 2 acceptance on the demo backend (synthetic lamp, known sensitivity):
full wizard run reproduces the given sensitivity, two runs agree within the
stability threshold, absolute run saves K + derived relative, derivation."""
from __future__ import annotations

import json

import numpy as np
import pytest

import calibration_profiles as cp
import calibration_workflow as cw
import reference_sources as rs
from calibration_utils import demo_lamp_irradiance

BACKEND = "Demo (virtual)"


@pytest.fixture
def demo(isolated_config):
    from _device_demo import DemoSpectrometer
    isolated_config._data.update({"demo_spectrum": "cal_lamp",
                                  "demo_sensitivity": "synthetic",
                                  "demo_fullscale_ms": 100.0})
    b = DemoSpectrometer(on_frame=lambda *a: None)
    b.connect("Demo Spectrometer")
    b.set_integration_time_us(20_000)
    b.start()
    yield b
    b.disconnect()


def _cfg():
    return cw.WorkflowConfig(n_frames=12, stability_delay_s=0.2, smooth_px=5)


def _ref():
    return rs.ReferenceLibrary().get(rs.DEMO_SOURCE_ID)


def _truth(wl, valid):
    from _device_demo import demo_true_sensitivity
    t = demo_true_sensitivity(wl)
    return t / t[valid].max()


def test_full_relative_run_reproduces_sensitivity_and_is_repeatable(demo):
    runs = []
    for _ in range(2):
        wf = cw.CalibrationWorkflow(demo, BACKEND, kind="relative",
                                    optics={"name": "Demo fibre", "input": "bare"},
                                    reference=_ref(), cfg=_cfg())
        rec = wf.run_all()
        runs.append((wf, rec))
        lo, hi = rec["valid_range_nm"]
        assert 380 <= lo < 400 and 760 < hi <= 780        # device spec ∩ ref ∩ SNR
        assert rec["checks"]["stability_pass"] and rec["checks"]["itime_pass"]
        assert 0.70 <= rec["acquisition"]["peak_frac"] <= 0.85
        v = wf.result["valid"]
        err = np.abs(wf.result["S"][v] / _truth(wf.result["wl"], v)[v] - 1)
        assert np.median(err) < 0.005 and np.percentile(err, 95) < 0.02
        json.dumps(rec, allow_nan=False)                   # protocol record is strict JSON

    (w1, r1), (w2, r2) = runs
    assert [p["version"] for p in w1.saved] == [1] and [p["version"] for p in w2.saved] == [2]
    m = w1.result["valid"] & w2.result["valid"]
    dev = cp.compare_rates(w1.result["S"], w2.result["S"], m)
    assert dev["p95_pct"] < w1.cfg.stability_threshold_pct


def test_absolute_run_saves_K_and_derived_relative(demo):
    wf = cw.CalibrationWorkflow(demo, BACKEND, kind="absolute",
                                optics={"name": "Demo CC", "input": "CC"},
                                reference=_ref(), cfg=_cfg())
    wf.run_all()
    ab, rel = wf.saved
    assert ab["type"] == "absolute" and ab["unit"] == "uW/cm2/nm per count/s"
    assert rel["type"] == "relative" and rel["derived_from"]["id"] == ab["id"]
    # applying K to the calibration count rate gives back the lamp
    ap = cp.CalibrationApplier(ab, wf.result["wl"])
    E = ap.apply(wf.result["C_cal"])
    v = ap.valid
    err = np.abs(E[v] / demo_lamp_irradiance(wf.result["wl"])[v] - 1)
    assert np.median(err) < 0.003 and np.percentile(err, 99) < 0.02   # raw C noise


def test_absolute_with_bare_fibre_is_refused(demo):
    with pytest.raises(ValueError):
        cw.CalibrationWorkflow(demo, BACKEND, kind="absolute",
                               optics={"name": "x", "input": "bare"}, reference=_ref())


def test_derivation_from_known_profile(demo):
    wf = cw.CalibrationWorkflow(demo, BACKEND, kind="relative",
                                optics={"name": "CC", "input": "CC"},
                                reference=_ref(), cfg=_cfg())
    wf.run_all()
    known = wf.saved[0]
    dw = cw.DerivationWorkflow(demo, BACKEND, known_profile_id=known["id"],
                               optics={"name": "bare from CC", "input": "bare"}, cfg=_cfg())
    dw.set_integration(wf.integration_us)
    dw.capture_known()
    dw.capture_new()          # same optics in the demo → S_new must equal S_known
    res = dw.compute()
    p = dw.save()
    assert p["derived_from"]["id"] == known["id"]
    m = res["valid"] & wf.result["valid"]
    assert cp.compare_rates(wf.result["S"], res["S"], m)["p95_pct"] < 1.0


def test_wavelength_check_reports_fingerprint(demo):
    chk = cw.wavelength_check(demo, BACKEND)
    assert chk["ok"] and chk["fingerprint"] == demo.wl_fingerprint()

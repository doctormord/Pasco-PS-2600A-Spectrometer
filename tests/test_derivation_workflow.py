"""DerivationWorkflow with the Demo backend: own integration time per optics,
brightness ratio 1:1000, source drift, independence of the integration times,
dark per side, spectral transmission and the configurable drift limit.

The demo parameter `source_scale` simulates the throughput of the optics; the
`swap` callback of run_all() changes it like a user changing the optics."""
from __future__ import annotations

import numpy as np
import pytest

import calibration_profiles as cp
import calibration_workflow as cw
import reference_sources as rs

BACKEND = "Demo (virtual)"
N_FRAMES = 6


@pytest.fixture
def demo(isolated_config):
    from _device_demo import DemoSpectrometer
    isolated_config._data.update({
        "demo_spectrum": "cal_lamp", "demo_sensitivity": "synthetic",
        "demo_fullscale_ms": 0.1,              # the demo clamps this to >= 1 ms
        "demo_min_integration_us": 10, "demo_max_integration_us": 2_000_000})
    b = DemoSpectrometer(on_frame=lambda *a: None)
    b.connect("Demo Spectrometer")
    b.set_integration_time_us(800)
    b.start()
    yield b
    b.disconnect()


def _cfg(**kw) -> cw.WorkflowConfig:
    base = dict(n_frames=N_FRAMES, stability_delay_s=0.1)
    base.update(kw)
    return cw.WorkflowConfig(**base)


def _known(demo, cfg=None):
    """Calibrate the known optics (scale 1) with the demo lamp; returns the profile."""
    wf = cw.CalibrationWorkflow(
        demo, BACKEND, kind="relative", optics={"name": "CC", "input": "CC"},
        reference=rs.ReferenceLibrary().get(rs.DEMO_SOURCE_ID), cfg=cfg or _cfg())
    wf.run_all()
    return wf.saved[0] if wf.saved else cp.profiles_for_device(
        BACKEND, demo.serial, demo.wl_fingerprint())[0]


def _dw(demo, prof, cfg=None, name="bare"):
    return cw.DerivationWorkflow(
        demo, BACKEND, known_profile_id=prof["id"],
        optics={"name": name, "input": "bare"}, cfg=cfg or _cfg())


def _swapper(demo, new_scale=1.0, back_scale=1.0):
    def swap(which):
        demo.source_scale = new_scale if which == "new" else back_scale
    return swap


def _stats(a, b, valid):
    d = 100.0 * np.abs(np.asarray(a)[valid] / np.asarray(b)[valid] - 1.0)
    return float(np.median(d)), float(np.percentile(d, 95))


def test_brightness_ratio_1_to_1000(demo):
    cfg = _cfg()
    prof = _known(demo, cfg)
    dw = _dw(demo, prof, cfg)
    rec = dw.run_all(swap=_swapper(demo, 1e-3, 1.0))
    demo.source_scale = 1.0
    lo, hi = cfg.target_frac
    k, n = rec["sides"]["known"], rec["sides"]["new"]
    # own integration time per optics, ~1000x apart, both auto
    assert k["auto_exposure"] is True and n["auto_exposure"] is True
    ratio = n["integration_us"] / k["integration_us"]
    assert 500 < ratio < 2000, ratio
    # no saturation, both levels in the target band
    assert k["n_saturated"] == 0 and n["n_saturated"] == 0
    for s in (k, n):
        assert lo - 0.05 <= s["peak_frac"] <= hi + 0.05, s["peak_frac"]
        assert s["itime_match"] is True
        assert s["linearity_text"]                     # on / off / n/a, never empty
    assert k["peak_frac_repeat"] is not None
    assert dw.checks["itime_match_known"] and dw.checks["itime_match_new"]
    assert rec["checks"]["drift_pass"] is True and dw.checks_passed
    # S_new equals S_known (same source, throughput only changes the level)
    res = dw.result
    valid = np.asarray(res["valid"], bool)
    med, p95 = _stats(res["S"], res["S_known"], valid)
    assert med < 0.5 and p95 < 2.0, (med, p95)
    # SNR of the dim side must not shrink the valid range
    n_known = cp.CalibrationApplier(prof, res["wl"]).valid.sum()
    assert rec["n_valid"] > 0.8 * n_known
    # record: both integration times, drift block, protocol fields
    assert rec["kind"] == "derivation_run"
    assert rec["acquisition"]["integration_known_us"] == k["integration_us"]
    assert rec["acquisition"]["integration_new_us"] == n["integration_us"]
    assert rec["drift"]["threshold_pct"] == cfg.derivation_drift_pct
    assert rec["source_hint"] == cw.DERIVATION_SOURCE_HINT
    from datetime import datetime
    assert datetime.fromisoformat(rec["started"]).tzinfo is not None
    assert datetime.fromisoformat(rec["finished"]).tzinfo is not None
    assert rec["spectra"]["C_known_repeat"] is not None
    assert len(dw.saved) == 1


def test_source_drift_fails_and_masks_pixels(demo):
    prof = _known(demo)
    dw = _dw(demo, prof)
    rec = dw.run_all(swap=_swapper(demo, 1.0, 0.95), save=False)
    demo.source_scale = 1.0
    assert rec["checks"]["drift_pass"] is False
    assert rec["drift"]["pass"] is False
    assert dw.checks_passed is False
    assert rec["drift"]["p95_pct"] > rec["drift"]["threshold_pct"]
    assert rec["drift"]["n_fail_pixels"] > 0
    assert rec["checks"]["drift_fail_pixels"] > 0
    # masked pixels are NaN in S (never gain 1)
    res = dw.result
    assert not np.isfinite(res["S"][~np.asarray(res["valid"], bool)]).any()
    assert any("Source drift check failed" in w for w in rec["warnings"])


def test_result_independent_of_integration_times(demo):
    prof = _known(demo)
    t_auto = None
    results = []
    for factor in (1.0, 0.5):
        dw = _dw(demo, prof)
        dw.set_integration("known", None)
        if t_auto is None:
            t_auto = dw.itime["known"]
        dw.set_integration("known", t_auto)
        dw.capture_known()
        dw.set_integration("new", int(t_auto * factor))
        dw.capture_new()
        dw.capture_repeat()
        dw.compute()
        results.append(dw)
    a, b = results
    assert a.itime["new"] != b.itime["new"]
    valid = np.asarray(a.result["valid"], bool) & np.asarray(b.result["valid"], bool)
    assert valid.sum() > 100
    med, p95 = _stats(a.result["S"], b.result["S"], valid)
    assert med < 0.5 and p95 < 2.0, (med, p95)


def test_dark_per_side_and_light_needs_dark(demo):
    prof = _known(demo)
    dw = _dw(demo, prof)
    with pytest.raises(RuntimeError):                  # no integration time yet
        dw.capture_dark_for("known")
    dw.set_integration("known", 600)
    with pytest.raises(RuntimeError):                  # light without dark
        dw.capture_light_for("known")
    dw.set_integration("new", 300)
    dw.capture_dark_for("known")
    dw.capture_light_for("known")
    with pytest.raises(RuntimeError):                  # new side has no dark of its own
        dw.capture_light_for("new")
    dw.capture_dark_for("new")
    dw.capture_light_for("new")
    dw.capture_dark_for("new", "after")
    dw.capture_light_for("repeat")
    dw.capture_dark_for("known", "after")
    dw.compute()
    assert dw.darks["known"]["before"].integration_us == 600
    assert dw.darks["new"]["before"].integration_us == 300
    assert dw.checks["itime_match_known"] is True
    assert dw.checks["itime_match_new"] is True
    assert {d["side"] for d in dw.dark_drift} == {"known", "new"}
    # a changed integration time of one side invalidates only its captures
    dw.set_integration("new", 450)
    assert dw.darks["new"]["before"] is None and dw.lights["new"] is None
    assert dw.darks["known"]["before"] is not None


def test_spectral_transmission_of_new_optics(demo):
    prof = _known(demo)
    dw = _dw(demo, prof)
    shape = demo._shape.copy()
    wl = demo.wavelength_array
    ramp = np.interp(wl, [wl[0], wl[-1]], [1.4, 0.6])        # transmission of the new optics

    def swap(which):
        demo._shape = shape * ramp if which == "new" else shape
    try:
        dw.run_all(swap=swap, save=False)
    finally:
        demo._shape = shape
    res = dw.result
    valid = np.asarray(res["valid"], bool)
    assert valid.sum() > 100
    s_new = np.asarray(res["S"])[valid]
    s_known = np.asarray(res["S_known"])[valid]
    exp = ramp[valid] * s_known
    exp /= exp.max()
    obs = s_new / s_new.max()
    # both normalised to their maximum: the ratio follows the ramp
    med = float(np.median(np.abs(obs / exp - 1.0))) * 100
    assert med < 2.0, med
    # S_new / S_known is NOT flat (the ramp changes by a factor 2.3)
    r = s_new / s_known
    assert r.max() / r.min() > 1.8


def test_drift_limit_from_config(demo, isolated_config):
    isolated_config._data["calibration_derivation_drift_pct"] = 0.35
    cfg = cw.WorkflowConfig.from_config()
    assert cfg.derivation_drift_pct == 0.35
    cfg.n_frames = N_FRAMES
    prof = _known(demo, _cfg())
    dw = _dw(demo, prof, cfg)
    rec = dw.run_all(swap=_swapper(demo), save=False)
    assert rec["drift"]["threshold_pct"] == 0.35
    assert rec["settings"]["derivation_drift_pct"] == 0.35
    assert rec["checks"]["drift_threshold_pct"] == 0.35


def test_legacy_single_integration_time(demo):
    prof = _known(demo)
    dw = _dw(demo, prof)
    t = dw.set_integration(500)
    assert t == 500 and dw.itime == {"known": 500, "new": 500}
    assert dw.integration_us == 500


# ── state after changing the integration time of a side (review finding) ──

def test_new_integration_time_drops_the_side_and_its_results(demo):
    prof = _known(demo)
    dw = _dw(demo, prof)
    dw.run_all(swap=_swapper(demo), save=False)
    t = dw.itime["known"]
    dw.set_integration("known", int(t * 0.6))
    assert dw.lights["known"] is None and dw.lights["repeat"] is None
    assert dw.rate_known is None and dw.rate_repeat is None
    assert dw.result is None and dw.drift is None
    dw.capture_known()
    res = dw.compute()                       # no repeat any more: drift not checked
    assert res["n_valid"] > 0
    assert dw.checks["drift_pass"] is None
    assert dw.rate_repeat is None


def test_compute_needs_the_new_side_again_after_its_time_changed(demo):
    prof = _known(demo)
    dw = _dw(demo, prof)
    dw.run_all(swap=_swapper(demo), save=False)
    dw.set_integration("new", int(dw.itime["new"] * 0.5))
    assert dw.rate_new is None
    with pytest.raises(RuntimeError):
        dw.compute()


def test_set_integration_edge_cases(demo):
    prof = _known(demo)
    dw = _dw(demo, prof)
    t = dw.set_integration(np.int64(700))            # legacy form, numpy integer
    assert t == dw.itime["known"] == dw.itime["new"]
    with pytest.raises(ValueError):
        dw.set_integration("new", None, auto=False)  # neither a value nor auto


def test_new_dark_before_resets_the_dark_after(demo):
    prof = _known(demo)
    dw = _dw(demo, prof)
    dw.set_integration("new", 700)
    dw.capture_dark_for("new")
    dw.capture_light_for("new")
    dw.capture_dark_for("new", "after")
    dw.capture_dark_for("new")                       # side started again
    assert dw.darks["new"]["after"] is None

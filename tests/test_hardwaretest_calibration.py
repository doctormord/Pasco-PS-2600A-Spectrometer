"""Hardware-test findings, block A (docs/BEFUNDE_hardwaretest.md points 1–6):
gain span hint, per-pixel checks with noise-dependent tolerance, cause model
(three synthetic cases, one cause each), dark before/after with time
interpolation, linearity calibration (model, workflow, application order,
factory-linearised devices, profile tag), verification and scope overlay."""
from __future__ import annotations

import json
import time

import numpy as np
import pytest

import calibration_capture as cc
import calibration_linearity as cl
import calibration_profiles as cp
import calibration_workflow as cw
import reference_sources as rs

BACKEND = "Demo (virtual)"
FS = 65535.0


# ══════════════════════════════════════════════════════════════════════════
#  Synthetic count-rate pairs for the cause model
# ══════════════════════════════════════════════════════════════════════════

def _pair(rng, *, offset=0.0, beta=0.0, dt_us=0.0, t=100_000, th=50_000, n=2048):
    wl = np.linspace(340, 850, n)
    shape = np.exp(-0.5 * ((wl - 620) / 150) ** 2) + 0.02
    rate_true = shape / shape.max() * 0.8 * FS / (t / 1e6)

    def cap(t_us, off):
        n_true = rate_true * (t_us + dt_us) / 1e6
        meas = n_true * (1 + beta * n_true / FS) + off
        sig = np.sqrt(np.maximum(n_true, 0) + 12.0 ** 2) / np.sqrt(16)
        mean = meas + rng.normal(0, sig)
        return mean / (t_us / 1e6), sig * np.sqrt(2) / (t_us / 1e6)
    cf, sf = cap(t, 0.0)
    ch, sh = cap(th, offset)
    return cf, ch, sf, sh, cf > 10 * sf


@pytest.mark.parametrize("case,kw,expected", [
    ("dark drift", {"offset": -150.0}, cp.CAUSE_DARK),
    ("time offset", {"dt_us": 600.0}, cp.CAUSE_TIME),
    ("nonlinearity", {"beta": -0.03}, cp.CAUSE_NONLINEAR),
])
def test_cause_model_identifies_single_cause(case, kw, expected):
    rng = np.random.default_rng(7)
    cf, ch, sf, sh, m = _pair(rng, **kw)
    r = cp.check_causes(cf, ch, sf, sh, m, t_ref_us=100_000, t_test_us=50_000,
                        full_scale=FS)
    assert r["dominant"] == expected, (case, r["message"])
    sig = [c["cause"] for c in r["causes"] if c["significant"]]
    assert sig == [expected], (case, r["message"])
    c = next(c for c in r["causes"] if c["cause"] == expected)
    if expected == cp.CAUSE_DARK:
        assert c["value"] == pytest.approx(-150.0, abs=15.0)        # magnitude in counts
    if expected == cp.CAUSE_TIME:
        assert c["time_offset_us"] == pytest.approx(600.0, rel=0.25)
    if expected == cp.CAUSE_NONLINEAR:
        assert c["response_pct_per_fs"] < -2.0                       # β = −3 %/FS
    assert c["message"] and "%" in c["message"]


def test_cause_model_clean_pair_has_no_cause():
    rng = np.random.default_rng(3)
    cf, ch, sf, sh, m = _pair(rng)
    r = cp.check_causes(cf, ch, sf, sh, m, t_ref_us=100_000, t_test_us=50_000,
                        full_scale=FS)
    assert r["dominant"] is None and "no significant" in r["message"]


def test_stability_cause_lamp_vs_dark():
    rng = np.random.default_rng(5)
    cf, _ch, sf, _sh, m = _pair(rng)
    lamp = cf * 1.012 + rng.normal(0, sf)
    r = cp.check_causes(cf, lamp, sf, sf, m, t_ref_us=100_000, t_test_us=100_000,
                        full_scale=FS)
    assert r["kind"] == "stability" and r["dominant"] == cp.CAUSE_LAMP
    dark = cf - 120.0 / 0.1 + rng.normal(0, sf)
    r = cp.check_causes(cf, dark, sf, sf, m, t_ref_us=100_000, t_test_us=100_000,
                        full_scale=FS)
    assert r["dominant"] == cp.CAUSE_DARK


# ══════════════════════════════════════════════════════════════════════════
#  Per-pixel check
# ══════════════════════════════════════════════════════════════════════════

def test_pixel_check_noise_tolerance_and_outliers():
    rng = np.random.default_rng(11)
    n = 1000
    a = np.linspace(50, 5000, n)
    sa = np.sqrt(a) * 0.5
    b = a + rng.normal(0, sa * np.sqrt(2))      # pure noise
    m = np.ones(n, bool)
    r = cp.pixel_check(a, b, sa, sa, m, threshold_pct=2.0, k=4.0)
    assert r["n_fail"] <= 1                      # noise alone does not fail
    b2 = b.copy()
    b2[10] = a[10] * 1.75                        # C_half/C_cal = 1.75 (hardware test)
    b2[20] = -5.0                                # negative rate
    r = cp.pixel_check(a, b2, sa, sa, m, threshold_pct=2.0, k=4.0)
    assert r["fail"][10] and r["fail"][20]
    m2 = m.copy()
    m2[10] = False                               # untested → ok
    assert cp.pixel_check(a, b2, sa, sa, m2, threshold_pct=2.0)["ok"][10]


def test_failing_pixels_limit_valid_segments():
    wl = np.linspace(380, 780, 401)
    C = np.full(wl.size, 1000.0)
    E = np.ones(wl.size)
    ok = np.ones(wl.size, bool)
    ok[200:210] = False
    res = cp.compute_calibration(wl, C, np.ones(wl.size), E, pixel_ok=ok)
    assert not res["valid"][200:210].any() and res["valid"][:200].all()
    segs = cp.valid_segments(wl, res["valid"])
    assert len(segs) == 2 and segs[0][1] < wl[200] and segs[1][0] > wl[209]
    assert not res["masks"]["checks"][205]


def test_gain_span_is_only_a_hint_with_default_200():
    assert cp.PlausibilityLimits().max_gain_span == 200.0
    wl = np.linspace(380, 780, 401)
    S = np.linspace(1.0, 1 / 300.0, wl.size)
    res = {"wl": wl, "S": S, "valid": np.ones(wl.size, bool), "gain_span": 300.0,
           "masks": {"base": np.ones(wl.size, bool)}}
    r = cp.broadband_check(res)
    assert r["pass"] and r["hints"] and not r["failures"]


def test_config_migration_old_gain_span_default(tmp_path):
    import app_config
    p = tmp_path / "cfg.json"
    p.write_text(json.dumps({"calibration_max_gain_span": 40.0, "theme": "light"}))
    c = app_config._Config(str(p))
    assert c.get("calibration_max_gain_span") == 200.0 and c.get("theme") == "light"
    p.write_text(json.dumps({"calibration_max_gain_span": 55.0}))
    assert app_config._Config(str(p)).get("calibration_max_gain_span") == 55.0
    p.write_text(json.dumps({"calibration_max_gain_span": 40.0, "config_version": 2}))
    assert app_config._Config(str(p)).get("calibration_max_gain_span") == 40.0


# ══════════════════════════════════════════════════════════════════════════
#  Dark before / after
# ══════════════════════════════════════════════════════════════════════════

def _cap(mean, t0, t1, n=16, std=5.0, t_us=10_000):
    mean = np.asarray(mean, float)
    return cc.Capture(wl=np.arange(mean.size, dtype=float), mean=mean,
                      std=np.full(mean.size, std), n_frames=n, integration_us=t_us,
                      saturated=np.zeros(mean.size, bool), saturation_level=60000,
                      full_scale=65535, kind="dark", t_start=t0, t_end=t1)


def test_interpolate_dark_in_time_and_drift():
    before = _cap(np.full(10, 1000.0), 0.0, 2.0)          # mid 1
    after = _cap(np.full(10, 800.0), 10.0, 12.0)          # mid 11
    d = cc.interpolate_dark(before, after, t_mid=3.5)     # 25 % of the way
    assert np.allclose(d.mean, 950.0)
    assert d.sem2 is not None and np.all(d.sem2 < 25.0 / 16)
    assert cc.interpolate_dark(before, None, 3.0) is before
    dr = cc.dark_drift(before, after)
    assert dr["mean_counts"] == pytest.approx(-200.0) and dr["seconds"] == pytest.approx(10.0)
    # count_rate uses the interpolated variance
    light = _cap(np.full(10, 5000.0), 3.0, 4.0)
    light.kind = "light"
    r = cc.count_rate(light, d)
    assert np.allclose(r.rate, (5000.0 - 950.0) / 0.01)


@pytest.fixture
def demo(isolated_config):
    from _device_demo import DemoSpectrometer
    isolated_config._data.update({"demo_spectrum": "cal_lamp",
                                  "demo_sensitivity": "synthetic",
                                  "demo_fullscale_ms": 100.0})
    holder = {}

    def make(**cfg):
        isolated_config._data.update(cfg)
        b = DemoSpectrometer(on_frame=lambda *a: None)
        b.connect("Demo Spectrometer")
        b.set_integration_time_us(20_000)
        b.start()
        holder["b"] = b
        return b
    yield make
    if "b" in holder:
        holder["b"].disconnect()


def _wcfg():
    return cw.WorkflowConfig(n_frames=12, stability_delay_s=0.2, smooth_px=5)


def test_workflow_dark_after_corrects_drift(demo):
    b = demo(demo_dark_drift=60.0)                 # +60 counts per second
    wf = cw.CalibrationWorkflow(b, BACKEND, kind="relative",
                                optics={"name": "Fibre", "input": "bare"},
                                reference=rs.ReferenceLibrary().get(rs.DEMO_SOURCE_ID),
                                cfg=_wcfg())
    rec = wf.run_all()
    assert wf.dark_after is not None and wf.dark_half_after is not None
    assert rec["dark_drift"] and rec["dark_drift"][0]["mean_counts"] > 20
    assert any(w.startswith("Dark drift") for w in rec["warnings"])
    assert rec["acquisition"]["dark_sequence"].startswith("before+after")
    # with interpolation the checks still pass; the stored profile records it
    assert rec["checks"]["stability_pass"] and rec["checks"]["itime_pass"], rec["warnings"]
    assert "stability" in rec["causes"] and "itime" in rec["causes"]
    assert rec["pixel_checks"]["itime"]["n_tested"] > 100
    assert wf.saved[0]["acquisition"]["linearity"]["active"] is False
    assert rec["acquisition"]["nl_correction_text"] == "n/a"
    json.dumps(rec, allow_nan=False)


# ══════════════════════════════════════════════════════════════════════════
#  Linearity
# ══════════════════════════════════════════════════════════════════════════

def _synthetic_steps(beta, rng, lamp_drift=0.0):
    n = 600
    R = np.linspace(2e5, 1.2e6, n)              # counts/s per pixel
    steps = []
    ts = [8000, 15000, 22000, 30000, 38000, 45000, 52000]
    for i, t in enumerate(ts):
        lamp = 1.0 + lamp_drift * i / len(ts)
        ntrue = R * t / 1e6 * lamp
        meas = ntrue * (1 + beta * ntrue / FS)
        sig = np.sqrt(ntrue + 144) / 4
        steps.append({"t_us": t, "net": meas + rng.normal(0, sig), "sigma": sig,
                      "lamp": lamp, "saturated": meas > 0.95 * FS})
    return steps


def test_linearity_fit_recovers_response_and_is_monotonic():
    rng = np.random.default_rng(2)
    m = cl.fit_linearity(_synthetic_steps(0.03, rng), full_scale=FS, degree=3)
    x = np.array([0.1, 0.4, 0.8]) * FS
    h = cl.model_h(m, x)
    true_h = 1 + 0.03 * x / FS
    assert np.allclose(h / h[0], true_h / true_h[0], atol=0.002)
    f = np.asarray(m["knots_f"])
    assert np.all(np.diff(f) > 0)
    # corrected counts are proportional to the true signal
    ntrue = np.array([0.1, 0.3, 0.6, 0.8]) * FS
    meas = ntrue * (1 + 0.03 * ntrue / FS)
    corr = cl.correct_counts(m, meas)
    ratio = corr / ntrue
    assert np.ptp(ratio) / ratio.mean() < 0.003
    assert m["rms_after_pct"] < m["rms_before_pct"]
    assert m["valid_range_counts"][1] > 0.5 * FS


def test_linearizer_frame_level_order():
    m = cl.fit_linearity(_synthetic_steps(0.03, np.random.default_rng(4)), full_scale=FS)
    fn = cl.make_linearizer(m)
    base = 500.0
    raw = base + np.array([0.0, 1000.0, 30000.0])
    out = fn(raw, base, 1.0)
    assert out[0] == pytest.approx(base)                     # offset untouched
    assert out[2] - base == pytest.approx(cl.correct_counts(m, np.array([30000.0]))[0])
    # fast-preview frame (ratio 4): corrected at its true (short) level
    out4 = fn(base + 4 * np.array([7500.0]), base, 4.0)
    assert out4[0] - base == pytest.approx(4 * cl.correct_counts(m, np.array([7500.0]))[0])


def test_backend_linearizer_applies_before_taps(demo):
    b = demo()
    seen = []
    b.set_linearizer(lambda px, dark, ratio: np.asarray(px, float) * 0 + 42.0)
    b.add_frame_tap(lambda px, *a: seen.append(float(np.mean(px))))
    t0 = time.time()
    while not seen and time.time() - t0 < 5:
        time.sleep(0.02)
    assert seen and seen[-1] == 42.0
    b.linearity_bypass = True
    n = len(seen)
    while len(seen) < n + 2 and time.time() - t0 < 5:
        time.sleep(0.02)
    assert seen[-1] != 42.0
    b.set_linearizer(None)


def test_linearity_workflow_on_demo_and_engine_application(demo, isolated_config):
    b = demo(demo_nonlinearity=0.03)
    cfg = cl.LinearityConfig(n_steps=7, n_frames=8)
    wf = cl.LinearityWorkflow(b, BACKEND, cfg)
    assert wf.mode == cl.MODE_CORRECTION
    rec = wf.run_all(sleep=lambda s: None)
    m = wf.model
    assert 6 <= len(wf.times_us) <= 10
    assert sum(1 for r, _t in wf.sequence if r == "ref") >= 3
    assert m["max_deviation_pct"] > 1.0                     # ≈ 3 %/FS × range
    assert rec["saved"]["mode"] == cl.MODE_CORRECTION and rec["activated"]
    fn, info = cl.runtime_for(b, BACKEND)
    assert fn is not None and info["state"] == cl.STATE_ACTIVE
    # profile tag + mismatch hint
    tag = cl.profile_linearity_tag(info)
    assert tag["active"] and tag["id"] == rec["saved"]["id"]
    assert cl.linearity_mismatch({"active": False, "id": None}, info)
    assert cl.linearity_mismatch(tag, info) is None
    assert cl.onoff_na(info) == "on" and cl.onoff_na(None) == "n/a"
    json.dumps(rec, allow_nan=False)


class _FactoryNL:
    """Minimal stand-in for an Ocean HDX with factory linearisation."""
    nl_correction_active = True
    serial = "HDX-1"


def test_factory_linearised_device_gets_check_only(isolated_config):
    wf = cl.LinearityWorkflow(_FactoryNL(), "Ocean HDX")
    assert wf.mode == cl.MODE_CHECK
    fn, info = cl.runtime_for(_FactoryNL(), "Ocean HDX")
    assert fn is None and info["state"] == cl.STATE_FACTORY
    e = cl.add_entry({"mode": cl.MODE_CHECK, "backend": "Ocean HDX", "serial": "HDX-1",
                      "model": {"knots_x": [0, 1], "knots_f": [0, 1]}}, activate=True)
    assert cl.active_id("Ocean HDX", "HDX-1") is None          # never activated
    with pytest.raises(ValueError):
        cl.set_active("Ocean HDX", "HDX-1", e["id"])
    fn, info = cl.runtime_for(_FactoryNL(), "Ocean HDX")
    assert fn is None                                           # no double correction


def test_profile_status_linearity_note(isolated_config):
    p = {"device": {"backend": "X", "serial": "1"}, "wl_fingerprint": "fp",
         "acquisition": {"linearity": {"active": True, "id": "lin-a"}}}
    st = cp.profile_status(p, "X", "1", "fp", None,
                           linearity={"state": cl.STATE_NONE, "id": None})
    assert st["state"] == cp.STATE_OK and "Linearity correction was on" in st["linearity_note"]
    st = cp.profile_status(p, "X", "1", "fp", None,
                           linearity={"state": cl.STATE_ACTIVE, "id": "lin-a"})
    assert "linearity_note" not in st


# ══════════════════════════════════════════════════════════════════════════
#  Verification + overlay
# ══════════════════════════════════════════════════════════════════════════

def test_verify_against_reference_bands():
    wl = np.linspace(380, 950, 571)
    e = np.exp(-0.5 * ((wl - 650) / 200) ** 2)
    v = 3.0 * e * np.where(wl < 425, 1.049, 1.0)            # +4.9 % below 425 nm
    r = cw.verify_against_reference(wl, v, e, band_nm=50.0, norm_range_nm=(550, 650))
    b = {int(x["lo"]): x for x in r["bands"]}
    assert b[400]["mean_pct"] > 1.0 and abs(b[550]["mean_pct"]) < 0.01
    assert not r["pass"] and b[350]["warn"]
    assert r["norm_range_nm"] == [550.0, 650.0]
    assert any("nm:" in line for line in cw.format_verification(r))


def _wait(e, job, timeout=90.0):
    t0 = time.time()
    while time.time() - t0 < timeout:
        j = e.jobs.get(job.id)
        if j.status in ("done", "failed", "cancelled"):
            return j
        time.sleep(0.05)
    raise AssertionError(f"job {job.id} stuck in {e.jobs.get(job.id).status}")


def test_engine_calibration_job_verifies_and_overlay(isolated_config):
    from engine import Engine
    isolated_config._data.update({"demo_spectrum": "cal_lamp",
                                  "demo_sensitivity": "synthetic",
                                  "demo_fullscale_ms": 100.0,
                                  "calibration_stability_delay_s": 0.2,
                                  "calibration_capture_frames": 8})
    e = Engine()
    try:
        e.connect(BACKEND)
        job = e.submit_calibration({"kind": "relative", "reference_id": rs.DEMO_SOURCE_ID,
                                    "optics": {"name": "Fibre", "input": "bare"}},
                                   owner="remote:test")
        j = _wait(e, job)
        assert j.status == "done", j.error
        res = j.result
        assert res["status"] == "saved", res
        ver = res["verification"]
        assert ver["overall"]["rms_pct"] < 2.0 and ver["bands"]
        assert e.last_verification["profile_id"] == res["profile_ids"][0]
        assert "dark_drift" in res and "causes" in res
        # overlay scaled onto the current display
        t0 = time.time()
        while e.last_frame is None and time.time() - t0 < 5:
            time.sleep(0.02)
        ev = e.last_frame
        y = e.reference_overlay(rs.DEMO_SOURCE_ID, ev.wavelengths, ev.pixels, ev.unit)
        assert y is not None and np.nanmax(y) > 0
        assert e.reference_overlay("nope", ev.wavelengths, ev.pixels, ev.unit) is None
        assert e.calibration_status()["linearity"]["state"] in ("none", "off")
    finally:
        e.close()


def test_engine_linearity_job_installs_correction(isolated_config):
    from engine import Engine
    isolated_config._data.update({"demo_spectrum": "tungsten", "demo_fullscale_ms": 100.0,
                                  "demo_nonlinearity": 0.03, "linearity_capture_frames": 6,
                                  "linearity_steps": 6})
    e = Engine()
    try:
        e.connect(BACKEND)
        j = _wait(e, e.submit_linearity({}, owner="remote:test"), timeout=120)
        assert j.status == "done", j.error
        res = j.result
        assert res["status"] == "saved", res
        assert e.linearity_info["state"] == cl.STATE_ACTIVE
        assert e.acquisition.linearizer is not None
        # report meta / status carry the state
        assert e.report_meta()["Linearity correction"] == "on"
        info = e.select_linearity(None, owner="remote:test")
        assert info["state"] == cl.STATE_OFF and e.acquisition.linearizer is None
    finally:
        e.close()

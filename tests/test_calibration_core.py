"""Phase 1/2 core: schema v2 + migration, count rate, S/K recovery, validity,
t_int independence, dark store, processing pipeline, demo-backend capture."""
from __future__ import annotations

import json
import os
import shutil
import time

import numpy as np
import pytest

import calibration_capture as cc
import calibration_profiles as cp
import processing
from calibration_utils import build_response_gain, demo_lamp_irradiance, wavelength_fingerprint

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


# ── helpers ──────────────────────────────────────────────────────────────

WL = np.linspace(340.0, 850.0, 1024)


def s_true(wl):
    return np.exp(-0.5 * ((wl - 600.0) / 160.0) ** 2)


def make_capture(mean, t_us, *, n=16, std=None, kind="light", sat_level=1e15,
                 fp="fp"):
    mean = np.asarray(mean, float)
    return cc.Capture(wl=WL.copy(), mean=mean,
                      std=np.full(mean.size, 5.0) if std is None else std,
                      n_frames=n, integration_us=int(t_us),
                      saturated=mean >= sat_level, saturation_level=sat_level,
                      full_scale=65535.0, backend="Synthetic", serial="X1",
                      wl_fingerprint=fp, kind=kind)


def synth_pair(t_us, A=2.0e5, bias=500.0, rate_dark=1000.0):
    """Light + dark captures of E_lamp·S_true·A counts/s (noise-free means)."""
    t = t_us / 1e6
    pattern = 3.0 * np.sin(np.arange(WL.size) / 7.0)     # per-pixel dark pattern
    dark = bias + rate_dark * t + pattern
    sig = demo_lamp_irradiance(WL) * s_true(WL) * A * t
    return make_capture(dark + sig, t_us), make_capture(dark, t_us, kind="dark")


# ── count rate ───────────────────────────────────────────────────────────

def test_count_rate_mean_minus_dark_over_t_no_clipping():
    light, dark = synth_pair(50_000)
    light.mean[:5] = dark.mean[:5] - 10.0              # below dark → negative rate
    r = cc.count_rate(light, dark)
    assert np.all(r.rate[:5] < 0)                       # kept, not clipped
    exp = (light.mean - dark.mean) / 0.05
    np.testing.assert_allclose(r.rate, exp, rtol=0, atol=1e-9)
    assert r.warnings == []


def test_count_rate_warns_on_itime_mismatch_and_refuses_px_mismatch():
    light, _ = synth_pair(50_000)
    _, dark = synth_pair(25_000)
    r = cc.count_rate(light, dark)
    assert any("integration time" in w for w in r.warnings)
    bad = make_capture(np.zeros(10), 50_000)
    with pytest.raises(ValueError):
        cc.count_rate(light, bad)


# ── S / K recovery (< 0.1 %) and t_int independence ──────────────────────

@pytest.mark.parametrize("t_us", [10_000, 50_000, 400_000])
def test_relative_and_absolute_recovery(t_us):
    A = 2.0e5
    light, dark = synth_pair(t_us, A=A)
    r = cc.count_rate(light, dark)
    E = demo_lamp_irradiance(WL)
    rel = cp.compute_calibration(WL, r.rate, np.full(WL.size, 1.0), E,
                                 kind="relative", spec_range_nm=(380, 780),
                                 snr_k=10, max_gain=1e6, smooth_px=0)
    v = rel["valid"]
    truth = s_true(WL) / s_true(WL[v]).max()
    assert np.max(np.abs(rel["S"][v] / truth[v] - 1)) < 1e-3
    assert rel["valid_range_nm"][0] >= 380 and rel["valid_range_nm"][1] <= 780

    ab = cp.compute_calibration(WL, r.rate, np.full(WL.size, 1.0), E,
                                kind="absolute", spec_range_nm=(380, 780),
                                snr_k=10, max_gain=1e6, smooth_px=0)
    v = ab["valid"]
    K_true = 1.0 / (s_true(WL) * A)
    assert np.max(np.abs(ab["K"][v] / K_true[v] - 1)) < 1e-3
    # measurement: E = C · K reproduces the lamp
    np.testing.assert_allclose((r.rate * ab["K"])[v], E[v], rtol=1e-3)


def test_independent_of_integration_time_and_averaging():
    res = []
    for t_us, n in ((20_000, 4), (80_000, 16), (320_000, 64)):
        light, dark = synth_pair(t_us)
        light.n_frames = n
        r = cc.count_rate(light, dark)
        res.append(cp.compute_calibration(
            WL, r.rate, np.ones(WL.size), demo_lamp_irradiance(WL),
            kind="absolute", spec_range_nm=(380, 780), snr_k=10, smooth_px=0)["K"])
    for k in res[1:]:
        m = np.isfinite(k) & np.isfinite(res[0])
        np.testing.assert_allclose(k[m], res[0][m], rtol=1e-9)


def test_validity_is_intersection_and_masks_with_nan():
    light, dark = synth_pair(50_000)
    r = cc.count_rate(light, dark)
    E = demo_lamp_irradiance(WL)
    E[WL < 400] = np.nan                            # reference starts at 400 nm
    sigma = np.full(WL.size, 1.0)
    sigma[(WL > 600) & (WL < 610)] = 1e12           # SNR hole
    light.saturated[(WL > 650) & (WL < 655)] = True
    res = cp.compute_calibration(WL, r.rate, sigma, E, kind="relative",
                                 saturated=light.saturated,
                                 spec_range_nm=(380, 780), snr_k=10, smooth_px=0)
    S, v = res["S"], res["valid"]
    assert not v[WL < 400].any() and not v[WL > 780].any()
    assert not v[(WL > 600) & (WL < 610)].any()
    assert not v[(WL > 650) & (WL < 655)].any()
    assert np.all(np.isnan(S[~v])) and np.all(np.isfinite(S[v]))
    assert res["valid_range_nm"][0] >= 400
    tbl = cp.result_table(res)
    assert any(row[1] is None for row in tbl)        # holes inside range → None


def test_max_gain_masks_instead_of_clamping():
    light, dark = synth_pair(50_000)
    r = cc.count_rate(light, dark)
    res = cp.compute_calibration(WL, r.rate, np.ones(WL.size), demo_lamp_irradiance(WL),
                                 kind="relative", snr_k=0, max_gain=2.0, smooth_px=0)
    assert np.nanmin(res["S"]) >= 0.5
    assert res["gain_span"] <= 2.0 + 1e-9


def test_smoothing_small_window_keeps_smooth_response():
    light, dark = synth_pair(50_000)
    r = cc.count_rate(light, dark)
    res = cp.compute_calibration(WL, r.rate, np.ones(WL.size), demo_lamp_irradiance(WL),
                                 kind="relative", spec_range_nm=(380, 780),
                                 snr_k=10, smooth_px=5)
    v = res["valid"]
    truth = s_true(WL) / s_true(WL[v]).max()
    assert np.max(np.abs(res["S"][v] / truth[v] - 1)) < 1e-3


def test_derive_relative_from_ratio():
    S_known = s_true(WL) / s_true(WL).max()
    optic_ratio = 0.5 + 0.3 * (WL - WL[0]) / (WL[-1] - WL[0])   # new optic transmits differently
    src = 1.0 + 0.5 * np.cos(WL / 80.0) ** 2                     # diffuse source, any shape
    c_known = src * S_known * 1e5
    c_new = src * S_known * optic_ratio * 1e5
    res = cp.derive_relative_from_ratio(WL, S_known, c_new, c_known, max_gain=1e6)
    truth = S_known * optic_ratio
    truth /= truth.max()
    np.testing.assert_allclose(res["S"], truth, rtol=1e-9)


def test_compare_rates_detects_drift_not_noise():
    rng = np.random.default_rng(1)
    a = 1e5 * s_true(WL)
    noisy = a * (1 + rng.normal(0, 0.01, WL.size))           # 1 % per-pixel noise
    m = np.ones(WL.size, bool)
    assert cp.compare_rates(a, noisy, m)["p95_pct"] < 0.6
    assert cp.compare_rates(a, a * 1.03, m)["p95_pct"] == pytest.approx(3.0, rel=1e-6)


# ── frame accumulator ────────────────────────────────────────────────────

def test_frame_accumulator_filters_and_statistics():
    acc = cc.FrameAccumulator(3, 10_000, saturation_level=100.0, skip_first=1)
    acc(np.full(4, 999.0), 0, 10_000)                       # skipped (first)
    acc(np.full(4, 5.0), 0, 20_000)                         # wrong t_int
    acc(np.full(4, 5.0), 0, 10_000, "short", 1.0)           # wrong kind
    acc(np.full(4, 5.0), 0, 10_000, "standard", 8.0)        # fast-preview scaled
    for v in (1.0, 2.0, 3.0):
        acc(np.array([v, v, v, 150.0]), 0, 10_000)
    assert acc.done.is_set()
    mean, std, sat, n = acc.stats()
    assert n == 3
    np.testing.assert_allclose(mean[:3], 2.0)
    np.testing.assert_allclose(std[:3], 1.0)
    assert sat.tolist() == [False, False, False, True]
    assert acc.rejected == {"itime": 1, "kind": 1, "scaled": 1}


def test_suggest_integration_time():
    new, ok = cc.suggest_integration_us(10_000, 10_000 + 500, 500, full_scale=65535,
                                        saturation_level=60000)
    assert not ok and 40_000 < new < 60_000
    new, ok = cc.suggest_integration_us(10_000, 50_000, 500, full_scale=65535,
                                        saturation_level=60000)
    assert ok and new == 10_000
    new, ok = cc.suggest_integration_us(10_000, 65000, 500, full_scale=65535,
                                        saturation_level=60000, any_saturated=True)
    assert not ok and new == 5_000


# ── dark store ───────────────────────────────────────────────────────────

def test_dark_store_per_device_and_itime():
    _, d1 = synth_pair(50_000)
    _, d2 = synth_pair(100_000)
    cc.save_dark(d1)
    cc.save_dark(d2)
    other = make_capture(d1.mean + 1, 50_000, kind="dark")
    other.serial = "X2"
    cc.save_dark(other)
    got = cc.find_dark("Synthetic", "X1", 50_000)
    np.testing.assert_allclose(got.mean, d1.mean)
    assert got.integration_us == 50_000 and got.timestamp
    assert cc.find_dark("Synthetic", "X1", 75_000) is None
    np.testing.assert_allclose(cc.find_dark("Synthetic", "X2", 50_000).mean, d1.mean + 1)
    assert len(cc.list_darks("Synthetic", "X1")) == 2


# ── schema v2: migration, versions, binding ──────────────────────────────

def _legacy_file(tmp_path, isolated_config):
    src = os.path.join(ROOT, "calibration_profiles.json")
    legacy = None
    if os.path.exists(src):
        with open(src, encoding="utf-8") as f:
            legacy = json.load(f)
        # A working copy whose sample file was already migrated to schema 2
        # (by running the app) is no legacy file any more.
        if not isinstance(legacy, dict) or "schema" in legacy or "profiles" in legacy:
            legacy = None
    if legacy is None:  # repo without the (legacy) sample file
        legacy = {"Demo (virtual)": {"test_calib": {
            "table": [[340.0, 0.5], [600.0, 1.0], [850.0, 0.4]],
            "created": "2026-01-01 10:00:00", "wl_min_nm": 340.0, "wl_max_nm": 850.0}}}
    path = isolated_config.get("calibration_profiles_file")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(legacy, f)
    return legacy, path


def test_migration_keeps_old_profiles_bit_identical(tmp_path, isolated_config):
    legacy, path = _legacy_file(tmp_path, isolated_config)
    dev, entries = next(iter(legacy.items()))
    name, entry = next(iter(entries.items()))
    wl = np.linspace(340, 850, 2048)
    old_gain = build_response_gain(wl, np.asarray(entry["table"], float))

    # schema-1 selection by NAME still resolves
    cp.set_active_selection(dev, name)
    sel = cp.active_selection(dev)
    p = cp.get_profile_by_id(sel)
    assert p["type"] == "relative" and p["optics"]["input"] == "unknown"
    assert p["optics"]["name"] == name and p["apply"] == cp.APPLY_LEGACY_GAIN
    gain, eff = cp.build_gain_for_selection(wl, dev, sel)
    assert eff == sel
    assert np.array_equal(gain, old_gain)
    gain2, _ = cp.build_gain_for_selection(wl, dev, name)     # by old name too
    assert np.array_equal(gain2, old_gain)

    # loading does not rewrite the file; a save writes schema 2 + a v1 backup
    with open(path, encoding="utf-8") as f:
        assert json.load(f) == legacy
    cp.save_registry(cp.load_registry())
    with open(path, encoding="utf-8") as f:
        assert json.load(f)["schema"] == 2
    assert os.path.exists(os.path.splitext(path)[0] + ".v1.bak.json")
    gain3, _ = cp.build_gain_for_selection(wl, dev, sel)
    assert np.array_equal(gain3, old_gain)

    rt = cp.CalibrationRuntime()
    rt.backend_name, rt.wl = dev, wl
    rt.refresh()
    assert rt.applier is None and np.array_equal(rt.legacy_gain, old_gain)


def _result(kind="relative"):
    light, dark = synth_pair(50_000)
    r = cc.count_rate(light, dark)
    return cp.compute_calibration(WL, r.rate, np.ones(WL.size), demo_lamp_irradiance(WL),
                                  kind=kind, spec_range_nm=(380, 780), smooth_px=0)


def _profile(kind="relative", serial="X1", fp=None, optic="CC", name="Fiber A"):
    return cp.new_profile(kind=kind, backend="Synthetic", serial=serial,
                          optics={"name": name, "input": optic},
                          reference={"name": "DEMO", "serial": "L1", "unit": "uW/cm2/nm"},
                          wl_fingerprint=fp or wavelength_fingerprint(WL),
                          result=_result(kind),
                          acquisition={"integration_us": 50_000, "averages": 16,
                                       "nl_correction": True},
                          checks={"stability_pct": 0.1, "itime_crosscheck_pct": 0.2})


def test_profile_schema_fields_and_versioning():
    a = cp.add_profile(_profile())
    b = cp.add_profile(_profile())
    for k in ("schema", "type", "device", "optics", "reference", "wl_fingerprint",
              "valid_range_nm", "table", "unit", "acquisition", "checks",
              "created", "version"):
        assert k in a
    assert a["schema"] == 2 and a["version"] == 1 and b["version"] == 2
    assert "+" in a["created"] or a["created"].endswith("Z") or a["created"][-6] in "+-"
    listed = cp.profiles_for_device("Synthetic", "X1", wavelength_fingerprint(WL))
    assert [p["id"] for p in listed] == [b["id"]]                     # newest valid
    assert len(cp.profiles_for_device("Synthetic", "X1", include_old=True)) == 2
    assert cp.get_profile_by_id(a["id"]) is not None                  # old retrievable
    json.dumps(cp.load_registry(), allow_nan=False)                   # strict JSON


def test_absolute_requires_defined_entrance_area():
    with pytest.raises(ValueError):
        cp.add_profile(_profile(kind="absolute", optic="bare"))
    assert cp.add_profile(_profile(kind="absolute", optic="CC"))["unit"].endswith("per count/s")


def test_serial_binding_and_copy_to_device():
    p = cp.add_profile(_profile(serial="HDX-A"))
    fp = wavelength_fingerprint(WL)
    assert cp.profiles_for_device("Synthetic", "HDX-B", fp) == []
    cpy = cp.copy_profile_to_device(p["id"], "HDX-B")
    assert cpy["copied_from"]["serial"] == "HDX-A"
    assert [q["id"] for q in cp.profiles_for_device("Synthetic", "HDX-B", fp)] == [cpy["id"]]


def test_fingerprint_mismatch_needs_confirmation():
    p = cp.add_profile(_profile(fp="0000000000000000"))
    cp.set_active_selection("Synthetic", p["id"])
    rt = cp.CalibrationRuntime()
    rt.backend_name, rt.serial, rt.wl = "Synthetic", "X1", WL
    rt.fingerprint = wavelength_fingerprint(WL)
    rt.refresh()
    assert rt.status["state"] == cp.STATE_WL_CHANGED and rt.applier is None
    cp.confirm_fingerprint_override(p["id"], rt.fingerprint)
    rt.refresh()
    assert rt.applier is not None


def test_export_import_roundtrip(tmp_path):
    p = cp.add_profile(_profile())
    f = tmp_path / "p.json"
    cp.export_profile(p["id"], str(f))
    q = cp.import_profile(str(f))
    assert q["id"] != p["id"] and q["version"] == 2 and q["table"] == p["table"]


# ── processing pipeline ──────────────────────────────────────────────────

def _pp(arr, **kw):
    base = dict(wavelengths=WL, response_gain=np.full(WL.size, 2.0),
                ob_slope=1.0, ob_offset=0.0, dark_bias_adc=100.0,
                dark_rate_adc_per_s=1000.0)
    base.update(kw)
    return processing.process_frame(arr, 50.0, 40_000, **base)


def test_processing_legacy_path_unchanged():
    rng = np.random.default_rng(0)
    raw = rng.uniform(0, 4000, WL.size)
    for dm in ("optical_black", "parametric"):
        out = _pp(raw, dark_correction=True, dark_mode=dm, response_compensation=True,
                  hot_pixel_filter=True, smoothing_width=5)["pixels"]
        d = 50.0 if dm == "optical_black" else 100.0 + 1000.0 * 0.04
        ref = np.maximum(0.0, raw - d)
        ref = processing.apply_despeckle(ref, 5)
        ref = np.clip(ref * 2.0, 0.0, None)
        assert np.array_equal(out, ref)


def test_processing_calibrated_rate_factor_and_mask():
    p = cp.add_profile(_profile(kind="absolute"))
    ap = cp.CalibrationApplier(p, WL, "uW/cm2/nm")
    light, dark = synth_pair(40_000)
    out = _pp(light.mean, calibration=ap, dark_mode="stored", stored_dark=dark.mean,
              response_compensation=True)
    assert out["unit"] == "uW/cm2/nm" and out["dark_source"] == "stored"
    v = ap.valid
    np.testing.assert_allclose(out["pixels"][v], demo_lamp_irradiance(WL)[v], rtol=1e-3)
    assert np.all(np.isnan(out["pixels"][~v]))
    ap2 = cp.CalibrationApplier(p, WL, "W/m2/nm")
    out2 = _pp(light.mean, calibration=ap2, dark_mode="stored", stored_dark=dark.mean)
    np.testing.assert_allclose(out2["pixels"][v], out["pixels"][v] * 0.01, rtol=1e-12)


def test_processing_counts_per_s_option():
    raw = np.full(WL.size, 1050.0)
    out = _pp(raw, dark_correction=True, dark_mode="optical_black",
              display_unit="counts_per_s")
    assert out["unit"] == "counts/s"
    np.testing.assert_allclose(out["pixels"], 1000.0 / 0.04)


# ── demo backend end-to-end (counts/s capture with dark spectrum) ────────

@pytest.fixture
def demo(isolated_config):
    from _device_demo import DemoSpectrometer
    isolated_config._data.update({"demo_spectrum": "cal_lamp",
                                  "demo_sensitivity": "synthetic",
                                  "demo_fullscale_ms": 100.0})
    b = DemoSpectrometer(on_frame=lambda *a: None)
    b.connect("Demo Spectrometer")
    b.set_integration_time_us(30_000)
    b.start()
    yield b
    b.disconnect()


def test_demo_capture_counts_per_s_with_dark(demo):
    from _device_demo import demo_true_sensitivity
    rates = {}
    for t in (30_000, 60_000):
        dark = cc.capture_dark(demo, 12, integration_us=t, backend_name="Demo (virtual)")
        light = cc.capture(demo, 12, integration_us=t, backend_name="Demo (virtual)")
        assert dark.kind == "dark" and dark.serial == "DEMO-0001"
        assert dark.peak_counts < 0.05 * dark.full_scale          # shutter closed
        assert light.n_saturated == 0
        r = cc.count_rate(light, dark)
        assert not r.warnings
        rates[t] = r
    m = (demo.wavelength_array > 400) & (demo.wavelength_array < 780)
    chk = cp.compare_rates(rates[60_000].rate, rates[30_000].rate, m)
    assert chk["p95_pct"] < 1.0                                    # t_int independent
    r = rates[60_000]
    res = cp.compute_calibration(r.wl, r.rate, r.sigma_dark, demo_lamp_irradiance(r.wl),
                                 kind="relative", saturated=r.saturated,
                                 spec_range_nm=demo.spec_range_nm, smooth_px=5)
    v = res["valid"]
    truth = demo_true_sensitivity(r.wl)
    truth = truth / truth[v].max()
    err = np.abs(res["S"][v] / truth[v] - 1)
    assert np.median(err) < 0.005 and np.percentile(err, 95) < 0.02

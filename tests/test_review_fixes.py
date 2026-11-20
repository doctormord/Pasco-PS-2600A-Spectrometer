"""Regression tests for the review findings (critical + important)."""
from __future__ import annotations

import numpy as np
import pytest

import calibration_capture as cc
import calibration_profiles as cp
import calibration_workflow as cw
import processing
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


def _abs_run(demo):
    wf = cw.CalibrationWorkflow(demo, BACKEND, kind="absolute",
                                optics={"name": "CC", "input": "CC"},
                                reference=rs.ReferenceLibrary().get(rs.DEMO_SOURCE_ID),
                                cfg=cw.WorkflowConfig(n_frames=12, stability_delay_s=0.1))
    wf.run_all()
    cp.set_active_selection(BACKEND, wf.saved[0]["id"])
    return wf


# ── critical: per-pixel dark always used with a v2 profile ────────────────

@pytest.mark.parametrize("dark_mode", ["optical_black", "parametric"])
def test_v2_uses_per_pixel_dark_whatever_the_dark_mode(demo, isolated_config, dark_mode):
    wf = _abs_run(demo)                                   # stores darks at t and t/2
    isolated_config._data.update({"dark_correction": False, "dark_mode": dark_mode})
    rt = cp.CalibrationRuntime()
    rt.configure(BACKEND, demo, demo.wavelength_array)
    wl = demo.wavelength_array
    out = {}
    for t in (15_000, 30_000, 60_000):
        light = cc.capture(demo, 16, integration_us=t, backend_name=BACKEND)
        kw = processing.pipeline_kwargs(isolated_config.get, rt, integration_us=t,
                                        ob_slope=1.0, ob_offset=0.0,
                                        default_bias=61.57, default_rate=40.94)
        r = processing.process_frame(light.mean, 0.0, t, wavelengths=wl, **kw)
        exact = t in (wf.integration_us, wf.half_us)
        assert r["dark_source"] == ("stored" if exact else "stored_model")
        out[t] = r["pixels"]
    v = rt.applier.valid
    for t in (15_000, 30_000):
        dev = cp.compare_rates(out[60_000], out[t], v)
        assert dev["p95_pct"] < 1.0 and abs(dev["mean_ratio"] - 1) < 0.005
    blue = v & (wl < 420)
    err = out[60_000][blue] / demo_lamp_irradiance(wl)[blue] - 1
    assert abs(np.median(err)) < 0.01                    # was −19 % with the scalar
    assert "dark_warning" not in rt.status


def test_v2_without_any_dark_warns(demo, isolated_config):
    _abs_run(demo)
    cc.delete_darks(BACKEND, demo.serial)
    rt = cp.CalibrationRuntime()
    rt.configure(BACKEND, demo, demo.wavelength_array)
    kw = processing.pipeline_kwargs(isolated_config.get, rt, integration_us=20_000,
                                    ob_slope=1.0, ob_offset=0.0)
    assert kw["stored_dark"] is None
    assert "dark_warning" in rt.status and rt.status["dark_source"] is None


def test_dark_model_is_linear_per_pixel(isolated_config):
    wl = np.linspace(400, 700, 50)
    b = 500 + np.arange(50.0)
    r = 2000 + 10 * np.arange(50.0)                      # counts/s per pixel
    for t in (10_000, 40_000):
        cc.save_dark(cc.Capture(wl=wl, mean=b + r * t / 1e6, std=np.ones(50),
                                n_frames=8, integration_us=t, saturated=np.zeros(50, bool),
                                saturation_level=6e4, full_scale=65535.0,
                                backend="X", serial="S", kind="dark"))
    src, arr, info = cc.dark_for_integration("X", "S", 25_000)
    assert src == "stored_model" and info["integration_us"] == [10_000, 40_000]
    np.testing.assert_allclose(arr, b + r * 0.025, rtol=1e-12)
    assert cc.dark_for_integration("X", "S", 10_000)[0] == "stored"


# ── important: save errors surface; NaN cleaned ───────────────────────────

def test_save_failure_raises(isolated_config, tmp_path):
    bad = tmp_path / "is_a_dir"
    bad.mkdir()
    isolated_config._data["calibration_profiles_file"] = str(bad)
    p = {"type": "relative", "device": {"backend": "B", "serial": "1"},
         "optics": {"name": "o", "input": "CC"}, "table": [[400.0, 1.0]],
         "checks": {"stability_pct": float("nan")}}
    with pytest.raises(OSError):
        cp.add_profile(p)


def test_nan_in_checks_is_stored_as_null(isolated_config):
    p = {"type": "relative", "device": {"backend": "B", "serial": "1"},
         "optics": {"name": "o", "input": "CC"}, "table": [[400.0, 1.0], [500.0, None]],
         "checks": {"stability_pct": float("nan"), "x": np.float64(np.inf)}}
    q = cp.add_profile(p)
    assert cp.get_profile_by_id(q["id"])["checks"] == {"stability_pct": None, "x": None}


# ── important: absolute needs an absolute reference unit ──────────────────

def test_absolute_with_relative_reference_refused(demo):
    ref = rs.ReferenceLibrary().get(rs.DEMO_SOURCE_ID)
    ref.unit = "relative"
    with pytest.raises(ValueError):
        cw.CalibrationWorkflow(demo, BACKEND, kind="absolute",
                               optics={"name": "CC", "input": "CC"}, reference=ref)
    p = {"type": "absolute", "device": {"backend": "B", "serial": "1"},
         "optics": {"name": "o", "input": "CC"}, "table": [[400.0, 1.0]],
         "reference": {"unit": "relative"}}
    with pytest.raises(ValueError):
        cp.add_profile(p)


# ── important: derivation guards ──────────────────────────────────────────

def test_derivation_refuses_legacy_known_profile(demo):
    leg = cp.save_profile(BACKEND, "old", np.array([[340.0, 0.5], [850.0, 1.0]]))
    with pytest.raises(ValueError):
        cw.DerivationWorkflow(demo, BACKEND, known_profile_id=leg["id"],
                              optics={"name": "new", "input": "bare"})


def test_derivation_refuses_saturated_capture(demo, isolated_config):
    wf = _abs_run(demo)
    dw = cw.DerivationWorkflow(demo, BACKEND, known_profile_id=wf.saved[0]["id"],
                               optics={"name": "new", "input": "bare"},
                               cfg=cw.WorkflowConfig(n_frames=4))
    dw.set_integration(400_000)                          # far into saturation
    dw.capture_dark_for("new")
    with pytest.raises(RuntimeError, match="saturated"):
        dw.capture_light_for("new")


# ── important: PASCO serial from the device path ──────────────────────────

def test_pasco_serial_from_windows_path(monkeypatch):
    import _device_pasco as dp
    monkeypatch.setattr(dp, "_IS_WINDOWS", True)
    f = dp.PascoPS2600A._serial_from_device
    assert f(r"\\?\usb#vid_0945&pid_ae01#ps2600a123#{a5dcbf10-6530-11d2-901f-00c04fb951ed}") == "PS2600A123"
    assert f(r"\\?\usb#vid_0945&pid_ae01#6&1a2b3c&0&2#{a5dcbf10}") is None

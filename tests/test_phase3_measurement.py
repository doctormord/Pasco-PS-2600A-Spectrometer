"""Phase 3 acceptance with the demo backend: three integration times give
coincident spectra with an active profile; desktop and web compute identical
values; lux from an absolute profile is plausible."""
from __future__ import annotations

import os

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
                                  "demo_fullscale_ms": 100.0,
                                  "selected_backend": BACKEND})
    b = DemoSpectrometer(on_frame=lambda *a: None)
    b.connect("Demo Spectrometer")
    b.set_integration_time_us(20_000)
    b.start()
    yield b
    b.disconnect()


def _absolute_profile(demo):
    wf = cw.CalibrationWorkflow(demo, BACKEND, kind="absolute",
                                optics={"name": "CC", "input": "CC"},
                                reference=rs.ReferenceLibrary().get(rs.DEMO_SOURCE_ID),
                                cfg=cw.WorkflowConfig(n_frames=12, stability_delay_s=0.1))
    wf.run_all()
    cp.set_active_selection(BACKEND, wf.saved[0]["id"])
    return wf


def _runtime(demo):
    rt = cp.CalibrationRuntime()
    rt.configure(BACKEND, demo, demo.wavelength_array)
    return rt


def test_three_integration_times_coincide(demo, isolated_config):
    _absolute_profile(demo)
    isolated_config._data.update({"dark_correction": True, "dark_mode": "stored"})
    rt = _runtime(demo)
    assert rt.active_v2 and rt.status["unit"] == "uW/cm2/nm"
    spectra = {}
    for t in (15_000, 30_000, 60_000):
        cc.save_dark(cc.capture_dark(demo, 16, integration_us=t, backend_name=BACKEND))
        rt.invalidate_darks()
        light = cc.capture(demo, 16, integration_us=t, backend_name=BACKEND)
        kw = processing.pipeline_kwargs(isolated_config.get, rt, integration_us=t,
                                        ob_slope=1.0, ob_offset=0.0)
        out = processing.process_frame(light.mean, 0.0, t,
                                       wavelengths=demo.wavelength_array, **kw)
        assert out["dark_source"] == "stored" and out["unit"] == "uW/cm2/nm"
        spectra[t] = out["pixels"]
    v = rt.applier.valid
    ref = spectra[60_000]
    for t in (15_000, 30_000):
        dev = cp.compare_rates(ref, spectra[t], v)
        assert dev["p95_pct"] < 1.0, (t, dev)
        assert abs(dev["mean_ratio"] - 1) < 0.005
    # and it is the lamp's spectral irradiance
    err = np.abs(ref[v] / demo_lamp_irradiance(demo.wavelength_array)[v] - 1)
    assert np.median(err) < 0.005
    assert np.all(np.isnan(ref[~v]))


def test_lux_from_absolute_profile_plausible(demo, isolated_config):
    import color_science as cs
    _absolute_profile(demo)
    isolated_config._data.update({"dark_correction": True, "dark_mode": "stored",
                                  "radiometric_calibration_factor": 123.0})
    rt = _runtime(demo)
    t = 40_000
    cc.save_dark(cc.capture_dark(demo, 16, integration_us=t, backend_name=BACKEND))
    light = cc.capture(demo, 16, integration_us=t, backend_name=BACKEND)
    kw = processing.pipeline_kwargs(isolated_config.get, rt, integration_us=t,
                                    ob_slope=1.0, ob_offset=0.0)
    out = processing.process_frame(light.mean, 0.0, t, wavelengths=demo.wavelength_array, **kw)
    spd, lux_cal = processing.photometric_inputs(out["pixels"], out["unit"])
    assert lux_cal == 1.0                                  # scalar factor ignored
    lux = cs.illuminance_lux(demo.wavelength_array, spd, lux_cal)
    wl = np.arange(380.0, 781.0)
    truth = cs.illuminance_lux(wl, demo_lamp_irradiance(wl) * 0.01, 1.0)
    # demo valid range ends ~780 nm and starts ~380 nm → within 1 %
    assert lux == pytest.approx(truth, rel=0.01)


def test_desktop_and_web_identical(demo, isolated_config, monkeypatch):
    pytest.importorskip("PyQt6")
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from conftest import qt_app
    app = qt_app()          # session-wide QApplication (never re-created)

    _absolute_profile(demo)
    isolated_config._data.update({"dark_correction": True, "dark_mode": "stored",
                                  "hot_pixel_filter": True, "frames_to_average": 1})
    t = 30_000
    cc.save_dark(cc.capture_dark(demo, 8, integration_us=t, backend_name=BACKEND))
    frame = cc.capture(demo, 1, integration_us=t, backend_name=BACKEND).mean
    ob = float(np.median(np.sort(frame)[:50]))
    wl = np.asarray(demo.wavelength_array, float)

    # ── web ──
    import web_server as ws
    ws.state.calib = _runtime(demo)
    ws.state.wavelength_array = wl
    ws.state.pixel_count = wl.size
    ws.state.ob_slope, ws.state.ob_offset = 1.0, 0.0
    ws.state.frames_avg = 1
    ws.state.spectrum_history.clear()
    ws.state.heatmap_linear_waves = np.linspace(wl[0], wl[-1], wl.size)
    ws.state.heatmap_buffer = np.zeros((wl.size, ws.HEATMAP_HISTORY_SIZE))
    ws._fused_emit(frame.copy(), ob, t)
    web_px = np.asarray(ws.state.current_pixels, float)
    assert ws.state.current_unit == "uW/cm2/nm"

    # ── desktop (real DashboardWindow code path, offscreen) ──
    # Since the shared engine (docs/PLAN_api_mcp.md) the desktop renders the
    # frames of its own Engine instance; same device state as the web engine.
    from gui_dashboard import DashboardWindow
    dash = DashboardWindow()
    try:
        dash.combo_backend.setCurrentText(BACKEND)
        dash.hardware_thread = demo
        dash.engine.connected_backend_name = BACKEND
        dash.engine.wavelength_array = wl
        dash.engine.pixel_count = wl.size
        dash.engine.ob_slope, dash.engine.ob_offset = 1.0, 0.0
        dash.engine.frames_avg = 1
        dash.engine.clear_heatmap()
        dash.apply_response_correction()
        assert dash.calib_runtime.active_v2
        dash.spectrum_history.clear()
        dash.process_new_spectrum(frame.copy(), ob, t)
        desk_px = np.asarray(dash.current_averaged_pixels, float)
        assert dash._current_unit == "uW/cm2/nm"
    finally:
        dash.hardware_thread = None
        dash.engine.connected_backend_name = None
        dash.close()
        app.processEvents()

    assert np.array_equal(np.isnan(web_px), np.isnan(desk_px))
    assert np.array_equal(web_px[~np.isnan(web_px)], desk_px[~np.isnan(desk_px)])


def test_legacy_pipeline_unchanged_without_profile(demo, isolated_config):
    """No profile, counts display: pipeline_kwargs path == old explicit call."""
    isolated_config._data.update({"dark_correction": True, "dark_mode": "optical_black"})
    rt = _runtime(demo)
    assert not processing.needs_shared_pipeline(isolated_config.get, rt)
    frame = cc.capture(demo, 1, integration_us=20_000, backend_name=BACKEND).mean
    kw = processing.pipeline_kwargs(isolated_config.get, rt, integration_us=20_000,
                                    ob_slope=1.0, ob_offset=0.0)
    new = processing.process_frame(frame, 600.0, 20_000,
                                   wavelengths=demo.wavelength_array, **kw)["pixels"]
    old = np.maximum(0.0, frame - 600.0)
    assert np.array_equal(new, old)

"""Radiance chain with the Demo backend (finding 17 / block B5):
CC absolute irradiance profile → sphere characterisation (radiance reference)
→ absolute radiance profile of an objective → measurement in radiance units
with the luminance in cd/m². Plus the admissibility matrix and the migration
of old profiles.

Demo model: the Demo backend's `source_scale` scales the simulated lamp. The
sphere port emits L(λ) = k·E_lamp(λ)/π; on the axis at the distance d the CC
sees E = k·E_lamp·r²/(r² + (d + d0)²), i.e. source_scale = k·g(d + d0). The
objective looks at the port with an arbitrary throughput (source_scale = s_obj);
its absolute calibration against the sphere then measures L directly."""
from __future__ import annotations

import json
import math

import numpy as np
import pytest

import calibration_profiles as cp
import calibration_sphere as S
import calibration_workflow as cw
import color_science as cs
import processing
import reference_sources as rs

BACKEND = "Demo (virtual)"
R_MM = 7.0
D0_TRUE = 0.3
K_SPHERE = 0.8


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
    b.source_scale = 1.0
    b.disconnect()


def _cc_profile(demo):
    wf = cw.CalibrationWorkflow(demo, BACKEND, kind="absolute",
                                optics={"name": "CC", "input": "CC"},
                                reference=rs.ReferenceLibrary().get(rs.DEMO_SOURCE_ID),
                                cfg=cw.WorkflowConfig(n_frames=12, stability_delay_s=0.1))
    wf.run_all()
    cp.set_active_selection(BACKEND, wf.saved[0]["id"])
    return wf.saved[0]


def _sphere(demo, heights=(0.0, 6.0, 16.0)):
    cfg = S.SphereConfig(n_frames=12)
    sc = S.SphereCharacterization(
        demo, BACKEND, cfg=cfg,
        sphere={"name": "Printed sphere", "serial": "S1", "port_radius_mm": R_MM,
                "edge_depth_mm": 11.0, "u_radius_mm": 0.02, "u_distance_mm": 0.1,
                "cc_uncertainty_pct": 3.0})

    def move(h):
        d = sc.distance_mm(h)
        demo.source_scale = K_SPHERE * float(S.disk_factor(R_MM, d + D0_TRUE))

    rec = sc.run_all(heights, move=move)
    return sc, rec


def test_full_chain_sphere_objective_luminance(demo, isolated_config):
    cc_prof = _cc_profile(demo)
    assert cp.profile_quantity(cc_prof) == "irradiance"

    # 1) sphere characterisation
    sc, rec = _sphere(demo)
    assert rec["d0_plausible"] and abs(rec["d0_mm"] - D0_TRUE) < 0.1
    assert rec["consistency_pass"], rec["overall"]
    assert rec["drift"]["pass"]
    src = sc.saved_source
    assert src.quantity == "radiance" and src.unit == "uW/cm2/sr/nm"
    lib_src = rs.ReferenceLibrary().get(src.id)
    assert lib_src is not None and lib_src.quantity == "radiance"
    meta = lib_src.meta
    assert meta["geometry"]["port_radius_mm"] == R_MM
    assert meta["cc_profile"]["id"] == cc_prof["id"]
    assert meta["device"]["backend"] == BACKEND and meta["date"]
    lo, hi = cc_prof["valid_range_nm"]
    assert lib_src.valid_min_nm >= lo - 1e-6 and lib_src.valid_max_nm <= hi + 1e-6
    # L = K_SPHERE · E_lamp / π (µW/(cm²·sr·nm))
    e_lamp = rs.ReferenceLibrary().get(rs.DEMO_SOURCE_ID).on_axis(lib_src.wl)
    ratio = lib_src.values / (K_SPHERE * e_lamp / math.pi)
    sel = (lib_src.wl > 450) & (lib_src.wl < 900)
    assert abs(float(np.median(ratio[sel])) - 1.0) < 0.02

    # 2) absolute radiance calibration of an objective against the sphere
    with pytest.raises(ValueError):                       # CC + radiance absolute
        cw.CalibrationWorkflow(demo, BACKEND, kind="absolute",
                               optics={"name": "x", "input": "CC"}, reference=lib_src)
    demo.source_scale = 0.05                              # objective throughput
    wf = cw.CalibrationWorkflow(demo, BACKEND, kind="absolute",
                                optics={"name": "Objective 10x", "input": "objective"},
                                reference=lib_src,
                                cfg=cw.WorkflowConfig(n_frames=12, stability_delay_s=0.1))
    rec2 = wf.run_all()
    assert rec2["quantity"] == "radiance" and rec2["source_hint"]
    prof = wf.saved[0]
    assert prof["type"] == "absolute" and prof["quantity"] == "radiance"
    assert prof["unit"] == "uW/cm2/sr/nm per count/s"
    assert wf.saved[1]["quantity"] == "radiance"          # derived relative
    cp.set_active_selection(BACKEND, prof["id"])

    # 3) measurement: radiance unit + luminance in cd/m²
    rt = cp.CalibrationRuntime()
    rt.configure(BACKEND, demo, demo.wavelength_array)
    assert rt.active_v2 and rt.status["unit"] == "uW/cm2/sr/nm"
    assert rt.status["quantity"] == "radiance"
    import calibration_capture as cc
    t = 30_000
    cc.save_dark(cc.capture_dark(demo, 12, integration_us=t, backend_name=BACKEND))
    rt.invalidate_darks()
    light = cc.capture(demo, 12, integration_us=t, backend_name=BACKEND)
    isolated_config._data.update({"dark_correction": True, "dark_mode": "stored"})
    kw = processing.pipeline_kwargs(isolated_config.get, rt, integration_us=t,
                                    ob_slope=1.0, ob_offset=0.0)
    out = processing.process_frame(light.mean, 0.0, t, wavelengths=light.wl, **kw)
    assert out["unit"] == "uW/cm2/sr/nm"
    L_meas = out["pixels"]
    L_ref = lib_src.on_axis(light.wl)
    m = np.isfinite(L_meas) & np.isfinite(L_ref) & (light.wl > 450) & (light.wl < 900)
    assert abs(float(np.median(L_meas[m] / L_ref[m])) - 1.0) < 0.02
    spd, cal = processing.photometric_inputs(L_meas, out["unit"])
    q = processing.photometric_quantity(out["unit"])
    assert q == "radiance" and cal == 1.0
    met = cs.measure_all(light.wl, spd, cal, quantity=q)
    spd_ref, _ = processing.photometric_inputs(np.where(np.isfinite(L_meas), L_ref, np.nan),
                                               "uW/cm2/sr/nm")
    lum_ref = cs.luminance_cd_m2(light.wl, spd_ref, 1.0)
    assert np.isfinite(met["luminance"]) and met["luminance"] > 0
    assert met["luminance"] == pytest.approx(lum_ref, rel=0.03)
    assert not np.isfinite(met["lux"])


def test_sphere_needs_active_cc_absolute_profile(demo):
    with pytest.raises(ValueError, match="No active calibration profile"):
        S.SphereCharacterization(demo, BACKEND, sphere={"name": "S", "port_radius_mm": 7})
    wf = cw.CalibrationWorkflow(demo, BACKEND, kind="relative",
                                optics={"name": "CC", "input": "CC"},
                                reference=rs.ReferenceLibrary().get(rs.DEMO_SOURCE_ID),
                                cfg=cw.WorkflowConfig(n_frames=8, stability_delay_s=0.05))
    wf.run_all()
    cp.set_active_selection(BACKEND, wf.saved[0]["id"])
    with pytest.raises(ValueError, match="not an absolute irradiance profile"):
        S.SphereCharacterization(demo, BACKEND, sphere={"name": "S", "port_radius_mm": 7})


def test_sphere_needs_three_distances(demo):
    _cc_profile(demo)
    sc = S.SphereCharacterization(demo, BACKEND, cfg=S.SphereConfig(n_frames=6),
                                  sphere={"name": "S", "port_radius_mm": 7})
    sc.set_integration(20_000)
    sc.capture_dark()
    sc.capture_position(0.0)
    sc.capture_position(5.0)
    with pytest.raises(RuntimeError, match="At least 3"):
        sc.compute()
    with pytest.raises(ValueError):
        sc.capture_position(5.0)                          # same distance twice


# ── admissibility matrix ──────────────────────────────────────────────────

@pytest.mark.parametrize("kind,q,inp,ok", [
    ("relative", "irradiance", "CC", True), ("relative", "irradiance", "bare", True),
    ("relative", "irradiance", "sphere", True), ("relative", "irradiance", "objective", True),
    ("relative", "radiance", "CC", True), ("relative", "radiance", "objective", True),
    ("absolute", "irradiance", "CC", True), ("absolute", "irradiance", "sphere", True),
    ("absolute", "irradiance", "bare", False), ("absolute", "irradiance", "objective", False),
    ("absolute", "radiance", "bare", True), ("absolute", "radiance", "objective", True),
    ("absolute", "radiance", "CC", False), ("absolute", "radiance", "sphere", False),
])
def test_admissibility(kind, q, inp, ok):
    assert cp.admissible(kind, q, inp)[0] is ok
    rows = [r for r in cp.admissibility_matrix()
            if (r["type"], r["quantity"], r["input"]) == (kind, q, inp)]
    assert len(rows) == 1 and rows[0]["allowed"] is ok


def test_profile_store_rejects_inadmissible(isolated_config):
    base = {"type": "absolute", "device": {"backend": BACKEND, "serial": "X"},
            "optics": {"name": "o", "input": "objective"},
            "reference": {"unit": "uW/cm2/nm"}, "table": [[400, 1.0], [500, 1.0]],
            "quantity": "irradiance"}
    with pytest.raises(ValueError):
        cp.add_profile(dict(base))
    ok = dict(base, quantity="radiance", reference={"unit": "uW/cm2/sr/nm"})
    assert cp.add_profile(ok)["quantity"] == "radiance"
    with pytest.raises(ValueError):                       # radiance with E unit
        cp.add_profile(dict(base, quantity="radiance"))


# ── migration of old profiles ─────────────────────────────────────────────

def test_old_schema2_profiles_become_irradiance(isolated_config):
    path = isolated_config._data["calibration_profiles_file"]
    old = {"schema": 2, "profiles": [
        {"schema": 2, "id": "a1", "type": "absolute", "version": 1,
         "device": {"backend": BACKEND, "serial": "X"},
         "optics": {"name": "CC", "input": "CC"},
         "reference": {"unit": "uW/cm2/nm"}, "unit": "uW/cm2/nm per count/s",
         "table": [[400, 1.0], [500, 1.0]], "apply": "v2"},
        {"schema": 2, "id": "r1", "type": "relative", "version": 1,
         "device": {"backend": BACKEND, "serial": "X"},
         "optics": {"name": "bare", "input": "bare"},
         "reference": {"unit": "relative"}, "unit": "relative",
         "table": [[400, 1.0], [500, 1.0]], "apply": "v2"}]}
    with open(path, "w", encoding="utf-8") as f:
        json.dump(old, f)
    ps = {p["id"]: p for p in cp.all_profiles()}
    assert ps["a1"]["quantity"] == "irradiance" and ps["r1"]["quantity"] == "irradiance"
    assert "absolute irradiance" in cp.profile_label(ps["a1"])
    # schema-1 (legacy) entries too
    reg = cp.migrate_registry({BACKEND: {"old": {"table": [[400, 1.0], [500, 0.5]]}}})
    assert reg["profiles"][0]["quantity"] == "irradiance"


def test_applier_units_follow_quantity():
    prof = {"type": "absolute", "quantity": "radiance", "unit": "uW/cm2/sr/nm per count/s",
            "table": [[400, 2.0], [800, 2.0]], "valid_range_nm": [400, 800]}
    wl = np.array([450.0, 600.0])
    ap = cp.CalibrationApplier(prof, wl, "W/m2/nm")
    assert ap.unit == "W/m2/sr/nm"
    assert np.allclose(ap.factor, 0.02)
    assert cp.output_unit_for("irradiance", "mW/m2/nm") == "mW/m2/nm"
    assert cp.output_unit_for("radiance", "mW/m2/nm") == "mW/m2/sr/nm"
    assert processing.unit_quantity("mW/m2/sr/nm") == "radiance"
    assert processing.is_absolute_unit("uW/cm2/sr/nm")

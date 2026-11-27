"""Sphere as a radiance reference: Lambertian disk model, fit with free offset
d0, consistency check, uncertainty, measurement quantities and units
(synthetic data, no device)."""
from __future__ import annotations

import math

import numpy as np
import pytest

import calibration_sphere as S
import reference_sources as rs


WL = np.arange(380.0, 1000.0, 0.5)
L_TRUE = 2.0 + 0.8 * np.sin(WL / 70.0)          # µW/(cm²·sr·nm)
R = 7.0                                         # Ø 14.00 mm


def _E(d, *, d0=0.0, p=1.0, L=L_TRUE, r=R):
    """Irradiance on the axis; p = 1 is the Lambertian disk."""
    return np.vstack([math.pi * L * S.disk_factor(r, di + d0) ** p for di in d])


@pytest.mark.parametrize("heights", [(0.0, 8.0, 20.0), (0.0, 5.0, 12.0, 30.0)])
@pytest.mark.parametrize("d0", [0.0, 0.37, -0.6])
def test_lambert_disk_reconstructs_L_and_d0(heights, d0):
    d = np.array([S.port_distance_mm(h) for h in heights])
    res = S.characterize(WL, _E(d, d0=d0), d, R, d0_max_mm=1.0)
    assert res["method"] == "fit" and res["d0_plausible"]
    assert abs(res["d0_mm"] - d0) < 0.1                       # acceptance: < 0.1 mm
    dev = np.nanmax(np.abs(res["L"] / L_TRUE - 1.0))
    assert dev < 0.005                                       # acceptance: < 0.5 %
    assert res["consistency_pass"]
    assert res["overall"]["spread_pct"] < 0.01
    # per band: d0 and spread reported
    assert res["bands"] and all(abs(b["d0_mm"] - d0) < 0.1 for b in res["bands"])


def test_lambert_disk_with_noise():
    rng = np.random.default_rng(1)
    d = np.array([11.0, 19.0, 31.0])
    E = _E(d, d0=0.25)
    E = E * (1.0 + 0.002 * rng.standard_normal(E.shape))
    res = S.characterize(WL, E, d, R)
    assert abs(res["d0_mm"] - 0.25) < 0.1
    assert abs(np.nanmedian(res["L"] / L_TRUE) - 1.0) < 0.005


def test_nominal_formula():
    # L = E (r² + d²) / (π r²)
    assert S.radiance_from_irradiance(math.pi, 1.0, 0.0) == pytest.approx(1.0)
    e = math.pi * 3.0 * 49.0 / (49.0 + 121.0)
    assert S.radiance_from_irradiance(e, 7.0, 11.0) == pytest.approx(3.0)
    assert S.port_distance_mm(5.0, edge_depth_mm=11.0, cc_ref_offset_mm=-0.4) == \
        pytest.approx(15.6)


@pytest.mark.parametrize("p", [0.8, 1.15, 1.3])
def test_non_lambertian_source_detected(p):
    """Wrong exponent of the distance dependence → consistency check fails
    (the free offset cannot absorb it plausibly)."""
    d = np.array([11.0, 16.0, 26.0, 41.0])
    res = S.characterize(WL, _E(d, p=p), d, R, consistency_max_pct=2.0, d0_max_mm=1.0)
    assert not res["consistency_pass"]
    assert any("Consistency check failed" in w for w in res["warnings"])


def test_implausible_d0_falls_back_to_mean_with_warning():
    d = np.array([11.0, 16.0, 26.0])
    res = S.characterize(WL, _E(d, d0=2.5), d, R, d0_max_mm=1.0)
    assert not res["d0_plausible"] and res["method"] == "mean"
    assert np.allclose(res["L"], res["L_mean"], equal_nan=True)
    assert any("d0" in w for w in res["warnings"])


def test_uncertainty_components():
    d = np.array([11.0, 16.0, 26.0])
    res = S.characterize(WL, _E(d), d, R, u_r_mm=0.02, u_d_mm=0.1,
                         cc_uncertainty_pct=3.0, drift_pct=0.6)
    u = res["uncertainty"]
    cr = np.mean(2 * d ** 2 / (R * (R * R + d ** 2)))
    cd = np.mean(2 * d / (R * R + d ** 2))
    assert u["u_rel_r_pct"] == pytest.approx(100 * cr * 0.02, rel=1e-3)
    assert u["u_rel_d_pct"] == pytest.approx(100 * cd * 0.1, rel=1e-3)
    assert u["u_rel_drift_pct"] == pytest.approx(0.6 / math.sqrt(3), rel=1e-6)
    tot = math.sqrt(u["u_rel_r_pct"] ** 2 + u["u_rel_d_pct"] ** 2
                    + u["u_rel_drift_pct"] ** 2 + 3.0 ** 2)
    assert u["u_rel_median_pct"] == pytest.approx(tot, rel=1e-3)
    assert u["U_rel_median_pct_k2"] == pytest.approx(2 * u["u_rel_median_pct"])


def test_input_validation():
    d = np.array([11.0, 16.0])
    with pytest.raises(ValueError):
        S.characterize(WL, _E(d), [11.0], R)
    with pytest.raises(ValueError):
        S.characterize(WL, _E(d), [11.0, 11.0], R)
    with pytest.raises(ValueError):
        S.characterize(WL, _E(d), d, 0.0)
    with pytest.raises(ValueError):
        S.characterize(WL, -_E(d), d, R)


# ── measurement quantities and units ──────────────────────────────────────

def test_units_convert_only_within_quantity():
    v = np.array([1.0, 2.0])
    assert np.allclose(rs.convert_units(v, "uW/cm2/sr/nm", "W/m2/sr/nm"), v * 0.01)
    assert np.allclose(rs.convert_units(v, "W/m2/nm", "uW/cm2/nm"), v * 100)
    with pytest.raises(ValueError):
        rs.convert_units(v, "uW/cm2/nm", "uW/cm2/sr/nm")
    with pytest.raises(ValueError):
        rs.convert_units(v, "relative", "W/m2/sr/nm")
    assert rs.quantity_of_unit("mW/m2/sr/nm") == rs.QUANTITY_RADIANCE
    assert rs.quantity_of_unit("mW/m2/nm") == rs.QUANTITY_IRRADIANCE
    assert rs.quantity_of_unit("relative") is None


def test_reference_source_quantity_roundtrip_and_old_records():
    src = rs.ReferenceSource(
        id="s", name="Sphere", serial="1", unit=rs.UNIT_UW_CM2_SR_NM,
        valid_min_nm=400, valid_max_nm=900, input_optic="objective",
        source_file="x", imported="", wl=np.array([400.0, 900.0]),
        values=np.array([1.0, 2.0]), quantity=rs.QUANTITY_RADIANCE,
        meta={"geometry": {"port_radius_mm": 7.0}}, uncertainty=np.array([0.01, 0.02]))
    d = src.to_dict()
    assert d["quantity"] == "radiance"
    back = rs.ReferenceSource.from_dict(d)
    assert back.quantity == "radiance" and back.meta["geometry"]["port_radius_mm"] == 7.0
    assert np.allclose(back.uncertainty, [0.01, 0.02])
    old = dict(d)
    old.pop("quantity")
    old["unit"] = "uW/cm2/nm"
    assert rs.ReferenceSource.from_dict(old).quantity == "irradiance"
    with pytest.raises(ValueError):
        rs.ReferenceSource.from_dict(dict(d, unit="uW/cm2/nm"))     # unit ≠ quantity


def test_library_import_with_quantity(tmp_path):
    f = tmp_path / "l.csv"
    f.write_text("400;1,0\n500;2,0\n600;3,0\n", encoding="utf-8")
    lib = rs.ReferenceLibrary(str(tmp_path / "lib.json"))
    s = lib.import_file(str(f), name="Sph", serial="A", unit="uW/cm2/sr/nm")
    assert s.quantity == "radiance"
    with pytest.raises(ValueError):
        lib.import_file(str(f), name="Sph", serial="B", unit="uW/cm2/nm",
                        quantity="radiance")
    lib2 = rs.ReferenceLibrary(str(tmp_path / "lib.json"))
    assert lib2.get(s.id).quantity == "radiance"
    assert "objective" in rs.INPUT_OPTICS


def test_luminance_analytic():
    """Flat radiance L0 W/(m²·sr·nm) over 380–780 nm: luminance = 683 · L0 · ∫V."""
    import color_science as cs
    wl = np.arange(360.0, 831.0, 1.0)
    L0 = 0.01
    L = np.full(wl.size, L0)
    m = cs.measure_all(wl, L, 1.0, quantity="radiance")
    integral_v = float(np.sum(cs.Y_BAR)) * float(cs.CIE_WL_5[1] - cs.CIE_WL_5[0])
    assert m["luminance"] == pytest.approx(683.002 * L0 * integral_v, rel=1e-6)
    assert 106.0 < integral_v < 107.5                       # ∫V dλ ≈ 106.9 nm
    assert not np.isfinite(m["lux"]) and not np.isfinite(m["fc"])
    rows = dict(cs.color_report_rows(wl, L, 1.0, quantity="radiance"))
    assert "Luminance" in rows and rows["Luminance"].endswith("cd/m²")
    assert "Illuminance" not in rows
    # irradiance unchanged
    m2 = cs.measure_all(wl, L, 1.0)
    assert m2["lux"] == pytest.approx(m["luminance"]) and not np.isfinite(m2["luminance"])

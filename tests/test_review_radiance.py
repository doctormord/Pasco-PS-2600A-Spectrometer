"""Review findings of feature/radiance: web colour report with absolute
units, quantity in the profile lists, verification with mismatched
quantities, overlay cache."""
from __future__ import annotations

import numpy as np
import pytest

pytest.importorskip("fastapi")
pytest.importorskip("httpx")
from fastapi.testclient import TestClient

import calibration_profiles as cp
import color_science as cs
import engine as eng
import processing
import reference_sources as rs
from api import ServerSettings, create_app

BACKEND = "Demo (virtual)"


@pytest.fixture
def engine(isolated_config):
    isolated_config._data.update({"demo_spectrum": "cal_lamp",
                                  "demo_sensitivity": "synthetic",
                                  "demo_fullscale_ms": 100.0,
                                  "selected_backend": BACKEND})
    e = eng.Engine()
    try:
        yield e
    finally:
        e.close()


@pytest.fixture
def client(engine):
    with TestClient(create_app(engine, ServerSettings())) as c:
        yield c


def _report_value(client, wl, values, unit, quantity, lux_cal):
    r = client.post("/api/color/report", json={
        "format": "csv", "wavelengths": list(map(float, wl)),
        "intensities": [None if not np.isfinite(v) else float(v) for v in values],
        "lux_calibration": lux_cal, "quantity": quantity, "unit": unit})
    assert r.status_code == 200
    rows = {}
    for line in r.text.splitlines():
        parts = [p.strip().strip('"') for p in line.replace(";", ",").split(",")]
        if len(parts) >= 2:
            rows[parts[0]] = parts[1]
    return rows


@pytest.mark.parametrize("unit,quantity,row,suffix", [
    ("uW/cm2/sr/nm", "radiance", "Luminance", "cd/m²"),
    ("uW/cm2/nm", "irradiance", "Illuminance", "lx"),
])
def test_web_colour_report_matches_engine(engine, client, unit, quantity, row, suffix):
    """Finding 1: the web report sends the frame in the profile unit; the
    route must apply the same photometric inputs as the engine (factor to the
    SI unit, masked pixels → 0), otherwise the value is 100× too high."""
    wl = np.arange(380.0, 781.0, 1.0)
    vals = np.full(wl.size, 1.0)
    vals[5] = np.nan                                       # one masked pixel
    rows = _report_value(client, wl, vals, unit, quantity, 1.0)
    assert row in rows, rows
    shown = float(rows[row].split()[0])
    expect_m = engine.color_metrics_for(wl, vals, unit)
    expect = expect_m["luminance"] if quantity == "radiance" else expect_m["lux"]
    assert np.isfinite(expect)
    assert shown == pytest.approx(expect, rel=0.01)
    assert rows[row].endswith(suffix)
    # 1 µW/(cm²·sr·nm) flat over 380–780 nm ≈ 0.01·683·∫V ≈ 730 cd/m² (not 73 000)
    assert 600 < shown < 800


def test_profile_items_carry_quantity(engine, isolated_config):
    import api
    p = cp.add_profile({
        "type": "absolute", "quantity": "radiance",
        "device": {"backend": BACKEND, "serial": None},
        "optics": {"name": "Obj", "input": "objective"},
        "reference": {"unit": "uW/cm2/sr/nm"}, "unit": "uW/cm2/sr/nm per count/s",
        "table": [[400, 1.0], [800, 1.0]], "valid_range_nm": [400, 800]})
    item = api._profile_item(engine, p, BACKEND, None, None)
    assert item["quantity"] == "radiance" and item["input"] == "objective"
    assert item["legacy"] is False and item["legacy_valid_range_nm"] is None
    leg = cp.save_profile(BACKEND, "old", [[400, 1.0], [600, 0.5]])
    item = api._profile_item(engine, leg, BACKEND, None, None)
    assert item["legacy"] is True and item["legacy_valid_range_nm"] == [400.0, 600.0]
    import mcp_server.server as ms
    view = ms._profile_items({"items": [api._profile_item(engine, p, BACKEND, None, None)]})
    assert view[0]["quantity"] == "radiance"


def test_verification_with_other_quantity_is_refused(engine):
    engine.connect(BACKEND, owner="local")
    p = cp.add_profile({
        "type": "absolute", "quantity": "radiance",
        "device": {"backend": BACKEND, "serial": None},
        "optics": {"name": "Obj", "input": "objective"},
        "reference": {"unit": "uW/cm2/sr/nm"}, "unit": "uW/cm2/sr/nm per count/s",
        "table": [[400, 1.0], [800, 1.0]], "valid_range_nm": [400, 800],
        "apply": cp.APPLY_V2})
    with pytest.raises(eng.Unprocessable, match="same measurement quantity"):
        engine.verify_profile(p["id"], rs.DEMO_SOURCE_ID, owner=eng.LOCAL)


def test_overlay_unscaled_only_for_same_quantity(engine):
    wl = np.linspace(400, 800, 50)
    disp = np.full(wl.size, 5.0)
    e_abs = engine.reference_overlay(rs.DEMO_SOURCE_ID, wl, disp, "uW/cm2/nm")
    ref = rs.ReferenceLibrary().get(rs.DEMO_SOURCE_ID).on_axis(wl)
    assert np.allclose(e_abs, ref, equal_nan=True)          # same quantity: unscaled
    e_rad = engine.reference_overlay(rs.DEMO_SOURCE_ID, wl, disp, "uW/cm2/sr/nm")
    m = np.isfinite(e_rad)
    assert np.allclose(np.sum(e_rad[m] * ref[m]) / np.sum(ref[m] ** 2) * ref[m], e_rad[m])
    assert not np.allclose(e_rad[m], ref[m])                # other quantity: scaled

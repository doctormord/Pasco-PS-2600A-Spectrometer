"""Sphere characterisation protocol (PDF + CSV) and the quantity in the other
protocols. Synthetic data, no device."""
from __future__ import annotations

import math
from dataclasses import asdict

import numpy as np

import calibration_report as rep
import calibration_sphere as S


def _record(with_nan=True):
    wl = np.linspace(400, 800, 81)
    r, d0 = 7.0, 0.3
    d = np.array([11.0, 17.0, 27.0])
    base = 5.0 * (1 + 0.5 * np.sin(wl / 60.0))
    E = np.vstack([math.pi * base * S.disk_factor(r, x + d0) for x in d])
    valid = np.ones(wl.size, bool)
    if with_nan:
        valid[:4] = False
    res = S.characterize(wl, E, d, r, valid=valid, u_r_mm=0.02, u_d_mm=0.1,
                         cc_uncertainty_pct=3.0, drift_pct=0.2)
    res["L_smoothed"] = res["L"]

    def arr(a):
        return [None if not np.isfinite(v) else float(v) for v in np.asarray(a, float)]
    sp = {"wl": arr(wl), "E": [arr(np.where(valid, e, np.nan)) for e in E],
          "E_repeat": arr(E[0] * 1.001), "L_i": [arr(x) for x in res["L_i"]],
          "L": arr(res["L"]), "L_fit": arr(res["L_fit"]), "L_mean": arr(res["L_mean"]),
          "L_smoothed": arr(res["L_smoothed"]), "spread_pct": arr(res["spread_pct"]),
          "u_rel_pct": arr(100 * res["u_rel"]), "valid": [bool(v) for v in valid]}
    return {
        "kind": "sphere_characterisation",
        "started": "2026-05-01T10:00:00+02:00", "finished": "2026-05-01T10:12:00+02:00",
        "quantity": "radiance", "unit": "uW/cm2/sr/nm", "irradiance_unit": "uW/cm2/nm",
        "device": {"backend": "Demo (virtual)", "serial": "SN1"},
        "sphere": {"name": "Printed sphere", "serial": "S1", "port_radius_mm": r,
                   "port_diameter_mm": 2 * r, "edge_depth_mm": 11.0, "cc_ref_offset_mm": 0.0,
                   "u_radius_mm": 0.02, "u_distance_mm": 0.1, "cc_uncertainty_pct": 3.0,
                   "notes": ""},
        "cc_profile": {"id": "cc-1", "version": 2, "optics": {"name": "CC", "input": "CC"},
                       "reference": {"name": "Lamp"}, "valid_range_nm": [420.0, 780.0]},
        "source_hint": "Radiance reference.",
        "acquisition": {"integration_us": 20000, "averages": 16,
                        "dark_sequence": "before", "nl_correction": True},
        "positions": [{"height_mm": h, "d_mm": float(x), "d_eff_mm": res["d_eff_mm"][i],
                       "peak_frac": 0.75, "captured": "2026-05-01T10:01:00+02:00"}
                      for i, (h, x) in enumerate(zip((0.0, 6.0, 16.0), d))],
        "settings": asdict(S.SphereConfig()),
        "formula": "L = E*(r^2 + d^2)/(pi*r^2)",
        "d0_mm": res["d0_mm"], "d0_plausible": res["d0_plausible"],
        "d0_max_mm": res["d0_max_mm"], "method": res["method"], "bands": res["bands"],
        "overall": res["overall"], "consistency_pass": res["consistency_pass"],
        "consistency_max_pct": res["consistency_max_pct"], "uncertainty": res["uncertainty"],
        "drift": {"p95_pct": 0.1, "mean_pct": 0.2, "limit_pct": 1.0, "pass": True,
                  "seconds": 60.0, "n": 70},
        "valid_range_nm": [420.0, 780.0], "warnings": ["Example warning."],
        "source": {"id": "src-1", "name": "Printed sphere", "unit": "uW/cm2/sr/nm",
                   "quantity": "radiance", "valid_min_nm": 420.0, "valid_max_nm": 780.0},
        "spectra": sp,
    }


def test_sphere_protocol_csv_and_pdf(tmp_path):
    rec = _record()
    name = rep.default_sphere_basename(rec)
    assert name.startswith("sphere_Printed-sphere_SN1_2026")
    pdf, csv = tmp_path / (name + ".pdf"), tmp_path / (name + ".csv")
    out = rep.write_sphere_protocol(rec, str(pdf), str(csv))
    assert set(out) == {str(pdf), str(csv)}
    assert pdf.read_bytes()[:4] == b"%PDF"
    txt = csv.read_text(encoding="utf-8")
    assert "nan" not in txt.lower()
    lines = txt.splitlines()
    head = {ln[2:].split(";", 1)[0]: ln.split(";", 1)[1] for ln in lines if ln.startswith("# ") and ";" in ln}
    assert head["started"].endswith("+02:00") and head["finished"].endswith("+02:00")
    for key in ("quantity", "unit", "port_radius_mm", "cc_profile_id", "d0_mm", "d0_max_mm",
                "consistency_max_pct", "drift_limit_pct", "u_rel_cc_pct", "U_rel_median_pct_k2",
                "drift_max_pct", "band_1", "position_3", "result_source_id", "warning_1"):
        assert key in head, key
    assert head["quantity"] == "radiance"
    cols = [ln for ln in lines if not ln.startswith("#")][0].split(";")
    assert cols == ["wavelength_nm", "E_1_uW_cm2_nm", "E_2_uW_cm2_nm", "E_3_uW_cm2_nm",
                    "E_repeat_uW_cm2_nm", "L_1_uW_cm2_sr_nm", "L_2_uW_cm2_sr_nm",
                    "L_3_uW_cm2_sr_nm", "L_fit", "L_mean", "L_used", "spread_pct",
                    "u_rel_pct", "valid"]
    rows = [ln.split(";") for ln in lines if not ln.startswith("#")][1:]
    assert len(rows) == 81 and all(len(r) == len(cols) for r in rows)
    assert rows[0][cols.index("L_used")] == "" and rows[0][cols.index("valid")] == "0"
    assert rows[40][cols.index("L_used")] != ""


def test_sphere_pdf_without_spectra(tmp_path):
    rec = _record()
    rec["spectra"] = {}
    rec["drift"] = None
    rec["source"] = None
    p = tmp_path / "s.pdf"
    rep.write_sphere_protocol(rec, str(p), None)
    assert p.read_bytes()[:4] == b"%PDF"


def test_calibration_protocol_has_quantity(tmp_path):
    rec = {"kind": "calibration_run", "type": "absolute", "quantity": "radiance",
           "source_hint": "Radiance of the sphere port.", "unit": "uW/cm2/sr/nm",
           "started": "2026-05-01T10:00:00+02:00", "device": {"backend": "Demo"},
           "optics": {"name": "Obj", "input": "objective"}, "spectra": None}
    csv, pdf = tmp_path / "c.csv", tmp_path / "c.pdf"
    rep.write_protocol(rec, str(pdf), str(csv))
    txt = csv.read_text(encoding="utf-8")
    assert "# quantity;radiance" in txt and "# source_hint;Radiance of the sphere port." in txt
    assert "# unit;uW/cm2/sr/nm" in txt
    assert pdf.read_bytes()[:4] == b"%PDF"
    assert rep._type_text("absolute", "radiance") == "absolute radiance"
    assert rep._type_text("relative", "") == "relative"


def test_derivation_protocol_has_quantity(tmp_path):
    rec = {"kind": "derivation_run", "type": "absolute", "quantity": "irradiance",
           "started": "2026-05-01T10:00:00+02:00", "spectra": None}
    csv = tmp_path / "d.csv"
    rep.write_derivation_protocol(rec, None, str(csv))
    assert "# quantity;irradiance" in csv.read_text(encoding="utf-8")

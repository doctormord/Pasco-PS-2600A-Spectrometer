"""Protocol PDF layout: no overlapping text, nothing outside the page margins,
no clipped table cells; new content (NL/linearity on/off/n/a, verification ...)."""
from __future__ import annotations

import numpy as np
import pytest

pdfplumber = pytest.importorskip("pdfplumber")

import calibration_linearity as cl  # noqa: E402
import calibration_report as rep  # noqa: E402

TOL = 0.6                      # pt
M = rep.MARGIN


# ── record builders ──────────────────────────────────────────────────────
LONG = ("This is a deliberately long sentence without any line break that must be wrapped "
        "instead of truncated or overlapping other blocks, including a veryveryveryverylong"
        "unbrokenwordthatexceedsthecolumnwidthofanytablecellinthisprotocolpageforsure. ") * 3


def _calib_record(n_bands=26, nl="off", lin_text="n/a (factory)"):
    wl = np.linspace(340, 850, 600)
    shape = np.exp(-0.5 * ((wl - 620) / 150) ** 2) + 0.05
    C = shape * 1000
    valid = (wl > 380) & (wl < 820)
    fail = np.zeros(wl.size, bool)
    fail[100:110] = True
    cause = {"kind": "stability", "dominant": "dark",
             "message": "Dark offset of -12.3 counts explains 0.8 % of the deviation. " + LONG,
             "causes": [{"cause": "dark offset", "value": -12.3, "unit": "counts",
                         "effect_pct": -0.8, "significant": True, "message": LONG},
                        {"cause": "time offset", "value": 3.0, "unit": "us",
                         "effect_pct": 0.01, "significant": False, "message": "negligible"}],
            "fit": {}}
    bands = [{"lo": 350 + 50 * i, "hi": 400 + 50 * i, "n": 40, "mean_pct": 0.3 * (i % 5) - 0.5,
              "max_abs_pct": 1.0 + 0.4 * (i % 7), "warn": i % 6 == 3} for i in range(n_bands)]
    ver_wl = wl[valid]
    return {
        "type": "absolute", "unit": "uW/cm2/nm",
        "started": "2026-11-25T10:00:00", "finished": "2026-11-25T10:20:00",
        "device": {"backend": "Demo (virtual)", "serial": "SN-1"},
        "optics": {"name": "Very long optics name " + "x" * 30, "input": "cosine corrector",
                   "notes": LONG * 2},
        "reference": {"name": "Reference lamp with a long name " + LONG[:80], "serial": "R-1",
                      "unit": "uW/cm2/nm", "valid_min_nm": 350, "valid_max_nm": 900,
                      "source_file": "C:/very/long/path/" + "folder/" * 12 + "ref.csv"},
        "acquisition": {"integration_us": 20000, "half_integration_us": 10000, "averages": 8,
                        "nl_correction": None, "nl_correction_text": nl,
                        "linearity": {"active": False, "id": "lin-1", "created": "2026-11-20",
                                      "state": "off"},
                        "linearity_text": lin_text, "peak_frac": 0.8,
                        "dark_sequence": "before+after (time-interpolated)"},
        "checks": {"stability_pct": 0.5, "stability_threshold_pct": 1.0, "stability_pass": True,
                   "itime_crosscheck_pct": 2.5, "itime_threshold_pct": 2.0, "itime_pass": False,
                   "stability_delay_s": 5, "stability_fail_pixels": 3, "itime_fail_pixels": 10,
                   "stability_cause": "dark", "itime_cause": "time", "dark_drift_counts": 1.2},
        "pixel_checks": {"stability": {"n_tested": 480, "n_fail": 3, "fail_frac": 0.006,
                                       "k": 4, "threshold_pct": 1.0},
                         "itime": {"n_tested": 480, "n_fail": 10, "fail_frac": 0.02,
                                   "k": 4, "threshold_pct": 2.0}},
        "causes": {"stability": cause, "itime": dict(cause, kind="itime")},
        "dark_drift": [{"integration_us": t, "mean_counts": 1.5 * i, "p95_abs_counts": 3.0,
                        "sigma_counts": 0.2, "seconds": 120.0, "warn": i == 1,
                        "pct_of_peak": 0.05 * i} for i, t in enumerate((10000, 20000, 40000))],
        "plausibility": {"pass": True, "override": False, "failures": [],
                         "hints": ["Gain span is 80x. " + LONG, "second hint"],
                         "metrics": {"coverage": 0.9, "gain_span": 80, "roughness": 0.01},
                         "limits": {"min_coverage": 0.5, "max_gain_span": 50}},
        "gain_span": 80, "n_valid": int(valid.sum()), "valid_range_nm": [380, 820],
        "valid_segments_nm": [[380, 500], [520, 820]], "wl_fingerprint": "abcdef" * 12,
        "verification": {"norm_range_nm": [550, 650], "band_nm": 50, "bands": bands,
                         "overall": {"rms_pct": 1.2, "mean_abs_pct": 0.9, "max_abs_pct": 3.4},
                         "absolute": {"bands": bands[:10],
                                      "overall": {"rms_pct": 2.0, "mean_abs_pct": 1.5,
                                                  "max_abs_pct": 4.0}},
                         "pass": False, "warn_pct": 2.0,
                         "curve": {"wl": ver_wl.tolist(),
                                   "deviation_pct": (np.sin(ver_wl / 30)).tolist()},
                         "reference_name": "Ref", "profile_label": "profile-1",
                         "measurement_id": "m-1", "integration_us": 20000},
        "warnings": [LONG * 2 for _ in range(6)] + ["short"],
        "profiles": [{"id": f"id-{i}", "type": "absolute", "version": 2} for i in range(3)],
        "spectra": {"wl": wl.tolist(), "C_cal": C.tolist(), "E_ref": (shape * 5).tolist(),
                    "S": shape.tolist(), "K": (shape * 2).tolist(), "valid": valid.tolist(),
                    "C_repeat": (C * 1.003).tolist(), "C_half": (C * 0.99).tolist(),
                    "check_fail": fail.tolist()},
    }


def _lin_record():
    x = np.linspace(100, 60000, 40)
    f = x * (1 + 0.03 * x / 65535)
    steps = []
    for i in range(14):
        steps.append({"role": "ref" if i % 4 == 0 else "step", "t_us": 5000 * (i + 1),
                      "peak_pct_fs": 5.0 + 6 * i, "lamp": 1 + 0.001 * i,
                      "rate_raw": 100.0 + i, "rate_corr": 100.0, "dev_raw_pct": -0.3 * i,
                      "dev_corr_pct": 0.01 * i})
    return {"kind": "linearity_run", "started": "2026-11-25T11:00:00",
            "finished": "2026-11-25T11:30:00", "mode": "correction",
            "device": {"backend": "Demo (virtual)", "serial": "SN-9"}, "factory_nl": "off",
            "settings": {f"option_{i}": "value " * 8 for i in range(8)},
            "integration_us": [5000 * (i + 1) for i in range(14)], "ref_us": 20000,
            "steps": steps, "lamp": {"ref_us": 20000, "levels_rel": [1, 1.001, 1.002, 1.001],
                                     "drift_pct": 0.2},
            "dark_drift": [{"integration_us": 10000, "mean_counts": 0.5, "p95_abs_counts": 1.0,
                            "sigma_counts": 0.1, "seconds": 90.0}],
            "model": {"type": "poly_ln_h", "degree": 3, "coeffs": [0.1, -0.2, 0.03],
                      "full_scale": 65535.0, "valid_range_counts": [500.0, 60000.0],
                      "valid_range_frac": [0.0076, 0.915], "rms_before_pct": 1.5,
                      "rms_after_pct": 0.1, "max_deviation_pct": 2.9},
            "curve": {"x_counts": x.tolist(), "f_counts": f.tolist(),
                      "deviation_pct": (3 * x / 65535).tolist()},
            "warnings": [LONG * 2, "lamp drift"], "saved": {"id": "lin-xyz", "mode": "correction",
                                                              "created": "2026-11-25"},
            "activated": True}


# ── geometry checks ──────────────────────────────────────────────────────
def _pages(path):
    with pdfplumber.open(path) as pdf:
        return [(p.width, p.height, p.extract_words(keep_blank_chars=False, use_text_flow=False,
                                                    extra_attrs=["size"]), p.rects)
                for p in pdf.pages]


def _overlap(a, b):
    w = min(a["x1"], b["x1"]) - max(a["x0"], b["x0"])
    h = min(a["bottom"], b["bottom"]) - max(a["top"], b["top"])
    return w > TOL and h > TOL


def _assert_layout(path):
    pages = _pages(path)
    assert pages
    n_words = 0
    for pi, (w, h, words, _rects) in enumerate(pages, 1):
        assert abs(w - 595.28) < 1 and abs(h - 841.89) < 1                      # A4 portrait
        n_words += len(words)
        for wd in words:
            assert wd["x0"] >= M - 2 and wd["x1"] <= w - M + 2, (pi, wd["text"], wd["x0"], wd["x1"])
            assert wd["top"] >= 0 and wd["bottom"] <= h, (pi, wd["text"])
        for i, a in enumerate(words):
            for b in words[i + 1:]:
                assert not _overlap(a, b), (pi, a["text"], b["text"], a, b)
    assert n_words > 50
    return pages


def _text(path):
    with pdfplumber.open(path) as pdf:
        return "\n".join(p.extract_text() or "" for p in pdf.pages)


def _is_cell_edge(col):
    """Table cell borders are drawn in #bbbbbb (0.733 grey); plot rects differ."""
    try:
        vals = list(col) if isinstance(col, (list, tuple)) else [col]
        return all(abs(float(v) - 0.7333) < 0.01 for v in vals)
    except (TypeError, ValueError):
        return False


def _table_cells_contain_text(path):
    """Words that start inside a table cell must end inside it."""
    n_checked = [0]
    with pdfplumber.open(path) as pdf:
        for p in pdf.pages:
            cells = [r for r in p.rects if r["width"] > 10 and 8 < r["height"] < 400
                     and _is_cell_edge(r.get("stroking_color"))]
            if not cells:
                continue
            n_checked[0] += 1
            for wd in p.extract_words():
                inside = [c for c in cells if c["x0"] - TOL <= wd["x0"] <= c["x1"] + TOL
                          and c["top"] - TOL <= wd["top"] <= c["bottom"] + TOL]
                for c in inside:
                    assert wd["x1"] <= c["x1"] + TOL and wd["bottom"] <= c["bottom"] + TOL, \
                        (wd["text"], wd, c)


# ── tests ────────────────────────────────────────────────────────────────
def test_calibration_pdf_no_overlap_multi_page(tmp_path):
    p = rep.write_protocol_pdf(_calib_record(), str(tmp_path / "c.pdf"))
    pages = _assert_layout(p)
    assert len(pages) >= 2                                   # flow layout breaks pages
    _table_cells_contain_text(p)
    txt = _text(p)
    for needle in ("Verification", "Per-pixel checks", "Cause analysis", "Dark drift",
                   "Hints", "Warnings", "Saved profiles", "Linearity corr."):
        assert needle in txt
    # long values are wrapped, not truncated
    assert "ref.csv" in txt.replace("\n", "") or "ref.csv" in txt
    assert txt.replace("\n", " ").count("unbrokenwordthatexceeds") >= 1


def test_warnings_not_truncated(tmp_path):
    rec = _calib_record()
    rec["warnings"] = ["START " + "word " * 120 + "THE_END_MARKER"]
    p = rep.write_protocol_pdf(rec, str(tmp_path / "w.pdf"))
    _assert_layout(p)
    assert "THE_END_MARKER" in _text(p)


@pytest.mark.parametrize("nl,expect", [("on", "on"), ("off", "off"), ("n/a", "n/a")])
def test_nl_correction_text(nl, expect, tmp_path):
    rec = _calib_record(nl=nl)
    csvp = rep.write_protocol_csv(rec, str(tmp_path / "n.csv"))
    meta = {l[2:].split(";", 1)[0]: l[2:].split(";", 1)[1]
            for l in open(csvp, encoding="utf-8").read().splitlines() if l.startswith("# ")
            and ";" in l}
    assert meta["nl_correction"] == expect
    assert meta["linearity_correction"] == "n/a (factory)"
    pdfp = rep.write_protocol_pdf(rec, str(tmp_path / "n.pdf"))
    assert f"NL correction {expect}" in _text(pdfp).replace("\n", " ")


@pytest.mark.parametrize("acq", [{"nl_correction": True}, {"nl_correction": False}, {}])
def test_nl_never_bool_or_empty(acq):
    m = dict(rep._metadata({"acquisition": acq}))
    assert m["nl_correction"] in ("on", "off", "n/a")
    assert m["linearity_correction"] in ("on", "off", "n/a", "n/a (factory)")


def test_calibration_csv_roundtrip(tmp_path):
    rec = _calib_record()
    p = rep.write_protocol_csv(rec, str(tmp_path / "r.csv"))
    lines = open(p, encoding="utf-8").read().splitlines()
    head = [l for l in lines if l.startswith("# ")]
    keys = [l[2:].split(";")[0] for l in head]
    for k in ("nl_correction", "linearity_correction", "linearity_id", "dark_sequence",
              "dark_drift_1", "pixel_check_stability", "pixel_check_itime", "cause_stability_dominant",
              "cause_stability_1", "plausibility_hint_1", "verification_pass",
              "verification_rms_pct", "verification_band_1", "verification_abs_band_1",
              "check_stability_fail_pixels", "warning_1", "profile_1"):
        assert k in keys, k
    assert all(";" in l for l in head[1:])                    # every metadata line has key;value
    cols = lines[len(head)].split(";")
    assert cols == rep.CSV_COLUMNS and cols[-1] == "check_fail"
    body = [l.split(";") for l in lines[len(head) + 1:]]
    assert len(body) == 600 and all(len(r) == len(cols) for r in body)
    ci = cols.index("check_fail")
    assert sum(r[ci] == "1" for r in body) == 10
    assert [float(r[0]) for r in body][0] == pytest.approx(340.0)
    # verification band flagged warn
    vb = {l[2:].split(";")[0]: l for l in head}
    assert "warn=yes" in vb["verification_band_4"]


def test_old_keys_still_present():
    keys = {k for k, _ in rep._metadata({})}
    assert {"started", "device", "optics_name", "t_int_us", "averages_N", "nl_correction",
            "valid_range_nm", "stability_pct", "itime_pass", "wl_fingerprint"} <= keys


def test_empty_record_pdf(tmp_path):
    p = rep.write_protocol_pdf({}, str(tmp_path / "e.pdf"))
    _assert_layout(p)


# ── linearity protocol ───────────────────────────────────────────────────
def test_linearity_basename():
    n = rep.default_linearity_basename(_lin_record())
    assert n == "linearity_Demo-virtual_SN-9_correction_20260125_113000".replace("20260125", "20261125")
    import re
    assert re.fullmatch(r"[A-Za-z0-9_-]+", rep.default_linearity_basename({}))


def test_linearity_protocol_pdf_csv(tmp_path):
    rec = _lin_record()
    out = rep.write_linearity_protocol(rec, pdf_path=str(tmp_path / "l.pdf"),
                                       csv_path=str(tmp_path / "l.csv"))
    assert len(out) == 2 and rep.write_linearity_protocol(rec) == []
    csv_p, pdf_p = out
    _assert_layout(pdf_p)
    _table_cells_contain_text(pdf_p)
    txt = _text(pdf_p)
    for needle in ("Linearity protocol", "Steps", "Dev. raw", "Model", "Warnings",
                   "lin-xyz", "Factory linear. off"):
        assert needle in txt.replace("\n", " ") or needle in txt
    # step table content is in the PDF (14 rows -> 14 distinct t_int us values)
    assert "70000" in txt and "5000" in txt
    lines = open(csv_p, encoding="utf-8").read().splitlines()
    assert lines[0] == "# linearity protocol"
    i_curve = lines.index("# table;curve")
    i_steps = lines.index("# table;steps")
    assert lines[i_curve + 1].split(";") == rep.LINEARITY_CURVE_COLUMNS
    assert lines[i_steps + 1].split(";") == rep.LINEARITY_STEP_COLUMNS
    assert len(lines[i_curve + 2:i_steps]) == 40
    steps = [l.split(";") for l in lines[i_steps + 2:]]
    assert len(steps) == 14 and steps[0][0] == "ref" and float(steps[1][1]) == 10000.0
    meta = {l[2:].split(";", 1)[0]: l[2:].split(";", 1)[1] for l in lines[:i_curve]
            if l.startswith("# ") and ";" in l}
    assert meta["mode"] == "correction" and meta["factory_linearisation"] == "off"
    assert meta["saved_id"] == "lin-xyz" and meta["activated"] == "yes"


def test_linearity_incomplete_record(tmp_path):
    out = rep.write_linearity_protocol({}, str(tmp_path / "x.pdf"), str(tmp_path / "x.csv"))
    assert open(out[1], "rb").read(4) == b"%PDF"
    _assert_layout(out[1])


def test_linearity_real_workflow_on_demo(isolated_config, tmp_path):
    from _device_demo import DemoSpectrometer
    isolated_config._data.update({"demo_spectrum": "cal_lamp", "demo_sensitivity": "synthetic",
                                  "demo_fullscale_ms": 100.0, "demo_nonlinearity": 0.03})
    b = DemoSpectrometer(on_frame=lambda *a: None)
    b.connect("Demo Spectrometer")
    b.set_integration_time_us(20_000)
    b.start()
    try:
        wf = cl.LinearityWorkflow(b, "Demo (virtual)", cl.LinearityConfig(n_steps=7, n_frames=8))
        rec = wf.run_all(sleep=lambda s: None)
    finally:
        b.disconnect()
    out = rep.write_linearity_protocol(rec, str(tmp_path / "d.pdf"), str(tmp_path / "d.csv"))
    _assert_layout(out[1])
    assert len(rec["steps"]) >= 6
    body = open(out[0], encoding="utf-8").read()
    assert len(body.splitlines()) > 20 and "# table;steps" in body

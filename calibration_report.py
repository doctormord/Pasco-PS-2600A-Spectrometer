"""Calibration protocol (PDF + CSV) from a CalibrationWorkflow.record() dict.

Device independent; robust against missing fields / spectra=None.
"""
from __future__ import annotations

import re
from datetime import datetime

CSV_COLUMNS = ["wavelength_nm", "C_cal_counts_per_s", "E_ref", "S_relative", "K",
               "valid", "C_repeat", "C_half"]
_SPEC_KEYS = ["wl", "C_cal", "E_ref", "S", "K", "valid", "C_repeat", "C_half"]


# ── helpers ──────────────────────────────────────────────────────────────
def _slug(s, default="x") -> str:
    s = re.sub(r"[^A-Za-z0-9_-]+", "-", str(s or "")).strip("-")
    return s or default


def _g(d, *keys, default=None):
    for k in keys:
        if not isinstance(d, dict):
            return default
        d = d.get(k)
    return default if d is None else d


def _num(v, fmt="{:.6g}", none=""):
    if v is None:
        return none
    try:
        return fmt.format(float(v))
    except (TypeError, ValueError):
        return str(v)


def _yesno(v, none="n/a"):
    return none if v is None else ("yes" if v else "no")


def _spectra(record: dict) -> dict:
    sp = record.get("spectra") if isinstance(record, dict) else None
    return sp if isinstance(sp, dict) else {}


def default_protocol_basename(record: dict) -> str:
    record = record or {}
    ts = record.get("finished") or record.get("started")
    stamp = None
    if ts:
        try:
            stamp = datetime.fromisoformat(str(ts)).strftime("%Y%m%d_%H%M%S")
        except ValueError:
            digits = re.sub(r"\D", "", str(ts))
            if len(digits) >= 14:
                stamp = f"{digits[:8]}_{digits[8:14]}"
    if stamp is None:
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    parts = ["calib", _slug(_g(record, "device", "backend"), "device"),
             _slug(_g(record, "device", "serial"), "noserial"),
             _slug(_g(record, "optics", "name"), "optics"),
             _slug(record.get("type"), "type"), stamp]
    return re.sub(r"[^A-Za-z0-9_-]", "", "_".join(parts))


def _metadata(record: dict) -> list[tuple[str, str]]:
    r = record or {}
    acq, chk, ref = r.get("acquisition") or {}, r.get("checks") or {}, r.get("reference") or {}
    t = acq.get("integration_us")
    h = acq.get("half_integration_us")
    peak = acq.get("peak_frac")
    vr = r.get("valid_range_nm")
    gs = r.get("gain_span")
    if isinstance(gs, (list, tuple)):
        gs_s = " - ".join(_num(x, "{:.4g}") for x in gs)
    else:
        gs_s = _num(gs, "{:.4g}")
    skipped = chk.get("itime_pass") is None
    m = [
        ("started", r.get("started") or ""), ("finished", r.get("finished") or ""),
        ("device", _g(r, "device", "backend", default="")),
        ("serial", _g(r, "device", "serial", default="")),
        ("optics_name", _g(r, "optics", "name", default="")),
        ("optics_input", _g(r, "optics", "input", default="")),
        ("optics_notes", _g(r, "optics", "notes", default="")),
        ("reference_name", ref.get("name") or ""), ("reference_serial", ref.get("serial") or ""),
        ("reference_unit", ref.get("unit") or ""),
        ("reference_valid_min_nm", _num(ref.get("valid_min_nm"))),
        ("reference_valid_max_nm", _num(ref.get("valid_max_nm"))),
        ("reference_source_file", ref.get("source_file") or ""),
        ("type", r.get("type") or ""),
        ("t_int_us", _num(t, "{:.0f}")), ("t_int_ms", _num(None if t is None else float(t) / 1000, "{:.4g}")),
        ("t_int_half_us", _num(h, "{:.0f}")),
        ("t_int_half_ms", _num(None if h is None else float(h) / 1000, "{:.4g}")),
        ("averages_N", _num(acq.get("averages"), "{:.0f}")),
        ("nl_correction", "" if acq.get("nl_correction") is None else str(bool(acq.get("nl_correction")))),
        ("peak_pct_fs", _num(None if peak is None else 100 * float(peak), "{:.1f}")),
        ("valid_range_nm", "" if not vr else " - ".join(_num(x, "{:.1f}") for x in vr)),
        ("gain_span", gs_s), ("n_valid", _num(r.get("n_valid"), "{:.0f}")),
        ("unit", r.get("unit") or ""),
        ("stability_pct", _num(chk.get("stability_pct"), "{:.3f}")),
        ("stability_threshold_pct", _num(chk.get("stability_threshold_pct"), "{:.3f}")),
        ("stability_delay_s", _num(chk.get("stability_delay_s"), "{:.3g}")),
        ("stability_pass", _yesno(chk.get("stability_pass"), "")),
        ("itime_crosscheck_pct", _num(chk.get("itime_crosscheck_pct"), "{:.3f}")),
        ("itime_threshold_pct", _num(chk.get("itime_threshold_pct"), "{:.3f}")),
        ("itime_pass", "skipped" if skipped else _yesno(chk.get("itime_pass"))),
        ("wl_fingerprint", r.get("wl_fingerprint") or ""),
    ]
    for i, w in enumerate(r.get("warnings") or [], 1):
        m.append((f"warning_{i}", str(w)))
    for i, p in enumerate(r.get("profiles") or [], 1):
        m.append((f"profile_{i}", f"{p.get('id', '')} type={p.get('type', '')} "
                                  f"version={p.get('version', '')}"))
    return m


# ── CSV ──────────────────────────────────────────────────────────────────
def _cell(v):
    if v is None:
        return ""
    if isinstance(v, bool):
        return "1" if v else "0"
    try:
        return repr(float(v))
    except (TypeError, ValueError):
        return str(v)


def write_protocol_csv(record: dict, path: str) -> str:
    sp = _spectra(record)
    cols = {k: sp.get(k) for k in _SPEC_KEYS}
    wl = cols["wl"] or []
    lines = ["# calibration protocol"]
    for k, v in _metadata(record):
        v = str(v).replace(";", ",").replace("\r", " ").replace("\n", " ")
        lines.append(f"# {k};{v}")
    lines.append(";".join(CSV_COLUMNS))
    for i in range(len(wl)):
        row = []
        for k in _SPEC_KEYS:
            c = cols[k]
            row.append(_cell(c[i] if c is not None and i < len(c) else None))
        lines.append(";".join(row))
    with open(path, "w", encoding="utf-8", newline="") as f:
        f.write("\r\n".join(lines) + "\r\n")
    return path


# ── PDF ──────────────────────────────────────────────────────────────────
def _arr(np, a, n):
    if a is None:
        return np.full(n, np.nan)
    out = np.array([np.nan if v is None else float(v) for v in a], float)
    if len(out) < n:
        out = np.concatenate([out, np.full(n - len(out), np.nan)])
    return out[:n]


def write_protocol_pdf(record: dict, path: str) -> str:
    import matplotlib
    matplotlib.use("Agg")
    import numpy as np
    from matplotlib.backends.backend_pdf import PdfPages
    from matplotlib.figure import Figure

    record = record or {}
    sp = _spectra(record)
    wl = np.array([np.nan if v is None else float(v) for v in (sp.get("wl") or [])], float)
    n = len(wl)
    C, E = _arr(np, sp.get("C_cal"), n), _arr(np, sp.get("E_ref"), n)
    S, K = _arr(np, sp.get("S"), n), _arr(np, sp.get("K"), n)
    rep, half = _arr(np, sp.get("C_repeat"), n), _arr(np, sp.get("C_half"), n)
    vl = sp.get("valid")
    valid = np.zeros(n, bool) if vl is None else np.array(
        [bool(v) for v in vl] + [False] * max(0, n - len(vl)), bool)[:n]
    vr = record.get("valid_range_nm")
    chk = record.get("checks") or {}
    kind = record.get("type")
    unit = record.get("unit") or ""
    GREY, BLUE, ORANGE, RED = "#d9d9d9", "#1f5fa8", "#d9730d", "#b00020"

    def shade(ax):
        if n and vr and len(vr) == 2 and None not in vr:
            ax.axvspan(np.nanmin(wl), vr[0], color=GREY, alpha=0.6, lw=0)
            ax.axvspan(vr[1], np.nanmax(wl), color=GREY, alpha=0.6, lw=0)
            ax.axvspan(vr[0], vr[1], color="#eef6ee", alpha=0.6, lw=0)

    def nodata(ax, txt="no data"):
        ax.text(0.5, 0.5, txt, ha="center", va="center", transform=ax.transAxes, color="grey")

    fig = Figure(figsize=(8.27, 11.69), facecolor="white")
    fig.text(0.06, 0.965, "Calibration protocol", fontsize=16, weight="bold", va="top")
    meta = dict(_metadata(record))
    left = [("Started", meta["started"]), ("Finished", meta["finished"]),
            ("Device", meta["device"]), ("Serial", meta["serial"] or "-"),
            ("Type", meta["type"]),
            ("Optics", f"{meta['optics_name']} ({meta['optics_input']})"),
            ("Notes", meta["optics_notes"] or "-"),
            ("Reference", f"{meta['reference_name']} (S/N {meta['reference_serial'] or '-'})"),
            ("Ref. unit / range", f"{meta['reference_unit']}  "
                                  f"{meta['reference_valid_min_nm']}-{meta['reference_valid_max_nm']} nm"),
            ("Ref. file", meta["reference_source_file"] or "-")]
    right = [("t_int", f"{meta['t_int_us']} us ({meta['t_int_ms']} ms)"),
             ("t_int / 2", f"{meta['t_int_half_us']} us ({meta['t_int_half_ms']} ms)"),
             ("Averages N", meta["averages_N"]), ("NL correction", meta["nl_correction"]),
             ("Peak", f"{meta['peak_pct_fs']} % FS"),
             ("Valid range", f"{meta['valid_range_nm']} nm"),
             ("Gain span", meta["gain_span"]), ("Valid pixels", meta["n_valid"]),
             ("Unit", meta["unit"]), ("WL fingerprint", (meta["wl_fingerprint"] or "-")[:24])]
    for col, items, x in ((0, left, 0.06), (1, right, 0.54)):
        for i, (k, v) in enumerate(items):
            y = 0.935 - i * 0.0155
            fig.text(x, y, k, fontsize=7.5, color="#555555", va="top")
            fig.text(x + 0.13, y, str(v)[:48], fontsize=7.5, va="top")

    def axes(rect):
        a = fig.add_axes(rect)
        a.set_facecolor("white")
        a.grid(alpha=0.3)
        return a

    # plot 1
    a1 = axes([0.09, 0.615, 0.82, 0.14])
    if n and np.isfinite(C).any():
        shade(a1)
        a1.plot(wl, C, color=BLUE, lw=1.2, label="C_cal")
        a1.set_ylabel("C_cal (counts/s)", color=BLUE)
        a2 = a1.twinx()
        a2.plot(wl, E, color=ORANGE, lw=1.2, label="E_ref")
        a2.set_ylabel(f"E_ref ({record.get('reference', {}).get('unit', '')})", color=ORANGE)
        a1.set_xlabel("wavelength (nm)")
    else:
        nodata(a1)
    a1.set_title("Calibration spectrum and reference", fontsize=9, loc="left")

    # plot 2
    a3 = axes([0.09, 0.415, 0.82, 0.14])
    res = K if kind == "absolute" else S
    lab = f"K ({unit})" if kind == "absolute" else "S (relative)"
    if n and np.isfinite(res).any():
        shade(a3)
        a3.plot(wl, np.where(valid, res, np.nan), color=BLUE, lw=1.3)
        a3.set_ylabel(lab)
        a3.set_xlabel("wavelength (nm)")
    else:
        nodata(a3)
    a3.set_title("Result (valid pixels only)", fontsize=9, loc="left")

    # plot 3
    a4 = axes([0.09, 0.215, 0.82, 0.14])
    with np.errstate(divide="ignore", invalid="ignore"):
        r_rep, r_half = rep / C, half / C
    has_rep, has_half = np.isfinite(r_rep[valid]).any() if n else False, \
        np.isfinite(r_half[valid]).any() if n else False
    if has_rep or has_half:
        shade(a4)
        if has_rep:
            a4.plot(wl, np.where(valid, r_rep, np.nan), color=BLUE, lw=1, label="C_repeat / C_cal")
        if has_half:
            a4.plot(wl, np.where(valid, r_half, np.nan), color=ORANGE, lw=1, label="C_half / C_cal")
        for key, c in (("stability_threshold_pct", BLUE), ("itime_threshold_pct", ORANGE)):
            th = chk.get(key)
            if th is not None and ((key[0] == "s" and has_rep) or (key[0] == "i" and has_half)):
                for s in (1, -1):
                    a4.axhline(1 + s * float(th) / 100, color=c, ls="--", lw=0.8)
        a4.legend(fontsize=7, loc="upper right")
        a4.set_xlabel("wavelength (nm)")
        a4.set_ylabel("ratio")
    else:
        nodata(a4, "no check spectra")
    a4.set_title("Checks (dashed: +/- threshold band)", fontsize=9, loc="left")

    # check table + warnings
    skipped = chk.get("itime_pass") is None
    rows = [["Stability (p95)", _num(chk.get("stability_pct"), "{:.2f} %", "n/a"),
             _num(chk.get("stability_threshold_pct"), "{:.2f} %", "n/a"),
             _num(chk.get("stability_delay_s"), "delay {:.3g} s", ""),
             {True: "passed", False: "FAILED"}.get(chk.get("stability_pass"), "n/a")],
            ["Integration time", _num(chk.get("itime_crosscheck_pct"), "{:.2f} %", "n/a"),
             _num(chk.get("itime_threshold_pct"), "{:.2f} %", "n/a"), "t/2 vs t",
             "skipped" if skipped else ("passed" if chk.get("itime_pass") else "FAILED")]]
    at = fig.add_axes([0.06, 0.125, 0.88, 0.06])
    at.axis("off")
    tb = at.table(cellText=rows, colLabels=["Check", "Deviation", "Threshold", "Note", "Result"],
                  loc="upper center", cellLoc="left")
    tb.auto_set_font_size(False)
    tb.set_fontsize(8)
    for (ri, ci), cell in tb.get_celld().items():
        cell.set_edgecolor("#bbbbbb")
        if ci == 4 and ri > 0 and "FAILED" in rows[ri - 1][4]:
            cell.get_text().set_color(RED)
    ws = record.get("warnings") or []
    fig.text(0.06, 0.1, "Warnings", fontsize=9, weight="bold", va="top")
    txt = "\n".join(f"- {w}"[:120] for w in ws[:8]) if ws else "none"
    fig.text(0.06, 0.082, txt, fontsize=7.5, va="top")
    profs = record.get("profiles") or []
    ptxt = "\n".join(f"{p.get('id', '')}  type={p.get('type', '')}  v{p.get('version', '')}"
                     for p in profs[:4]) or "none"
    fig.text(0.54, 0.1, "Saved profiles", fontsize=9, weight="bold", va="top")
    fig.text(0.54, 0.082, ptxt, fontsize=7.5, va="top")

    with PdfPages(path) as pdf:
        pdf.savefig(fig)
    return path


def write_protocol(record: dict, pdf_path: str | None = None,
                   csv_path: str | None = None) -> list[str]:
    out = []
    if csv_path:
        out.append(write_protocol_csv(record, csv_path))
    if pdf_path:
        out.append(write_protocol_pdf(record, pdf_path))
    return out

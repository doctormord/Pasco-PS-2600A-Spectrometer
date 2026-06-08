"""filter_analysis.py — device-agnostic optical-filter characterization.

Given a *reference* spectrum (the light source measured WITHOUT the filter, i.e.
the 100% baseline) and a *sample* spectrum (the same source measured THROUGH the
filter), compute the spectral transmission T(λ) = sample / reference and derive
the classic filter metrics: type (long-pass / short-pass / band-pass / band-stop
/ neutral-density), peak transmission, centre wavelength, FWHM / bandwidth, the
50% cut-on / cut-off edges, edge steepness (10–90% Δλ), passband-average
transmission, and blocking expressed as optical density OD = −log10(T).

This module is pure numpy — no hardware, no GUI, no device specifics — so both
the desktop app and the web server can share it (mirrors the reference-library
split). All wavelengths are in nm; transmission is returned as a fraction (the
UI multiplies by 100 for %). Metrics that don't apply to the detected type are
returned as ``None``.
"""

from __future__ import annotations

import numpy as np


def smooth_for_display(y, window: int):
    """NaN-aware moving average for the LIVE transmission curve only (display
    cosmetics — never feed this into metrics). `window` is the number of points
    (forced odd, ≥3); NaN gaps are preserved so masked regions stay masked."""
    import numpy as _np
    y = _np.asarray(y, dtype=float)
    w = int(window)
    if w < 3 or y.size == 0:
        return y
    if w % 2 == 0:
        w += 1
    half = w // 2
    finite = _np.isfinite(y)
    vals = _np.where(finite, y, 0.0)
    kernel = _np.ones(w)
    num = _np.convolve(vals, kernel, mode="same")
    den = _np.convolve(finite.astype(float), kernel, mode="same")
    with _np.errstate(invalid="ignore", divide="ignore"):
        out = num / den
    out[~finite] = _np.nan          # keep original NaN gaps
    out[den == 0] = _np.nan
    return out


def _render_report_meta(fig, meta: dict | None) -> None:
    """Draw an optional ordered acquisition-parameter dict as a monospaced header
    block at the top of the report figure (mirrors color_science)."""
    if not meta:
        return
    items = [f"{k}: {v}" for k, v in meta.items() if v not in (None, "")]
    if not items:
        return
    lines = ["    ".join(items[i:i + 3]) for i in range(0, len(items), 3)]
    fig.text(0.06, 0.997, "\n".join(lines), family="monospace", fontsize=7.0,
             va="top", ha="left", color="#222222")

# Transmission level (fraction of peak) that defines the band edges / FWHM.
HALF = 0.5
# Reference samples below this fraction of the reference peak carry no usable
# light, so T there is division noise — masked out of the analysis.
DEFAULT_REF_FLOOR_FRAC = 0.02
# A filter whose transmission varies by less than this (peak − min, in fraction
# of peak) AND never reaches full transmission is treated as neutral-density.
ND_FLATNESS = 0.25
ND_MAX_T = 0.92


def _interp_crossings(x: np.ndarray, y: np.ndarray, level: float):
    """All x where y crosses ``level`` (linearly interpolated), each tagged with
    a direction (+1 rising, −1 falling). NaNs break segments."""
    out = []
    for i in range(len(y) - 1):
        y0, y1 = y[i], y[i + 1]
        if not (np.isfinite(y0) and np.isfinite(y1)):
            continue
        d0, d1 = y0 - level, y1 - level
        if d0 == 0.0:
            out.append((float(x[i]), 1 if y1 >= y0 else -1))
        elif d0 * d1 < 0.0:
            t = d0 / (y0 - y1) if (y0 - y1) != 0 else 0.0
            xc = x[i] + t * (x[i + 1] - x[i])
            out.append((float(xc), 1 if y1 > y0 else -1))
    return out


def _edge_steepness(x, y, peak, x_edge, rising):
    """10%→90%-of-peak Δλ (nm) around the edge nearest ``x_edge``. Smaller =
    steeper. Returns None if the 10%/90% levels can't both be bracketed."""
    lvl10, lvl90 = 0.10 * peak, 0.90 * peak
    c10 = _interp_crossings(x, y, lvl10)
    c90 = _interp_crossings(x, y, lvl90)
    want = 1 if rising else -1
    pick = lambda cs: min(
        (c for c in cs if c[1] == want), key=lambda c: abs(c[0] - x_edge),
        default=None)
    a, b = pick(c10), pick(c90)
    if a is None or b is None:
        return None
    return abs(b[0] - a[0])


def analyze_filter(
    wavelengths,
    reference,
    sample,
    *,
    ref_floor_frac: float = DEFAULT_REF_FLOOR_FRAC,
    level: float = HALF,
) -> dict:
    """Characterize a filter from a reference (no filter) and sample (through
    filter) spectrum on a shared wavelength axis. See module docstring."""
    wl = np.asarray(wavelengths, dtype=float)
    ref = np.asarray(reference, dtype=float)
    smp = np.asarray(sample, dtype=float)
    if wl.shape != ref.shape or wl.shape != smp.shape or wl.size < 8:
        raise ValueError("wavelengths, reference and sample must share a length >= 8")

    # Ensure ascending λ.
    if wl[0] > wl[-1]:
        wl, ref, smp = wl[::-1], ref[::-1], smp[::-1]

    # Transmission, masked where the reference has no usable light.
    ref_peak = float(np.nanmax(ref)) if np.any(np.isfinite(ref)) else 0.0
    floor = ref_floor_frac * ref_peak
    with np.errstate(divide="ignore", invalid="ignore"):
        T = smp / ref
    valid = np.isfinite(T) & (ref > floor)
    T = np.where(valid, T, np.nan)
    # Clip tiny negatives (dark-subtraction noise) to 0; allow >1 (gain/ripple).
    T = np.where(np.isfinite(T), np.clip(T, 0.0, None), np.nan)

    result = {
        "wavelengths": wl,
        "transmission": T,                 # fraction, nan where invalid
        "type": "unknown",
        "peak_T_pct": None, "peak_wavelength_nm": None,
        "min_T_pct": None,
        "center_wavelength_nm": None, "fwhm_nm": None,
        "left_edge_nm": None, "right_edge_nm": None,
        "cut_on_nm": None, "cut_off_nm": None,
        "edge_steepness_left_nm": None, "edge_steepness_right_nm": None,
        "avg_T_passband_pct": None,
        "blocking_min_T_pct": None, "peak_OD": None, "avg_OD_blocking": None,
        "od_at_center": None,
        "valid_range_nm": None,
        "notes": "",
    }

    if not np.any(valid):
        result["notes"] = "No overlap with usable reference light."
        return result

    vwl = wl[valid]
    vT = T[valid]
    result["valid_range_nm"] = (float(vwl[0]), float(vwl[-1]))

    peak = float(np.nanmax(vT))
    tmin = float(np.nanmin(vT))
    if peak <= 0:
        result["notes"] = "Sample shows no transmission over the reference band."
        result["peak_T_pct"] = 0.0
        result["min_T_pct"] = 0.0
        return result

    result["peak_T_pct"] = 100.0 * peak
    result["min_T_pct"] = 100.0 * tmin
    result["peak_wavelength_nm"] = float(vwl[int(np.nanargmax(vT))])

    # Work on the valid samples; classify on the peak-normalised curve.
    Tn = vT / peak
    hi = Tn >= level                       # "passing" mask
    left_hi, right_hi = bool(hi[0]), bool(hi[-1])
    span = float(np.nanmax(Tn) - np.nanmin(Tn))

    # Half-of-peak crossings on the *absolute* transmission (for edges/FWHM).
    half_level = level * peak
    crossings = _interp_crossings(vwl, vT, half_level)

    # ---- Classification ------------------------------------------------------
    if span < ND_FLATNESS and peak < ND_MAX_T:
        ftype = "neutral"
    elif left_hi and right_hi and np.any(~hi):
        ftype = "bandstop"
    elif (not left_hi) and (not right_hi) and np.any(hi):
        ftype = "bandpass"
    elif right_hi and not left_hi:
        ftype = "longpass"                 # passes long wavelengths
    elif left_hi and not right_hi:
        ftype = "shortpass"                # passes short wavelengths
    else:
        ftype = "unknown"
    result["type"] = ftype

    # ---- Type-specific metrics ----------------------------------------------
    if ftype == "neutral":
        avgT = float(np.nanmean(vT))
        result["avg_T_passband_pct"] = 100.0 * avgT
        result["od_at_center"] = (-np.log10(avgT)) if avgT > 0 else float("inf")
        result["peak_OD"] = (-np.log10(tmin)) if tmin > 0 else float("inf")
        result["notes"] = "Roughly flat transmission — neutral-density."

    elif ftype in ("bandpass", "bandstop"):
        # Edges = the two half-level crossings bracketing the feature.
        feat_wl = result["peak_wavelength_nm"] if ftype == "bandpass" \
            else float(vwl[int(np.nanargmin(vT))])
        left = [c for c in crossings if c[0] <= feat_wl]
        right = [c for c in crossings if c[0] >= feat_wl]
        l_edge = max((c[0] for c in left), default=None)
        r_edge = min((c[0] for c in right), default=None)
        result["left_edge_nm"] = l_edge
        result["right_edge_nm"] = r_edge
        if l_edge is not None and r_edge is not None and r_edge > l_edge:
            result["center_wavelength_nm"] = 0.5 * (l_edge + r_edge)
            result["fwhm_nm"] = r_edge - l_edge
            band = (vwl >= l_edge) & (vwl <= r_edge)
            if ftype == "bandpass" and np.any(band):
                result["avg_T_passband_pct"] = 100.0 * float(np.nanmean(vT[band]))
            result["edge_steepness_left_nm"] = _edge_steepness(
                vwl, vT, peak, l_edge, rising=(ftype == "bandpass"))
            result["edge_steepness_right_nm"] = _edge_steepness(
                vwl, vT, peak, r_edge, rising=(ftype == "bandstop"))
        if ftype == "bandstop":
            result["blocking_min_T_pct"] = 100.0 * tmin
            result["peak_OD"] = (-np.log10(tmin)) if tmin > 0 else float("inf")
        elif ftype == "bandpass" and l_edge is not None and r_edge is not None:
            # Blocking = transmission OUTSIDE the passband (the stop regions).
            stop = (vwl < l_edge) | (vwl > r_edge)
            if np.any(stop):
                sb_min = float(np.nanmin(vT[stop]))
                result["blocking_min_T_pct"] = 100.0 * sb_min
                result["peak_OD"] = (-np.log10(sb_min)) if sb_min > 0 else float("inf")
        result["notes"] = (
            "Band-pass" if ftype == "bandpass" else "Band-stop / notch") + " filter."

    elif ftype in ("longpass", "shortpass"):
        rising = (ftype == "longpass")     # T rises with λ for a long-pass
        want = 1 if rising else -1
        edges = [c for c in crossings if c[1] == want]
        # The dominant edge = the half-crossing furthest into the band transition
        edge = (max if rising else min)((c[0] for c in edges), default=None) \
            if edges else None
        if edge is None and crossings:
            edge = crossings[0][0]
        if edge is not None:
            if rising:
                result["cut_on_nm"] = edge
            else:
                result["cut_off_nm"] = edge
            result["edge_steepness_left_nm" if rising else "edge_steepness_right_nm"] = \
                _edge_steepness(vwl, vT, peak, edge, rising=rising)
            # Passband-average over the half that passes.
            pb = (vwl >= edge) if rising else (vwl <= edge)
            if np.any(pb):
                result["avg_T_passband_pct"] = 100.0 * float(np.nanmean(vT[pb]))
            # Blocking on the stop side.
            sb = ~pb
            if np.any(sb):
                sb_min = float(np.nanmin(vT[sb]))
                result["blocking_min_T_pct"] = 100.0 * sb_min
                result["peak_OD"] = (-np.log10(sb_min)) if sb_min > 0 else float("inf")
        result["notes"] = (
            "Long-pass (passes long λ)" if rising
            else "Short-pass (passes short λ)") + " filter."

    else:
        result["notes"] = "Could not classify the filter shape."

    return result


def metrics_rows(res: dict, full: bool = False) -> list[tuple[str, str]]:
    """Flatten an analyze_filter result into ordered (label, value-string) rows
    for a table / CSV / report. By default not-applicable (None) metrics are
    skipped; with full=True the key metrics — slope, OD, FWHM, edges, blocking —
    are always shown ("—" when N/A) so a report/CSV is complete and consistent."""
    def nm(v):  return None if v is None else f"{v:.2f} nm"
    def pct(v): return None if v is None else f"{v:.2f} %"
    def od(v):
        if v is None:
            return None
        return "∞ (full block)" if not np.isfinite(v) else f"{v:.2f}"
    type_label = {
        "bandpass": "Band-pass", "bandstop": "Band-stop / notch",
        "longpass": "Long-pass", "shortpass": "Short-pass",
        "neutral": "Neutral-density", "unknown": "Unknown",
    }.get(res.get("type"), res.get("type"))

    # Metrics that should always appear in a full report/CSV even when N/A.
    ALWAYS = {
        "FWHM / bandwidth", "Edge steepness, left (10–90%)",
        "Edge steepness, right (10–90%)", "Peak optical density (OD)",
        "Blocking min transmission",
    }

    candidates = [
        ("Filter type", type_label),
        ("Valid range", None if not res.get("valid_range_nm")
            else f"{res['valid_range_nm'][0]:.1f}–{res['valid_range_nm'][1]:.1f} nm"),
        ("Peak transmission", pct(res.get("peak_T_pct"))),
        ("Peak wavelength", nm(res.get("peak_wavelength_nm"))),
        ("Centre wavelength", nm(res.get("center_wavelength_nm"))),
        ("FWHM / bandwidth", nm(res.get("fwhm_nm"))),
        ("Left edge (50%)", nm(res.get("left_edge_nm"))),
        ("Right edge (50%)", nm(res.get("right_edge_nm"))),
        ("Cut-on (50%)", nm(res.get("cut_on_nm"))),
        ("Cut-off (50%)", nm(res.get("cut_off_nm"))),
        ("Edge steepness, left (10–90%)", nm(res.get("edge_steepness_left_nm"))),
        ("Edge steepness, right (10–90%)", nm(res.get("edge_steepness_right_nm"))),
        ("Passband avg transmission", pct(res.get("avg_T_passband_pct"))),
        ("Min transmission", pct(res.get("min_T_pct"))),
        ("Blocking min transmission", pct(res.get("blocking_min_T_pct"))),
        ("Peak optical density (OD)", od(res.get("peak_OD"))),
        ("OD at centre (ND)", od(res.get("od_at_center"))),
        ("Avg OD (blocking)", od(res.get("avg_OD_blocking"))),
    ]
    out = []
    for k, v in candidates:
        if v is not None:
            out.append((k, v))
        elif full and k in ALWAYS:
            out.append((k, "—"))
    return out


_TYPE_LABEL = {
    "bandpass": "Band-pass", "bandstop": "Band-stop / notch",
    "longpass": "Long-pass", "shortpass": "Short-pass",
    "neutral": "Neutral-density", "unknown": "Unknown",
}


def render_report(dst, res: dict, fmt: str = "pdf", dpi: int = 150,
                  meta: dict | None = None) -> None:
    """Render a self-contained filter report — transmission plot (with 50% line
    and edge/centre markers) plus the full metrics table, on a WHITE background —
    to ``dst`` (a file path or a binary file-like). ``fmt`` is 'pdf' or 'png'.
    ``meta`` is an optional ordered dict of acquisition parameters rendered as a
    monospaced header block. Shared by the web server and the desktop app so both
    produce identical reports. Raises ImportError if matplotlib is not installed."""
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    wl = np.asarray(res["wavelengths"], dtype=float)
    T = np.asarray(res["transmission"], dtype=float) * 100.0
    finite = np.isfinite(T)

    fig = plt.figure(figsize=(8.27, 7.4), facecolor="white")   # A4 width
    _render_report_meta(fig, meta)
    ax = fig.add_axes([0.10, 0.50, 0.85, 0.42])
    ax.set_facecolor("white")
    if np.any(finite):
        ax.plot(wl[finite], T[finite], color="#0d9488", lw=1.6)
    ax.axhline(50, ls="--", lw=0.8, color="#999999")
    for key, col, style in (("center_wavelength_nm", "#0d9488", ":"),
                            ("left_edge_nm",  "#b8860b", "--"),
                            ("right_edge_nm", "#b8860b", "--"),
                            ("cut_on_nm",     "#b8860b", "--"),
                            ("cut_off_nm",    "#b8860b", "--")):
        v = res.get(key)
        if v is not None:
            ax.axvline(v, ls=style, lw=0.9, color=col)
    ax.set_xlabel("Wavelength (nm)")
    ax.set_ylabel("Transmission (%)")
    top = max(110.0, float(np.nanmax(T)) * 1.05) if np.any(finite) else 110.0
    ax.set_ylim(0, top)
    ax.grid(alpha=0.3)
    ax.set_title("Optical filter characterization — "
                 + _TYPE_LABEL.get(res.get("type"), str(res.get("type"))))

    rows = metrics_rows(res, full=True)
    tax = fig.add_axes([0.10, 0.04, 0.85, 0.40])
    tax.axis("off")
    table = tax.table(cellText=[[k, v] for k, v in rows],
                      colLabels=["Metric", "Value"], loc="upper center",
                      cellLoc="left", colWidths=[0.60, 0.40])
    table.auto_set_font_size(False)
    table.set_fontsize(9)
    table.scale(1.0, 1.3)

    fig.savefig(dst, format=fmt, dpi=dpi, facecolor="white")
    plt.close(fig)

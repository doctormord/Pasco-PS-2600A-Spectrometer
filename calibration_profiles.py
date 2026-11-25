"""
calibration_profiles.py — Device-agnostic calibration store + capture helpers.

Two independent calibration kinds with different physics and lifetimes:

1. **Wavelength solution** — ONE per device (pixel -> lambda, degree-3 polynomial).
   A property of grating/detector geometry; does NOT change when a fibre is
   swapped. Persisted as a per-device config key (e.g. "pasco_wl_poly_coeffs");
   the driver loads it in connect() and the wavelength wizard writes it. The fit
   math lives in calibration_utils.fit_wavelength_polynomial.

2. **Spectral response correction** — a NAMED, per-device set of [lambda, S]
   tables (the same format as a driver RESPONSE_TABLE; sensitivity normalised to
   1.0 at peak). This is what actually varies per fibre/optical setup. Stored in
   a separate JSON registry (response tables are large arrays). The GUI offers a
   per-device dropdown:
       "None"           -> no correction (gain = ones, behaviour unchanged)
       "Device default" -> the driver's built-in RESPONSE_TABLE (if any)
       <profile name>   -> a saved table from this registry
   The active selection per device lives in the MAIN config under
   "response_correction_active" ({device: selection}); the legacy boolean
   "spectral_response_compensation" is kept in sync so processing.py needs no
   change.

All maths here is device-agnostic; drivers only consume the results (they
already accept a wavelength solution via calibrate_from_lines and a response
table via RESPONSE_TABLE).
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import asdict, dataclass

import numpy as np

from calibration_utils import (
    build_response_gain,
    wavelength_to_pixel,
)


# ══════════════════════════════════════════════════════════════════════════
#  Known emission lines for the wavelength wizard
# ══════════════════════════════════════════════════════════════════════════
# Curated strong, reasonably isolated lines per discharge lamp (NIST values).
# Used to pre-fill the wizard's line-assignment table. Broad phosphor/LED bands
# are intentionally excluded — only sharp lines make good calibration anchors.

CALIBRATION_ELEMENT_LINES: dict[str, list[float]] = {
    "Mercury (Hg)": [365.0, 404.7, 435.8, 546.1, 576.9, 579.1],
    "Cadmium (Cd)": [346.6, 361.1, 467.8, 480.0, 508.6, 643.8],
    "Neon (Ne)":    [585.2, 594.5, 607.4, 614.3, 640.2, 650.6,
                     692.9, 703.2, 717.4, 724.5, 743.9],
    "Argon (Ar)":   [696.5, 706.7, 727.3, 738.4, 750.4, 763.5,
                     772.4, 794.8, 811.5, 826.5, 842.5],
    "Helium (He)":  [388.9, 447.1, 471.3, 492.2, 501.6, 587.6,
                     667.8, 706.5, 728.1],
    "Krypton (Kr)": [427.4, 431.9, 436.3, 450.2, 557.0, 587.1,
                     760.2, 769.5, 810.4, 819.0, 829.8],
    "Xenon (Xe)":   [450.1, 462.4, 467.1, 473.4, 480.7, 492.3,
                     823.2, 828.0, 834.7, 880.0],
    "Sodium (Na)":  [589.0, 589.6],
    "Hydrogen (H)": [410.2, 434.0, 486.1, 656.3],
}


def _combine_lines(*element_keys: str, min_sep_nm: float = 1.5) -> list[float]:
    """Union of several elements' lines, sorted, with lines closer than
    min_sep_nm collapsed to one. Two known lines that fall inside the same
    detector peak can't be assigned separately (that's the exact trap that
    produces impossible matches), so near-coincident lines are merged."""
    lines = sorted(l for k in element_keys for l in CALIBRATION_ELEMENT_LINES[k])
    merged: list[float] = []
    for l in lines:
        if not merged or (l - merged[-1]) >= min_sep_nm:
            merged.append(l)
    return merged


# Combination lamps (single bulb containing several fill gases/metals, or a
# common pen-ray pairing). Selecting one matches ALL its elements against one
# capture in a single step — no need to switch lamps in the wizard.
CALIBRATION_COMBO_LINES: dict[str, list[float]] = {
    "Cadmium-Mercury (Cd/Hg)":      _combine_lines("Cadmium (Cd)", "Mercury (Hg)"),
    "Mercury-Argon (Hg/Ar)":        _combine_lines("Mercury (Hg)", "Argon (Ar)"),
    "Mercury-Neon (Hg/Ne)":         _combine_lines("Mercury (Hg)", "Neon (Ne)"),
    "Neon-Argon (Ne/Ar)":           _combine_lines("Neon (Ne)", "Argon (Ar)"),
    "Argon-Krypton (Ar/Kr)":        _combine_lines("Argon (Ar)", "Krypton (Kr)"),
    "Mercury-Cadmium-Argon (Hg/Cd/Ar)":
        _combine_lines("Mercury (Hg)", "Cadmium (Cd)", "Argon (Ar)"),
    "Mercury-Neon-Argon (Hg/Ne/Ar)":
        _combine_lines("Mercury (Hg)", "Neon (Ne)", "Argon (Ar)"),
}

# Public list the wizard reads: pure elements first, then combination lamps.
CALIBRATION_LINES: dict[str, list[float]] = {
    **CALIBRATION_ELEMENT_LINES,
    **CALIBRATION_COMBO_LINES,
}


# ══════════════════════════════════════════════════════════════════════════
#  Peak detection (sub-pixel) for the wavelength wizard
# ══════════════════════════════════════════════════════════════════════════

def detect_peaks(
    intensity,
    *,
    prominence: float | None = None,
    min_distance_px: int = 8,
    max_peaks: int = 60,
) -> list[float]:
    """
    Find emission-line peaks in a 1-D spectrum, refined to sub-pixel precision
    by parabolic interpolation around each integer maximum.

    Returns fractional pixel positions, strongest peak first.

    prominence defaults to 0.4 % of the spectrum's dynamic range if not given.
    (A high default like 2 % of a range dominated by one very strong line
    silently drops genuine but weaker calibration lines — e.g. Hg 404.7 next to
    a 50 000-ADC line — so the wizard then can't anchor those and mis-assigns.)
    """
    from scipy.signal import find_peaks

    y = np.asarray(intensity, dtype=float)
    if y.size < 3:
        return []
    if prominence is None:
        rng = float(np.nanmax(y) - np.nanmin(y))
        prominence = max(rng * 0.004, 1.0)

    idx, _ = find_peaks(
        y, prominence=prominence, distance=max(1, int(min_distance_px))
    )

    refined: list[tuple[float, float]] = []
    for i in idx:
        if 0 < i < len(y) - 1:
            ym1, y0, yp1 = y[i - 1], y[i], y[i + 1]
            denom = (ym1 - 2.0 * y0 + yp1)
            delta = 0.5 * (ym1 - yp1) / denom if denom != 0 else 0.0
            refined.append((float(i) + float(np.clip(delta, -0.5, 0.5)), float(y0)))
        else:
            refined.append((float(i), float(y[i])))

    refined.sort(key=lambda t: t[1], reverse=True)
    return [p for p, _ in refined[:max_peaks]]


def _assign_nearest(peak_nm, known_nm, tol_nm):
    """Globally pair known lines to peaks by ascending distance (nearest pair
    first), each peak and each line used at most once. Order-independent, unlike
    a per-line greedy scan — so a bright peak from another element in the same
    lamp can't 'steal' a line just because that line was listed first.

    Returns {known_index: peak_index}.
    """
    cand = [
        (abs(pnm - k), ki, pj)
        for ki, k in enumerate(known_nm)
        for pj, pnm in enumerate(peak_nm)
        if abs(pnm - k) <= tol_nm
    ]
    cand.sort()
    used_k: set[int] = set()
    used_p: set[int] = set()
    out: dict[int, int] = {}
    for _d, ki, pj in cand:
        if ki in used_k or pj in used_p:
            continue
        used_k.add(ki)
        used_p.add(pj)
        out[ki] = pj
    return out


def auto_match_lines(
    peak_px,
    wl_axis,
    known_nm,
    tol_nm: float = 8.0,
    *,
    max_resid_nm: float = 1.2,
    min_keep: int = 4,
    reject_nm: float = 2.5,
) -> list[tuple[float, float]]:
    """
    Pair detected peak pixel positions with known line wavelengths, robustly.

    The old version matched each known line to its nearest unused peak on the
    *current* (approximate) axis, in list order. Two failure modes bit us:
      • A known line with NO real peak in the spectrum (e.g. Cd 361.1 on a lamp
        that doesn't show it) grabbed an unrelated nearby peak within tol —
        producing a geometrically impossible pair (two lines 14 nm apart mapped
        to pixels 2 px apart) that then skewed the whole fit and made the axis
        "run apart" at the ends.
      • On a coarse start axis (off by ~tol at the ends), the tight tol dropped
        or mis-assigned the extreme lines, leaving the fit unconstrained there.

    This version:
      1. Global nearest-first assignment (order-independent) at a generous tol.
      2. One provisional low-degree refit to recentre the axis, then re-assign —
         so end lines that the coarse start axis pushed just out of tol come back.
      3. Robust trim: fit, drop the single worst-residual pair while the max
         residual exceeds max_resid_nm and > min_keep pairs remain. This is what
         removes the impossible/absent-line matches instead of trusting them.

    Returns [(pixel_position, known_wavelength_nm), ...] sorted by pixel,
    suitable for calibrate_from_lines(..., pixel_positions=...).
    """
    wl_axis = np.asarray(wl_axis, dtype=float)
    n = len(wl_axis)
    px_axis = np.arange(n, dtype=float)
    peak_px = np.asarray(peak_px, dtype=float)
    if peak_px.size == 0 or len(known_nm) == 0:
        return []

    def peaks_to_nm(axis_of_px):
        return [float(v) for v in axis_of_px(peak_px)]

    cur_axis = lambda p: np.interp(p, px_axis, wl_axis)

    m = _assign_nearest(peaks_to_nm(cur_axis), known_nm, tol_nm)

    # provisional recentre so the extreme lines get a fair second assignment
    if len(m) >= 3:
        xs = np.array([peak_px[pj] for pj in m.values()])
        ys = np.array([known_nm[ki] for ki in m.keys()])
        deg = min(3, len(m) - 1)
        coeffs = np.polyfit(xs, ys, deg)
        refit_axis = lambda p, c=coeffs: np.polyval(c, p)
        m = _assign_nearest(peaks_to_nm(refit_axis), known_nm, tol_nm)

    pairs = [(float(peak_px[pj]), float(known_nm[ki])) for ki, pj in m.items()]

    # robust trim: discard the worst geometric outlier until the fit is clean
    while len(pairs) > max(min_keep, 2):
        xs = np.array([p for p, _ in pairs])
        ys = np.array([w for _, w in pairs])
        deg = min(3, len(pairs) - 1)
        coeffs = np.polyfit(xs, ys, deg)
        resid = np.polyval(coeffs, xs) - ys
        worst = int(np.argmax(np.abs(resid)))
        if abs(resid[worst]) <= max_resid_nm:
            break
        pairs.pop(worst)

    # Final consistency guard: if a well-populated set still can't be fit to
    # within reject_nm, the lines almost certainly don't belong to this capture
    # (e.g. a lamp selected that isn't the one measured). Return nothing rather
    # than inject bogus rows — important for the multi-lamp 'add' workflow.
    if len(pairs) >= max(min_keep, 4):
        xs = np.array([p for p, _ in pairs])
        ys = np.array([w for _, w in pairs])
        coeffs = np.polyfit(xs, ys, min(3, len(pairs) - 1))
        if float(np.max(np.abs(np.polyval(coeffs, xs) - ys))) > reject_nm:
            return []

    pairs.sort(key=lambda t: t[0])
    return pairs


# ══════════════════════════════════════════════════════════════════════════
#  Spectral response correction
# ══════════════════════════════════════════════════════════════════════════

def _smooth_response(sens, reliable, window):
    """Light moving-average smoothing of the sensitivity over reliable pixels
    only (the response is physically smooth; this tames per-pixel noise that
    would otherwise become per-pixel gain). Unreliable pixels stay at 1.0.

    Edge-correct: normalised by the actual window coverage so the array ends are
    not pulled toward zero (a plain 'same' convolution zero-pads and would, e.g.,
    drop a true 0.3 edge sensitivity to 0.18 → spurious edge gain)."""
    w = int(window)
    if w < 3:
        return sens
    if w % 2 == 0:
        w += 1
    k = np.ones(w)
    num = np.convolve(sens, k, mode="same")
    den = np.convolve(np.ones_like(sens), k, mode="same")
    sm = num / den
    out = sens.copy()
    out[reliable] = sm[reliable]
    return np.clip(out, 1e-3, 1.0)


def compute_response_table(
    meas_wl,
    meas_inten,
    ref_wl,
    ref_inten,
    *,
    ref_floor_frac: float = 0.05,
    smooth_window: int = 9,
    meas_sigma=None,
    snr_k: float | None = None,
):
    """
    Compute a per-pixel relative-sensitivity table S(lambda) for the device that
    produced (meas_wl, meas_inten), using (ref_wl, ref_inten) as the target
    shape. Both must be broadband measurements of the SAME lamp (halogen,
    white LED, ...) — never a line/band lamp.

        S(lambda) = meas_norm(lambda) / ref_norm(lambda)

    The result is a FULL-axis table (one row per device pixel) with
    RESPONSE_TABLE semantics (sensitivity normalised so its reliable peak = 1.0;
    build_response_gain inverts it, gain = min(1/S, max_gain)). Pixels where the
    reference is unreliable — below ref_floor_frac of its peak, or outside its
    coverage — are set to sensitivity 1.0, i.e. NO correction there. This is the
    key robustness property: a banded source (big dark gaps, bare edges) yields a
    gain of 1.0 in those regions instead of amplifying the noise floor, and there
    are no wild interpolations across gaps.

    Returns (table Nx2 [[lambda, S]] ascending in lambda, info dict). info
    carries 'coverage_frac' and, when the source is too banded to be a good
    response reference, a 'warning' string for the caller to surface.
    """
    mw = np.asarray(meas_wl, dtype=float)
    mi = np.asarray(meas_inten, dtype=float)
    rw = np.asarray(ref_wl, dtype=float)
    ri = np.asarray(ref_inten, dtype=float)

    if mw.size < 2 or rw.size < 2:
        raise ValueError("Both spectra need at least two samples.")

    ref_on_m = np.interp(mw, rw, ri, left=np.nan, right=np.nan)

    mi_peak = np.nanmax(mi)
    rf_peak = np.nanmax(ref_on_m)
    if not (mi_peak > 0) or not (rf_peak > 0):
        raise ValueError("Measurement or reference has no positive signal.")

    mi_n = mi / mi_peak
    ref_n = ref_on_m / rf_peak

    reliable = np.isfinite(ref_n) & (ref_n > ref_floor_frac) & (mi_n >= 0.0)
    # Optional SNR-based validity (v2 extension): a pixel is only reliable if the
    # measured signal exceeds snr_k times its noise. With a per-pixel sigma this
    # replaces the crude "5 % of the reference peak" as the real criterion (pass
    # ref_floor_frac=0 to use the SNR criterion alone). Defaults: unchanged.
    if snr_k is not None and meas_sigma is not None:
        sig = np.asarray(meas_sigma, dtype=float)
        reliable &= np.isfinite(sig) & (mi > float(snr_k) * sig)
    if not np.any(reliable):
        raise ValueError(
            "No usable overlap between measurement and reference "
            "(check that they cover the same wavelength range and lamp)."
        )

    with np.errstate(divide="ignore", invalid="ignore"):
        S = mi_n / ref_n

    # Robust peak-normalisation over reliable pixels: use a high percentile, not
    # the bare max, so a single noise spike can't rescale the whole table (which
    # would inject a global >1 gain). Then clip to the RESPONSE_TABLE [.., 1.0].
    S_rel = S[reliable]
    norm = np.nanpercentile(S_rel, 98.0)
    if not (norm > 0):
        norm = np.nanmax(S_rel)
    S = S / norm

    # Full-axis sensitivity: measured where reliable, 1.0 (no correction) else.
    sens = np.ones_like(mw)
    sens[reliable] = np.clip(S[reliable], 1e-3, 1.0)
    sens = _smooth_response(sens, reliable, smooth_window)

    order = np.argsort(mw)
    table = np.column_stack([mw[order], sens[order]])

    coverage = float(np.count_nonzero(reliable)) / float(mw.size)
    rel_wl = mw[reliable]
    info = {
        "n_points": int(np.count_nonzero(reliable)),
        "wl_min_nm": float(rel_wl.min()),
        "wl_max_nm": float(rel_wl.max()),
        "ref_floor_frac": float(ref_floor_frac),
        "coverage_frac": coverage,
    }
    if coverage < 0.60:
        info["warning"] = (
            f"The reference covers only {coverage*100:.0f}% of the spectrum "
            f"({info['wl_min_nm']:.0f}–{info['wl_max_nm']:.0f} nm with signal). "
            f"This looks like a line/band source — a response correction needs a "
            f"SMOOTH BROADBAND lamp (halogen, white LED). The correction is set to "
            f"1.0 (no change) wherever the reference has no signal; the result "
            f"away from the bands is not meaningful."
        )
    return table, info


# ══════════════════════════════════════════════════════════════════════════
#  Spectral calibration v2 — maths (relative S, absolute K, validity)
# ══════════════════════════════════════════════════════════════════════════
#
#  All in count-rate space (calibration_capture.count_rate):
#      C(λ) = (raw_linearised − dark) / t_int                       [counts/s]
#  Relative:  S = C_cal / E_ref, normalised to peak 1;  correction  C / S
#  Absolute:  K = E_ref / C_cal  [E-unit per count/s];   measurement E = C · K
#  Valid range = device spec range ∩ reference range ∩ (C_cal > k·σ_dark)
#                ∩ not saturated ∩ (gain 1/S <= max_gain).
#  Outside: masked (NaN), never "gain 1".

TYPE_RELATIVE = "relative"
TYPE_ABSOLUTE = "absolute"
INPUT_OPTICS = ("CC", "bare", "sphere")
INPUT_UNKNOWN = "unknown"
UNIT_RELATIVE = "relative"
UNIT_COUNTS = "counts"
UNIT_COUNTS_PER_S = "counts/s"

APPLY_V2 = "v2"                  # C / S  or  C · K, masked outside valid range
APPLY_LEGACY_GAIN = "legacy_gain"   # old behaviour: counts · min(1/S, max_gain), gain 1 outside


def _nan_moving_average(x, mask, window: int):
    """Centred moving average over `mask` pixels only (others neither used nor
    changed), applied per contiguous run of masked pixels. Near the end of a run
    the window SHRINKS SYMMETRICALLY (half-width = distance to the run edge), so
    a sloped response is not biased at the edges of the valid range (a one-sided
    window would shift it by ~slope·w/4)."""
    w = int(window)
    out = np.array(x, dtype=float, copy=True)
    if w < 3:
        return out
    half = w // 2
    m = np.asarray(mask, bool) & np.isfinite(out)
    idx = np.flatnonzero(m)
    if idx.size == 0:
        return out
    breaks = np.flatnonzero(np.diff(idx) > 1)
    starts = np.r_[idx[0], idx[breaks + 1]]
    ends = np.r_[idx[breaks], idx[-1]]
    src = out.copy()
    for a, b in zip(starts, ends):
        seg = src[a:b + 1]
        cs = np.r_[0.0, np.cumsum(seg)]
        n = seg.size
        i = np.arange(n)
        h = np.minimum(np.minimum(i, n - 1 - i), half)
        out[a:b + 1] = (cs[i + h + 1] - cs[i - h]) / (2 * h + 1)
    return out


def _range_mask(wl, lo, hi):
    wl = np.asarray(wl, float)
    m = np.ones(wl.size, bool)
    if lo is not None:
        m &= wl >= float(lo)
    if hi is not None:
        m &= wl <= float(hi)
    return m


def compute_calibration(
    wl,
    rate,
    sigma_dark,
    e_ref,
    *,
    kind: str = TYPE_RELATIVE,
    saturated=None,
    spec_range_nm: tuple = (None, None),
    snr_k: float = 10.0,
    max_gain: float = 50.0,
    smooth_px: int = 0,
    pixel_ok=None,
) -> dict:
    """S (relative) or K (absolute) from a calibration count rate.

    wl, rate, sigma_dark : pixel axis, C_cal [counts/s], single-frame dark noise
                           [counts/s] (calibration_capture.CountRate fields).
    e_ref                : reference irradiance ON THE PIXEL AXIS, NaN outside the
                           source's valid range (ReferenceSource.on_axis, PCHIP).
    pixel_ok             : optional bool mask of the per-pixel checks (stability,
                           integration-time cross check, see pixel_check); False
                           pixels are masked and so limit the valid segments.
    Returns a dict with per-pixel arrays (NaN where masked):
        S (peak 1), K (absolute only, else None), valid (bool), masks (dict of the
        individual criteria), valid_range_nm, gain_span, n_valid.
    """
    wl = np.asarray(wl, float)
    C = np.asarray(rate, float)
    E = np.asarray(e_ref, float)
    sd = np.asarray(sigma_dark, float)
    if not (wl.shape == C.shape == E.shape == sd.shape):
        raise ValueError("wl, rate, sigma_dark and e_ref must have the same shape.")
    if kind not in (TYPE_RELATIVE, TYPE_ABSOLUTE):
        raise ValueError(f"unknown calibration type '{kind}'")
    sat = (np.zeros(wl.size, bool) if saturated is None
           else np.asarray(saturated, bool))

    m_dev = _range_mask(wl, *spec_range_nm)
    m_ref = np.isfinite(E) & (E > 0)
    m_snr = np.isfinite(C) & (C > float(snr_k) * np.maximum(sd, 0.0)) & (C > 0)
    m_sat = ~sat
    m_chk = (np.ones(wl.size, bool) if pixel_ok is None
             else np.asarray(pixel_ok, bool))
    valid0 = m_dev & m_ref & m_snr & m_sat & m_chk
    if not np.any(valid0):
        raise ValueError(
            "No valid pixels: the calibration signal is below the SNR limit, "
            "saturated, or outside the device / reference range everywhere.")

    with np.errstate(divide="ignore", invalid="ignore"):
        s_raw = np.where(valid0, C / E, np.nan)          # counts/s per E-unit
    s_raw = _nan_moving_average(s_raw, valid0, smooth_px)
    peak = float(np.nanmax(s_raw[valid0]))
    S = s_raw / peak
    m_gain = np.zeros(wl.size, bool)
    m_gain[valid0] = S[valid0] >= 1.0 / float(max_gain)
    valid = valid0 & m_gain
    S = np.where(valid, S, np.nan)

    K = None
    if kind == TYPE_ABSOLUTE:
        with np.errstate(divide="ignore", invalid="ignore"):
            K = np.where(valid, 1.0 / s_raw, np.nan)       # E-unit per count/s

    vw = wl[valid]
    gain_vals = (K[valid] if K is not None else 1.0 / S[valid])
    return {
        "wl": wl, "C": C, "E": E, "S": S, "K": K, "valid": valid,
        "masks": {"device": m_dev, "reference": m_ref, "snr": m_snr,
                  "saturation": m_sat, "checks": m_chk, "gain": m_gain | ~valid0,
                  # pixels where a calibration is expected at all
                  # (device spec range ∩ reference range): coverage basis
                  "base": m_dev & m_ref},
        "valid_range_nm": [float(vw.min()), float(vw.max())],
        "gain_span": float(np.max(gain_vals) / np.min(gain_vals)),
        "n_valid": int(np.count_nonzero(valid)),
        "type": kind,
    }


def compare_rates(c_ref, c_test, mask, *, min_frac: float = 0.1,
                  smooth_px: int = 21) -> dict:
    """Deviation of two count-rate spectra (stability / t_int cross check).

    Uses pixels in `mask` with c_ref > min_frac·max. The ratio is smoothed over
    `smooth_px` pixels first: real instabilities (lamp drift, t_int
    non-proportionality) are spectrally smooth, while per-pixel shot noise is
    not and would otherwise dominate a percentile. Returns percentages."""
    a = np.asarray(c_ref, float)
    b = np.asarray(c_test, float)
    m = np.asarray(mask, bool) & np.isfinite(a) & np.isfinite(b)
    if not np.any(m):
        return {"p95_pct": float("nan"), "median_pct": float("nan"),
                "max_pct": float("nan"), "mean_ratio": float("nan"), "n": 0}
    m &= a > min_frac * float(np.max(a[m]))
    with np.errstate(divide="ignore", invalid="ignore"):
        r = np.where(m, b / a, np.nan)
    rs = _nan_moving_average(r, m, smooth_px)
    d = np.abs(rs[m] - 1.0) * 100.0
    return {"p95_pct": float(np.percentile(d, 95)),
            "median_pct": float(np.median(d)),
            "max_pct": float(np.max(d)),
            "mean_ratio": float(np.mean(rs[m])),
            "n": int(np.count_nonzero(m))}


def pixel_check(c_ref, c_test, sigma_ref, sigma_test, mask, *,
                threshold_pct: float, k: float = 4.0, expected_ratio=1.0) -> dict:
    """Per-pixel comparison of two count-rate spectra with a NOISE-DEPENDENT
    tolerance (stability: repeat at t; cross check: capture at ~t/2).

    A tested pixel passes when
        |c_test − r·c_ref| <= k·sqrt(σ_ref² + σ_test²) + threshold_pct/100·|c_ref|
    with σ the per-pixel standard deviation of the mean rate (dark noise and
    shot noise of the actual captures, calibration_capture.count_rate) and r the
    expected ratio (1, or a lamp-drift correction). The relative term is the
    systematic deviation the check accepts as a whole; the noise term keeps
    weak pixels from failing on noise alone. Non-positive or non-finite rates
    fail. Untested pixels (mask False) are reported as ok.
    Returns {"ok", "tested", "fail", "n_tested", "n_fail", "fail_frac",
    "ratio" (c_test/c_ref, NaN where not tested), "z"}."""
    a = np.asarray(c_ref, float)
    b = np.asarray(c_test, float)
    sa = np.asarray(sigma_ref, float)
    sb = np.asarray(sigma_test, float)
    m = np.asarray(mask, bool)
    r = np.broadcast_to(np.asarray(expected_ratio, float), a.shape)
    with np.errstate(invalid="ignore", divide="ignore"):
        sig = np.sqrt(np.maximum(sa, 0) ** 2 + np.maximum(sb, 0) ** 2)
        tol = float(k) * sig + float(threshold_pct) / 100.0 * np.abs(a)
        dev = b - r * a
        bad = (~np.isfinite(a) | ~np.isfinite(b) | (b <= 0) | (a <= 0)
               | ~(np.abs(dev) <= tol))
        z = np.where(sig > 0, dev / sig, np.nan)
        ratio = np.where(m & np.isfinite(a) & (a != 0), b / a, np.nan)
    fail = m & bad
    n_t = int(np.count_nonzero(m))
    n_f = int(np.count_nonzero(fail))
    return {"ok": ~fail, "tested": m, "fail": fail, "n_tested": n_t, "n_fail": n_f,
            "fail_frac": (n_f / n_t) if n_t else 0.0, "ratio": ratio, "z": z,
            "k": float(k), "threshold_pct": float(threshold_pct)}


CAUSE_DARK = "dark_drift"            # constant count offset
CAUSE_TIME = "time_offset"           # uniform factor between t and t/2
CAUSE_LAMP = "lamp_drift"            # uniform factor between two captures at t
CAUSE_NONLINEAR = "nonlinearity"     # signal-dependent deviation


def check_causes(c_ref, c_test, sigma_ref, sigma_test, mask, *, t_ref_us: int,
                 t_test_us: int, full_scale: float | None = None,
                 nonlinear: bool | None = None, min_effect_pct: float = 0.3,
                 sig_k: float = 3.0, lamp_scale: float = 1.0) -> dict:
    """Cause model of a stability or cross check over all tested pixels.

    In counts of the TEST capture (x = counts expected from the reference
    rate, y = measured counts):
        y = a + s·x + c·x²/FS                 (c = 0 for a repeat at the same t)
      a  constant count offset  → dark drift / dark mismatch            [counts]
      s  uniform factor         → time offset (t/2 check: effective exposure
                                   t+δ instead of t) or lamp drift (repeat at t)
      c  signal-dependent term  → detector nonlinearity
    Weighted least squares (weights from the per-pixel noise), one robust
    re-fit without > 5σ outliers. A term is reported as a cause when it is
    significant (|value| > sig_k·standard error) AND its effect on the ratio
    y/x reaches min_effect_pct. lamp_scale: expected lamp factor (from the
    stability check) applied to the reference before a cross-check fit.

    Returns {"kind", "causes": [{cause, value, unit, effect_pct, se,
    significant, message}], "dominant": cause | None, "message", "fit"}."""
    a = np.asarray(c_ref, float)
    b = np.asarray(c_test, float)
    sra = np.asarray(sigma_ref, float)
    srb = np.asarray(sigma_test, float)
    m = (np.asarray(mask, bool) & np.isfinite(a) & np.isfinite(b)
         & np.isfinite(sra) & np.isfinite(srb))
    t_test = max(int(t_test_us), 1) / 1e6
    t_ref = max(int(t_ref_us), 1) / 1e6
    same_t = abs(t_test - t_ref) <= 0.005 * t_ref
    if nonlinear is None:
        nonlinear = not same_t
    out = {"kind": "stability" if same_t else "crosscheck", "causes": [],
           "dominant": None, "message": "", "fit": None}
    if np.count_nonzero(m) < 8:
        out["message"] = "too few pixels for the cause analysis"
        return out
    x = a[m] * t_test * float(lamp_scale)
    y = b[m] * t_test
    fs = float(full_scale) if full_scale and full_scale > 0 else float(np.max(np.abs(y)))
    var = (srb[m] * t_test) ** 2 + (sra[m] * t_test * float(lamp_scale)) ** 2
    var = np.maximum(var, 1e-12)
    cols = [np.ones_like(x), x] + ([x * x / fs] if nonlinear else [])
    X = np.column_stack(cols)
    keep = np.ones(x.size, bool)
    coef = cov = None
    chi2 = 1.0
    for _ in range(3):
        w = 1.0 / var[keep]
        Xk = X[keep]
        try:
            A = Xk.T @ (Xk * w[:, None])
            coef = np.linalg.solve(A, Xk.T @ (y[keep] * w))
            cov0 = np.linalg.inv(A)
        except np.linalg.LinAlgError:
            out["message"] = "cause analysis: fit singular"
            return out
        resid = y - X @ coef
        dof = max(int(np.count_nonzero(keep)) - X.shape[1], 1)
        chi2 = float(np.sum(resid[keep] ** 2 / var[keep]) / dof)
        cov = cov0 * max(chi2, 1.0)
        new_keep = np.abs(resid) <= 5.0 * np.sqrt(var * max(chi2, 1.0))
        if np.array_equal(new_keep, keep) or np.count_nonzero(new_keep) < 8:
            break
        keep = new_keep
    se = np.sqrt(np.maximum(np.diag(cov), 0.0))
    xk = x[keep] if np.any(keep) else x
    x_ref = float(np.median(xk))
    x_lo, x_hi = float(np.percentile(xk, 5)), float(np.percentile(xk, 95))
    a0, s0 = float(coef[0]), float(coef[1])
    c0 = float(coef[2]) if nonlinear else 0.0
    causes = []

    def add(cause, value, unit, effect, se_effect, msg, **extra):
        sig = (abs(effect) > sig_k * se_effect) and abs(effect) >= float(min_effect_pct)
        d = {"cause": cause, "value": float(value), "unit": unit,
             "effect_pct": float(effect), "se_effect_pct": float(se_effect),
             "significant": bool(sig), "message": msg}
        d.update(extra)
        causes.append(d)

    off_eff = 100.0 * a0 / x_ref if x_ref else 0.0
    add(CAUSE_DARK, a0, "counts", off_eff,
        100.0 * se[0] / abs(x_ref) if x_ref else float("inf"),
        f"dark drift: constant offset {a0:+.1f} counts "
        f"({off_eff:+.2f} % at the median signal of {x_ref:.0f} counts)",
        offset_counts=a0)
    scale_eff = 100.0 * (s0 - 1.0)
    if same_t:
        add(CAUSE_LAMP, s0 - 1.0, "fraction", scale_eff, 100.0 * se[1],
            f"lamp drift: uniform change {scale_eff:+.2f} % between the captures")
    else:
        th, tf = t_test * 1e6, t_ref * 1e6
        den = s0 * th - tf
        delta = (tf * th * (1.0 - s0) / den) if abs(den) > 1e-12 else float("nan")
        add(CAUSE_TIME, s0 - 1.0, "fraction", scale_eff, 100.0 * se[1],
            f"time offset: uniform factor {scale_eff:+.2f} % between t and t/2 "
            f"(effective exposure offset ≈ {delta:+.0f} µs)",
            time_offset_us=float(delta) if np.isfinite(delta) else None)
    if nonlinear:
        span = (x_hi - x_lo) / fs
        nl_eff = 100.0 * c0 * span
        add(CAUSE_NONLINEAR, c0, "per full scale", nl_eff, 100.0 * se[2] * span,
            f"nonlinearity: ratio changes by {nl_eff:+.2f} % between "
            f"{100 * x_lo / fs:.0f} % and {100 * x_hi / fs:.0f} % FS "
            f"(detector response ≈ {-100.0 * c0 / max(t_ref / t_test - 1.0, 1e-9):+.2f} % "
            f"per 100 % FS)",
            response_pct_per_fs=float(-100.0 * c0 / max(t_ref / t_test - 1.0, 1e-9)))
    sig = [c for c in causes if c["significant"]]
    if sig:
        sig.sort(key=lambda c: -abs(c["effect_pct"]))
        out["dominant"] = sig[0]["cause"]
        out["message"] = "; ".join(c["message"] for c in sig)
    else:
        out["message"] = "no significant systematic deviation"
    out["causes"] = causes
    out["fit"] = {"offset_counts": a0, "scale": s0, "nonlinear_c": c0,
                  "se": [float(v) for v in se], "chi2_red": chi2,
                  "n_pixels": int(np.count_nonzero(keep)), "x_ref_counts": x_ref,
                  "x_range_counts": [x_lo, x_hi], "full_scale": fs,
                  "lamp_scale": float(lamp_scale)}
    return out


def derive_relative_from_ratio(wl, s_known, c_new, c_known, *, valid_known=None,
                               sigma_new=None, sigma_known=None, snr_k: float = 10.0,
                               max_gain: float = 50.0, smooth_px: int = 0) -> dict:
    """Derive S for a new optics configuration from a known one:
         S_new = S_known · C_new / C_known
    with both count rates measured on the SAME diffuse, extended source.
    Returns the same dict layout as compute_calibration (type relative)."""
    wl = np.asarray(wl, float)
    sk = np.asarray(s_known, float)
    cn = np.asarray(c_new, float)
    ck = np.asarray(c_known, float)
    m = np.isfinite(sk) & (sk > 0) & np.isfinite(cn) & np.isfinite(ck) & (cn > 0) & (ck > 0)
    if valid_known is not None:
        m &= np.asarray(valid_known, bool)
    if sigma_new is not None:
        m &= cn > snr_k * np.asarray(sigma_new, float)
    if sigma_known is not None:
        m &= ck > snr_k * np.asarray(sigma_known, float)
    if not np.any(m):
        raise ValueError("No overlap with sufficient signal for the derivation.")
    with np.errstate(divide="ignore", invalid="ignore"):
        s = np.where(m, sk * cn / ck, np.nan)
    s = _nan_moving_average(s, m, smooth_px)
    S = s / float(np.nanmax(s[m]))
    g = np.zeros(wl.size, bool)
    g[m] = S[m] >= 1.0 / float(max_gain)
    valid = m & g
    S = np.where(valid, S, np.nan)
    vw = wl[valid]
    base = np.isfinite(sk) & (sk > 0)
    if valid_known is not None:
        base &= np.asarray(valid_known, bool)
    return {"wl": wl, "S": S, "K": None, "valid": valid,
            "masks": {"overlap": m, "gain": g | ~m, "base": base},
            "valid_range_nm": [float(vw.min()), float(vw.max())],
            "gain_span": float(1.0 / np.nanmin(S[valid])),
            "n_valid": int(np.count_nonzero(valid)), "type": TYPE_RELATIVE}


# ── Plausibility of a calibration result (broadband check) ───────────────
#
# A response calibration is only meaningful with a smooth, broadband source on
# BOTH sides (lamp under test and reference). Measuring a line source (Ne, Hg …)
# against a continuous reference passes the per-pixel SNR mask only at the line
# peaks: S is then defined on a few isolated islands, jumps within each line,
# and the outer "valid range" spans large masked gaps. These checks detect that
# before a profile is stored.

@dataclass(frozen=True)
class PlausibilityLimits:
    min_coverage: float = 0.5        # valid / (device ∩ reference) pixels
    max_gain_span: float = 200.0     # max/min gain: reported as a HINT only
    max_roughness: float = 0.05      # p95 |S / S_smoothed − 1|
    max_isolated_frac: float = 0.2   # share of valid pixels in short islands
    min_segment_nm: float = 5.0      # a valid run shorter than this is an island
    max_gap_nm: float = 2.0          # masked gaps up to this width are bridged
    min_largest_segment_frac: float = 0.5   # widest segment / base width
    roughness_px: int = 21           # smoothing window of the roughness metric

    @classmethod
    def from_config(cls) -> "PlausibilityLimits":
        try:
            from app_config import Config as C
        except Exception:                                   # noqa: BLE001
            return cls()
        d = cls()

        def num(key, default, typ=float):
            try:                         # null / garbage in the config → default
                v = typ(C.get(key, default))
                return v if np.isfinite(v) and v >= 0 else default
            except (TypeError, ValueError):
                return default
        return cls(
            min_coverage=num("calibration_min_coverage", d.min_coverage),
            max_gain_span=num("calibration_max_gain_span", d.max_gain_span),
            max_roughness=num("calibration_max_roughness", d.max_roughness),
            max_isolated_frac=num("calibration_max_isolated_frac", d.max_isolated_frac),
            min_segment_nm=num("calibration_min_segment_nm", d.min_segment_nm),
            max_gap_nm=num("calibration_max_gap_nm", d.max_gap_nm),
            min_largest_segment_frac=num("calibration_min_largest_segment_frac",
                                         d.min_largest_segment_frac),
            roughness_px=max(3, num("calibration_roughness_px", d.roughness_px, int)),
        )


def _runs(mask) -> list[tuple[int, int]]:
    """Inclusive index runs of True values."""
    idx = np.flatnonzero(np.asarray(mask, bool))
    if idx.size == 0:
        return []
    br = np.flatnonzero(np.diff(idx) > 1)
    starts = np.r_[idx[0], idx[br + 1]]
    ends = np.r_[idx[br], idx[-1]]
    return [(int(a), int(b)) for a, b in zip(starts, ends)]


def valid_segments(wl, valid, max_gap_nm: float = 0.0) -> list[list[float]]:
    """Contiguous valid wavelength segments [[lo, hi], …].

    Masked gaps whose width (distance between the neighbouring valid pixels,
    minus one pixel pitch) is at most `max_gap_nm` are bridged, i.e. reported as
    part of one segment. Bridging only affects this report — the masked pixels
    stay masked and are never applied."""
    wl = np.asarray(wl, float)
    runs = _runs(valid)
    if not runs:
        return []
    pitch = float(np.median(np.abs(np.diff(wl)))) if wl.size > 1 else 0.0
    segs = [[runs[0][0], runs[0][1]]]
    for a, b in runs[1:]:
        gap = float(wl[a] - wl[segs[-1][1]]) - pitch
        if gap <= float(max_gap_nm) + 1e-9:
            segs[-1][1] = b
        else:
            segs.append([a, b])
    return [[float(wl[a]), float(wl[b])] for a, b in segs]


def format_segments(segments, max_items: int = 4) -> str:
    """'585–590, 602–610 nm (+n more)' — for status lines and the protocol."""
    if not segments:
        return "none"
    parts = [f"{lo:.0f}–{hi:.0f}" for lo, hi in segments[:max_items]]
    more = len(segments) - max_items
    return ", ".join(parts) + " nm" + (f" (+{more} more)" if more > 0 else "")


def broadband_check(result: dict, limits: PlausibilityLimits | None = None) -> dict:
    """Plausibility of a compute_calibration / derive_relative_from_ratio result.

    Metrics (all reported, each with its limit):
      coverage            valid pixels / pixels where a calibration is expected
                          (masks['base']: device spec ∩ reference range)
      gain_span           max/min gain over the valid pixels (HINT only)
      roughness           p95 of |S / S_smoothed − 1| (line sources: S jumps
                          within every line)
      isolated_frac       share of valid pixels in runs < min_segment_nm
      largest_segment_frac widest bridged segment / width of the base range
    Returns {"pass", "failures": [str], "hints": [str], "metrics", "limits",
    "segments_nm"}."""
    lim = limits or PlausibilityLimits()
    wl = np.asarray(result["wl"], float)
    valid = np.asarray(result["valid"], bool)
    S = np.asarray(result["S"], float)
    base = np.asarray((result.get("masks") or {}).get("base", np.ones(wl.size, bool)), bool)
    n_base = int(np.count_nonzero(base))
    n_valid = int(np.count_nonzero(valid))
    coverage = n_valid / n_base if n_base else 0.0

    S_sm = _nan_moving_average(np.where(valid, S, np.nan), valid, lim.roughness_px)
    with np.errstate(divide="ignore", invalid="ignore"):
        dev = np.abs(S[valid] / S_sm[valid] - 1.0)
    dev = dev[np.isfinite(dev)]
    roughness = float(np.percentile(dev, 95)) if dev.size else float("inf")

    isolated = 0
    for a, b in _runs(valid):
        if float(wl[b] - wl[a]) < lim.min_segment_nm:
            isolated += b - a + 1
    isolated_frac = isolated / n_valid if n_valid else 1.0

    segs = valid_segments(wl, valid, lim.max_gap_nm)
    bw = wl[base]
    base_width = float(bw.max() - bw.min()) if bw.size > 1 else 0.0
    widest = max((hi - lo for lo, hi in segs), default=0.0)
    largest_frac = widest / base_width if base_width > 0 else 0.0
    gain_span = float(result.get("gain_span", float("nan")))

    failures = []
    hints = []
    if coverage < lim.min_coverage:
        failures.append(f"coverage {coverage * 100:.0f} % < {lim.min_coverage * 100:.0f} % "
                        f"of the expected range")
    # The gain span is a property of detector + source (a halogen lamp on a
    # Si detector easily spans 50-100x between 380 and 950 nm); it does not
    # identify a line source. Reported as a hint; line sources are caught by
    # coverage, roughness and isolated peaks.
    if not np.isfinite(gain_span) or gain_span > lim.max_gain_span:
        hints.append(f"gain span {gain_span:.1f}× > {lim.max_gain_span:.1f}× "
                     f"(hint only: weak signal at the range ends)")
    if roughness > lim.max_roughness:
        failures.append(f"roughness {roughness * 100:.1f} % > {lim.max_roughness * 100:.1f} % "
                        f"(not a smooth broadband response)")
    if isolated_frac > lim.max_isolated_frac:
        failures.append(f"{isolated_frac * 100:.0f} % of the valid pixels lie in isolated "
                        f"peaks narrower than {lim.min_segment_nm:.0f} nm "
                        f"(limit {lim.max_isolated_frac * 100:.0f} %)")
    if largest_frac < lim.min_largest_segment_frac:
        failures.append(f"widest contiguous segment covers {largest_frac * 100:.0f} % of the "
                        f"expected range (limit {lim.min_largest_segment_frac * 100:.0f} %)")
    return {
        "pass": not failures,
        "failures": failures,
        "hints": hints,
        "metrics": {"coverage": coverage, "gain_span": gain_span,
                    "roughness": roughness, "isolated_frac": isolated_frac,
                    "largest_segment_frac": largest_frac,
                    "n_segments": len(segs), "n_valid": n_valid, "n_base": n_base},
        "limits": asdict(lim),
        "segments_nm": segs,
    }


def relative_from_absolute(K, valid):
    """The relative profile implied by an absolute one: S ∝ 1/K, peak 1."""
    K = np.asarray(K, float)
    valid = np.asarray(valid, bool) & np.isfinite(K) & (K > 0)
    with np.errstate(divide="ignore", invalid="ignore"):
        s = np.where(valid, 1.0 / K, np.nan)
    return s / float(np.nanmax(s[valid])), valid


def result_table(result: dict) -> list:
    """[[λ, value|None], …] over the valid range (inclusive); value = S for
    relative, K for absolute; masked pixels inside the range are None."""
    wl = result["wl"]
    vals = result["K"] if result["type"] == TYPE_ABSOLUTE else result["S"]
    lo, hi = result["valid_range_nm"]
    sel = (wl >= lo) & (wl <= hi)
    return [[float(x), (float(v) if np.isfinite(v) else None)]
            for x, v in zip(wl[sel], vals[sel])]


# ══════════════════════════════════════════════════════════════════════════
#  Profile store v2 (versioned, bound to device serial + wavelength fingerprint)
# ══════════════════════════════════════════════════════════════════════════

DEFAULT_REGISTRY_FILE = "calibration_profiles.json"
SCHEMA_VERSION = 2

SEL_NONE = "None"
SEL_DEVICE_DEFAULT = "Device default"

STATE_OK = "ok"
STATE_WL_CHANGED = "wl_changed"
STATE_NL_CHANGED = "nl_changed"     # detector linearisation switched since calibration
# states that are applied only after an explicit confirmation
CONFIRM_STATES = (STATE_WL_CHANGED, STATE_NL_CHANGED)
STATE_OTHER_DEVICE = "other_device"
STATE_UNBOUND = "unbound"           # legacy / no serial known: not bound to a unit


def _registry_path() -> str:
    import data_paths
    try:
        from app_config import Config
        return data_paths.resolve(Config.get("calibration_profiles_file",
                                             DEFAULT_REGISTRY_FILE))
    except Exception:
        return data_paths.resolve(DEFAULT_REGISTRY_FILE)


def _now_iso() -> str:
    from datetime import datetime
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _legacy_id(device: str, name: str) -> str:
    import hashlib
    return "legacy-" + hashlib.sha1(f"{device}\x00{name}".encode("utf-8")).hexdigest()[:10]


def migrate_legacy_entry(device: str, name: str, entry: dict) -> dict:
    """One schema-1 entry {table, created, wl_min_nm, …} → schema-2 profile.

    type relative, optics "unbekannt"/unknown, no serial and no fingerprint (both
    unknown for old data), and apply mode legacy_gain so the correction it
    produces is bit-identical to before (gain = min(1/S, 10), 1 outside)."""
    table = entry.get("table") or []
    wls = [r[0] for r in table if r and len(r) >= 2]
    lo = entry.get("wl_min_nm", min(wls) if wls else None)
    hi = entry.get("wl_max_nm", max(wls) if wls else None)
    extra = {k: v for k, v in entry.items()
             if k not in ("table", "created", "wl_min_nm", "wl_max_nm")}
    return {
        "schema": SCHEMA_VERSION,
        "id": _legacy_id(device, name),
        "type": TYPE_RELATIVE,
        "device": {"backend": device, "serial": None},
        "optics": {"name": name, "input": INPUT_UNKNOWN, "notes": ""},
        "reference": {"name": "", "serial": "", "unit": ""},
        "wl_fingerprint": None,
        "valid_range_nm": [lo, hi],
        "table": table,
        "unit": UNIT_RELATIVE,
        "acquisition": {},
        "checks": {},
        "created": entry.get("created", ""),
        "version": 1,
        "apply": APPLY_LEGACY_GAIN,
        "legacy_name": name,
        "migrated_from": 1,
        "legacy_meta": extra,
    }


def migrate_registry(raw) -> dict:
    """Any stored registry (schema 1 dict-of-dicts, or schema 2) → schema 2."""
    if not raw:
        return {"schema": SCHEMA_VERSION, "profiles": []}
    if isinstance(raw, dict) and raw.get("schema") == SCHEMA_VERSION:
        raw.setdefault("profiles", [])
        return raw
    profiles = []
    for device, entries in (raw or {}).items():
        if not isinstance(entries, dict):
            continue
        for name, entry in entries.items():
            if isinstance(entry, dict) and "table" in entry:
                profiles.append(migrate_legacy_entry(device, name, entry))
    return {"schema": SCHEMA_VERSION, "profiles": profiles}


def load_registry() -> dict:
    path = _registry_path()
    if not os.path.exists(path):
        return {"schema": SCHEMA_VERSION, "profiles": []}
    try:
        with open(path, "r", encoding="utf-8") as f:
            raw = json.load(f) or {}
    except Exception as e:
        print(f"[calib] failed to read {path}: {e}")
        return {"schema": SCHEMA_VERSION, "profiles": []}
    return migrate_registry(raw)


def save_registry(reg: dict) -> None:
    """Write schema 2. The first time a schema-1 file is overwritten, a backup
    (<name>.v1.bak.json) of the original is kept next to it."""
    path = _registry_path()
    try:
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8") as f:
                try:
                    old = json.load(f)
                except Exception:
                    old = None
            if isinstance(old, dict) and old and old.get("schema") != SCHEMA_VERSION:
                bak = os.path.splitext(path)[0] + ".v1.bak.json"   # data dir
                if not os.path.exists(bak):
                    with open(bak, "w", encoding="utf-8") as fb:
                        json.dump(old, fb, indent=2)
        reg = dict(reg)
        reg["schema"] = SCHEMA_VERSION
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(_json_clean(reg), f, indent=1, allow_nan=False)
        os.replace(tmp, path)
    except Exception as e:
        # Never pretend a profile was stored: the caller (wizard / web) must
        # report this to the user.
        print(f"[calib] failed to write {path}: {e}")
        raise OSError(f"Could not write the calibration profiles to {path}: {e}") from e


def _json_clean(obj):
    """Deep copy with NaN/±inf → None and numpy scalars → Python (strict JSON)."""
    if isinstance(obj, dict):
        return {str(k): _json_clean(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_json_clean(v) for v in obj]
    if isinstance(obj, (np.floating, float)):
        v = float(obj)
        return v if np.isfinite(v) else None
    if isinstance(obj, np.integer):
        return int(obj)
    if isinstance(obj, np.bool_):
        return bool(obj)
    if isinstance(obj, np.ndarray):
        return _json_clean(obj.tolist())
    return obj


def all_profiles() -> list[dict]:
    return list(load_registry().get("profiles", []))


def get_profile_by_id(pid: str) -> dict | None:
    for p in all_profiles():
        if p.get("id") == pid:
            return p
    return None


def series_key(p: dict) -> tuple:
    d = p.get("device") or {}
    return (d.get("backend") or "", d.get("serial") or "",
            (p.get("optics") or {}).get("name") or "", p.get("type") or "")


def device_nl_active(backend) -> bool | None:
    """The backend's current detector-linearisation state (None = the device
    has none / unknown → not checked)."""
    if backend is None:
        return None
    try:
        v = backend.nl_correction_active
    except Exception:
        return None
    return v if isinstance(v, bool) else None


def _onoff(v: bool) -> str:
    return "ON" if v else "OFF"


def profile_status(p: dict, backend: str, serial: str | None,
                   fingerprint: str | None, nl_active: bool | None = None,
                   linearity: dict | None = None) -> dict:
    """See _profile_status. `linearity`: the unit's current linearity state
    (calibration_linearity.runtime_for info); when the profile was made with a
    different linearity correction a 'linearity_note' hint is added (the
    profile is still applied — no confirmation needed)."""
    st = _profile_status(p, backend, serial, fingerprint, nl_active)
    if linearity is not None and st["state"] != STATE_OTHER_DEVICE:
        try:
            import calibration_linearity as cl
            note = cl.linearity_mismatch((p.get("acquisition") or {}).get("linearity"),
                                         linearity)
        except Exception:                                   # noqa: BLE001
            note = None
        if note:
            st["linearity_note"] = note
    return st


def _profile_status(p: dict, backend: str, serial: str | None,
                    fingerprint: str | None, nl_active: bool | None = None) -> dict:
    """Is profile p usable on this device as it is calibrated right now?

    Besides device/serial binding, two conditions of the calibration must still
    hold, otherwise the profile is applied only after confirmation:
      * wavelength calibration unchanged (fingerprint)        → wl_changed
      * detector linearisation unchanged (acquisition.nl_correction vs the
        backend's nl_correction_active; only checked when both are known) →
        nl_changed. S/K are fixed per-pixel factors measured at ONE signal
        level; the linearisation is what makes them valid at every other level,
        so switching it after calibrating makes the results level-dependent."""
    d = p.get("device") or {}
    if d.get("backend") != backend:
        return {"state": STATE_OTHER_DEVICE,
                "message": f"Profile belongs to {d.get('backend')}."}
    ps = d.get("serial") or None
    if ps is not None and (serial or None) != ps:
        return {"state": STATE_OTHER_DEVICE,
                "message": f"Profile is bound to serial {ps}; connected unit is "
                           f"{serial or 'unknown'}. Use 'Copy profile to this device'."}
    fp = p.get("wl_fingerprint")
    wl_changed = bool(fp and fingerprint and fp != fingerprint)
    pn = (p.get("acquisition") or {}).get("nl_correction")
    nl_changed = isinstance(pn, bool) and isinstance(nl_active, bool) and pn != nl_active
    if wl_changed or nl_changed:
        parts = []
        if wl_changed:
            parts.append("Wavelength calibration changed")
        if nl_changed:
            parts.append(f"Detector linearisation was {_onoff(pn)} when calibrated, "
                         f"is {_onoff(nl_active)} now")
        return {"state": STATE_WL_CHANGED if wl_changed else STATE_NL_CHANGED,
                "wl_changed": wl_changed, "nl_changed": nl_changed,
                "message": "; ".join(parts) + " – recalibrate. "
                           "(Applying it anyway requires confirmation.)"}
    if ps is None or not fp:
        return {"state": STATE_UNBOUND,
                "message": "Legacy/unbound profile (no serial or wavelength "
                           "fingerprint stored)."}
    return {"state": STATE_OK, "message": ""}


def profiles_for_device(backend: str, serial: str | None,
                        fingerprint: str | None = None, *,
                        include_old: bool = False,
                        nl_active: bool | None = None) -> list[dict]:
    """Profiles selectable on this unit: same backend AND (same serial, or
    unbound/legacy). Profiles of another unit of the same type are NOT listed.
    Default: one entry per series (backend, serial, optics, type) — the newest
    VALID version (fingerprint + linearisation ok) if any, else the newest.
    include_old lists every version. Sorted by optics name, type, version
    (newest first)."""
    mine = []
    for p in all_profiles():
        d = p.get("device") or {}
        if d.get("backend") != backend:
            continue
        ps = d.get("serial") or None
        if ps is not None and ps != (serial or None):
            continue
        mine.append(p)
    if not include_old:
        best: dict[tuple, dict] = {}
        for p in mine:
            k = series_key(p)
            ok = profile_status(p, backend, serial, fingerprint,
                                nl_active)["state"] not in CONFIRM_STATES
            cur = best.get(k)
            if cur is None:
                best[k] = p
                continue
            cur_ok = profile_status(cur, backend, serial, fingerprint,
                                    nl_active)["state"] not in CONFIRM_STATES
            if (ok, int(p.get("version", 0))) > (cur_ok, int(cur.get("version", 0))):
                best[k] = p
        mine = list(best.values())
    mine.sort(key=lambda p: ((p.get("optics") or {}).get("name", "").lower(),
                             p.get("type", ""), -int(p.get("version", 0))))
    return mine


def profiles_other_units(backend: str, serial: str | None) -> list[dict]:
    """Profiles of the same backend bound to ANOTHER serial — the candidates for
    'Copy profile to this device'. All versions, newest first."""
    out = [p for p in all_profiles()
           if (p.get("device") or {}).get("backend") == backend
           and (p.get("device") or {}).get("serial") not in (None, "", serial)]
    out.sort(key=lambda p: (str((p.get("device") or {}).get("serial")),
                            (p.get("optics") or {}).get("name", ""),
                            -int(p.get("version", 0))))
    return out


def profile_label(p: dict) -> str:
    """Dropdown text: name · type · version · date."""
    o = (p.get("optics") or {}).get("name") or "?"
    t = p.get("type", TYPE_RELATIVE)
    date = str(p.get("created", ""))[:10]
    tag = " · legacy" if p.get("apply") == APPLY_LEGACY_GAIN else ""
    if is_implausible(p):
        tag += " · NOT PLAUSIBLE"
    if not (p.get("device") or {}).get("serial") and p.get("apply") != APPLY_LEGACY_GAIN:
        tag += " · unbound"
    return f"{o} · {t} · v{p.get('version', 1)} · {date}{tag}"


def is_implausible(p: dict) -> bool:
    """True for a profile saved although its plausibility check failed
    (explicit user override)."""
    pl = p.get("plausibility") or {}
    return pl.get("pass") is False


def is_absolute_reference_unit(unit: str) -> bool:
    try:
        from reference_sources import KNOWN_UNITS, UNIT_RELATIVE
    except Exception:
        return bool(unit) and unit != "relative"
    return unit in KNOWN_UNITS and unit != UNIT_RELATIVE


def _validate_profile(p: dict) -> None:
    t = p.get("type")
    if t not in (TYPE_RELATIVE, TYPE_ABSOLUTE):
        raise ValueError(f"invalid profile type '{t}'")
    inp = (p.get("optics") or {}).get("input")
    if t == TYPE_ABSOLUTE and inp not in ("CC", "sphere"):
        raise ValueError("Absolute profiles need an input optic with a defined "
                         "entrance area (CC or sphere); 'bare' is always relative.")
    if t == TYPE_ABSOLUTE and not is_absolute_reference_unit(
            (p.get("reference") or {}).get("unit", "")):
        raise ValueError("Absolute profiles need a reference source with an "
                         "absolute irradiance unit (e.g. uW/cm2/nm).")
    if not (p.get("optics") or {}).get("name"):
        raise ValueError("Optics configuration needs a name.")
    if not (p.get("device") or {}).get("backend"):
        raise ValueError("Profile needs device.backend.")
    if not p.get("table"):
        raise ValueError("Profile has an empty table.")


def new_profile(*, kind: str, backend: str, serial: str | None, optics: dict,
                reference: dict, wl_fingerprint: str, result: dict,
                acquisition: dict, checks: dict, ref_unit: str = "",
                plausibility: dict | None = None) -> dict:
    """Assemble a schema-2 profile from a compute_calibration() result.

    plausibility: broadband_check() output (+ "override": True when the user
    saved a failed result explicitly); stored so the profile stays flagged."""
    unit = (UNIT_RELATIVE if kind == TYPE_RELATIVE
            else f"{ref_unit or reference.get('unit', '')} per count/s")
    out = {
        "schema": SCHEMA_VERSION,
        "type": kind,
        "device": {"backend": backend, "serial": serial},
        "optics": {"name": optics.get("name", ""),
                   "input": optics.get("input", INPUT_UNKNOWN),
                   "notes": optics.get("notes", "")},
        "reference": {"name": reference.get("name", ""),
                      "serial": reference.get("serial", ""),
                      "unit": reference.get("unit", ref_unit)},
        "wl_fingerprint": wl_fingerprint,
        "valid_range_nm": list(result["valid_range_nm"]),
        "table": result_table(result),
        "unit": unit,
        "acquisition": dict(acquisition),
        "checks": dict(checks),
        "gain_span": float(result.get("gain_span", float("nan"))),
        "apply": APPLY_V2,
    }
    if result.get("segments_nm") is not None:
        out["valid_segments_nm"] = [list(map(float, s)) for s in result["segments_nm"]]
    if plausibility is not None:
        out["plausibility"] = dict(plausibility)
    return out


def add_profile(p: dict) -> dict:
    """Store p as a NEW version of its series (older versions stay)."""
    import uuid
    p = _json_clean(p)                          # deep copy, strict-JSON clean
    p["schema"] = SCHEMA_VERSION
    p.setdefault("apply", APPLY_V2)
    _validate_profile(p)
    reg = load_registry()
    k = series_key(p)
    versions = [int(q.get("version", 0)) for q in reg["profiles"] if series_key(q) == k]
    p["version"] = (max(versions) + 1) if versions else 1
    p["id"] = uuid.uuid4().hex[:12]
    p.setdefault("created", _now_iso())
    if not p.get("created"):
        p["created"] = _now_iso()
    reg["profiles"].append(p)
    save_registry(reg)
    return p


def delete_profile_id(pid: str) -> bool:
    reg = load_registry()
    n = len(reg["profiles"])
    reg["profiles"] = [p for p in reg["profiles"] if p.get("id") != pid]
    if len(reg["profiles"]) != n:
        save_registry(reg)
        return True
    return False


def copy_profile_to_device(pid: str, serial: str | None, backend: str | None = None) -> dict:
    """Deliberately adopt a profile on another unit (e.g. a 2nd HDX). The copy
    is bound to the new serial and records where it came from."""
    src = get_profile_by_id(pid)
    if src is None:
        raise KeyError(pid)
    cp = json.loads(json.dumps(src))
    for k in ("id", "version"):
        cp.pop(k, None)
    cp["created"] = _now_iso()
    cp["device"] = {"backend": backend or (src.get("device") or {}).get("backend"),
                    "serial": serial}
    cp["copied_from"] = {"id": src.get("id"),
                         "serial": (src.get("device") or {}).get("serial"),
                         "version": src.get("version"),
                         "date": _now_iso()}
    return add_profile(cp)


def export_profile(pid: str, path: str) -> None:
    p = get_profile_by_id(pid)
    if p is None:
        raise KeyError(pid)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(p, f, indent=1, allow_nan=False)


def import_profile(path: str) -> dict:
    """Import a profile file (schema 2, or a bare schema-1 entry) as a new
    version of its series."""
    with open(path, "r", encoding="utf-8") as f:
        p = json.load(f)
    if p.get("schema") != SCHEMA_VERSION:
        name = os.path.splitext(os.path.basename(path))[0]
        p = migrate_legacy_entry(p.get("device", "unknown") if isinstance(p.get("device"), str)
                                 else "unknown", name, p)
    p = dict(p)
    p.pop("id", None)
    p.pop("version", None)
    p["imported_from"] = os.path.basename(path)
    return add_profile(p)


def profile_arrays(p: dict):
    """(wl, value) arrays of a profile table; masked rows → NaN."""
    t = p.get("table") or []
    wl = np.array([r[0] for r in t], float)
    v = np.array([np.nan if r[1] is None else r[1] for r in t], float)
    return wl, v


# ── Legacy-compatible name-based API (schema-1 call sites) ────────────────

def _find_legacy(device: str, name: str) -> dict | None:
    for p in all_profiles():
        if (p.get("device") or {}).get("backend") != device:
            continue
        if p.get("id") == name or p.get("legacy_name") == name:
            return p
    return None


def list_profiles(device: str) -> list[str]:
    """IDs of every profile stored for this backend (any serial/version)."""
    return sorted(p["id"] for p in all_profiles()
                  if (p.get("device") or {}).get("backend") == device)


def get_profile(device: str, name: str) -> dict | None:
    return _find_legacy(device, name)


def get_profile_table(device: str, name: str) -> np.ndarray | None:
    p = get_profile(device, name)
    if not p or not p.get("table"):
        return None
    wl, v = profile_arrays(p)
    return np.column_stack([wl, v])


def save_profile(device: str, name: str, table, meta: dict | None = None) -> dict:
    """Legacy path ('reference = second measurement'): store a full-axis
    sensitivity table with the OLD application semantics (legacy_gain)."""
    entry = {"table": np.asarray(table, dtype=float).tolist(), "created": _now_iso()}
    if meta:
        entry.update(meta)
    p = migrate_legacy_entry(device, name, entry)
    p.pop("migrated_from", None)
    p.pop("id", None)
    return add_profile(p)


def delete_profile(device: str, name: str) -> bool:
    p = _find_legacy(device, name)
    return delete_profile_id(p["id"]) if p else False


# ── Active-selection bookkeeping (lives in the MAIN config) ───────────────

def active_selection(device: str) -> str:
    """SEL_NONE / SEL_DEVICE_DEFAULT / <profile id>. Schema-1 selections
    (profile names) are resolved to the migrated profile's id."""
    try:
        from app_config import Config
        m = Config.get("response_correction_active", {}) or {}
        sel = m.get(device, SEL_NONE)
    except Exception:
        return SEL_NONE
    if sel in (SEL_NONE, SEL_DEVICE_DEFAULT):
        return sel
    p = _find_legacy(device, sel)
    return p["id"] if p else SEL_NONE


def set_active_selection(device: str, selection: str) -> None:
    """Persist the selection for a device and keep the legacy master boolean
    'spectral_response_compensation' in sync (None -> off, anything else -> on)."""
    from app_config import Config
    m = dict(Config.get("response_correction_active", {}) or {})
    m[device] = selection
    Config.set("response_correction_active", m, save=False)
    Config.set("spectral_response_compensation", selection != SEL_NONE)


def _override_token(fingerprint: str | None, nl_active: bool | None) -> str:
    """The device state a confirmation is valid for. Without a known
    linearisation state it is the plain fingerprint (backward compatible)."""
    fp = fingerprint or ""
    return fp if nl_active is None else f"{fp}|nl={int(bool(nl_active))}"


def confirm_fingerprint_override(pid: str, fingerprint: str | None,
                                 nl_active: bool | None = None) -> None:
    """User confirmed applying profile pid although the wavelength calibration
    and/or the detector linearisation changed. Valid only for exactly this
    current state (fingerprint + linearisation); any further change asks again."""
    from app_config import Config
    m = dict(Config.get("calibration_fingerprint_overrides", {}) or {})
    m[pid] = _override_token(fingerprint, nl_active)
    Config.set("calibration_fingerprint_overrides", m)


def fingerprint_override_ok(pid: str, fingerprint: str | None,
                            nl_active: bool | None = None) -> bool:
    try:
        from app_config import Config
        m = Config.get("calibration_fingerprint_overrides", {}) or {}
    except Exception:
        return False
    if not fingerprint and nl_active is None:
        return False
    return m.get(pid) == _override_token(fingerprint, nl_active)


def confirm_override(pid: str, backend) -> None:
    """Convenience for GUI/web: confirm for the backend's CURRENT state."""
    fp = None
    try:
        fp = backend.wl_fingerprint() if backend is not None else None
    except Exception:
        pass
    confirm_fingerprint_override(pid, fp, device_nl_active(backend))


def resolve_table(device: str, selection: str, device_default_table=None):
    """Nx2 table for a LEGACY-gain selection, or None (no legacy gain)."""
    if selection == SEL_NONE:
        return None
    if selection == SEL_DEVICE_DEFAULT:
        return (None if device_default_table is None
                else np.asarray(device_default_table, dtype=float))
    p = _find_legacy(device, selection)
    if p is None or p.get("apply") != APPLY_LEGACY_GAIN:
        return None
    return get_profile_table(device, selection)


def build_gain_for_selection(wl, device: str, selection: str,
                             device_default_table=None, max_gain: float = 10.0):
    """Legacy multiplicative gain for desktop + web. Returns (gain,
    effective_selection). v2 profiles are not a gain (they are applied by
    processing.apply_calibration) → ones here, selection kept."""
    wl = np.asarray(wl, dtype=float)
    p = None if selection in (SEL_NONE, SEL_DEVICE_DEFAULT) else _find_legacy(device, selection)
    if p is not None and p.get("apply") == APPLY_V2:
        return np.ones(len(wl)), selection
    table = resolve_table(device, selection, device_default_table)
    if table is None:                      # unchanged schema-1 behaviour
        return np.ones(len(wl)), selection
    try:
        mg = float((p or {}).get("legacy_meta", {}).get("max_gain", max_gain))
        return build_response_gain(wl, np.asarray(table, dtype=float),
                                   max_gain=mg), selection
    except Exception as e:
        print(f"[calib] gain build failed for '{selection}': {e}")
        return np.ones(len(wl)), SEL_NONE


def selection_items(device: str, has_device_default: bool,
                    serial: str | None = None, fingerprint: str | None = None,
                    include_old: bool = False,
                    nl_active: bool | None = None) -> list[str]:
    """Dropdown values: None, optionally Device default, then profile ids of
    this unit (see profiles_for_device). Labels via selection_label()."""
    items = [SEL_NONE]
    if has_device_default:
        items.append(SEL_DEVICE_DEFAULT)
    items.extend(p["id"] for p in profiles_for_device(
        device, serial, fingerprint, include_old=include_old, nl_active=nl_active))
    return items


def selection_label(selection: str) -> str:
    if selection in (SEL_NONE, SEL_DEVICE_DEFAULT):
        return selection
    p = get_profile_by_id(selection)
    return profile_label(p) if p else selection


# ══════════════════════════════════════════════════════════════════════════
#  Runtime: what the measurement pipeline applies (shared desktop + web)
# ══════════════════════════════════════════════════════════════════════════

def irradiance_unit_factor(profile_unit: str, out_unit: str) -> float:
    """Scale from a profile's E-unit to the requested display unit."""
    from reference_sources import convert_irradiance
    src = (profile_unit or "").replace(" per count/s", "").strip()
    if not src or src == out_unit:
        return 1.0
    return float(convert_irradiance(np.array([1.0]), src, out_unit)[0])


class CalibrationApplier:
    """Per-pixel application of a v2 profile on a given wavelength axis.

    factor: relative → 1/S, absolute → K·unit_scale; NaN where masked. The
    measurement count rate C is multiplied by factor."""

    def __init__(self, profile: dict, wl, out_irradiance_unit: str = "uW/cm2/nm"):
        self.profile = profile
        self.profile_id = profile.get("id")
        self.type = profile.get("type", TYPE_RELATIVE)
        wl = np.asarray(wl, float)
        twl, tv = profile_arrays(profile)
        if twl.size < 2:
            raise ValueError("profile table too short")
        order = np.argsort(twl)
        twl, tv = twl[order], tv[order]
        lo, hi = profile.get("valid_range_nm") or [twl[0], twl[-1]]
        lo = twl[0] if lo is None else max(float(lo), twl[0])
        hi = twl[-1] if hi is None else min(float(hi), twl[-1])
        good = np.isfinite(tv)
        # value on the axis: linear interpolation between finite rows; a pixel is
        # masked if it lies outside [lo, hi] or nearest to a masked table row.
        val = np.interp(wl, twl[good], tv[good]) if np.count_nonzero(good) >= 2 \
            else np.full(wl.size, np.nan)
        idx = np.clip(np.searchsorted(twl, wl), 1, twl.size - 1)
        nearest = np.where(np.abs(wl - twl[idx - 1]) <= np.abs(twl[idx] - wl), idx - 1, idx)
        valid = (wl >= lo) & (wl <= hi) & good[nearest] & np.isfinite(val) & (val > 0)
        if self.type == TYPE_ABSOLUTE:
            self.unit = out_irradiance_unit
            scale = irradiance_unit_factor(profile.get("unit", ""), out_irradiance_unit)
            factor = val * scale
        else:
            self.unit = UNIT_RELATIVE
            with np.errstate(divide="ignore", invalid="ignore"):
                factor = 1.0 / val
        self.valid = valid
        self.factor = np.where(valid, factor, np.nan)
        self.valid_range_nm = (float(lo), float(hi))
        # "Corrected counts" display: corrected rate × t_int, normalised so that
        # in the pixel of maximum sensitivity (smallest factor) Corrected = Raw.
        fv = self.factor[valid & np.isfinite(self.factor) & (self.factor > 0)]
        self.counts_norm = float(1.0 / np.min(fv)) if fv.size else 1.0

    def apply(self, rate: np.ndarray) -> np.ndarray:
        return np.asarray(rate, float) * self.factor


class CalibrationRuntime:
    """Resolves the active selection of the connected device into what the
    pipeline needs. Used IDENTICALLY by desktop and web, so both apply the same
    profile the same way.

    After configure()/refresh():
        applier      CalibrationApplier | None    (v2 profile in effect)
        legacy_gain  ndarray | None               (legacy profile / device default)
        status       dict(state, message, label, unit, profile_id)
    """

    def __init__(self):
        self.backend_name = ""
        self.serial = None
        self.fingerprint = None
        self.nl_active = None
        self.linearity = None            # calibration_linearity info of the unit
        self.wl = None
        self.device_default_table = None
        self.applier: CalibrationApplier | None = None
        self.legacy_gain = None
        self.profile = None
        self.status = {"state": "none", "message": "", "label": SEL_NONE,
                       "unit": UNIT_COUNTS, "profile_id": None}
        self._dark_cache: dict = {}
        self.last_dark_source = None

    def configure(self, backend_name: str, backend=None, wl=None) -> None:
        self.backend_name = backend_name or ""
        self.serial = getattr(backend, "serial", None) if backend is not None else None
        try:
            self.fingerprint = backend.wl_fingerprint() if backend is not None else None
        except Exception:
            self.fingerprint = None
        self.nl_active = device_nl_active(backend)
        self.linearity = getattr(backend, "linearity_info", None) if backend is not None else None
        if wl is None and backend is not None:
            wl = backend.wavelength_array
        self.wl = None if wl is None else np.asarray(wl, float)
        self.device_default_table = (getattr(backend, "RESPONSE_TABLE", None)
                                     if backend is not None else None)
        self._dark_cache.clear()
        self.refresh()

    def update_axis(self, wl, backend=None) -> None:
        """Wavelength axis changed (e.g. live wavelength calibration)."""
        self.wl = np.asarray(wl, float)
        if backend is not None:
            try:
                self.fingerprint = backend.wl_fingerprint()
            except Exception:
                pass
            self.nl_active = device_nl_active(backend)
        else:
            from calibration_utils import wavelength_fingerprint
            self.fingerprint = wavelength_fingerprint(self.wl)
        self._dark_cache.clear()
        self.refresh()

    @property
    def active_v2(self) -> bool:
        return self.applier is not None

    def refresh(self) -> None:
        self.applier = None
        self.legacy_gain = None
        self.profile = None
        sel = active_selection(self.backend_name) if self.backend_name else SEL_NONE
        st = {"state": "none", "message": "", "label": selection_label(sel),
              "unit": UNIT_COUNTS, "profile_id": None, "selection": sel}
        if self.wl is None or sel == SEL_NONE:
            self.status = st
            return
        if sel == SEL_DEVICE_DEFAULT:
            gain, eff = build_gain_for_selection(
                self.wl, self.backend_name, sel, self.device_default_table)
            if eff != SEL_NONE:
                self.legacy_gain = gain
                st.update(state=STATE_OK, message="Device default response table")
            self.status = st
            return
        p = get_profile_by_id(sel)
        if p is None:
            st.update(state="missing", message="Selected profile not found.")
            self.status = st
            return
        ps = profile_status(p, self.backend_name, self.serial, self.fingerprint,
                            self.nl_active, linearity=self.linearity)
        st.update(state=ps["state"], message=ps["message"], profile_id=p["id"],
                  type=p.get("type"))
        if ps.get("linearity_note"):
            st["linearity_note"] = ps["linearity_note"]
            st["message"] = ((st.get("message") or "") + " " + ps["linearity_note"]).strip()
        if ps["state"] == STATE_OTHER_DEVICE:
            self.status = st
            return
        if (ps["state"] in CONFIRM_STATES
                and not fingerprint_override_ok(p["id"], self.fingerprint, self.nl_active)):
            st["message"] = (ps["message"].split(" (Applying")[0]
                             + " Not applied (confirm to apply anyway).")
            st["needs_confirmation"] = True
            self.status = st
            return
        self.profile = p
        if p.get("apply") == APPLY_LEGACY_GAIN:
            gain, _ = build_gain_for_selection(self.wl, self.backend_name, p["id"])
            self.legacy_gain = gain
        else:
            try:
                from app_config import Config
                out_u = Config.get("irradiance_unit", "uW/cm2/nm")
            except Exception:
                out_u = "uW/cm2/nm"
            try:
                self.applier = CalibrationApplier(p, self.wl, out_u)
                st["unit"] = self.applier.unit
                st["valid_range_nm"] = list(self.applier.valid_range_nm)
                if p.get("valid_segments_nm"):
                    st["valid_segments_nm"] = [list(map(float, x))
                                               for x in p["valid_segments_nm"]]
                if is_implausible(p):
                    st["implausible"] = True
            except Exception as e:
                st.update(state="error", message=f"Profile could not be applied: {e}")
        self.status = st

    def dark_for(self, integration_us: int):
        """(per-pixel dark | None, source) for this unit at this t_int, cached.
        source: 'stored' (exact t_int), 'stored_model' (b + r·t fitted over the
        stored darks at other t_int) or None (nothing usable stored)."""
        from calibration_capture import dark_for_integration
        key = int(integration_us)
        if key not in self._dark_cache:
            src, arr, _info = dark_for_integration(
                self.backend_name, self.serial, key,
                pixel_count=None if self.wl is None else self.wl.size)
            self._dark_cache[key] = (arr, src)
        arr, src = self._dark_cache[key]
        self.note_dark_source(src if arr is not None else None)
        return arr, src

    def stored_dark(self, integration_us: int):
        """Per-pixel dark for this t_int (exact or modelled), or None."""
        return self.dark_for(integration_us)[0]

    def note_dark_source(self, src) -> None:
        """Record which dark the calibrated pipeline used; warns in the status
        when a v2 profile runs without a per-pixel dark."""
        self.last_dark_source = src
        if not self.active_v2:
            return
        if src is None:
            self.status["dark_warning"] = (
                "No per-pixel dark spectrum stored for this integration time — "
                "using the scalar dark estimate; calibrated values are NOT "
                "reliable. Use 'Capture dark' (input covered).")
        else:
            self.status.pop("dark_warning", None)
        self.status["dark_source"] = src

    def invalidate_darks(self) -> None:
        self._dark_cache.clear()

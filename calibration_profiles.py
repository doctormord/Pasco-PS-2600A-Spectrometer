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
#  Registry (separate JSON file — tables are large)
# ══════════════════════════════════════════════════════════════════════════

DEFAULT_REGISTRY_FILE = "calibration_profiles.json"

SEL_NONE = "None"
SEL_DEVICE_DEFAULT = "Device default"


def _registry_path() -> str:
    try:
        from app_config import Config
        return Config.get("calibration_profiles_file", DEFAULT_REGISTRY_FILE)
    except Exception:
        return DEFAULT_REGISTRY_FILE


def load_registry() -> dict:
    path = _registry_path()
    if not os.path.exists(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f) or {}
    except Exception as e:
        print(f"[calib] failed to read {path}: {e}")
        return {}


def save_registry(reg: dict) -> None:
    path = _registry_path()
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(reg, f, indent=2)
    except Exception as e:
        print(f"[calib] failed to write {path}: {e}")


def list_profiles(device: str) -> list[str]:
    """Saved response-correction profile names for a device, sorted."""
    return sorted((load_registry().get(device) or {}).keys())


def get_profile(device: str, name: str) -> dict | None:
    return (load_registry().get(device) or {}).get(name)


def get_profile_table(device: str, name: str) -> np.ndarray | None:
    p = get_profile(device, name)
    if not p or "table" not in p:
        return None
    return np.asarray(p["table"], dtype=float)


def save_profile(device: str, name: str, table, meta: dict | None = None) -> None:
    """Store (overwrite) a response-correction table under device/name."""
    reg = load_registry()
    dev = reg.setdefault(device, {})
    entry = {
        "table": np.asarray(table, dtype=float).tolist(),
        "created": time.strftime("%Y-%m-%d %H:%M:%S"),
    }
    if meta:
        entry.update(meta)
    dev[name] = entry
    save_registry(reg)


def delete_profile(device: str, name: str) -> bool:
    reg = load_registry()
    dev = reg.get(device)
    if dev and name in dev:
        del dev[name]
        if not dev:
            del reg[device]
        save_registry(reg)
        return True
    return False


# ── Active-selection bookkeeping (lives in the MAIN config) ───────────────

def active_selection(device: str) -> str:
    """Return the current response-correction selection for a device.
    One of SEL_NONE / SEL_DEVICE_DEFAULT / <profile name>."""
    try:
        from app_config import Config
        m = Config.get("response_correction_active", {}) or {}
        return m.get(device, SEL_NONE)
    except Exception:
        return SEL_NONE


def set_active_selection(device: str, selection: str) -> None:
    """Persist the selection for a device and keep the legacy master boolean
    'spectral_response_compensation' in sync (None -> off, anything else -> on)
    so processing.py / web_server need no awareness of profiles."""
    from app_config import Config
    m = dict(Config.get("response_correction_active", {}) or {})
    m[device] = selection
    Config.set("response_correction_active", m, save=False)
    Config.set("spectral_response_compensation", selection != SEL_NONE)


def resolve_table(device: str, selection: str, device_default_table=None):
    """Return the Nx2 sensitivity table for a selection, or None when the
    effective correction is a no-op (gain = ones)."""
    if selection == SEL_NONE:
        return None
    if selection == SEL_DEVICE_DEFAULT:
        return (None if device_default_table is None
                else np.asarray(device_default_table, dtype=float))
    return get_profile_table(device, selection)


def build_gain_for_selection(
    wl,
    device: str,
    selection: str,
    device_default_table=None,
):
    """Shared helper for desktop + web: turn a selection into a per-pixel gain
    array on the given wavelength axis. Returns (gain, effective_selection);
    a missing/unreadable profile falls back to ones + SEL_NONE."""
    wl = np.asarray(wl, dtype=float)
    table = resolve_table(device, selection, device_default_table)
    if table is None:
        return np.ones(len(wl)), selection
    try:
        return build_response_gain(wl, np.asarray(table, dtype=float)), selection
    except Exception as e:
        print(f"[calib] gain build failed for '{selection}': {e}")
        return np.ones(len(wl)), SEL_NONE


def selection_items(device: str, has_device_default: bool) -> list[str]:
    """Dropdown items for a device: None, optionally Device default, then
    saved profiles."""
    items = [SEL_NONE]
    if has_device_default:
        items.append(SEL_DEVICE_DEFAULT)
    items.extend(list_profiles(device))
    return items

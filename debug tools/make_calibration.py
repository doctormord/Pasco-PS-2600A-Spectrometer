#!/usr/bin/env python3
"""
make_calibration.py — Calibration-file generator for the PASCO PS-2600A
Multi-Device Spectrum Analyzer (PASCO / ASEQ LR-2T / Ocean HDX).

Two independent jobs, exposed as sub-commands:

  wavelength   Fit a degree-3 (configurable) pixel -> nm polynomial from a
               measured emission-lamp spectrum (e.g. a Cadmium lamp). Detects
               the lamp's known lines in the trace, fits the polynomial and
               writes the coefficients in the form the drivers expect.

  response     Cross-device spectral-response (gain) calibration. Takes a
               reference spectrum from one spectrometer (e.g. the HDX) and the
               SAME broadband source measured on the other spectrometers,
               normalises every spectrum to its own peak (= 1.0) and divides
               each device by the reference to obtain a per-wavelength relative
               sensitivity table. Fed to the driver's response compensation
               (calibration_utils.build_response_gain) this makes the other
               devices reproduce the reference's spectral shape.

The tool is device-agnostic: it reads the project's CSV export formats and
writes the calibration artefacts. It reuses calibration_utils.py so the fit and
gain conventions match the rest of the application exactly.

Run `python make_calibration.py -h` and `python make_calibration.py <cmd> -h`
for full help on every parameter.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from typing import Optional

import numpy as np
from scipy.signal import find_peaks

# Reuse the project's calibration conventions (single source of truth).
try:
    from calibration_utils import (
        fit_wavelength_polynomial,
        poly_wavelength_array,
    )
except ImportError:
    sys.stderr.write(
        "ERROR: calibration_utils.py not found. Run this script from the "
        "project directory (next to calibration_utils.py).\n"
    )
    raise


# ══════════════════════════════════════════════════════════════════════════
# Known calibration-grade emission lines (nm), per lamp.
# Strong, comparatively isolated lines suited to wavelength calibration.
# Values match the project's reference library / NIST.
# ══════════════════════════════════════════════════════════════════════════

CALIBRATION_LINES: dict[str, list[float]] = {
    "Cd": [346.620, 361.051, 467.815, 479.991, 508.582, 643.847],
    "Hg": [365.015, 404.656, 435.833, 546.074, 576.960, 579.066],
    "Ne": [585.249, 614.306, 640.225, 650.653, 692.947, 703.241,
           724.517, 743.890],
    "Ar": [696.543, 706.722, 738.398, 750.387, 763.511, 772.376,
           811.531, 842.465],
    "Kr": [427.397, 431.958, 445.392, 557.029, 587.092, 758.741,
           760.155, 769.454, 810.436, 819.006, 829.811],
    "Xe": [462.428, 467.123, 473.415, 480.702, 492.315, 823.163,
           828.012, 834.682, 881.941],
    "He": [388.865, 447.148, 471.315, 492.193, 501.568, 587.562,
           667.815, 706.519, 728.135],
    "Na": [568.820, 589.293, 615.420],
    "H":  [410.174, 434.047, 486.135, 656.279],
}

# Friendly aliases -> canonical symbol.
_LAMP_ALIASES = {
    "cadmium": "Cd", "cd": "Cd",
    "mercury": "Hg", "hg": "Hg",
    "neon": "Ne", "ne": "Ne",
    "argon": "Ar", "ar": "Ar",
    "krypton": "Kr", "kr": "Kr",
    "xenon": "Xe", "xe": "Xe",
    "helium": "He", "he": "He",
    "sodium": "Na", "na": "Na",
    "hydrogen": "H", "h": "H",
}

# Per-device output hints (config key / module constant the coeffs go to).
DEVICE_WL_TARGET = {
    "pasco": "module constant PASCO_WAVELENGTH_COEFFS in _device_pasco.py",
    "lr2t":  "driver attribute self.wl_poly_coeffs in _device_lr2t.py",
    "hdx":   "driver attribute self.wl_poly_coeffs in _device_ocean.py",
}
DEVICE_RESPONSE_TARGET = {
    "pasco": "module constant PASCO_RESPONSE_TABLE in _device_pasco.py",
    "lr2t":  "config key (no key yet; mirror ocean_response_table for LR-2T)",
    "hdx":   "config key ocean_response_table in spectrometer_config.json",
}


# ══════════════════════════════════════════════════════════════════════════
# CSV loading — handles every export format used in this project
# ══════════════════════════════════════════════════════════════════════════

class Spectrum:
    """A loaded spectrum: pixel index, optional wavelength axis, intensity."""

    def __init__(self, pixels, intensity, wavelength=None, source=""):
        self.pixels = np.asarray(pixels, dtype=float)
        self.intensity = np.asarray(intensity, dtype=float)
        self.wavelength = (None if wavelength is None
                           else np.asarray(wavelength, dtype=float))
        self.source = source

    def __len__(self):
        return len(self.intensity)


def _detect_delimiter(line: str) -> Optional[str]:
    """Return ';' or ',' if present, else None (meaning whitespace-split)."""
    if ";" in line:
        return ";"
    if "," in line:
        return ","
    return None


def _is_number(tok: str) -> bool:
    try:
        float(tok)
        return True
    except ValueError:
        return False


def load_spectrum(path: str, column: Optional[str] = None) -> Spectrum:
    """
    Load a spectrum CSV in any of the project's formats:

      * comma + header        wavelength_nm,intensity_counts        (HDX export)
      * whitespace, no header  "<nm> <intensity>"                   (Lasertack)
      * ';' + '#' comments + header  Pixel_ID;Wavelength_nm;ADC_Counts (PASCO)
      * ';' multi-column + header   Wavelength_nm;<col>;<col>;...    (library)

    Column detection:
      pixel       -> header name containing 'pixel' (else row index is used)
      wavelength  -> header name containing 'wave'/'nm' (else first numeric col)
      intensity   -> `column` (by header name) if given, else a header name
                     containing 'intens'/'adc'/'count', else the last column
    """
    with open(path, "r", encoding="utf-8-sig", newline="") as f:
        raw = [ln.rstrip("\r\n") for ln in f]

    rows = [ln for ln in raw if ln.strip() and not ln.lstrip().startswith("#")]
    if not rows:
        raise ValueError(f"{path}: no data rows found.")

    delim = _detect_delimiter(rows[0])

    def split(ln: str) -> list[str]:
        return ln.split(delim) if delim else ln.split()

    first = split(rows[0])
    has_header = not _is_number(first[0].strip())

    if has_header:
        header = [h.strip() for h in first]
        data_rows = rows[1:]
    else:
        header = []
        data_rows = rows

    # Parse the numeric grid (tolerate ragged/garbage tail lines).
    parsed: list[list[float]] = []
    ncol = len(first)
    for ln in data_rows:
        toks = split(ln)
        if len(toks) < ncol:
            continue
        try:
            parsed.append([float(t) for t in toks[:ncol]])
        except ValueError:
            continue
    if not parsed:
        raise ValueError(f"{path}: could not parse any numeric rows.")
    arr = np.asarray(parsed, dtype=float)

    # Resolve column roles.
    pixel_idx = wl_idx = int_idx = None
    if header:
        low = [h.lower() for h in header]
        for i, h in enumerate(low):
            if pixel_idx is None and "pixel" in h:
                pixel_idx = i
            if wl_idx is None and ("wave" in h or h.endswith("_nm") or h == "nm"
                                   or "wavelength" in h):
                wl_idx = i
        if column is not None:
            matches = [i for i, h in enumerate(header)
                       if h.lower() == column.lower()]
            if not matches:
                raise ValueError(
                    f"{path}: column '{column}' not found. "
                    f"Available: {', '.join(header)}")
            int_idx = matches[0]
        else:
            for i, h in enumerate(low):
                if int_idx is None and ("intens" in h or "adc" in h
                                        or "count" in h):
                    int_idx = i
        if wl_idx is None:
            wl_idx = 0
        if int_idx is None:
            int_idx = arr.shape[1] - 1
    else:
        # No header: first col = wavelength, second = intensity (or single col).
        if arr.shape[1] == 1:
            wl_idx, int_idx = None, 0
        else:
            wl_idx, int_idx = 0, 1

    intensity = arr[:, int_idx]
    wavelength = arr[:, wl_idx] if wl_idx is not None else None
    if pixel_idx is not None:
        pixels = arr[:, pixel_idx]
    else:
        pixels = np.arange(len(intensity), dtype=float)

    return Spectrum(pixels, intensity, wavelength, source=os.path.basename(path))


# ══════════════════════════════════════════════════════════════════════════
# Peak detection with sub-pixel refinement
# ══════════════════════════════════════════════════════════════════════════

def find_lines(spec: Spectrum, prominence: Optional[float],
               prominence_frac: float, min_distance: int):
    """
    Find emission peaks. Returns parallel arrays of
    (refined_pixel, refined_wavelength_or_None, height).
    Sub-pixel position via parabolic interpolation on the raw intensity.
    """
    y = spec.intensity
    if prominence is None:
        span = float(np.nanmax(y) - np.nanmin(y))
        prom = max(span * prominence_frac, 1e-9)
    else:
        prom = prominence

    idx, props = find_peaks(y, prominence=prom, distance=max(1, min_distance))
    if len(idx) == 0:
        return np.array([]), np.array([]), np.array([])

    refined_px = []
    refined_wl = []
    heights = []
    for i in idx:
        # Parabolic sub-pixel refinement using neighbours.
        if 0 < i < len(y) - 1:
            ym1, y0, yp1 = y[i - 1], y[i], y[i + 1]
            denom = (ym1 - 2.0 * y0 + yp1)
            delta = 0.5 * (ym1 - yp1) / denom if denom != 0 else 0.0
            delta = float(np.clip(delta, -0.5, 0.5))
        else:
            delta = 0.0
        px = float(spec.pixels[i] + delta)
        refined_px.append(px)
        heights.append(float(y[i]))
        if spec.wavelength is not None:
            # Interpolate the (approx) wavelength axis at the refined pixel.
            wl = float(np.interp(px, spec.pixels, spec.wavelength))
            refined_wl.append(wl)
        else:
            refined_wl.append(None)

    return (np.asarray(refined_px, dtype=float),
            np.asarray(refined_wl, dtype=object),
            np.asarray(heights, dtype=float))


def match_lines(peak_px, peak_wl, peak_h, known_nm, window_nm):
    """
    Match known lines to detected peaks by approximate wavelength using global
    nearest-neighbour assignment: among all (line, peak) pairs within
    `window_nm`, assign the closest pairs first, each line and each peak used
    at most once. This is order-independent and robust to a roughly-correct but
    shifted starting axis (gross residuals are cleaned up later by the fit's
    outlier rejection).

    Returns (matched_pixels, matched_known_nm, unmatched_known_nm).
    """
    if any(w is None for w in peak_wl):
        raise ValueError(
            "The measured spectrum has no wavelength axis, so peaks cannot be "
            "matched to known lines by wavelength. Use a CSV that includes the "
            "device's current (approximate) wavelength column.")

    peak_wl_f = peak_wl.astype(float)

    # All candidate pairs within the window, sorted by absolute distance.
    cands = []
    for ki, k in enumerate(known_nm):
        for j in range(len(peak_px)):
            d = abs(peak_wl_f[j] - k)
            if d <= window_nm:
                cands.append((d, ki, j))
    cands.sort()

    used_line = set()
    used_peak = set()
    assigned: dict[int, int] = {}   # line index -> peak index
    for d, ki, j in cands:
        if ki in used_line or j in used_peak:
            continue
        assigned[ki] = j
        used_line.add(ki)
        used_peak.add(j)

    matched_px, matched_nm = [], []
    unmatched = []
    for ki, k in enumerate(known_nm):
        if ki in assigned:
            matched_px.append(float(peak_px[assigned[ki]]))
            matched_nm.append(float(k))
        else:
            unmatched.append(float(k))

    pairs = sorted(zip(matched_px, matched_nm))
    mpx = [p for p, _ in pairs]
    mnm = [w for _, w in pairs]
    return mpx, mnm, unmatched


# ══════════════════════════════════════════════════════════════════════════
# Sub-command: wavelength
# ══════════════════════════════════════════════════════════════════════════

def cmd_wavelength(args) -> int:
    lamp_key = _LAMP_ALIASES.get(args.lamp.lower().strip())
    if args.lines:
        known = sorted(float(x) for x in args.lines.split(","))
        lamp_label = "custom lines"
    else:
        if lamp_key is None:
            sys.stderr.write(
                f"Unknown lamp '{args.lamp}'. Known: "
                f"{', '.join(sorted(CALIBRATION_LINES))} "
                "(or pass --lines).\n")
            return 2
        known = CALIBRATION_LINES[lamp_key]
        lamp_label = lamp_key

    spec = load_spectrum(args.spectrum, column=args.column)
    print(f"Loaded {len(spec)} points from {spec.source}")
    if spec.wavelength is not None:
        print(f"  Approx. axis from file: "
              f"{spec.wavelength.min():.1f}–{spec.wavelength.max():.1f} nm")

    # ── Mode 1: list detected peaks and exit (read off pixel positions) ────
    if args.list_peaks:
        peak_px, peak_wl, peak_h = find_lines(
            spec, args.prominence, args.prominence_frac, args.min_distance)
        order = np.argsort(-peak_h)
        print(f"\nDetected {len(peak_px)} peaks "
              f"(strongest first; pixel / approx-nm / height):")
        for j in order:
            wl = peak_wl[j]
            wl_s = f"{float(wl):8.2f}" if wl is not None else "      --"
            print(f"    px {peak_px[j]:9.3f}   {wl_s} nm   {peak_h[j]:12.1f}")
        print("\nIdentify the lines you trust, then fit deterministically:")
        print("  python make_calibration.py wavelength <csv> "
              "--pixels p1,p2,... --lines nm1,nm2,...")
        return 0

    # ── Mode 2: explicit pixel<->wavelength pairs (deterministic, reliable) ─
    if args.pixels:
        px_list = [float(x) for x in args.pixels.split(",")]
        if len(px_list) != len(known):
            sys.stderr.write(
                f"\nERROR: --pixels has {len(px_list)} values but there are "
                f"{len(known)} line wavelengths "
                f"({'--lines' if args.lines else f'lamp {lamp_label}'}). "
                "Provide one pixel per line (and use --lines to set the exact "
                "lines/order if needed).\n")
            return 1
        pairs = sorted(zip(px_list, [float(k) for k in known]))
        matched_px = [p for p, _ in pairs]
        matched_nm = [w for _, w in pairs]
        print(f"  Using {len(matched_px)} explicit pixel/line pairs.")
    else:
        # ── Mode 3: automatic peak detection + matching ────────────────────
        print(f"  Lamp: {lamp_label}  ({len(known)} reference lines)")
        peak_px, peak_wl, peak_h = find_lines(
            spec, args.prominence, args.prominence_frac, args.min_distance)
        print(f"  Detected {len(peak_px)} peaks (prominence "
              f"{'auto' if args.prominence is None else args.prominence}, "
              f"min distance {args.min_distance} px)")
        matched_px, matched_nm, unmatched = match_lines(
            peak_px, peak_wl, peak_h, known, args.match_window_nm)
        if unmatched:
            print(f"  WARNING: {len(unmatched)} reference line(s) not found "
                  f"within +/-{args.match_window_nm} nm: "
                  f"{', '.join(f'{u:.1f}' for u in unmatched)}")
        if len(matched_px) < args.degree + 1:
            sys.stderr.write(
                f"\nERROR: matched only {len(matched_px)} line(s); a degree-"
                f"{args.degree} fit needs at least {args.degree + 1}.\n"
                "Auto-matching assumes the starting axis is roughly correct "
                "(within ~the line spacing). If it is far off, use\n"
                "  --list-peaks   to read pixel positions, then\n"
                "  --pixels p1,p2,...  with --lines nm1,nm2,...  to fit "
                "deterministically.\n")
            return 1
        print(f"\nMatched {len(matched_px)} lines:")
        for px, nm in zip(matched_px, matched_nm):
            print(f"    px {px:9.3f}  ->  {nm:8.3f} nm")
        print("  (auto-match assumes a roughly-correct starting axis; verify "
              "the pairs above, or use --pixels for a deterministic fit.)")

    pixel_count = int(args.pixel_count) if args.pixel_count else len(spec)

    # Fit with iterative single-worst-point outlier rejection. A spurious match
    # (e.g. a weak UV line snapped onto an artifact) shows up as a large
    # residual; drop it and refit while enough points remain.
    fit_px = list(matched_px)
    fit_nm = list(matched_nm)
    dropped = []
    while True:
        coeffs, residuals = fit_wavelength_polynomial(
            fit_px, fit_nm, degree=args.degree, pixel_count=pixel_count)
        max_err = float(np.max(np.abs(residuals)))
        if (args.max_residual_nm <= 0 or max_err <= args.max_residual_nm
                or len(fit_px) <= args.degree + 1):
            break
        worst = int(np.argmax(np.abs(residuals)))
        dropped.append((fit_nm[worst], float(residuals[worst])))
        del fit_px[worst]
        del fit_nm[worst]

    if dropped:
        print(f"\nDropped {len(dropped)} outlier line(s) "
              f"(|residual| > {args.max_residual_nm} nm):")
        for nm, r in dropped:
            print(f"    {nm:8.3f} nm  (residual {r:+.3f} nm)")
    matched_px, matched_nm = fit_px, fit_nm

    full = poly_wavelength_array(coeffs, pixel_count)
    print(f"\nFit (degree {args.degree}, {pixel_count} px):")
    print(f"  coeffs (ascending, lambda = C0 + C1*i + C2*i^2 + ...):")
    for n, c in enumerate(coeffs):
        print(f"    C{n} = {c: .10g}")
    print(f"  residuals (nm): "
          f"{', '.join(f'{r:+.3f}' for r in residuals.tolist())}")
    print(f"  max |residual| = {max_err:.4f} nm   RMS = "
          f"{float(np.sqrt(np.mean(residuals**2))):.4f} nm")
    print(f"  resulting range: {full[0]:.2f} .. {full[-1]:.2f} nm")

    # ── Sanity checks (catch overfit / mismatched-line garbage) ───────────
    warnings = []
    if len(matched_px) == args.degree + 1:
        warnings.append(
            f"only {len(matched_px)} points for a degree-{args.degree} fit "
            "(no redundancy: residuals are ~0 by construction and cannot reveal "
            "a bad match). Add more lines or lower --degree.")
    d = np.diff(full)
    if not (np.all(d > 0) or np.all(d < 0)):
        warnings.append(
            "the fitted axis is NOT monotonic across the sensor — the curve "
            "turns around inside the pixel range. The line<->pixel pairs are "
            "almost certainly mismatched. Re-run with --list-peaks and supply "
            "--pixels explicitly.")
    span = abs(full[-1] - full[0])
    if span > 4000 or full.min() < -200 or full.max() > 5000:
        warnings.append(
            f"the fitted range ({full.min():.0f}..{full.max():.0f} nm) is "
            "physically implausible — almost certainly a bad match. Use "
            "--list-peaks + --pixels.")
    if warnings:
        print("\n*** SANITY WARNING ***")
        for wmsg in warnings:
            print("  - " + wmsg)

    result = {
        "kind": "wavelength_polynomial",
        "device": args.device,
        "lamp": lamp_label,
        "degree": args.degree,
        "pixel_count": pixel_count,
        "coeffs": coeffs,
        "convention": "lambda(i) = C0 + C1*i + C2*i^2 + C3*i^3 (i = 0-based pixel)",
        "lines": [
            {"nm": float(nm), "pixel": float(px), "residual_nm": float(r)}
            for px, nm, r in zip(matched_px, matched_nm, residuals.tolist())
        ],
        "max_error_nm": max_err,
        "rms_error_nm": float(np.sqrt(np.mean(residuals**2))),
        "source_csv": spec.source,
    }

    out = args.out or f"wl_poly_{args.device}.json"
    with open(out, "w", encoding="utf-8") as f:
        json.dump(result, f, indent=2)
    print(f"\nWrote {out}")

    # Ready-to-use snippets.
    coeff_str = ", ".join(f"{c:.10g}" for c in coeffs)
    print("\n--- paste into the driver ------------------------------------")
    print(f"  target: {DEVICE_WL_TARGET.get(args.device, '(unknown device)')}")
    if args.device == "pasco":
        print("  PASCO_WAVELENGTH_COEFFS = [")
        for c in coeffs:
            print(f"      {c:.10g},")
        print("  ]")
    else:
        print(f"  self.wl_poly_coeffs = [{coeff_str}]")
        print(f"  # or config: \"{args.device}_wl_poly_coeffs\": [{coeff_str}]")
    print("--------------------------------------------------------------")

    if args.save_spectrum:
        _save_clean_spectrum(spec, args.save_spectrum)
        print(f"Saved cleaned spectrum -> {args.save_spectrum}")

    if args.plot:
        _plot_wavelength(matched_px, matched_nm, residuals, coeffs,
                         pixel_count, args.plot)

    return 0


def _save_clean_spectrum(spec: Spectrum, path: str) -> None:
    with open(path, "w", encoding="utf-8", newline="") as f:
        if spec.wavelength is not None:
            f.write("Pixel,Wavelength_nm,Intensity\n")
            for p, w, i in zip(spec.pixels, spec.wavelength, spec.intensity):
                f.write(f"{p:.0f},{w:.5f},{i:.5f}\n")
        else:
            f.write("Pixel,Intensity\n")
            for p, i in zip(spec.pixels, spec.intensity):
                f.write(f"{p:.0f},{i:.5f}\n")


# ══════════════════════════════════════════════════════════════════════════
# Sub-command: response (cross-device gain calibration)
# ══════════════════════════════════════════════════════════════════════════

def _parse_named_path(token: str) -> tuple[str, str]:
    """Parse a 'name=path' token. If no '=', name is derived from the filename."""
    if "=" in token:
        name, path = token.split("=", 1)
        return name.strip(), path.strip()
    base = os.path.splitext(os.path.basename(token))[0]
    return base, token


def _normalise_peak(y: np.ndarray) -> np.ndarray:
    m = float(np.nanmax(y))
    return y / m if m > 0 else y


def _moving_average(y: np.ndarray, n: int) -> np.ndarray:
    if n <= 1:
        return y
    n = int(n) | 1  # force odd
    pad = n // 2
    ypad = np.pad(y, pad, mode="edge")
    kern = np.ones(n) / n
    return np.convolve(ypad, kern, mode="valid")


def cmd_response(args) -> int:
    ref_name, ref_path = _parse_named_path(args.reference)
    ref = load_spectrum(ref_path, column=args.ref_column)
    if ref.wavelength is None:
        sys.stderr.write("ERROR: reference spectrum has no wavelength axis.\n")
        return 1
    print(f"Reference: {ref_name}  <- {ref.source}  "
          f"({ref.wavelength.min():.1f}–{ref.wavelength.max():.1f} nm)")

    targets = []
    for tok in args.target:
        name, path = _parse_named_path(tok)
        sp = load_spectrum(path, column=args.target_column)
        if sp.wavelength is None:
            sys.stderr.write(f"ERROR: target '{name}' has no wavelength axis.\n")
            return 1
        targets.append((name, sp))
        print(f"Target:    {name}  <- {sp.source}  "
              f"({sp.wavelength.min():.1f}–{sp.wavelength.max():.1f} nm)")

    # Common wavelength grid = overlap of all spectra, optionally clipped.
    lo = max([ref.wavelength.min()] + [s.wavelength.min() for _, s in targets])
    hi = min([ref.wavelength.max()] + [s.wavelength.max() for _, s in targets])
    if args.wl_min is not None:
        lo = max(lo, args.wl_min)
    if args.wl_max is not None:
        hi = min(hi, args.wl_max)
    if hi <= lo:
        sys.stderr.write("ERROR: no overlapping wavelength range.\n")
        return 1
    grid = np.arange(lo, hi + args.grid_step_nm, args.grid_step_nm)
    print(f"\nCommon grid: {lo:.1f}–{hi:.1f} nm, step {args.grid_step_nm} nm "
          f"({len(grid)} points)")

    # Reference on grid, peak-normalised.
    ref_g = _normalise_peak(np.interp(grid, ref.wavelength, ref.intensity))
    smooth_pts = max(1, int(round(args.smooth_nm / args.grid_step_nm)))
    valid_ref = ref_g >= args.min_rel
    if not valid_ref.any():
        sys.stderr.write("ERROR: reference below --min-rel everywhere.\n")
        return 1

    os.makedirs(args.out_dir, exist_ok=True)
    report = {"reference": {"name": ref_name, "source": ref.source},
              "grid_nm": [round(float(g), 4) for g in grid],
              "min_rel": args.min_rel, "smooth_nm": args.smooth_nm,
              "targets": {}}

    plot_series = []  # (name, sensitivity) for optional plotting

    for name, sp in targets:
        tgt_g = _normalise_peak(np.interp(grid, sp.wavelength, sp.intensity))

        # Relative sensitivity vs reference: where the device responds MORE
        # than the reference, sensitivity is high -> gain (1/sens) is low.
        with np.errstate(divide="ignore", invalid="ignore"):
            sens = tgt_g / ref_g
        # Smooth to tame division noise, then handle the dark (invalid) region.
        sens = _moving_average(sens, smooth_pts)

        valid = valid_ref & (tgt_g >= args.min_rel)
        if not valid.any():
            print(f"  [{name}] WARNING: no overlap above --min-rel; skipped.")
            continue

        # Normalise so the peak relative-sensitivity = 1.0 (drives gain = 1.0
        # there; build_response_gain clips sensitivity to <= 1.0 and boosts the
        # rest up toward the reference shape).
        smax = float(np.nanmax(sens[valid]))
        if smax > 0:
            sens = sens / smax
        # Where the source is too dark to characterise, leave the device
        # uncompensated (sensitivity 1.0 -> gain 1.0) instead of exploding.
        sens = np.where(valid, sens, 1.0)
        sens = np.clip(sens, args.min_rel, 1.0)

        table = np.column_stack([grid, sens])
        plot_series.append((name, sens.copy()))

        dev = args.device_map.get(name, name)
        n_valid = int(valid.sum())
        gain_max = float(np.max(1.0 / sens[valid]))
        print(f"  [{name}] characterised {n_valid}/{len(grid)} points; "
              f"max boost x{gain_max:.2f} "
              f"(device target: {DEVICE_RESPONSE_TARGET.get(dev, dev)})")

        # JSON table (config form: list of [nm, sensitivity]).
        json_table = [[round(float(w), 3), round(float(s), 6)]
                      for w, s in table]
        out_json = os.path.join(args.out_dir, f"{name}_response_table.json")
        with open(out_json, "w", encoding="utf-8") as f:
            json.dump({"device": dev, "reference": ref_name,
                       "source": sp.source,
                       "response_table": json_table}, f, indent=2)

        # Python snippet (paste form) — numpy array for PASCO-style constants.
        out_py = os.path.join(args.out_dir, f"{name}_response_table.py")
        with open(out_py, "w", encoding="utf-8") as f:
            f.write(f"# Spectral response table for {dev} "
                    f"(relative to {ref_name})\n")
            f.write(f"# source: {sp.source}\n")
            f.write("import numpy as np\n\n")
            f.write("RESPONSE_TABLE = np.array([\n")
            for w, s in table:
                f.write(f"    [{w:.2f}, {s:.6f}],\n")
            f.write("], dtype=np.float64)\n")

        report["targets"][name] = {
            "device": dev, "source": sp.source,
            "points_characterised": n_valid,
            "max_gain": gain_max,
            "files": {"json": out_json, "py": out_py},
        }
        print(f"           -> {out_json}")
        print(f"           -> {out_py}")

    with open(os.path.join(args.out_dir, "response_report.json"),
              "w", encoding="utf-8") as f:
        json.dump(report, f, indent=2)

    print("\nNote: response calibration is only physically meaningful when the "
          "SAME broadband source (e.g. halogen/white LED) illuminates all "
          "devices. Line lamps give signal only at discrete wavelengths.")
    print("Paste a JSON table into config as ocean_response_table (HDX), or a "
          "numpy RESPONSE_TABLE into the driver (PASCO).")

    if args.plot:
        _plot_response(grid, ref_g, plot_series, args.plot)

    return 0


# ══════════════════════════════════════════════════════════════════════════
# Optional plotting (matplotlib is an optional dependency)
# ══════════════════════════════════════════════════════════════════════════

def _plot_wavelength(px, nm, residuals, coeffs, pixel_count, path):
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except ImportError:
        print("(matplotlib not available — skipping --plot)")
        return
    full = poly_wavelength_array(coeffs, pixel_count)
    fig, (a, b) = plt.subplots(2, 1, figsize=(8, 7))
    a.plot(np.arange(pixel_count), full, "-", lw=1, label="fit")
    a.plot(px, nm, "o", ms=6, label="lines")
    a.set_xlabel("pixel"); a.set_ylabel("wavelength (nm)")
    a.legend(); a.set_title("Wavelength calibration fit")
    b.stem(nm, residuals)
    b.axhline(0, color="k", lw=0.5)
    b.set_xlabel("wavelength (nm)"); b.set_ylabel("residual (nm)")
    b.set_title("Fit residuals")
    fig.tight_layout(); fig.savefig(path, dpi=120)
    print(f"Saved plot -> {path}")


def _plot_response(grid, ref_g, series, path):
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except ImportError:
        print("(matplotlib not available — skipping --plot)")
        return
    fig, (a, b) = plt.subplots(2, 1, figsize=(8, 7))
    a.plot(grid, ref_g, "k-", lw=1.2, label="reference (norm)")
    for name, sens in series:
        a.plot(grid, sens, lw=1, label=f"{name} sensitivity")
    a.set_xlabel("wavelength (nm)"); a.set_ylabel("normalised")
    a.legend(); a.set_title("Reference & relative sensitivities")
    for name, sens in series:
        b.plot(grid, 1.0 / sens, lw=1, label=f"{name} gain")
    b.set_xlabel("wavelength (nm)"); b.set_ylabel("compensation gain (1/sens)")
    b.legend(); b.set_title("Resulting response-compensation gain")
    fig.tight_layout(); fig.savefig(path, dpi=120)
    print(f"Saved plot -> {path}")


# ══════════════════════════════════════════════════════════════════════════
# Argument parsing
# ══════════════════════════════════════════════════════════════════════════

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="make_calibration.py",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        description=__doc__)
    sub = p.add_subparsers(dest="cmd", required=True)

    # ── wavelength ────────────────────────────────────────────────────────
    w = sub.add_parser(
        "wavelength", help="fit pixel->nm polynomial from a lamp spectrum",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        description=(
            "Fit a pixel->wavelength polynomial from a measured emission-lamp\n"
            "spectrum. The lamp's known lines are located in the trace and a\n"
            "degree-N polynomial (default 3) is fitted, matching the PASCO\n"
            "factory convention lambda(i) = C0 + C1*i + C2*i^2 + C3*i^3.\n\n"
            "Example:\n"
            "  python make_calibration.py wavelength PS2600A_LR2T_spectrum_CD.csv \\\n"
            "      --lamp Cd --device pasco --plot fit.png"))
    w.add_argument("spectrum", help="measured lamp spectrum CSV")
    w.add_argument("--list-peaks", action="store_true", dest="list_peaks",
                   help="detect peaks and print their pixel/approx-nm/height, "
                        "then exit (use to read off positions for --pixels)")
    w.add_argument("--pixels",
                   help="explicit comma-separated pixel positions, paired in "
                        "ascending order with the lamp lines (or --lines). "
                        "When given, peak detection/matching is skipped and the "
                        "fit is fully deterministic — the reliable path.")
    w.add_argument("--lamp", default="Cd",
                   help="calibration lamp: Cd, Ne, Hg, Ar, Kr, Xe, He, Na, H "
                        "(default Cd). Ignored if --lines is given.")
    w.add_argument("--lines",
                   help="explicit comma-separated known line wavelengths (nm), "
                        "overrides --lamp, e.g. '467.8,480.0,508.6,643.8'")
    w.add_argument("--device", default="pasco",
                   choices=["pasco", "lr2t", "hdx", "generic"],
                   help="device the coeffs are for (affects output naming)")
    w.add_argument("--degree", type=int, default=3,
                   help="polynomial degree (default 3; use 2 for <4 lines)")
    w.add_argument("--column",
                   help="intensity column name for multi-column CSVs")
    w.add_argument("--prominence", type=float, default=None,
                   help="absolute peak prominence (ADC). Default: auto from "
                        "--prominence-frac of the dynamic range.")
    w.add_argument("--prominence-frac", type=float, default=0.05,
                   dest="prominence_frac",
                   help="auto prominence as a fraction of (max-min) "
                        "(default 0.05)")
    w.add_argument("--min-distance", type=int, default=8, dest="min_distance",
                   help="minimum peak separation in pixels (default 8)")
    w.add_argument("--match-window-nm", type=float, default=8.0,
                   dest="match_window_nm",
                   help="max nm between a known line and a detected peak to "
                        "accept the match (default 8.0). Widen this if the "
                        "starting axis is grossly off.")
    w.add_argument("--max-residual-nm", type=float, default=2.0,
                   dest="max_residual_nm",
                   help="iteratively drop the worst-fitting line until the max "
                        "residual is below this (nm); 0 disables (default 2.0)")
    w.add_argument("--pixel-count", type=int, default=None, dest="pixel_count",
                   help="device pixel count for the output array "
                        "(default: rows in the CSV)")
    w.add_argument("--out", help="output JSON path (default wl_poly_<device>.json)")
    w.add_argument("--save-spectrum", dest="save_spectrum",
                   help="also write the loaded spectrum to this CSV path")
    w.add_argument("--plot", help="write a fit+residual PNG to this path")
    w.set_defaults(func=cmd_wavelength)

    # ── response ──────────────────────────────────────────────────────────
    r = sub.add_parser(
        "response", help="cross-device gain calibration vs a reference",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        description=(
            "Build per-wavelength relative-sensitivity tables for one or more\n"
            "spectrometers against a reference spectrometer. Measure the SAME\n"
            "broadband source on every device; each spectrum is peak-normalised\n"
            "to 1.0 and divided by the reference. The result, fed to the\n"
            "driver's response compensation, makes the target devices reproduce\n"
            "the reference's spectral shape.\n\n"
            "Example (HDX is the reference):\n"
            "  python make_calibration.py response \\\n"
            "      --reference hdx=hdx_spectrum.csv \\\n"
            "      --target pasco=pasco_white.csv --target lr2t=lr2t_white.csv \\\n"
            "      --device pasco=pasco --device lr2t=lr2t --plot resp.png"))
    r.add_argument("--reference", required=True,
                   help="reference spectrum as name=path (e.g. hdx=hdx.csv)")
    r.add_argument("--target", action="append", required=True, default=[],
                   help="target spectrum as name=path; repeatable")
    r.add_argument("--device", action="append", default=[], dest="device_pairs",
                   help="map a target name to a device id as name=device "
                        "(e.g. pasco=pasco); repeatable. Affects output hints.")
    r.add_argument("--ref-column", dest="ref_column",
                   help="intensity column name if the reference CSV is multi-column")
    r.add_argument("--target-column", dest="target_column",
                   help="intensity column name if target CSVs are multi-column")
    r.add_argument("--grid-step-nm", type=float, default=2.0, dest="grid_step_nm",
                   help="output table wavelength step in nm (default 2.0)")
    r.add_argument("--smooth-nm", type=float, default=5.0, dest="smooth_nm",
                   help="moving-average window (nm) to denoise the ratio "
                        "(default 5.0)")
    r.add_argument("--min-rel", type=float, default=0.02, dest="min_rel",
                   help="ignore wavelengths where a normalised spectrum is "
                        "below this fraction of its peak (default 0.02). Below "
                        "it the device is left uncompensated (gain 1.0).")
    r.add_argument("--wl-min", type=float, default=None, dest="wl_min",
                   help="clip output to >= this wavelength (nm)")
    r.add_argument("--wl-max", type=float, default=None, dest="wl_max",
                   help="clip output to <= this wavelength (nm)")
    r.add_argument("--out-dir", default="response_cal", dest="out_dir",
                   help="output directory (default response_cal/)")
    r.add_argument("--plot", help="write a sensitivity+gain PNG to this path")
    r.set_defaults(func=cmd_response)

    return p


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    if getattr(args, "cmd", None) == "response":
        args.device_map = dict(_parse_named_path(d) for d in args.device_pairs)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())

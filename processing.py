"""
Pure-Python processing functions used by both the PyQt6 desktop GUI and
the web server. No Qt or framework dependencies — just numpy + scipy.

Keeping this in one place ensures the desktop and web frontends produce
bit-identical processed spectra.
"""
from __future__ import annotations
import numpy as np
from scipy.signal import savgol_filter, find_peaks


# ──────────────────────────────────────────────────────────────────────
# Dark-current correction
# ──────────────────────────────────────────────────────────────────────
def apply_dark_correction(
    pixels: np.ndarray, *,
    mode: str,                 # "optical_black" | "parametric"
    ob_mean: float,
    integration_us: int,
    ob_slope: float,
    ob_offset: float,
    bias_adc: float,
    rate_adc_per_s: float,
) -> np.ndarray:
    """Subtract dark current from a raw pixel array (positive output)."""
    if mode == "optical_black":
        dark = ob_slope * ob_mean + ob_offset
    else:                                       # parametric
        dark = bias_adc + rate_adc_per_s * integration_us / 1_000_000.0
    return np.maximum(0.0, pixels - dark)


DARK_MODE_STORED = "stored"


def dark_estimate(
    *, mode: str, ob_mean: float, integration_us: int, ob_slope: float,
    ob_offset: float, bias_adc: float, rate_adc_per_s: float,
    stored_dark: np.ndarray | None = None,
):
    """Dark level to subtract: per-pixel array for mode 'stored' (when a stored
    dark at this t_int exists), else the scalar of the optical-black /
    parametric model. Returns (dark, source) with source in
    {'stored', 'optical_black', 'parametric'}. A missing stored dark falls back
    to the optical-black/baseline estimate."""
    if mode == DARK_MODE_STORED and stored_dark is not None:
        return np.asarray(stored_dark, dtype=float), DARK_MODE_STORED
    if mode == "parametric":
        return bias_adc + rate_adc_per_s * integration_us / 1_000_000.0, "parametric"
    return ob_slope * ob_mean + ob_offset, "optical_black"


def to_count_rate(net_counts: np.ndarray, integration_us: int) -> np.ndarray:
    """counts → counts/s (dark must already be subtracted)."""
    t = max(int(integration_us), 1) / 1_000_000.0
    return np.asarray(net_counts, dtype=float) / t


# ──────────────────────────────────────────────────────────────────────
# Hot-pixel / median despeckle
# ──────────────────────────────────────────────────────────────────────
def apply_despeckle(pixels: np.ndarray, window: int) -> np.ndarray:
    """Sliding-median filter. Window will be forced odd."""
    w = max(3, int(window))
    if w % 2 == 0:
        w += 1
    pad = w // 2
    padded = np.pad(pixels, (pad, pad), mode="edge")
    windows = np.lib.stride_tricks.sliding_window_view(padded, w)
    return np.median(windows, axis=1)


# ──────────────────────────────────────────────────────────────────────
# Spatial (pixel-window) smoothing
# ──────────────────────────────────────────────────────────────────────
def apply_spatial_smoothing(
    pixels: np.ndarray, width: int, kind: str = "boxcar"
) -> np.ndarray:
    """Smooth the trace along the wavelength axis over a window of pixels.

    Unlike the temporal filter (fusion.py, random frame-to-frame noise) and the
    despeckle median (hot-pixel rejection), this is a plain neighbourhood
    average that visually de-noises the displayed spectrum — including the
    reproducible fixed-pattern structure temporal smoothing cannot touch.

    `width` is forced odd and >= 3. Wider windows give a smoother trace but
    broaden and lower real peaks (resolution loss), so keep the window well
    below the narrowest peak's FWHM.

    kind:
      "boxcar"  — uniform moving average (default).
      "savgol"  — Savitzky-Golay (preserves peak height/width better at a
                  given window; falls back to boxcar if the fit fails).
    """
    w = max(3, int(width))
    if w % 2 == 0:
        w += 1
    if pixels.size < w:
        return pixels

    if kind == "savgol":
        order = min(2, w - 1)
        try:
            return savgol_filter(pixels, window_length=w, polyorder=order)
        except Exception:
            pass  # fall through to boxcar

    pad = w // 2
    padded = np.pad(pixels, (pad, pad), mode="edge")
    kernel = np.ones(w, dtype=float) / w
    return np.convolve(padded, kernel, mode="valid")


# ──────────────────────────────────────────────────────────────────────
# Spectral-response compensation (TCD1304AP QE inverse)
# ──────────────────────────────────────────────────────────────────────
def apply_response_compensation(pixels: np.ndarray, gain: np.ndarray) -> np.ndarray:
    """Multiply by the per-pixel inverse-QE gain; clip negatives."""
    return np.clip(pixels * gain, 0.0, None)


# ──────────────────────────────────────────────────────────────────────
# Peak detection — Savitzky-Golay smoothing + prominence filtering
# ──────────────────────────────────────────────────────────────────────
def detect_peaks(
    pixels: np.ndarray,
    wavelengths: np.ndarray,
    *,
    savgol_window: int = 11,
    savgol_order: int = 3,
    min_prominence: float = 50.0,
    min_distance_px: int = 40,
    min_height: float = 15.0,
    max_count: int = 3,
) -> list[dict]:
    """
    Returns a list of detected peaks sorted by ascending wavelength.
    Each peak is `{"index": int, "wavelength_nm": float, "intensity_adc": float,
                   "prominence": float}`.
    """
    if pixels.size < 6:
        return []
    w = max(5, int(savgol_window))
    if w % 2 == 0:
        w += 1
    order = max(1, min(int(savgol_order), w - 1))
    try:
        smooth = savgol_filter(pixels, window_length=w, polyorder=order)
    except Exception:
        smooth = pixels

    idx, props = find_peaks(
        smooth,
        prominence=float(min_prominence),
        distance=int(min_distance_px),
        height=float(min_height),
    )
    if idx.size == 0:
        return []

    proms = props.get("prominences", np.ones_like(idx, dtype=float))
    order_by_prom = np.argsort(proms)[::-1]
    chosen = idx[order_by_prom[: int(max_count)]]
    chosen.sort()

    return [
        {
            "index":         int(i),
            "wavelength_nm": float(wavelengths[i]),
            "intensity_adc": float(pixels[i]),
            "prominence":    float(smooth[i]),
        }
        for i in chosen
    ]


# ──────────────────────────────────────────────────────────────────────
# All-in-one helper used by the web server
# ──────────────────────────────────────────────────────────────────────
def process_frame(
    pixels: np.ndarray,
    ob_mean: float,
    integration_us: int,
    *,
    wavelengths: np.ndarray,
    response_gain: np.ndarray,
    ob_slope: float,
    ob_offset: float,
    # Toggles & params (typically pulled from the live config)
    dark_correction: bool = False,
    dark_mode: str = "optical_black",
    dark_bias_adc: float = 0.0,
    dark_rate_adc_per_s: float = 0.0,
    hot_pixel_filter: bool = False,
    smoothing_width: int = 5,
    spatial_smoothing: bool = False,
    spatial_smoothing_width: int = 5,
    response_compensation: bool = False,
    peak_detection: bool = False,
    peak_params: dict | None = None,
    # Spectral calibration v2 (all default to the unchanged legacy behaviour)
    calibration=None,               # calibration_profiles.CalibrationApplier | None
    stored_dark: np.ndarray | None = None,
    stored_dark_source: str = "stored",   # "stored" | "stored_model" (reporting)
    display_unit: str = "counts",   # "counts" | "counts_per_s" (no profile)
) -> dict:
    """Apply the full processing pipeline. Returns a dict containing the
    processed pixel array plus any computed peaks, the y unit and the dark
    source actually used.

    Legacy path (no v2 profile, display_unit 'counts', dark mode not 'stored'):
    bit-identical to the previous implementation.

    Calibrated path (v2 profile active): dark subtraction is mandatory and NOT
    clipped (the configured dark mode; 'stored' = per-pixel dark at this t_int),
    then counts/s, despeckle/smoothing, then × factor (1/S or K) and NaN outside
    the profile's valid range. The legacy response gain is not applied."""
    arr = np.asarray(pixels, dtype=float).copy()
    unit = "counts"
    dark_source = None

    calibrated = calibration is not None
    rate_mode = calibrated or display_unit == "counts_per_s"
    new_dark = (dark_mode == DARK_MODE_STORED)

    if calibrated or (dark_correction and new_dark):
        dark, dark_source = dark_estimate(
            mode=dark_mode, ob_mean=ob_mean, integration_us=integration_us,
            ob_slope=ob_slope, ob_offset=ob_offset, bias_adc=dark_bias_adc,
            rate_adc_per_s=dark_rate_adc_per_s, stored_dark=stored_dark)
        if dark_source == DARK_MODE_STORED:
            dark_source = stored_dark_source
        arr = arr - dark
        if not calibrated:
            arr = np.maximum(0.0, arr)       # display path keeps the old clip
    elif dark_correction:
        arr = apply_dark_correction(
            arr, mode=dark_mode, ob_mean=ob_mean,
            integration_us=integration_us,
            ob_slope=ob_slope, ob_offset=ob_offset,
            bias_adc=dark_bias_adc,
            rate_adc_per_s=dark_rate_adc_per_s,
        )
        dark_source = dark_mode
    if rate_mode:
        arr = to_count_rate(arr, integration_us)
        unit = "counts/s"
    if hot_pixel_filter:
        arr = apply_despeckle(arr, smoothing_width)
    if spatial_smoothing:
        arr = apply_spatial_smoothing(arr, spatial_smoothing_width)
    # Dark-corrected, response-UNcorrected signal (what the wavelength wizard
    # averages); additive output, does not change "pixels".
    pre_response = arr.copy()
    if calibrated:
        arr = calibration.apply(arr)
        unit = calibration.unit
    elif response_compensation:
        arr = apply_response_compensation(arr, response_gain)

    peaks: list[dict] = []
    if peak_detection:
        p = peak_params or {}
        peaks = detect_peaks(
            np.nan_to_num(arr, nan=0.0) if calibrated else arr, wavelengths,
            savgol_window  = p.get("peak_savgol_window", 11),
            savgol_order   = p.get("peak_savgol_order", 3),
            min_prominence = p.get("peak_prominence", 50.0),
            min_distance_px= p.get("peak_min_distance_px", 40),
            min_height     = p.get("peak_min_height", 15.0),
            max_count      = p.get("peak_max_count", 3),
        )

    return {"pixels": arr, "peaks": peaks, "unit": unit, "dark_source": dark_source,
            "pre_response": pre_response}


def pipeline_kwargs(cfg_get, runtime, *, integration_us: int, ob_slope: float,
                    ob_offset: float, default_bias: float = 0.0,
                    default_rate: float = 0.0) -> dict:
    """The keyword arguments of process_frame(), built from the live config and
    the active CalibrationRuntime. Desktop AND web call this with the same
    config, runtime and OB constants, so both run process_frame() with identical
    inputs → identical output (plan: bit-identical desktop/web).

    cfg_get: Config.get-like callable. runtime: calibration_profiles
    .CalibrationRuntime (or None). OB constants: the device's OB model (PASCO)
    or (1, 0) for devices without optical-black pixels."""
    dark_mode = cfg_get("dark_mode", "optical_black")
    applier = getattr(runtime, "applier", None)
    legacy = getattr(runtime, "legacy_gain", None)
    stored = None
    stored_src = DARK_MODE_STORED
    # With an active v2 profile a PER-PIXEL dark is always used (the scalar
    # optical-black / parametric estimates bias the weak blue end badly and are
    # not t_int-consistent). Exact stored dark, else per-pixel b + r·t model;
    # only if neither exists the configured scalar mode is the fallback (and
    # the runtime status carries a warning).
    if runtime is not None and (applier is not None or dark_mode == DARK_MODE_STORED):
        stored, src = runtime.dark_for(integration_us)
        if stored is not None:
            stored_src = src
            if applier is not None:
                dark_mode = DARK_MODE_STORED
    return dict(
        response_gain=(legacy if legacy is not None else 1.0),
        ob_slope=float(ob_slope),
        ob_offset=float(ob_offset),
        dark_correction=bool(cfg_get("dark_correction", False)),
        dark_mode=dark_mode,
        dark_bias_adc=float(cfg_get("dark_bias_adc", default_bias)),
        dark_rate_adc_per_s=float(cfg_get("dark_rate_adc_per_s", default_rate)),
        hot_pixel_filter=bool(cfg_get("hot_pixel_filter", False)),
        smoothing_width=int(cfg_get("smoothing_width", 5)),
        spatial_smoothing=bool(cfg_get("spatial_smoothing", False)),
        spatial_smoothing_width=int(cfg_get("spatial_smoothing_width", 5)),
        response_compensation=legacy is not None,
        peak_detection=bool(cfg_get("show_peaks", False)),
        peak_params={k: cfg_get(k) for k in (
            "peak_savgol_window", "peak_savgol_order", "peak_prominence",
            "peak_min_distance_px", "peak_min_height", "peak_max_count")
            if cfg_get(k) is not None},
        calibration=applier,
        stored_dark=stored,
        stored_dark_source=stored_src,
        display_unit=str(cfg_get("display_unit", "counts")),
    )


def needs_shared_pipeline(cfg_get, runtime) -> bool:
    """True when a frame must go through process_frame() with the v2 features
    (active v2 profile, counts/s display, or the stored-dark mode)."""
    return bool(getattr(runtime, "active_v2", False)
                or cfg_get("display_unit", "counts") == "counts_per_s"
                or (cfg_get("dark_correction", False)
                    and cfg_get("dark_mode", "optical_black") == DARK_MODE_STORED))


# ──────────────────────────────────────────────────────────────────────
# Units / photometry helpers (shared desktop + web)
# ──────────────────────────────────────────────────────────────────────
UNIT_LABELS = {
    "counts": "counts",
    "counts/s": "counts/s",
    "relative": "relative",
    "uW/cm2/nm": "µW/(cm²·nm)",
    "W/m2/nm": "W/(m²·nm)",
    "mW/m2/nm": "mW/(m²·nm)",
}

# factor to W/(m²·nm) for the absolute units
_TO_W_M2_NM = {"uW/cm2/nm": 0.01, "W/m2/nm": 1.0, "mW/m2/nm": 1e-3}


def unit_label(unit: str) -> str:
    return UNIT_LABELS.get(unit, unit)


def is_absolute_unit(unit: str) -> bool:
    return unit in _TO_W_M2_NM


def photometric_inputs(values: np.ndarray, unit: str):
    """Spectrum + lux calibration for color_science.measure_all().

    Masked (NaN) pixels → 0. For an absolute unit the spectrum is returned in
    W/(m²·nm) and lux_calibration = 1.0, so illuminance_lux() gives true lux
    directly from E(λ) (radiometric_calibration_factor is then ignored). For
    any other unit returns lux_calibration None (= use the configured factor)."""
    v = np.nan_to_num(np.asarray(values, dtype=float), nan=0.0,
                      posinf=0.0, neginf=0.0)
    if is_absolute_unit(unit):
        return v * _TO_W_M2_NM[unit], 1.0
    return v, None


# ──────────────────────────────────────────────────────────────────────
# Display modes (Darstellungsarten) — shared desktop + web + API
# ──────────────────────────────────────────────────────────────────────
#
# The pipeline result (process_frame "pixels", unit) is the CORRECTED data
# every evaluation uses (CIE, CRI, TM-30, filter, exports' base values). The
# display mode only changes what is plotted:
#
#   raw          Raw ADC — counts without calibration profile (dark correction
#                and smoothing as configured; identical to the legacy display
#                when no profile is active).
#   corrected    Corrected counts (default with an active profile):
#                corrected rate × current t_int, normalised so that in the
#                pixel of maximum sensitivity (smallest correction factor)
#                Corrected = Raw (net counts). Keeps the familiar ADC scale.
#   normalised   max = 1 over the valid (finite) range.
#   count_rate   counts/s (with a profile: corrected rate on the same
#                normalisation as "corrected").
#   absolute     irradiance (only with an absolute profile).
#   auto         corrected with an active v2 profile, else count_rate when the
#                legacy display_unit is counts_per_s, else raw.
DISPLAY_AUTO = "auto"
DISPLAY_RAW = "raw"
DISPLAY_CORRECTED = "corrected"
DISPLAY_NORMALISED = "normalised"
DISPLAY_RATE = "count_rate"
DISPLAY_ABSOLUTE = "absolute"
DISPLAY_MODES = (DISPLAY_RAW, DISPLAY_CORRECTED, DISPLAY_NORMALISED, DISPLAY_RATE,
                 DISPLAY_ABSOLUTE)
DISPLAY_LABELS = {
    DISPLAY_AUTO: "Auto",
    DISPLAY_RAW: "Raw ADC",
    DISPLAY_CORRECTED: "Corrected counts",
    DISPLAY_NORMALISED: "Normalised",
    DISPLAY_RATE: "Count rate",
    DISPLAY_ABSOLUTE: "Absolute",
}
UNIT_COUNTS_CORRECTED = "counts_corrected"
UNIT_NORMALISED = "normalised"
UNIT_LABELS.update({UNIT_COUNTS_CORRECTED: "counts (corrected)",
                    UNIT_NORMALISED: "normalised"})


def available_display_modes(profile_type: str | None) -> list[str]:
    """Modes selectable now. profile_type: 'relative' | 'absolute' | None
    (no v2 profile active)."""
    if profile_type is None:
        return [DISPLAY_RAW, DISPLAY_NORMALISED, DISPLAY_RATE]
    modes = [DISPLAY_RAW, DISPLAY_CORRECTED, DISPLAY_NORMALISED, DISPLAY_RATE]
    if profile_type == "absolute":
        modes.append(DISPLAY_ABSOLUTE)
    return modes


def effective_display_mode(requested: str | None, profile_type: str | None,
                           legacy_display_unit: str = "counts") -> str:
    """The mode actually shown: 'auto' and modes that are not available with
    the current profile fall back (absolute → corrected → raw)."""
    req = (requested or DISPLAY_AUTO).strip().lower()
    avail = available_display_modes(profile_type)
    if req == DISPLAY_AUTO or req not in DISPLAY_MODES:
        if profile_type is not None:
            return DISPLAY_CORRECTED
        return DISPLAY_RATE if legacy_display_unit == "counts_per_s" else DISPLAY_RAW
    if req in avail:
        return req
    if req == DISPLAY_ABSOLUTE and profile_type is not None:
        return DISPLAY_CORRECTED
    return DISPLAY_RAW


def display_transform(mode: str, *, values: np.ndarray, unit: str,
                      integration_us: int, calibration=None,
                      raw_counts: np.ndarray | None = None) -> tuple[np.ndarray, str]:
    """(display values, display unit) for an EFFECTIVE mode.

    values/unit: process_frame result (corrected data). calibration: the active
    CalibrationApplier (None without v2 profile). raw_counts: the uncalibrated
    counts (needed for 'raw' when a profile is active; else values are used)."""
    v = np.asarray(values, float)
    t = max(int(integration_us), 1) / 1_000_000.0
    if calibration is not None:
        norm = float(getattr(calibration, "counts_norm", 1.0) or 1.0)
        if mode == DISPLAY_RAW:
            base = v if raw_counts is None else np.asarray(raw_counts, float)
            return base, "counts"
        if mode == DISPLAY_ABSOLUTE and getattr(calibration, "type", "") == "absolute":
            return v, unit
        rate = v * norm                    # corrected count rate (counts/s)
        if mode == DISPLAY_RATE:
            return rate, "counts/s"
        if mode == DISPLAY_NORMALISED:
            return normalise(rate), UNIT_NORMALISED
        return rate * t, UNIT_COUNTS_CORRECTED
    # no v2 profile: values are counts or counts/s (legacy pipeline)
    if mode == DISPLAY_RATE:
        return (v if unit == "counts/s" else v / t), "counts/s"
    if mode == DISPLAY_NORMALISED:
        return normalise(v), UNIT_NORMALISED
    if mode == DISPLAY_RAW and unit == "counts/s":
        return v * t, "counts"
    return v, unit


def normalise(values: np.ndarray) -> np.ndarray:
    """max = 1 over the finite (valid) values; all-NaN / non-positive → as is."""
    v = np.asarray(values, float)
    fin = np.isfinite(v)
    if not np.any(fin):
        return v
    mx = float(np.max(v[fin]))
    return v / mx if mx > 0 else v


def exposure_level(raw_averaged, *, full_scale: float, saturation_level: float) -> dict:
    """Detector drive level in ADC (independent of the display mode):
    peak counts, % of full scale, saturated pixel count."""
    r = np.asarray(raw_averaged, float)
    fin = r[np.isfinite(r)]
    peak = float(np.max(fin)) if fin.size else 0.0
    fs = float(full_scale) if full_scale else 0.0
    return {"peak_counts": peak,
            "peak_pct_fs": (100.0 * peak / fs) if fs > 0 else None,
            "full_scale": fs, "saturation_level": float(saturation_level),
            "n_saturated": int(np.count_nonzero(fin >= float(saturation_level)))
            if saturation_level else 0,
            "saturated": bool(saturation_level and peak >= float(saturation_level))}


def format_value(v, unit: str) -> str:
    """Readout text with the active unit; '–' for masked / invalid pixels."""
    try:
        x = float(v)
    except (TypeError, ValueError):
        return "–"
    if not np.isfinite(x):
        return "–"
    label = unit_label(unit)
    if unit in ("counts", UNIT_COUNTS_CORRECTED):
        return f"{x:.0f} {label}"
    if unit == UNIT_NORMALISED:
        return f"{x:.4f}"
    return f"{x:.4g} {label}"

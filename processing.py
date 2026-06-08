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
) -> dict:
    """Apply the full processing pipeline. Returns a dict containing the
    processed pixel array plus any computed peaks."""
    arr = np.asarray(pixels, dtype=float).copy()

    if dark_correction:
        arr = apply_dark_correction(
            arr, mode=dark_mode, ob_mean=ob_mean,
            integration_us=integration_us,
            ob_slope=ob_slope, ob_offset=ob_offset,
            bias_adc=dark_bias_adc,
            rate_adc_per_s=dark_rate_adc_per_s,
        )
    if hot_pixel_filter:
        arr = apply_despeckle(arr, smoothing_width)
    if spatial_smoothing:
        arr = apply_spatial_smoothing(arr, spatial_smoothing_width)
    if response_compensation:
        arr = apply_response_compensation(arr, response_gain)

    peaks: list[dict] = []
    if peak_detection:
        p = peak_params or {}
        peaks = detect_peaks(
            arr, wavelengths,
            savgol_window  = p.get("peak_savgol_window", 11),
            savgol_order   = p.get("peak_savgol_order", 3),
            min_prominence = p.get("peak_prominence", 50.0),
            min_distance_px= p.get("peak_min_distance_px", 40),
            min_height     = p.get("peak_min_height", 15.0),
            max_count      = p.get("peak_max_count", 3),
        )

    return {"pixels": arr, "peaks": peaks}

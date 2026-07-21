"""
calibration_utils.py — Generic wavelength calibration utilities

Device-agnostic helpers for pixel-to-wavelength mapping.
Each device driver imports what it needs; no device-specific code here.

Polynomial convention
---------------------
All polynomials use the form:
    λ(i) = C[0] + C[1]*i + C[2]*i² + C[3]*i³  (degree 3)
where i is the pixel index (0-based) and λ is wavelength in nm.

This matches the PASCO factory calibration format and can be used for
any spectrometer with a polynomial pixel-to-wavelength mapping.
"""

from __future__ import annotations

import numpy as np
from typing import Sequence


# ── Polynomial wavelength mapping ─────────────────────────────────────────

def poly_wavelength_array(
    coeffs: Sequence[float],
    pixel_count: int,
) -> np.ndarray:
    """
    Compute per-pixel wavelengths from a polynomial calibration.

    Parameters
    ----------
    coeffs : [C0, C1, C2, C3]
        Polynomial coefficients, ascending degree.
        λ(i) = C0 + C1·i + C2·i² + C3·i³
    pixel_count : int
        Number of pixels.

    Returns
    -------
    wavelengths : np.ndarray, shape (pixel_count,)
        Wavelength in nm for each pixel index.

    Examples
    --------
    >>> # PASCO PS-2600A factory calibration
    >>> coeffs = [75.45192816, 0.345838363, -2.33680103e-05, 1.22593755e-09]
    >>> wl = poly_wavelength_array(coeffs, 3648)
    >>> print(f"{wl[0]:.1f} – {wl[-1]:.1f} nm")
    75.5 – 1335.4 nm
    """
    idx = np.arange(pixel_count, dtype=float)
    # np.polyval uses descending-degree order → reverse coeffs
    return np.polyval(list(reversed(coeffs)), idx)


def apply_wl_offset(
    wavelengths: np.ndarray,
    offset_nm: float,
) -> np.ndarray:
    """
    Shift a wavelength array by a fixed offset (nm).
    Positive offset shifts spectrum toward red (longer wavelengths).

    Used for coarse inter-unit calibration when per-pixel
    calibration data is unavailable but a systematic offset is known
    from comparison against a reference source.
    """
    if offset_nm == 0.0:
        return wavelengths
    return wavelengths + offset_nm


# ── Polynomial fitting from known emission lines ──────────────────────────

def fit_wavelength_polynomial(
    pixel_positions: Sequence[float],
    wavelengths_nm: Sequence[float],
    degree: int = 3,
    pixel_count: int | None = None,
) -> tuple[list[float], np.ndarray]:
    """
    Fit a polynomial calibration curve to measured emission-line positions.

    Parameters
    ----------
    pixel_positions : list[float]
        Sub-pixel positions of known emission lines, e.g. from peak finding.
        Can be fractional pixels.
    wavelengths_nm : list[float]
        Known wavelengths (nm) for each entry in pixel_positions.
        Must be the same length as pixel_positions.
    degree : int, optional
        Polynomial degree (default 3). Use 3 for most spectrometers.
        Reduce to 2 if fewer than 4 reference lines are available.
    pixel_count : int, optional
        If provided, also returns the full per-pixel wavelength array.

    Returns
    -------
    coeffs : list[float]
        Polynomial coefficients [C0, C1, C2, ...], ascending degree.
    residuals_nm : np.ndarray
        Fit residuals at each calibration point (nm).
        Inspect these to verify calibration quality; typical values < 0.5 nm.

    Raises
    ------
    ValueError
        If fewer reference points are provided than (degree + 1).

    Examples
    --------
    >>> # Calibrate LR-2T against Neon lines
    >>> pixels    = [412.3, 876.1, 1203.5, 2441.8]   # peak pixel positions
    >>> known_nm  = [585.2, 640.2, 667.8, 703.2]      # Ne line wavelengths
    >>> coeffs, resid = fit_wavelength_polynomial(pixels, known_nm, degree=3)
    >>> print(f"Max residual: {max(abs(resid)):.3f} nm")
    """
    px = np.array(pixel_positions, dtype=float)
    wl = np.array(wavelengths_nm, dtype=float)

    if len(px) != len(wl):
        raise ValueError(
            f"pixel_positions ({len(px)}) and wavelengths_nm ({len(wl)}) "
            "must have the same length."
        )
    if len(px) < degree + 1:
        raise ValueError(
            f"Need at least {degree + 1} reference points for a degree-{degree} "
            f"polynomial; got {len(px)}. Reduce degree or add more lines."
        )

    # np.polyfit returns descending-degree coefficients
    poly_desc = np.polyfit(px, wl, degree)
    # Convert to ascending-degree [C0, C1, C2, ...]
    coeffs = list(reversed(poly_desc.tolist()))

    # Residuals at calibration points
    predicted  = np.polyval(poly_desc, px)
    residuals  = wl - predicted

    return coeffs, residuals


def wavelength_to_pixel(
    wavelength_nm: float,
    wavelength_array: np.ndarray,
) -> float:
    """
    Find the (interpolated) pixel index for a given wavelength.

    Useful for mapping known line wavelengths to pixel positions when
    you know the current approximate calibration but want to refine it.

    Returns fractional pixel index via linear interpolation.
    Returns NaN if the wavelength is outside the array range.
    """
    if (wavelength_nm < wavelength_array[0]
            or wavelength_nm > wavelength_array[-1]):
        return float("nan")
    return float(np.interp(
        wavelength_nm,
        wavelength_array,
        np.arange(len(wavelength_array), dtype=float),
    ))


# ── Spectral response compensation ────────────────────────────────────────

def build_response_gain(
    wavelength_array: np.ndarray,
    response_table: np.ndarray,
    min_floor: float = 0.001,
    max_gain:  float = 10.0,
) -> np.ndarray:
    """
    Build a per-pixel multiplicative gain array from a spectral response table.

    Parameters
    ----------
    wavelength_array : np.ndarray
        Per-pixel wavelengths in nm.
    response_table : np.ndarray, shape (N, 2)
        Columns: [wavelength_nm, relative_sensitivity].
        Relative sensitivity is normalised to 1.0 at peak.
    min_floor : float
        Minimum sensitivity before gain is applied.
        Prevents division by near-zero in UV/NIR extremes.
    max_gain : float
        Maximum multiplicative gain allowed.
        Prevents noise explosion at very low sensitivity wavelengths.

    Returns
    -------
    gain : np.ndarray
        Per-pixel gain: multiply raw ADC values by this to compensate.
        1.0 = no correction at the peak sensitivity wavelength.
    """
    # OUTSIDE the table's measured wavelength range there is no calibration
    # information, so the gain must be 1.0 (no correction) — NOT max_gain.
    # Filling with min_floor here would set gain = 1/min_floor = max_gain across
    # every uncovered pixel, amplifying the bare noise floor (e.g. the whole red
    # region for a banded source whose table only spans the visible bands).
    sensitivity = np.clip(
        np.interp(
            wavelength_array,
            response_table[:, 0],
            response_table[:, 1],
            left=1.0,
            right=1.0,
        ),
        min_floor,
        1.0,
    )
    return np.minimum(1.0 / sensitivity, max_gain)


def axis_is_monotonic(wl, min_nm: float = 50.0, max_nm: float = 1400.0) -> bool:
    """True if `wl` is a strictly increasing, plausibly-ranged wavelength axis.
    A saved calibration polynomial can turn over at the array ends (a degree-3
    fit through a limited line span), which folds the plotted trace back on
    itself (duplicate x → 'double y values') and corrupts every wavelength-based
    calculation. Drivers use this to reject such an axis on load and fall back to
    the factory/default one."""
    import numpy as _np
    wl = _np.asarray(wl, dtype=float)
    if wl.size < 2:
        return False
    if not _np.all(_np.diff(wl) > 0):
        return False
    if wl[0] < min_nm or wl[-1] > max_nm:
        return False
    return True

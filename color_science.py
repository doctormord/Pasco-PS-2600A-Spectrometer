"""
Color science: CIE 1931 chromaticity, CCT, Duv, Planckian / spectrum locus,
and CRI Ra (CIE 13.3-1995 method).

All routines accept (wavelengths_nm, intensities) as numpy arrays and
resample internally onto the CIE 5 nm grid (380…780 nm).
"""

from __future__ import annotations
import numpy as np


# ════════════════════════════════════════════════════════════
# CIE 1931 2° standard colorimetric observer (10 nm steps)
# ════════════════════════════════════════════════════════════
CIE_WL = np.arange(380, 781, 10, dtype=float)

# x̄(λ), ȳ(λ), z̄(λ) — CIE 1931 2° observer, standard table
_X_BAR_10 = np.array([
    0.0014, 0.0042, 0.0143, 0.0435, 0.1344, 0.2839, 0.3483, 0.3362,
    0.2908, 0.1954, 0.0956, 0.0320, 0.0049, 0.0093, 0.0633, 0.1655,
    0.2904, 0.4334, 0.5945, 0.7621, 0.9163, 1.0263, 1.0622, 1.0026,
    0.8544, 0.6424, 0.4479, 0.2835, 0.1649, 0.0874, 0.0468, 0.0227,
    0.0114, 0.0058, 0.0029, 0.0014, 0.0007, 0.0003, 0.0002, 0.0001,
    0.0000,
])
_Y_BAR_10 = np.array([
    0.0000, 0.0001, 0.0004, 0.0012, 0.0040, 0.0116, 0.0230, 0.0380,
    0.0600, 0.0910, 0.1390, 0.2080, 0.3230, 0.5030, 0.7100, 0.8620,
    0.9540, 0.9950, 0.9950, 0.9520, 0.8700, 0.7570, 0.6310, 0.5030,
    0.3810, 0.2650, 0.1750, 0.1070, 0.0610, 0.0320, 0.0170, 0.0082,
    0.0041, 0.0021, 0.0010, 0.0005, 0.0002, 0.0001, 0.0001, 0.0000,
    0.0000,
])
_Z_BAR_10 = np.array([
    0.0065, 0.0201, 0.0679, 0.2074, 0.6456, 1.3856, 1.7471, 1.7721,
    1.6692, 1.2876, 0.8130, 0.4652, 0.2720, 0.1582, 0.0782, 0.0422,
    0.0203, 0.0087, 0.0039, 0.0021, 0.0017, 0.0011, 0.0008, 0.0003,
    0.0002, 0.0000, 0.0000, 0.0000, 0.0000, 0.0000, 0.0000, 0.0000,
    0.0000, 0.0000, 0.0000, 0.0000, 0.0000, 0.0000, 0.0000, 0.0000,
    0.0000,
])

# Upsample to 5 nm for accuracy
CIE_WL_5 = np.arange(380, 781, 5, dtype=float)
X_BAR = np.interp(CIE_WL_5, CIE_WL, _X_BAR_10)
Y_BAR = np.interp(CIE_WL_5, CIE_WL, _Y_BAR_10)
Z_BAR = np.interp(CIE_WL_5, CIE_WL, _Z_BAR_10)


# ════════════════════════════════════════════════════════════
# CIE 13.3-1995 test color samples (TCS01–TCS15, 20 nm steps)
#   TCS01-08 → general Ra (8-sample mean)
#   TCS09    → strong red       (R9 — quality indicator)
#   TCS10    → strong yellow
#   TCS11    → strong green
#   TCS12    → strong blue
#   TCS13    → light yellowish pink (Caucasian complexion)
#   TCS14    → moderate olive green  (leaf)
#   TCS15    → light yellowish pink  (Asian complexion, CIE-S 010/E)
# Values are spectral reflectance (0-1). Resampled to 5 nm internally.
# ════════════════════════════════════════════════════════════
TCS_WL_20 = np.arange(380, 781, 20, dtype=float)
_TCS_20 = np.array([
    # 380   400   420   440   460   480   500   520   540   560   580   600   620   640   660   680   700   720   740   760   780
    [.116, .117, .124, .135, .144, .151, .158, .165, .184, .213, .245, .291, .345, .398, .420, .444, .476, .510, .554, .591, .616],  # TCS01
    [.053, .054, .059, .073, .090, .107, .116, .158, .230, .270, .302, .330, .358, .408, .435, .469, .504, .527, .534, .541, .555],  # TCS02
    [.058, .061, .062, .078, .097, .156, .250, .402, .482, .490, .475, .439, .355, .265, .197, .157, .140, .124, .119, .115, .115],  # TCS03
    [.057, .061, .061, .063, .075, .110, .180, .297, .408, .413, .339, .245, .181, .146, .130, .124, .123, .124, .122, .121, .118],  # TCS04
    [.144, .166, .198, .247, .291, .342, .342, .345, .314, .272, .232, .190, .167, .156, .146, .144, .146, .150, .158, .166, .176],  # TCS05
    [.136, .156, .198, .279, .354, .421, .420, .379, .318, .246, .196, .166, .146, .139, .137, .135, .135, .137, .137, .139, .139],  # TCS06
    [.213, .246, .319, .430, .443, .398, .328, .252, .196, .179, .187, .213, .260, .293, .293, .279, .295, .343, .402, .434, .451],  # TCS07
    [.150, .187, .246, .319, .337, .296, .249, .215, .205, .225, .265, .339, .422, .467, .477, .485, .493, .514, .547, .581, .617],  # TCS08
    [.066, .062, .058, .055, .052, .052, .051, .050, .050, .051, .054, .060, .090, .245, .495, .643, .703, .730, .740, .748, .750],  # TCS09 strong red
    [.050, .054, .075, .105, .140, .175, .220, .300, .450, .605, .730, .770, .788, .800, .811, .824, .833, .842, .847, .851, .853],  # TCS10 strong yellow
    [.030, .032, .036, .040, .045, .052, .075, .155, .345, .405, .295, .170, .105, .080, .070, .070, .075, .080, .085, .092, .100],  # TCS11 strong green
    [.072, .090, .165, .290, .460, .430, .330, .200, .115, .075, .058, .052, .050, .052, .058, .067, .075, .085, .092, .100, .108],  # TCS12 strong blue
    [.150, .180, .200, .250, .300, .348, .412, .450, .475, .490, .510, .555, .595, .610, .625, .635, .640, .643, .645, .647, .650],  # TCS13 complexion
    [.043, .046, .050, .054, .062, .085, .114, .155, .205, .215, .180, .140, .115, .110, .115, .140, .180, .200, .215, .225, .235],  # TCS14 olive leaf
    [.137, .144, .155, .205, .250, .285, .310, .330, .360, .405, .500, .585, .620, .630, .635, .640, .642, .644, .646, .648, .650],  # TCS15 Asian skin
])
# Resample to the CIE 5 nm grid
TCS = np.array([np.interp(CIE_WL_5, TCS_WL_20, row) for row in _TCS_20])


# ════════════════════════════════════════════════════════════
# Scotopic luminous efficiency function V'(λ) — CIE 1951
# Peak at 507 nm, used for S/P ratio
# ════════════════════════════════════════════════════════════
_V_SCOTOPIC_10 = np.array([
    0.000589, 0.002209, 0.009290, 0.034840, 0.096600, 0.199800, 0.328100, 0.455000,
    0.567000, 0.676000, 0.793000, 0.904000, 0.982000, 0.997000, 0.935000, 0.811000,
    0.650000, 0.481000, 0.328800, 0.207600, 0.121200, 0.065500, 0.033150, 0.015930,
    0.007370, 0.003335, 0.001497, 0.000677, 0.000313, 0.000148, 0.000071, 0.000035,
    0.000018, 0.000009, 0.000005, 0.000003, 0.000001, 0.000001, 0.000000, 0.000000,
    0.000000,
])
V_SCOTOPIC = np.interp(CIE_WL_5, CIE_WL, _V_SCOTOPIC_10)


# Photometric constants
LM_PER_WATT_PHOTOPIC = 683.002    # K_m for photopic vision
LM_PER_WATT_SCOTOPIC = 1700.06    # K'_m for scotopic vision
LX_TO_FC = 0.09290304             # 1 lx = 0.0929 fc


# ════════════════════════════════════════════════════════════
# Basic colorimetry
# ════════════════════════════════════════════════════════════
def resample_spd(wavelengths_nm: np.ndarray, intensities: np.ndarray) -> np.ndarray:
    """Resample an arbitrary SPD onto the CIE 5 nm grid (380-780 nm)."""
    return np.interp(CIE_WL_5, wavelengths_nm, intensities, left=0.0, right=0.0)


def spectrum_to_xyz(wavelengths_nm: np.ndarray,
                    intensities: np.ndarray) -> tuple[float, float, float]:
    """Integrate an SPD against the CIE 1931 2° observer to get tristimulus XYZ."""
    spd = resample_spd(wavelengths_nm, intensities)
    # Sanitise: replace any inf/nan with 0 so the tristimulus integrals stay finite.
    spd = np.nan_to_num(spd, nan=0.0, posinf=0.0, neginf=0.0)
    dx = float(CIE_WL_5[1] - CIE_WL_5[0])
    X = float(np.sum(spd * X_BAR)) * dx
    Y = float(np.sum(spd * Y_BAR)) * dx
    Z = float(np.sum(spd * Z_BAR)) * dx
    return X, Y, Z


def xyz_to_xy(X: float, Y: float, Z: float) -> tuple[float, float]:
    s = X + Y + Z
    if s <= 0.0:
        return 0.333, 0.333
    return X / s, Y / s


def xy_to_uv60(x: float, y: float) -> tuple[float, float]:
    """CIE 1960 UCS — used by both CCT and CRI."""
    denom = -2.0 * x + 12.0 * y + 3.0
    if denom == 0.0:
        return 0.0, 0.0
    return 4.0 * x / denom, 6.0 * y / denom


def cct_mccamy(x: float, y: float) -> float:
    """McCamy's cubic approximation of CCT from CIE 1931 xy (valid 1000-15000 K)."""
    denom = (0.1858 - y)
    if denom == 0.0:
        return float('nan')
    n = (x - 0.3320) / denom
    return 449.0 * n ** 3 + 3525.0 * n ** 2 + 6823.3 * n + 5520.33


def _valid_cct(cct: float, duv: float) -> bool:
    """CCT is only physically meaningful for near-white sources close to the
    Planckian locus. Off-locus spectra (e.g. a saturated 2-band magenta) push
    McCamy's approximation far outside its valid range and yield absurd values
    (e.g. -34,000,000 K). Treat CCT as undefined when the point is too far from
    the locus (|Duv| > 0.05, the ANSI/IES whiteness limit) or out of range."""
    return (np.isfinite(cct) and 1000.0 <= cct <= 25000.0
            and np.isfinite(duv) and abs(duv) <= 0.05)


# ════════════════════════════════════════════════════════════
# Reference illuminants (Planckian + CIE D-series)
# ════════════════════════════════════════════════════════════
def planckian_spd(T_K: float, wl_nm: np.ndarray = CIE_WL_5) -> np.ndarray:
    """Planck blackbody SPD in W·m⁻²·nm⁻¹·sr⁻¹ (unnormalised)."""
    h = 6.62607015e-34
    c = 2.99792458e8
    k = 1.380649e-23
    wl_m = wl_nm * 1e-9
    a = (2.0 * h * c ** 2) / (wl_m ** 5)
    b = (h * c) / (wl_m * k * T_K)
    # Guard against overflow at low T / short wavelength, and against a zero
    # denominator: exp(b)-1 → 0 as b → 0, so keep b strictly positive.
    b = np.clip(b, 1e-3, 700.0)
    return a / np.expm1(b)


def planckian_uv(T_K: float) -> tuple[float, float]:
    spd = planckian_spd(T_K)
    X, Y, Z = spectrum_to_xyz(CIE_WL_5, spd)
    x, y = xyz_to_xy(X, Y, Z)
    return xy_to_uv60(x, y)


def planckian_locus_xy(T_min: float = 1500.0, T_max: float = 20000.0,
                       n: int = 60) -> np.ndarray:
    """Returns (N,2) array of (x,y) along the Planckian locus."""
    temps = np.linspace(T_min, T_max, n)
    pts = []
    for T in temps:
        spd = planckian_spd(T)
        X, Y, Z = spectrum_to_xyz(CIE_WL_5, spd)
        pts.append(xyz_to_xy(X, Y, Z))
    return np.array(pts)


def d_illuminant_spd(T_K: float, wl_nm: np.ndarray = CIE_WL_5) -> np.ndarray:
    """
    CIE daylight (D-series) illuminant SPD at correlated colour temperature T_K.
    Valid for 4000 K ≤ T ≤ 25000 K.
    """
    # 1) Compute D-illuminant chromaticity (x_D, y_D)
    if T_K <= 7000.0:
        x_D = -4.6070e9 / T_K ** 3 + 2.9678e6 / T_K ** 2 + 0.09911e3 / T_K + 0.244063
    else:
        x_D = -2.0064e9 / T_K ** 3 + 1.9018e6 / T_K ** 2 + 0.24748e3 / T_K + 0.237040
    y_D = -3.0 * x_D ** 2 + 2.870 * x_D - 0.275

    # 2) Compute M1, M2 weights
    M1 = (-1.3515 - 1.7703 * x_D + 5.9114 * y_D) / (0.0241 + 0.2562 * x_D - 0.7341 * y_D)
    M2 = ( 0.0300 - 31.4424 * x_D + 30.0717 * y_D) / (0.0241 + 0.2562 * x_D - 0.7341 * y_D)

    # 3) S0, S1, S2 basis functions — CIE D-series eigenvectors at 10 nm
    S0_10 = np.array([
        63.4, 65.8, 94.8,104.8,105.9,  96.8,113.9,125.6,125.5,121.3,121.3,113.5,
       113.1,110.8,106.5,108.8,105.3,104.4,100.0, 96.0, 95.1, 89.1, 90.5, 90.3,
        88.4, 84.0, 85.1, 81.9, 82.6, 84.9, 81.3, 71.9, 74.3, 76.4, 63.3, 71.7,
        77.0, 65.2, 47.7, 68.6, 65.0,
    ])
    S1_10 = np.array([
        38.5, 35.0, 43.4, 46.3, 43.9, 37.1, 36.7, 35.9, 32.6, 27.9, 24.3, 20.1,
        16.2, 13.2, 8.6, 6.1, 4.2, 1.9, 0.0,-1.6,-3.5,-3.5,-5.8,-7.2, -8.6,-9.5,
       -10.9,-10.7,-12.0,-14.0,-13.6,-12.0,-13.3,-12.9,-10.6,-11.6,-12.2,-10.2,
        -7.8,-11.2,-10.4,
    ])
    S2_10 = np.array([
         3.0,  1.2,  -1.1,  -0.5,  -0.7,  -1.2,  -2.6,  -2.9,  -2.8,  -2.6,  -2.6,  -1.8,
        -1.5,  -1.3,  -1.2,  -1.0,  -0.5,  -0.3,   0.0,   0.2,   0.5,   2.1,   3.2,   4.1,
         4.7,   5.1,   6.7,   7.3,   8.6,   9.8,  10.2,   8.3,   9.6,   8.5,   7.0,   7.6,
         8.0,   6.7,   5.2,   7.4,   6.8,
    ])
    S0 = np.interp(wl_nm, CIE_WL, S0_10)
    S1 = np.interp(wl_nm, CIE_WL, S1_10)
    S2 = np.interp(wl_nm, CIE_WL, S2_10)
    return S0 + M1 * S1 + M2 * S2


def reference_illuminant_spd(cct_K: float) -> np.ndarray:
    """CIE-13.3 recommended reference: Planckian below 5000 K, D-series at/above."""
    if cct_K < 5000.0:
        return planckian_spd(cct_K)
    return d_illuminant_spd(cct_K)


# ════════════════════════════════════════════════════════════
# Duv (signed distance from Planckian locus in CIE 1960 u,v)
# ════════════════════════════════════════════════════════════
def duv_from_xy(x: float, y: float) -> float:
    """Compute Duv using the Ohno 2014 method (sign: + = above locus, - = below)."""
    u, v = xy_to_uv60(x, y)
    # Sample the Planckian locus in (u,v) and find closest
    T = np.linspace(1500.0, 20000.0, 400)
    pl = np.array([planckian_uv(t) for t in T])
    d = np.hypot(pl[:, 0] - u, pl[:, 1] - v)
    i = int(np.argmin(d))
    # signed: positive if measurement above locus (higher v)
    sign = 1.0 if v >= pl[i, 1] else -1.0
    return sign * float(d[i])


# ════════════════════════════════════════════════════════════
# CRI Ra — CIE 13.3-1995 method (TCS01-TCS08 → general Ra)
# ════════════════════════════════════════════════════════════
def _xyz_under(spd_illum: np.ndarray, reflectance: np.ndarray
               ) -> tuple[float, float, float]:
    dx = float(CIE_WL_5[1] - CIE_WL_5[0])
    p = spd_illum * reflectance
    X = float(np.sum(p * X_BAR)) * dx
    Y = float(np.sum(p * Y_BAR)) * dx
    Z = float(np.sum(p * Z_BAR)) * dx
    return X, Y, Z


# ════════════════════════════════════════════════════════════
# Display-colour helpers — render a reflective sample under an illuminant to an
# sRGB hex string, for the Ref/Test colour-patch panels and the per-sample bar
# colours. The sample XYZ are chromatically adapted (Bradford) from the source
# white to D65 before the sRGB transform, so what is drawn is the *adapted*
# appearance (how an observer adapted to the source perceives the object) — the
# Ref vs Test difference then shows the pure colour-rendering shift, not the
# overall white-point tint. Pure numpy: no dependency on the optional
# colour-science package.
# ════════════════════════════════════════════════════════════
_BRADFORD = np.array([[ 0.8951,  0.2664, -0.1614],
                      [-0.7502,  1.7135,  0.0367],
                      [ 0.0389, -0.0685,  1.0296]])
_BRADFORD_INV = np.linalg.inv(_BRADFORD)
_D65_XYZ = np.array([95.047, 100.0, 108.883])      # 2° D65 reference white
_SRGB_M = np.array([[ 3.2406, -1.5372, -0.4986],   # linear sRGB (D65)
                    [-0.9689,  1.8758,  0.0415],
                    [ 0.0557, -0.2040,  1.0570]])


def _adapt_xyz_to_d65(XYZ: np.ndarray, white_xyz: np.ndarray) -> np.ndarray:
    """Bradford-adapt XYZ (relative to ``white_xyz``) to the D65 viewing white.
    Accepts a single (3,) vector or an (N,3) array."""
    white_xyz = np.asarray(white_xyz, dtype=float)
    src = _BRADFORD @ white_xyz
    dst = _BRADFORD @ _D65_XYZ
    M = _BRADFORD_INV @ np.diag(dst / np.where(src == 0, 1e-9, src)) @ _BRADFORD
    return np.asarray(XYZ, dtype=float) @ M.T


def _xyz_to_srgb_hex(XYZ: np.ndarray) -> "str | list[str]":
    """Convert D65-relative XYZ (Y≈100 = white) to a clipped sRGB '#rrggbb' hex.
    Accepts a (3,) vector (returns one string) or an (N,3) array (returns list)."""
    arr = np.atleast_2d(np.asarray(XYZ, dtype=float)) / 100.0
    rgb = arr @ _SRGB_M.T
    rgb = np.clip(rgb, 0.0, 1.0)
    rgb = np.where(rgb <= 0.0031308, 12.92 * rgb,
                   1.055 * np.power(rgb, 1.0 / 2.4) - 0.055)
    rgb = np.clip(rgb, 0.0, 1.0)
    out = ["#%02x%02x%02x" % tuple((row * 255 + 0.5).astype(int)) for row in rgb]
    return out[0] if np.ndim(XYZ) == 1 else out


def _xyz_to_lab_d65(XYZ: np.ndarray) -> np.ndarray:
    """CIELAB (under D65) from D65-relative XYZ (Y≈100 = white). (3,) or (N,3)."""
    arr = np.atleast_2d(np.asarray(XYZ, dtype=float)) / _D65_XYZ
    eps, kappa = 216.0 / 24389.0, 24389.0 / 27.0
    f = np.where(arr > eps, np.cbrt(arr), (kappa * arr + 16.0) / 116.0)
    L = 116.0 * f[:, 1] - 16.0
    a = 500.0 * (f[:, 0] - f[:, 1])
    b = 200.0 * (f[:, 1] - f[:, 2])
    lab = np.stack([L, a, b], axis=1)
    return lab[0] if np.ndim(XYZ) == 1 else lab


def _render_sample_hex(spd_illum: np.ndarray, reflectance: np.ndarray,
                       white_xyz: np.ndarray) -> tuple[str, np.ndarray]:
    """Render one reflective sample under ``spd_illum`` to an adapted sRGB hex and
    its CIELAB triplet. ``white_xyz`` is the (raw, un-normalised) illuminant white
    so the sample is scaled relative to it before adaptation."""
    w = np.asarray(white_xyz, dtype=float)
    scale = 100.0 / max(w[1], 1e-12)
    xyz = np.array(_xyz_under(spd_illum, reflectance)) * scale
    xyz_d65 = _adapt_xyz_to_d65(xyz, w * scale)
    return _xyz_to_srgb_hex(xyz_d65), _xyz_to_lab_d65(xyz_d65)


def cri_tcs_swatches(wavelengths_nm: np.ndarray, intensities: np.ndarray
                     ) -> "dict | None":
    """Render the 15 CIE 13.3 test-colour samples (TCS01–TCS15) under both the
    measured source and the CRI reference illuminant, for the Ref/Test colour-
    patch panel. Returns a JSON-safe dict::

        {labels:[...], ref:[hex...], test:[hex...], dE:[float|None...],
         source_ref:hex, source_test:hex}

    where ``ref``/``test`` are the adapted sRGB appearances and ``dE`` is the
    CIELAB ΔE*ab between them per sample. ``source_*`` are the un-adapted source
    white tints (Ref vs Test white point). None if the spectrum is degenerate."""
    spd_test = resample_spd(wavelengths_nm, intensities)
    if np.sum(spd_test) <= 0:
        return None
    X_t, Y_t, Z_t = spectrum_to_xyz(CIE_WL_5, spd_test)
    x_t, y_t = xyz_to_xy(X_t, Y_t, Z_t)
    cct = cct_mccamy(x_t, y_t)
    if not np.isfinite(cct) or cct <= 0:
        return None
    spd_ref = reference_illuminant_spd(cct)
    X_r, Y_r, Z_r = spectrum_to_xyz(CIE_WL_5, spd_ref)
    if Y_r > 0:                                   # normalise ref to the test Y
        spd_ref = spd_ref * (Y_t / Y_r)
    w_test = np.array(spectrum_to_xyz(CIE_WL_5, spd_test))
    w_ref = np.array(spectrum_to_xyz(CIE_WL_5, spd_ref))

    labels, ref_hex, test_hex, dE = [], [], [], []
    for k in range(len(TCS)):
        hr, lab_r = _render_sample_hex(spd_ref, TCS[k], w_ref)
        ht, lab_t = _render_sample_hex(spd_test, TCS[k], w_test)
        labels.append(f"TCS{k + 1:02d}")
        ref_hex.append(hr); test_hex.append(ht)
        d = float(np.sqrt(np.sum((lab_t - lab_r) ** 2)))
        dE.append(d if np.isfinite(d) else None)

    # Source patches: the un-adapted white-point tint of each illuminant, so the
    # observer sees Ref vs Test colour temperature / Duv at a glance.
    def _white_tint(white_xyz):
        w = np.asarray(white_xyz, dtype=float)
        return _xyz_to_srgb_hex(w * (100.0 / max(w[1], 1e-12)))
    return {
        "labels": labels, "ref": ref_hex, "test": test_hex, "dE": dE,
        "source_ref": _white_tint(w_ref), "source_test": _white_tint(w_test),
    }


def _adapt_uv(u_ki: float, v_ki: float,
              u_t: float, v_t: float,
              u_r: float, v_r: float) -> tuple[float, float]:
    """Judd / CIE 13.3 chromatic adaptation transform in CIE 1960 UCS."""
    def cd(u, v):
        return ((4.0 - u - 10.0 * v) / v,
                (1.708 * v + 0.404 - 1.481 * u) / v)
    c_t, d_t = cd(u_t, v_t)
    c_r, d_r = cd(u_r, v_r)
    c_ki, d_ki = cd(u_ki, v_ki)
    denom = 16.518 + 1.481 * (c_r / c_t) * c_ki - (d_r / d_t) * d_ki
    u_ad = (10.872 + 0.404 * (c_r / c_t) * c_ki - 4.0 * (d_r / d_t) * d_ki) / denom
    v_ad = 5.520 / denom
    return u_ad, v_ad


def _UVW(Y: float, u: float, v: float, u_r: float, v_r: float
         ) -> tuple[float, float, float]:
    W = 25.0 * (max(Y, 1e-9)) ** (1.0 / 3.0) - 17.0
    U = 13.0 * W * (u - u_r)
    V = 13.0 * W * (v - v_r)
    return U, V, W


def cri_ra(wavelengths_nm: np.ndarray, intensities: np.ndarray
           ) -> tuple[float, list[float], float]:
    """
    Returns (Ra, [R1..R15], CCT_used).

    Method: CIE 13.3-1995 with Judd chromatic adaptation transform.
    Ra is the mean of R1..R8; R9..R15 are reported as additional indices.
    """
    spd_test = resample_spd(wavelengths_nm, intensities)
    if np.sum(spd_test) <= 0:
        return float('nan'), [float('nan')] * 15, float('nan')

    X_t, Y_t, Z_t = spectrum_to_xyz(CIE_WL_5, spd_test)
    x_t, y_t = xyz_to_xy(X_t, Y_t, Z_t)
    cct = cct_mccamy(x_t, y_t)
    if not np.isfinite(cct) or cct <= 0:
        return float('nan'), [float('nan')] * 15, float('nan')

    spd_ref = reference_illuminant_spd(cct)
    X_r, Y_r, Z_r = spectrum_to_xyz(CIE_WL_5, spd_ref)
    if Y_r > 0:
        spd_ref = spd_ref * (Y_t / Y_r)
        X_r, Y_r, Z_r = spectrum_to_xyz(CIE_WL_5, spd_ref)

    u_t, v_t = xy_to_uv60(x_t, y_t)
    x_r, y_r = xyz_to_xy(X_r, Y_r, Z_r)
    u_r, v_r = xy_to_uv60(x_r, y_r)

    Ri: list[float] = []
    for k in range(len(TCS)):
        X_kt, Y_kt, Z_kt = _xyz_under(spd_test, TCS[k])
        X_kr, Y_kr, Z_kr = _xyz_under(spd_ref,  TCS[k])
        x_kt, y_kt = xyz_to_xy(X_kt, Y_kt, Z_kt)
        x_kr, y_kr = xyz_to_xy(X_kr, Y_kr, Z_kr)
        u_kt, v_kt = xy_to_uv60(x_kt, y_kt)
        u_kr, v_kr = xy_to_uv60(x_kr, y_kr)
        u_kt_a, v_kt_a = _adapt_uv(u_kt, v_kt, u_t, v_t, u_r, v_r)
        Y_kt_n = 100.0 * Y_kt / max(Y_t, 1e-9)
        Y_kr_n = 100.0 * Y_kr / max(Y_r, 1e-9)
        U_t, V_t, W_t = _UVW(Y_kt_n, u_kt_a, v_kt_a, u_r, v_r)
        U_r, V_r, W_r = _UVW(Y_kr_n, u_kr,  v_kr,  u_r, v_r)
        dE = np.sqrt((U_t - U_r) ** 2 + (V_t - V_r) ** 2 + (W_t - W_r) ** 2)
        Ri.append(100.0 - 4.6 * float(dE))

    Ra = float(np.mean(Ri[:8]))
    return Ra, Ri, float(cct)


# ════════════════════════════════════════════════════════════
# Spectrum locus (CIE horseshoe boundary)
# ════════════════════════════════════════════════════════════
def spectrum_locus_xy() -> np.ndarray:
    """
    Returns the closed CIE 1931 horseshoe boundary as an (N,2) array of (x,y).
    The last point closes back to the first along the purple line.
    """
    pts = []
    for wl, x, y, z in zip(CIE_WL_5, X_BAR, Y_BAR, Z_BAR):
        s = x + y + z
        if s > 1e-9:
            pts.append((x / s, y / s))
    # Filter to the typical visible range and close along purple line
    pts = np.array(pts)
    # Restrict to 380-700nm (beyond 700nm the locus barely moves but introduces noise)
    mask_idx = (CIE_WL_5 >= 380) & (CIE_WL_5 <= 700)
    pts = pts[mask_idx[:len(pts)]]
    # Close polygon back to start
    return np.vstack([pts, pts[:1]])


def summarize(wavelengths_nm: np.ndarray, intensities: np.ndarray) -> dict:
    """Convenience wrapper — returns everything the GUI needs in one call."""
    X, Y, Z = spectrum_to_xyz(wavelengths_nm, intensities)
    x, y = xyz_to_xy(X, Y, Z)
    cct = cct_mccamy(x, y)
    duv = duv_from_xy(x, y) if Y > 0 else float('nan')
    try:
        Ra, Ri, _ = cri_ra(wavelengths_nm, intensities)
    except Exception:
        Ra, Ri = float('nan'), [float('nan')] * 15
    return dict(X=X, Y=Y, Z=Z, x=x, y=y,
                cct=cct, duv=duv, Ra=Ra, Ri=Ri)


# ════════════════════════════════════════════════════════════
# 1960 / 1976 UCS — uniform colour spaces
# ════════════════════════════════════════════════════════════
def xy_to_uv76(x: float, y: float) -> tuple[float, float]:
    """CIE 1976 UCS u', v' (a.k.a. uniform chromaticity scale 1976)."""
    denom = -2.0 * x + 12.0 * y + 3.0
    if denom == 0.0:
        return 0.0, 0.0
    return 4.0 * x / denom, 9.0 * y / denom


# ════════════════════════════════════════════════════════════
# Photometric integrals — illuminance, luminance, S/P ratio
# ════════════════════════════════════════════════════════════
def illuminance_lux(wavelengths_nm: np.ndarray,
                    intensities: np.ndarray,
                    calibration: float = 0.0) -> float:
    """
    Photopic illuminance in lux.

    `calibration` is the absolute radiometric scale factor in W/m²/nm per ADC
    count. Without a measurement against a traceable reference source this
    value is unknown. When calibration == 0.0 (the default) the function
    returns NaN so the GUI can display "— lx" instead of a meaningless number
    that looks real. Pass a measured factor to get true lux values.
    """
    if calibration <= 0.0:
        return float('nan')
    spd = resample_spd(wavelengths_nm, intensities)
    dx = float(CIE_WL_5[1] - CIE_WL_5[0])
    return LM_PER_WATT_PHOTOPIC * float(np.sum(spd * Y_BAR)) * dx * calibration


def illuminance_fc(wavelengths_nm: np.ndarray,
                   intensities: np.ndarray,
                   calibration: float = 0.0) -> float:
    """Same as illuminance_lux but in foot-candles (1 fc = 10.764 lx).
    Returns NaN when calibration == 0.0."""
    return illuminance_lux(wavelengths_nm, intensities, calibration) * LX_TO_FC


def sp_ratio(wavelengths_nm: np.ndarray, intensities: np.ndarray) -> float:
    """
    Scotopic / Photopic luminous-efficacy ratio of an SPD.
    Unitless and calibration-independent.
    """
    spd = resample_spd(wavelengths_nm, intensities)
    photopic = float(np.sum(spd * Y_BAR))
    scotopic = float(np.sum(spd * V_SCOTOPIC))
    if photopic <= 0:
        return float('nan')
    return (LM_PER_WATT_SCOTOPIC / LM_PER_WATT_PHOTOPIC) * (scotopic / photopic)


# ════════════════════════════════════════════════════════════
# Spectral descriptors — peak / dominant / centroid / FWHM / purity
# ════════════════════════════════════════════════════════════
def peak_wavelength(wavelengths_nm: np.ndarray,
                    intensities: np.ndarray) -> float:
    if intensities.size == 0:
        return float('nan')
    i = int(np.argmax(intensities))
    return float(wavelengths_nm[i])


def centroid_wavelength(wavelengths_nm: np.ndarray,
                        intensities: np.ndarray) -> float:
    """Intensity-weighted mean wavelength (first-moment of the SPD)."""
    s = float(np.sum(intensities))
    if s <= 0:
        return float('nan')
    return float(np.sum(wavelengths_nm * intensities) / s)


def fwhm_nm(wavelengths_nm: np.ndarray,
            intensities: np.ndarray) -> tuple[float, float, float]:
    """
    Returns (fwhm_nm, lo_nm, hi_nm) — full width at half maximum about the peak.
    lo/hi are the wavelengths where the curve crosses half-max on either side.
    """
    if intensities.size == 0:
        return float('nan'), float('nan'), float('nan')
    half = float(np.max(intensities)) / 2.0
    if half <= 0:
        return float('nan'), float('nan'), float('nan')
    peak_idx = int(np.argmax(intensities))
    # walk left
    lo_idx = peak_idx
    while lo_idx > 0 and intensities[lo_idx] > half:
        lo_idx -= 1
    # walk right
    hi_idx = peak_idx
    while hi_idx < len(intensities) - 1 and intensities[hi_idx] > half:
        hi_idx += 1
    # linear interpolate to find the half-max crossings
    def _xcross(i0, i1):
        if i0 == i1:
            return float(wavelengths_nm[i0])
        y0, y1 = intensities[i0], intensities[i1]
        if y1 == y0:
            return float(wavelengths_nm[i0])
        t = (half - y0) / (y1 - y0)
        return float(wavelengths_nm[i0] + t *
                     (wavelengths_nm[i1] - wavelengths_nm[i0]))
    lo = _xcross(lo_idx, min(lo_idx + 1, len(intensities) - 1))
    hi = _xcross(max(hi_idx - 1, 0), hi_idx)
    return float(hi - lo), lo, hi


def central_wavelength(wavelengths_nm: np.ndarray,
                       intensities: np.ndarray) -> float:
    """Midpoint of the FWHM band — (lo + hi) / 2."""
    _, lo, hi = fwhm_nm(wavelengths_nm, intensities)
    if not (np.isfinite(lo) and np.isfinite(hi)):
        return float('nan')
    return (lo + hi) / 2.0


# ── Dominant wavelength + Excitation purity ────────────────
def _white_point_xy(cct: float) -> tuple[float, float]:
    """Return the reference white point at given CCT (D-illuminant or Planck)."""
    spd = reference_illuminant_spd(cct) if np.isfinite(cct) else planckian_spd(6500)
    X, Y, Z = spectrum_to_xyz(CIE_WL_5, spd)
    return xyz_to_xy(X, Y, Z)


def dominant_wavelength_and_purity(
        x: float, y: float, cct: float) -> tuple[float, float]:
    """
    Cast a ray from the white point through (x, y); return the wavelength of
    its intersection with the spectrum locus and the excitation purity (%).

    Negative wavelength encodes a complementary dominant: the ray exits via
    the purple line — the returned value is the complementary λ negated
    (e.g. -550 means complementary dominant at 550 nm).
    """
    wx, wy = _white_point_xy(cct)
    locus = spectrum_locus_xy()                # (N, 2) closed polygon
    # ray direction
    dx, dy = x - wx, y - wy
    if dx == 0 and dy == 0:
        return float('nan'), 0.0
    # Find intersection of ray (from white in direction dx,dy) with each segment
    pts = locus
    n = len(pts) - 1
    best_t = np.inf
    best_idx = -1
    best_u  = 0.0
    for k in range(n):
        x1, y1 = pts[k]
        x2, y2 = pts[k + 1]
        denom = (-dx) * (y2 - y1) - (-dy) * (x2 - x1)
        if abs(denom) < 1e-12:
            continue
        t = ((x1 - wx) * (y2 - y1) - (y1 - wy) * (x2 - x1)) / denom
        u = ((x1 - wx) * (-dy)     - (y1 - wy) * (-dx))     / denom
        # t = scalar along ray (>0 forward); u = position along segment [0,1]
        if t > 1e-9 and -1e-6 <= u <= 1.0 + 1e-6 and t < best_t:
            best_t = t; best_idx = k; best_u = u
    if best_idx < 0:
        return float('nan'), 0.0
    # Wavelength of the locus segment at position u
    # The locus arrays correspond to CIE_WL_5 indices (with the closing wrap-around)
    valid_n = len(pts) - 1
    if best_idx >= valid_n - 1:
        # Hit on the closing purple line → complementary dominant
        # Re-shoot the ray in the opposite direction
        dx2, dy2 = -dx, -dy
        best_t2 = np.inf; best_idx2 = -1; best_u2 = 0.0
        for k in range(n):
            x1, y1 = pts[k]
            x2, y2 = pts[k + 1]
            denom = (-dx2) * (y2 - y1) - (-dy2) * (x2 - x1)
            if abs(denom) < 1e-12:
                continue
            t = ((x1 - wx) * (y2 - y1) - (y1 - wy) * (x2 - x1)) / denom
            u = ((x1 - wx) * (-dy2)    - (y1 - wy) * (-dx2)) / denom
            if t > 1e-9 and -1e-6 <= u <= 1.0 + 1e-6 and t < best_t2:
                best_t2 = t; best_idx2 = k; best_u2 = u
        if best_idx2 < 0:
            return float('nan'), 0.0
        idx_low = best_idx2
        u_seg = best_u2
        # Convert segment index → wavelength via the underlying CIE 5 nm grid
        # The locus polygon ends with a duplicate first point, so segment idx maps to CIE_WL_5[idx]
        wl_lo = CIE_WL_5[idx_low] if idx_low < len(CIE_WL_5) else CIE_WL_5[-1]
        wl_hi = CIE_WL_5[idx_low + 1] if idx_low + 1 < len(CIE_WL_5) else wl_lo
        wl = wl_lo + u_seg * (wl_hi - wl_lo)
        # Purity along the reversed ray:
        d_white_to_color = np.hypot(dx, dy)
        d_white_to_locus_neg = best_t2 * np.hypot(dx, dy)  # reversed ray
        purity = 100.0 * d_white_to_color / max(d_white_to_color + d_white_to_locus_neg, 1e-9)
        return -float(wl), float(purity)
    else:
        idx_low = best_idx
        u_seg = best_u
        wl_lo = CIE_WL_5[idx_low] if idx_low < len(CIE_WL_5) else CIE_WL_5[-1]
        wl_hi = CIE_WL_5[idx_low + 1] if idx_low + 1 < len(CIE_WL_5) else wl_lo
        wl = wl_lo + u_seg * (wl_hi - wl_lo)
        d_white_to_color = np.hypot(dx, dy)
        d_white_to_locus = best_t * np.hypot(dx, dy)
        purity = 100.0 * d_white_to_color / max(d_white_to_locus, 1e-9)
        return float(wl), float(min(100.0, purity))


# ── RGB band ratios (energy in B/G/R bands) ────────────────
def rgb_band_ratios(wavelengths_nm: np.ndarray,
                    intensities: np.ndarray,
                    bands: tuple = ((380, 490), (490, 580), (580, 780))
                    ) -> tuple[float, float, float]:
    """
    Returns (red%, green%, blue%) — percentage of total spectral energy in
    each of the three CIE-style colour bands. Bands order = R, G, B.
    """
    spd = resample_spd(wavelengths_nm, intensities)
    total = float(np.sum(spd))
    if total <= 0:
        return float('nan'), float('nan'), float('nan')
    b_mask = (CIE_WL_5 >= bands[0][0]) & (CIE_WL_5 < bands[0][1])
    g_mask = (CIE_WL_5 >= bands[1][0]) & (CIE_WL_5 < bands[1][1])
    r_mask = (CIE_WL_5 >= bands[2][0]) & (CIE_WL_5 <= bands[2][1])
    blue  = 100.0 * float(np.sum(spd[b_mask])) / total
    green = 100.0 * float(np.sum(spd[g_mask])) / total
    red   = 100.0 * float(np.sum(spd[r_mask])) / total
    return red, green, blue


# ── SDCM — Standard Deviation of Color Matching ────────────
def sdcm(x: float, y: float, cct: float) -> float:
    """
    Approximate SDCM (MacAdam step) — distance from the target Planckian
    point at the given CCT, in CIE 1960 u,v scaled by the canonical
    1 SDCM ≈ 0.0011 uv-distance threshold.
    """
    if not np.isfinite(cct) or cct <= 0:
        return float('nan')
    u, v = xy_to_uv60(x, y)
    u0, v0 = planckian_uv(cct)
    d_uv = float(np.hypot(u - u0, v - v0))
    return d_uv / 0.0011


# ════════════════════════════════════════════════════════════
# Top-level convenience: compute everything in one go
# ════════════════════════════════════════════════════════════
def measure_all(wavelengths_nm: np.ndarray,
                intensities: np.ndarray,
                lux_calibration: float = 0.0) -> dict:
    """
    Compute every metric exposed by this module. Returns a flat dict keyed by
    short names. Numeric fields are NaN where the measurement is degenerate
    (e.g. all-zero spectrum).
    """
    out: dict = {}
    if intensities is None or intensities.size == 0 or np.sum(intensities) <= 0:
        out.update(dict(X=0, Y=0, Z=0, x=float('nan'), y=float('nan'),
                        cct=float('nan'), duv=float('nan'),
                        u60=float('nan'), v60=float('nan'),
                        u76=float('nan'), v76=float('nan'),
                        lux=float('nan'), fc=float('nan'),
                        Ra=float('nan'), Ri=[float('nan')] * 15,
                        sp_ratio=float('nan'), sdcm=float('nan'),
                        peak_nm=float('nan'), dominant_nm=float('nan'),
                        centroid_nm=float('nan'), central_nm=float('nan'),
                        fwhm_nm=float('nan'),
                        purity_pct=float('nan'),
                        red_pct=float('nan'), green_pct=float('nan'),
                        blue_pct=float('nan'),
                        tcs_swatches=None))
        return out

    X, Y, Z = spectrum_to_xyz(wavelengths_nm, intensities)
    x, y = xyz_to_xy(X, Y, Z)
    cct = cct_mccamy(x, y)
    duv = duv_from_xy(x, y) if Y > 0 else float('nan')
    if not _valid_cct(cct, duv):
        cct = float('nan')        # off-locus → CCT undefined (shown as "—")
    u60, v60 = xy_to_uv60(x, y)
    u76, v76 = xy_to_uv76(x, y)
    lux = illuminance_lux(wavelengths_nm, intensities, lux_calibration)
    fc  = illuminance_fc (wavelengths_nm, intensities, lux_calibration)
    try:
        Ra, Ri, _ = cri_ra(wavelengths_nm, intensities)
    except Exception:
        Ra, Ri = float('nan'), [float('nan')] * 15
    sp = sp_ratio(wavelengths_nm, intensities)
    sd = sdcm(x, y, cct)
    pk = peak_wavelength(wavelengths_nm, intensities)
    fw, _, _ = fwhm_nm(wavelengths_nm, intensities)
    cn = centroid_wavelength(wavelengths_nm, intensities)
    cw = central_wavelength(wavelengths_nm, intensities)
    dom, pur = dominant_wavelength_and_purity(x, y, cct)
    r_p, g_p, b_p = rgb_band_ratios(wavelengths_nm, intensities)
    try:
        swatches = cri_tcs_swatches(wavelengths_nm, intensities)
    except Exception:
        swatches = None

    out.update(dict(X=X, Y=Y, Z=Z, x=x, y=y,
                    cct=cct, duv=duv,
                    u60=u60, v60=v60, u76=u76, v76=v76,
                    lux=lux, fc=fc,
                    Ra=Ra, Ri=Ri,
                    sp_ratio=sp, sdcm=sd,
                    peak_nm=pk, dominant_nm=dom,
                    centroid_nm=cn, central_nm=cw,
                    fwhm_nm=fw,
                    purity_pct=pur,
                    red_pct=r_p, green_pct=g_p, blue_pct=b_p,
                    tcs_swatches=swatches))
    return out


# ════════════════════════════════════════════════════════════
# Report / CSV export (shared by the desktop CIE tab and the web app)
# ════════════════════════════════════════════════════════════
# Approximate display colour for each TCS sample (R1..R15) — used to colour the
# CRI bar chart and radar dots in reports and the web tab.
TCS_DISPLAY_COLORS = [
    "#E8B89B", "#D9C982", "#C7D068", "#8FBE7C", "#7BBFAE",
    "#7AA3D4", "#9F8DCB", "#C77AB1", "#C13F3A", "#D8C04A",
    "#4FA864", "#2F4FB8", "#E2B89A", "#7E8F50", "#D9B8A0",
]


def color_report_rows(wavelengths_nm, intensities, lux_calibration: float = 0.0):
    """Flatten measure_all() into ordered (label, value-string) rows for a CSV /
    report table. The single authoritative metric set for both apps."""
    m = measure_all(np.asarray(wavelengths_nm, dtype=float),
                     np.asarray(intensities, dtype=float), lux_calibration)

    def f(v, nd=2, suf=""):
        if v is None or (isinstance(v, float) and not np.isfinite(v)):
            return "—"
        return f"{v:.{nd}f}{suf}"

    lux_str = "—"
    if m.get("lux") is not None and np.isfinite(m["lux"]):
        lux_str = f"{m['lux']:.0f} lx" + ("" if lux_calibration else " (uncal.)")

    rows = [
        ("CCT", f(m["cct"], 0, " K")),
        ("Duv", f(m["duv"], 4)),
        ("CIE x", f(m["x"], 4)),
        ("CIE y", f(m["y"], 4)),
        ("CIE u' (1976)", f(m["u76"], 4)),
        ("CIE v' (1976)", f(m["v76"], 4)),
        ("Illuminance", lux_str),
        ("S/P ratio", f(m["sp_ratio"], 3)),
        ("SDCM (MacAdam steps)", f(m["sdcm"], 1)),
        ("CRI Ra", f(m["Ra"], 0)),
        ("R9 (deep red)", f(m["Ri"][8], 0) if m.get("Ri") else "—"),
        ("Peak wavelength", f(m["peak_nm"], 1, " nm")),
        ("Dominant wavelength", f(m["dominant_nm"], 1, " nm")),
        ("Centroid wavelength", f(m["centroid_nm"], 1, " nm")),
        ("Central wavelength", f(m["central_nm"], 1, " nm")),
        ("FWHM", f(m["fwhm_nm"], 1, " nm")),
        ("Colour purity", f(m["purity_pct"], 1, " %")),
        ("Red fraction", f(m["red_pct"], 1, " %")),
        ("Green fraction", f(m["green_pct"], 1, " %")),
        ("Blue fraction", f(m["blue_pct"], 1, " %")),
    ]
    Ri = m.get("Ri") or []
    for i in range(min(15, len(Ri))):
        rows.append((f"R{i + 1}", f(Ri[i], 0)))

    # TM-30-18 and CQS (optional — only if colour-science is installed).
    t = tm30_metrics(wavelengths_nm, intensities)
    if t is not None:
        rows.append(("TM-30 Rf (fidelity)", f(t["Rf"], 0)))
        rows.append(("TM-30 Rg (gamut)", f(t["Rg"], 0)))
    c = cqs_metrics(wavelengths_nm, intensities)
    if c is not None:
        rows.append(("CQS Qa", f(c["Qa"], 0)))
        rows.append(("CQS Qf (fidelity)", f(c["Qf"], 0)))
        rows.append(("CQS Qg (gamut)", f(c["Qg"], 0)))
    return rows


def _sample_ramp_colors(n: int) -> list:
    """Distinct per-sample colours (continuous red→magenta hue ramp) for the 99
    TM-30 colour-evaluation samples, so each bar gets its own colour instead of
    sharing one of the 16 hue-bin colours."""
    import colorsys
    out = []
    for i in range(max(n, 0)):
        r, g, b = colorsys.hsv_to_rgb((i / max(n - 1, 1)) * 0.83, 0.62, 0.97)
        out.append("#%02x%02x%02x" % (int(r * 255), int(g * 255), int(b * 255)))
    return out


def _render_report_meta(fig, meta: dict | None) -> None:
    """Draw an optional ordered acquisition-parameter dict as a monospaced header
    block at the top of a report figure (used by both colour and filter reports)."""
    if not meta:
        return
    items = [f"{k}: {v}" for k, v in meta.items() if v not in (None, "")]
    if not items:
        return
    lines = ["    ".join(items[i:i + 3]) for i in range(0, len(items), 3)]
    fig.text(0.06, 0.997, "\n".join(lines), family="monospace", fontsize=7.0,
             va="top", ha="left", color="#222222")


def render_color_report(dst, wavelengths_nm, intensities,
                        fmt: str = "pdf", dpi: int = 150,
                        lux_calibration: float = 0.0,
                        meta: dict | None = None) -> None:
    """Render a self-contained colour / CRI report — SPD plot, the R1–R15 colour-
    rendering bar chart, and the full metrics table — on a WHITE background, to
    ``dst`` (path or binary file-like). ``fmt`` is 'pdf' or 'png'. ``meta`` is an
    optional ordered dict of acquisition parameters (spectrometer, serial, date,
    exposure, dark, peak ADC) rendered as a monospaced header block. Raises
    ImportError if matplotlib is missing."""
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    wl = np.asarray(wavelengths_nm, dtype=float)
    inten = np.asarray(intensities, dtype=float)
    m = measure_all(wl, inten, lux_calibration)
    Ri = m.get("Ri") or [float("nan")] * 15
    vals = [r if (r is not None and np.isfinite(r)) else 0.0 for r in Ri[:15]]
    t = tm30_metrics(wl, inten)              # None if colour-science missing
    c = cqs_metrics(wl, inten)

    has_tm30 = t is not None
    fig = plt.figure(figsize=(8.27, 13.0 if has_tm30 else 10.4), facecolor="white")
    _render_report_meta(fig, meta)

    if has_tm30:
        spd_box  = [0.10, 0.83, 0.85, 0.13]
        radar_box= [0.05, 0.625, 0.36, 0.165]
        bars_box = [0.50, 0.645, 0.45, 0.145]
        cvg_box  = [0.06, 0.40, 0.34, 0.185]
        s99_box  = [0.47, 0.41, 0.49, 0.165]
        patch_box= [0.06, 0.315, 0.90, 0.060]
        tbl_box  = [0.06, 0.02, 0.90, 0.275]
    else:
        spd_box  = [0.10, 0.74, 0.85, 0.20]
        radar_box= [0.07, 0.435, 0.36, 0.27]
        bars_box = [0.52, 0.455, 0.43, 0.25]
        cvg_box = s99_box = None
        patch_box= [0.08, 0.345, 0.88, 0.060]
        tbl_box  = [0.08, 0.03, 0.88, 0.285]

    # 1) Spectral power distribution
    ax1 = fig.add_axes(spd_box); ax1.set_facecolor("white")
    if inten.size and np.nanmax(inten) > 0:
        ax1.plot(wl, inten, color="#b91c1c", lw=1.4)
        ax1.fill_between(wl, inten, color="#b91c1c", alpha=0.08)
    ax1.set_xlabel("Wavelength (nm)"); ax1.set_ylabel("Rel. power")
    ax1.grid(alpha=0.3)
    cct = m.get("cct"); ra = m.get("Ra", float("nan"))
    title_cct = f"{cct:.0f} K" if (cct is not None and np.isfinite(cct)) else "—"
    title_ra = f"{ra:.0f}" if np.isfinite(ra) else "—"
    extra = (f" · TM-30 Rf {t['Rf']:.0f}/Rg {t['Rg']:.0f}" if has_tm30 else "")
    ax1.set_title(f"Colour / CRI report — CCT {title_cct} · Ra {title_ra}{extra}")

    # 2a) CRI radar (R1..R15) — drawn in Cartesian coordinates (matplotlib's
    # polar fill mis-renders the closing edge with our wrapped angles and drops
    # the R1–R4 wedge; Cartesian is exact and matches the web/desktop radars).
    _draw_cri_radar(fig.add_axes(radar_box), vals)

    # 2b) CRI R1..R15 bar chart in TCS colours
    ax2 = fig.add_axes(bars_box); ax2.set_facecolor("white")
    idx = np.arange(1, 16)
    ax2.bar(idx, vals, color=list(TCS_DISPLAY_COLORS), edgecolor="#666", linewidth=0.4)
    ax2.axhline(0, color="#666", lw=0.6)
    ax2.set_xticks(idx); ax2.set_xticklabels([f"R{i}" for i in idx], fontsize=6, rotation=90)
    ax2.set_ylim(min(0, min(vals) - 5), 100); ax2.set_ylabel("Ri")
    ax2.set_title("CRI — colour rendering by sample", fontsize=9)
    ax2.grid(axis="y", alpha=0.3)

    # 3) TM-30 colour-vector graphic + 99-sample fidelity bars
    if has_tm30:
        _draw_tm30_cvg(fig.add_axes(cvg_box), t)
        axs = fig.add_axes(s99_box); axs.set_facecolor("white")
        Rs = t["Rs"]; bins = t["bins"]
        # Each bar in the CES sample's true (reference) colour; fall back to the
        # synthetic ramp only if the colorimetry data couldn't be derived.
        cols = t.get("Rs_hex") or _sample_ramp_colors(len(Rs))
        axs.bar(np.arange(len(Rs)), Rs, color=cols, width=1.0)
        axs.axhline(100, color="#999", lw=0.5, ls="--")
        axs.set_ylim(0, max(105, (max(Rs) if Rs else 100) + 5))
        axs.set_xlim(-1, len(Rs)); axs.set_xticks([])
        axs.set_ylabel("Rf,i"); axs.grid(axis="y", alpha=0.3)
        axs.set_title("TM-30 — fidelity of all 99 colour samples (true sample colour)",
                      fontsize=9)

    # 3b) CRI Ref/Test colour-patch panel — top row reference, bottom row test
    sw = m.get("tcs_swatches")
    if sw:
        _draw_cri_patches(fig.add_axes(patch_box), sw)

    # 4) Metrics table
    rows = color_report_rows(wl, inten, lux_calibration)
    tax = fig.add_axes(tbl_box); tax.axis("off")
    half = (len(rows) + 1) // 2
    left, right = rows[:half], rows[half:]
    while len(right) < len(left):
        right.append(("", ""))
    cell = [[l[0], l[1], r[0], r[1]] for l, r in zip(left, right)]
    table = tax.table(cellText=cell, colLabels=["Metric", "Value", "Metric", "Value"],
                      loc="upper center", cellLoc="left",
                      colWidths=[0.30, 0.20, 0.30, 0.20])
    table.auto_set_font_size(False); table.set_fontsize(8); table.scale(1.0, 1.22)

    fig.savefig(dst, format=fmt, dpi=dpi, facecolor="white")
    plt.close(fig)


def _draw_cri_radar(ax, vals) -> None:
    """CRI R1–R15 radar on a plain Cartesian axes: grid rings, spokes, the
    closed Ri polygon (Ri/100 as radius) and per-sample TCS-coloured dots, with
    R1 at the top going clockwise."""
    N = 15
    v = np.array([max(0.0, min(100.0, x)) for x in vals[:15]]) / 100.0
    ang = np.pi / 2 - np.arange(N) * 2 * np.pi / N
    th = np.linspace(0, 2 * np.pi, 220)
    for r in (0.2, 0.4, 0.6, 0.8, 1.0):
        ax.plot(r * np.cos(th), r * np.sin(th),
                color=("#bbb" if r == 1.0 else "#e2e2e2"), lw=0.6, zorder=1)
    for a in ang:
        ax.plot([0, np.cos(a)], [0, np.sin(a)], color="#ececec", lw=0.5, zorder=1)
    xs, ys = v * np.cos(ang), v * np.sin(ang)
    ax.fill(np.append(xs, xs[0]), np.append(ys, ys[0]),
            color="#0d9488", alpha=0.18, zorder=2)
    ax.plot(np.append(xs, xs[0]), np.append(ys, ys[0]),
            color="#0d9488", lw=1.4, zorder=3)
    for j in range(N):
        ax.plot([xs[j]], [ys[j]], "o", ms=4,
                color=TCS_DISPLAY_COLORS[j], zorder=4)
        ax.text(1.14 * np.cos(ang[j]), 1.14 * np.sin(ang[j]), f"R{j+1}",
                ha="center", va="center", fontsize=6.5, color="#444")
    ax.text(0.02, 1.04, "100", fontsize=6, color="#999")
    ax.set_xlim(-1.32, 1.32); ax.set_ylim(-1.32, 1.32)
    ax.set_aspect("equal"); ax.axis("off")
    ax.set_title("CRI radar (R1–R15)", fontsize=9)


def _draw_cri_patches(ax, sw: dict) -> None:
    """CRI Ref/Test colour-patch panel: for each of the 15 TCS (plus the source
    white) a reference swatch (top row) over the test swatch (bottom row), with
    the sample label and ΔE*ab underneath. Visualises the per-sample colour shift
    the same way as professional CRI tools."""
    ax.set_facecolor("white")
    labels = list(sw.get("labels", []))
    ref = list(sw.get("ref", [])); test = list(sw.get("test", []))
    dE = list(sw.get("dE", []))
    # append the source-white column
    cols = list(zip(labels, ref, test, dE))
    cols.append(("Src", sw.get("source_ref", "#fff"),
                 sw.get("source_test", "#fff"), None))
    n = len(cols)
    import matplotlib.patches as mpatches
    for i, (lab, hr, ht, d) in enumerate(cols):
        x = i / n
        w = 1.0 / n * 0.86
        ax.add_patch(mpatches.Rectangle((x, 0.52), w, 0.46, transform=ax.transAxes,
                                        facecolor=hr, edgecolor="#888", lw=0.4))
        ax.add_patch(mpatches.Rectangle((x, 0.04), w, 0.46, transform=ax.transAxes,
                                        facecolor=ht, edgecolor="#888", lw=0.4))
        ax.text(x + w / 2, -0.10, lab, transform=ax.transAxes, ha="center",
                va="top", fontsize=5.0, color="#333", rotation=0)
        if d is not None:
            ax.text(x + w / 2, -0.30, f"{d:.1f}", transform=ax.transAxes,
                    ha="center", va="top", fontsize=4.6, color="#777")
    ax.text(-0.012, 0.75, "Ref", transform=ax.transAxes, ha="right", va="center",
            fontsize=6, color="#555")
    ax.text(-0.012, 0.27, "Test", transform=ax.transAxes, ha="right", va="center",
            fontsize=6, color="#555")
    ax.set_title("CRI — reference vs. test appearance (ΔE*ab per sample)",
                 fontsize=9, pad=2)
    ax.set_xlim(0, 1); ax.set_ylim(0, 1); ax.axis("off")


def _draw_tm30_cvg(ax, t: dict) -> None:
    """TM-30 colour-vector graphic: reference unit circle, the 16 hue-bin test
    chromaticities (normalised by the per-bin reference radius) connected into a
    polygon, with arrows showing the reference→test colour shift per bin."""
    ref = np.asarray(t["avg_ref"], dtype=float)
    test = np.asarray(t["avg_test"], dtype=float)
    rref = np.hypot(ref[:, 0], ref[:, 1])
    rref[rref == 0] = 1.0
    refu = ref / rref[:, None]
    testu = test / rref[:, None]

    th = np.linspace(0, 2 * np.pi, 200)
    ax.plot(np.cos(th), np.sin(th), color="#444", lw=1.0)          # reference circle
    for r in (0.8, 1.2):
        ax.plot(r * np.cos(th), r * np.sin(th), color="#ddd", lw=0.5)
    # closed test polygon
    tx = np.append(testu[:, 0], testu[0, 0]); ty = np.append(testu[:, 1], testu[0, 1])
    ax.plot(tx, ty, color="#b91c1c", lw=1.3)
    # per-bin arrows ref→test, coloured by bin
    for j in range(len(refu)):
        ax.annotate("", xy=(testu[j, 0], testu[j, 1]),
                    xytext=(refu[j, 0], refu[j, 1]),
                    arrowprops=dict(arrowstyle="->", color=TM30_BIN_COLORS[j], lw=1.1))
    ax.set_xlim(-1.5, 1.5); ax.set_ylim(-1.5, 1.5)
    ax.set_aspect("equal"); ax.axis("off")
    ax.set_title(f"TM-30 colour vector graphic\nRf {t['Rf']:.0f} · Rg {t['Rg']:.0f}",
                 fontsize=9)


# ════════════════════════════════════════════════════════════
# TM-30-18 (fidelity Rf, gamut Rg, 16 hue bins, colour vector graphic) and CQS.
# These use the 'colour-science' package (validated against the IES calculator)
# and are OPTIONAL — every function returns None if colour-science is missing or
# the spectrum is degenerate, so the rest of the app is unaffected. The
# computation is relatively heavy (~0.1 s on a PC, more on a Pi), so callers
# should throttle / run it on demand rather than per frame.
# ════════════════════════════════════════════════════════════
def tm30_available() -> bool:
    try:
        import colour  # noqa: F401
        from colour.quality import colour_fidelity_index_ANSIIESTM3018  # noqa
        return True
    except Exception:
        return False


def _colour_sd(wavelengths_nm, intensities):
    import colour
    from colour import SpectralDistribution, SpectralShape
    wl = np.asarray(wavelengths_nm, dtype=float)
    inten = np.asarray(intensities, dtype=float)
    order = np.argsort(wl)
    wl, inten = wl[order], inten[order]
    # de-duplicate wavelengths (SpectralDistribution needs unique keys)
    uniq, idx = np.unique(wl, return_index=True)
    sd = SpectralDistribution(dict(zip(uniq.tolist(), inten[idx].tolist())))
    return sd.align(SpectralShape(360, 830, 1))


def _fl(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return float("nan")


# 16 hue-bin display colours for the colour-vector graphic and the per-sample
# bar chart (even hue wheel — bin 1 starts at red, going counter-clockwise).
TM30_BIN_COLORS = [
    "#E62B2B", "#E66B1E", "#E0991B", "#D8C637", "#A9C73B", "#5FB13C",
    "#3CA86B", "#34A39A", "#2E8FB8", "#2E6BC0", "#4A4FC0", "#7A3CC0",
    "#A82EA8", "#C02E7A", "#C72E55", "#E0314A",
]


def tm30_metrics(wavelengths_nm, intensities) -> dict | None:
    """IES TM-30-18 metrics. Returns a dict with Rf, Rg, CCT, Duv, the 99
    per-sample fidelity values (Rs) and their hue-bin index (bins), the 16
    per-hue-bin fidelity / chroma-shift / hue-shift (Rfhj/Rcshj/Rhshj), and the
    16 binned (a',b') chromaticity averages for the colour-vector graphic
    (avg_test / avg_ref). None if colour-science is missing or the SPD is empty."""
    try:
        from colour.quality import colour_fidelity_index_ANSIIESTM3018 as _tm30
    except Exception:
        return None
    inten = np.asarray(intensities, dtype=float)
    if inten.size == 0 or not np.isfinite(np.nansum(inten)) or np.nansum(inten) <= 0:
        return None
    try:
        s = _tm30(_colour_sd(wavelengths_nm, inten), additional_data=True)
    except Exception:
        return None
    cct_v, duv_v = _fl(s.CCT), _fl(s.D_uv)
    if not _valid_cct(cct_v, duv_v):
        cct_v = float('nan')      # off-locus → CCT undefined (shown as "—")
    return {
        "Rf": _fl(s.R_f), "Rg": _fl(s.R_g),
        "CCT": cct_v, "Duv": duv_v,
        "Rs":    [_fl(v) for v in np.asarray(s.R_s).ravel()],
        "Rs_hex": _ces_sample_hex(s),
        "bins":  [int(b) for b in np.asarray(s.bins).ravel()],
        "Rfhj":  [_fl(v) for v in np.asarray(s.R_fs).ravel()],
        "Rcshj": [_fl(v) for v in np.asarray(s.R_cs).ravel()],
        "Rhshj": [_fl(v) for v in np.asarray(s.R_hs).ravel()],
        "avg_test": np.asarray(s.averages_test, dtype=float).tolist(),
        "avg_ref":  np.asarray(s.averages_reference, dtype=float).tolist(),
    }


def _ces_sample_hex(spec) -> "list[str] | None":
    """Per-CES bar colours: the *reference* appearance (stable across sources) of
    all 99 colour-evaluation samples, from the TM-30 spec's colorimetry data,
    Bradford-adapted to D65. Returns None if the data can't be derived (the bars
    then fall back to a synthetic ramp). Uses the CIE 1964 10° observer (the
    TM-30 observer) for the reference white."""
    try:
        ref_xyz = np.asarray(spec.colorimetry_data[1].XYZ, dtype=float)  # (99,3)
        if ref_xyz.ndim != 2 or ref_xyz.shape[1] != 3:
            return None
        try:
            from colour.colorimetry import sd_to_XYZ, MSDS_CMFS
            cmfs = MSDS_CMFS["CIE 1964 10 Degree Standard Observer"].copy().align(
                spec.sd_reference.shape)
            wp = np.asarray(sd_to_XYZ(spec.sd_reference, cmfs), dtype=float)
            wp = wp * (100.0 / max(wp[1], 1e-12))
            ref_xyz = _adapt_xyz_to_d65(ref_xyz, wp)
        except Exception:
            pass                       # no white → draw un-adapted (still sane)
        return _xyz_to_srgb_hex(ref_xyz)
    except Exception:
        return None


def cqs_metrics(wavelengths_nm, intensities) -> dict | None:
    """Color Quality Scale (NIST). Returns Qa/Qf/Qg/Qp or None."""
    try:
        from colour.quality import colour_quality_scale as _cqs
    except Exception:
        return None
    inten = np.asarray(intensities, dtype=float)
    if inten.size == 0 or not np.isfinite(np.nansum(inten)) or np.nansum(inten) <= 0:
        return None
    try:
        c = _cqs(_colour_sd(wavelengths_nm, inten), additional_data=True)
    except Exception:
        return None
    return {"Qa": _fl(c.Q_a), "Qf": _fl(c.Q_f),
            "Qg": _fl(c.Q_g), "Qp": _fl(c.Q_p)}

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
    # Guard against overflow at low T / short wavelength
    b = np.clip(b, 0.0, 700.0)
    return a / (np.exp(b) - 1.0)


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
    Returns (Ra, [R1..R8], CCT_used).

    Method: CIE 13.3-1995. Uses the CIE 1960 UCS chromatic adaptation
    transform (Judd's correction) and the 8 general TCS samples.
    """
    spd_test = resample_spd(wavelengths_nm, intensities)
    if np.sum(spd_test) <= 0:
        return float('nan'), [float('nan')] * 8, float('nan')

    # CCT of test source
    X_t, Y_t, Z_t = spectrum_to_xyz(CIE_WL_5, spd_test)
    x_t, y_t = xyz_to_xy(X_t, Y_t, Z_t)
    cct = cct_mccamy(x_t, y_t)
    if not np.isfinite(cct) or cct <= 0:
        return float('nan'), [float('nan')] * 8, float('nan')

    # Reference illuminant at the same CCT, normalised so Y_r = Y_t
    spd_ref = reference_illuminant_spd(cct)
    X_r, Y_r, Z_r = spectrum_to_xyz(CIE_WL_5, spd_ref)
    if Y_r > 0:
        spd_ref = spd_ref * (Y_t / Y_r)
        X_r, Y_r, Z_r = spectrum_to_xyz(CIE_WL_5, spd_ref)

    u_t, v_t = xy_to_uv60(x_t, y_t)
    x_r, y_r = xyz_to_xy(X_r, Y_r, Z_r)
    u_r, v_r = xy_to_uv60(x_r, y_r)

    Ri: list[float] = []
    for k in range(8):
        # XYZ of TCSk under test and reference
        X_kt, Y_kt, Z_kt = _xyz_under(spd_test, TCS[k])
        X_kr, Y_kr, Z_kr = _xyz_under(spd_ref,  TCS[k])
        x_kt, y_kt = xyz_to_xy(X_kt, Y_kt, Z_kt)
        x_kr, y_kr = xyz_to_xy(X_kr, Y_kr, Z_kr)
        u_kt, v_kt = xy_to_uv60(x_kt, y_kt)
        u_kr, v_kr = xy_to_uv60(x_kr, y_kr)

        # Chromatic-adapt the test colour to reference white-point
        u_kt_a, v_kt_a = _adapt_uv(u_kt, v_kt, u_t, v_t, u_r, v_r)

        # U*V*W* (normalise Y to ref white = 100)
        Y_kt_n = 100.0 * Y_kt / max(Y_t, 1e-9)
        Y_kr_n = 100.0 * Y_kr / max(Y_r, 1e-9)
        U_t, V_t, W_t = _UVW(Y_kt_n, u_kt_a, v_kt_a, u_r, v_r)
        U_r, V_r, W_r = _UVW(Y_kr_n, u_kr,  v_kr,  u_r, v_r)

        dE = np.sqrt((U_t - U_r) ** 2 + (V_t - V_r) ** 2 + (W_t - W_r) ** 2)
        Ri.append(100.0 - 4.6 * float(dE))

    Ra = float(np.mean(Ri))
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
        Ra, Ri = float('nan'), [float('nan')] * 8
    return dict(X=X, Y=Y, Z=Z, x=x, y=y,
                cct=cct, duv=duv, Ra=Ra, Ri=Ri)

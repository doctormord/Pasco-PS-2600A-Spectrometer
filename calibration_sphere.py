"""
calibration_sphere.py — Integrating sphere as a radiance reference (Qt-free).

An integrating sphere with a Lambertian exit port of radius r (knife-edge
aperture) is a disk source of uniform radiance L(λ). On the port axis, at the
distance d from the knife-edge plane, a cosine corrector (CC) measures

    E(λ, d) = π · L(λ) · r² / (r² + d²)          (exact for a Lambertian disk)

so with E from an absolute irradiance profile (CC) the radiance follows as

    L(λ) = E(λ, d) · (r² + d²) / (π · r²).

Geometry of the printed sphere (all values measured at the part, entered by
the user):
    a   distance knife-edge plane → top surface of the housing (default 11.00 mm;
        the port lies BELOW the top surface)
    h_i distance top surface → CC support surface at position i (spacers)
    c   CC reference plane relative to its support surface (default 0;
        positive = farther from the port)
    d_i = a + h_i + c

Evaluation per wavelength:
  * L_i = E_i · (r² + d_i²) / (π r²) for every distance; their spread (per
    50-nm band and overall) measures the uniformity / Lambertian behaviour.
  * Least-squares fit of E(d) = π·L·r² / (r² + (d + d0)²) with a free offset d0
    shared by all wavelengths (overall) and per band; fitted in log space
    (relative residuals). Used L = fit result when |d0| < d0_max_mm, else the
    mean of L_i with nominal distances and a warning.
  * Simplified GUM propagation (relative, uncorrelated inputs):
        u²(L)/L² = (c_r·u_r)² + (c_d·u_d)² + (s/√N)² + u_drift² + u_CC²
    c_r = mean_i 2 d_i² / (r (r² + d_i²)),  c_d = mean_i 2 d_i / (r² + d_i²),
    s = relative spread of L_i over the distances, u_drift = |drift| / √3
    (rectangular), u_CC = relative standard uncertainty of the CC calibration.

The run (SphereCharacterization) measures E with the CC at N ≥ 3 distances at
ONE integration time (auto exposure at the first distance — start with the
smallest distance, the brightest), darks before and after the block
(time-interpolated), and a final repeat of the first distance (source drift).
The result is stored as a reference source with quantity 'radiance'.
"""

from __future__ import annotations

import math
import time
from dataclasses import asdict, dataclass

import numpy as np

import calibration_capture as cc
import calibration_profiles as cp
import reference_sources as rs


# ══════════════════════════════════════════════════════════════════════════
#  Configuration
# ══════════════════════════════════════════════════════════════════════════

DEFAULT_EDGE_DEPTH_MM = 11.0       # knife-edge plane below the housing top surface


@dataclass
class SphereConfig:
    n_frames: int = 16
    snr_k: float = 10.0
    smooth_px: int = 5
    target_frac: tuple = (0.70, 0.85)
    saturation_warn_frac: float = 0.90
    min_positions: int = 3
    band_nm: float = 50.0
    consistency_max_pct: float = 2.0      # spread of L_i over the distances
    d0_max_mm: float = 1.0                # plausible |d0| of the fit
    drift_max_pct: float = 1.0            # repeat of the first distance (p95)
    cc_uncertainty_pct: float = 3.0       # default u(CC calibration), relative, k=1
    itime_match_tol_pct: float = 0.5      # dark and light "same integration time"
    dark_bright_frac: float = 0.5         # "dark unusually bright" warning factor

    @classmethod
    def from_config(cls) -> "SphereConfig":
        try:
            from app_config import Config as C
        except Exception:                                   # noqa: BLE001
            return cls()
        try:
            import processing
            sat = float(processing.saturation_warn_frac())
        except Exception:                                   # noqa: BLE001
            sat = 0.90
        lo = float(C.get("calibration_target_frac_lo", 0.70))
        hi = min(float(C.get("calibration_target_frac_hi", 0.85)), sat)
        if lo >= hi:
            lo = 0.8 * hi
        return cls(
            n_frames=max(2, int(C.get("calibration_capture_frames", 16))),
            snr_k=float(C.get("calibration_snr_k", 10.0)),
            smooth_px=int(C.get("calibration_smooth_px", 5)),
            target_frac=(lo, hi),
            saturation_warn_frac=sat,
            min_positions=max(3, int(C.get("sphere_min_positions", 3))),
            band_nm=float(C.get("sphere_band_nm", 50.0)),
            consistency_max_pct=float(C.get("sphere_consistency_max_pct", 2.0)),
            d0_max_mm=float(C.get("sphere_d0_max_mm", 1.0)),
            drift_max_pct=float(C.get("sphere_drift_max_pct", 1.0)),
            cc_uncertainty_pct=float(C.get("sphere_cc_uncertainty_pct", 3.0)),
            itime_match_tol_pct=float(C.get("calibration_itime_match_tol_pct", 0.5)),
            dark_bright_frac=float(C.get("calibration_dark_bright_frac", 0.5)),
        )


# ══════════════════════════════════════════════════════════════════════════
#  Geometry and the Lambertian disk model
# ══════════════════════════════════════════════════════════════════════════

def disk_factor(r_mm: float, d_mm):
    """g = r² / (r² + d²): E = π · L · g on the axis of a Lambertian disk."""
    r2 = float(r_mm) ** 2
    d = np.asarray(d_mm, float)
    return r2 / (r2 + d * d)


def radiance_from_irradiance(e, r_mm: float, d_mm: float):
    """L = E · (r² + d²) / (π r²) (same prefix unit, per sr)."""
    return np.asarray(e, float) / (math.pi * disk_factor(r_mm, d_mm))


def port_distance_mm(height_mm: float, *, edge_depth_mm: float = DEFAULT_EDGE_DEPTH_MM,
                     cc_ref_offset_mm: float = 0.0) -> float:
    """d = a + h + c (see module doc)."""
    return float(edge_depth_mm) + float(height_mm) + float(cc_ref_offset_mm)


def _band_edges(wl_valid: np.ndarray, band_nm: float):
    b0 = math.floor(float(wl_valid.min()) / band_nm) * band_nm
    b1 = math.ceil(float(wl_valid.max()) / band_nm) * band_nm
    if b1 <= b0:
        b1 = b0 + band_nm
    return np.arange(b0, b1, band_nm)


def _fit_d0(logE: np.ndarray, d: np.ndarray, r: float, bounds: tuple[float, float]):
    """Common offset d0 for the columns of logE (N distances × M pixels):
    minimises Σ_px Σ_i (ln E_i − ln g_i(d0) − a_px)², a_px free (= ln πL).
    Returns (d0, rms of the relative residual, a per pixel)."""
    from scipy.optimize import minimize_scalar
    yc = logE - logE.mean(axis=0, keepdims=True)

    def cost(d0):
        h = np.log(disk_factor(r, d + d0))
        hc = h - h.mean()
        res = yc - hc[:, None]
        return float(np.sum(res * res))

    lo, hi = bounds
    # coarse scan first (the cost can have a shallow second minimum), then refine
    grid = np.linspace(lo, hi, 81)
    costs = np.array([cost(g) for g in grid])
    k = int(np.argmin(costs))
    a = grid[max(k - 1, 0)]
    b = grid[min(k + 1, grid.size - 1)]
    if b > a:
        opt = minimize_scalar(cost, bounds=(a, b), method="bounded",
                              options={"xatol": 1e-6})
        d0 = float(opt.x)
    else:
        d0 = float(grid[k])
    h = np.log(disk_factor(r, d + d0))
    apx = (logE - h[:, None]).mean(axis=0)
    n = logE.size
    rms = math.sqrt(cost(d0) / max(n, 1))
    return d0, rms, apx


def _spread_pct(L: np.ndarray) -> np.ndarray:
    """Relative spread (sample std / mean, %) over axis 0."""
    n = L.shape[0]
    with np.errstate(divide="ignore", invalid="ignore"):
        return 100.0 * np.std(L, axis=0, ddof=1 if n > 1 else 0) / np.mean(L, axis=0)


def characterize(wl, E, d_mm, r_mm: float, *, valid=None, band_nm: float = 50.0,
                 consistency_max_pct: float = 2.0, d0_max_mm: float = 1.0,
                 u_r_mm: float = 0.0, u_d_mm: float = 0.0,
                 cc_uncertainty_pct: float = 0.0, drift_pct: float = 0.0) -> dict:
    """Radiance of a Lambertian port from CC irradiance at N distances.

    wl : (M,) nm.  E : (N, M) irradiance (any absolute unit; L comes out in the
    same prefix unit per sr).  d_mm : (N,) distances knife-edge plane → CC
    reference plane.  valid : optional (M,) bool (e.g. CC profile valid range).
    drift_pct : signed mean drift (repeat / first − 1) in % for the uncertainty.

    Returns dict with per-pixel arrays (NaN where not valid) "L", "L_fit",
    "L_mean", "L_i" (N×M, nominal d), "spread_pct", "u_rel", and the summaries
    "d0_mm", "d0_plausible", "bands", "overall", "consistency_pass", "method",
    "uncertainty", "warnings"."""
    wl = np.asarray(wl, float)
    E = np.atleast_2d(np.asarray(E, float))
    d = np.asarray(d_mm, float).ravel()
    r = float(r_mm)
    N, M = E.shape
    if d.size != N:
        raise ValueError("one distance per irradiance spectrum is needed")
    if N < 2:
        raise ValueError("at least two distances are needed")
    if not r > 0:
        raise ValueError("the port radius must be positive")
    if np.any(d < 0):
        raise ValueError("distances must not be negative")
    if len(set(np.round(d, 6))) < N:
        raise ValueError("the distances must differ")
    m = np.all(np.isfinite(E) & (E > 0), axis=0)
    if valid is not None:
        m &= np.asarray(valid, bool)
    if np.count_nonzero(m) < 3:
        raise ValueError("No valid pixels: the irradiance is not positive at all "
                         "distances inside the CC profile's valid range.")
    warnings: list[str] = []
    g_nom = disk_factor(r, d)                                     # (N,)
    L_i = np.full((N, M), np.nan)
    L_i[:, m] = E[:, m] / (math.pi * g_nom[:, None])

    # ── fit with free offset d0 (overall and per band) ──
    logE = np.log(E[:, m])
    # d + d0 stays positive; |d0| beyond a few mm is implausible anyway
    bounds = (-0.9 * float(d.min()) if d.min() > 0 else -1.0,
              max(5.0, 0.5 * float(d.max())))
    d0, rms_fit, apx = _fit_d0(logE, d, r, bounds)
    wl_m = wl[m]
    bands = []
    for lo in _band_edges(wl_m, band_nm):
        sel = (wl_m >= lo) & (wl_m < lo + band_nm)
        if np.count_nonzero(sel) < 1:
            continue
        b_d0, b_rms, _ = _fit_d0(logE[:, sel], d, r, bounds)
        bands.append({"lo": float(lo), "hi": float(lo + band_nm),
                      "n": int(np.count_nonzero(sel)), "d0_mm": b_d0,
                      "fit_rms_pct": 100.0 * b_rms})
    d0_ok = bool(abs(d0) < float(d0_max_mm))
    L_fit = np.full(M, np.nan)
    L_fit[m] = np.exp(apx) / math.pi
    L_mean = np.full(M, np.nan)
    L_mean[m] = np.mean(L_i[:, m], axis=0)
    if d0_ok:
        method = "fit"
        d_eff = d + d0
        L = L_fit
    else:
        method = "mean"
        d_eff = d
        L = L_mean
        warnings.append(f"Fitted offset d0 = {d0:+.3f} mm exceeds the plausibility "
                        f"limit ±{d0_max_mm:.2f} mm — the mean of L over the distances "
                        f"(nominal geometry) is used. Check the distances and the "
                        f"reference plane of the CC.")
    # ── consistency of L over the distances (with the geometry used) ──
    g_eff = disk_factor(r, d_eff)
    L_eff = np.full((N, M), np.nan)
    L_eff[:, m] = E[:, m] / (math.pi * g_eff[:, None])
    spread = np.full(M, np.nan)
    spread[m] = _spread_pct(L_eff[:, m])
    spread_nom = _spread_pct(L_i[:, m])

    def band_spread(Lmat, sel):
        per_d = np.mean(Lmat[:, sel], axis=1)                # band mean per distance
        return float(_spread_pct(per_d[:, None])[0])

    for b in bands:
        sel = (wl_m >= b["lo"]) & (wl_m < b["hi"])
        b["spread_pct"] = band_spread(L_eff[:, m], sel)
        b["spread_nominal_pct"] = band_spread(L_i[:, m], sel)
        b["pass"] = bool(b["spread_pct"] <= consistency_max_pct)
    all_sel = np.ones(wl_m.size, bool)
    overall = {"spread_pct": band_spread(L_eff[:, m], all_sel),
               "spread_nominal_pct": band_spread(L_i[:, m], all_sel),
               "spread_px_median_pct": float(np.median(spread[m])),
               "spread_px_nominal_median_pct": float(np.median(spread_nom)),
               "fit_rms_pct": 100.0 * rms_fit, "n_valid": int(np.count_nonzero(m))}
    cons_ok = bool(overall["spread_pct"] <= consistency_max_pct
                   and all(b["pass"] for b in bands))
    if not cons_ok:
        bad = [f"{b['lo']:.0f}–{b['hi']:.0f} nm ({b['spread_pct']:.2f} %)"
               for b in bands if not b["pass"]]
        warnings.append(
            f"Consistency check failed: spread of L over the distances "
            f"{overall['spread_pct']:.2f} % (limit {consistency_max_pct:.2f} %)"
            + (f"; bands: {', '.join(bad)}" if bad else "")
            + " — the port is not a uniform Lambertian source, or the distances "
              "are wrong.")
    # ── uncertainty (relative, k = 1) ──
    cr = float(np.mean(2.0 * d_eff ** 2 / (r * (r * r + d_eff ** 2))))
    cd = float(np.mean(2.0 * d_eff / (r * r + d_eff ** 2)))
    u_r = cr * float(u_r_mm)
    u_d = cd * float(u_d_mm)
    u_sp = np.full(M, np.nan)
    u_sp[m] = spread[m] / 100.0 / math.sqrt(N)
    u_dr = abs(float(drift_pct)) / 100.0 / math.sqrt(3.0)
    u_cc = float(cc_uncertainty_pct) / 100.0
    # Offset d0: on the axis, a shift of the reference planes and a slightly
    # non-Lambertian port (L ∝ cosⁿθ) look alike — both are absorbed by d0
    # without raising the spread. The fitted offset is therefore carried as
    # an uncertainty (rectangular, half-width c_d·|d0|); for a cosⁿ port this
    # covers the bias of L at k = 1.
    u_d0 = cd * abs(float(d0)) / math.sqrt(3.0)
    d0_note_lim = max(2.0 * float(u_d_mm), 0.1)
    if abs(d0) > d0_note_lim:
        warnings.append(
            f"Fitted offset d0 = {d0:+.3f} mm is larger than "
            f"{d0_note_lim:.2f} mm (twice the distance uncertainty): either the "
            f"reference planes are off or the port is not exactly Lambertian — "
            f"both look alike on the axis. Its effect is included in the "
            f"uncertainty ({100.0 * u_d0:.2f} %).")
    u_rel = np.sqrt(u_r ** 2 + u_d ** 2 + u_sp ** 2 + u_dr ** 2 + u_cc ** 2 + u_d0 ** 2)
    for b in bands:
        sel = (wl_m >= b["lo"]) & (wl_m < b["hi"])
        b["u_rel_pct"] = float(100.0 * np.mean(u_rel[m][sel]))
    unc = {"c_r_per_mm": cr, "c_d_per_mm": cd,
           "u_r_mm": float(u_r_mm), "u_d_mm": float(u_d_mm),
           "u_rel_r_pct": 100.0 * u_r, "u_rel_d_pct": 100.0 * u_d,
           "u_rel_spread_median_pct": float(100.0 * np.median(u_sp[m])),
           "u_rel_drift_pct": 100.0 * u_dr, "u_rel_cc_pct": 100.0 * u_cc,
           "u_rel_d0_pct": 100.0 * u_d0,
           "u_rel_median_pct": float(100.0 * np.median(u_rel[m])),
           "u_rel_max_pct": float(100.0 * np.max(u_rel[m])),
           "U_rel_median_pct_k2": float(200.0 * np.median(u_rel[m])),
           "drift_pct": float(drift_pct), "k": 1}
    return {"wl": wl, "valid": m, "L": L, "L_fit": L_fit, "L_mean": L_mean,
            "L_i": L_i, "spread_pct": spread, "u_rel": np.where(m, u_rel, np.nan),
            "d_mm": d.tolist(), "d_eff_mm": d_eff.tolist(), "r_mm": r,
            "d0_mm": d0, "d0_plausible": d0_ok, "d0_max_mm": float(d0_max_mm),
            "method": method, "bands": bands, "overall": overall,
            "consistency_max_pct": float(consistency_max_pct),
            "consistency_pass": cons_ok, "uncertainty": unc, "warnings": warnings}


# ══════════════════════════════════════════════════════════════════════════
#  Prerequisite: active absolute irradiance profile with a CC
# ══════════════════════════════════════════════════════════════════════════

def cc_profile_for(backend, backend_name: str) -> dict:
    """The ACTIVE profile of the connected unit, if it is an absolute
    irradiance profile with a cosine corrector valid with the current
    wavelength calibration. Raises ValueError with a clear message otherwise."""
    sel = cp.active_selection(backend_name)
    if sel in (cp.SEL_NONE, cp.SEL_DEVICE_DEFAULT):
        raise ValueError("No active calibration profile. Activate an absolute "
                         "irradiance profile with cosine corrector (input CC) for "
                         "this device first.")
    p = cp.get_profile_by_id(sel)
    if p is None:
        raise ValueError("The active calibration profile was not found.")
    if p.get("type") != cp.TYPE_ABSOLUTE or cp.profile_quantity(p) != cp.QUANTITY_IRRADIANCE:
        raise ValueError("The active profile is not an absolute irradiance profile. "
                         "Activate an absolute irradiance profile with cosine "
                         "corrector (input CC).")
    if (p.get("optics") or {}).get("input") != "CC":
        raise ValueError("The active absolute irradiance profile does not use a "
                         "cosine corrector (input CC).")
    if p.get("apply") != cp.APPLY_V2:
        raise ValueError("Legacy profiles cannot be used for the sphere "
                         "characterisation.")
    st = cp.profile_status(p, backend_name, getattr(backend, "serial", None),
                           backend.wl_fingerprint(), cp.device_nl_active(backend))
    if st["state"] in cp.CONFIRM_STATES + (cp.STATE_OTHER_DEVICE,):
        raise ValueError(f"The active CC profile is not valid on this device: "
                         f"{st['message']}")
    return p


# ══════════════════════════════════════════════════════════════════════════
#  The run
# ══════════════════════════════════════════════════════════════════════════

# User steps (desktop wizard, API job) in their order. cover/uncover only on
# devices without a shutter. "position" repeats for every distance.
SPHERE_STEPS = (
    ("mount", "Place the CC on the sphere port (first, smallest distance)"),
    ("integration", "Integration time (first distance)"),
    ("dark", "Dark spectrum before"),
    ("position", "Irradiance at each distance"),
    ("repeat", "Repeat of the first distance (source drift)"),
    ("dark_after", "Dark spectrum after"),
    ("compute", "Compute L"),
    ("save", "Save the reference source"),
)

SPHERE_SOURCE_HINT = (
    "Sphere port facing up, CC centred on the port axis, its support surface "
    "parallel to the housing top. Enter the distance housing top → CC support "
    "surface for every position (spacers). Start with the smallest distance. "
    "Keep the sphere lamp and its power unchanged during the run.")


class SphereCharacterization:
    """State of one sphere characterisation run (see module doc)."""

    def __init__(self, backend, backend_name: str, *, sphere: dict,
                 cfg: SphereConfig | None = None, library=None):
        self.backend = backend
        self.backend_name = backend_name
        self.cfg = cfg or SphereConfig.from_config()
        self.cc_profile = cc_profile_for(backend, backend_name)
        name = str(sphere.get("name", "")).strip()
        if not name:
            raise ValueError("Name the sphere.")
        r = float(sphere.get("port_radius_mm", 0.0))
        if not r > 0:
            raise ValueError("The port radius must be positive (measured at the part).")
        a = float(sphere.get("edge_depth_mm", DEFAULT_EDGE_DEPTH_MM))
        if a < 0:
            raise ValueError("The distance knife-edge plane → housing top must not be "
                             "negative.")
        u_cc = sphere.get("cc_uncertainty_pct")
        self.sphere = {
            "name": name, "serial": str(sphere.get("serial", "")).strip(),
            "port_radius_mm": r,
            "port_diameter_mm": 2.0 * r,
            "edge_depth_mm": a,
            "cc_ref_offset_mm": float(sphere.get("cc_ref_offset_mm", 0.0)),
            "u_radius_mm": abs(float(sphere.get("u_radius_mm", 0.0))),
            "u_distance_mm": abs(float(sphere.get("u_distance_mm", 0.0))),
            "cc_uncertainty_pct": abs(float(self.cfg.cc_uncertainty_pct if u_cc is None
                                            else u_cc)),
            "notes": str(sphere.get("notes", "")),
        }
        self._library = library
        self.wl_fp = backend.wl_fingerprint()
        self.applier = cp.CalibrationApplier(self.cc_profile, backend.wavelength_array,
                                             rs.UNIT_UW_CM2_NM)
        self.integration_us: int | None = None
        self.exposure: dict | None = None
        self.dark = self.dark_after = None
        self.positions: list[dict] = []       # {"height_mm", "d_mm", "capture"}
        self.repeat = None
        self.E: list[np.ndarray] = []
        self.E_repeat = None
        self.drift: dict | None = None
        self.result: dict | None = None
        self.checks: dict = {}
        self.warnings: list[str] = []
        self.saved_source: rs.ReferenceSource | None = None
        self.started = cc.now_iso()

    # ── helpers ──────────────────────────────────────────────────────────
    @property
    def needs_user_cover(self) -> bool:
        return not getattr(self.backend, "SUPPORTS_SHUTTER", False)

    def _warn(self, msg) -> None:
        if msg and msg not in self.warnings:
            self.warnings.append(msg)

    def _check_fp(self) -> None:
        if self.backend.wl_fingerprint() != self.wl_fp:
            raise RuntimeError("The wavelength calibration changed during the run — "
                               "restart the sphere characterisation.")

    def _same_t(self, a: int, b: int) -> bool:
        return abs(int(a) - int(b)) <= self.cfg.itime_match_tol_pct / 100.0 * max(int(a), 1)

    def distance_mm(self, height_mm: float) -> float:
        return port_distance_mm(height_mm, edge_depth_mm=self.sphere["edge_depth_mm"],
                                cc_ref_offset_mm=self.sphere["cc_ref_offset_mm"])

    @property
    def library(self):
        if self._library is None:
            self._library = rs.ReferenceLibrary()
        return self._library

    # ── steps ────────────────────────────────────────────────────────────
    def set_integration(self, integration_us: int | None = None, *, auto: bool = True) -> dict:
        """One integration time for the whole run: auto exposure with the CC
        at the FIRST (smallest, brightest) distance, or a fixed value."""
        b = self.backend
        cap, converged = None, True
        if integration_us is None and auto:
            t, cap, converged = cc.auto_expose(b, target_frac=self.cfg.target_frac,
                                               backend_name=self.backend_name)
        else:
            b.set_integration_time_us(int(integration_us or b.current_integration_time_us))
            t = int(b.current_integration_time_us)
        if self.integration_us is not None and not self._same_t(self.integration_us, t):
            # new t_int: earlier captures are no longer comparable
            self.dark = self.dark_after = self.repeat = None
            self.positions, self.E, self.E_repeat = [], [], None
            self.result = self.drift = None
        self.integration_us = int(t)
        self.exposure = {"integration_us": int(t), "converged": bool(converged),
                         "auto": integration_us is None and auto,
                         "peak_frac": None if cap is None else float(cap.peak_frac)}
        if not converged:
            self._warn(f"Auto exposure did not reach the target band "
                       f"{self.cfg.target_frac[0]*100:.0f}–"
                       f"{self.cfg.target_frac[1]*100:.0f} % FS.")
        return dict(self.exposure)

    def capture_dark(self, phase: str = "before"):
        if self.integration_us is None:
            raise RuntimeError("set the integration time first")
        if phase not in ("before", "after"):
            raise ValueError("phase must be 'before' or 'after'")
        if phase == "after" and self.dark is None:
            raise RuntimeError("capture the dark spectrum before first")
        d = cc.capture_dark(self.backend, self.cfg.n_frames,
                            integration_us=self.integration_us,
                            backend_name=self.backend_name)
        if not self._same_t(d.integration_us, self.integration_us):
            raise RuntimeError("The device ran the dark at another integration time — "
                               "repeat the dark.")
        cc.save_dark(d)
        if phase == "before":
            self.dark, self.dark_after = d, None
        else:
            self.dark_after = d
        self.backend.set_integration_time_us(self.integration_us)
        return d

    def _capture_light(self, what: str):
        if self.dark is None:
            raise RuntimeError("capture the dark spectrum first")
        self._check_fp()
        light = cc.capture(self.backend, self.cfg.n_frames,
                           integration_us=self.integration_us,
                           backend_name=self.backend_name)
        if not self._same_t(light.integration_us, self.integration_us):
            raise RuntimeError("Dark and light were taken at different integration times.")
        if light.n_saturated:
            raise RuntimeError(f"{light.n_saturated} pixel(s) saturated {what} — start "
                               f"again with the smallest distance first or a shorter "
                               f"integration time.")
        if self.dark.peak_frac > self.cfg.dark_bright_frac * max(light.peak_frac, 1e-9):
            self._warn(f"The dark spectrum is unusually bright compared to the light "
                       f"spectrum {what} (factor {self.cfg.dark_bright_frac:g}) — was "
                       f"the light blocked during the dark?")
        if light.peak_frac >= self.cfg.saturation_warn_frac:
            self._warn(f"Exposure {what}: raw peak at {light.peak_frac*100:.1f} % of full "
                       f"scale (saturation warning threshold "
                       f"{self.cfg.saturation_warn_frac*100:.0f} %).")
        return light

    def capture_position(self, height_mm: float) -> dict:
        """CC at the distance housing top → CC support surface = height_mm."""
        h = float(height_mm)
        if h < -self.sphere["edge_depth_mm"]:
            raise ValueError("The CC cannot lie below the knife-edge plane.")
        if any(abs(p["height_mm"] - h) < 1e-6 for p in self.positions):
            raise ValueError(f"Distance {h:g} mm was already measured; use another "
                             f"spacer height.")
        d = self.distance_mm(h)
        light = self._capture_light(f"at {h:.2f} mm")
        self.positions.append({"height_mm": h, "d_mm": d, "capture": light})
        self.result = None
        return {"index": len(self.positions) - 1, "height_mm": h, "d_mm": d,
                "peak_frac": float(light.peak_frac)}

    def capture_repeat(self) -> dict:
        """Repeat of the FIRST distance at the end (source drift)."""
        if not self.positions:
            raise RuntimeError("measure the distances first")
        h = self.positions[0]["height_mm"]
        self.repeat = self._capture_light(f"of the repeat at {h:.2f} mm")
        self.result = None
        return {"height_mm": h, "peak_frac": float(self.repeat.peak_frac)}

    # ── evaluation ───────────────────────────────────────────────────────
    def _irradiance(self, light):
        dark = cc.interpolate_dark(self.dark, self.dark_after, light.t_mid)
        rate = cc.count_rate(light, dark)
        for w in rate.warnings:
            self._warn(w)
        e = self.applier.apply(rate.rate)
        ok = (self.applier.valid & ~rate.saturated
              & (rate.rate > self.cfg.snr_k * rate.sigma_dark))
        return np.where(ok, e, np.nan), rate

    def compute(self) -> dict:
        n = len(self.positions)
        if n < self.cfg.min_positions:
            raise RuntimeError(f"At least {self.cfg.min_positions} distances are needed "
                               f"({n} measured).")
        if self.dark_after is None:
            self._warn("No dark after the measurements — the dark before is used.")
        wl = np.asarray(self.backend.wavelength_array, float)
        E = []
        for p in self.positions:
            e, _ = self._irradiance(p["capture"])
            E.append(e)
        drift_mean = 0.0
        self.drift = None
        if self.repeat is not None:
            e_rep, _ = self._irradiance(self.repeat)
            self.E_repeat = e_rep
            m = np.isfinite(E[0]) & np.isfinite(e_rep) & (E[0] > 0)
            # Smoothed over the evaluation band (sphere_band_nm): a source drift
            # is spectrally smooth, while the shot noise of the calibrated
            # spectrum (amplified where the CC sensitivity is low) is not and
            # would otherwise dominate the percentile near the limit.
            pitch = float(np.nanmedian(np.abs(np.diff(wl)))) if wl.size > 1 else 1.0
            smooth = max(21, int(round(self.cfg.band_nm / pitch)) | 1) if pitch > 0 else 21
            st = cp.compare_rates(np.nan_to_num(E[0]), np.nan_to_num(e_rep), m,
                                  smooth_px=smooth)
            drift_mean = (100.0 * (st["mean_ratio"] - 1.0)
                          if np.isfinite(st["mean_ratio"]) else float("nan"))
            p95 = st["p95_pct"]
            passed = bool(np.isfinite(p95) and p95 <= self.cfg.drift_max_pct)
            t1, t2 = self.positions[0]["capture"].t_mid, self.repeat.t_mid
            self.drift = {"p95_pct": float(p95) if np.isfinite(p95) else None,
                          "mean_pct": float(drift_mean) if np.isfinite(drift_mean) else None,
                          "limit_pct": float(self.cfg.drift_max_pct), "pass": passed,
                          "seconds": float(t2 - t1) if t1 and t2 else None,
                          "n": int(st["n"])}
            self.checks["drift_pass"] = passed
            if self.drift["p95_pct"] is None:
                self._warn("Drift could not be evaluated (no common valid pixels of the "
                           "first distance and its repeat) — drift check failed; the "
                           "uncertainty contains no drift term.")
            elif not passed:
                self._warn(f"Drift check failed: {self.drift['p95_pct']:.2f} % "
                           f"(limit {self.cfg.drift_max_pct:.2f} %) between the first "
                           f"distance and its repeat — keep the sphere lamp stable "
                           f"(warm-up) and repeat the run.")
            # first distance: mean of the capture and its repeat (mid-run level)
            E[0] = np.where(np.isfinite(e_rep), 0.5 * (E[0] + e_rep), E[0])
            if not np.isfinite(drift_mean):
                drift_mean = 0.0
        else:
            self.checks["drift_pass"] = None
            self._warn("Source drift not checked (no repeat of the first distance).")
        d = [p["d_mm"] for p in self.positions]
        self.E = E
        res = characterize(
            wl, np.vstack(E), d, self.sphere["port_radius_mm"],
            valid=self.applier.valid, band_nm=self.cfg.band_nm,
            consistency_max_pct=self.cfg.consistency_max_pct,
            d0_max_mm=self.cfg.d0_max_mm, u_r_mm=self.sphere["u_radius_mm"],
            u_d_mm=self.sphere["u_distance_mm"],
            cc_uncertainty_pct=self.sphere["cc_uncertainty_pct"], drift_pct=drift_mean)
        for w in res["warnings"]:
            self._warn(w)
        # optional smoothing (same window as the spectral calibration)
        Ls = cp._nan_moving_average(res["L"], res["valid"], self.cfg.smooth_px)
        res["L_smoothed"] = np.where(res["valid"], Ls, np.nan)
        # stored range: largest contiguous valid run
        seg = _largest_run(res["valid"])
        if seg is None:
            raise ValueError("No contiguous valid wavelength range.")
        i0, i1 = seg
        res["valid_range_nm"] = [float(wl[i0]), float(wl[i1])]
        res["store_slice"] = (i0, i1)
        self.checks.update({"consistency_pass": res["consistency_pass"],
                            "d0_plausible": res["d0_plausible"],
                            "d0_mm": res["d0_mm"], "method": res["method"]})
        self.result = res
        return res

    @property
    def checks_passed(self) -> bool:
        return not any(self.checks.get(k) is False
                       for k in ("drift_pass", "consistency_pass"))

    # ── save / protocol ──────────────────────────────────────────────────
    def reference_source(self) -> rs.ReferenceSource:
        if self.result is None:
            raise RuntimeError("compute first")
        res = self.result
        i0, i1 = res["store_slice"]
        wl = np.asarray(res["wl"], float)[i0:i1 + 1]
        L = np.asarray(res["L_smoothed"], float)[i0:i1 + 1]
        u = (np.asarray(res["u_rel"], float) * np.asarray(res["L_smoothed"], float))[i0:i1 + 1]
        ok = np.isfinite(L) & np.isfinite(u)
        wl, L, u = wl[ok], L[ok], u[ok]
        stamp = time.strftime("%Y%m%d-%H%M%S")
        s = self.sphere
        k = self.cc_profile
        meta = {
            "kind": "sphere_characterisation",
            "geometry": {"port_radius_mm": s["port_radius_mm"],
                         "port_diameter_mm": s["port_diameter_mm"],
                         "edge_depth_mm": s["edge_depth_mm"],
                         "cc_ref_offset_mm": s["cc_ref_offset_mm"],
                         "u_radius_mm": s["u_radius_mm"],
                         "u_distance_mm": s["u_distance_mm"],
                         "heights_mm": [p["height_mm"] for p in self.positions],
                         "distances_mm": [p["d_mm"] for p in self.positions],
                         "d0_mm": res["d0_mm"], "method": res["method"]},
            "date": cc.now_iso(),
            "device": {"backend": self.backend_name,
                       "serial": getattr(self.backend, "serial", None)},
            "cc_profile": {"id": k.get("id"), "version": k.get("version"),
                           "optics": (k.get("optics") or {}).get("name"),
                           "reference": (k.get("reference") or {}).get("name")},
            "consistency_pct": res["overall"]["spread_pct"],
            "consistency_pass": res["consistency_pass"],
            "drift": self.drift,
            "uncertainty": res["uncertainty"],
        }
        return rs.ReferenceSource(
            id=rs._slug(s["name"], s["serial"], "radiance", stamp),
            name=s["name"], serial=s["serial"], unit=rs.UNIT_UW_CM2_SR_NM,
            quantity=rs.QUANTITY_RADIANCE,
            valid_min_nm=float(wl.min()), valid_max_nm=float(wl.max()),
            input_optic="", source_file="sphere characterisation",
            imported=cc.now_iso(), wl=wl, values=L,
            notes=(f"Radiance of the sphere port (Ø {s['port_diameter_mm']:.2f} mm), "
                   f"characterised with CC profile {k.get('id')} v{k.get('version')}. "
                   + s["notes"]).strip(),
            meta=meta, uncertainty=u)

    def save(self, *, allow_failed_checks: bool = False) -> rs.ReferenceSource:
        if self.result is None:
            raise RuntimeError("compute first")
        if not self.checks_passed and not allow_failed_checks:
            raise RuntimeError("Checks failed (" + ", ".join(self.failed_checks())
                               + ") — not saved.")
        src = self.reference_source()
        self.saved_source = self.library.add(src)
        return self.saved_source

    def failed_checks(self) -> list[str]:
        out = []
        if self.checks.get("drift_pass") is False:
            out.append("source drift")
        if self.checks.get("consistency_pass") is False:
            out.append("consistency over the distances")
        return out

    def run_all(self, heights_mm, *, integration_us: int | None = None, move=None,
                save: bool = True) -> dict:
        """Headless full run (shutter devices / tests). move(height_mm) is
        called before each position and before the repeat (test hook)."""
        hs = [float(h) for h in heights_mm]
        if move:
            move(hs[0])
        self.set_integration(integration_us, auto=integration_us is None)
        self.capture_dark("before")
        for h in hs:
            if move:
                move(h)
            self.capture_position(h)
        if move:
            move(hs[0])
        self.capture_repeat()
        self.capture_dark("after")
        self.compute()
        if save:
            self.save(allow_failed_checks=True)
        return self.record()

    def record(self) -> dict:
        """JSON-able run record for the protocol (PDF/CSV)."""
        res = self.result or {}

        def arr(a):
            return None if a is None else [None if not np.isfinite(v) else float(v)
                                           for v in np.asarray(a, float)]
        spectra = {}
        if res:
            spectra = {"wl": arr(res["wl"]),
                       "E": [arr(e) for e in self.E],
                       "E_repeat": arr(self.E_repeat),
                       "L_i": [arr(x) for x in res["L_i"]],
                       "L": arr(res["L"]), "L_fit": arr(res["L_fit"]),
                       "L_mean": arr(res["L_mean"]),
                       "L_smoothed": arr(res["L_smoothed"]),
                       "spread_pct": arr(res["spread_pct"]),
                       "u_rel_pct": arr(100.0 * np.asarray(res["u_rel"], float)),
                       "valid": [bool(v) for v in res["valid"]]}
        k = self.cc_profile
        return {
            "kind": "sphere_characterisation",
            "started": self.started, "finished": cc.now_iso(),
            "quantity": rs.QUANTITY_RADIANCE,
            "unit": rs.UNIT_UW_CM2_SR_NM, "irradiance_unit": rs.UNIT_UW_CM2_NM,
            "device": {"backend": self.backend_name,
                       "serial": getattr(self.backend, "serial", None)},
            "sphere": dict(self.sphere),
            "cc_profile": {"id": k.get("id"), "version": k.get("version"),
                           "optics": dict(k.get("optics") or {}),
                           "reference": dict(k.get("reference") or {}),
                           "valid_range_nm": k.get("valid_range_nm")},
            "source_hint": SPHERE_SOURCE_HINT,
            "wl_fingerprint": self.wl_fp,
            "acquisition": {"integration_us": self.integration_us,
                            "exposure": self.exposure,
                            "averages": self.cfg.n_frames,
                            "dark_sequence": ("before+after (time-interpolated)"
                                              if self.dark_after is not None else "before"),
                            "nl_correction": self.backend.nl_correction_active},
            "positions": [{"height_mm": p["height_mm"], "d_mm": p["d_mm"],
                           "d_eff_mm": (None if not res else res["d_eff_mm"][i]),
                           "peak_frac": float(p["capture"].peak_frac),
                           "captured": p["capture"].timestamp}
                          for i, p in enumerate(self.positions)],
            "settings": asdict(self.cfg),
            "formula": "L = E·(r² + d²)/(π·r²);  fit E(d) = π·L·r²/(r² + (d + d0)²)",
            "d0_mm": res.get("d0_mm"), "d0_plausible": res.get("d0_plausible"),
            "d0_max_mm": res.get("d0_max_mm"),
            "method": res.get("method"),
            "bands": res.get("bands"), "overall": res.get("overall"),
            "consistency_pass": res.get("consistency_pass"),
            "consistency_max_pct": res.get("consistency_max_pct"),
            "uncertainty": res.get("uncertainty"),
            "drift": self.drift,
            "checks": dict(self.checks),
            "valid_range_nm": res.get("valid_range_nm"),
            "warnings": list(self.warnings),
            "source": None if self.saved_source is None else {
                "id": self.saved_source.id, "name": self.saved_source.name,
                "unit": self.saved_source.unit, "quantity": self.saved_source.quantity,
                "valid_min_nm": self.saved_source.valid_min_nm,
                "valid_max_nm": self.saved_source.valid_max_nm},
            "spectra": spectra,
        }


def _largest_run(mask) -> tuple[int, int] | None:
    m = np.asarray(mask, bool)
    idx = np.flatnonzero(m)
    if idx.size == 0:
        return None
    breaks = np.flatnonzero(np.diff(idx) > 1)
    starts = np.r_[idx[0], idx[breaks + 1]]
    ends = np.r_[idx[breaks], idx[-1]]
    k = int(np.argmax(ends - starts))
    return int(starts[k]), int(ends[k])

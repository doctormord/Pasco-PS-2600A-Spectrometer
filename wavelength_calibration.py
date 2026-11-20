"""
wavelength_calibration.py — Qt-free logic of the wavelength calibration
(line lamp -> peaks -> (pixel, nm) pairs -> polynomial fit -> save).

One WavelengthSession per device. Used by the desktop dialog (gui_calibration)
and the web API (api_v1_wavelength). The module never touches the display or
the engine: fit()/discard() return the new axis, the caller adopts it
(desktop: dash.sync_active_axis, API: engine.update_axis).
"""
from __future__ import annotations

import datetime
import json
import os

import numpy as np

import calibration_profiles as calprof

# ── Limits (plausibility guard) ──────────────────────────────────────────
MAX_RESIDUAL_NM = 8.0            # larger max residual -> fit rejected
AXIS_START_RANGE_NM = (120.0, 1350.0)   # open interval for wl[0]
AXIS_END_RANGE_NM = (200.0, 1400.0)     # open interval for wl[-1]
COVERAGE_GAP_NM = 60.0           # lines leave > this at either end -> warning
RMS_WARN_NM = 0.5
DEDUP_PX = 4.0
MATCH_TOL_NM = 8.0
PEAK_MIN_DISTANCE_PX = 10
DEFAULT_DEGREE = 3

# backend name -> config key holding the wavelength polynomial
WL_CONFIG_KEYS = {
    "PASCO PS-2600A": "pasco_wl_poly_coeffs",
    "ASEQ / Lasertrack LR-2T": "lr2t_wl_poly_coeffs",
    "Ocean HDX-UV-VIS": "ocean_wl_poly_coeffs",
}
OCEAN_NAME = "Ocean HDX-UV-VIS"
BACKUP_KEYS = ("pasco_wl_poly_coeffs", "lr2t_wl_poly_coeffs", "ocean_wl_poly_coeffs",
               "lr2t_wl_offset_nm", "ocean_wl_offset_nm", "ocean_use_device_wavelength")


class WavelengthError(ValueError):
    """Invalid use / precondition not met (maps to 422 in the API)."""


class NotSupported(WavelengthError):
    """The device has a fixed/virtual axis (calibrate_from_lines unusable)."""


def lamps() -> dict[str, list[float]]:
    return {k: [float(x) for x in v] for k, v in calprof.CALIBRATION_LINES.items()}


class WavelengthSession:
    def __init__(self, backend, backend_name: str):
        self.backend = backend
        self.backend_name = backend_name
        self.open_snapshot = backend.wl_calibration_snapshot()
        self.saved = False
        self.last_fit: dict | None = None
        self.capture: tuple[np.ndarray, np.ndarray] | None = None
        self.peaks: list[float] | None = None
        self.pairs: list[tuple[float, float]] = []

    # ── capture / peaks ──────────────────────────────────────────────────
    def set_capture(self, wl, intensities) -> None:
        self.capture = (np.asarray(wl, float).copy(), np.asarray(intensities, float).copy())
        self.peaks = None

    @property
    def has_signal(self) -> bool:
        return self.capture is not None and float(np.max(self.capture[1])) >= 1.0

    def detect_peaks(self) -> list[float]:
        if self.capture is None:
            raise WavelengthError("No capture yet.")
        if not self.has_signal:
            self.peaks = None
            return []
        self.peaks = calprof.detect_peaks(self.capture[1], min_distance_px=PEAK_MIN_DISTANCE_PX)
        return list(self.peaks)

    # ── matching ─────────────────────────────────────────────────────────
    def match(self, lamp: str, accumulate: bool = False, tol_nm: float = MATCH_TOL_NM) -> dict:
        if lamp not in calprof.CALIBRATION_LINES:
            raise WavelengthError(f"Unknown lamp '{lamp}'.")
        if self.capture is None or not self.peaks:
            raise WavelengthError("No peaks detected; capture a line lamp first.")
        wl = self.capture[0]
        known = [k for k in calprof.CALIBRATION_LINES[lamp] if wl[0] <= k <= wl[-1]]
        found = calprof.auto_match_lines(self.peaks, wl, known, tol_nm=tol_nm)
        if accumulate:
            added = self._merge(found)
        else:
            self.pairs = [(float(p), float(n)) for p, n in found]
            added = len(found)
        return {"lamp": lamp, "pairs": list(self.pairs), "n_peaks": len(self.peaks),
                "n_known": len(known), "n_assigned": len(found), "n_added": added,
                "n_skipped": max(0, len(known) - len(found)), "n_total": len(self.pairs),
                "accumulate": bool(accumulate)}

    def _merge(self, pairs, dedup_px: float = DEDUP_PX) -> int:
        existing = [p for p, _ in self.pairs]
        added = 0
        for px, nm in pairs:
            if any(abs(px - e) <= dedup_px for e in existing):
                continue
            self.pairs.append((float(px), float(nm)))
            existing.append(px)
            added += 1
        return added

    def set_pairs(self, pairs) -> list[tuple[float, float]]:
        out = []
        for p in pairs:
            try:
                px, nm = float(p[0]), float(p[1])
            except (TypeError, ValueError, IndexError):
                raise WavelengthError(f"Invalid pair {p!r}; expected [pixel, nm].")
            if not (np.isfinite(px) and np.isfinite(nm)):
                raise WavelengthError(f"Non-finite pair {p!r}.")
            out.append((px, nm))
        self.pairs = out
        return list(out)

    def clear(self) -> None:
        self.pairs = []
        self.capture = None
        self.peaks = None
        self.last_fit = None

    # ── fit ──────────────────────────────────────────────────────────────
    def fit(self, degree: int = DEFAULT_DEGREE) -> dict:
        backend = self.backend
        deg = int(degree)
        if len(self.pairs) < deg + 1:
            raise WavelengthError(
                f"A degree-{deg} fit needs at least {deg + 1} assigned lines "
                f"(have {len(self.pairs)}). Add lines or lower the degree.")
        pxs = [p for p, _ in self.pairs]
        nms = [n for _, n in self.pairs]
        snap = backend.wl_calibration_snapshot()
        try:
            res = backend.calibrate_from_lines(nms, pixel_positions=pxs, degree=deg)
        except Exception:
            backend.wl_calibration_restore(snap)
            raise
        if not isinstance(res, dict) or "coeffs" not in res:
            backend.wl_calibration_restore(snap)
            raise NotSupported("This device does not support wavelength calibration "
                               "(its axis is fixed/virtual).")
        wl_new = np.asarray(backend.wavelength_array, dtype=float)
        max_res = float(res["max_error"])
        residuals = [float(x) for x in res["residuals"]]
        rms = float(np.sqrt(np.mean(np.square(residuals)))) if residuals else 0.0
        monotonic = bool(np.all(np.diff(wl_new) > 0))
        lo0, lo1 = AXIS_START_RANGE_NM
        hi0, hi1 = AXIS_END_RANGE_NM
        in_band = (lo0 < wl_new[0] < lo1) and (hi0 < wl_new[-1] < hi1)
        reasons = []
        if not monotonic:
            reasons.append("the wavelength axis comes out non-monotonic")
        if not in_band:
            reasons.append(f"the axis runs {wl_new[0]:.0f}–{wl_new[-1]:.0f} nm "
                           f"(outside a plausible range)")
        if max_res > MAX_RESIDUAL_NM:
            reasons.append(f"the max residual is {max_res:.1f} nm")
        line_lo, line_hi = float(min(nms)), float(max(nms))
        dev_lo, dev_hi = float(wl_new[0]), float(wl_new[-1])
        out = {"accepted": not reasons, "reasons": reasons, "degree": deg,
               "n_lines": len(nms), "coeffs": [float(c) for c in res["coeffs"]],
               "max_error": max_res, "rms": rms, "residuals": residuals,
               "line_range": [line_lo, line_hi], "device_range": [dev_lo, dev_hi],
               "coverage_warning": None, "rms_warning": None}
        if reasons:
            backend.wl_calibration_restore(snap)
            self.last_fit = None
            out["wavelengths"] = np.asarray(backend.wavelength_array, dtype=float)
            return out
        if line_lo - dev_lo > COVERAGE_GAP_NM or dev_hi - line_hi > COVERAGE_GAP_NM:
            out["coverage_warning"] = (
                f"Coverage warning: lines span {line_lo:.0f}–{line_hi:.0f} nm but the "
                f"device covers {dev_lo:.0f}–{dev_hi:.0f} nm. The axis is extrapolated "
                f"outside the line span — small residuals do not guarantee accuracy "
                f"there. Add lines from other lamps for full coverage.")
        elif rms > RMS_WARN_NM:
            out["rms_warning"] = (f"RMS {rms:.2f} nm is a bit high — check for a "
                                  f"mis-assigned line before saving.")
        out["wavelengths"] = wl_new
        self.last_fit = {k: v for k, v in out.items() if k != "wavelengths"}
        if self.capture is not None:                 # re-plot on the new axis
            self.capture = (wl_new.copy(), self.capture[1])
        return out

    # ── save / discard ───────────────────────────────────────────────────
    def config_key(self) -> str | None:
        return WL_CONFIG_KEYS.get(self.backend_name)

    def backup(self) -> str:
        from app_config import Config
        snap = {k: Config.get(k, None) for k in BACKUP_KEYS}
        ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        snap["_backed_up_at"] = ts
        snap["_note"] = "Calibration snapshot taken before the wizard overwrote it."
        try:
            cfg_dir = os.path.dirname(os.path.abspath(Config.path)) or "."
            path = os.path.join(cfg_dir, f"old_calib_data_{ts}.json")
            with open(path, "w", encoding="utf-8") as f:
                json.dump(snap, f, indent=2, sort_keys=True)
            return path
        except Exception as e:
            raise WavelengthError(f"Could not write the calibration backup: {e}. "
                                  f"Nothing was saved.")

    def save(self, confirm: bool) -> dict:
        if not self.last_fit:
            raise WavelengthError("No accepted fit to save.")
        if not confirm:
            raise WavelengthError(
                "Confirmation required. Saving replaces the current wavelength "
                "calibration; a timestamped backup is written before.")
        key = self.config_key()
        if key is None:
            raise WavelengthError(
                f"No wavelength-coefficient config key for '{self.backend_name}'. "
                f"The fit is applied for this session but not saved.")
        backup_path = self.backup()
        from app_config import Config
        Config.set(key, list(self.last_fit["coeffs"]))
        extra = ""
        if self.backend_name == OCEAN_NAME:
            Config.set("ocean_use_device_wavelength", False)
            extra = ("Switched 'ocean_use_device_wavelength' → false so this "
                     "calibration is used instead of the device's factory axis. "
                     "Set it back to true to revert to the device axis.")
        self.saved = True
        return {"key": key, "backup_path": backup_path, "extra_note": extra}

    def discard(self) -> np.ndarray | None:
        """Roll back to the axis at session start unless saved. Returns the
        axis now in effect (caller adopts it), or None if nothing was done."""
        if self.saved or self.open_snapshot is None:
            return None
        self.backend.wl_calibration_restore(self.open_snapshot)
        self.last_fit = None
        return np.asarray(self.backend.wavelength_array, dtype=float)

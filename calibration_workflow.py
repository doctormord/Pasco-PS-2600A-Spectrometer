"""
calibration_workflow.py — The calibration wizard's fixed sequence, Qt-free.

Identical for every backend and every optics configuration (plan §4, Phase 2):

  1. choose device, optics configuration (name + input CC/bare/sphere/
     objective), reference source from the library, type relative/absolute.
     The measurement quantity follows from the reference source (irradiance
     or radiance); calibration_profiles.admissible() decides which input
     optics are allowed (absolute irradiance: CC/sphere; absolute radiance:
     bare/objective; relative: all).
  2. wavelength-calibration check (fingerprint)
  3. integration time: auto exposure to the target band (70–85 % FS), then fixed
  4. dark spectra BEFORE — at t_int AND at ~t_int/2, in one covered phase
  5. calibration spectrum + repeat after a delay (stability)
  6. capture at ~t_int/2 (integration-time cross check)
  7. dark spectra AFTER — again at t_int and t_int/2 (second covered phase).
     Every light capture is corrected with the dark interpolated in time
     between its dark before and after (calibration_capture.interpolate_dark);
     the dark drift is reported. Then the stability and cross checks are
     evaluated PER PIXEL with a noise-dependent tolerance (failing pixels are
     masked and limit the valid segments) plus a cause model (dark drift /
     time offset or lamp drift / nonlinearity) with magnitudes.
  8. compute S or K, valid range (plan §2.4), gain span (hint only)
  9. save as a new profile version; absolute additionally saves the derived
     relative profile; build the run record for the protocol (PDF/CSV)
 10. verification (engine.verify_profile): the reference source measured
     through the live pipeline with the new profile, deviation per 50-nm band

All methods are blocking; a GUI runs them from a worker thread. The GUI only
adds prompts ("cover the input", "uncover") between steps — see
needs_user_cover().

Also: DerivationWorkflow — derive a new optics configuration from a known one
via the ratio of two measurements of the same diffuse, extended source:
    S_new = S_known · C_new / C_known
with an own integration time and dark per optics and a final repeat of the
known optics (source drift).
"""

from __future__ import annotations

import numbers
import time
from dataclasses import dataclass, field, asdict

import numpy as np

import calibration_capture as cc
import calibration_profiles as cp


@dataclass
class WorkflowConfig:
    n_frames: int = 16
    snr_k: float = 10.0
    max_gain: float = 50.0
    smooth_px: int = 5
    target_frac: tuple = (0.70, 0.85)
    stability_delay_s: float = 5.0
    stability_threshold_pct: float = 1.0
    itime_threshold_pct: float = 2.0
    # plausibility (broadband) limits — see calibration_profiles.broadband_check
    min_coverage: float = 0.5
    max_gain_span: float = 200.0
    max_roughness: float = 0.05
    max_isolated_frac: float = 0.2
    min_segment_nm: float = 5.0
    max_gap_nm: float = 2.0
    min_largest_segment_frac: float = 0.5
    roughness_px: int = 21
    # per-pixel checks / cause model / dark drift
    pixel_check_k: float = 4.0
    cause_min_pct: float = 0.3
    dark_drift_warn_pct: float = 0.5      # of the net peak counts
    # derivation: repeat of the known optics at the end (source drift, %)
    derivation_drift_pct: float = 2.0
    # saturation warning threshold (fraction of full scale, raw peak) — the
    # same value as the exposure meter (processing.saturation_warn_frac)
    saturation_warn_frac: float = 0.90
    # tolerance for "dark and light at the same integration time" (%) and the
    # factor for "dark unusually bright": warn when dark peak > factor · light peak
    itime_match_tol_pct: float = 0.5
    dark_bright_frac: float = 0.5

    def __post_init__(self):
        # the auto-exposure target band must stay below the saturation warning
        lo, hi = float(self.target_frac[0]), float(self.target_frac[1])
        w = float(self.saturation_warn_frac)
        if hi > w:
            hi = w
        if lo >= hi:
            lo = 0.8 * hi
        self.target_frac = (lo, hi)

    def plausibility_limits(self) -> "cp.PlausibilityLimits":
        return cp.PlausibilityLimits(
            min_coverage=self.min_coverage, max_gain_span=self.max_gain_span,
            max_roughness=self.max_roughness, max_isolated_frac=self.max_isolated_frac,
            min_segment_nm=self.min_segment_nm, max_gap_nm=self.max_gap_nm,
            min_largest_segment_frac=self.min_largest_segment_frac,
            roughness_px=self.roughness_px)

    @classmethod
    def from_config(cls) -> "WorkflowConfig":
        try:
            from app_config import Config as C
        except Exception:
            return cls()
        lim = cp.PlausibilityLimits.from_config()
        return cls(
            min_coverage=lim.min_coverage, max_gain_span=lim.max_gain_span,
            max_roughness=lim.max_roughness, max_isolated_frac=lim.max_isolated_frac,
            min_segment_nm=lim.min_segment_nm, max_gap_nm=lim.max_gap_nm,
            min_largest_segment_frac=lim.min_largest_segment_frac,
            roughness_px=lim.roughness_px,
            n_frames=max(2, int(C.get("calibration_capture_frames", 16))),
            snr_k=float(C.get("calibration_snr_k", 10.0)),
            max_gain=float(C.get("calibration_max_gain", 50.0)),
            smooth_px=int(C.get("calibration_smooth_px", 5)),
            target_frac=(float(C.get("calibration_target_frac_lo", 0.70)),
                         float(C.get("calibration_target_frac_hi", 0.85))),
            stability_delay_s=float(C.get("calibration_stability_delay_s", 5.0)),
            stability_threshold_pct=float(C.get("calibration_stability_threshold_pct", 1.0)),
            itime_threshold_pct=float(C.get("calibration_itime_threshold_pct", 2.0)),
            pixel_check_k=float(C.get("calibration_pixel_check_k", 4.0)),
            cause_min_pct=float(C.get("calibration_cause_min_pct", 0.3)),
            dark_drift_warn_pct=float(C.get("calibration_dark_drift_warn_pct", 0.5)),
            derivation_drift_pct=float(C.get("calibration_derivation_drift_pct", 2.0)),
            saturation_warn_frac=_sat_warn_frac(),
            itime_match_tol_pct=float(C.get("calibration_itime_match_tol_pct", 0.5)),
            dark_bright_frac=float(C.get("calibration_dark_bright_frac", 0.5)),
        )


def _sat_warn_frac() -> float:
    import processing
    return processing.saturation_warn_frac()


def _high_exposure_warning(cap, warn_frac: float, what: str) -> str | None:
    """Warning text when a light capture's raw peak reached the saturation
    warning threshold (but no pixel is saturated — that is an error)."""
    if cap is None or cap.n_saturated or cap.peak_frac < float(warn_frac):
        return None
    return (f"Exposure {what}: raw peak at {cap.peak_frac*100:.1f} % of full scale "
            f"(saturation warning threshold {float(warn_frac)*100:.0f} %).")


class PlausibilityError(RuntimeError):
    """The result failed the broadband plausibility check; saving needs an
    explicit override (save(allow_implausible=True))."""


def _plausibility(result: dict, cfg: "WorkflowConfig", warnings: list) -> dict:
    pl = cp.broadband_check(result, cfg.plausibility_limits())
    result["segments_nm"] = pl["segments_nm"]
    for h in pl.get("hints") or []:
        msg = "Hint: " + h
        if msg not in warnings:
            warnings.append(msg)
    if not pl["pass"]:
        msg = ("Plausibility check failed: " + "; ".join(pl["failures"])
               + ". The calibration source must be smooth and broadband "
                 "(no line lamp).")
        if msg not in warnings:
            warnings.append(msg)
    return pl


def _stored_plausibility(pl, override: bool):
    if pl is None:
        return None
    return {"pass": bool(pl["pass"]), "override": bool(override and not pl["pass"]),
            "failures": list(pl["failures"]), "hints": list(pl.get("hints") or []),
            "metrics": dict(pl["metrics"]), "limits": dict(pl["limits"])}


def backend_linearity_info(backend) -> dict:
    """The linearity state the engine installed on the backend
    (calibration_linearity.runtime_for); 'none' when nothing is known."""
    info = getattr(backend, "linearity_info", None)
    if isinstance(info, dict):
        return dict(info)
    try:
        factory = backend.nl_correction_active
    except Exception:                                       # noqa: BLE001
        factory = None
    return {"state": "factory" if factory is True else "none", "id": None,
            "created": None, "factory_nl": factory if isinstance(factory, bool) else None}


def wavelength_check(backend, backend_name: str, optics_name: str = "",
                     kind: str = "") -> dict:
    """Step 2. ok=False only if the axis is unusable (non-monotonic). Also
    reports whether the axis looks like an uncalibrated linear default and
    whether earlier profiles of this optics series used another calibration."""
    wl = np.asarray(backend.wavelength_array, float)
    fp = backend.wl_fingerprint()
    out = {"ok": True, "fingerprint": fp, "messages": [],
           "range_nm": [float(wl[0]), float(wl[-1])]}
    if wl.size < 2 or not np.all(np.diff(wl) > 0):
        out["ok"] = False
        out["messages"].append("Wavelength axis is not monotonic — run the "
                               "wavelength calibration first.")
        return out
    lin = np.linspace(getattr(backend, "WL_MIN_NM", wl[0]),
                      getattr(backend, "WL_MAX_NM", wl[-1]), wl.size)
    if np.allclose(wl, lin, atol=1e-6):
        out["linear_default"] = True
        out["messages"].append("The axis is the backend's linear default — if this "
                               "device needs a wavelength calibration, do it first "
                               "(S and K contain the pixel width in nm).")
    serial = getattr(backend, "serial", None)
    olds = [p for p in cp.profiles_for_device(backend_name, serial, fp, include_old=True)
            if (p.get("optics") or {}).get("name") == optics_name
            and (not kind or p.get("type") == kind) and p.get("wl_fingerprint")]
    if olds and all(p.get("wl_fingerprint") != fp for p in olds):
        out["messages"].append("The wavelength calibration changed since the last "
                               "calibration of this optics configuration — the old "
                               "versions are marked invalid.")
    return out


# Text shown by the desktop wizard and the web dialog / API job for a
# radiance calibration (one source).
RADIANCE_SOURCE_HINT = (
    "Radiance: the source must fill the field of view of the optics completely "
    "and uniformly — e.g. the sphere port under the objective, focused on the "
    "port plane; a bare fibre close above the port, its acceptance cone inside "
    "the port.")


def reference_quantity(reference) -> str:
    """Measurement quantity of a library source (irradiance by default)."""
    q = getattr(reference, "quantity", None)
    return q if q in cp.QUANTITIES else cp.QUANTITY_IRRADIANCE


class CalibrationWorkflow:
    """State of one wizard run (relative or absolute, against a library lamp
    or a characterised radiance source)."""

    def __init__(self, backend, backend_name: str, *, kind: str, optics: dict,
                 reference, cfg: WorkflowConfig | None = None):
        if kind not in (cp.TYPE_RELATIVE, cp.TYPE_ABSOLUTE):
            raise ValueError(f"unknown type '{kind}'")
        inp = optics.get("input", "")
        if inp not in cp.INPUT_OPTICS:
            raise ValueError(f"input optic must be one of {cp.INPUT_OPTICS}")
        quantity = reference_quantity(reference)
        ok, why = cp.admissible(kind, quantity, inp)
        if not ok:
            raise ValueError(why)
        if not optics.get("name"):
            raise ValueError("Name the optics configuration.")
        if kind == cp.TYPE_ABSOLUTE and not cp.is_absolute_reference_unit(
                getattr(reference, "unit", ""), quantity):
            raise ValueError(f"Absolute calibration needs a reference source with an "
                             f"absolute {quantity} unit; '{reference.name}' is "
                             f"'{reference.unit}'. Use type 'relative'.")
        self.backend = backend
        self.backend_name = backend_name
        self.kind = kind
        self.quantity = quantity
        self.optics = {"name": optics["name"], "input": inp,
                       "notes": optics.get("notes", "")}
        self.reference = reference                     # reference_sources.ReferenceSource
        self.cfg = cfg or WorkflowConfig.from_config()
        self.integration_us: int | None = None
        self.half_us: int | None = None
        self.dark = self.dark_half = None                 # darks BEFORE
        self.dark_after = self.dark_half_after = None     # darks AFTER
        self.light = self.light2 = self.light_half = None
        self.rate = self.rate2 = self.rate_half = None
        self.pixel_ok = None                              # per-pixel checks (bool)
        self.pixel_checks: dict = {}
        self.causes: dict = {}
        self.dark_drift: list[dict] = []
        self._checks_final = False
        self.verification: dict | None = None
        self.wl_check: dict | None = None
        self.result: dict | None = None
        self.checks: dict = {}
        self.warnings: list[str] = []
        self.saved: list[dict] = []
        self.plausibility: dict | None = None
        self.saved_with_override = False
        self.started = cc.now_iso()

    # ── helpers ──────────────────────────────────────────────────────────
    @property
    def needs_user_cover(self) -> bool:
        """True if the GUI must ask the user to block the light for the darks."""
        return not getattr(self.backend, "SUPPORTS_SHUTTER", False)

    def _check_fp(self):
        fp = self.backend.wl_fingerprint()
        if self.wl_check and fp != self.wl_check["fingerprint"]:
            raise RuntimeError("The wavelength calibration changed during the run "
                               "— restart the calibration.")

    # ── steps ────────────────────────────────────────────────────────────
    def check_wavelength(self) -> dict:
        self.wl_check = wavelength_check(self.backend, self.backend_name,
                                         self.optics["name"], self.kind)
        return self.wl_check

    def set_integration(self, integration_us: int | None = None, *,
                        auto: bool = True) -> dict:
        """Step 3. Auto exposure to the target band, or a fixed value."""
        b = self.backend
        converged = True
        cap = None
        if integration_us is None and auto:
            t, cap, converged = cc.auto_expose(b, target_frac=self.cfg.target_frac,
                                               backend_name=self.backend_name)
        else:
            t = int(integration_us or b.current_integration_time_us)
            b.set_integration_time_us(t)
            t = int(b.current_integration_time_us)
        self.integration_us = t
        lo = int(getattr(b, "min_integration_us", 1))
        self.half_us = max(lo, int(round(t / 2)))
        if self.half_us >= t:
            self.half_us = None                       # cannot halve at the minimum
        info = {"integration_us": t, "half_us": self.half_us, "converged": converged,
                "peak_frac": None if cap is None else cap.peak_frac}
        if not converged:
            self.warnings.append("Auto exposure did not reach the target band "
                                 f"{self.cfg.target_frac[0]*100:.0f}–"
                                 f"{self.cfg.target_frac[1]*100:.0f} % FS.")
        return info

    def capture_darks(self) -> dict:
        """Step 4: darks BEFORE the light captures, at t_int and t_int/2. Light
        must be blocked (shutter is used automatically when the backend has
        one)."""
        if self.integration_us is None:
            raise RuntimeError("set the integration time first")
        n = self.cfg.n_frames
        self.dark = cc.capture_dark(self.backend, n, integration_us=self.integration_us,
                                    backend_name=self.backend_name)
        cc.save_dark(self.dark)
        if self.half_us:
            self.dark_half = cc.capture_dark(self.backend, n, integration_us=self.half_us,
                                             backend_name=self.backend_name)
            cc.save_dark(self.dark_half)
        self.backend.set_integration_time_us(self.integration_us)
        return {"dark_peak": self.dark.peak_counts,
                "dark_mean": float(np.mean(self.dark.mean)), "phase": "before"}

    def capture_calibration(self, *, sleep=time.sleep) -> dict:
        """Step 5: calibration spectrum + repeat after cfg.stability_delay_s.
        The stability result returned here is PROVISIONAL (dark before only);
        the final evaluation follows capture_darks_after()."""
        if self.dark is None:
            raise RuntimeError("capture the dark spectrum first")
        self._check_fp()
        n = self.cfg.n_frames
        self.light = cc.capture(self.backend, n, integration_us=self.integration_us,
                                backend_name=self.backend_name)
        self.rate = cc.count_rate(self.light, self.dark)
        if self.dark.peak_frac > self.cfg.dark_bright_frac * max(self.light.peak_frac, 1e-9):
            self.warnings.append("The dark spectrum is unusually bright compared to "
                                 "the calibration spectrum — was the light blocked?")
        self.warnings.extend(w for w in self.rate.warnings if w not in self.warnings)
        if self.light.n_saturated:
            raise RuntimeError(self.rate.warnings[-1])
        hw = _high_exposure_warning(self.light, self.cfg.saturation_warn_frac,
                                    "of the calibration spectrum")
        if hw and hw not in self.warnings:
            self.warnings.append(hw)
        sleep(max(0.0, self.cfg.stability_delay_s))
        self.light2 = cc.capture(self.backend, n, integration_us=self.integration_us,
                                 backend_name=self.backend_name)
        self.evaluate_checks(final=False)
        st = self.checks["stability_detail"]
        return {"peak_frac": self.light.peak_frac, "stability": st,
                "pass": self.checks["stability_pass"], "provisional": True}

    def crosscheck_integration(self) -> dict:
        """Step 6: capture at ~t/2. Provisional result (dark before only)."""
        if self.half_us is None or self.dark_half is None:
            self.checks["itime_crosscheck_pct"] = None
            self.checks["itime_pass"] = None
            msg = ("Integration-time cross check skipped (t_int at the device "
                   "minimum).")
            if msg not in self.warnings:
                self.warnings.append(msg)
            return {"skipped": True}
        self._check_fp()
        self.light_half = cc.capture(self.backend, self.cfg.n_frames,
                                     integration_us=self.half_us,
                                     backend_name=self.backend_name)
        self.backend.set_integration_time_us(self.integration_us)
        self.evaluate_checks(final=False)
        return {"crosscheck": self.checks.get("itime_detail"),
                "pass": self.checks.get("itime_pass"), "provisional": True}

    def capture_darks_after(self) -> dict:
        """Step 7: darks AFTER the light captures (t_int and t_int/2; light
        blocked again). Then every light capture is corrected with the dark
        interpolated in time, the dark drift is reported and the checks are
        evaluated finally (per pixel + cause model)."""
        if self.dark is None or self.light is None:
            raise RuntimeError("capture the dark and calibration spectra first")
        n = self.cfg.n_frames
        self.dark_after = cc.capture_dark(self.backend, n, integration_us=self.integration_us,
                                          backend_name=self.backend_name)
        cc.save_dark(self.dark_after)
        if self.half_us and self.dark_half is not None:
            self.dark_half_after = cc.capture_dark(self.backend, n,
                                                   integration_us=self.half_us,
                                                   backend_name=self.backend_name)
            cc.save_dark(self.dark_half_after)
        self.backend.set_integration_time_us(self.integration_us)
        self.evaluate_checks(final=True)
        return {"drift": list(self.dark_drift),
                "stability": self.checks.get("stability_detail"),
                "stability_pass": self.checks.get("stability_pass"),
                "crosscheck": self.checks.get("itime_detail"),
                "itime_pass": self.checks.get("itime_pass"),
                "causes": self.causes, "pixel_fail": {
                    k: v["n_fail"] for k, v in self.pixel_checks.items()}}

    def _rates(self) -> None:
        """Count rates with the time-interpolated dark (before/after)."""
        self.rate = cc.count_rate(self.light, cc.interpolate_dark(
            self.dark, self.dark_after, self.light.t_mid))
        if self.light2 is not None:
            self.rate2 = cc.count_rate(self.light2, cc.interpolate_dark(
                self.dark, self.dark_after, self.light2.t_mid))
        if self.light_half is not None and self.dark_half is not None:
            self.rate_half = cc.count_rate(self.light_half, cc.interpolate_dark(
                self.dark_half, self.dark_half_after, self.light_half.t_mid))

    def evaluate_checks(self, final: bool = True) -> dict:
        """Stability and integration-time cross check.

        Per pixel (calibration_profiles.pixel_check): noise-dependent
        tolerance; failing pixels are masked (self.pixel_ok → compute()).
        Aggregate (compare_rates p95 over the passing pixels) decides
        pass/fail as before. Cause model (check_causes) over all tested pixels:
        constant offset = dark drift, uniform factor = lamp drift (repeat) /
        time offset (t/2), signal-dependent = nonlinearity."""
        self._rates()
        cfg = self.cfg
        full = float(self.light.full_scale) if self.light is not None else None
        self.pixel_checks, self.causes = {}, {}
        ok = np.ones(self.rate.rate.size, bool)
        m = self._check_mask(self.rate)
        # dark drift
        self.dark_drift = []
        if final and self.dark_after is not None:
            pairs = [(self.dark, self.dark_after)]
            if self.dark_half is not None and self.dark_half_after is not None:
                pairs.append((self.dark_half, self.dark_half_after))
            net_peak = max(float(self.light.peak_counts - np.percentile(self.dark.mean, 50)),
                           1.0)
            for b, a in pairs:
                d = cc.dark_drift(b, a)
                d["warn"] = bool(abs(d["mean_counts"]) > max(3.0 * d["sigma_counts"],
                                                             cfg.dark_drift_warn_pct / 100.0
                                                             * net_peak))
                d["pct_of_peak"] = 100.0 * d["mean_counts"] / net_peak
                self.dark_drift.append(d)
                if d["warn"]:
                    msg = (f"Dark drift {d['mean_counts']:+.1f} counts "
                           f"({d['pct_of_peak']:+.2f} % of the peak signal) between the "
                           f"dark before and after at {d['integration_us']/1000:.3f} ms "
                           f"— time-interpolated; check the detector warm-up.")
                    if msg not in self.warnings:
                        self.warnings.append(msg)
            self.checks["dark_drift_counts"] = [round(d["mean_counts"], 2)
                                                for d in self.dark_drift]
        # stability (repeat at t)
        lamp_rate = None
        if self.rate2 is not None:
            pc = cp.pixel_check(self.rate.rate, self.rate2.rate, self.rate.sigma_rate,
                                self.rate2.sigma_rate, m,
                                threshold_pct=cfg.stability_threshold_pct, k=cfg.pixel_check_k)
            cz = cp.check_causes(self.rate.rate, self.rate2.rate, self.rate.sigma_rate,
                                 self.rate2.sigma_rate, m, t_ref_us=self.integration_us,
                                 t_test_us=self.integration_us, full_scale=full,
                                 nonlinear=False, min_effect_pct=cfg.cause_min_pct)
            st = cp.compare_rates(self.rate.rate, self.rate2.rate, m & pc["ok"])
            st["n_fail_pixels"] = pc["n_fail"]
            self.pixel_checks["stability"] = pc
            self.causes["stability"] = cz
            ok &= pc["ok"]
            self.checks["stability_pct"] = st["p95_pct"]
            self.checks["stability_detail"] = st
            self.checks["stability_delay_s"] = cfg.stability_delay_s
            self.checks["stability_threshold_pct"] = cfg.stability_threshold_pct
            self.checks["stability_pass"] = bool(st["p95_pct"] < cfg.stability_threshold_pct)
            self.checks["stability_fail_pixels"] = pc["n_fail"]
            self.checks["stability_cause"] = cz["message"]
            # lamp drift rate → expected factor at the t/2 capture
            lamp = next((c for c in cz["causes"] if c["cause"] == cp.CAUSE_LAMP), None)
            t1, t2 = self.light.t_mid, self.light2.t_mid
            if lamp is not None and lamp["significant"] and t1 and t2 and t2 > t1:
                lamp_rate = lamp["value"] / (t2 - t1)
        # integration-time cross check (t/2), reference = repeat at t (closest in time)
        if self.rate_half is not None:
            ref = self.rate2 if self.rate2 is not None else self.rate
            ref_cap = self.light2 if self.light2 is not None else self.light
            lamp_scale = 1.0
            if lamp_rate is not None and self.light_half.t_mid and ref_cap.t_mid:
                lamp_scale = 1.0 + lamp_rate * (self.light_half.t_mid - ref_cap.t_mid)
            pc = cp.pixel_check(ref.rate, self.rate_half.rate, ref.sigma_rate,
                                self.rate_half.sigma_rate, m,
                                threshold_pct=cfg.itime_threshold_pct, k=cfg.pixel_check_k,
                                expected_ratio=lamp_scale)
            cz = cp.check_causes(ref.rate, self.rate_half.rate, ref.sigma_rate,
                                 self.rate_half.sigma_rate, m, t_ref_us=self.integration_us,
                                 t_test_us=self.half_us, full_scale=full, nonlinear=True,
                                 min_effect_pct=cfg.cause_min_pct, lamp_scale=lamp_scale)
            st = cp.compare_rates(ref.rate * lamp_scale, self.rate_half.rate, m & pc["ok"])
            st["n_fail_pixels"] = pc["n_fail"]
            st["lamp_scale"] = lamp_scale
            self.pixel_checks["itime"] = pc
            self.causes["itime"] = cz
            ok &= pc["ok"]
            self.checks["itime_crosscheck_pct"] = st["p95_pct"]
            self.checks["itime_detail"] = st
            self.checks["itime_half_us"] = self.half_us
            self.checks["itime_threshold_pct"] = cfg.itime_threshold_pct
            self.checks["itime_pass"] = bool(st["p95_pct"] < cfg.itime_threshold_pct)
            self.checks["itime_fail_pixels"] = pc["n_fail"]
            self.checks["itime_cause"] = cz["message"]
        self.pixel_ok = ok
        self._checks_final = bool(final)
        if final:
            self._check_warnings()
        return {"stability": self.checks.get("stability_detail"),
                "crosscheck": self.checks.get("itime_detail"), "causes": self.causes}

    def _check_warnings(self) -> None:
        """Final check messages (cause and magnitude) for dialog + protocol."""
        self.warnings[:] = [w for w in self.warnings
                            if not w.startswith(("Stability check failed",
                                                 "Integration-time cross check failed",
                                                 "Stability check:", "Cross check t/2:"))]
        c = self.checks
        if c.get("stability_pass") is False:
            self.warnings.append(
                f"Stability check failed: {c['stability_pct']:.2f} % > "
                f"{self.cfg.stability_threshold_pct:.2f} % — cause: "
                f"{c.get('stability_cause') or 'unknown'}.")
        elif (self.causes.get("stability") or {}).get("dominant"):
            self.warnings.append(f"Stability check: {c.get('stability_cause')}.")
        if c.get("itime_pass") is False:
            self.warnings.append(
                f"Integration-time cross check failed: {c['itime_crosscheck_pct']:.2f} % > "
                f"{self.cfg.itime_threshold_pct:.2f} % — cause: "
                f"{c.get('itime_cause') or 'unknown'}.")
        elif (self.causes.get("itime") or {}).get("dominant"):
            self.warnings.append(f"Cross check t/2: {c.get('itime_cause')}.")
        nf = {k: v["n_fail"] for k, v in self.pixel_checks.items() if v["n_fail"]}
        if nf:
            parts = ", ".join(f"{v} ({'stability' if k == 'stability' else 't/2'})"
                              for k, v in nf.items())
            self.warnings.append(f"Pixels masked by the per-pixel checks: {parts}.")

    def _check_mask(self, rate):
        m = cp._range_mask(rate.wl, *self.backend.spec_range_nm)
        e = self.reference_on_axis(rate.wl)
        return m & np.isfinite(e) & (rate.rate > self.cfg.snr_k * rate.sigma_dark) & ~rate.saturated

    def reference_on_axis(self, wl=None):
        wl = np.asarray(self.backend.wavelength_array if wl is None else wl, float)
        return self.reference.on_axis(wl)

    def compute(self) -> dict:
        """Step 8. Mean of the two stability captures is used as C_cal.
        Pixels that failed the per-pixel checks are masked."""
        if self.rate is None:
            raise RuntimeError("capture the calibration spectrum first")
        if not self.pixel_checks or not self._checks_final:
            self.evaluate_checks(final=True)
        r = self.rate
        rate = r.rate if self.rate2 is None else 0.5 * (r.rate + self.rate2.rate)
        sat = r.saturated | (self.rate2.saturated if self.rate2 is not None else False)
        self.result = cp.compute_calibration(
            r.wl, rate, r.sigma_dark, self.reference_on_axis(r.wl), kind=self.kind,
            saturated=sat, spec_range_nm=self.backend.spec_range_nm,
            snr_k=self.cfg.snr_k, max_gain=self.cfg.max_gain,
            smooth_px=self.cfg.smooth_px, pixel_ok=self.pixel_ok)
        self.result["C_cal"] = rate
        self.plausibility = _plausibility(self.result, self.cfg, self.warnings)
        self.checks["broadband_pass"] = self.plausibility["pass"]
        return self.result

    @property
    def plausible(self) -> bool:
        return bool(self.plausibility is None or self.plausibility["pass"])

    def save(self, *, allow_implausible: bool = False) -> list[dict]:
        """Step 8. New profile version(s). Absolute → also the derived relative.

        A result that failed the plausibility check is only stored with
        allow_implausible=True; the profile is then flagged (plausibility.pass
        False, override True)."""
        if self.result is None:
            raise RuntimeError("compute first")
        if not self.plausible and not allow_implausible:
            raise PlausibilityError("; ".join(self.plausibility["failures"]))
        self._check_fp()
        override = not self.plausible
        stored_pl = _stored_plausibility(self.plausibility, override)
        ref = self.reference
        reference = {"name": ref.name, "serial": ref.serial, "unit": ref.unit,
                     "id": ref.id, "valid_range_nm": [ref.valid_min_nm, ref.valid_max_nm]}
        acq = {"integration_us": int(self.integration_us),
               "averages": int(self.cfg.n_frames),
               "nl_correction": self.backend.nl_correction_active,
               "linearity": _lin_tag(self.backend),
               "dark_frames": int(self.dark.n_frames),
               "dark_sequence": "before+after (time-interpolated)"
               if self.dark_after is not None else "before",
               "peak_frac": float(self.light.peak_frac),
               "snr_k": self.cfg.snr_k, "max_gain": self.cfg.max_gain,
               "smooth_px": self.cfg.smooth_px}
        checks = {"stability_pct": self.checks.get("stability_pct"),
                  "itime_crosscheck_pct": self.checks.get("itime_crosscheck_pct"),
                  "stability_pass": self.checks.get("stability_pass"),
                  "itime_pass": self.checks.get("itime_pass"),
                  "broadband_pass": self.checks.get("broadband_pass"),
                  "stability_fail_pixels": self.checks.get("stability_fail_pixels"),
                  "itime_fail_pixels": self.checks.get("itime_fail_pixels"),
                  "stability_cause": self.checks.get("stability_cause"),
                  "itime_cause": self.checks.get("itime_cause"),
                  "dark_drift_counts": self.checks.get("dark_drift_counts")}
        serial = getattr(self.backend, "serial", None)
        fp = self.wl_check["fingerprint"] if self.wl_check else self.backend.wl_fingerprint()
        p = cp.new_profile(kind=self.kind, backend=self.backend_name, serial=serial,
                           optics=self.optics, reference=reference, wl_fingerprint=fp,
                           result=self.result, acquisition=acq, checks=checks,
                           ref_unit=ref.unit, plausibility=stored_pl,
                           quantity=self.quantity)
        saved = [cp.add_profile(p)]
        if self.kind == cp.TYPE_ABSOLUTE:
            S, valid = cp.relative_from_absolute(self.result["K"], self.result["valid"])
            rel = dict(self.result, S=S, K=None, valid=valid, type=cp.TYPE_RELATIVE)
            q = cp.new_profile(kind=cp.TYPE_RELATIVE, backend=self.backend_name,
                               serial=serial, optics=self.optics, reference=reference,
                               wl_fingerprint=fp, result=rel, acquisition=acq,
                               checks=checks, ref_unit=ref.unit,
                               plausibility=stored_pl, quantity=self.quantity)
            q["derived_from"] = {"id": saved[0]["id"], "how": "relative_from_absolute"}
            saved.append(cp.add_profile(q))
        self.saved = saved
        self.saved_with_override = override
        return saved

    def run_all(self, *, integration_us: int | None = None, sleep=time.sleep,
                save: bool = True) -> dict:
        """Headless full run (shutter devices / tests): steps 2–9."""
        self.check_wavelength()
        self.set_integration(integration_us, auto=integration_us is None)
        self.capture_darks()
        self.capture_calibration(sleep=sleep)
        self.crosscheck_integration()
        self.capture_darks_after()
        self.compute()
        if save:
            self.save()
        return self.record()

    # ── protocol record ──────────────────────────────────────────────────
    def record(self) -> dict:
        """JSON-able run record for calibration_report (PDF/CSV)."""
        res = self.result or {}
        ref = self.reference
        wl = None if self.rate is None else self.rate.wl

        def arr(a):
            return None if a is None else [None if not np.isfinite(v) else float(v)
                                           for v in np.asarray(a, float)]
        spectra = {}
        if wl is not None:
            spectra = {
                "wl": arr(wl),
                "C_cal": arr(res.get("C_cal", self.rate.rate)),
                "E_ref": arr(self.reference_on_axis(wl)),
                "S": arr(res.get("S")),
                "K": arr(res.get("K")),
                "valid": None if "valid" not in res else [bool(v) for v in res["valid"]],
                "C_half": None if self.rate_half is None else arr(self.rate_half.rate),
                "C_repeat": None if self.rate2 is None else arr(self.rate2.rate),
                "check_fail": None if self.pixel_ok is None
                else [bool(not v) for v in self.pixel_ok],
            }
        return {
            "kind": "calibration_run",
            "started": self.started,
            "finished": cc.now_iso(),
            "type": self.kind,
            "quantity": self.quantity,
            "source_hint": (RADIANCE_SOURCE_HINT if self.quantity == cp.QUANTITY_RADIANCE
                            else ""),
            "device": {"backend": self.backend_name,
                       "serial": getattr(self.backend, "serial", None)},
            "optics": dict(self.optics),
            "reference": {"id": ref.id, "name": ref.name, "serial": ref.serial,
                          "unit": ref.unit, "quantity": self.quantity,
                          "valid_min_nm": ref.valid_min_nm,
                          "valid_max_nm": ref.valid_max_nm,
                          "source_file": ref.source_file},
            "wl_fingerprint": None if not self.wl_check else self.wl_check["fingerprint"],
            "wl_check_messages": [] if not self.wl_check else list(self.wl_check["messages"]),
            "acquisition": {"integration_us": self.integration_us,
                            "half_integration_us": self.half_us,
                            "averages": self.cfg.n_frames,
                            "nl_correction": self.backend.nl_correction_active,
                            "nl_correction_text": _onoff_na(self.backend.nl_correction_active),
                            "linearity": _lin_tag(self.backend),
                            "linearity_text": _lin_text(self.backend),
                            "dark_sequence": "before+after (time-interpolated)"
                            if self.dark_after is not None else "before",
                            "peak_frac": None if self.light is None else self.light.peak_frac,
                            "saturation_level": None if self.light is None
                            else self.light.saturation_level,
                            "full_scale": None if self.light is None else self.light.full_scale},
            "settings": asdict(self.cfg),
            "valid_range_nm": res.get("valid_range_nm"),
            "valid_segments_nm": res.get("segments_nm"),
            "plausibility": _stored_plausibility(self.plausibility,
                                                 self.saved_with_override),
            "gain_span": res.get("gain_span"),
            "n_valid": res.get("n_valid"),
            "unit": (cp.UNIT_RELATIVE if self.kind == cp.TYPE_RELATIVE
                     else f"{ref.unit} per count/s"),
            "checks": {k: v for k, v in self.checks.items() if not k.endswith("_detail")},
            "pixel_checks": {k: {kk: vv for kk, vv in v.items()
                                 if kk in ("n_tested", "n_fail", "fail_frac", "k",
                                           "threshold_pct")}
                             for k, v in self.pixel_checks.items()},
            "causes": self.causes,
            "dark_drift": list(self.dark_drift),
            "verification": self.verification,
            "warnings": list(self.warnings),
            "profiles": [{"id": p["id"], "type": p["type"], "version": p["version"]}
                         for p in self.saved],
            "spectra": spectra,
        }


def derivation_source_ok(profile: dict, backend, backend_name: str) -> tuple[bool, str]:
    """A known profile is usable for a derivation only if it is a v2 profile
    (legacy tables carry S = 1 outside their reliable range) that is valid on
    this unit with the CURRENT wavelength calibration."""
    if profile.get("apply") != cp.APPLY_V2:
        return False, ("Legacy profiles cannot be used as the known optics "
                       "(no valid-range mask). Calibrate it with the wizard first.")
    st = cp.profile_status(profile, backend_name, getattr(backend, "serial", None),
                           backend.wl_fingerprint(), cp.device_nl_active(backend))
    if st["state"] in cp.CONFIRM_STATES + (cp.STATE_OTHER_DEVICE,):
        return False, f"Known profile not valid on this device: {st['message']}"
    return True, ""


SIDE_KNOWN, SIDE_NEW, SIDE_REPEAT = "known", "new", "repeat"
DERIVATION_SIDES = (SIDE_KNOWN, SIDE_NEW)

# Text shown by the desktop wizard and the web dialog / API job (one source).
DERIVATION_SOURCE_HINT = (
    "Source: a diffuse, uniformly luminous, extended target that fills the field "
    "of view of BOTH optics (e.g. back-illuminated opal glass in the object "
    "plane). Keep the source, its position and its power unchanged during the "
    "whole run.")

# User steps of a derivation run in their order (desktop wizard, API job).
# "cover"/"uncover" only on devices without a shutter.
DERIVATION_STEPS = (
    ("mount_known", "Mount the KNOWN optics on the source"),
    ("integration_known", "Integration time (known optics)"),
    ("dark_known", "Dark spectrum (known optics)"),
    ("light_known", "Light spectrum (known optics)"),
    ("swap_optics", "Mount the NEW optics on the same source"),
    ("integration_new", "Integration time (new optics)"),
    ("dark_new", "Dark spectrum (new optics)"),
    ("light_new", "Light spectrum (new optics)"),
    ("dark_new_after", "Dark spectrum after (new optics)"),
    ("swap_back", "Mount the KNOWN optics again"),
    ("repeat_known", "Repeat with the known optics (source drift)"),
    ("dark_known_after", "Dark spectrum after (known optics)"),
    ("compute", "Compute S_new"),
    ("save", "Save the profile"),
)


class DerivationWorkflow:
    """Derive the relative profile of a NEW optics configuration from a KNOWN
    profile, measuring the same diffuse, extended source with both optics:
        S_new = S_known · C_new / C_known
    with C the count rates [counts/s] after linearisation (the unit's
    linearity correction is applied in the capture path, exactly as in the
    spectral calibration) and dark subtraction.

    Each side has its OWN integration time (auto exposure to the same target
    band calibration_target_frac_lo/hi, or a fixed value): the brightness of
    two optics on the same source can differ by orders of magnitude (bare
    fibre vs. cosine corrector). Because the calculation uses count rates, the
    result does not depend on the chosen integration times; driving both sides
    to the same ADC band also keeps residual nonlinearity equal on both sides.

    Sequence (user steps between them — see DERIVATION_STEPS):
      known optics: set_integration("known") → capture_dark_for("known")
                    → capture_light_for("known")
      new optics:   set_integration("new") → capture_dark_for("new")
                    → capture_light_for("new") → capture_dark_for("new", "after")
      known again:  capture_light_for("repeat") → capture_dark_for("known", "after")
      compute → save
    The darks "after" are optional (without them the dark before is used);
    with them every light capture is corrected with the dark interpolated in
    time between its side's dark before and after (block dark, as in the
    spectral calibration). The repeat of the known optics is optional too;
    with it the source drift is evaluated per pixel and overall against
    cfg.derivation_drift_pct, failing pixels are masked (they limit the valid
    range) and the known count rate is interpolated in time to the capture of
    the new optics (compensates a linear drift)."""

    def __init__(self, backend, backend_name: str, *, known_profile_id: str,
                 optics: dict, cfg: WorkflowConfig | None = None):
        self.backend = backend
        self.backend_name = backend_name
        self.known = cp.get_profile_by_id(known_profile_id)
        if self.known is None:
            raise KeyError(known_profile_id)
        ok, why = derivation_source_ok(self.known, backend, backend_name)
        if not ok:
            raise ValueError(why)
        if optics.get("input") not in cp.INPUT_OPTICS or not optics.get("name"):
            raise ValueError("Name the new optics configuration and its input optic.")
        self.optics = {"name": optics["name"], "input": optics["input"],
                       "notes": optics.get("notes", "")}
        self.cfg = cfg or WorkflowConfig.from_config()
        self.itime: dict = {SIDE_KNOWN: None, SIDE_NEW: None}
        self.exposure: dict = {SIDE_KNOWN: None, SIDE_NEW: None}   # AE / fixed info
        self.darks: dict = {SIDE_KNOWN: {"before": None, "after": None},
                            SIDE_NEW: {"before": None, "after": None}}
        self.lights: dict = {SIDE_KNOWN: None, SIDE_NEW: None, SIDE_REPEAT: None}
        self.linearity: dict = {}          # which → linearity tag at the capture
        self.rate_known = self.rate_new = self.rate_repeat = None
        self.rate_known_used = None        # known rate the ratio uses (drift-interpolated)
        self.drift: dict | None = None
        self.drift_check: dict | None = None
        self.pixel_ok = None
        self.dark_drift: list[dict] = []
        self.checks: dict = {}
        self.result = None
        self.saved: list[dict] = []
        self.warnings: list[str] = []
        self.plausibility: dict | None = None
        self.saved_with_override = False
        self.verification = None
        self.started = cc.now_iso()
        self.wl_fp = backend.wl_fingerprint()

    # ── helpers ──────────────────────────────────────────────────────────
    @property
    def plausible(self) -> bool:
        return bool(self.plausibility is None or self.plausibility["pass"])

    @property
    def drift_failed(self) -> bool:
        """True when the source drift check was evaluated and exceeded."""
        return self.checks.get("drift_pass") is False

    def drift_status(self) -> dict | None:
        """Unambiguous drift result (None when no drift was evaluated)."""
        d = self.drift
        if not d or self.checks.get("drift_pass") is None:
            return None
        p95, mean = d.get("p95_pct"), d.get("mean_pct")
        lim = float(d.get("threshold_pct", self.cfg.derivation_drift_pct))
        failed = self.drift_failed
        fmt = lambda v: "n/a" if v is None else f"{float(v):.2f}"
        msg = (f"Drift check failed: {fmt(p95)} % (limit {lim:.2f} %), mean {fmt(mean)} %"
               if failed else
               f"Drift check passed: {fmt(p95)} % (limit {lim:.2f} %), mean {fmt(mean)} %")
        return {"status": "drift_check_failed" if failed else "drift_check_passed",
                "p95_pct": p95, "mean_pct": mean, "limit_pct": lim, "message": msg}

    @property
    def checks_passed(self) -> bool:
        """False when the drift check or the per-side consistency checks
        failed (status like a failed stability check: saving needs a
        confirmation / save_on_failed_checks)."""
        return not any(self.checks.get(k) is False for k in
                       ("drift_pass", "itime_match_known", "itime_match_new",
                        "linearity_consistent"))

    @property
    def needs_user_cover(self) -> bool:
        return not getattr(self.backend, "SUPPORTS_SHUTTER", False)

    @property
    def integration_us(self):
        """Legacy: the common integration time when both sides use the same
        one, else the dict {"known": …, "new": …}."""
        k, n = self.itime[SIDE_KNOWN], self.itime[SIDE_NEW]
        return k if k == n else dict(self.itime)

    @staticmethod
    def _side(which: str) -> str:
        if which not in DERIVATION_SIDES:
            raise ValueError(f"side must be one of {DERIVATION_SIDES}, not {which!r}")
        return which

    def _warn(self, msg: str | None) -> None:
        if msg and msg not in self.warnings:
            self.warnings.append(msg)

    def _check_fp(self) -> None:
        if self.backend.wl_fingerprint() != self.wl_fp:
            raise RuntimeError("The wavelength calibration changed during the run "
                               "— restart the derivation.")

    def _same_t(self, a: int, b: int) -> bool:
        return abs(int(a) - int(b)) <= (self.cfg.itime_match_tol_pct / 100.0) * max(int(a), 1)

    # ── integration time (per side) ──────────────────────────────────────
    def set_integration(self, which=None, integration_us: int | None = None, *,
                        auto: bool = True):
        """Integration time of one side ("known" | "new"). integration_us None
        (and auto) → auto exposure with that side's optics mounted and the
        source on, to cfg.target_frac (calibration_target_frac_lo/hi); else
        the fixed value (clamped by the device). Returns {"which",
        "integration_us", "converged", "peak_frac", "auto"}.

        Legacy form set_integration(us) / set_integration(None) (no side):
        one integration time for BOTH sides; returns the int."""
        if which is None or (isinstance(which, numbers.Real)
                             and not isinstance(which, bool)):
            us = which if integration_us is None else integration_us
            info = self.set_integration(SIDE_KNOWN, us, auto=auto if us is None else True)
            t = info["integration_us"]
            self._assign_itime(SIDE_NEW, t)
            self.exposure[SIDE_NEW] = dict(info, which=SIDE_NEW, shared=True)
            return t
        which = self._side(which)
        if integration_us is None and not auto:
            raise ValueError(f"give the integration time of the {which} optics or use "
                             f"auto exposure")
        b = self.backend
        cap, converged, is_auto = None, True, integration_us is None
        if is_auto:
            t, cap, converged = cc.auto_expose(b, target_frac=self.cfg.target_frac,
                                               backend_name=self.backend_name)
        else:
            b.set_integration_time_us(int(integration_us))
            t = int(b.current_integration_time_us)
        self._assign_itime(which, int(t))
        info = {"which": which, "integration_us": t, "converged": bool(converged),
                "peak_frac": None if cap is None else float(cap.peak_frac),
                "auto": bool(is_auto),
                "target_frac": [float(self.cfg.target_frac[0]),
                                float(self.cfg.target_frac[1])]}
        self.exposure[which] = info
        if not converged:
            self._warn(f"Auto exposure ({which} optics) did not reach the target band "
                       f"{self.cfg.target_frac[0]*100:.0f}–"
                       f"{self.cfg.target_frac[1]*100:.0f} % FS.")
        return info

    def _invalidate(self, which: str) -> None:
        """Captures of a side are no longer usable (new t_int): drop them and
        everything computed from them."""
        self.darks[which] = {"before": None, "after": None}
        self.lights[which] = None
        setattr(self, f"rate_{which}", None)
        self.linearity.pop(which, None)
        if which == SIDE_KNOWN:
            self.lights[SIDE_REPEAT] = None
            self.rate_repeat = None
            self.linearity.pop(SIDE_REPEAT, None)
        self.rate_known_used = None
        self.drift = self.drift_check = self.pixel_ok = None
        self.result = self.plausibility = None

    def _assign_itime(self, which: str, t: int) -> None:
        if self.itime[which] is not None and not self._same_t(self.itime[which], t):
            self._invalidate(which)
        self.itime[which] = int(t)

    # ── captures ─────────────────────────────────────────────────────────
    # Split dark / light steps so a GUI can prompt "cover" / "uncover" and
    # "swap optics" between them on devices without a shutter.
    def capture_dark_for(self, which: str, phase: str = "before"):
        """Dark at the side's own integration time. phase "before" (before the
        side's light capture) or "after" (after it; for the known optics after
        the repeat)."""
        which = self._side(which)
        if phase not in ("before", "after"):
            raise ValueError("phase must be 'before' or 'after'")
        t = self.itime[which]
        if t is None:
            raise RuntimeError(f"set the integration time of the {which} optics first")
        if phase == "after" and self.darks[which]["before"] is None:
            raise RuntimeError("capture the dark spectrum before first")
        d = cc.capture_dark(self.backend, self.cfg.n_frames, integration_us=t,
                            backend_name=self.backend_name)
        if not self._same_t(d.integration_us, t):
            raise RuntimeError(f"The device ran the dark at {d.integration_us/1000:.3f} ms "
                               f"instead of {t/1000:.3f} ms — repeat the dark.")
        cc.save_dark(d)
        if phase == "before":
            # a new dark before starts the side's block again: the old dark
            # after (taken before this one) must not be interpolated with it
            self.darks[which]["after"] = None
        self.darks[which][phase] = d
        self.backend.set_integration_time_us(t)
        return d

    def capture_light_for(self, which: str):
        """Light capture: "known", "new", or "repeat" (known optics mounted
        again at the end, at the known integration time)."""
        side = SIDE_KNOWN if which == SIDE_REPEAT else self._side(which)
        dark = self.darks[side]["before"]
        if dark is None:
            raise RuntimeError("capture the dark spectrum first")
        if which == SIDE_REPEAT and self.lights[SIDE_KNOWN] is None:
            raise RuntimeError("capture the known optics first")
        self._check_fp()
        t = self.itime[side]
        light = cc.capture(self.backend, self.cfg.n_frames, integration_us=t,
                           backend_name=self.backend_name)
        if not self._same_t(light.integration_us, dark.integration_us):
            raise RuntimeError("Dark and light were taken at different integration "
                               "times — capture the dark again.")
        if light.n_saturated:
            raise RuntimeError(f"{light.n_saturated} pixel(s) saturated with the "
                               f"{side} optics — reduce the integration time of this "
                               f"side and repeat its measurement (dark + light).")
        what = {"known": "with the known optics", "new": "with the new optics",
                "repeat": "of the repeat with the known optics"}[which]
        self._warn(_high_exposure_warning(light, self.cfg.saturation_warn_frac, what))
        if dark.peak_frac > self.cfg.dark_bright_frac * max(light.peak_frac, 1e-9):
            self._warn(f"The dark spectrum ({side} optics) is unusually bright "
                       f"compared to the light spectrum — was the light blocked?")
        self.lights[which] = light
        self.linearity[which] = _lin_tag(self.backend)
        rate = cc.count_rate(light, dark)
        for w in rate.warnings:
            self._warn(w)
        setattr(self, f"rate_{which}", rate)
        return rate

    def capture_known(self):
        """Dark + light with the KNOWN optics (shutter devices / headless).
        Sets the integration time first if not done yet (auto exposure)."""
        if self.itime[SIDE_KNOWN] is None:
            self.set_integration(SIDE_KNOWN)
        self.capture_dark_for(SIDE_KNOWN)
        return self.capture_light_for(SIDE_KNOWN)

    def capture_new(self, *, dark_after: bool = True):
        """Dark + light (+ dark after) with the NEW optics (shutter / headless)."""
        if self.itime[SIDE_NEW] is None:
            self.set_integration(SIDE_NEW)
        self.capture_dark_for(SIDE_NEW)
        r = self.capture_light_for(SIDE_NEW)
        if dark_after:
            self.capture_dark_for(SIDE_NEW, "after")
        return r

    def capture_repeat(self, *, dark_after: bool = True):
        """Repeat with the KNOWN optics (+ dark after) — shutter / headless."""
        r = self.capture_light_for(SIDE_REPEAT)
        if dark_after:
            self.capture_dark_for(SIDE_KNOWN, "after")
        return r

    # ── evaluation ───────────────────────────────────────────────────────
    def _rate(self, which: str):
        side = SIDE_KNOWN if which == SIDE_REPEAT else which
        light = self.lights[which]
        if light is None:
            return None
        d = self.darks[side]
        return cc.count_rate(light, cc.interpolate_dark(d["before"], d["after"],
                                                        light.t_mid))

    def _rates(self) -> None:
        """Count rates from the captures present now (None where missing)."""
        for w in (SIDE_KNOWN, SIDE_NEW, SIDE_REPEAT):
            setattr(self, f"rate_{w}", self._rate(w))

    def _itime_checks(self) -> None:
        """Dark and light of each side at the side's own integration time."""
        for side in DERIVATION_SIDES:
            t = self.itime[side]
            caps = [self.darks[side]["before"], self.darks[side]["after"], self.lights[side]]
            if side == SIDE_KNOWN:
                caps.append(self.lights[SIDE_REPEAT])
            caps = [c for c in caps if c is not None]
            ok = bool(t is not None and caps
                      and all(self._same_t(c.integration_us, t) for c in caps))
            self.checks[f"itime_match_{side}"] = ok
            if not ok:
                self._warn(f"Dark and light of the {side} optics were not taken at the "
                           f"same integration time.")

    def _dark_drift(self) -> None:
        self.dark_drift = []
        for side in DERIVATION_SIDES:
            b, a = self.darks[side]["before"], self.darks[side]["after"]
            light = self.lights[side]
            if b is None or a is None or light is None:
                continue
            net_peak = max(float(light.peak_counts - np.percentile(b.mean, 50)), 1.0)
            d = cc.dark_drift(b, a)
            d["side"] = side
            d["warn"] = bool(abs(d["mean_counts"]) > max(
                3.0 * d["sigma_counts"], self.cfg.dark_drift_warn_pct / 100.0 * net_peak))
            d["pct_of_peak"] = 100.0 * d["mean_counts"] / net_peak
            self.dark_drift.append(d)
            if d["warn"]:
                self._warn(f"Dark drift ({side} optics) {d['mean_counts']:+.1f} counts "
                           f"({d['pct_of_peak']:+.2f} % of the peak signal) between the "
                           f"dark before and after at {d['integration_us']/1000:.3f} ms "
                           f"— time-interpolated; check the detector warm-up.")
        self.checks["dark_drift_counts"] = {d["side"]: round(d["mean_counts"], 2)
                                            for d in self.dark_drift}

    def _linearity_check(self) -> None:
        tags = {k: v for k, v in self.linearity.items() if v is not None}
        ref = tags.get(SIDE_KNOWN)
        same = all(v == ref for v in tags.values())
        self.checks["linearity_consistent"] = bool(same)
        if not same:
            self._warn("The linearity correction changed between the captures — the "
                       "count rates of both optics are not comparable; repeat the run.")

    def _usable_known(self, rk) -> np.ndarray:
        """Pixels of the known optics that can be tested: in the known
        profile's valid range, above the SNR limit, not saturated."""
        ap = cp.CalibrationApplier(self.known, rk.wl)
        return (ap.valid & np.isfinite(rk.rate) & ~rk.saturated
                & (rk.rate > self.cfg.snr_k * rk.sigma_dark))

    def evaluate_drift(self) -> dict | None:
        """Source drift from the repeat of the known optics: per pixel
        (calibration_profiles.pixel_check, noise-dependent tolerance +
        cfg.derivation_drift_pct) and overall (p95 of the smoothed ratio and
        mean ratio). Failing pixels are masked (self.pixel_ok). None without
        a repeat."""
        self.drift, self.drift_check = None, None
        rk, rr = self.rate_known, self.rate_repeat
        n = rk.rate.size
        self.pixel_ok = np.ones(n, bool)
        self.rate_known_used = rk
        if rr is None:
            self.checks["drift_pass"] = None
            self._warn("Source drift not checked (no repeat with the known optics).")
            return None
        cfg = self.cfg
        m = self._usable_known(rk) & ~rr.saturated
        pc = cp.pixel_check(rk.rate, rr.rate, rk.sigma_rate, rr.sigma_rate, m,
                            threshold_pct=cfg.derivation_drift_pct, k=cfg.pixel_check_k)
        # overall drift over ALL tested pixels: a uniform drift makes the
        # high-SNR pixels fail first, so the passing ones alone would hide it
        st = cp.compare_rates(rk.rate, rr.rate, m)
        full = float(self.lights[SIDE_KNOWN].full_scale)
        t = self.itime[SIDE_KNOWN]
        cz = cp.check_causes(rk.rate, rr.rate, rk.sigma_rate, rr.sigma_rate, m,
                             t_ref_us=t, t_test_us=t, full_scale=full, nonlinear=False,
                             min_effect_pct=cfg.cause_min_pct)
        t1, t2 = self.lights[SIDE_KNOWN].t_mid, self.lights[SIDE_REPEAT].t_mid
        with np.errstate(divide="ignore", invalid="ignore"):
            per_px = np.where(m, 100.0 * (rr.rate / rk.rate - 1.0), np.nan)
        mean_pct = (100.0 * (st["mean_ratio"] - 1.0)
                    if np.isfinite(st["mean_ratio"]) else float("nan"))
        p95 = st["p95_pct"]
        passed = bool(p95 is not None and np.isfinite(p95)
                      and p95 < cfg.derivation_drift_pct)
        def fin(v):
            return float(v) if v is not None and np.isfinite(v) else None
        p95, mean_pct = fin(p95), fin(mean_pct)
        self.drift = {"p95_pct": p95, "median_pct": fin(st["median_pct"]),
                      "max_pct": fin(st["max_pct"]), "mean_pct": mean_pct,
                      "n_compared": st["n"], "n_tested": pc["n_tested"],
                      "n_fail_pixels": pc["n_fail"], "fail_frac": pc["fail_frac"],
                      "threshold_pct": float(cfg.derivation_drift_pct),
                      "k": float(cfg.pixel_check_k), "pass": passed,
                      "seconds": (float(t2 - t1) if t1 and t2 else None),
                      "cause": cz["message"], "dominant": cz.get("dominant"),
                      "per_pixel_pct": per_px}
        self.drift_check = pc
        self.pixel_ok = pc["ok"].copy()
        self.checks.update({"drift_pct": p95, "drift_mean_pct": mean_pct,
                            "drift_pass": passed,
                            "drift_threshold_pct": float(cfg.derivation_drift_pct),
                            "drift_fail_pixels": pc["n_fail"],
                            "drift_cause": cz["message"]})
        self.warnings[:] = [w for w in self.warnings
                            if not w.startswith(("Source drift check failed",
                                                 "Source drift:",
                                                 "Pixels masked by the source drift"))]
        if not passed:
            p95_txt = "no comparable pixels" if p95 is None else f"{p95:.2f} %"
            self._warn(f"Source drift check failed: {p95_txt} (limit "
                       f"{cfg.derivation_drift_pct:.2f} %) between the capture with the "
                       f"known optics and its repeat — cause: {cz['message']}.")
        elif cz.get("dominant"):
            self._warn(f"Source drift: {cz['message']}.")
        if pc["n_fail"]:
            self._warn(f"Pixels masked by the source drift check: {pc['n_fail']}.")
        # known rate at the time of the new capture (linear in time between the
        # first capture and the repeat; mean when timestamps are missing)
        tn = self.lights[SIDE_NEW].t_mid if self.lights[SIDE_NEW] is not None else None
        if t1 and t2 and tn and t2 > t1:
            w = float(np.clip((tn - t1) / (t2 - t1), 0.0, 1.0))
        else:
            w = 0.5
        rate = (1.0 - w) * rk.rate + w * rr.rate
        sig_r = np.sqrt((1.0 - w) ** 2 * rk.sigma_rate ** 2 + w ** 2 * rr.sigma_rate ** 2)
        sig_d = np.sqrt((1.0 - w) * rk.sigma_dark ** 2 + w * rr.sigma_dark ** 2)
        import dataclasses
        self.rate_known_used = dataclasses.replace(
            rk, rate=rate, sigma_rate=sig_r, sigma_dark=sig_d,
            saturated=rk.saturated | rr.saturated, warnings=[])
        self.drift["interp_weight"] = w
        return self.drift

    def compute(self) -> dict:
        if self.lights[SIDE_KNOWN] is None or self.lights[SIDE_NEW] is None:
            raise RuntimeError("capture both optics first")
        self._rates()
        self._itime_checks()
        self._dark_drift()
        self._linearity_check()
        self.evaluate_drift()
        rk, rn = self.rate_known_used, self.rate_new
        wl = rk.wl
        ap = cp.CalibrationApplier(self.known, wl)
        # relative: factor = 1/S; absolute: factor = K ∝ 1/S → in both cases
        # S_known ∝ 1/factor (normalised again inside the derivation)
        with np.errstate(divide="ignore", invalid="ignore"):
            s_known = 1.0 / ap.factor
        usable = ap.valid & ~rk.saturated & ~rn.saturated & self.pixel_ok
        self.result = cp.derive_relative_from_ratio(
            wl, s_known, rn.rate, rk.rate,
            valid_known=usable, sigma_new=rn.sigma_dark,
            sigma_known=rk.sigma_dark, snr_k=self.cfg.snr_k,
            max_gain=self.cfg.max_gain, smooth_px=self.cfg.smooth_px)
        self.result["C_known"] = rk.rate
        self.result["C_new"] = rn.rate
        self.result["S_known"] = s_known
        self.plausibility = _plausibility(self.result, self.cfg, self.warnings)
        self.checks["broadband_pass"] = self.plausibility["pass"]
        return self.result

    def run_all(self, *, integration_known_us: int | None = None,
                integration_new_us: int | None = None, swap=None,
                save: bool = True) -> dict:
        """Headless full run (shutter devices / tests). swap(which) is called
        before the new optics ("new") and before the repeat ("known") — a test
        or script changes the optics there."""
        self.set_integration(SIDE_KNOWN, integration_known_us)
        self.capture_known()
        if swap:
            swap(SIDE_NEW)
        self.set_integration(SIDE_NEW, integration_new_us)
        self.capture_new()
        if swap:
            swap(SIDE_KNOWN)
        self.capture_repeat()
        self.compute()
        if save:
            self.save(allow_implausible=not self.plausible)
        return self.record()

    # ── save / protocol ──────────────────────────────────────────────────
    def _acquisition(self) -> dict:
        def peak(w):
            c = self.lights.get(w)
            return None if c is None else float(c.peak_frac)
        return {"integration_us": None if self.itime[SIDE_NEW] is None
                else int(self.itime[SIDE_NEW]),
                "integration_known_us": self.itime[SIDE_KNOWN],
                "integration_new_us": self.itime[SIDE_NEW],
                "averages": int(self.cfg.n_frames),
                "nl_correction": self.backend.nl_correction_active,
                "linearity": self.linearity.get(SIDE_NEW) or _lin_tag(self.backend),
                "linearity_known": self.linearity.get(SIDE_KNOWN),
                "peak_frac_known": peak(SIDE_KNOWN), "peak_frac_new": peak(SIDE_NEW),
                "peak_frac_repeat": peak(SIDE_REPEAT),
                "dark_sequence": {s: ("before+after (time-interpolated)"
                                      if self.darks[s]["after"] is not None else "before")
                                  for s in DERIVATION_SIDES},
                "snr_k": self.cfg.snr_k, "max_gain": self.cfg.max_gain,
                "smooth_px": self.cfg.smooth_px}

    def _profile_checks(self) -> dict:
        keys = ("broadband_pass", "drift_pct", "drift_mean_pct", "drift_pass",
                "drift_threshold_pct", "drift_fail_pixels", "drift_cause",
                "itime_match_known", "itime_match_new", "linearity_consistent",
                "dark_drift_counts")
        return {k: self.checks.get(k) for k in keys}

    def save(self, *, allow_implausible: bool = False) -> dict:
        if self.result is None:
            raise RuntimeError("compute first")
        if not self.plausible and not allow_implausible:
            raise PlausibilityError("; ".join(self.plausibility["failures"]))
        self._check_fp()
        k = self.known
        override = not self.plausible
        p = cp.new_profile(kind=cp.TYPE_RELATIVE, backend=self.backend_name,
                           serial=getattr(self.backend, "serial", None),
                           optics=self.optics, reference=dict(k.get("reference") or {}),
                           wl_fingerprint=self.wl_fp,
                           result=self.result, acquisition=self._acquisition(),
                           checks=self._profile_checks(),
                           plausibility=_stored_plausibility(self.plausibility, override),
                           quantity=cp.profile_quantity(k))
        p["derived_from"] = {"id": k["id"], "how": "ratio C_new/C_known",
                             "known_optics": (k.get("optics") or {}).get("name"),
                             "known_version": k.get("version")}
        saved = cp.add_profile(p)
        self.saved = [saved]
        self.saved_with_override = override
        return saved

    def record(self) -> dict:
        """JSON-able run record for the derivation protocol (PDF/CSV)."""
        res = self.result or {}

        def arr(a):
            return None if a is None else [None if not np.isfinite(v) else float(v)
                                           for v in np.asarray(a, float)]

        def side(s):
            light = self.lights[s]
            rep = self.lights[SIDE_REPEAT] if s == SIDE_KNOWN else None
            lin = self.linearity.get(s)
            ex = self.exposure.get(s) or {}
            return {"integration_us": self.itime[s],
                    "auto_exposure": ex.get("auto"),
                    "converged": ex.get("converged"),
                    "peak_frac": None if light is None else float(light.peak_frac),
                    "peak_frac_repeat": None if rep is None else float(rep.peak_frac),
                    "n_saturated": None if light is None else light.n_saturated,
                    "saturation_level": None if light is None else light.saturation_level,
                    "full_scale": None if light is None else light.full_scale,
                    "linearity": lin,
                    "linearity_text": (None if lin is None
                                       else _lin_text_from_tag(lin)),
                    "dark_sequence": ("before+after (time-interpolated)"
                                      if self.darks[s]["after"] is not None else "before"),
                    "itime_match": self.checks.get(f"itime_match_{s}"),
                    "captured": None if light is None else light.timestamp}
        spectra = {}
        wl = None if self.rate_known is None else self.rate_known.wl
        if wl is not None:
            spectra = {
                "wl": arr(wl),
                "C_known": arr(self.rate_known.rate),
                "C_known_repeat": None if self.rate_repeat is None
                else arr(self.rate_repeat.rate),
                "C_known_used": None if self.rate_known_used is None
                else arr(self.rate_known_used.rate),
                "C_new": None if self.rate_new is None else arr(self.rate_new.rate),
                "S_known": arr(res.get("S_known")),
                "S": arr(res.get("S")),
                "valid": None if "valid" not in res else [bool(v) for v in res["valid"]],
                "drift_pct": None if not self.drift else arr(self.drift["per_pixel_pct"]),
                "drift_fail": None if self.drift_check is None
                else [bool(v) for v in self.drift_check["fail"]],
            }
        k = self.known
        drift = None if not self.drift else {
            kk: vv for kk, vv in self.drift.items() if kk != "per_pixel_pct"}
        return {
            "kind": "derivation_run",
            "started": self.started,
            "finished": cc.now_iso(),
            "type": cp.TYPE_RELATIVE,
            "device": {"backend": self.backend_name,
                       "serial": getattr(self.backend, "serial", None)},
            "optics": dict(self.optics),
            "quantity": cp.profile_quantity(k),
            "known_profile": {"id": k.get("id"), "version": k.get("version"),
                              "type": k.get("type"),
                              "quantity": cp.profile_quantity(k),
                              "optics": dict(k.get("optics") or {}),
                              "reference": dict(k.get("reference") or {})},
            "source_hint": DERIVATION_SOURCE_HINT,
            "wl_fingerprint": self.wl_fp,
            "sides": {s: side(s) for s in DERIVATION_SIDES},
            "acquisition": dict(self._acquisition(),
                                nl_correction_text=_onoff_na(
                                    self.backend.nl_correction_active),
                                linearity_text=_lin_text(self.backend)),
            "settings": asdict(self.cfg),
            "drift": drift,
            "drift_status": self.drift_status(),
            "checks": dict(self.checks),
            "dark_drift": list(self.dark_drift),
            "valid_range_nm": res.get("valid_range_nm"),
            "valid_segments_nm": res.get("segments_nm"),
            "plausibility": _stored_plausibility(self.plausibility,
                                                 self.saved_with_override),
            "gain_span": res.get("gain_span"),
            "n_valid": res.get("n_valid"),
            "unit": cp.UNIT_RELATIVE,
            "warnings": list(self.warnings),
            "profiles": [{"id": p["id"], "type": p["type"], "version": p["version"]}
                         for p in self.saved],
            "spectra": spectra,
        }


# ══════════════════════════════════════════════════════════════════════════
#  Linearity tags (stored in profiles / protocols)
# ══════════════════════════════════════════════════════════════════════════

def _lin_tag(backend) -> dict:
    import calibration_linearity as cl
    return cl.profile_linearity_tag(backend_linearity_info(backend))


def _lin_text(backend) -> str:
    import calibration_linearity as cl
    return cl.onoff_na(backend_linearity_info(backend))


def _lin_text_from_tag(tag: dict | None) -> str:
    """'on (<id>)' / 'off' / 'n/a' from a stored linearity tag."""
    import calibration_linearity as cl
    t = cl.onoff_na(tag or {})
    if isinstance(tag, dict) and tag.get("active") and tag.get("id"):
        t += f" ({tag['id']})"
    return t


def _onoff_na(v) -> str:
    """Device NL correction: 'on' / 'off' / 'n/a' (device has none)."""
    return "on" if v is True else ("off" if v is False else "n/a")


# ══════════════════════════════════════════════════════════════════════════
#  Verification of a saved profile against the reference file
# ══════════════════════════════════════════════════════════════════════════

def verify_against_reference(wl, values, e_ref, *, kind: str = cp.TYPE_RELATIVE,
                             valid=None, band_nm: float = 50.0,
                             norm_range_nm=(550.0, 650.0),
                             warn_pct: float = 2.0) -> dict:
    """Deviation of a measurement of the reference source (taken through the
    live pipeline with the new profile) from the reference file.

    Normalised comparison (always): measured / (scale · E_ref) − 1, with the
    scale fitted by least squares over norm_range_nm (default 550–650 nm, the
    range used in the hardware test; falls back to the whole valid overlap).
    Absolute profiles additionally report the unnormalised deviation.
    Per band of band_nm (aligned to multiples of band_nm) and overall.
    Returns {"norm_range_nm", "bands": [{lo, hi, n, mean_pct, max_abs_pct,
    warn}], "overall": {rms_pct, mean_abs_pct, max_abs_pct}, "absolute":
    {…} | None, "pass", "warn_pct", "curve": {"wl", "deviation_pct"}}."""
    wl = np.asarray(wl, float)
    v = np.asarray(values, float)
    e = np.asarray(e_ref, float)
    m = np.isfinite(v) & np.isfinite(e) & (e > 0)
    if valid is not None:
        m &= np.asarray(valid, bool)
    if np.count_nonzero(m) < 3:
        raise ValueError("No overlap between the measurement and the reference.")
    lo_n, hi_n = (float(norm_range_nm[0]), float(norm_range_nm[1])) if norm_range_nm \
        else (float(wl[m].min()), float(wl[m].max()))
    mn = m & (wl >= lo_n) & (wl <= hi_n)
    if np.count_nonzero(mn) < 3:
        mn = m
        lo_n, hi_n = float(wl[m].min()), float(wl[m].max())
    scale = float(np.sum(v[mn] * e[mn]) / np.sum(e[mn] ** 2))

    def stats(dev):
        bands = []
        if np.any(m):
            b0 = np.floor(wl[m].min() / band_nm) * band_nm
            b1 = np.ceil(wl[m].max() / band_nm) * band_nm
            for lo in np.arange(b0, b1, band_nm):
                sel = m & (wl >= lo) & (wl < lo + band_nm)
                if not np.any(sel):
                    continue
                d = dev[sel]
                mean = float(np.mean(d))
                bands.append({"lo": float(lo), "hi": float(lo + band_nm),
                              "n": int(d.size), "mean_pct": mean,
                              "max_abs_pct": float(np.max(np.abs(d))),
                              "warn": bool(abs(mean) > warn_pct)})
        d = dev[m]
        overall = {"rms_pct": float(np.sqrt(np.mean(d ** 2))),
                   "mean_abs_pct": float(np.mean(np.abs(d))),
                   "max_abs_pct": float(np.max(np.abs(d)))}
        return bands, overall

    with np.errstate(divide="ignore", invalid="ignore"):
        dev_n = np.where(m, 100.0 * (v / (scale * e) - 1.0), np.nan)
    bands, overall = stats(dev_n)
    absolute = None
    if kind == cp.TYPE_ABSOLUTE:
        with np.errstate(divide="ignore", invalid="ignore"):
            dev_a = np.where(m, 100.0 * (v / e - 1.0), np.nan)
        ab, ao = stats(dev_a)
        absolute = {"bands": ab, "overall": ao, "scale": 1.0}
    return {"norm_range_nm": [lo_n, hi_n], "band_nm": float(band_nm), "scale": scale,
            "bands": bands, "overall": overall, "absolute": absolute,
            "warn_pct": float(warn_pct),
            "pass": not any(b["warn"] for b in bands),
            "curve": {"wl": [float(x) for x in wl[m]],
                      "deviation_pct": [float(x) for x in dev_n[m]]}}


def format_verification(ver: dict | None, max_bands: int = 30) -> list[str]:
    """Human-readable lines (dialog, protocol, job result)."""
    if not ver:
        return []
    o = ver["overall"]
    lines = [f"Verification (normalised {ver['norm_range_nm'][0]:.0f}–"
             f"{ver['norm_range_nm'][1]:.0f} nm): rms {o['rms_pct']:.2f} %, "
             f"max {o['max_abs_pct']:.2f} %"]
    for b in ver["bands"][:max_bands]:
        lines.append(f"  {b['lo']:.0f}–{b['hi']:.0f} nm: {b['mean_pct']:+.2f} %"
                     + ("  (!)" if b["warn"] else ""))
    if ver.get("absolute"):
        ao = ver["absolute"]["overall"]
        lines.append(f"Absolute: rms {ao['rms_pct']:.2f} %, max {ao['max_abs_pct']:.2f} %")
    return lines

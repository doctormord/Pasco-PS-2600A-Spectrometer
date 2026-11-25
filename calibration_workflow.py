"""
calibration_workflow.py — The calibration wizard's fixed sequence, Qt-free.

Identical for every backend and every optics configuration (plan §4, Phase 2):

  1. choose device, optics configuration (name + input CC/bare/sphere),
     reference source from the library, type relative/absolute
     (absolute only with a defined entrance area, i.e. input != bare)
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
"""

from __future__ import annotations

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
        )


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


class CalibrationWorkflow:
    """State of one wizard run (relative or absolute, against a library lamp)."""

    def __init__(self, backend, backend_name: str, *, kind: str, optics: dict,
                 reference, cfg: WorkflowConfig | None = None):
        if kind not in (cp.TYPE_RELATIVE, cp.TYPE_ABSOLUTE):
            raise ValueError(f"unknown type '{kind}'")
        inp = optics.get("input", "")
        if inp not in cp.INPUT_OPTICS:
            raise ValueError(f"input optic must be one of {cp.INPUT_OPTICS}")
        if kind == cp.TYPE_ABSOLUTE and inp == "bare":
            raise ValueError("Absolute calibration needs a defined entrance area "
                             "(CC or sphere); a bare fibre is always relative.")
        if not optics.get("name"):
            raise ValueError("Name the optics configuration.")
        if kind == cp.TYPE_ABSOLUTE and not cp.is_absolute_reference_unit(
                getattr(reference, "unit", "")):
            raise ValueError(f"Absolute calibration needs a reference source with an "
                             f"absolute irradiance unit; '{reference.name}' is "
                             f"'{reference.unit}'. Use type 'relative'.")
        self.backend = backend
        self.backend_name = backend_name
        self.kind = kind
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
        if self.dark.peak_frac > 0.5 * max(self.light.peak_frac, 1e-9):
            self.warnings.append("The dark spectrum is unusually bright compared to "
                                 "the calibration spectrum — was the light blocked?")
        self.warnings.extend(w for w in self.rate.warnings if w not in self.warnings)
        if self.light.n_saturated:
            raise RuntimeError(self.rate.warnings[-1])
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
        if not self.pixel_checks:
            self.evaluate_checks()
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
                           ref_unit=ref.unit, plausibility=stored_pl)
        saved = [cp.add_profile(p)]
        if self.kind == cp.TYPE_ABSOLUTE:
            S, valid = cp.relative_from_absolute(self.result["K"], self.result["valid"])
            rel = dict(self.result, S=S, K=None, valid=valid, type=cp.TYPE_RELATIVE)
            q = cp.new_profile(kind=cp.TYPE_RELATIVE, backend=self.backend_name,
                               serial=serial, optics=self.optics, reference=reference,
                               wl_fingerprint=fp, result=rel, acquisition=acq,
                               checks=checks, ref_unit=ref.unit,
                               plausibility=stored_pl)
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
            "device": {"backend": self.backend_name,
                       "serial": getattr(self.backend, "serial", None)},
            "optics": dict(self.optics),
            "reference": {"id": ref.id, "name": ref.name, "serial": ref.serial,
                          "unit": ref.unit, "valid_min_nm": ref.valid_min_nm,
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


class DerivationWorkflow:
    """Derive the relative profile of a NEW optics configuration from a KNOWN
    profile, measuring the same diffuse, extended source with both optics:
        S_new = S_known · C_new / C_known
    Sequence: set_integration → capture_known (known optics mounted, incl. dark)
    → (user swaps optics) → capture_new (incl. dark) → compute → save."""

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
        self.integration_us = None
        self.rate_known = self.rate_new = None
        self.result = None
        self.saved: list[dict] = []
        self.warnings: list[str] = []
        self.plausibility: dict | None = None

    @property
    def plausible(self) -> bool:
        return bool(self.plausibility is None or self.plausibility["pass"])

    def set_integration(self, integration_us: int | None = None) -> int:
        if integration_us is None:
            t, _cap, _ok = cc.auto_expose(self.backend, target_frac=self.cfg.target_frac,
                                          backend_name=self.backend_name)
        else:
            self.backend.set_integration_time_us(int(integration_us))
            t = int(self.backend.current_integration_time_us)
        self.integration_us = t
        return t

    @property
    def needs_user_cover(self) -> bool:
        return not getattr(self.backend, "SUPPORTS_SHUTTER", False)

    # Split dark / light steps so a GUI can prompt "cover" / "uncover" between
    # them on devices without a shutter. which ∈ {"known", "new"}.
    def capture_dark_for(self, which: str):
        d = cc.capture_dark(self.backend, self.cfg.n_frames,
                            integration_us=self.integration_us,
                            backend_name=self.backend_name)
        setattr(self, f"_dark_{which}", d)
        return d

    def capture_light_for(self, which: str):
        dark = getattr(self, f"_dark_{which}", None)
        if dark is None:
            raise RuntimeError("capture the dark spectrum first")
        light = cc.capture(self.backend, self.cfg.n_frames,
                           integration_us=self.integration_us,
                           backend_name=self.backend_name)
        if abs(light.integration_us - dark.integration_us) > \
                cc.ITIME_REL_TOL * max(light.integration_us, 1):
            raise RuntimeError("Dark and light were taken at different integration "
                               "times — capture the dark again.")
        if light.n_saturated:
            # e.g. a bare fibre is much brighter than a CC at the same t_int:
            # lower t_int (count rates are t_int independent) and repeat.
            raise RuntimeError(f"{light.n_saturated} pixel(s) saturated with the "
                               f"{which} optics — reduce the integration time and "
                               f"repeat this measurement (dark + light).")
        rate = cc.count_rate(light, dark)
        self.warnings.extend(w for w in rate.warnings if w not in self.warnings)
        setattr(self, f"rate_{which}", rate)
        return rate

    def capture_known(self):
        """Dark + light with the KNOWN optics (shutter devices / headless)."""
        self.capture_dark_for("known")
        return self.capture_light_for("known")

    def capture_new(self):
        """Dark + light with the NEW optics (shutter devices / headless)."""
        self.capture_dark_for("new")
        return self.capture_light_for("new")

    def compute(self) -> dict:
        wl = self.rate_known.wl
        ap = cp.CalibrationApplier(self.known, wl)
        # relative: factor = 1/S; absolute: factor = K ∝ 1/S → in both cases
        # S_known ∝ 1/factor (normalised again inside the derivation)
        with np.errstate(divide="ignore", invalid="ignore"):
            s_known = 1.0 / ap.factor
        usable = ap.valid & ~self.rate_known.saturated & ~self.rate_new.saturated
        self.result = cp.derive_relative_from_ratio(
            wl, s_known, self.rate_new.rate, self.rate_known.rate,
            valid_known=usable, sigma_new=self.rate_new.sigma_dark,
            sigma_known=self.rate_known.sigma_dark, snr_k=self.cfg.snr_k,
            max_gain=self.cfg.max_gain, smooth_px=self.cfg.smooth_px)
        self.result["C_known"] = self.rate_known.rate
        self.result["C_new"] = self.rate_new.rate
        self.plausibility = _plausibility(self.result, self.cfg, self.warnings)
        return self.result

    def save(self, *, allow_implausible: bool = False) -> dict:
        if self.result is None:
            raise RuntimeError("compute first")
        if not self.plausible and not allow_implausible:
            raise PlausibilityError("; ".join(self.plausibility["failures"]))
        k = self.known
        acq = {"integration_us": int(self.integration_us),
               "averages": int(self.cfg.n_frames),
               "nl_correction": self.backend.nl_correction_active,
               "linearity": _lin_tag(self.backend)}
        p = cp.new_profile(kind=cp.TYPE_RELATIVE, backend=self.backend_name,
                           serial=getattr(self.backend, "serial", None),
                           optics=self.optics, reference=dict(k.get("reference") or {}),
                           wl_fingerprint=self.backend.wl_fingerprint(),
                           result=self.result, acquisition=acq,
                           checks={"broadband_pass": self.plausible},
                           plausibility=_stored_plausibility(
                               self.plausibility, not self.plausible))
        p["derived_from"] = {"id": k["id"], "how": "ratio C_new/C_known",
                             "known_optics": (k.get("optics") or {}).get("name")}
        saved = cp.add_profile(p)
        self.saved = [saved]
        return saved


# ══════════════════════════════════════════════════════════════════════════
#  Linearity tags (stored in profiles / protocols)
# ══════════════════════════════════════════════════════════════════════════

def _lin_tag(backend) -> dict:
    import calibration_linearity as cl
    return cl.profile_linearity_tag(backend_linearity_info(backend))


def _lin_text(backend) -> str:
    import calibration_linearity as cl
    return cl.onoff_na(backend_linearity_info(backend))


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

"""
calibration_workflow.py — The calibration wizard's fixed sequence, Qt-free.

Identical for every backend and every optics configuration (plan §4, Phase 2):

  1. choose device, optics configuration (name + input CC/bare/sphere),
     reference source from the library, type relative/absolute
     (absolute only with a defined entrance area, i.e. input != bare)
  2. wavelength-calibration check (fingerprint)
  3. integration time: auto exposure to the target band (70–85 % FS), then fixed
  4. dark spectra — at t_int AND at ~t_int/2 (the latter for the cross check);
     both in ONE covered phase, so a device without a shutter needs the user to
     cover / uncover only once
  5. calibration spectrum + automatic stability check (2nd capture after a delay)
  6. integration-time cross check (capture at ~t_int/2 with its own dark)
  7. compute S or K, valid range (plan §2.4), gain span
  8. save as a new profile version; absolute additionally saves the derived
     relative profile; build the run record for the protocol (PDF/CSV)

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

    @classmethod
    def from_config(cls) -> "WorkflowConfig":
        try:
            from app_config import Config as C
        except Exception:
            return cls()
        return cls(
            n_frames=max(2, int(C.get("calibration_capture_frames", 16))),
            snr_k=float(C.get("calibration_snr_k", 10.0)),
            max_gain=float(C.get("calibration_max_gain", 50.0)),
            smooth_px=int(C.get("calibration_smooth_px", 5)),
            target_frac=(float(C.get("calibration_target_frac_lo", 0.70)),
                         float(C.get("calibration_target_frac_hi", 0.85))),
            stability_delay_s=float(C.get("calibration_stability_delay_s", 5.0)),
            stability_threshold_pct=float(C.get("calibration_stability_threshold_pct", 1.0)),
            itime_threshold_pct=float(C.get("calibration_itime_threshold_pct", 2.0)),
        )


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
        self.dark = self.dark_half = None
        self.light = self.light2 = self.light_half = None
        self.rate = self.rate2 = self.rate_half = None
        self.wl_check: dict | None = None
        self.result: dict | None = None
        self.checks: dict = {}
        self.warnings: list[str] = []
        self.saved: list[dict] = []
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
        """Step 4 (+ the dark for step 6). Light must be blocked (shutter is
        used automatically when the backend has one)."""
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
                "dark_mean": float(np.mean(self.dark.mean))}

    def capture_calibration(self, *, sleep=time.sleep) -> dict:
        """Step 5: calibration spectrum + stability check (2nd capture after
        cfg.stability_delay_s)."""
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
        self.rate2 = cc.count_rate(self.light2, self.dark)
        m = self._check_mask(self.rate)
        st = cp.compare_rates(self.rate.rate, self.rate2.rate, m)
        self.checks["stability_pct"] = st["p95_pct"]
        self.checks["stability_detail"] = st
        self.checks["stability_delay_s"] = self.cfg.stability_delay_s
        self.checks["stability_threshold_pct"] = self.cfg.stability_threshold_pct
        self.checks["stability_pass"] = bool(st["p95_pct"] < self.cfg.stability_threshold_pct)
        if not self.checks["stability_pass"]:
            self.warnings.append(f"Stability check failed: {st['p95_pct']:.2f} % > "
                                 f"{self.cfg.stability_threshold_pct:.2f} % (lamp "
                                 f"warmed up? fibre/connector fixed?).")
        return {"peak_frac": self.light.peak_frac, "stability": st,
                "pass": self.checks["stability_pass"]}

    def crosscheck_integration(self) -> dict:
        """Step 6: C(λ) at ~t/2 with its own dark must match C(λ) at t."""
        if self.half_us is None or self.dark_half is None:
            self.checks["itime_crosscheck_pct"] = None
            self.checks["itime_pass"] = None
            self.warnings.append("Integration-time cross check skipped (t_int at "
                                 "the device minimum).")
            return {"skipped": True}
        self._check_fp()
        self.light_half = cc.capture(self.backend, self.cfg.n_frames,
                                     integration_us=self.half_us,
                                     backend_name=self.backend_name)
        self.backend.set_integration_time_us(self.integration_us)
        self.rate_half = cc.count_rate(self.light_half, self.dark_half)
        m = self._check_mask(self.rate)
        st = cp.compare_rates(self.rate.rate, self.rate_half.rate, m)
        self.checks["itime_crosscheck_pct"] = st["p95_pct"]
        self.checks["itime_detail"] = st
        self.checks["itime_half_us"] = self.half_us
        self.checks["itime_threshold_pct"] = self.cfg.itime_threshold_pct
        self.checks["itime_pass"] = bool(st["p95_pct"] < self.cfg.itime_threshold_pct)
        if not self.checks["itime_pass"]:
            self.warnings.append(f"Integration-time cross check failed: "
                                 f"{st['p95_pct']:.2f} % > {self.cfg.itime_threshold_pct:.2f} % "
                                 f"(nonlinearity / dark mismatch?).")
        return {"crosscheck": st, "pass": self.checks["itime_pass"]}

    def _check_mask(self, rate):
        m = cp._range_mask(rate.wl, *self.backend.spec_range_nm)
        e = self.reference_on_axis(rate.wl)
        return m & np.isfinite(e) & (rate.rate > self.cfg.snr_k * rate.sigma_dark) & ~rate.saturated

    def reference_on_axis(self, wl=None):
        wl = np.asarray(self.backend.wavelength_array if wl is None else wl, float)
        return self.reference.on_axis(wl)

    def compute(self) -> dict:
        """Step 7. Mean of the two stability captures is used as C_cal."""
        if self.rate is None:
            raise RuntimeError("capture the calibration spectrum first")
        r = self.rate
        rate = r.rate if self.rate2 is None else 0.5 * (r.rate + self.rate2.rate)
        sat = r.saturated | (self.rate2.saturated if self.rate2 is not None else False)
        self.result = cp.compute_calibration(
            r.wl, rate, r.sigma_dark, self.reference_on_axis(r.wl), kind=self.kind,
            saturated=sat, spec_range_nm=self.backend.spec_range_nm,
            snr_k=self.cfg.snr_k, max_gain=self.cfg.max_gain,
            smooth_px=self.cfg.smooth_px)
        self.result["C_cal"] = rate
        return self.result

    def save(self) -> list[dict]:
        """Step 8. New profile version(s). Absolute → also the derived relative."""
        if self.result is None:
            raise RuntimeError("compute first")
        self._check_fp()
        ref = self.reference
        reference = {"name": ref.name, "serial": ref.serial, "unit": ref.unit,
                     "id": ref.id, "valid_range_nm": [ref.valid_min_nm, ref.valid_max_nm]}
        acq = {"integration_us": int(self.integration_us),
               "averages": int(self.cfg.n_frames),
               "nl_correction": self.backend.nl_correction_active,
               "dark_frames": int(self.dark.n_frames),
               "peak_frac": float(self.light.peak_frac),
               "snr_k": self.cfg.snr_k, "max_gain": self.cfg.max_gain,
               "smooth_px": self.cfg.smooth_px}
        checks = {"stability_pct": self.checks.get("stability_pct"),
                  "itime_crosscheck_pct": self.checks.get("itime_crosscheck_pct"),
                  "stability_pass": self.checks.get("stability_pass"),
                  "itime_pass": self.checks.get("itime_pass")}
        serial = getattr(self.backend, "serial", None)
        fp = self.wl_check["fingerprint"] if self.wl_check else self.backend.wl_fingerprint()
        p = cp.new_profile(kind=self.kind, backend=self.backend_name, serial=serial,
                           optics=self.optics, reference=reference, wl_fingerprint=fp,
                           result=self.result, acquisition=acq, checks=checks,
                           ref_unit=ref.unit)
        saved = [cp.add_profile(p)]
        if self.kind == cp.TYPE_ABSOLUTE:
            S, valid = cp.relative_from_absolute(self.result["K"], self.result["valid"])
            rel = dict(self.result, S=S, K=None, valid=valid, type=cp.TYPE_RELATIVE)
            q = cp.new_profile(kind=cp.TYPE_RELATIVE, backend=self.backend_name,
                               serial=serial, optics=self.optics, reference=reference,
                               wl_fingerprint=fp, result=rel, acquisition=acq,
                               checks=checks, ref_unit=ref.unit)
            q["derived_from"] = {"id": saved[0]["id"], "how": "relative_from_absolute"}
            saved.append(cp.add_profile(q))
        self.saved = saved
        return saved

    def run_all(self, *, integration_us: int | None = None, sleep=time.sleep,
                save: bool = True) -> dict:
        """Headless full run (shutter devices / tests): steps 2–8."""
        self.check_wavelength()
        self.set_integration(integration_us, auto=integration_us is None)
        self.capture_darks()
        self.capture_calibration(sleep=sleep)
        self.crosscheck_integration()
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
                            "peak_frac": None if self.light is None else self.light.peak_frac,
                            "saturation_level": None if self.light is None
                            else self.light.saturation_level,
                            "full_scale": None if self.light is None else self.light.full_scale},
            "settings": asdict(self.cfg),
            "valid_range_nm": res.get("valid_range_nm"),
            "gain_span": res.get("gain_span"),
            "n_valid": res.get("n_valid"),
            "unit": (cp.UNIT_RELATIVE if self.kind == cp.TYPE_RELATIVE
                     else f"{ref.unit} per count/s"),
            "checks": {k: v for k, v in self.checks.items() if not k.endswith("_detail")},
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
        return self.result

    def save(self) -> dict:
        k = self.known
        acq = {"integration_us": int(self.integration_us),
               "averages": int(self.cfg.n_frames),
               "nl_correction": self.backend.nl_correction_active}
        p = cp.new_profile(kind=cp.TYPE_RELATIVE, backend=self.backend_name,
                           serial=getattr(self.backend, "serial", None),
                           optics=self.optics, reference=dict(k.get("reference") or {}),
                           wl_fingerprint=self.backend.wl_fingerprint(),
                           result=self.result, acquisition=acq, checks={})
        p["derived_from"] = {"id": k["id"], "how": "ratio C_new/C_known",
                             "known_optics": (k.get("optics") or {}).get("name")}
        saved = cp.add_profile(p)
        self.saved = [saved]
        return saved

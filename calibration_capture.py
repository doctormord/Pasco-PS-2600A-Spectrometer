"""
calibration_capture.py — Generic, device-agnostic calibration capture.

Everything the spectral calibration measures goes through this module, for
every backend alike (Ocean HDX, PASCO, LR-2T, Demo):

  * Frames are taken from the backend's frame tap (BaseSpectrometer.add_frame_tap)
    — i.e. the driver's raw output AFTER detector linearisation and BEFORE fusion,
    display averaging, despeckle/smoothing and response correction.
  * N frames are averaged as a MEAN (per pixel), with the per-pixel standard
    deviation and a per-pixel saturation flag (any frame >= the backend's
    adc_saturation_level).
  * A per-pixel dark spectrum at the SAME integration time is mandatory; the
    count rate is  C = (mean_light − mean_dark) / t_int  [counts/s]. Negative
    values are kept (no clipping) — clipping would bias the mean of noisy pixels.
  * Dark spectra are stored per device (backend + serial) and integration time,
    with timestamp, so the main pipeline can use them as "Stored dark".

No Qt here. GUI/web drive the blocking helpers from a worker thread or use the
non-blocking CaptureSession.
"""

from __future__ import annotations

import json
import os
import threading
import time
from dataclasses import dataclass, field

import numpy as np

FRAME_STANDARD = "standard"     # == fusion.FRAME_STANDARD (kept local: no import cycle)

DEFAULT_DARK_FILE = "dark_spectra.json"
MAX_DARKS_PER_DEVICE = 24
ITIME_REL_TOL = 0.005           # 0.5 %: dark and light count as "same t_int"


def now_iso() -> str:
    """ISO-8601 timestamp with local time-zone offset."""
    from datetime import datetime
    return datetime.now().astimezone().isoformat(timespec="seconds")


# ══════════════════════════════════════════════════════════════════════════
#  Data containers
# ══════════════════════════════════════════════════════════════════════════

@dataclass
class Capture:
    """Mean of N raw (linearised) frames at one integration time."""
    wl: np.ndarray
    mean: np.ndarray                 # mean counts per pixel (dark NOT subtracted)
    std: np.ndarray                  # per-pixel std over frames (ddof=1; 0 if N=1)
    n_frames: int
    integration_us: int
    saturated: np.ndarray            # bool per pixel: any frame >= saturation_level
    saturation_level: float
    full_scale: float
    timestamp: str = field(default_factory=now_iso)
    backend: str = ""
    serial: str | None = None
    wl_fingerprint: str = ""
    kind: str = "light"              # "light" | "dark"
    # wall-clock (epoch seconds) of the first / last accepted frame — used to
    # interpolate the dark measured before and after a light capture in time
    t_start: float | None = None
    t_end: float | None = None
    linearity_bypassed: bool = False  # frames taken without the linearity correction
    # per-pixel variance of `mean` when it is not std²/n_frames (time-
    # interpolated dark from two captures); None → std² / n_frames
    sem2: np.ndarray | None = field(default=None, repr=False)

    @property
    def t_mid(self) -> float | None:
        if self.t_start is None or self.t_end is None:
            return None
        return 0.5 * (float(self.t_start) + float(self.t_end))

    @property
    def peak_counts(self) -> float:
        return float(np.max(self.mean)) if self.mean.size else 0.0

    @property
    def peak_frac(self) -> float:
        return self.peak_counts / self.full_scale if self.full_scale else 0.0

    @property
    def n_saturated(self) -> int:
        return int(np.count_nonzero(self.saturated))

    def to_dict(self) -> dict:
        return {
            "wl": np.asarray(self.wl, float).tolist(),
            "mean": np.asarray(self.mean, float).tolist(),
            "std": np.asarray(self.std, float).tolist(),
            "n_frames": int(self.n_frames),
            "integration_us": int(self.integration_us),
            "saturated": np.flatnonzero(self.saturated).tolist(),
            "saturation_level": float(self.saturation_level),
            "full_scale": float(self.full_scale),
            "timestamp": self.timestamp,
            "backend": self.backend,
            "serial": self.serial,
            "wl_fingerprint": self.wl_fingerprint,
            "kind": self.kind,
            "t_start": self.t_start,
            "t_end": self.t_end,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "Capture":
        mean = np.asarray(d["mean"], float)
        sat = np.zeros(mean.size, bool)
        idx = np.asarray(d.get("saturated", []), int)
        if idx.size:
            sat[idx] = True
        return cls(
            wl=np.asarray(d["wl"], float), mean=mean,
            std=np.asarray(d.get("std", np.zeros(mean.size)), float),
            n_frames=int(d.get("n_frames", 1)),
            integration_us=int(d["integration_us"]),
            saturated=sat,
            saturation_level=float(d.get("saturation_level", 0.0)),
            full_scale=float(d.get("full_scale", 0.0)),
            timestamp=str(d.get("timestamp", "")),
            backend=str(d.get("backend", "")),
            serial=d.get("serial"),
            wl_fingerprint=str(d.get("wl_fingerprint", "")),
            kind=str(d.get("kind", "light")),
            t_start=d.get("t_start"),
            t_end=d.get("t_end"),
        )


@dataclass
class CountRate:
    """C(λ) = (light − dark) / t_int  [counts/s], plus its uncertainty."""
    wl: np.ndarray
    rate: np.ndarray                 # counts/s (may be negative in noise)
    sigma_dark: np.ndarray           # single-frame dark noise per pixel [counts/s]
    sigma_rate: np.ndarray           # std of the mean rate per pixel [counts/s]
    saturated: np.ndarray            # bool per pixel (light capture)
    integration_us: int
    n_frames: int
    n_dark_frames: int
    warnings: list[str] = field(default_factory=list)
    wl_fingerprint: str = ""
    timestamp: str = field(default_factory=now_iso)
    peak_frac: float = 0.0           # light peak / full scale (exposure level)


# ══════════════════════════════════════════════════════════════════════════
#  Frame accumulation (the tap)
# ══════════════════════════════════════════════════════════════════════════

class FrameAccumulator:
    """Callable frame tap that averages N clean frames.

    Accepted frames: kind == "standard", ratio == 1 (no fast-preview scaling)
    and integration_us equal to the requested one (within ITIME_REL_TOL). The
    first `skip_first` accepted frames are discarded, because a driver may still
    deliver a frame integrated (partly) under the previous exposure setting."""

    def __init__(self, n_frames: int, integration_us: int, *,
                 saturation_level: float, skip_first: int = 1):
        self.n_target = max(1, int(n_frames))
        self.integration_us = int(integration_us)
        self.saturation_level = float(saturation_level)
        self.skip_first = max(0, int(skip_first))
        self._lock = threading.Lock()
        self.done = threading.Event()
        self._sum = None
        self._sumsq = None
        self._sat = None
        self.n = 0
        self._skipped = 0
        self._dark_sum = 0.0
        self.t_first: float | None = None
        self.t_last: float | None = None
        self.rejected: dict[str, int] = {}

    def _reject(self, why: str) -> None:
        self.rejected[why] = self.rejected.get(why, 0) + 1

    def __call__(self, pixels, dark=0.0, integration_us=0,
                 kind=FRAME_STANDARD, ratio=1.0) -> None:
        if self.done.is_set():
            return
        if kind != FRAME_STANDARD:
            self._reject("kind"); return
        if abs(float(ratio) - 1.0) > 1e-9:
            self._reject("scaled"); return
        t = int(integration_us)
        if abs(t - self.integration_us) > ITIME_REL_TOL * max(self.integration_us, 1):
            self._reject("itime"); return
        px = np.asarray(pixels, dtype=np.float64)
        with self._lock:
            if self._skipped < self.skip_first:
                self._skipped += 1
                return
            if self._sum is None or self._sum.shape != px.shape:
                self._sum = np.zeros_like(px)
                self._sumsq = np.zeros_like(px)
                self._sat = np.zeros(px.shape, bool)
                self.n = 0
                self._dark_sum = 0.0
                self.t_first = None
            now = time.time()
            if self.t_first is None:
                self.t_first = now
            self.t_last = now
            try:
                self._dark_sum += float(np.mean(dark))
            except (TypeError, ValueError):
                pass
            self._sum += px
            self._sumsq += px * px
            self._sat |= px >= self.saturation_level
            self.n += 1
            if self.n >= self.n_target:
                self.done.set()

    @property
    def dark_mean(self) -> float:
        """Mean of the driver's per-frame dark scalar (optical-black mean /
        baseline estimate) over the accepted frames; 0.0 if none."""
        with self._lock:
            return self._dark_sum / self.n if self.n else 0.0

    def stats(self):
        """(mean, std, saturated, n) of what was accumulated so far."""
        with self._lock:
            if self._sum is None or self.n == 0:
                return None
            n = self.n
            mean = self._sum / n
            if n > 1:
                var = (self._sumsq - n * mean * mean) / (n - 1)
                std = np.sqrt(np.maximum(var, 0.0))
            else:
                std = np.zeros_like(mean)
            return mean, std, self._sat.copy(), n


def _backend_name(backend) -> str:
    return str(getattr(backend, "display_name", "") or type(backend).__name__)


class CaptureSession:
    """Non-blocking capture on a live backend (GUI-friendly).

    start() prepares the backend (integration time, fast preview and auto
    exposure off, un-paused) and installs the tap; poll() reports progress;
    finish() removes the tap, restores the backend and returns the Capture."""

    def __init__(self, backend, n_frames: int, *, integration_us: int | None = None,
                 kind: str = "light", backend_name: str | None = None,
                 skip_first: int = 1, timeout_s: float | None = None,
                 raw: bool = False):
        self.backend = backend
        self.raw = bool(raw)            # bypass the linearity correction
        self.n_frames = max(1, int(n_frames))
        self.integration_us = int(integration_us or backend.current_integration_time_us)
        self.kind = kind
        self.backend_name = backend_name or _backend_name(backend)
        self.skip_first = skip_first
        t = self.integration_us / 1e6
        self.timeout_s = (timeout_s if timeout_s is not None
                          else 10.0 + 4.0 * (self.n_frames + skip_first + 2) * (t + 0.05))
        self.acc: FrameAccumulator | None = None
        self._restore: dict = {}
        self._t0 = 0.0
        self._active = False

    def start(self) -> None:
        b = self.backend
        self._restore = {
            "fast_preview_enabled": getattr(b, "fast_preview_enabled", False),
            "is_auto_exposure_active": getattr(b, "is_auto_exposure_active", False),
            "is_measurement_paused": getattr(b, "is_measurement_paused", False),
            "linearity_bypass": getattr(b, "linearity_bypass", False),
        }
        if self.raw:
            b.linearity_bypass = True
        b.fast_preview_enabled = False
        b.is_auto_exposure_active = False
        if b.current_integration_time_us != self.integration_us:
            b.set_integration_time_us(self.integration_us)
        # the device may clamp — capture at what it actually runs
        self.integration_us = int(b.current_integration_time_us)
        self.acc = FrameAccumulator(self.n_frames, self.integration_us,
                                    saturation_level=float(b.adc_saturation_level),
                                    skip_first=self.skip_first)
        b.add_frame_tap(self.acc)
        b.is_measurement_paused = False
        self._t0 = time.monotonic()
        self._active = True

    def poll(self) -> tuple[int, int]:
        return (self.acc.n if self.acc else 0), self.n_frames

    @property
    def finished(self) -> bool:
        return bool(self.acc and self.acc.done.is_set())

    @property
    def timed_out(self) -> bool:
        return self._active and (time.monotonic() - self._t0) > self.timeout_s

    def wait(self) -> bool:
        """Block until done or timeout. Returns True when complete."""
        remaining = self.timeout_s - (time.monotonic() - self._t0)
        return self.acc.done.wait(max(0.0, remaining))

    def cancel(self) -> None:
        self._teardown()

    def _teardown(self) -> None:
        if not self._active:
            return
        b = self.backend
        try:
            b.remove_frame_tap(self.acc)
        finally:
            b.fast_preview_enabled = self._restore.get("fast_preview_enabled", False)
            b.is_auto_exposure_active = self._restore.get("is_auto_exposure_active", False)
            b.is_measurement_paused = self._restore.get("is_measurement_paused", False)
            if self.raw:
                b.linearity_bypass = self._restore.get("linearity_bypass", False)
            self._active = False

    def finish(self) -> Capture:
        """Tear down and build the Capture. Raises TimeoutError if fewer than
        the requested frames arrived."""
        self._teardown()
        st = self.acc.stats() if self.acc else None
        if st is None or st[3] < self.n_frames:
            got = 0 if st is None else st[3]
            raise TimeoutError(
                f"Capture incomplete: {got}/{self.n_frames} frames "
                f"(rejected: {self.acc.rejected if self.acc else {}}). Is the "
                f"device streaming at the requested integration time?")
        mean, std, sat, n = st
        b = self.backend
        return Capture(
            wl=np.asarray(b.wavelength_array, float).copy(),
            mean=mean, std=std, n_frames=n,
            integration_us=self.integration_us, saturated=sat,
            saturation_level=float(b.adc_saturation_level),
            full_scale=float(b.adc_full_scale),
            backend=self.backend_name, serial=getattr(b, "serial", None),
            wl_fingerprint=b.wl_fingerprint(), kind=self.kind,
            t_start=self.acc.t_first, t_end=self.acc.t_last,
            linearity_bypassed=bool(self.raw or getattr(b, "linearizer", None) is None))


def capture(backend, n_frames: int, *, integration_us: int | None = None,
            kind: str = "light", backend_name: str | None = None,
            skip_first: int = 1, timeout_s: float | None = None,
            raw: bool = False) -> Capture:
    """Blocking capture of N clean frames (run from a worker thread in a GUI).
    raw=True bypasses the unit's linearity correction (linearity measurement)."""
    s = CaptureSession(backend, n_frames, integration_us=integration_us, kind=kind,
                       backend_name=backend_name, skip_first=skip_first,
                       timeout_s=timeout_s, raw=raw)
    s.start()
    try:
        s.wait()
    finally:
        s._teardown()
    return s.finish()


def capture_dark(backend, n_frames: int, *, integration_us: int | None = None,
                 backend_name: str | None = None, timeout_s: float | None = None,
                 use_shutter: bool = True, raw: bool = False) -> Capture:
    """Dark capture. Closes the backend's shutter if it has one (demo lamp,
    devices with a shutter); otherwise the caller must have blocked the light."""
    closed = False
    if use_shutter and getattr(backend, "SUPPORTS_SHUTTER", False):
        closed = bool(backend.set_shutter(True))
    try:
        # one extra skipped frame: the frame in flight may still contain light
        return capture(backend, n_frames, integration_us=integration_us,
                       kind="dark", backend_name=backend_name,
                       skip_first=2 if closed else 1, timeout_s=timeout_s, raw=raw)
    finally:
        if closed:
            backend.set_shutter(False)


# ══════════════════════════════════════════════════════════════════════════
#  Count rate
# ══════════════════════════════════════════════════════════════════════════

def _robust_frame_sigma(dark: Capture) -> np.ndarray:
    """Per-pixel single-frame dark noise. Uses the temporal std when N >= 2;
    for a single frame falls back to a spatial MAD estimate (global)."""
    if dark.n_frames >= 2 and np.any(dark.std > 0):
        return np.asarray(dark.std, float)
    d = np.asarray(dark.mean, float)
    if d.size < 3:
        return np.zeros_like(d)
    hp = d[1:-1] - 0.5 * (d[:-2] + d[2:])
    mad = float(np.median(np.abs(hp - np.median(hp))))
    sigma = 1.4826 * mad / np.sqrt(1.5)
    return np.full_like(d, sigma)


def count_rate(light: Capture, dark: Capture) -> CountRate:
    """C(λ) = (mean_light − mean_dark) / t_int  [counts/s]. No clipping.

    Warns (does not refuse) when the dark was taken at a different integration
    time or with a different wavelength calibration; refuses a pixel-count
    mismatch."""
    L = np.asarray(light.mean, float)
    D = np.asarray(dark.mean, float)
    if L.shape != D.shape:
        raise ValueError(f"Dark ({D.size} px) and light ({L.size} px) spectra "
                         f"have different pixel counts.")
    warnings: list[str] = []
    tl, td = int(light.integration_us), int(dark.integration_us)
    if abs(tl - td) > ITIME_REL_TOL * max(tl, 1):
        warnings.append(
            f"Dark spectrum was taken at {td/1000:.3f} ms but the light spectrum "
            f"at {tl/1000:.3f} ms — take a new dark at the same integration time.")
    if (light.wl_fingerprint and dark.wl_fingerprint
            and light.wl_fingerprint != dark.wl_fingerprint):
        warnings.append("Dark and light spectra were taken with different "
                        "wavelength calibrations.")
    if light.n_saturated:
        warnings.append(f"{light.n_saturated} pixel(s) reached the saturation "
                        f"level ({light.saturation_level:.0f} counts) — reduce "
                        f"the integration time.")
    t = tl / 1e6
    rate = (L - D) / t
    sig_d1 = _robust_frame_sigma(dark)
    sig_l = np.asarray(light.std, float)
    var_dark_mean = (np.asarray(dark.sem2, float) if dark.sem2 is not None
                     else sig_d1 ** 2 / max(dark.n_frames, 1))
    sigma_rate = np.sqrt(sig_l ** 2 / max(light.n_frames, 1) + var_dark_mean) / t
    return CountRate(
        wl=np.asarray(light.wl, float), rate=rate, sigma_dark=sig_d1 / t,
        sigma_rate=sigma_rate, saturated=np.asarray(light.saturated, bool),
        integration_us=tl, n_frames=light.n_frames, n_dark_frames=dark.n_frames,
        warnings=warnings, wl_fingerprint=light.wl_fingerprint,
        peak_frac=light.peak_frac)


# ══════════════════════════════════════════════════════════════════════════
#  Dark before / after a light capture (time interpolation, drift)
# ══════════════════════════════════════════════════════════════════════════
#
#  A dark offset that changes between runs (LR-2T: +70 … −200 counts) biases
#  C = (L − D)/t at weak pixels. Every light capture is therefore bracketed by
#  a dark capture at the SAME t_int before and after it; the dark subtracted
#  is the per-pixel linear interpolation of both to the light's mid time.

def interpolate_dark(before: Capture, after: Capture | None,
                     t_mid: float | None) -> Capture:
    """Dark for a light capture with mid time t_mid (epoch s) taken between
    `before` and `after` (same t_int, same pixels): per-pixel linear
    interpolation in time. Without usable timestamps the mean of both is used;
    without `after` the `before` dark is returned unchanged."""
    if after is None:
        return before
    B = np.asarray(before.mean, float)
    A = np.asarray(after.mean, float)
    if A.shape != B.shape:
        raise ValueError("Dark spectra before/after have different pixel counts.")
    tb, ta = before.t_mid, after.t_mid
    if tb is None or ta is None or t_mid is None or ta <= tb:
        w = 0.5
    else:
        w = float(np.clip((float(t_mid) - tb) / (ta - tb), 0.0, 1.0))
    sb, sa = _robust_frame_sigma(before), _robust_frame_sigma(after)
    mean = (1.0 - w) * B + w * A
    sem2 = ((1.0 - w) ** 2 * sb ** 2 / max(before.n_frames, 1)
            + w ** 2 * sa ** 2 / max(after.n_frames, 1))
    std = np.sqrt((1.0 - w) * sb ** 2 + w * sa ** 2)       # single-frame noise
    return Capture(
        wl=np.asarray(before.wl, float), mean=mean, std=std,
        n_frames=int(before.n_frames + after.n_frames),
        integration_us=int(before.integration_us),
        saturated=np.asarray(before.saturated, bool) | np.asarray(after.saturated, bool),
        saturation_level=before.saturation_level, full_scale=before.full_scale,
        timestamp=after.timestamp, backend=before.backend, serial=before.serial,
        wl_fingerprint=before.wl_fingerprint, kind="dark",
        t_start=before.t_start, t_end=after.t_end,
        linearity_bypassed=before.linearity_bypassed, sem2=sem2)


def dark_drift(before: Capture, after: Capture) -> dict:
    """Change of the dark level between the dark before and after a light
    capture (same t_int). mean_counts = mean over pixels of (after − before);
    sigma_counts = its expected noise (from the per-pixel frame noise), so
    |mean| ≫ sigma means a real drift. p95_abs_counts: per-pixel spread."""
    B = np.asarray(before.mean, float)
    A = np.asarray(after.mean, float)
    d = A - B
    sb, sa = _robust_frame_sigma(before), _robust_frame_sigma(after)
    var_px = sb ** 2 / max(before.n_frames, 1) + sa ** 2 / max(after.n_frames, 1)
    sigma_mean = float(np.sqrt(np.mean(var_px) / max(d.size, 1)))
    dt = None
    if before.t_mid is not None and after.t_mid is not None:
        dt = float(after.t_mid - before.t_mid)
    return {"integration_us": int(before.integration_us),
            "mean_counts": float(np.mean(d)) if d.size else 0.0,
            "p95_abs_counts": float(np.percentile(np.abs(d), 95)) if d.size else 0.0,
            "sigma_counts": sigma_mean, "seconds": dt,
            "before_mean_counts": float(np.mean(B)) if B.size else 0.0,
            "after_mean_counts": float(np.mean(A)) if A.size else 0.0}


# ══════════════════════════════════════════════════════════════════════════
#  Exposure helper (wizard step 3)
# ══════════════════════════════════════════════════════════════════════════

def suggest_integration_us(current_us: int, peak_counts: float, baseline_counts: float,
                           *, full_scale: float, saturation_level: float,
                           target_frac: tuple[float, float] = (0.70, 0.85),
                           bounds_us: tuple[int, int] = (1, 10_000_000),
                           any_saturated: bool = False) -> tuple[int, bool]:
    """Next integration time to bring the raw peak into target_frac·full_scale
    (and below the saturation level). Returns (new_us, already_in_target)."""
    lo_f, hi_f = target_frac
    hi_counts = min(hi_f * full_scale, saturation_level * 0.995)
    lo_counts = min(lo_f * full_scale, hi_counts * 0.95)
    in_target = (not any_saturated) and lo_counts <= peak_counts <= hi_counts
    if in_target:
        return int(current_us), True
    if any_saturated or peak_counts >= saturation_level:
        new = int(current_us * 0.5)
    else:
        signal = max(peak_counts - baseline_counts, 1.0)
        goal = 0.5 * (lo_counts + hi_counts) - baseline_counts
        r = float(np.clip(goal / signal, 0.1, 8.0))
        new = int(round(current_us * r))
    new = int(np.clip(new, bounds_us[0], bounds_us[1]))
    return new, False


def auto_expose(backend, *, target_frac: tuple[float, float] = (0.70, 0.85),
                frames: int = 2, max_iter: int = 10,
                backend_name: str | None = None) -> tuple[int, Capture, bool]:
    """Blocking auto exposure for the calibration capture. Iterates short
    captures until the raw peak sits in the target band. Returns
    (integration_us, last_capture, converged). The caller then FIXES this t_int."""
    t = int(backend.current_integration_time_us)
    bounds = (int(getattr(backend, "min_integration_us", 1)),
              int(getattr(backend, "max_integration_us", 10_000_000)))
    cap = None
    for _ in range(max_iter):
        cap = capture(backend, frames, integration_us=t, backend_name=backend_name)
        baseline = float(np.percentile(cap.mean, 2))
        new, ok = suggest_integration_us(
            t, cap.peak_counts, baseline, full_scale=cap.full_scale,
            saturation_level=cap.saturation_level, target_frac=target_frac,
            bounds_us=bounds, any_saturated=bool(cap.n_saturated))
        if ok:
            return t, cap, True
        if new == t:            # pinned at a bound
            return t, cap, False
        t = new
    return t, cap, False


# ══════════════════════════════════════════════════════════════════════════
#  Dark-spectrum store (per device + integration time)
# ══════════════════════════════════════════════════════════════════════════

def _dark_path() -> str:
    import data_paths
    try:
        from app_config import Config
        return data_paths.resolve(Config.get("dark_spectra_file", DEFAULT_DARK_FILE))
    except Exception:
        return data_paths.resolve(DEFAULT_DARK_FILE)


def _load_darks() -> list[dict]:
    path = _dark_path()
    if not os.path.exists(path):
        return []
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f) or {}
        return list(data.get("darks", []))
    except Exception as e:
        print(f"[dark] failed to read {path}: {e}")
        return []


def _save_darks(darks: list[dict]) -> None:
    path = _dark_path()
    try:
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump({"schema": 1, "darks": darks}, f)
    except Exception as e:
        print(f"[dark] failed to write {path}: {e}")


def _same_device(d: dict, backend: str, serial: str | None) -> bool:
    return d.get("backend") == backend and (d.get("serial") or None) == (serial or None)


def save_dark(dark: Capture) -> None:
    """Store a dark spectrum for (backend, serial, t_int); replaces an older dark
    at the same integration time. Keeps at most MAX_DARKS_PER_DEVICE per device."""
    darks = _load_darks()
    t = int(dark.integration_us)
    keep = [d for d in darks
            if not (_same_device(d, dark.backend, dark.serial)
                    and abs(int(d["integration_us"]) - t) <= ITIME_REL_TOL * max(t, 1))]
    entry = dark.to_dict()
    entry["kind"] = "dark"
    keep.append(entry)
    mine = [d for d in keep if _same_device(d, dark.backend, dark.serial)]
    if len(mine) > MAX_DARKS_PER_DEVICE:
        mine.sort(key=_ts_key)
        drop = {id(d) for d in mine[:len(mine) - MAX_DARKS_PER_DEVICE]}
        keep = [d for d in keep if id(d) not in drop]
    _save_darks(keep)


def _ts_key(d: dict) -> float:
    """Sortable timestamp (handles different UTC offsets, e.g. around DST)."""
    from datetime import datetime
    try:
        return datetime.fromisoformat(str(d.get("timestamp", ""))).timestamp()
    except Exception:
        return float("-inf")


def find_dark(backend: str, serial: str | None, integration_us: int, *,
              pixel_count: int | None = None) -> Capture | None:
    """Newest stored dark for this device at this integration time, or None."""
    t = int(integration_us)
    best = None
    for d in _load_darks():
        if not _same_device(d, backend, serial):
            continue
        if abs(int(d["integration_us"]) - t) > ITIME_REL_TOL * max(t, 1):
            continue
        if pixel_count is not None and len(d.get("mean", [])) != int(pixel_count):
            continue
        if best is None or _ts_key(d) > _ts_key(best):
            best = d
    return Capture.from_dict(best) if best else None


def dark_for_integration(backend: str, serial: str | None, integration_us: int, *,
                         pixel_count: int | None = None):
    """Per-pixel dark spectrum for ANY integration time.

    1. a stored dark at exactly this t_int            → ("stored", array, info)
    2. else, ≥ 2 stored darks at different t_int: per-pixel linear model
       dark(t) = b + r·t fitted over all of them (detector bias + dark current;
       the wizard always stores t and t/2)            → ("stored_model", array, info)
    3. else                                            → (None, None, info)
    info: {"integration_us": [...used...], "newest": iso, "age_s": float}"""
    exact = find_dark(backend, serial, integration_us, pixel_count=pixel_count)
    if exact is not None:
        age = None
        try:
            from datetime import datetime
            age = datetime.now().astimezone().timestamp() - _ts_key(exact.to_dict())
        except Exception:
            pass
        return "stored", np.asarray(exact.mean, float), {
            "integration_us": [exact.integration_us], "newest": exact.timestamp,
            "age_s": age}
    # newest dark per distinct t_int
    per_t: dict[int, dict] = {}
    for d in _load_darks():
        if not _same_device(d, backend, serial):
            continue
        if pixel_count is not None and len(d.get("mean", [])) != int(pixel_count):
            continue
        t = int(d["integration_us"])
        if t not in per_t or _ts_key(d) > _ts_key(per_t[t]):
            per_t[t] = d
    if len(per_t) < 2:
        return None, None, {"integration_us": sorted(per_t)}
    ts = np.array(sorted(per_t), float)
    Y = np.array([per_t[int(t)]["mean"] for t in ts], float)      # (n_t, n_px)
    A = np.column_stack([np.ones_like(ts), ts])
    coef, *_ = np.linalg.lstsq(A, Y, rcond=None)                   # (2, n_px)
    model = coef[0] + coef[1] * float(integration_us)
    newest = max(per_t.values(), key=_ts_key)
    return "stored_model", model, {"integration_us": [int(t) for t in ts],
                                   "newest": newest.get("timestamp")}


def list_darks(backend: str, serial: str | None) -> list[dict]:
    """Metadata (no arrays) of the stored darks of one device, newest first."""
    out = []
    for d in _load_darks():
        if _same_device(d, backend, serial):
            item = {k: d.get(k) for k in ("integration_us", "n_frames",
                                          "timestamp", "wl_fingerprint")}
            try:
                item["mean_counts"] = float(np.mean(np.asarray(d["mean"], float)))
            except (KeyError, TypeError, ValueError):
                item["mean_counts"] = None
            out.append(item)
    out.sort(key=_ts_key, reverse=True)
    return out


def delete_darks(backend: str, serial: str | None) -> int:
    darks = _load_darks()
    keep = [d for d in darks if not _same_device(d, backend, serial)]
    _save_darks(keep)
    return len(darks) - len(keep)

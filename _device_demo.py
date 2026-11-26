"""
_device_demo.py — Virtual demo spectrometer (no hardware)

A drop-in backend that synthesises a plausible spectrum with noise so the GUI /
web app and all downstream features (colour, CRI/TM-30, filter tab, scope
overlays, fast preview, auto-exposure) can be exercised without a real device.

Design notes
------------
- Structurally parallel to the real `_device_*.py` drivers (scan / connect /
  start / stop / disconnect / set_integration_time_us / wavelength_array /
  _run last), so it clones cleanly.
- No hardware libraries — imports only numpy + stdlib, so it is ALWAYS available.
- 16-bit output in [0, 65535] with realistic SOFT saturation near full scale.
- Honours integration time 1 ms … 1000 ms: signal AND dark current scale with the
  exposure (dark = fixed bias + dark-current·t), like a real detector. Per-device
  bounds come from `demo_min/max_integration_us`, exposed via
  `min_integration_us` / `max_integration_us`.
- Spectrum is chosen by the `demo_spectrum` config key: a built-in synthetic shape
  (led_white / led_cool / tungsten / flat) or any column name from
  reference_spectra.csv (Solar AM1.5G, Fluorescent 3-band, Mercury (Hg), …).
- Supports Fast Preview (short exposure + software scaling) and Auto Exposure,
  using the same control law as the real drivers. Because the synthetic frames
  are always cleanly integrated (no stale frames), AE converges quickly — a useful
  contrast to real hardware quirks.
"""

from __future__ import annotations

import os
import threading
import time

import numpy as np

from device_manager import BaseSpectrometer
from fusion import FRAME_STANDARD

# ── Static description ──────────────────────────────────────────────────────
DEMO_PIXEL_COUNT = 2048
DEMO_WL_MIN_NM   = 340.0
DEMO_WL_MAX_NM   = 850.0
DEMO_ADC_MAX     = 65535          # 16-bit full scale

# Dark is exposure-dependent like a real detector: a fixed read/bias offset that
# is present even at t=0, PLUS dark current that accumulates with exposure.
DEMO_DARK_BIAS    = 500.0         # fixed read offset (counts)
DEMO_DARK_CURRENT = 1.0           # dark current (counts per ms of exposure)

# Soft saturation: the response is linear up to this fraction of full scale, then
# compresses smoothly toward full scale (asymptotic, C1-continuous at the knee),
# like real full-well behaviour. Set demo_soft_knee = 1.0 for hard clipping.
DEMO_SOFT_KNEE_FRAC = 0.85

# Integration time (µs)
DEMO_MIN_INTEGRATION_US = 1_000      # 1 ms
DEMO_MAX_INTEGRATION_US = 1_000_000  # 1000 ms
# Hard limits for the config keys demo_min/max_integration_us (test setups with
# very different brightness, e.g. an optics derivation 1:1000).
DEMO_ABS_MIN_INTEGRATION_US = 10          # 10 µs
DEMO_ABS_MAX_INTEGRATION_US = 10_000_000  # 10 s

# Brightness calibration: the brightest pixel's LINEAR (pre-saturation) signal
# reaches DEMO_FULLSCALE_FRAC of full scale at DEMO_FULLSCALE_MS, so the whole
# 1–1000 ms span is a clean ramp and the top sits in the soft-saturation region.
# Lower demo_fullscale_ms in config to make the source saturate earlier.
DEMO_FULLSCALE_MS   = 1000.0
DEMO_FULLSCALE_FRAC = 0.98

N_DARK_ESTIMATE_PIXELS = 50

# Auto-exposure (16-bit), same shape as the real drivers
DEMO_AE_TARGET_ADC          = 50_000
DEMO_AE_DEADZONE            = 2_000
DEMO_AE_SAT_ADC             = int(DEMO_ADC_MAX * 0.98)   # treat as "saturated"
AUTO_EXP_EMERGENCY_DROP     = 0.2
AUTO_EXP_MIN_RATIO          = 0.1
AUTO_EXP_MAX_RATIO          = 8.0


# ── Spectrum library ────────────────────────────────────────────────────────
# Built-in synthetic shapes live here (driver = code/data); the CHOICE is a
# config key (`demo_spectrum`). Any name that isn't a built-in is looked up as a
# column in reference_spectra.csv (e.g. "Solar AM1.5G", "Fluorescent 3-band",
# "LED White Cool", "Mercury (Hg)"), so all real reference spectra are selectable.

def _bi_led_white(wl):
    return (np.exp(-((wl - 450.0) / 14.0) ** 2)
            + 0.85 * np.exp(-((wl - 560.0) / 95.0) ** 2)
            + 0.30 * np.exp(-((wl - 630.0) / 60.0) ** 2))

def _bi_led_cool(wl):
    return (1.10 * np.exp(-((wl - 455.0) / 13.0) ** 2)
            + 0.70 * np.exp(-((wl - 545.0) / 80.0) ** 2))

def _bi_tungsten(wl, T=2856.0):
    l = wl * 1e-9
    h, c, k = 6.62607e-34, 2.99792e8, 1.380649e-23
    return (1.0 / l ** 5) / (np.exp(h * c / (l * k * T)) - 1.0)

def _bi_flat(wl):
    return np.ones_like(wl)

def _bi_cal_lamp(wl):
    """Calibration lamp irradiance E_lamp(lambda) (see calibration_utils)."""
    from calibration_utils import demo_lamp_irradiance
    return np.asarray(demo_lamp_irradiance(wl), dtype=float)


def demo_true_sensitivity(wl_nm) -> np.ndarray:
    """Known synthetic detector sensitivity S_true(lambda), peak-normalised
    over the demo axis (max = 1). Used when demo_sensitivity == "synthetic"."""
    def _raw(w):
        return np.exp(-0.5 * ((np.asarray(w, dtype=float) - 600.0) / 160.0) ** 2)
    ref = float(np.max(_raw(np.linspace(DEMO_WL_MIN_NM, DEMO_WL_MAX_NM,
                                        DEMO_PIXEL_COUNT))))
    return _raw(wl_nm) / ref


_BUILTINS = {
    "cal_lamp":  _bi_cal_lamp,
    "led_white": _bi_led_white,
    "led_cool":  _bi_led_cool,
    "tungsten":  _bi_tungsten,
    "flat":      _bi_flat,
}


def _load_csv_spectrum(name: str, wl: np.ndarray):
    """Resample a named column from reference_spectra.csv onto `wl`. None if not found."""
    target = name.strip().lower()
    for d in (os.path.dirname(__file__), os.getcwd()):
        path = os.path.join(d, "reference_spectra.csv")
        if not os.path.exists(path):
            continue
        try:
            with open(path, "r", encoding="utf-8") as f:
                header = [h.strip() for h in f.readline().rstrip("\n").split(";")]
                idx = next((i for i, h in enumerate(header)
                            if h.lower() == target), None)
                if idx is None:    # partial match (e.g. "solar" → "Solar AM1.5G")
                    idx = next((i for i, h in enumerate(header)
                                if i > 0 and target in h.lower()), None)
                if not idx:        # None or 0 (wavelength column) → no match
                    return None
                xs, ys = [], []
                for line in f:
                    p = line.rstrip("\n").split(";")
                    if len(p) <= idx:
                        continue
                    try:
                        xs.append(float(p[0])); ys.append(float(p[idx]))
                    except ValueError:
                        continue
            if len(xs) < 2:
                return None
            shape = np.interp(wl, np.array(xs), np.array(ys), left=0.0, right=0.0)
            return np.maximum(shape, 0.0)
        except Exception:
            return None
    return None


def _resolve_spectrum(name: str, wl: np.ndarray,
                      sensitivity: str = "none") -> np.ndarray:
    """Return a peak-normalised (max=1) shape for `name`: a built-in or a CSV column."""
    key = (name or "led_white").strip()
    fn = _BUILTINS.get(key.lower())
    shape = fn(wl) if fn is not None else _load_csv_spectrum(key, wl)
    if shape is None:
        print(f"[DEMO] spectrum '{name}' not found (built-in or "
              f"reference_spectra.csv column) — using led_white")
        shape = _bi_led_white(wl)
    if str(sensitivity).strip().lower() == "synthetic":
        shape = np.asarray(shape, dtype=float) * demo_true_sensitivity(wl)
    m = float(np.max(shape))
    return (shape / m) if m > 0 else np.zeros_like(wl)


def _soft_saturate(x: np.ndarray, knee: float, full: float) -> np.ndarray:
    """Linear below `knee`; smooth asymptotic approach to `full` above it
    (slope-continuous at the knee). Never exceeds `full`."""
    span = max(full - knee, 1e-9)
    over = np.exp(-np.maximum(x - knee, 0.0) / span)   # 1 at knee → 0 far above
    return np.where(x <= knee, x, full - span * over)


class DemoSpectrometer(BaseSpectrometer):
    """Virtual spectrometer — no hardware required."""

    PIXEL_COUNT           = DEMO_PIXEL_COUNT
    WL_MIN_NM             = DEMO_WL_MIN_NM
    WL_MAX_NM             = DEMO_WL_MAX_NM
    # Demo ships with a visible spec sub-band (380–780 nm inside its 340–850 nm
    # data) so the out-of-spec shading is demonstrable without real hardware.
    SPEC_CONFIG_PREFIX    = "demo"       # demo_spec_min_nm / demo_spec_max_nm
    SPEC_MIN_NM_DEFAULT   = 380.0
    SPEC_MAX_NM_DEFAULT   = 780.0
    SUPPORTS_OB           = False
    SUPPORTS_FAST_PREVIEW = True
    SUPPORTS_SHUTTER      = True
    RESPONSE_TABLE        = None
    ADC_FULL_SCALE        = float(DEMO_ADC_MAX)

    def __init__(self, **kwargs):
        super().__init__(**kwargs)

        self._thread:  threading.Thread | None = None
        self._running = False
        self._connected = False

        self._wl    = np.linspace(self.WL_MIN_NM, self.WL_MAX_NM, self.PIXEL_COUNT)
        self._shape = _resolve_spectrum("led_white", self._wl)
        self._rng   = np.random.default_rng()
        self._shutter_closed = False
        # Test effect: throughput of the (simulated) input optics, multiplies the
        # photo-signal (e.g. 1e-3 for a dim optics in a derivation test).
        self.source_scale = 1.0
        self._stop_evt = threading.Event()
        self.serial: str | None = None

        # Per-device integration bounds (µs); overridden from Config in connect.
        self._min_us = DEMO_MIN_INTEGRATION_US
        self._max_us = DEMO_MAX_INTEGRATION_US
        self.min_integration_us = DEMO_MIN_INTEGRATION_US
        self.max_integration_us = DEMO_MAX_INTEGRATION_US

        # Noise / dark / saturation model — overridden from Config in connect.
        self._read_noise    = 12.0
        self._shot_noise    = 1.0
        self._dark_bias     = DEMO_DARK_BIAS
        self._dark_current  = DEMO_DARK_CURRENT
        self._knee_counts   = DEMO_SOFT_KNEE_FRAC * DEMO_ADC_MAX
        # Optional test effects (config demo_nonlinearity / demo_dark_drift):
        # response = n·(1 + beta·n/FS) on the photo signal, and a dark offset
        # that drifts linearly in time (counts per second since connect).
        self._nl_beta       = 0.0
        self._dark_drift    = 0.0
        self._t_connect     = time.monotonic()

        # Peak signal counts gained per ms of exposure (set in connect).
        self._gain_per_ms = DEMO_FULLSCALE_FRAC * DEMO_ADC_MAX / DEMO_FULLSCALE_MS

        self.current_integration_time_us = 100_000   # 100 ms

    # ── Lifecycle ───────────────────────────────────────────────────────────
    @staticmethod
    def scan() -> list[str]:
        # Always exactly one virtual device.
        return ["Demo Spectrometer"]

    def connect(self, device_id: str) -> None:
        from app_config import Config
        self._min_us = max(DEMO_ABS_MIN_INTEGRATION_US,
                           int(Config.get("demo_min_integration_us",
                                          DEMO_MIN_INTEGRATION_US)))
        self._max_us = min(DEMO_ABS_MAX_INTEGRATION_US,
                           int(Config.get("demo_max_integration_us",
                                          DEMO_MAX_INTEGRATION_US)))
        self.min_integration_us = self._min_us
        self.max_integration_us = self._max_us
        self._read_noise = float(Config.get("demo_read_noise", 12.0))
        self._shot_noise = float(Config.get("demo_shot_noise", 1.0))
        self._dark_bias    = float(Config.get("demo_dark_bias", DEMO_DARK_BIAS))
        self._dark_current = float(Config.get("demo_dark_current", DEMO_DARK_CURRENT))
        knee_frac = float(Config.get("demo_soft_knee", DEMO_SOFT_KNEE_FRAC))
        self._knee_counts = max(0.0, min(1.0, knee_frac)) * DEMO_ADC_MAX
        fullscale_ms = max(1.0, float(Config.get("demo_fullscale_ms", DEMO_FULLSCALE_MS)))
        self._gain_per_ms = DEMO_FULLSCALE_FRAC * DEMO_ADC_MAX / fullscale_ms
        self._nl_beta = float(Config.get("demo_nonlinearity", 0.0) or 0.0)
        self._dark_drift = float(Config.get("demo_dark_drift", 0.0) or 0.0)
        self._t_connect = time.monotonic()

        spec_name = str(Config.get("demo_spectrum", "led_white"))
        sens = str(Config.get("demo_sensitivity", "none"))
        self._sensitivity = sens
        self._spectrum_name = spec_name
        self._shape = _resolve_spectrum(spec_name, self._wl, sens)
        self.serial = str(Config.get("demo_serial", "DEMO-0001"))

        self.current_integration_time_us = max(
            self._min_us, min(self._max_us, self.current_integration_time_us))
        self._connected = True
        print(f"[DEMO] Virtual spectrometer ready: spectrum='{spec_name}', "
              f"{self.WL_MIN_NM:.0f}–{self.WL_MAX_NM:.0f} nm, {self.PIXEL_COUNT} px, "
              f"16-bit, {self._min_us/1000:.0f}–{self._max_us/1000:.0f} ms, "
              f"soft-knee@{self._knee_counts/DEMO_ADC_MAX*100:.0f}%")

    def start(self) -> None:
        self._running = True
        self._stop_evt.clear()
        self._thread  = threading.Thread(
            target=self._run, daemon=True, name="DEMO-acq")
        self._thread.start()

    def stop(self) -> None:
        self._running = False
        self._stop_evt.set()                 # wakes the frame wait immediately
        if self._thread:
            self._thread.join(timeout=3.0 + self.current_integration_time_us / 1e6)
            self._thread = None

    def disconnect(self) -> None:
        self.stop()
        self._connected = False

    # ── Runtime spectrum switch (calibration wizard, demo sources) ──────────
    @property
    def spectrum_name(self) -> str:
        return getattr(self, "_spectrum_name", "led_white")

    def set_spectrum(self, name: str) -> str:
        """Switch the simulated source at runtime (not persisted to the config).
        Returns the previous spectrum name so the caller can restore it."""
        prev = self.spectrum_name
        self._shape = _resolve_spectrum(name, self._wl,
                                        getattr(self, "_sensitivity", "none"))
        self._spectrum_name = str(name)
        return prev

    def set_integration_time_us(self, us: int) -> None:
        self.current_integration_time_us = max(
            self._min_us, min(self._max_us, int(us)))

    @property
    def wavelength_array(self) -> np.ndarray:
        return self._wl

    def calibrate_from_lines(self, *args, **kwargs) -> bool:
        # Virtual device — wavelength axis is exact, nothing to calibrate.
        return False

    @property
    def adc_saturation_level(self) -> float:
        # Above the soft knee the response is nonlinear.
        return float(min(self._knee_counts, 0.98 * DEMO_ADC_MAX))

    def set_shutter(self, closed: bool) -> bool:
        self._shutter_closed = bool(closed)
        return True

    # ── Frame synthesis ─────────────────────────────────────────────────────
    def _expose(self, t_us: int) -> np.ndarray:
        """Synthesise one 16-bit frame genuinely 'integrated' for t_us, with
        exposure-dependent dark current and soft saturation near full scale."""
        t_ms = t_us / 1000.0
        dark_current = self._dark_current * t_ms          # accumulates with t
        dark   = self._dark_bias + dark_current
        if self._dark_drift:
            dark = dark + self._dark_drift * (time.monotonic() - self._t_connect)
        signal = self._shape * (self._gain_per_ms * t_ms * float(self.source_scale))
        if self._shutter_closed:
            signal = np.zeros_like(signal)                 # true dark frame
        if self._nl_beta:
            n = signal + dark_current
            signal = n * (1.0 + self._nl_beta * n / DEMO_ADC_MAX) - dark_current
        ideal  = dark + signal
        # Shot noise ∝ sqrt(photo-signal + dark-current counts); read noise fixed.
        sigma  = (np.sqrt(np.maximum(signal + dark_current, 0.0)) * self._shot_noise
                  + self._read_noise)
        noisy  = ideal + self._rng.normal(0.0, sigma)
        sat    = _soft_saturate(noisy, self._knee_counts, float(DEMO_ADC_MAX))
        return np.clip(np.round(sat), 0.0, DEMO_ADC_MAX)

    # ── Acquisition loop (last, like the real drivers) ──────────────────────
    def _run(self) -> None:
        while self._running:
            if self.is_measurement_paused:
                time.sleep(0.05)
                continue
            if not self._connected:
                break

            try:
                t_long = self.current_integration_time_us

                # ── Fast Preview: short exposure + software scaling ──────────
                fp = (self.fast_preview_enabled
                      and not self.is_auto_exposure_active
                      and t_long > self._min_us * 2)
                t_this = (max(self._min_us,
                              int(t_long * self.fast_preview_short_pct / 100.0))
                          if fp else t_long)

                pixel_array = self._expose(t_this)

                # ── Dark baseline from darkest pixels ───────────────────────
                sorted_px = np.sort(pixel_array)
                ob_mean   = float(np.median(sorted_px[:N_DARK_ESTIMATE_PIXELS]))

                forward = True
                if fp:
                    if float(np.max(pixel_array)) < DEMO_ADC_MAX * 0.92:
                        ratio  = t_long / float(t_this)
                        net    = np.maximum(0.0, pixel_array - ob_mean)
                        scaled = np.minimum(net * ratio, DEMO_ADC_MAX - ob_mean)
                        pixel_array = scaled + ob_mean
                    else:
                        forward = False   # even the short frame clips → drop

                # ── Auto exposure (FP off; mutually exclusive) ──────────────
                if self.is_auto_exposure_active:
                    max_adc = float(np.max(pixel_array))
                    signal  = max(50.0, max_adc - ob_mean)
                    t       = t_long
                    if max_adc >= DEMO_AE_SAT_ADC:
                        new_us = int(t * AUTO_EXP_EMERGENCY_DROP)
                    elif abs(max_adc - DEMO_AE_TARGET_ADC) > DEMO_AE_DEADZONE:
                        r = max(AUTO_EXP_MIN_RATIO,
                                min(AUTO_EXP_MAX_RATIO,
                                    (DEMO_AE_TARGET_ADC - ob_mean) / signal))
                        new_us = int(t * r)
                    else:
                        new_us = t
                    new_us = max(self._min_us, min(self._max_us, new_us))
                    if new_us != t:
                        self.set_integration_time_us(new_us)
                        if self.on_auto_exp:
                            self.on_auto_exp(new_us / 1000.0)

                if forward:
                    # Report the exposure THIS frame was taken at (t_long).
                    self.on_frame(pixel_array, ob_mean, t_long,
                                  FRAME_STANDARD, 1.0)

                # New-data cadence ≈ 1/t (like a real device), capped so 1 ms
                # doesn't emit thousands of frames/s into the UI.
                self._stop_evt.wait(max(t_this / 1_000_000.0, 0.012))

            except Exception as exc:
                if self._running:
                    print(f"[DEMO] Error: {exc}")
                    if self.on_connection_lost:
                        self.on_connection_lost()
                break

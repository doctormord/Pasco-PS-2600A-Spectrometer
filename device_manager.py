"""
device_manager.py — Device abstraction layer

All spectrometer backends implement BaseSpectrometer and register
themselves here.  The GUI/web server only ever talks to this module.

Adding a new device:
    1.  Create _device_<name>.py implementing BaseSpectrometer.
    2.  Import it at the bottom of this file and add it to REGISTRY.

The on_frame callback signature is identical across all backends:
    on_frame(pixel_array: np.ndarray, ob_mean: float, integration_us: int)
ob_mean is 0.0 for devices that don't have optical-black pixels.
"""

from __future__ import annotations

import threading
import time
from abc import ABC, abstractmethod
from typing import Callable

import numpy as np


# ── Abstract base class ────────────────────────────────────────────────────


class BaseSpectrometer(ABC):
    """
    Common interface every backend must implement.

    Lifecycle:
        scan()      → list of device identifiers (strings, shown in dropdown)
        connect(id) → opens device, raises on failure
        start()     → starts background acquisition thread
        stop()      → stops thread, does not close device
        disconnect()→ stop + close
        set_integration_time_us(int) → change exposure, thread-safe
    """

    # Subclasses should set these to describe their wavelength coverage
    # and pixel count so the GUI can adapt gracefully.
    PIXEL_COUNT: int    = 2048
    WL_MIN_NM:   float  = 200.0
    WL_MAX_NM:   float  = 1100.0
    SUPPORTS_OB: bool   = False    # True only for PASCO with optical-black pixels
    SUPPORTS_FAST_PREVIEW: bool = False
    RESPONSE_TABLE = None

    # Manufacturer-specified (guaranteed) wavelength range in nm. The sensor
    # still returns data outside this, but the maker doesn't guarantee accuracy
    # there — the GUI and web UI shade those edges. Subclasses set a config
    # prefix; users override per device via '{prefix}_spec_min_nm' /
    # '{prefix}_spec_max_nm'. Either bound None → that side isn't shaded.
    SPEC_CONFIG_PREFIX:  str | None   = None
    SPEC_MIN_NM_DEFAULT: float | None = None
    SPEC_MAX_NM_DEFAULT: float | None = None

    # Raw ADC full scale of the detector (counts). Subclasses set the real value
    # (4095 for 12-bit PASCO, 65535 for 16-bit devices). Used by the generic
    # calibration capture for the per-pixel saturation check.
    ADC_FULL_SCALE: float = 65535.0

    @property
    def adc_full_scale(self) -> float:
        """Raw ADC full scale (counts) of the connected detector."""
        return float(getattr(self, "_adc_full_scale", self.ADC_FULL_SCALE))

    @property
    def adc_saturation_level(self) -> float:
        """Counts at/above which a pixel is treated as saturated (no longer
        linear). Default 98 % of full scale; backends with a known practical
        limit (configurable threshold, soft-saturation knee) override this."""
        return 0.98 * self.adc_full_scale

    def wl_fingerprint(self) -> str:
        """Fingerprint of the CURRENT wavelength calibration (pixel -> nm).

        Derived from the effective per-pixel axis, so any change of the
        polynomial, offset, flip or source (factory vs. user) changes it.
        Response/absolute profiles store it; a mismatch means the pixel widths
        baked into S/K no longer match and the profile must be re-done."""
        from calibration_utils import wavelength_fingerprint
        return wavelength_fingerprint(self.wavelength_array)

    @property
    def nl_correction_active(self) -> bool | None:
        """True/False if the backend applies a detector nonlinearity
        correction before on_frame, None if the device has none / unknown.
        Recorded in calibration profiles (acquisition.nl_correction)."""
        return None

    # Optional shutter / light-source control. Generic hook for the dark
    # capture: backends that can block the light (or the virtual demo lamp)
    # return True; everyone else returns False and the wizard asks the user.
    SUPPORTS_SHUTTER: bool = False

    def set_shutter(self, closed: bool) -> bool:
        """Close/open the light path. Returns True if supported and applied."""
        return False

    # ── Frame taps (generic calibration capture) ─────────────────────────
    # Drivers call self.on_frame(...). __init__ routes that through
    # _dispatch_frame so any number of taps (e.g. a calibration accumulator)
    # see EVERY raw (linearised) driver frame — before fusion, averaging,
    # smoothing or response correction — without any driver changes.
    def add_frame_tap(self, fn: Callable[..., None]) -> None:
        with self._tap_lock:
            if fn not in self._frame_taps:
                self._frame_taps.append(fn)

    def remove_frame_tap(self, fn: Callable[..., None]) -> None:
        with self._tap_lock:
            if fn in self._frame_taps:
                self._frame_taps.remove(fn)

    def _dispatch_frame(self, pixels, dark=0.0, integration_us=0,
                        kind="standard", ratio=1.0) -> None:
        with self._tap_lock:
            taps = list(self._frame_taps)
        for fn in taps:
            try:
                fn(pixels, dark, integration_us, kind, ratio)
            except Exception as exc:          # a tap must never kill acquisition
                print(f"[device] frame tap error: {exc}")
        cb = self._user_on_frame
        if cb is not None:
            cb(pixels, dark, integration_us, kind, ratio)

    @property
    def spec_range_nm(self) -> tuple[float | None, float | None]:
        """(min_nm, max_nm) of the manufacturer-guaranteed range, each possibly
        None. Read from config (falling back to the class defaults) so it can be
        set per device without code changes."""
        lo, hi = self.SPEC_MIN_NM_DEFAULT, self.SPEC_MAX_NM_DEFAULT
        if self.SPEC_CONFIG_PREFIX:
            try:
                from app_config import Config
                lo = Config.get(f"{self.SPEC_CONFIG_PREFIX}_spec_min_nm", lo)
                hi = Config.get(f"{self.SPEC_CONFIG_PREFIX}_spec_max_nm", hi)
            except Exception:
                pass

        def _num(v):
            try:
                return None if v is None or v == "" else float(v)
            except (TypeError, ValueError):
                return None
        return (_num(lo), _num(hi))

    # ── Wavelength-calibration snapshot/restore ───────────────────────────
    # Used by the calibration wizard to discard an unsaved live preview (or to
    # roll back a fit that failed the sanity check) so a bad calibration can't
    # leave the live view in a broken state. Default operates on self._wl, which
    # PASCO and LR-2T use directly; Ocean overrides (coeff-based axis).
    def wl_calibration_snapshot(self) -> dict:
        wl = getattr(self, "_wl", None)
        if wl is None:
            wl = np.asarray(self.wavelength_array)
        return {"_wl": np.asarray(wl, dtype=float).copy()}

    def wl_calibration_restore(self, snap: dict) -> None:
        if snap and "_wl" in snap and hasattr(self, "_wl"):
            self._wl = np.asarray(snap["_wl"], dtype=float)

    def __init__(
        self,
        *,
        on_frame:            Callable[..., None],
        on_auto_exp:         Callable[[float], None] | None = None,
        on_connection_lost:  Callable[[], None]      | None = None,
    ):
        # on_frame is called as:
        #   on_frame(pixels, dark, integration_us, kind="standard", ratio=1.0)
        # where kind is one of fusion.FRAME_STANDARD / FRAME_LONG / FRAME_SHORT.
        # The app callback is wrapped by _dispatch_frame (frame taps first).
        self._user_on_frame     = on_frame
        self._frame_taps: list[Callable[..., None]] = []
        self._tap_lock          = threading.Lock()
        self.on_frame           = self._dispatch_frame
        self.on_auto_exp        = on_auto_exp
        self.on_connection_lost = on_connection_lost

        self.current_integration_time_us: int  = 50_000
        self.is_auto_exposure_active:     bool = False
        self.is_measurement_paused:       bool = False

        # Per-device integration-time bounds (µs). Subclasses overwrite these
        # from their *_min/max_integration_us config keys (Ocean, LR-2T) or
        # fixed hardware constants (PASCO). The GUI and web server read them so
        # the exposure control reflects the connected device, not a global cap.
        self.min_integration_us: int = 1_000
        self.max_integration_us: int = 10_000_000

        # Optional device serial number for report headers; backends set it on
        # connect where the hardware exposes one (None → omitted from reports).
        self.serial: str | None = None

        # Fast Preview (dual-rate) — shared state, set by GUI/webserver.
        self.fast_preview_enabled:   bool = False
        self.fast_preview_short_pct: int  = 8
        self.fast_preview_n_short:   int  = 4
        self._fp_frame_counter:      int  = 0

    # ── Abstract interface ─────────────────────────────────────────────────

    @staticmethod
    @abstractmethod
    def scan() -> list[str]:
        """Return list of device identifiers visible on the system."""
        ...

    @abstractmethod
    def connect(self, device_id: str) -> None:
        """Open and initialise the device. Raise on failure."""
        ...

    @abstractmethod
    def start(self) -> None:
        """Start the background acquisition thread."""
        ...

    @abstractmethod
    def stop(self) -> None:
        """Stop acquisition thread. Device remains open."""
        ...

    @abstractmethod
    def disconnect(self) -> None:
        """Stop thread and close device."""
        ...

    @abstractmethod
    def set_integration_time_us(self, microseconds: int) -> None:
        ...

    def _wait_interruptible(self, seconds: float, orig_us: int,
                            step: float = 0.02) -> bool:
        """Sleep up to `seconds`, but abort early (return False) if the loop
        should stop/pause OR the requested integration time changed. Lets a long
        exposure be cancelled the instant the user edits the field / hits the
        preset, instead of blocking the whole session (e.g. a 30 s frame).
        Returns True only if the full time elapsed. `orig_us` is the integration
        time captured at the top of the frame (compare against the live value)."""
        end = time.monotonic() + max(0.0, seconds)
        while True:
            now = time.monotonic()
            if now >= end:
                return True
            if not getattr(self, "_running", True):
                return False
            if getattr(self, "is_measurement_paused", False):
                return False
            if self.current_integration_time_us != orig_us:
                return False
            time.sleep(min(step, end - now))

    @property
    def wavelength_array(self) -> np.ndarray:
        """
        Per-pixel wavelength axis in nm.  Default: linear interpolation.
        Override in subclass to use hardware-provided calibration.
        """
        return np.linspace(self.WL_MIN_NM, self.WL_MAX_NM, self.PIXEL_COUNT)


# ── Device registry ────────────────────────────────────────────────────────


def _try_import(module_name: str):
    try:
        import importlib
        return importlib.import_module(module_name)
    except Exception:
        return None


def build_registry() -> dict[str, type[BaseSpectrometer]]:
    """
    Returns {display_name: BackendClass} for every available backend.
    A backend is excluded if its required library is not installed.
    """
    registry: dict[str, type[BaseSpectrometer]] = {}

    # PASCO PS-2600A — always available (pure ctypes, no extra pip package)
    from _device_pasco import PascoPS2600A
    registry["PASCO PS-2600A"] = PascoPS2600A

    # Ocean HDX-UV-VIS — raw OBP over libusb (pyusb + libusb-package).
    # Visible whenever the USB stack is importable; connect() gives a clear
    # install/driver message if the device or backend isn't actually usable.
    if _try_import("usb.core") and _try_import("libusb_package"):
        from _device_ocean import OceanHDX
        registry["Ocean HDX-UV-VIS"] = OceanHDX

    # ASEQ Instruments / Lasertrack LR-2T — always visible in dropdown.
    # The `hid` package is checked at connect() time; if missing a clear
    # error message with install instructions is shown.
    from _device_lr2t import LasertrackLR2T
    registry["ASEQ / Lasertrack LR-2T"] = LasertrackLR2T

    # Demo (virtual) — no hardware, always available. Synthesises a noisy 16-bit
    # spectrum so the app can be driven without a real device.
    from _device_demo import DemoSpectrometer
    registry["Demo (virtual)"] = DemoSpectrometer

    return registry


REGISTRY: dict[str, type[BaseSpectrometer]] = {}   # populated at app startup


def init_registry() -> None:
    global REGISTRY
    REGISTRY = build_registry()


def get_backend_class(display_name: str) -> type[BaseSpectrometer] | None:
    return REGISTRY.get(display_name)


def list_backends() -> list[str]:
    return list(REGISTRY.keys())

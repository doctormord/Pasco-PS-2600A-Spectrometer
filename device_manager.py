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
        self.on_frame           = on_frame
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
        """Apply new integration time to hardware."""
        ...

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

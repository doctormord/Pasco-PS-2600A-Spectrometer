"""
_device_pasco.py  —  PASCO PS-2600A backend

Everything PASCO-specific lives here:
  - All hardware constants (pixel count, ADC range, USB IDs, commands)
  - Wavelength calibration polynomial and computed per-pixel arrays
  - Spectral response compensation table and gain array
  - Dark current correction parameters
  - WinUSB ctypes layer (Windows) / pyusb layer (Linux/macOS via _usb_linux)
  - SpectrometerAcquisition background thread
  - PascoPS2600A backend class (implements BaseSpectrometer)

spectrometer_core.py is intentionally free of any PASCO-specific code.

Hardware
--------
  CCD   : Toshiba TCD1304AP, 3648 pixels, 12-bit ADC
  USB   : VID 0x0945 / PID 0x0002, WinUSB on Windows
  Comms : Endpoint 0x82 bulk IN, Endpoint 0x00 vendor control OUT

Optical black pixels
--------------------
  Bytes 4–63 of every 7360-byte bulk transfer contain 30 masked
  CCD pixels (optical black). These track dark current per-frame and
  are used as the live dark reference in OB correction mode.
"""

from __future__ import annotations

import ctypes
import csv
import os
import struct
import sys
import threading
import time

import numpy as np

from device_manager import BaseSpectrometer
from fusion import FRAME_STANDARD
from calibration_utils import (
    poly_wavelength_array,
    build_response_gain,
    apply_wl_offset,
    fit_wavelength_polynomial,
    wavelength_to_pixel,
)
from device_constants import (
    AUTO_EXP_MIN_RATIO,
    AUTO_EXP_MAX_RATIO,
    AUTO_EXP_EMERGENCY_DROP_RATIO,
)

# ══════════════════════════════════════════════════════════════════════════
# 1. PASCO HARDWARE CONSTANTS
# ══════════════════════════════════════════════════════════════════════════

# USB identification
PASCO_DEVICE_GUID     = "{DEE824EF-729B-4A0E-9C14-B7117D33A817}"
PASCO_VID_STRING      = "vid_0945"
PASCO_PID_STRING      = "pid_0002"

# CCD
PASCO_PIXEL_COUNT     = 3648
PASCO_ADC_BITS        = 12
PASCO_ADC_MAX         = 4095
PASCO_ADC_SATURATION  = 3800

# Integration time (µs)
PASCO_MIN_INTEGRATION_US   = 1_000
PASCO_MAX_INTEGRATION_US   = 2_500_000
PASCO_START_INTEGRATION_US = 20_000

# Auto-exposure targets (12-bit ADC)
PASCO_AE_TARGET_ADC  = 3400
PASCO_AE_DEADZONE    = 100

# USB transfer sizes
PASCO_SPECTRUM_BYTES = 7360
PASCO_DRAIN_BYTES    = 28
PASCO_HEADER_BYTES   = 64
PASCO_BYTES_PER_PX   = 2

# Optical-black pixels (within the 64-byte header)
PASCO_OB_BYTE_START   = 4
PASCO_OB_BYTE_END     = 64
PASCO_OB_PIXEL_COUNT  = (PASCO_OB_BYTE_END - PASCO_OB_BYTE_START) // 2

# USB commands
PASCO_USB_VENDOR_OUT  = 0x40
PASCO_USB_VENDOR_IN   = 0xC0
PASCO_BULK_IN_EP      = 0x82

PASCO_CMD_INIT        = 1
PASCO_CMD_SET_EXP     = 2
PASCO_CMD_TRIGGER     = 9
PASCO_CMD_POLL_STATUS = 131

PASCO_POLL_READY      = 0
PASCO_POLL_INTERVAL_S = 0.002
PASCO_PAUSE_SLEEP_S   = 0.05

# ── Dark current correction ───────────────────────────────────────────────

PASCO_OB_CORRECTION_SLOPE   = 1.008180
PASCO_OB_CORRECTION_OFFSET  = -0.6377
PASCO_OB_TEMP_REFERENCE_ADC = 61.5
PASCO_DARK_BIAS_ADC         = 61.57
PASCO_DARK_RATE_ADC_PER_SEC = 40.94
DARK_MODE_OPTICAL_BLACK     = "optical_black"
DARK_MODE_PARAMETRIC        = "parametric"

# ── Wavelength calibration (cubic polynomial, pixel → nm) ─────────────────
# Fitted against Cadmium emission lines: 467.8, 480.0, 508.6, 643.8 nm

PASCO_WAVELENGTH_COEFFS = [
    75.45192816,
    0.345838363,
    -2.33680103e-05,
    1.22593755e-09,
]

# ── Spectral response compensation (TCD1304AP from datasheet) ─────────────
# [wavelength_nm, relative_sensitivity], normalised to 1.0 at peak (~520 nm)

PASCO_RESPONSE_TABLE = np.array([
    [ 75, 0.0001], [100, 0.0001], [150, 0.0001], [200, 0.0001],
    [250, 0.0001], [300, 0.0002], [320, 0.0005], [340, 0.002 ],
    [360, 0.010 ], [370, 0.050 ], [380, 0.400 ],
    [400, 0.80  ], [420, 0.86  ], [440, 0.91  ], [460, 0.95  ],
    [480, 0.98  ], [500, 0.995 ], [520, 1.00  ], [540, 1.00  ],
    [560, 1.00  ], [580, 0.99  ], [600, 0.97  ], [620, 0.94  ],
    [640, 0.90  ], [660, 0.86  ], [680, 0.82  ],
    [700, 0.75  ], [720, 0.68  ], [740, 0.60  ], [760, 0.53  ],
    [780, 0.47  ], [800, 0.42  ], [820, 0.37  ], [840, 0.32  ],
    [860, 0.28  ], [880, 0.24  ], [900, 0.20  ], [920, 0.17  ],
    [940, 0.14  ], [960, 0.11  ], [980, 0.085 ], [1000, 0.060],
    [1020, 0.040], [1040, 0.022], [1060, 0.010], [1080, 0.004],
    [1100, 0.001],
], dtype=np.float64)

PASCO_RESPONSE_MIN_FLOOR = 0.001
PASCO_RESPONSE_MAX_GAIN  = 10.0

# ── Derived per-pixel arrays (computed once at import) ────────────────────

PASCO_WAVELENGTH_ARRAY: np.ndarray = poly_wavelength_array(
    PASCO_WAVELENGTH_COEFFS, PASCO_PIXEL_COUNT
)

PASCO_SPECTRAL_RESPONSE_GAIN: np.ndarray = build_response_gain(
    PASCO_WAVELENGTH_ARRAY,
    PASCO_RESPONSE_TABLE,
    min_floor=PASCO_RESPONSE_MIN_FLOOR,
    max_gain=PASCO_RESPONSE_MAX_GAIN,
)

# ══════════════════════════════════════════════════════════════════════════
# 2. PLATFORM-SPECIFIC USB LAYER
# ══════════════════════════════════════════════════════════════════════════

_IS_WINDOWS = sys.platform == "win32"

if _IS_WINDOWS:
    from ctypes import wintypes

    # WinAPI flags
    DIGCF_PRESENT          = 0x02
    DIGCF_DEVICEINTERFACE  = 0x10
    GENERIC_READ           = 0x80000000
    GENERIC_WRITE          = 0x40000000
    FILE_SHARE_READ        = 0x00000001
    FILE_SHARE_WRITE       = 0x00000002
    OPEN_EXISTING          = 0x00000003
    FILE_FLAG_OVERLAPPED   = 0x40000000
    INVALID_HANDLE_VALUE   = -1

    setupapi = ctypes.WinDLL("setupapi", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32",  use_last_error=True)
    winusb   = ctypes.WinDLL("winusb",    use_last_error=True)

    class GUID(ctypes.Structure):
        _fields_ = [
            ("Data1", wintypes.DWORD),
            ("Data2", wintypes.WORD),
            ("Data3", wintypes.WORD),
            ("Data4", ctypes.c_ubyte * 8),
        ]

    class SP_DEVICE_INTERFACE_DATA(ctypes.Structure):
        _fields_ = [
            ("cbSize",              wintypes.DWORD),
            ("InterfaceClassGuid",  GUID),
            ("Flags",               wintypes.DWORD),
            ("Reserved",            ctypes.c_void_p),
        ]

    class SP_DEVICE_INTERFACE_DETAIL_DATA_W(ctypes.Structure):
        _fields_ = [
            ("cbSize",     wintypes.DWORD),
            ("DevicePath", wintypes.WCHAR * 1024),
        ]

    class WINUSB_SETUP_PACKET(ctypes.Structure):
        _fields_ = [
            ("RequestType", ctypes.c_ubyte),
            ("Request",     ctypes.c_ubyte),
            ("Value",       ctypes.c_ushort),
            ("Index",       ctypes.c_ushort),
            ("Length",      ctypes.c_ushort),
        ]

    def _str_to_guid(s: str) -> GUID:
        from uuid import UUID
        u = UUID(s)
        d4 = (ctypes.c_ubyte * 8)(*u.bytes[8:])
        return GUID(u.time_low, u.time_mid, u.time_hi_version, d4)

    # Set up argtypes for the WinAPI calls
    setupapi.SetupDiGetClassDevsW.argtypes = [
        ctypes.POINTER(GUID), wintypes.LPCWSTR,
        wintypes.HWND, wintypes.DWORD]
    setupapi.SetupDiGetClassDevsW.restype = ctypes.c_void_p

    setupapi.SetupDiEnumDeviceInterfaces.argtypes = [
        ctypes.c_void_p, ctypes.c_void_p,
        ctypes.POINTER(GUID), wintypes.DWORD,
        ctypes.POINTER(SP_DEVICE_INTERFACE_DATA)]
    setupapi.SetupDiEnumDeviceInterfaces.restype = wintypes.BOOL

    setupapi.SetupDiGetDeviceInterfaceDetailW.argtypes = [
        ctypes.c_void_p, ctypes.POINTER(SP_DEVICE_INTERFACE_DATA),
        ctypes.c_void_p, wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD), ctypes.c_void_p]
    setupapi.SetupDiGetDeviceInterfaceDetailW.restype = wintypes.BOOL

    kernel32.CreateFileW.argtypes = [
        wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
        ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
    kernel32.CreateFileW.restype = wintypes.HANDLE

    winusb.WinUsb_Initialize.argtypes = [
        wintypes.HANDLE, ctypes.POINTER(ctypes.c_void_p)]
    winusb.WinUsb_Initialize.restype = wintypes.BOOL

    winusb.WinUsb_ControlTransfer.argtypes = [
        ctypes.c_void_p, WINUSB_SETUP_PACKET, ctypes.c_void_p,
        wintypes.ULONG, ctypes.POINTER(wintypes.ULONG), ctypes.c_void_p]
    winusb.WinUsb_ControlTransfer.restype = wintypes.BOOL

    winusb.WinUsb_ReadPipe.argtypes = [
        ctypes.c_void_p, ctypes.c_ubyte, ctypes.c_void_p,
        wintypes.ULONG, ctypes.POINTER(wintypes.ULONG), ctypes.c_void_p]
    winusb.WinUsb_ReadPipe.restype = wintypes.BOOL

    _g_usb_handle    = None
    _g_device_handle = None

    def scan_usb_devices() -> list[str]:
        found = []
        guid  = _str_to_guid(PASCO_DEVICE_GUID)
        hdev  = setupapi.SetupDiGetClassDevsW(
            ctypes.byref(guid), None, None,
            DIGCF_PRESENT | DIGCF_DEVICEINTERFACE)
        idx = 0
        while True:
            iface = SP_DEVICE_INTERFACE_DATA()
            iface.cbSize = ctypes.sizeof(iface)
            if not setupapi.SetupDiEnumDeviceInterfaces(
                    hdev, None, ctypes.byref(guid),
                    idx, ctypes.byref(iface)):
                break
            req = wintypes.DWORD()
            setupapi.SetupDiGetDeviceInterfaceDetailW(
                hdev, ctypes.byref(iface), None, 0,
                ctypes.byref(req), None)
            buf    = ctypes.create_string_buffer(req.value)
            detail = ctypes.cast(
                buf, ctypes.POINTER(SP_DEVICE_INTERFACE_DETAIL_DATA_W))
            detail.contents.cbSize = (
                8 if ctypes.sizeof(ctypes.c_void_p) == 8 else 6)
            if setupapi.SetupDiGetDeviceInterfaceDetailW(
                    hdev, ctypes.byref(iface), detail,
                    req, None, None):
                path = detail.contents.DevicePath
                if (PASCO_VID_STRING in path.lower()
                        and PASCO_PID_STRING in path.lower()):
                    found.append(path)
            idx += 1
        return found

    def connect_usb_device(device_path: str) -> None:
        global _g_device_handle, _g_usb_handle
        free_usb_resources()
        _g_device_handle = kernel32.CreateFileW(
            device_path,
            GENERIC_READ | GENERIC_WRITE,
            FILE_SHARE_READ | FILE_SHARE_WRITE,
            None, OPEN_EXISTING, FILE_FLAG_OVERLAPPED, None)
        if _g_device_handle == wintypes.HANDLE(INVALID_HANDLE_VALUE).value:
            raise Exception(
                f"Access denied or device in use. "
                f"Error code: {ctypes.get_last_error()}")
        _g_usb_handle = ctypes.c_void_p()
        if not winusb.WinUsb_Initialize(
                _g_device_handle, ctypes.byref(_g_usb_handle)):
            free_usb_resources()
            raise Exception("WinUsb_Initialize failed.")

    def usb_control_out(req: int, value: int = 0, index: int = 0) -> bool:
        if not _g_usb_handle:
            return False
        pkt = WINUSB_SETUP_PACKET(PASCO_USB_VENDOR_OUT, req, value, index, 0)
        n   = wintypes.ULONG()
        return bool(winusb.WinUsb_ControlTransfer(
            _g_usb_handle, pkt, None, 0, ctypes.byref(n), None))

    def usb_control_in(req: int, length: int) -> bytes | None:
        if not _g_usb_handle:
            return None
        pkt = WINUSB_SETUP_PACKET(PASCO_USB_VENDOR_IN, req, 0, 0, length)
        buf = (ctypes.c_ubyte * length)()
        n   = wintypes.ULONG()
        if winusb.WinUsb_ControlTransfer(
                _g_usb_handle, pkt, buf, length, ctypes.byref(n), None):
            return bytes(buf)
        return None

    def usb_bulk_read(size: int) -> bytes | None:
        if not _g_usb_handle:
            return None
        buf = (ctypes.c_ubyte * size)()
        n   = wintypes.ULONG()
        ok  = winusb.WinUsb_ReadPipe(
            _g_usb_handle, PASCO_BULK_IN_EP, buf, size,
            ctypes.byref(n), None)
        return bytes(buf) if (ok and n.value >= size) else None

    def usb_drain() -> None:
        if not _g_usb_handle:
            return
        buf = (ctypes.c_ubyte * PASCO_DRAIN_BYTES)()
        n   = wintypes.ULONG()
        winusb.WinUsb_ReadPipe(
            _g_usb_handle, PASCO_BULK_IN_EP, buf,
            PASCO_DRAIN_BYTES, ctypes.byref(n), None)

    def free_usb_resources() -> None:
        global _g_usb_handle, _g_device_handle
        if _g_usb_handle:
            winusb.WinUsb_Free(_g_usb_handle)
            _g_usb_handle = None
        if _g_device_handle:
            kernel32.CloseHandle(_g_device_handle)
            _g_device_handle = None

else:
    # Linux / macOS — delegate to pyusb backend
    from _usb_linux import (
        scan_usb_devices,
        connect_usb_device,
        usb_control_out,
        usb_control_in,
        usb_bulk_read,
        usb_drain,
        free_usb_resources,
    )


# ══════════════════════════════════════════════════════════════════════════
# 3. SPECTROMETERACQUISITION — PASCO HARDWARE THREAD
# ══════════════════════════════════════════════════════════════════════════

class SpectrometerAcquisition(threading.Thread):
    """
    Background thread for continuous PASCO PS-2600A acquisition.

    Callbacks (all called from this thread, not the GUI thread):
      on_frame(pixels, ob_mean, integration_us)
      on_auto_exp(new_integration_ms)
      on_connection_lost()
    """

    def __init__(
        self,
        *,
        on_frame,
        on_auto_exp        = None,
        on_connection_lost = None,
    ):
        super().__init__(daemon=True)
        self.on_frame           = on_frame
        self.on_auto_exp        = on_auto_exp
        self.on_connection_lost = on_connection_lost

        self.is_thread_running      = True
        self.is_measurement_paused  = False
        self.is_auto_exposure_active = False
        self.current_integration_time_us = PASCO_START_INTEGRATION_US

        # Fast Preview state
        self.fast_preview_enabled    = False
        self.fast_preview_short_pct  = 8
        self.fast_preview_n_short    = 4
        self._fp_frame_counter       = 0

        usb_control_out(PASCO_CMD_INIT)
        # NOTE: do NOT call apply_hardware_exposure_time here.
        # PascoPS2600A.start() sets current_integration_time_us to the
        # user's saved value before calling thread.start(), so the first
        # run() iteration picks up the correct time via last_hw_us.

    def apply_hardware_exposure_time(self, us: int) -> None:
        low  = us & 0xFFFF
        high = (us >> 16) & 0xFFFF
        usb_control_out(PASCO_CMD_SET_EXP, value=low, index=high)

    def _acquire_one_frame(self, t_us: int) -> tuple[np.ndarray, float] | None:
        """Trigger, poll, read, drain. Returns (pixels, ob_mean) or None."""
        if not usb_control_out(PASCO_CMD_TRIGGER):
            return None
        max_polls = int((t_us / 1000.0 + 1000) / 2)
        ready = False
        for _ in range(max_polls):
            if not self.is_thread_running:
                return None
            status = usb_control_in(PASCO_CMD_POLL_STATUS, 1)
            if status is None:
                return None
            if status[0] != PASCO_POLL_READY:
                ready = True
                break
            time.sleep(PASCO_POLL_INTERVAL_S)
        if not ready:
            return None

        raw = usb_bulk_read(PASCO_SPECTRUM_BYTES)
        if raw is None:
            return None
        usb_drain()

        ob = raw[PASCO_OB_BYTE_START:PASCO_OB_BYTE_END]
        ob_mean = float(np.mean(
            np.array(struct.unpack(f"<{PASCO_OB_PIXEL_COUNT}H", ob),
                     dtype=float)))

        spec = raw[PASCO_HEADER_BYTES:
                   PASCO_HEADER_BYTES + PASCO_PIXEL_COUNT * PASCO_BYTES_PER_PX]
        pixels = np.array(
            struct.unpack(f"<{PASCO_PIXEL_COUNT}H", spec), dtype=float)
        return pixels, ob_mean

    def stop_thread(self) -> None:
        self.is_thread_running = False
        self.join(timeout=4.0)

    def run(self) -> None:
        # last_hw_us tracks what the hardware is currently programmed for.
        # -1 on first iteration forces a pre-trigger prime so frame 1 uses
        # the GUI value. Every subsequent frame is programmed at the END of
        # the previous iteration (documented protocol order:
        #   TRIGGER → READ → DRAIN → SET_EXP(next))
        # so each frame is genuinely exposed at the requested time with no
        # one-cycle lag.
        last_hw_us = -1

        while self.is_thread_running:
            if self.is_measurement_paused:
                time.sleep(PASCO_PAUSE_SLEEP_S)
                continue

            t_long = self.current_integration_time_us

            # ── Fast Preview (simple: short exposure + software scaling) ──
            # FP just shortens the exposure to `short_pct` of the user value
            # and scales the net signal back up by t_long/t_short. No long
            # frame, no interleave cadence — every frame is a complete,
            # displayable, level-correct spectrum. Mutually exclusive with AE.
            fp = (self.fast_preview_enabled
                  and not self.is_auto_exposure_active
                  and t_long > PASCO_MIN_INTEGRATION_US * 2)
            t_this = (max(PASCO_MIN_INTEGRATION_US,
                          int(t_long * self.fast_preview_short_pct / 100.0))
                      if fp else t_long)

            # Pre-trigger prime: only on the very first frame. Later frames are
            # pre-programmed at the end of the previous iteration.
            if last_hw_us == -1:
                self.apply_hardware_exposure_time(t_this)
                last_hw_us = t_this

            result = self._acquire_one_frame(t_this)
            if result is None:
                if self.on_connection_lost:
                    self.on_connection_lost()
                break
            pixels, ob_mean = result

            forward = True

            if fp:
                # Scale the short frame up to long-exposure-equivalent ADC.
                # Dark current is handled per frame: scale ONLY the net above
                # the dark baseline (signal ∝ exposure), then re-add the dark.
                # Clip to saturation so an over-bright short frame can't make
                # runaway scaled counts.
                if float(np.max(pixels)) < PASCO_ADC_SATURATION * 0.92:
                    dark_est = (PASCO_OB_CORRECTION_SLOPE * ob_mean
                                + PASCO_OB_CORRECTION_OFFSET)
                    ratio  = t_long / float(t_this)
                    scaled = np.minimum((pixels - dark_est) * ratio,
                                        PASCO_ADC_SATURATION - dark_est)
                    pixels = np.maximum(0.0, scaled + dark_est)
                else:
                    # Even the short exposure is clipping → bad data, drop.
                    forward = False

            # ── Auto exposure (FP off; AE and FP are mutually exclusive) ──
            if self.is_auto_exposure_active:
                peak = float(np.max(pixels))
                new_us = t_long
                if peak >= PASCO_ADC_SATURATION:
                    new_us = int(t_long * AUTO_EXP_EMERGENCY_DROP_RATIO)
                else:
                    if peak < 10:
                        peak = 10
                    if abs(peak - PASCO_AE_TARGET_ADC) > PASCO_AE_DEADZONE:
                        r = PASCO_AE_TARGET_ADC / peak
                        r = max(AUTO_EXP_MIN_RATIO,
                                min(AUTO_EXP_MAX_RATIO, r))
                        new_us = int(t_long * r)
                new_us = max(PASCO_MIN_INTEGRATION_US,
                             min(PASCO_MAX_INTEGRATION_US, new_us))
                if new_us != t_long:
                    self.current_integration_time_us = new_us
                    t_long = new_us          # keep local view consistent
                    if self.on_auto_exp:
                        self.on_auto_exp(new_us / 1000.0)

            # ── POST-READ: program hardware for the NEXT iteration ──────
            # Uses the (possibly AE-updated) current_integration_time_us and
            # re-derives the short exposure, so an exposure change — including
            # toggling FP off (t_short → t_long) — is applied before the next
            # trigger.
            t_long_next = self.current_integration_time_us
            t_next = (max(PASCO_MIN_INTEGRATION_US,
                          int(t_long_next * self.fast_preview_short_pct / 100.0))
                      if fp else t_long_next)
            if t_next != last_hw_us:
                self.apply_hardware_exposure_time(t_next)
                last_hw_us = t_next

            if forward:
                self.on_frame(pixels, ob_mean, t_long, FRAME_STANDARD, 1.0)


# ══════════════════════════════════════════════════════════════════════════
# 4. PASCORE PS-2600A BACKEND (BaseSpectrometer implementation)
# ══════════════════════════════════════════════════════════════════════════

class PascoPS2600A(BaseSpectrometer):
    """
    PASCO PS-2600A backend.
    All constants, USB layer, and acquisition thread are above.
    Property setters propagate runtime changes into the running thread.
    """

    PIXEL_COUNT           = PASCO_PIXEL_COUNT
    WL_MIN_NM             = float(PASCO_WAVELENGTH_ARRAY[0])
    WL_MAX_NM             = float(PASCO_WAVELENGTH_ARRAY[-1])
    SPEC_CONFIG_PREFIX    = "pasco"      # pasco_spec_min_nm / pasco_spec_max_nm
    SUPPORTS_OB           = True
    SUPPORTS_FAST_PREVIEW = True
    RESPONSE_TABLE        = PASCO_RESPONSE_TABLE   # Nx2 array; GUI builds gain from this

    def __init__(self, **kwargs):
        self._thread = None
        self._is_measurement_paused   = False
        self._is_auto_exposure_active = False
        self._fast_preview_enabled    = False
        self._fast_preview_short_pct  = 8
        self._fast_preview_n_short    = 4
        # Instance wavelength array — starts as the factory polynomial but can
        # be overridden by calibrate_from_lines() without touching the module constant.
        self._wl = PASCO_WAVELENGTH_ARRAY.copy()
        super().__init__(**kwargs)

        # PASCO integration bounds are fixed by the hardware (not config-tunable).
        # Exposed so the GUI / web server size the exposure control correctly.
        self.min_integration_us = PASCO_MIN_INTEGRATION_US
        self.max_integration_us = PASCO_MAX_INTEGRATION_US

    # ── Propagating properties ────────────────────────────────────────

    @property
    def is_measurement_paused(self) -> bool:
        return self._is_measurement_paused

    @is_measurement_paused.setter
    def is_measurement_paused(self, v: bool) -> None:
        self._is_measurement_paused = v
        if self._thread:
            self._thread.is_measurement_paused = v

    @property
    def is_auto_exposure_active(self) -> bool:
        return self._is_auto_exposure_active

    @is_auto_exposure_active.setter
    def is_auto_exposure_active(self, v: bool) -> None:
        self._is_auto_exposure_active = v
        if self._thread:
            self._thread.is_auto_exposure_active = v

    @property
    def fast_preview_enabled(self) -> bool:
        return self._fast_preview_enabled

    @fast_preview_enabled.setter
    def fast_preview_enabled(self, v: bool) -> None:
        self._fast_preview_enabled = v
        if self._thread:
            self._thread.fast_preview_enabled = v
            self._thread._fp_frame_counter = 0

    @property
    def fast_preview_short_pct(self) -> int:
        return self._fast_preview_short_pct

    @fast_preview_short_pct.setter
    def fast_preview_short_pct(self, v: int) -> None:
        self._fast_preview_short_pct = v
        if self._thread:
            self._thread.fast_preview_short_pct = v
            self._thread._fp_frame_counter = 0

    @property
    def fast_preview_n_short(self) -> int:
        return self._fast_preview_n_short

    @fast_preview_n_short.setter
    def fast_preview_n_short(self, v: int) -> None:
        self._fast_preview_n_short = v
        if self._thread:
            self._thread.fast_preview_n_short = v
            self._thread._fp_frame_counter = 0

    # ── BaseSpectrometer interface ─────────────────────────────────────

    @staticmethod
    def scan() -> list[str]:
        try:
            return scan_usb_devices()
        except Exception:
            return []

    def connect(self, device_id: str) -> None:
        connect_usb_device(device_id)
        # Apply a persisted wavelength calibration (from the wavelength wizard)
        # over the factory polynomial, if one is stored for this device.
        try:
            from app_config import Config
            coeffs = Config.get("pasco_wl_poly_coeffs", None)
            if coeffs:
                self._wl = poly_wavelength_array(list(coeffs), PASCO_PIXEL_COUNT)
                self.WL_MIN_NM = float(self._wl[0])
                self.WL_MAX_NM = float(self._wl[-1])
                print(f"[PASCO] Wavelength calibration loaded from config "
                      f"({self.WL_MIN_NM:.1f}-{self.WL_MAX_NM:.1f} nm)")
        except Exception as e:
            print(f"[PASCO] wl coeff load skipped: {e}")
        # Do NOT reset current_integration_time_us here — the GUI sets it
        # from the spinbox value before calling start().

    def start(self) -> None:
        self._thread = SpectrometerAcquisition(
            on_frame           = self.on_frame,
            on_auto_exp        = self.on_auto_exp,
            on_connection_lost = self.on_connection_lost,
        )
        self._thread.current_integration_time_us = self.current_integration_time_us
        self._thread.is_auto_exposure_active      = self._is_auto_exposure_active
        self._thread.is_measurement_paused        = self._is_measurement_paused
        self._thread.fast_preview_enabled         = self._fast_preview_enabled
        self._thread.fast_preview_short_pct       = self._fast_preview_short_pct
        self._thread.fast_preview_n_short         = self._fast_preview_n_short
        self._thread.start()

    def stop(self) -> None:
        if self._thread:
            self._thread.stop_thread()
            self._thread = None

    def disconnect(self) -> None:
        self.stop()
        free_usb_resources()

    def set_integration_time_us(self, us: int) -> None:
        us = max(PASCO_MIN_INTEGRATION_US, min(PASCO_MAX_INTEGRATION_US, us))
        self.current_integration_time_us = us
        if self._thread:
            self._thread.current_integration_time_us = us
            self._thread.apply_hardware_exposure_time(us)

    @property
    def wavelength_array(self) -> np.ndarray:
        return self._wl

    def calibrate_from_lines(
        self,
        known_wavelengths_nm: list[float],
        pixel_positions: list[float] | None = None,
        degree: int = 3,
    ) -> dict:
        """
        Fit a wavelength polynomial from known emission lines and apply it.

        Parameters
        ----------
        known_wavelengths_nm : list[float]
            True wavelengths of the calibration lines (nm).
        pixel_positions : list[float], optional
            Measured sub-pixel positions of those lines.
            If omitted, estimated from the current wavelength array.
        degree : int
            Polynomial degree (default 3; use 2 if fewer than 4 lines).

        Returns
        -------
        dict with keys:
            "coeffs"    : list[float] — new polynomial [C0..C3]
            "residuals" : list[float] — fit residuals per line (nm)
            "max_error" : float       — maximum absolute residual (nm)

        The updated wavelength array takes effect immediately via wavelength_array.
        To persist, save the returned coeffs to Config as "pasco_wl_poly_coeffs".
        """
        if pixel_positions is None:
            pixel_positions = [
                wavelength_to_pixel(wl, self._wl)
                for wl in known_wavelengths_nm
            ]
            pairs = [(px, wl) for px, wl in
                     zip(pixel_positions, known_wavelengths_nm)
                     if px == px]   # drop NaN
            pixel_positions      = [p for p, _ in pairs]
            known_wavelengths_nm = [w for _, w in pairs]

        coeffs, residuals = fit_wavelength_polynomial(
            pixel_positions, known_wavelengths_nm,
            degree=degree, pixel_count=PASCO_PIXEL_COUNT,
        )
        self._wl       = poly_wavelength_array(coeffs, PASCO_PIXEL_COUNT)
        self.WL_MIN_NM = float(self._wl[0])
        self.WL_MAX_NM = float(self._wl[-1])

        result = {
            "coeffs":    coeffs,
            "residuals": residuals.tolist(),
            "max_error": float(np.max(np.abs(residuals))),
        }
        print(f"[PASCO] Polynomial fit: max residual {result['max_error']:.3f} nm")
        print(f"[PASCO] Coeffs: {coeffs}")
        return result

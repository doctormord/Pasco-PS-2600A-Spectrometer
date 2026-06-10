"""
_device_lr2t.py  —  ASEQ Instruments / Lasertrack LR-2T backend

All ASEQ-specific constants, commands, and calibration live here.

Protocol source: libspectr.c (official DLL source code)

Device identification
---------------------
  Manufacturer : ASEQ Instruments
  VID          : 0xE220
  PID          : 0x0100
  Driver       : Windows HID (native, no Zadig needed)
  Serial format: "ASQ_SPCxxxxxxx"

Communication
-------------
HID reports, 65 bytes each:
  byte 0    : Report ID = 0x00
  bytes 1–64: Payload (command + parameters, little-endian)

Exposure unit: 10 µs increments (50 ms = 5000 units)

Pixel order
-----------
The device returns pixels with index 0 = longest wavelength (NIR) and
index 3652 = shortest wavelength (UV).  The calibration file lists
wavelengths in ascending order (UV → NIR).  We flip the raw pixel array
after reading so index 0 = shortest wavelength, matching the calibration
file and the rest of the application.

Calibration file
----------------
Named <serial>.cal, placed in the application directory.
  Line 0  : model string  (e.g. "LR2B4.0 c.N 1894")
  Line 1  : absolute irradiance coefficient
  Line 2  : BoxCar width (ignored here)
  Lines 3–11  : empty
  Lines 12–3664 : wavelength (nm) per pixel, ascending UV → NIR (3653 values)
  Line 3665    : empty
  Lines 3666–7318 : intensity normalisation coefficients (3653 values)

Dark current
------------
The LR-2T has no optical-black pixels.  Per-frame baseline is estimated
from the median of the N_DARK_ESTIMATE_PIXELS darkest pixels in the frame
(default: 50).  This is subtracted before forwarding the frame so the
GUI dark correction pipeline receives a baseline-corrected spectrum.

Wavelength calibration note
---------------------------
The factory calibration file may be off by up to 15 nm depending on unit.
A fine-tuning offset (WL_OFFSET_NM) can be adjusted here or persisted via
spectrometer_config.json ("lr2t_wl_offset_nm").

Install
-------
  Windows: pip install hidapi      ← required
  Linux:   pip install hidapi  AND  add udev rule for VID_E220 PID_0100
"""

from __future__ import annotations

import os
import threading
import time

import numpy as np

# Try both HID bindings — hidapi is required on Windows, hid works on Linux/macOS
try:
    import hidapi as _hid_module
    _HID_AVAILABLE = True
    _HID_BINDING   = "hidapi"
except ImportError:
    try:
        import hid as _hid_module
        _HID_AVAILABLE = True
        _HID_BINDING   = "hid"
    except ImportError:
        _HID_AVAILABLE = False
        _HID_BINDING   = None

from device_manager import BaseSpectrometer
from fusion import FRAME_STANDARD
from calibration_utils import (
    apply_wl_offset,
    fit_wavelength_polynomial,
    poly_wavelength_array,
    build_response_gain,
    wavelength_to_pixel,
)
from device_constants import (
    GLOBAL_MIN_INTEGRATION_TIME_US,
    GLOBAL_MAX_INTEGRATION_TIME_US,
    AUTO_EXP_MIN_RATIO,
    AUTO_EXP_MAX_RATIO,
    AUTO_EXP_EMERGENCY_DROP_RATIO,
)

# ── Device hardware constants ─────────────────────────────────────────────

ASEQ_VID = 0xE220
ASEQ_PID = 0x0100

ASEQ_PIXEL_COUNT         = 3653
ASEQ_DUMMY_PIXELS        = 29    # Leading dummy/dark pixels the device sends
                                  # before the real imaging pixels start.
                                  # Empirically determined from Cd lamp comparison:
                                  # skip first 29 of 3694 → sub-0.3nm accuracy.
                                  # Raw frame = ASEQ_DUMMY_PIXELS + ASEQ_PIXEL_COUNT
ASEQ_ADC_MAX             = 65535          # 16-bit ADC
ASEQ_ADC_SATURATION      = 64500          # threshold below full-scale
ASEQ_MIN_INTEGRATION_US  = 1_000
ASEQ_MAX_INTEGRATION_US  = 10_000_000

# Auto-exposure targets (16-bit ADC)
ASEQ_AE_TARGET_ADC  = 55000
ASEQ_AE_DEADZONE    = 2000

# Dark baseline estimation
N_DARK_ESTIMATE_PIXELS = 50   # median of N darkest pixels used as offset

# HID communication
REPORT_ID          = 0x00
PACKET_SIZE        = 64
REPORT_SIZE        = PACKET_SIZE + 1
TIMEOUT_MS         = 300
NUM_PIXELS_IN_PACK = 30
REMAINING_ERR      = 250

# ── Command codes (from libspectr.c) ──────────────────────────────────────

CMD_STATUS         = 0x01
CMD_SET_EXPOSURE   = 0x02
CMD_SET_ACQ_PARAMS = 0x03
CMD_SET_FRAME_FMT  = 0x04
CMD_GET_FRAME_FMT  = 0x08
CMD_TRIGGER        = 0x06
CMD_GET_FRAME      = 0x0A
CMD_CLEAR_MEMORY   = 0x07

REPLY_STATUS    = 0x81
REPLY_SET_EXP   = 0x82
REPLY_SET_ACQ   = 0x83
REPLY_GET_FMT   = 0x88
REPLY_CLEAR_MEM = 0x87
REPLY_GET_FRAME = 0x8A

SCAN_MODE_CONTINUOUS = 0


# ── Calibration file parser ────────────────────────────────────────────────

def _load_calibration(path: str
                      ) -> tuple[np.ndarray, np.ndarray, float] | None:
    """
    Parse an ASEQ .cal file.

    Returns (wavelengths_nm, norm_coeffs, irradiance_factor)
    where both arrays are indexed pixel 0 = shortest wavelength (UV).
    Returns None on any parse failure.
    """
    try:
        with open(path, "r", encoding="utf-8", errors="ignore") as f:
            raw = [l.strip().replace("\t", "").replace("\r", "")
                   for l in f.readlines()]
    except Exception:
        return None

    n_px = 3653

    # Absolute irradiance coefficient at line 1
    try:
        irr_factor = float(raw[1])
    except (IndexError, ValueError):
        irr_factor = 1.0

    # Wavelengths at lines 12 … 12+n_px-1
    try:
        wl = np.array([float(raw[12 + i]) for i in range(n_px)], dtype=float)
    except (IndexError, ValueError):
        return None

    # Intensity normalisation at lines 12+n_px+1 … (line 3666 onwards)
    norm_start = 12 + n_px + 1
    try:
        norm = np.array([float(raw[norm_start + i]) for i in range(n_px)],
                        dtype=float)
    except (IndexError, ValueError):
        norm = np.ones(n_px, dtype=float)

    # Cal file is already ascending (UV→NIR), no flip needed here.
    # The pixel array is flipped in _cmd_get_frame() to match.
    return wl, norm, irr_factor


# ── Backend class ─────────────────────────────────────────────────────────

class LasertrackLR2T(BaseSpectrometer):
    """
    ASEQ Instruments spectrometer (sold as Lasertrack LR-2T and similar).
    Full HID protocol from the libspectr.c DLL source.
    """

    PIXEL_COUNT           = ASEQ_PIXEL_COUNT
    WL_MIN_NM             = 297.0
    WL_MAX_NM             = 981.0
    SUPPORTS_OB           = False
    SUPPORTS_FAST_PREVIEW = True
    RESPONSE_TABLE        = None   # no response correction characterised yet

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self._dev                         = None
        self._thread: threading.Thread | None = None
        self._running                     = False
        self._n_pixels_in_frame           = ASEQ_PIXEL_COUNT
        self._wl: np.ndarray              = np.linspace(
            self.WL_MIN_NM, self.WL_MAX_NM, ASEQ_PIXEL_COUNT)
        self._norm: np.ndarray            = np.ones(ASEQ_PIXEL_COUNT)
        self._irr_factor: float           = 1.0
        self._lock                        = threading.Lock()

        # Wavelength fine-tuning — loaded from Config in connect(); initialised
        # here to avoid AttributeError if wavelength_array is read before connect.
        self.wl_offset_nm: float          = 0.0

        # Pixel order: False = device sends UV first (ascending), no flip needed.
        # ASQ_SPC5636146 confirmed: flip_pixels=False is correct.
        self.flip_pixels: bool            = False
        # Optional override polynomial (fit from emission lines).
        self.wl_poly_coeffs: list[float] | None = None

        # Fast-preview state
        self.fast_preview_enabled   = False
        self.fast_preview_short_pct = 8
        self.fast_preview_n_short   = 4
        self._fp_frame_counter      = 0

        # Per-device integration bounds (µs) — overridden from Config in
        # connect(); defaults match the historical hardware constants.
        self._min_us = ASEQ_MIN_INTEGRATION_US
        self._max_us = ASEQ_MAX_INTEGRATION_US
        self.min_integration_us = ASEQ_MIN_INTEGRATION_US
        self.max_integration_us = ASEQ_MAX_INTEGRATION_US

        # Frames to drop after an integration-time change (stale-frame guard,
        # mirrors the HDX). Loaded from Config in connect().
        self._discard_n            = 1
        self._discard_after_change = 0

    @staticmethod
    def scan() -> list[str]:
        if not _HID_AVAILABLE:
            return []
        try:
            if _HID_BINDING == "hidapi":
                devices = _hid_module.enumerate(ASEQ_VID, ASEQ_PID)
            else:
                devices = _hid_module.enumerate(ASEQ_VID, ASEQ_PID)
            seen, out = set(), []
            for d in (devices or []):
                sn = (d.get("serial_number") or d.get("serial") or "")
                key = sn or str(d.get("path", ""))
                if key and key not in seen:
                    seen.add(key)
                    label = f"ASEQ {sn}" if sn else f"ASEQ (port {key[-6:]})"
                    out.append(label)
            return out
        except Exception:
            return []

    def connect(self, device_id: str) -> None:
        if not _HID_AVAILABLE:
            raise RuntimeError(
                "hidapi package not installed.\n"
                "Run:  pip install hidapi\n"
                "No driver change needed — uses the native Windows HID driver."
            )

        serial = None
        if device_id.startswith("ASEQ ") and not device_id.startswith("ASEQ ("):
            serial = device_id[5:].strip()

        # Open the HID device — API differs slightly between bindings
        if _HID_BINDING == "hidapi":
            self._dev = _hid_module.Device(vendor_id=ASEQ_VID,
                                           product_id=ASEQ_PID,
                                           serial=serial or None)
        else:
            self._dev = _hid_module.device()
            self._dev.open(ASEQ_VID, ASEQ_PID, serial or None)

        self._dev.set_nonblocking(0)

        # Read actual frame size from device
        self._n_pixels_in_frame = self._cmd_get_frame_format()

        # A persisted wavelength polynomial (from the wavelength wizard) takes
        # precedence over the device's linear/cal-file axis. The cal-file block
        # below already honours self.wl_poly_coeffs; the fallback after it covers
        # the no-cal-file case.
        try:
            from app_config import Config as _Cfg
            _persisted = _Cfg.get("lr2t_wl_poly_coeffs", None)
            if _persisted:
                self.wl_poly_coeffs = list(_persisted)
        except Exception:
            pass

        # Load calibration file if present
        if serial:
            for search_dir in (os.path.dirname(__file__), os.getcwd()):
                cal_path = os.path.join(search_dir, f"{serial}.cal")
                if os.path.exists(cal_path):
                    result = _load_calibration(cal_path)
                    if result is not None:
                        wl, norm, irr = result
                        # Cal file is ascending UV→NIR.
                        # If flip_pixels=True: device sends NIR first,
                        #   we flip data → arr[0]=UV = matches wl[0]=UV ✓
                        # If flip_pixels=False: device sends UV first,
                        #   no flip → arr[0]=UV = matches wl[0]=UV ✓
                        # Either way wl stays ascending. norm stays in same
                        # order as wl (both ascending UV→NIR).
                        self._norm        = norm
                        self._irr_factor  = irr
                        self.PIXEL_COUNT  = len(wl)
                        if self.wl_poly_coeffs is not None:
                            wl = poly_wavelength_array(
                                self.wl_poly_coeffs, self.PIXEL_COUNT)
                        self._wl          = wl + self.wl_offset_nm
                        self.WL_MIN_NM    = float(self._wl[0])
                        self.WL_MAX_NM    = float(self._wl[-1])
                        print(f"[LR-2T] Cal: {cal_path}")
                        print(f"[LR-2T] Range: "
                              f"{self.WL_MIN_NM:.1f}–{self.WL_MAX_NM:.1f} nm, "
                              f"flip_pixels={self.flip_pixels}, "
                              f"offset={self.wl_offset_nm:+.1f} nm")
                    break

        # Fallback: if a polynomial calibration is set but no cal file applied
        # it above (no .cal present for this serial), build the axis from the
        # polynomial now so the wizard's calibration still takes effect.
        if self.wl_poly_coeffs is not None and not np.array_equal(
                self._wl,
                poly_wavelength_array(self.wl_poly_coeffs, self.PIXEL_COUNT)
                + self.wl_offset_nm):
            self._wl = (poly_wavelength_array(self.wl_poly_coeffs, self.PIXEL_COUNT)
                        + self.wl_offset_nm)
            self.WL_MIN_NM = float(self._wl[0])
            self.WL_MAX_NM = float(self._wl[-1])

        # Resolve per-device integration bounds from config (fall back to the
        # hardware constants). Exposed so the GUI / web server size the exposure
        # control to this device.
        from app_config import Config
        self._min_us = max(GLOBAL_MIN_INTEGRATION_TIME_US,
                           int(Config.get("lr2t_min_integration_us",
                                          ASEQ_MIN_INTEGRATION_US)))
        self._max_us = int(Config.get("lr2t_max_integration_us",
                                      ASEQ_MAX_INTEGRATION_US))
        self.min_integration_us = self._min_us
        self.max_integration_us = self._max_us
        self._discard_n = max(0, int(Config.get("lr2t_discard_frames_after_itime", 1)))

        self.current_integration_time_us = max(
            self._min_us, self.current_integration_time_us)
        self._cmd_set_exposure(self.current_integration_time_us)
        self._cmd_set_acq_params(scans=1, blank_scans=0,
                                 scan_mode=SCAN_MODE_CONTINUOUS)

    def start(self) -> None:
        self._running = True
        self._thread  = threading.Thread(
            target=self._run, daemon=True, name="ASEQ-acq")
        self._thread.start()

    def stop(self) -> None:
        self._running = False
        if self._thread:
            self._thread.join(timeout=3.0)
            self._thread = None

    def disconnect(self) -> None:
        self.stop()
        if self._dev:
            try:
                self._dev.close()
            except Exception:
                pass
            self._dev = None

    def set_integration_time_us(self, us: int) -> None:
        # Only update the stored value; the run loop applies it safely
        # between frames to avoid USB collision.
        self.current_integration_time_us = max(
            self._min_us, min(self._max_us, us))

    @property
    def wavelength_array(self) -> np.ndarray:
        # Hier holen wir den Wert direkt aus der Config, ohne andere Abhängigkeiten zu stören
        from app_config import Config
        return self._wl + Config.get("lr2t_wl_offset_nm", 0.0)

    def calibrate_from_lines(
        self,
        known_wavelengths_nm: list[float],
        pixel_positions: list[float] | None = None,
        degree: int = 3,
    ) -> dict:
        """
        Fit a degree-3 polynomial wavelength calibration from known emission lines.

        If pixel_positions is None, the positions are estimated by finding
        the nearest peaks in the current wavelength array against the
        known wavelengths (useful for a rough first pass).

        Parameters
        ----------
        known_wavelengths_nm : list[float]
            True wavelengths of the calibration lines (nm).
        pixel_positions : list[float], optional
            Measured sub-pixel positions of those lines.
            If omitted, estimated from the current wavelength array.
        degree : int
            Polynomial degree (default 3).  Use 2 if < 4 lines.

        Returns
        -------
        dict with keys:
            "coeffs"     : list[float] — new polynomial [C0..C3]
            "residuals"  : list[float] — fit residuals per line (nm)
            "max_error"  : float       — maximum absolute residual (nm)

        After a successful fit, set self.wl_poly_coeffs = result["coeffs"]
        and reconnect, or update self._wl directly for an immediate effect.
        """
        if pixel_positions is None:
            from calibration_utils import wavelength_to_pixel
            pixel_positions = [
                wavelength_to_pixel(wl, self._wl)
                for wl in known_wavelengths_nm
            ]
            # Remove NaN entries
            pairs = [(px, wl) for px, wl in
                     zip(pixel_positions, known_wavelengths_nm)
                     if not (px != px)]  # NaN check
            pixel_positions       = [p for p, _ in pairs]
            known_wavelengths_nm  = [w for _, w in pairs]

        coeffs, residuals = fit_wavelength_polynomial(
            pixel_positions, known_wavelengths_nm, degree=degree,
            pixel_count=self.PIXEL_COUNT)

        # Apply immediately
        self.wl_poly_coeffs = coeffs
        self._wl = poly_wavelength_array(coeffs, self.PIXEL_COUNT)
        if self.wl_offset_nm:
            self._wl = self._wl + self.wl_offset_nm
        self.WL_MIN_NM = float(self._wl[0])
        self.WL_MAX_NM = float(self._wl[-1])

        result = {
            "coeffs":    coeffs,
            "residuals": residuals.tolist(),
            "max_error": float(np.max(np.abs(residuals))),
        }
        print(f"[LR-2T] Polynomial fit: max residual {result['max_error']:.3f} nm")
        print(f"[LR-2T] Coeffs: {coeffs}")
        return result

    # ── HID helpers ───────────────────────────────────────────────────────

    def _write(self, buf: bytes) -> None:
        report = bytearray(REPORT_SIZE)
        report[0] = REPORT_ID
        report[1:1 + len(buf)] = buf
        with self._lock:
            self._dev.write(bytes(report))

    def _read(self, expected: int) -> bytearray:
        data = self._dev.read(REPORT_SIZE, TIMEOUT_MS)
        if not data or len(data) < 2:
            raise IOError(
                f"[LR-2T] Empty read (expected 0x{expected:02X})")
        report = bytearray(data[:REPORT_SIZE])
        if report[0] != expected:
            raise IOError(
                f"[LR-2T] Wrong reply 0x{report[0]:02X}, "
                f"expected 0x{expected:02X}")
        return report

    def _wr(self, buf: bytes, expected: int) -> bytearray:
        self._write(buf)
        return self._read(expected)

    # ── Device commands ────────────────────────────────────────────────────

    def _cmd_set_exposure(self, us: int) -> None:
        """Exposure in 10 µs units."""
        units = max(1, us // 10)
        buf = bytearray(7)
        buf[0] = CMD_SET_EXPOSURE
        buf[1] = units & 0xFF
        buf[2] = (units >> 8)  & 0xFF
        buf[3] = (units >> 16) & 0xFF
        buf[4] = (units >> 24) & 0xFF
        buf[5] = 1    # force flag
        self._wr(bytes(buf), REPLY_SET_EXP)

    def _cmd_set_acq_params(self, scans: int, blank_scans: int,
                             scan_mode: int) -> None:
        buf = bytearray(7)
        buf[0] = CMD_SET_ACQ_PARAMS
        buf[1] = scans & 0xFF
        buf[2] = (scans >> 8) & 0xFF
        buf[3] = blank_scans & 0xFF
        buf[4] = (blank_scans >> 8) & 0xFF
        buf[5] = scan_mode
        self._wr(bytes(buf), REPLY_SET_ACQ)

    def _cmd_get_frame_format(self) -> int:
        """
        Query actual frame size from device.
        The device reports 3694 pixels (3653 real + 41 dummy).
        We also call setFrameFormat to try to restrict output to 3653,
        but trim anyway in _cmd_get_frame as a belt-and-suspenders.
        """
        report = self._wr(bytearray([CMD_GET_FRAME_FMT]), REPLY_GET_FMT)
        n = report[6] | (report[7] << 8)
        print(f"[LR-2T] Device reports {n} frame elements")
        return n   # return actual size; trimming happens in _cmd_get_frame

    def _cmd_get_status(self) -> tuple[int, int]:
        report = self._wr(bytearray([CMD_STATUS]), REPLY_STATUS)
        return report[1], (report[2] | (report[3] << 8))

    def _cmd_trigger(self) -> None:
        buf = bytearray([CMD_TRIGGER])
        self._write(bytes(buf))   # no reply

    def _cmd_clear_memory(self) -> None:
        self._wr(bytearray([CMD_CLEAR_MEMORY]), REPLY_CLEAR_MEM)

    def _cmd_get_frame(self, frame_num: int = 0) -> np.ndarray:
        """
        Read one complete frame.

        The device delivers pixels in reverse wavelength order
        (index 0 = longest λ).  We flip the array so index 0 = shortest λ,
        matching the ascending calibration wavelength table.
        """
        n_px   = self._n_pixels_in_frame
        n_pack = (n_px + NUM_PIXELS_IN_PACK - 1) // NUM_PIXELS_IN_PACK

        buf = bytearray(7)
        buf[0] = CMD_GET_FRAME
        buf[1] = buf[2] = 0           # start offset low/high
        buf[3] = frame_num & 0xFF
        buf[4] = (frame_num >> 8) & 0xFF
        buf[5] = n_pack
        self._write(bytes(buf))

        pixels = np.zeros(n_px, dtype=np.uint16)

        while True:
            data = self._dev.read(REPORT_SIZE, TIMEOUT_MS)
            if not data:
                raise IOError("[LR-2T] Timeout reading frame")
            report = bytearray(data)
            if report[0] != REPLY_GET_FRAME:
                raise IOError(f"[LR-2T] Bad frame reply 0x{report[0]:02X}")

            offset    = report[1] | (report[2] << 8)
            pkts_left = report[3]
            if pkts_left >= REMAINING_ERR:
                raise IOError(f"[LR-2T] Frame error {pkts_left}")

            for i in range(NUM_PIXELS_IN_PACK):
                idx = offset + i
                if idx >= n_px:
                    break
                b = 4 + i * 2
                pixels[idx] = report[b] | (report[b + 1] << 8)

            if pkts_left == 0:
                break

        # ── Trim dummy pixels ────────────────────────────────────────────
        # The device sends ASEQ_DUMMY_PIXELS leading dark/dummy pixels
        # before the real imaging pixels. Trim them so we get exactly
        # ASEQ_PIXEL_COUNT pixels matching the calibration file.
        raw = pixels.astype(float)
        if len(raw) > ASEQ_PIXEL_COUNT:
            raw = raw[ASEQ_DUMMY_PIXELS:ASEQ_DUMMY_PIXELS + ASEQ_PIXEL_COUNT]
        elif len(raw) < ASEQ_PIXEL_COUNT:
            # Pad with zeros if device sent fewer than expected (shouldn't happen)
            raw = np.concatenate([raw, np.zeros(ASEQ_PIXEL_COUNT - len(raw))])

        # ── Apply intensity normalisation ────────────────────────────────
        if len(self._norm) == len(raw) and not np.all(self._norm == 1.0):
            raw *= self._norm

        # ── Ensure wavelength axis matches pixel count ───────────────────
        # With correct trimming this should never fall back to linspace.
        if len(self._wl) != len(raw):
            print(f"[LR-2T] WARNING: wl length {len(self._wl)} != "
                  f"pixel count {len(raw)} — falling back to linspace")
            self._wl = np.linspace(self.WL_MIN_NM, self.WL_MAX_NM, len(raw))

        return raw

    # ── Acquisition loop ──────────────────────────────────────────────────

    def _run(self) -> None:
        last_hw_us = -1   # -1 forces hardware push on the very first frame

        while self._running:
            if self.is_measurement_paused:
                time.sleep(0.05)
                continue
            if self._dev is None:
                break

            try:
                t_long = self.current_integration_time_us

                # ── Fast Preview (simple: short exposure + software scaling) ──
                # LR-2T is trigger-based (SET_EXP → TRIGGER → WAIT → GET), so a
                # frame is genuinely exposed at exactly the programmed time. FP
                # just programs a short exposure (short_pct of the user value)
                # and scales the net signal back up by t_long/t_short. No long
                # frame, no interleave. Mutually exclusive with auto-exposure.
                fp = (self.fast_preview_enabled
                      and not self.is_auto_exposure_active
                      and t_long > self._min_us * 2)
                t_this = (max(self._min_us,
                              int(t_long * self.fast_preview_short_pct / 100.0))
                          if fp else t_long)

                # ── Apply integration time (always before trigger) ──────
                if t_this != last_hw_us:
                    self._cmd_set_exposure(t_this)
                    last_hw_us = t_this
                    # After a change the device (continuous-scan) can hand back a
                    # buffered / under-integrated frame that does NOT reflect the
                    # new exposure. Drop the next N frames so auto-exposure never
                    # regulates on a stale frame (this was the AE oscillation).
                    self._discard_after_change = self._discard_n

                self._cmd_trigger()

                # Wait the FULL programmed integration (the old 85% heuristic
                # returned under-integrated frames after a change), then poll for
                # completion WITHOUT re-triggering — a re-trigger restarts the
                # exposure, so on a long exposure a frame never completed cleanly.
                time.sleep(max(0.01, t_this / 1_000_000.0))
                n_frames = 0
                poll_deadline = time.monotonic() + max(0.2,
                                                       min(3.0, t_this / 1_000_000.0 * 0.3))
                while time.monotonic() < poll_deadline:
                    _, n_frames = self._cmd_get_status()
                    if n_frames > 0:
                        break
                    time.sleep(0.005)
                if n_frames == 0:
                    continue   # not ready within deadline → retry (re-triggers)

                pixel_array = self._cmd_get_frame(frame_num=0)
                self._cmd_clear_memory()

                # Drop stale frame(s) after an exposure change before any
                # processing / auto-exposure / forwarding.
                if self._discard_after_change > 0:
                    self._discard_after_change -= 1
                    continue

                # ── Dark baseline from darkest pixels ─────────────────────
                sorted_px = np.sort(pixel_array)
                ob_mean   = float(np.median(sorted_px[:N_DARK_ESTIMATE_PIXELS]))

                # ── Fast Preview: scale the short frame to long-equivalent ──
                forward = True
                if fp:
                    if float(np.max(pixel_array)) < ASEQ_ADC_SATURATION * 0.92:
                        ratio  = t_long / float(t_this)
                        net    = np.maximum(0.0, pixel_array - ob_mean)
                        scaled = np.minimum(net * ratio,
                                            ASEQ_ADC_SATURATION - ob_mean)
                        pixel_array = scaled + ob_mean
                    else:
                        # Even the short exposure is clipping → bad data, drop.
                        forward = False

                # ── Auto exposure (FP off; AE and FP are mutually exclusive) ──
                if self.is_auto_exposure_active:
                    max_adc = float(np.max(pixel_array))
                    signal  = max(50.0, max_adc - ob_mean)
                    t       = t_long
                    branch  = "hold"

                    if max_adc >= ASEQ_ADC_SATURATION:
                        new_us = int(t * AUTO_EXP_EMERGENCY_DROP_RATIO)
                        branch = "SAT-drop"
                    elif abs(max_adc - ASEQ_AE_TARGET_ADC) > ASEQ_AE_DEADZONE:
                        r = max(AUTO_EXP_MIN_RATIO,
                                min(AUTO_EXP_MAX_RATIO,
                                    (ASEQ_AE_TARGET_ADC - ob_mean) / signal))
                        new_us = int(t * r)
                        branch = f"prop r={r:.2f}"
                    else:
                        new_us = t

                    new_us = max(self._min_us,
                                 min(self._max_us, new_us))

                    if os.environ.get("AE_DEBUG") == "1":
                        # Diagnostic only. Watch for an over→under→over limit
                        # cycle (SAT-drop ×0.2 fighting a large prop recovery)
                        # vs. max_adc not tracking the programmed t (a stale /
                        # under-integrated frame → timing, not gain).
                        print(f"[lr2t-ae] t={t/1000:7.1f}ms max_adc={max_adc:7.0f} "
                              f"ob={ob_mean:6.0f} sig={signal:7.0f} "
                              f"{branch:>11} -> {new_us/1000:7.1f}ms", flush=True)

                    if new_us != t:
                        self.set_integration_time_us(new_us)
                        if self.on_auto_exp:
                            self.on_auto_exp(new_us / 1000.0)

                if forward:
                    # Report the exposure THIS frame was taken at (t_long), not
                    # self.current_integration_time_us — auto-exposure may have
                    # just changed the latter, which corrupted the reported time.
                    self.on_frame(pixel_array, ob_mean,
                                  t_long,
                                  FRAME_STANDARD, 1.0)

            except Exception as exc:
                if self._running:
                    print(f"[LR-2T] Error: {exc}")
                    if self.on_connection_lost:
                        self.on_connection_lost()
                break

"""
_device_ocean.py  —  Ocean Optics / Ocean Insight HDX-UV-VIS backend

All Ocean-specific constants, the OBP (Ocean Binary Protocol) transport,
and the device calibration handling live here.  Nothing Ocean-specific
leaks into spectrometer_core.py or device_manager.py.

Why a raw OBP driver instead of python-seabreeze?
-------------------------------------------------
The protocol implemented below was reverse-engineered and validated with
``hdx_diagnostic.py`` against a real HDX (S/N HDX00512).  Going straight to
OBP over libusb lets us:
  * read the on-device WAVELENGTH CALIBRATION (4 cubic coefficients), and
  * read the on-device NONLINEARITY COEFFICIENTS (8 coefficients),
both of which we apply per frame.  seabreeze hides the raw coefficients
behind ``correct_*`` flags, and on this unit only the *raw HDX* spectrum
command (0x00101000) returns data — the buffered/legacy commands NACK.

Device identification
---------------------
  Manufacturer : Ocean Optics
  Product      : OceanHDX
  VID          : 0x2457
  PID          : 0x2003
  Driver       : libusb (pyusb + libusb-package; WinUSB via Zadig on Windows)
  Serial format: "HDXxxxxx"   (e.g. "HDX00512")

Transport (OBP, "Ocean Binary Protocol")
-----------------------------------------
44-byte header + optional payload + 20-byte footer, little-endian.
  * SET commands are fire-and-forget — the device sends NO response.
  * GET commands return a header whose ``bytes_remaining`` field tells us
    how many more bytes follow, so a response is read in full regardless of
    USB packet chunking.
  * The IN pipe is flushed before each GET to drop any stale spectrum bytes
    left over from a previous acquisition (otherwise a GET parses garbage).

Pixel count
-----------
The HDX returns 2068 pixels in raw mode (≈2048 imaging pixels plus a few
transition/dark pixels).  This is roughly half the pixel count of the PASCO
(3648) and LR-2T (3653) detectors, so the GUI resizes its wavelength axis,
heatmap buffer and CIE mask to match when this device connects.

Wavelength calibration
----------------------
λ(i) = C0 + C1·i + C2·i² + C3·i³, with the four C-coefficients read from the
device at connect time.  A fine-tune offset ("ocean_wl_offset_nm") can be
applied on top.  If the device read fails, a linear fallback axis is used.

Nonlinearity correction
------------------------
The detector response is linearised with the device's 8 nonlinearity
coefficients NL[0..7]:
    P(s) = Σ NL[k] · sᵏ          (s = dark-subtracted counts)
    s_linear = s / P(s)
P(s) evaluates to ≈1.0, so this is a sub-percent shaping correction.  It is
applied to the *dark-subtracted* signal (Ocean's canonical order) and the
dark baseline is then added back, so the GUI dark-correction pipeline still
receives a normal spectrum to work on.

Dark current
------------
The HDX exposes no usable optical-black pixels in raw mode, so — exactly
like the LR-2T — the per-frame dark baseline is the median of the
N darkest pixels (default 50).  That value is forwarded as ``ob_mean`` so
the GUI "Optical Black (live)" dark mode and the parametric Bias+Rate mode
both function.

Install
-------
  pip install pyusb libusb-package
  Windows: install a WinUSB driver for the HDX with Zadig (once).
  Linux:   add a udev rule for VID 2457 / PID 2003 (or run as root once).
"""

from __future__ import annotations

import os
import struct
import threading
import time

import numpy as np

# pyusb + libusb-package are imported defensively: the module must still load
# (so the backend can appear in menus and give a helpful error) even when the
# USB stack is missing.  Mirrors the hid/hidapi handling in _device_lr2t.py.
try:
    import usb.core
    import usb.util
    import usb.backend.libusb1
    import libusb_package
    _USB_AVAILABLE = True
    _USB_IMPORT_ERROR = ""
except Exception as _exc:                       # pragma: no cover - import guard
    _USB_AVAILABLE = False
    _USB_IMPORT_ERROR = str(_exc)

from app_config import Config
from device_manager import BaseSpectrometer
from fusion import FRAME_STANDARD
from calibration_utils import (
    poly_wavelength_array,
    fit_wavelength_polynomial,
    wavelength_to_pixel,
)
from device_constants import (
    GLOBAL_MIN_INTEGRATION_TIME_US,
    GLOBAL_MAX_INTEGRATION_TIME_US,
    AUTO_EXP_MIN_RATIO,
    AUTO_EXP_MAX_RATIO,
    AUTO_EXP_EMERGENCY_DROP_RATIO,
)

# ══════════════════════════════════════════════════════════════════════════
# 1.  HARDWARE / PROTOCOL CONSTANTS  (fixed facts of the device — not tunables)
# ══════════════════════════════════════════════════════════════════════════

# ── USB identity ──────────────────────────────────────────────────────────
OCEAN_VID            = 0x2457
OCEAN_PID            = 0x2003
OCEAN_EP_OUT         = 0x01          # bulk OUT endpoint (host → device)
OCEAN_EP_IN          = 0x81          # bulk IN  endpoint (device → host)
OCEAN_USB_INTERFACE  = 0             # interface number to claim

# ── OBP framing ───────────────────────────────────────────────────────────
OBP_START            = 0xC0C1        # header magic
OBP_PROTOCOL         = 0x1100        # protocol version
OBP_END              = 0xC2C3C4C5    # footer magic
OBP_HEADER_SZ        = 44            # bytes
OBP_FOOTER_SZ        = 20            # bytes
OBP_IMMEDIATE_MAX    = 16            # payloads ≤16 B ride inside the header
OBP_BYTES_REM_OFFSET = 40            # offset of the uint32 bytes_remaining field

# OBP flag bits (in the header "flags" field of a response)
OBP_FLAG_RESPONSE    = 0x0001
OBP_FLAG_ACK         = 0x0002
OBP_FLAG_REQUEST_ACK = 0x0004        # set on a request to ask for an ACK
OBP_FLAG_NACK        = 0x0008        # set on a response that failed

# ── Fast-preview classifier tunable ───────────────────────────────────────
# Per-frame decay of the long-level reference used to tell long frames from
# short ones by their net pixel level. <1.0 lets the reference follow slow lamp
# or sample drift while staying well above the short level (short ≈ ref·t_s/t_l).
FP_LONG_REF_DECAY    = 0.98

# USB read chunk size used while reassembling a response
OBP_READ_CHUNK       = 512

# ── OBP message types ─────────────────────────────────────────────────────
# SET commands — fire-and-forget, NO response from the device.
MSG_SET_TRIG_MODE      = 0x00110110   # uint8   trigger mode (0 = free-running)
MSG_SET_ITIME          = 0x00110010   # uint32  integration time in µs
MSG_SET_TE_ENABLE      = 0x00420010   # uint8   TEC on/off
MSG_SET_TE_SETPOINT    = 0x00420011   # float32 TEC setpoint °C

# GET commands — send request, read response.
MSG_GET_SERIAL_LEN     = 0x00000101   # → uint8  length of serial string
MSG_GET_SERIAL         = 0x00000100   # → bytes  serial string
MSG_GET_WL_COEFF_COUNT = 0x00180100   # → uint8  number of wavelength coeffs
MSG_GET_WL_COEFF       = 0x00180101   # uint8 idx → float32 coefficient
MSG_GET_NL_COEFF_COUNT = 0x00181100   # → uint8  number of nonlinearity coeffs
MSG_GET_NL_COEFF       = 0x00181101   # uint8 idx → float32 coefficient
MSG_GET_TE_TEMP        = 0x00420004   # → float32 TEC temperature °C
MSG_GET_SPECTRUM_BUF   = 0x00100928   # FX buffered cmd — NACKs on this HDX firmware
MSG_GET_SPECTRUM_META  = 0x00100980   # HDX spectrum WITH metadata (header + pixels + trailer)
MSG_GET_SPECTRUM_RAW   = 0x00101000   # raw spectrum (HDX variant), no metadata
MSG_GET_SPECTRUM_RAW2  = 0x00101100   # raw spectrum (older OBP)

# Friendly names → message type, used by the "ocean_spectrum_command" config
# key and by the connect-time auto-probe (ordered most→least preferred).
# "metadata" is preferred: every frame carries its TRUE integration time in the
# header (offset 16, µs), which is what makes real fast-preview possible. The
# auto-probe falls back to raw_hdx on firmwares that NACK 0x00100980.
SPECTRUM_COMMANDS: dict[str, int] = {
    "metadata":   MSG_GET_SPECTRUM_META,
    "raw_hdx":    MSG_GET_SPECTRUM_RAW,
    "buffered":   MSG_GET_SPECTRUM_BUF,
    "raw_legacy": MSG_GET_SPECTRUM_RAW2,
}
SPECTRUM_PROBE_ORDER = ["metadata", "raw_hdx", "buffered", "raw_legacy"]

# Frame geometry. The metadata frame is [64 B header][pixels][4 B trailer]; the
# header is stripped via the leading-metadata path in _read_spectrum, and the
# true per-frame integration time lives at SPECTRUM_META_ITIME_OFFSET (uint32 µs).
# Verified on HDX00512: header[2]=64 (block len), header[4]=pixel-byte-count,
# header[16]=integration time µs (tracks SET_ITIME cleanly, step not ramp).
SPECTRUM_BYTES_PER_PIXEL   = 2        # uint16 little-endian
SPECTRUM_META_BYTES        = 64       # leading metadata header on the metadata frame
SPECTRUM_META_ITIME_OFFSET = 16       # uint32 µs: true integration time of THIS frame

# Sanity caps for the calibration coefficient counts read from flash.  Values
# above these indicate a pipeline desync (we read a spectrum frame instead of a
# count byte) and are clamped rather than trusted.
WL_COEFF_COUNT_CAP   = 10
NL_COEFF_COUNT_CAP   = 8
# float32 magnitude below which a coefficient slot is treated as uninitialised
# flash padding (and reading stops there).
NL_PADDING_EPSILON   = 1e-38

# ══════════════════════════════════════════════════════════════════════════
# 2.  CONFIG KEYS + DEFAULTS  (every tunable lives here — no inline magic
#     numbers).  Values are read from spectrometer_config.json via Config.get,
#     falling back to these defaults when a key is absent.
# ══════════════════════════════════════════════════════════════════════════

# Config key                                              Default        Meaning
CFG_PIXEL_COUNT         = "ocean_pixel_count";            DEF_PIXEL_COUNT        = 2068
CFG_WL_FROM_DEVICE      = "ocean_use_device_wavelength";  DEF_WL_FROM_DEVICE     = True
CFG_WL_OFFSET_NM        = "ocean_wl_offset_nm";           DEF_WL_OFFSET_NM       = 0.0
CFG_WL_FALLBACK_MIN     = "ocean_wl_fallback_min_nm";     DEF_WL_FALLBACK_MIN    = 200.0
CFG_WL_FALLBACK_MAX     = "ocean_wl_fallback_max_nm";     DEF_WL_FALLBACK_MAX    = 1100.0
CFG_NL_CORRECTION       = "ocean_nonlinearity_correction"; DEF_NL_CORRECTION     = True
CFG_DARK_PIXELS         = "ocean_dark_estimate_pixels";   DEF_DARK_PIXELS        = 50
CFG_ADC_SATURATION      = "ocean_adc_saturation";         DEF_ADC_SATURATION     = 60000
CFG_AE_TARGET_ADC       = "ocean_ae_target_adc";          DEF_AE_TARGET_ADC      = 50000
CFG_AE_DEADZONE         = "ocean_ae_deadzone";            DEF_AE_DEADZONE        = 2000
CFG_TRIGGER_MODE        = "ocean_trigger_mode";           DEF_TRIGGER_MODE       = 0
CFG_MIN_ITIME_US        = "ocean_min_integration_us";     DEF_MIN_ITIME_US       = 6_000
CFG_MAX_ITIME_US        = "ocean_max_integration_us";     DEF_MAX_ITIME_US       = 10_000_000
CFG_SPECTRUM_COMMAND    = "ocean_spectrum_command";       DEF_SPECTRUM_COMMAND   = "metadata"
CFG_USB_TIMEOUT_MS      = "ocean_usb_timeout_ms";         DEF_USB_TIMEOUT_MS     = 15_000
CFG_FLUSH_TIMEOUT_MS    = "ocean_flush_timeout_ms";       DEF_FLUSH_TIMEOUT_MS   = 50
CFG_LOOP_SLEEP_FACTOR   = "ocean_loop_sleep_factor";      DEF_LOOP_SLEEP_FACTOR  = 0.5
CFG_LOOP_SLEEP_MIN_S    = "ocean_loop_sleep_min_s";       DEF_LOOP_SLEEP_MIN_S   = 0.005
CFG_LOOP_SLEEP_MAX_S    = "ocean_loop_sleep_max_s";       DEF_LOOP_SLEEP_MAX_S   = 0.20
# Fast Preview throttle: a small FIXED yield instead of the t_short-proportional
# sleep above. FP_DEBUG showed the proportional sleep is pure dead time in the
# short-exposure FP regime (0 repeats, ~25 ms host floor); a tiny yield keeps the
# thread from a 100 % busy-spin while letting device frame production set the
# cadence. Standard/AE streaming still uses the proportional sleep.
CFG_FP_LOOP_SLEEP_S     = "ocean_fp_loop_sleep_s";        DEF_FP_LOOP_SLEEP_S    = 0.001
CFG_DISCARD_AFTER_ITIME = "ocean_discard_frames_after_itime"; DEF_DISCARD_AFTER_ITIME = 1
CFG_FP_LONG_REFRESH_S   = "ocean_fp_long_refresh_s";      DEF_FP_LONG_REFRESH_S  = 3.0

# Max frames discarded after an exposure change while the per-frame metadata
# settles into the target regime (stale -1 frame + the one in-flight
# previous-exposure frame). The trigger-mode-0 probe settles in ~2-3 frames;
# this bounds the loop so a no-metadata firmware or dark sample can't stall it.
FP_SETTLE_MAX_FRAMES = 8
# Optional spectral-response table for response compensation.  A list of
# [wavelength_nm, relative_sensitivity] pairs (sensitivity normalised to 1.0 at
# peak).  None / empty → no response correction (GUI gain stays 1.0).  Populate
# it from sensitivity_calibration.py output to enable response compensation.
CFG_RESPONSE_TABLE      = "ocean_response_table";         DEF_RESPONSE_TABLE     = None

# Thread pause poll interval while measurement is paused.
THREAD_PAUSE_SLEEP_SEC = 0.05


# ══════════════════════════════════════════════════════════════════════════
# 3.  OBP PACKET BUILD / PARSE  (ported verbatim from the validated diagnostic)
# ══════════════════════════════════════════════════════════════════════════

def _obp_pack(msg_type: int, payload: bytes = b"", request_ack: bool = False) -> bytes:
    """Build an OBP request frame for ``msg_type`` carrying ``payload``."""
    flags = OBP_FLAG_REQUEST_ACK if request_ack else 0

    if len(payload) <= OBP_IMMEDIATE_MAX:
        # Small payloads ride inside the 16-byte immediate-data field.
        imm_len  = len(payload)
        imm_data = payload + b"\x00" * (OBP_IMMEDIATE_MAX - len(payload))
        ext      = b""
        brem     = OBP_FOOTER_SZ
    else:
        # Larger payloads are appended after the header as an extended block.
        imm_len  = 0
        imm_data = b"\x00" * OBP_IMMEDIATE_MAX
        ext      = payload
        brem     = len(ext) + OBP_FOOTER_SZ

    hdr = struct.pack(
        "<HHHHLL6sBB16sL",
        OBP_START, OBP_PROTOCOL, flags, 0,
        msg_type, 0, b"\x00" * 6, 0, imm_len, imm_data, brem,
    )
    ftr = struct.pack("<16sL", b"\x00" * 16, OBP_END)
    return hdr + ext + ftr


def _obp_unpack(data: bytes) -> tuple[int, bytes, int]:
    """Parse an OBP response → (msg_type, payload_bytes, raw_flags).

    Raises RuntimeError if the device set the NACK flag.
    """
    if len(data) < OBP_HEADER_SZ + OBP_FOOTER_SZ:
        raise ValueError(f"OBP response too short: {len(data)} B")

    (start, proto, flags, error, msg_type, _reg,
     _res, _cs, imm_len, imm_data, bytes_rem,
     ) = struct.unpack_from("<HHHHLL6sBB16sL", data, 0)

    if flags & OBP_FLAG_NACK:
        raise RuntimeError(f"Device NACK (error=0x{error:04X})")

    ext_len = max(0, bytes_rem - OBP_FOOTER_SZ)
    if imm_len > 0:
        payload = bytes(imm_data[:imm_len])
    elif ext_len > 0:
        payload = bytes(data[OBP_HEADER_SZ: OBP_HEADER_SZ + ext_len])
    else:
        payload = b""
    return msg_type, payload, flags


# ══════════════════════════════════════════════════════════════════════════
# 4.  BACKEND CLASS
# ══════════════════════════════════════════════════════════════════════════

class OceanHDX(BaseSpectrometer):
    """Ocean Optics HDX-UV-VIS spectrometer over raw OBP / libusb."""

    # Defaults; overwritten from the device at connect() time.
    PIXEL_COUNT          = DEF_PIXEL_COUNT
    WL_MIN_NM            = 346.0
    WL_MAX_NM            = 929.0
    SUPPORTS_OB          = False          # no optical-black pixels in raw mode
    RESPONSE_TABLE       = None           # filled from config if a table is provided
    SUPPORTS_FAST_PREVIEW = True          # FP: short exposure (trigger mode 0) + scaling

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self._dev = None
        self._thread: threading.Thread | None = None
        self._running = False
        self._lock = threading.Lock()

        # Fast-preview state. Simple model: FP only changes the hardware
        # exposure to short_pct of the user value and scales the net back up;
        # there is no long/short cadence, so no counters or references here.
        self.fast_preview_enabled   = False
        self.fast_preview_short_pct = 8
        self.fast_preview_n_short   = 4   # deprecated/no-op (kept for compat)
        self._fp_frame_counter      = 0   # deprecated/no-op (kept for compat)

        # FP_DEBUG instrumentation (ONLY touched while FP_DEBUG=1). Lets us
        # measure the TRUE new-data rate vs the 1/t_short ceiling so the
        # buffered / high-speed milestone can be judged against a real bench
        # baseline instead of guessed. A re-read of an unchanged buffered frame
        # is bit-identical to its predecessor (two genuine integrations never
        # are, due to shot noise), so a payload hash cleanly flags "new vs
        # repeat". new_times is a short sliding window of new-frame stamps.
        self._fpdbg_last_t   = 0.0
        self._fpdbg_prev_sig = None
        self._fpdbg_new_times: list[float] = []

        # Calibration / identity state (populated at connect()).
        self._serial: str = "?"
        self._wl_coeffs: list[float] | None = None
        self._nl_coeffs: list[float] | None = None
        self._wl_base: np.ndarray = np.linspace(
            self.WL_MIN_NM, self.WL_MAX_NM, DEF_PIXEL_COUNT)
        self._n_pixels: int = DEF_PIXEL_COUNT

        # Resolved spectrum command (key + numeric msg type).
        self._spectrum_key: str = DEF_SPECTRUM_COMMAND
        self._spectrum_msg: int = SPECTRUM_COMMANDS[DEF_SPECTRUM_COMMAND]

        # True integration time (µs) parsed from the metadata frame header for
        # the most recent read; None when the active command carries no metadata.
        self._last_meta_itime_us: int | None = None

        # Cached, config-driven runtime parameters (refreshed in connect()).
        self._nl_enabled        = DEF_NL_CORRECTION
        self._n_dark_pixels     = DEF_DARK_PIXELS
        self._adc_saturation    = DEF_ADC_SATURATION
        self._ae_target_adc     = DEF_AE_TARGET_ADC
        self._ae_deadzone       = DEF_AE_DEADZONE
        self._min_itime_us      = DEF_MIN_ITIME_US
        self._max_itime_us      = DEF_MAX_ITIME_US
        self._usb_timeout_ms    = DEF_USB_TIMEOUT_MS
        self._flush_timeout_ms  = DEF_FLUSH_TIMEOUT_MS
        self._loop_sleep_factor = DEF_LOOP_SLEEP_FACTOR
        self._loop_sleep_min    = DEF_LOOP_SLEEP_MIN_S
        self._loop_sleep_max    = DEF_LOOP_SLEEP_MAX_S
        self._fp_loop_sleep_s   = DEF_FP_LOOP_SLEEP_S
        self._discard_after_itime = DEF_DISCARD_AFTER_ITIME

        # Optional override polynomial (e.g. fit from emission lines).
        self.wl_poly_coeffs: list[float] | None = None

    # ── Config snapshot ─────────────────────────────────────────────────────

    def _load_config(self) -> None:
        """Cache every tunable from Config so the hot loop never touches JSON."""
        self._spectrum_key      = str(Config.get(CFG_SPECTRUM_COMMAND, DEF_SPECTRUM_COMMAND))
        self._spectrum_msg      = SPECTRUM_COMMANDS.get(
            self._spectrum_key, MSG_GET_SPECTRUM_RAW)
        self._nl_enabled        = bool(Config.get(CFG_NL_CORRECTION, DEF_NL_CORRECTION))
        self._n_dark_pixels     = int(Config.get(CFG_DARK_PIXELS, DEF_DARK_PIXELS))
        self._adc_saturation    = float(Config.get(CFG_ADC_SATURATION, DEF_ADC_SATURATION))
        self._ae_target_adc     = float(Config.get(CFG_AE_TARGET_ADC, DEF_AE_TARGET_ADC))
        self._ae_deadzone       = float(Config.get(CFG_AE_DEADZONE, DEF_AE_DEADZONE))
        self._min_itime_us      = int(Config.get(CFG_MIN_ITIME_US, DEF_MIN_ITIME_US))
        self._max_itime_us      = int(Config.get(CFG_MAX_ITIME_US, DEF_MAX_ITIME_US))
        # Expose the resolved bounds so the GUI / web server size their exposure
        # control to this device (clamped below by the global sanity floor).
        self.min_integration_us = max(GLOBAL_MIN_INTEGRATION_TIME_US, self._min_itime_us)
        self.max_integration_us = self._max_itime_us
        self._usb_timeout_ms    = int(Config.get(CFG_USB_TIMEOUT_MS, DEF_USB_TIMEOUT_MS))
        self._flush_timeout_ms  = int(Config.get(CFG_FLUSH_TIMEOUT_MS, DEF_FLUSH_TIMEOUT_MS))
        self._loop_sleep_factor = float(Config.get(CFG_LOOP_SLEEP_FACTOR, DEF_LOOP_SLEEP_FACTOR))
        self._loop_sleep_min    = float(Config.get(CFG_LOOP_SLEEP_MIN_S, DEF_LOOP_SLEEP_MIN_S))
        self._loop_sleep_max    = float(Config.get(CFG_LOOP_SLEEP_MAX_S, DEF_LOOP_SLEEP_MAX_S))
        self._fp_loop_sleep_s   = float(Config.get(CFG_FP_LOOP_SLEEP_S, DEF_FP_LOOP_SLEEP_S))
        self._discard_after_itime = int(
            Config.get(CFG_DISCARD_AFTER_ITIME, DEF_DISCARD_AFTER_ITIME))
        self._fp_long_refresh_s = float(
            Config.get(CFG_FP_LONG_REFRESH_S, DEF_FP_LONG_REFRESH_S))
        self._n_pixels          = int(Config.get(CFG_PIXEL_COUNT, DEF_PIXEL_COUNT))

        # Persisted wavelength polynomial from the wavelength wizard takes
        # precedence over the device-reported coeffs in _rebuild_wavelength_axis
        # (which prefers self.wl_poly_coeffs). null = use the device axis.
        _persisted = Config.get("ocean_wl_poly_coeffs", None)
        if _persisted:
            self.wl_poly_coeffs = list(_persisted)

        # Build the optional response table from config (enables the GUI's
        # response-compensation control for this device).
        self.RESPONSE_TABLE = self._response_table_from_config()

    @staticmethod
    def _response_table_from_config() -> np.ndarray | None:
        raw = Config.get(CFG_RESPONSE_TABLE, DEF_RESPONSE_TABLE)
        if not raw:
            return None
        try:
            tbl = np.asarray(raw, dtype=float)
            if tbl.ndim == 2 and tbl.shape[1] == 2 and tbl.shape[0] >= 2:
                return tbl
            print("[HDX] ocean_response_table malformed (need Nx2) — ignoring.")
        except Exception as exc:
            print(f"[HDX] ocean_response_table parse error: {exc} — ignoring.")
        return None

    # ── Discovery ───────────────────────────────────────────────────────────

    @staticmethod
    def scan() -> list[str]:
        """Return a label for each HDX visible on the USB bus."""
        if not _USB_AVAILABLE:
            return []
        try:
            backend = usb.backend.libusb1.get_backend(
                find_library=lambda x: libusb_package.find_library(x))
            found = usb.core.find(
                find_all=True, idVendor=OCEAN_VID, idProduct=OCEAN_PID,
                backend=backend)
            out: list[str] = []
            for dev in (found or []):
                try:
                    sn = usb.util.get_string(dev, dev.iSerialNumber) if dev.iSerialNumber else ""
                except Exception:
                    sn = ""
                out.append(f"Ocean HDX {sn}" if sn else
                           f"Ocean HDX (bus {dev.bus} addr {dev.address})")
            return out
        except Exception:
            return []

    # ── Lifecycle ───────────────────────────────────────────────────────────

    def connect(self, device_id: str) -> None:
        if not _USB_AVAILABLE:
            raise RuntimeError(
                "pyusb / libusb-package not installed.\n"
                "Run:  pip install pyusb libusb-package\n"
                f"(import error: {_USB_IMPORT_ERROR})")

        self._load_config()

        # ── Open the matching HDX ────────────────────────────────────────────
        backend = usb.backend.libusb1.get_backend(
            find_library=lambda x: libusb_package.find_library(x))
        want_serial = self._serial_from_label(device_id)

        target = None
        for dev in (usb.core.find(find_all=True, idVendor=OCEAN_VID,
                                  idProduct=OCEAN_PID, backend=backend) or []):
            if want_serial:
                try:
                    sn = usb.util.get_string(dev, dev.iSerialNumber) if dev.iSerialNumber else ""
                except Exception:
                    sn = ""
                if want_serial in (sn or ""):
                    target = dev
                    break
            else:
                target = dev
                break
        if target is None:
            raise RuntimeError(
                "Ocean HDX not found. Check the USB cable and that a "
                "libusb/WinUSB driver is installed (Zadig on Windows).")

        # On Linux a kernel driver may hold the interface — detach it.
        try:
            if target.is_kernel_driver_active(OCEAN_USB_INTERFACE):
                target.detach_kernel_driver(OCEAN_USB_INTERFACE)
        except Exception:
            pass

        target.set_configuration()
        usb.util.claim_interface(target, OCEAN_USB_INTERFACE)
        self._dev = target

        try:
            self._serial = target.serial_number or "?"
        except Exception:
            self._serial = "?"
        print(f"[HDX] Connected: {self._serial}")

        # ── Initialise acquisition ───────────────────────────────────────────
        self._set_trigger_mode(int(Config.get(CFG_TRIGGER_MODE, DEF_TRIGGER_MODE)))
        self.current_integration_time_us = self._clamp_itime(
            self.current_integration_time_us)
        self._set_integration_time_hw(self.current_integration_time_us)

        # ── Read calibration from the device ─────────────────────────────────
        self._read_serial()
        self._read_wavelength_coeffs()
        self._read_nonlinearity_coeffs()

        # ── Pick the spectrum command that actually returns data ─────────────
        self._resolve_spectrum_command()

        print(f"[HDX] Range: {self.WL_MIN_NM:.1f}–{self.WL_MAX_NM:.1f} nm "
              f"({self._n_pixels} px), spectrum='{self._spectrum_key}', "
              f"nonlinearity={'on' if self._nl_enabled else 'off'}, "
              f"offset={self._wl_offset_nm():+.2f} nm")

    def start(self) -> None:
        self._running = True
        self._thread = threading.Thread(
            target=self._run, daemon=True, name="HDX-acq")
        self._thread.start()

    def stop(self) -> None:
        self._running = False
        if self._thread:
            self._thread.join(timeout=3.0)
            self._thread = None

    def disconnect(self) -> None:
        self.stop()
        if self._dev is not None:
            try:
                usb.util.release_interface(self._dev, OCEAN_USB_INTERFACE)
                usb.util.dispose_resources(self._dev)
            except Exception:
                pass
            self._dev = None

    def set_integration_time_us(self, microseconds: int) -> None:
        # Store only; the acquisition loop pushes the change to hardware between
        # frames so it never collides with an in-flight spectrum transfer.
        self.current_integration_time_us = self._clamp_itime(microseconds)

    # ── Wavelength axis ──────────────────────────────────────────────────────

    @property
    def wavelength_array(self) -> np.ndarray:
        """Per-pixel wavelength in nm, including the live config offset."""
        return self._wl_base + self._wl_offset_nm()

    @staticmethod
    def _wl_offset_nm() -> float:
        return float(Config.get(CFG_WL_OFFSET_NM, DEF_WL_OFFSET_NM))

    def _rebuild_wavelength_axis(self) -> None:
        """Recompute the base wavelength axis from the active calibration."""
        coeffs = self.wl_poly_coeffs or self._wl_coeffs
        use_device = bool(Config.get(CFG_WL_FROM_DEVICE, DEF_WL_FROM_DEVICE))
        if coeffs and use_device:
            self._wl_base = poly_wavelength_array(coeffs, self._n_pixels)
        else:
            lo = float(Config.get(CFG_WL_FALLBACK_MIN, DEF_WL_FALLBACK_MIN))
            hi = float(Config.get(CFG_WL_FALLBACK_MAX, DEF_WL_FALLBACK_MAX))
            self._wl_base = np.linspace(lo, hi, self._n_pixels)
        wl = self.wavelength_array
        self.PIXEL_COUNT = self._n_pixels
        self.WL_MIN_NM = float(wl[0])
        self.WL_MAX_NM = float(wl[-1])

    def calibrate_from_lines(
        self,
        known_wavelengths_nm: list[float],
        pixel_positions: list[float] | None = None,
        degree: int = 3,
    ) -> dict:
        """Fit a polynomial wavelength calibration from known emission lines.

        Mirrors LasertrackLR2T.calibrate_from_lines: if ``pixel_positions`` is
        omitted, positions are estimated from the current axis.  On success the
        fit is applied immediately (and can be persisted to "ocean_wl_poly").
        """
        if pixel_positions is None:
            pixel_positions = [
                wavelength_to_pixel(wl, self._wl_base)
                for wl in known_wavelengths_nm
            ]
            pairs = [(px, wl) for px, wl in zip(pixel_positions, known_wavelengths_nm)
                     if px == px]                          # drop NaNs
            pixel_positions      = [p for p, _ in pairs]
            known_wavelengths_nm = [w for _, w in pairs]

        coeffs, residuals = fit_wavelength_polynomial(
            pixel_positions, known_wavelengths_nm, degree=degree,
            pixel_count=self._n_pixels)
        self.wl_poly_coeffs = coeffs
        self._rebuild_wavelength_axis()

        result = {
            "coeffs":    coeffs,
            "residuals": residuals.tolist(),
            "max_error": float(np.max(np.abs(residuals))),
        }
        print(f"[HDX] Polynomial fit: max residual {result['max_error']:.3f} nm")
        return result

    # ══════════════════════════════════════════════════════════════════════
    #  OBP TRANSPORT
    # ══════════════════════════════════════════════════════════════════════

    def _flush_pipeline(self) -> None:
        """Drain stale IN-pipe bytes left from a prior acquisition.

        Read with a short timeout until the device stops returning data, so the
        next GET parses its own header rather than leftovers.
        """
        if self._dev is None:
            return
        while True:
            try:
                self._dev.read(OCEAN_EP_IN, OBP_READ_CHUNK,
                               timeout=self._flush_timeout_ms)
            except Exception:
                break

    def _send(self, msg_type: int, payload: bytes = b"") -> None:
        """Write a request (fire-and-forget for SET commands)."""
        with self._lock:
            self._dev.write(OCEAN_EP_OUT, _obp_pack(msg_type, payload),
                            timeout=self._usb_timeout_ms)

    def _recv(self) -> bytes:
        """Read one complete OBP response, reassembling multi-chunk transfers."""
        buf = b""
        while len(buf) < OBP_HEADER_SZ:
            buf += bytes(self._dev.read(OCEAN_EP_IN, OBP_READ_CHUNK,
                                        timeout=self._usb_timeout_ms))
        bytes_rem = struct.unpack_from("<L", buf, OBP_BYTES_REM_OFFSET)[0]
        total = OBP_HEADER_SZ + bytes_rem
        while len(buf) < total:
            want = total - len(buf)
            buf += bytes(self._dev.read(OCEAN_EP_IN, max(want, OBP_READ_CHUNK),
                                        timeout=self._usb_timeout_ms))
        return buf[:total]

    def _query(self, msg_type: int, payload: bytes = b"",
               flush: bool = True) -> bytes:
        """Send a GET and return its payload bytes (flushing stale data first)."""
        if flush:
            self._flush_pipeline()
        with self._lock:
            self._dev.write(OCEAN_EP_OUT, _obp_pack(msg_type, payload),
                            timeout=self._usb_timeout_ms)
            raw = self._recv()
        _, data, _ = _obp_unpack(raw)
        return data

    # ── SET commands ─────────────────────────────────────────────────────────

    def _set_trigger_mode(self, mode: int) -> None:
        self._send(MSG_SET_TRIG_MODE, struct.pack("<B", mode & 0xFF))

    def _set_integration_time_hw(self, us: int) -> None:
        self._send(MSG_SET_ITIME, struct.pack("<I", self._clamp_itime(us)))

    def _clamp_itime(self, us: int) -> int:
        lo = max(GLOBAL_MIN_INTEGRATION_TIME_US, self._min_itime_us)
        hi = min(GLOBAL_MAX_INTEGRATION_TIME_US, self._max_itime_us)
        return int(max(lo, min(hi, us)))

    # ── GET: identity & calibration ────────────────────────────────────────

    def _read_serial(self) -> None:
        try:
            data = self._query(MSG_GET_SERIAL_LEN)
            slen = struct.unpack_from("<B", data)[0] if data else 16
            data = self._query(MSG_GET_SERIAL)
            if data:
                self._serial = data[:slen].decode("ascii", "replace").rstrip("\x00")
        except Exception as exc:
            print(f"[HDX] serial read failed: {exc}")

    def _read_wavelength_coeffs(self) -> None:
        """Read the cubic wavelength coefficients and build the axis."""
        try:
            data = self._query(MSG_GET_WL_COEFF_COUNT)
            n = struct.unpack_from("<B", data)[0] if data else 0
            if n <= 0 or n > WL_COEFF_COUNT_CAP:
                print(f"[HDX] WL coeff count {n} invalid — using fallback axis.")
                self._wl_coeffs = None
            else:
                coeffs = []
                for i in range(n):
                    d = self._query(MSG_GET_WL_COEFF, struct.pack("<B", i))
                    coeffs.append(struct.unpack_from("<f", d)[0] if len(d) >= 4 else 0.0)
                self._wl_coeffs = coeffs
                print("[HDX] WL coeffs: " + ", ".join(f"{c:.6g}" for c in coeffs))
        except Exception as exc:
            print(f"[HDX] WL coeff read failed: {exc}")
            self._wl_coeffs = None
        self._rebuild_wavelength_axis()

    def _read_nonlinearity_coeffs(self) -> None:
        """Read the nonlinearity polynomial coefficients (NL[0..7])."""
        try:
            data = self._query(MSG_GET_NL_COEFF_COUNT)
            if not data or len(data) > 4:
                self._nl_coeffs = None
                return
            n = struct.unpack_from("<B", data)[0]
            n = min(n, NL_COEFF_COUNT_CAP)
            coeffs: list[float] = []
            for i in range(n):
                d = self._query(MSG_GET_NL_COEFF, struct.pack("<B", i))
                if len(d) < 4:
                    break
                c = struct.unpack_from("<f", d)[0]
                # Stop at the first flash-padding slot (everything past it is junk).
                if abs(c) < NL_PADDING_EPSILON and i > 0:
                    break
                coeffs.append(c)
            self._nl_coeffs = coeffs or None
            if self._nl_coeffs:
                print(f"[HDX] NL coeffs ({len(self._nl_coeffs)}): " +
                      ", ".join(f"{c:.4g}" for c in self._nl_coeffs))
        except Exception as exc:
            print(f"[HDX] NL coeff read failed: {exc}")
            self._nl_coeffs = None

    # ── GET: spectrum ────────────────────────────────────────────────────────

    def _resolve_spectrum_command(self) -> None:
        """Probe spectrum commands until one returns a frame.

        Order: "metadata" (0x00100980) FIRST — it is strictly better (same
        pixels plus the true per-frame integration time that fast-preview needs)
        and falls back automatically. Then the configured command, then the rest.
        Probing metadata first means a stale config (e.g. an older "raw_hdx")
        does not silently disable fast-preview. Firmwares without 0x00100980
        simply NACK it and fall through to raw_hdx.
        """
        preferred = ["metadata", self._spectrum_key]
        order = preferred + [k for k in SPECTRUM_PROBE_ORDER if k not in preferred]
        for key in order:
            msg = SPECTRUM_COMMANDS.get(key)
            if msg is None:
                continue
            try:
                self._flush_pipeline()
                px = self._read_spectrum(msg)
                # Success: lock in this command and the real pixel count.
                self._spectrum_key = key
                self._spectrum_msg = msg
                if len(px) != self._n_pixels:
                    self._n_pixels = len(px)
                    self._rebuild_wavelength_axis()
                has_meta = self._last_meta_itime_us is not None
                print(f"[HDX] spectrum command: {key} "
                      f"(0x{msg:08X}, metadata={'yes' if has_meta else 'no'})",
                      flush=True)
                return
            except Exception:
                continue
        raise RuntimeError(
            "No working spectrum command (metadata/raw/buffered/legacy all "
            "failed). Try increasing integration time or re-seating the USB cable.")

    def _read_spectrum(self, msg_type: int) -> np.ndarray:
        """Acquire one spectrum frame and return it as float counts."""
        with self._lock:
            self._dev.write(OCEAN_EP_OUT, _obp_pack(msg_type),
                            timeout=self._usb_timeout_ms)
            raw = self._recv()
        _, payload, _ = _obp_unpack(raw)

        min_bytes = self._n_pixels * SPECTRUM_BYTES_PER_PIXEL
        if len(payload) < min_bytes:
            # Pixel count not yet known precisely — derive it from the payload.
            self._last_meta_itime_us = None
            usable = (len(payload) // SPECTRUM_BYTES_PER_PIXEL)
            if usable < 2:
                raise RuntimeError(f"Spectrum payload too short: {len(payload)} B")
            return np.frombuffer(payload[:usable * SPECTRUM_BYTES_PER_PIXEL],
                                 dtype="<u2").astype(np.float64)

        # The metadata frame prepends a 64-byte header (raw frames don't). When
        # present, the header carries THIS frame's true integration time (µs) at
        # SPECTRUM_META_ITIME_OFFSET — the basis for correct fast-preview scaling.
        full_with_meta = min_bytes + SPECTRUM_META_BYTES
        if len(payload) >= full_with_meta:
            offset = SPECTRUM_META_BYTES
            itime = struct.unpack_from("<I", payload, SPECTRUM_META_ITIME_OFFSET)[0]
            self._last_meta_itime_us = itime or None
        else:
            offset = 0
            self._last_meta_itime_us = None
        block = payload[offset: offset + min_bytes]
        return np.frombuffer(block, dtype="<u2").astype(np.float64)

    def _linearize(self, signal: np.ndarray) -> np.ndarray:
        """Apply the nonlinearity correction to a dark-subtracted signal.

        s_linear = s / P(s),  P(s) = Σ NL[k]·sᵏ  (≈1.0 across the ADC range).
        """
        if not self._nl_enabled or not self._nl_coeffs:
            return signal
        # np.polyval wants highest-degree-first; our coeffs are lowest-first.
        poly = np.polyval(list(reversed(self._nl_coeffs)), signal)
        poly = np.where(np.abs(poly) < 1e-9, 1.0, poly)
        return signal / poly

    # ══════════════════════════════════════════════════════════════════════
    #  ACQUISITION LOOP
    # ══════════════════════════════════════════════════════════════════════

    def _run(self) -> None:
        last_hw_us   = -1   # most recent SET_ITIME sent to hardware
        last_user_us = -1   # tracks user-requested t_long (for change detection)

        while self._running:
            if self.is_measurement_paused:
                time.sleep(THREAD_PAUSE_SLEEP_SEC)
                continue
            if self._dev is None:
                break

            try:
                t_long = self.current_integration_time_us

                # ── Fast Preview (simple: short exposure + software scaling) ──
                # Proven from the trigger-mode-0 probe (Trigger_Probe_Log): after
                # SET_ITIME(t_short) the HDX returns one stale frame (metadata
                # itime = -1), the metadata then flips to t_short one frame before
                # the pixels do, and from there every read is a genuine t_short
                # frame at the short wall-time. So FP is just: set the short
                # exposure ONCE, settle past the transition using the per-frame
                # metadata, then stream + scale. No long frame, no interleave,
                # no exposure ramp (the old "ramp" was an artifact of toggling
                # the exposure every frame). Mutually exclusive with AE.
                fp = (self.fast_preview_enabled
                      and not self.is_auto_exposure_active
                      and t_long > self._min_itime_us * 2)
                t_this = (self._clamp_itime(
                              int(t_long * self.fast_preview_short_pct / 100.0))
                          if fp else t_long)

                # ── Integration-time management ─────────────────────────
                # Only act when the TARGET exposure changes: a user/AE change of
                # t_long, toggling FP, or changing short_pct. Steady streaming
                # needs no per-frame switch (that was what confused the
                # free-running pipeline before).
                user_change     = (last_hw_us == -1 or t_long != last_user_us)
                exposure_switch = (t_this != last_hw_us)
                if user_change or exposure_switch:
                    self._set_integration_time_hw(t_this)
                    last_hw_us, last_user_us = t_this, t_long
                    self._flush_pipeline()
                    # Drain the transition: discard frames whose metadata
                    # exposure is not yet in the target regime (the stale -1
                    # frame and the in-flight previous-exposure frame). The
                    # metadata leads the pixels by one frame, so once the
                    # reported exposure is in-regime we take ONE more read whose
                    # pixels have caught up. Capped so a no-metadata firmware or
                    # a dark sample can never stall the loop.
                    lo, hi = 0.5 * t_this, 2.0 * t_this
                    for _ in range(FP_SETTLE_MAX_FRAMES):
                        try:
                            self._read_spectrum(self._spectrum_msg)
                        except Exception:
                            break
                        meta = self._last_meta_itime_us or 0
                        if meta <= 0:
                            continue                      # stale (-1) frame
                        if lo <= meta <= hi:
                            try:                          # pixels catch up
                                self._read_spectrum(self._spectrum_msg)
                            except Exception:
                                pass
                            break

                # ── Read one spectrum ───────────────────────────────────
                raw = self._read_spectrum(self._spectrum_msg)

                # Pixel-count drift safety net (firmware quirk)
                if len(raw) != self._n_pixels:
                    self._n_pixels = len(raw)
                    self._rebuild_wavelength_axis()

                # ── Dark baseline (median of N darkest pixels) ──────────
                k    = max(1, min(self._n_dark_pixels, len(raw)))
                dark = float(np.median(np.sort(raw)[:k]))

                # ── Linearise (device nonlinearity polynomial) ──────────
                if self._nl_enabled and self._nl_coeffs:
                    pixel_array = self._linearize(raw - dark) + dark
                else:
                    pixel_array = raw

                forward = True

                if fp:
                    # Scale the short frame up to long-exposure-equivalent ADC.
                    # Prefer the per-frame metadata exposure (exact) over the
                    # commanded t_this; fall back to t_this if no metadata.
                    true_itime = self._last_meta_itime_us
                    eff_short  = float(true_itime) if (true_itime and true_itime > 0) \
                                 else float(t_this)
                    ratio = max(1.0, t_long / eff_short)
                    if float(np.max(pixel_array)) < self._adc_saturation * 0.92:
                        scaled = np.minimum((pixel_array - dark) * ratio,
                                            self._adc_saturation - dark)
                        pixel_array = np.maximum(0.0, scaled + dark)
                    else:
                        # Even the short exposure is clipping → bad data, drop.
                        forward = False

                # ── Auto exposure (FP off; AE and FP are mutually exclusive) ──
                if self.is_auto_exposure_active:
                    max_adc = float(np.max(pixel_array))
                    signal  = max(50.0, max_adc - dark)
                    t = t_long
                    if max_adc >= self._adc_saturation:
                        new_us = int(t * AUTO_EXP_EMERGENCY_DROP_RATIO)
                    elif abs(max_adc - self._ae_target_adc) > self._ae_deadzone:
                        r = max(AUTO_EXP_MIN_RATIO,
                                min(AUTO_EXP_MAX_RATIO,
                                    (self._ae_target_adc - dark) / signal))
                        new_us = int(t * r)
                    else:
                        new_us = t
                    new_us = self._clamp_itime(new_us)
                    if new_us != t:
                        self.set_integration_time_us(new_us)
                        if self.on_auto_exp:
                            self.on_auto_exp(new_us / 1000.0)

                # ── Forward frame to the display stage ──────────────────
                if forward:
                    if fp and os.environ.get("FP_DEBUG") == "1":
                        try:
                            # Read cadence + genuine new-data rate. `dt` is the
                            # wall-time between forwarded reads; `new` flags
                            # whether THIS read is a fresh integration (payload
                            # changed) or a re-read of the same buffered frame;
                            # `new_hz` is the measured fresh-frame rate over a
                            # short sliding window; `ceil` is the 1/t_short
                            # theoretical ceiling. new_hz ≪ ceil ⇒ there is a
                            # dead-time gap a buffered/high-speed path could
                            # recover; new_hz ≈ ceil ⇒ already at the physics
                            # limit and buffering buys nothing.
                            now   = time.perf_counter()
                            dt_ms = (now - self._fpdbg_last_t) * 1e3 \
                                    if self._fpdbg_last_t else 0.0
                            self._fpdbg_last_t = now
                            sig    = hash(raw.tobytes())
                            is_new = (sig != self._fpdbg_prev_sig)
                            self._fpdbg_prev_sig = sig
                            if is_new:
                                self._fpdbg_new_times.append(now)
                                cutoff = now - 2.0     # keep ~2 s of stamps
                                while (len(self._fpdbg_new_times) > 1
                                       and self._fpdbg_new_times[0] < cutoff):
                                    self._fpdbg_new_times.pop(0)
                            span = (self._fpdbg_new_times[-1]
                                    - self._fpdbg_new_times[0]
                                    if len(self._fpdbg_new_times) > 1 else 0.0)
                            new_hz = ((len(self._fpdbg_new_times) - 1) / span
                                      if span > 0 else 0.0)
                            ceil_hz = 1e6 / float(t_this)
                            raw_net = float(np.max(raw)) - dark
                            print(f"[hdx-fp] dt={dt_ms:6.1f}ms "
                                  f"new={'Y' if is_new else '.'} "
                                  f"new_hz={new_hz:5.1f} ceil={ceil_hz:5.1f} "
                                  f"meta_itime={self._last_meta_itime_us} "
                                  f"t_short={t_this} ratio={t_long/float(t_this):.1f} "
                                  f"raw_net={raw_net:8.1f} "
                                  f"out_net={float(np.max(pixel_array)) - dark:9.1f}",
                                  flush=True)
                        except Exception:
                            pass
                    self.on_frame(pixel_array, dark, t_long,
                                  FRAME_STANDARD, 1.0)

                # ── Throttle ────────────────────────────────────────────
                # Standard/AE streaming: sleep ∝ integration time so a long
                # exposure never busy-polls the device. Fast Preview runs in the
                # short-exposure regime (<~250 ms t_long) where that proportional
                # sleep is pure dead time — FP_DEBUG showed 0 repeats and a fixed
                # ~25 ms host floor, i.e. we are overhead-limited, not polling too
                # fast. FP therefore uses a small FIXED yield; device frame
                # production sets the real cadence.
                if fp:
                    time.sleep(self._fp_loop_sleep_s)
                else:
                    itime_s = t_this / 1_000_000.0
                    time.sleep(max(self._loop_sleep_min,
                                   min(self._loop_sleep_max,
                                       itime_s * self._loop_sleep_factor)))

            except Exception as exc:
                if self._running:
                    print(f"[HDX] Error: {exc}")
                    if self.on_connection_lost:
                        self.on_connection_lost()
                break

    # ── Helpers ──────────────────────────────────────────────────────────────

    @staticmethod
    def _serial_from_label(label: str) -> str | None:
        """Extract the serial from a scan() label like 'Ocean HDX HDX00512'."""
        if not label:
            return None
        label = label.strip()
        if label.startswith("Ocean HDX (") or "bus " in label:
            return None
        if label.startswith("Ocean HDX "):
            return label[len("Ocean HDX "):].strip() or None
        return label or None

"""
hdx_diagnostic.py  —  Full OBP diagnostic for Ocean HDX (VID=0x2457, PID=0x2003)

Probes every known OBP command, saves spectrum to CSV, plots with matplotlib.

Usage:
    python hdx_diagnostic.py
    python hdx_diagnostic.py --itime 100000   # 100 ms integration
    python hdx_diagnostic.py --out myspec.csv

Requires:
    pip install pyusb libusb-package numpy matplotlib
"""
from __future__ import annotations

import argparse
import struct
import sys
import time
import traceback
from datetime import datetime
from pathlib import Path

import numpy as np

import usb.core
import usb.util
import usb.backend.libusb1
import libusb_package

# ── OBP framing ───────────────────────────────────────────────────────────────
OBP_START        = 0xC0C1
OBP_PROTOCOL     = 0x1100
OBP_END          = 0xC2C3C4C5
FLAG_REQUEST_ACK = 0x0004
FLAG_NACK        = 0x0008
FLAG_ACK         = 0x0002
FLAG_RESPONSE    = 0x0001

HEADER_SZ        = 44
FOOTER_SZ        = 20
BYTES_REM_OFFSET = 40   # offset of 'bytes_remaining' inside header

# ── HDX USB constants ─────────────────────────────────────────────────────────
VID        = 0x2457
PID        = 0x2003
EP_OUT     = 0x01
EP_IN      = 0x81
TIMEOUT_MS = 15_000   # generous for spectrum commands

# ── HDX hardware constants ────────────────────────────────────────────────────
PIXEL_COUNT    = 2068
META_BYTES     = 64
SPECTRUM_BYTES = PIXEL_COUNT * 2 + META_BYTES   # 4200
MIN_ITIME_US   = 6_000
MAX_ITIME_US   = 10_000_000

# ── All known OBP message types ───────────────────────────────────────────────
# SET commands — fire-and-forget, NO response from device
MSG_SET_TRIG_MODE      = 0x00110110   # uint8
MSG_SET_ITIME          = 0x00110010   # uint32 µs
MSG_SET_TE_ENABLE      = 0x00420010   # uint8  (TEC on/off)
MSG_SET_TE_SETPOINT    = 0x00420011   # float32 °C

# GET commands — send + receive response
MSG_GET_SERIAL_LEN     = 0x00000101   # → uint8 (length of serial string)
MSG_GET_SERIAL         = 0x00000100   # → string (bytes)
MSG_GET_WL_COEFF_COUNT = 0x00180100   # → uint8
MSG_GET_WL_COEFF       = 0x00180101   # payload: uint8 index → float32 (NOT float64!)
MSG_GET_NL_COEFF_COUNT = 0x00181100   # → uint8
MSG_GET_NL_COEFF       = 0x00181101   # payload: uint8 index → float32
MSG_GET_TE_TEMP        = 0x00420004   # → float32 °C  (TEC temperature)
MSG_GET_SPECTRUM_BUF   = 0x00100928   # buffered spectrum with 64-byte metadata
MSG_GET_SPECTRUM_RAW   = 0x00101000   # raw spectrum (HDX variant)
MSG_GET_SPECTRUM_RAW2  = 0x00101100   # raw spectrum (older OBP)

# Multicast (rarely implemented on HDX but worth probing)
MSG_GET_MC_ENABLED     = 0x00000A80
MSG_GET_MC_GROUP_ADDR  = 0x00000A81
MSG_GET_MC_GROUP_PORT  = 0x00000A82
MSG_GET_MC_TTL         = 0x00000A83


# ── OBP packet builder ────────────────────────────────────────────────────────

def _pack(msg_type: int, payload: bytes = b"", request_ack: bool = False) -> bytes:
    flags = FLAG_REQUEST_ACK if request_ack else 0

    if len(payload) <= 16:
        imm_len  = len(payload)
        imm_data = payload + b"\x00" * (16 - len(payload))
        ext      = b""
        brem     = FOOTER_SZ
    else:
        imm_len  = 0
        imm_data = b"\x00" * 16
        ext      = payload
        brem     = len(ext) + FOOTER_SZ

    hdr = struct.pack(
        "<HHHHLL6sBB16sL",
        OBP_START, OBP_PROTOCOL, flags, 0,
        msg_type, 0, b"\x00" * 6, 0, imm_len, imm_data, brem,
    )
    ftr = struct.pack("<16sL", b"\x00" * 16, OBP_END)
    return hdr + ext + ftr


def _unpack(data: bytes) -> tuple[int, bytes, int]:
    """Returns (msg_type, payload_bytes, raw_flags)."""
    if len(data) < HEADER_SZ + FOOTER_SZ:
        raise ValueError(f"Response too short: {len(data)} B")

    (start, proto, flags, error, msg_type, _reg,
     _res, _cs, imm_len, imm_data, bytes_rem,
     ) = struct.unpack_from("<HHHHLL6sBB16sL", data, 0)

    if flags & FLAG_NACK:
        raise RuntimeError(f"Device NACK (error=0x{error:04X})")

    ext_len = max(0, bytes_rem - FOOTER_SZ)
    if imm_len > 0:
        payload = bytes(imm_data[:imm_len])
    elif ext_len > 0:
        payload = bytes(data[HEADER_SZ: HEADER_SZ + ext_len])
    else:
        payload = b""

    return msg_type, payload, flags


# ── Driver ────────────────────────────────────────────────────────────────────

class OceanHDX:

    def __init__(self) -> None:
        backend = usb.backend.libusb1.get_backend(
            find_library=lambda x: libusb_package.find_library(x)
        )
        dev = usb.core.find(idVendor=VID, idProduct=PID, backend=backend)
        if dev is None:
            raise RuntimeError("HDX not found. Check USB cable and libusb-package.")
        dev.set_configuration()
        usb.util.claim_interface(dev, 0)
        self._dev = dev
        print(f"  Connected : {dev.manufacturer} — {dev.product}")
        print(f"  S/N       : {dev.serial_number}")

    # ── transport ─────────────────────────────────────────────────────────────

    def _flush_pipeline(self, label: str = "") -> None:
        """
        Drain any stale bytes sitting in the USB IN buffer.

        Why this is necessary
        ---------------------
        SET commands (trigger mode, integration time) don't consume a
        response.  But if the device was in the middle of a spectrum
        acquisition when we connected, or if a previous test run left
        bytes in the pipe, those bytes will be read by the *next* _recv()
        call, corrupting completely unrelated GET commands.

        Strategy: read with a very short timeout in a loop until we get
        USBTimeoutError, which means the buffer is empty.
        """
        drained = 0
        while True:
            try:
                chunk = bytes(self._dev.read(EP_IN, 512, timeout=50))
                drained += len(chunk)
            except Exception:
                break
        if drained and label:
            print(f"  (flushed {drained} stale bytes before {label})")

    def _send(self, msg_type: int, payload: bytes = b"") -> None:
        """Fire-and-forget — used for all SET commands (no device response)."""
        self._dev.write(EP_OUT, _pack(msg_type, payload, request_ack=False),
                        timeout=TIMEOUT_MS)

    def _recv(self) -> bytes:
        """Read a complete OBP response, handling multi-chunk USB transfers."""
        buf = b""
        while len(buf) < HEADER_SZ:
            buf += bytes(self._dev.read(EP_IN, 512, timeout=TIMEOUT_MS))
        bytes_rem = struct.unpack_from("<L", buf, BYTES_REM_OFFSET)[0]
        total = HEADER_SZ + bytes_rem
        while len(buf) < total:
            want  = total - len(buf)
            buf  += bytes(self._dev.read(EP_IN, max(want, 512), timeout=TIMEOUT_MS))
        return buf[:total]

    def _query(self, msg_type: int, payload: bytes = b"") -> tuple[bytes, int]:
        """Send + receive. Returns (payload_bytes, raw_flags)."""
        self._send(msg_type, payload)
        raw = self._recv()
        _, data, flags = _unpack(raw)
        return data, flags

    def _try_query(self, label: str, msg_type: int,
                   payload: bytes = b"") -> bytes | None:
        """Like _query but catches exceptions; prints pass/fail. Returns payload or None."""
        try:
            data, flags = self._query(msg_type, payload)
            flag_str = []
            if flags & FLAG_RESPONSE: flag_str.append("RESP")
            if flags & FLAG_ACK:      flag_str.append("ACK")
            print(f"  ✔  {label:<40}  {len(data):4d} B  flags=[{','.join(flag_str) or '0'}]")
            return data
        except RuntimeError as e:
            print(f"  ✘  {label:<40}  NACK/error: {e}")
            return None
        except Exception as e:
            print(f"  ✘  {label:<40}  {type(e).__name__}: {e}")
            return None

    # ── SET helpers ───────────────────────────────────────────────────────────

    def set_trigger_mode(self, mode: int = 0) -> None:
        self._send(MSG_SET_TRIG_MODE, struct.pack("<B", mode))

    def set_integration_time(self, us: int) -> None:
        us = max(MIN_ITIME_US, min(MAX_ITIME_US, us))
        self._send(MSG_SET_ITIME, struct.pack("<I", us))
        self._itime_us = us

    # ── GET helpers ───────────────────────────────────────────────────────────

    def get_serial(self) -> str:
        # Flush any spectrum bytes left in the pipe from the SET commands above
        self._flush_pipeline("GET_SERIAL_LEN")
        data = self._try_query("GET_SERIAL_LEN  (0x00000101)", MSG_GET_SERIAL_LEN)
        # Payload is a single uint8 with the string length
        serial_len = struct.unpack_from("<B", data)[0] if (data and len(data) >= 1) else 16

        self._flush_pipeline("GET_SERIAL")
        data = self._try_query("GET_SERIAL      (0x00000100)", MSG_GET_SERIAL)
        if data:
            return data[:serial_len].decode("ascii", errors="replace").rstrip("\x00")
        return "?"

    def get_wavelength_coeffs(self) -> np.ndarray | None:
        self._flush_pipeline("GET_WL_COEFF_COUNT")
        data = self._try_query("GET_WL_COEFF_COUNT (0x00180100)", MSG_GET_WL_COEFF_COUNT)
        if not data or len(data) < 1:
            return None
        # Device returns exactly 1 byte: the count as uint8
        n = struct.unpack_from("<B", data)[0]
        # Sanity cap — HDX has 4 WL coefficients; >10 means pipeline desync
        if n > 10:
            print(f"     ! Coeff count {n} looks wrong (pipeline desync?). Capping at 4.")
            n = 4
        print(f"     → {n} wavelength coefficients")
        coeffs = []
        for i in range(n):
            self._flush_pipeline()
            d = self._try_query(f"  GET_WL_COEFF[{i}]  (0x00180101)",
                                MSG_GET_WL_COEFF, struct.pack("<B", i))
            if d and len(d) >= 4:
                # HDX returns float32 (4 bytes), not float64
                c = struct.unpack_from("<f", d)[0]
                coeffs.append(c)
                print(f"       coeff[{i}] = {c:.8g}")
            else:
                print(f"       coeff[{i}] — bad payload ({len(d) if d else 0} B)")
        if coeffs:
            px = np.arange(PIXEL_COUNT, dtype=float)
            wl = sum(c * px**i for i, c in enumerate(coeffs))
            print(f"     → WL range: {wl[0]:.2f} – {wl[-1]:.2f} nm")
            return wl
        return None

    def get_nonlinearity_coeffs(self) -> list[float] | None:
        self._flush_pipeline("GET_NL_COEFF_COUNT")
        data = self._try_query("GET_NL_COEFF_COUNT (0x00181100)", MSG_GET_NL_COEFF_COUNT)
        if not data or len(data) < 1:
            return None
        # Device returns 1 byte as uint8.  If we got >4 bytes it's a pipeline
        # desync — the payload would be a spectrum frame, not a count.
        if len(data) > 4:
            print(f"     ! NL count payload {len(data)} B — pipeline desync, skipping")
            return None
        n = struct.unpack_from("<B", data)[0]
        # HDX has 8 real NL coefficients; anything higher is corrupted flash padding
        if n > 8:
            print(f"     ! NL coeff count {n} > 8 — capping at 8 (rest is flash padding)")
            n = 8
        print(f"     → {n} nonlinearity coefficients")
        coeffs = []
        PADDING_SENTINEL = 1.72493e-39   # value seen in flash-uninitialized slots
        for i in range(n):
            self._flush_pipeline()
            d = self._try_query(f"  GET_NL_COEFF[{i}]  (0x00181101)",
                                MSG_GET_NL_COEFF, struct.pack("<B", i))
            if d and len(d) >= 4:
                c = struct.unpack_from("<f", d)[0]
                if abs(c) < 1e-38 and i > 0:
                    print(f"       nl_coeff[{i}] = {c:.6g}  ← flash padding, stopping")
                    break
                coeffs.append(c)
                print(f"       nl_coeff[{i}] = {c:.6g}")
        return coeffs if coeffs else None

    def get_te_temperature(self) -> float | None:
        self._flush_pipeline("GET_TE_TEMPERATURE")
        data = self._try_query("GET_TE_TEMPERATURE (0x00420004)", MSG_GET_TE_TEMP)
        if data and len(data) >= 4:
            t = struct.unpack_from("<f", data)[0]
            print(f"     → TEC temperature: {t:.2f} °C")
            return t
        return None

    def probe_multicast(self) -> None:
        self._flush_pipeline("multicast")
        cmds = [
            ("GET_MC_ENABLED    (0x00000A80)", MSG_GET_MC_ENABLED),
            ("GET_MC_GROUP_ADDR (0x00000A81)", MSG_GET_MC_GROUP_ADDR),
            ("GET_MC_GROUP_PORT (0x00000A82)", MSG_GET_MC_GROUP_PORT),
            ("GET_MC_TTL        (0x00000A83)", MSG_GET_MC_TTL),
        ]
        for label, cmd in cmds:
            self._try_query(label, cmd)

    def get_spectrum(self, msg_type: int = MSG_GET_SPECTRUM_BUF) -> np.ndarray:
        """Acquire one spectrum. msg_type selects buffered vs raw."""
        self._send(msg_type)
        buf = self._recv()
        _, payload, _ = _unpack(buf)
        if len(payload) < PIXEL_COUNT * 2:
            raise RuntimeError(f"Spectrum payload too short: {len(payload)} B "
                               f"(need {PIXEL_COUNT * 2})")
        px_offset = META_BYTES if len(payload) >= SPECTRUM_BYTES else 0
        raw = payload[px_offset: px_offset + PIXEL_COUNT * 2]
        return np.frombuffer(raw, dtype="<u2").astype(np.float64)

    def close(self) -> None:
        try:
            usb.util.release_interface(self._dev, 0)
            usb.util.dispose_resources(self._dev)
        except Exception:
            pass
        print("  Device closed.")


# ── Helpers ───────────────────────────────────────────────────────────────────

def _sep(char: str = "─", n: int = 60) -> None:
    print(char * n)

def _section(title: str) -> None:
    print()
    _sep()
    print(f"  {title}")
    _sep()


def _warmup(hdx: OceanHDX, n: int = 3, msg_type: int = MSG_GET_SPECTRUM_BUF) -> None:
    """Flush stale frames from the device pipeline."""
    for i in range(n):
        try:
            s = hdx.get_spectrum(msg_type)
            print(f"  Warmup {i+1}/{n}: max={s.max():.0f}")
        except Exception as e:
            print(f"  Warmup {i+1}/{n}: failed — {e}")


def _save_csv(wl: np.ndarray, spec: np.ndarray, path: Path) -> None:
    import csv
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["wavelength_nm", "intensity_counts"])
        for wl_i, s_i in zip(wl, spec):
            w.writerow([f"{wl_i:.4f}", f"{s_i:.1f}"])
    print(f"  Saved → {path.resolve()}")


def _plot(wl: np.ndarray, spec: np.ndarray, itime_us: int,
          saturated_px: int, dark_mean: float) -> None:
    try:
        import matplotlib
        matplotlib.use("TkAgg")   # works on most Windows setups
    except Exception:
        pass
    import matplotlib.pyplot as plt

    fig, ax = plt.subplots(figsize=(12, 5))
    ax.plot(wl, spec, lw=0.9, color="#2196F3", label="OceanHDX")

    # Saturation line
    ax.axhline(65535, color="#f44336", lw=0.8, ls="--", label="Saturation (65535)")

    # Dark estimate band
    ax.axhspan(0, dark_mean * 1.5, alpha=0.08, color="grey", label=f"Dark ~{dark_mean:.0f} cts")

    ax.set_xlabel("Wavelength (nm)")
    ax.set_ylabel("Intensity (counts, 16-bit)")
    ax.set_title(
        f"OceanHDX  —  {itime_us/1000:.1f} ms  |  "
        f"pixels={len(spec)}  |  max={spec.max():.0f}  |  "
        f"saturated={saturated_px}"
    )
    ax.legend(fontsize=8)
    ax.grid(alpha=0.3)
    ax.set_xlim(wl[0], wl[-1])
    ax.set_ylim(-200, max(spec.max() * 1.05, 5000))

    plt.tight_layout()
    plt.show()


# ── Main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    ap = argparse.ArgumentParser(description="Ocean HDX full OBP diagnostic")
    ap.add_argument("--itime", type=int, default=50_000,
                    help="Integration time in µs (default: 50000)")
    ap.add_argument("--out", default="hdx_spectrum.csv",
                    help="CSV output path (default: hdx_spectrum.csv)")
    ap.add_argument("--no-plot", action="store_true",
                    help="Skip the matplotlib plot")
    args = ap.parse_args()

    _sep("═")
    print("  Ocean HDX  —  Full OBP Diagnostic")
    print(f"  {datetime.now().strftime('%Y-%m-%d  %H:%M:%S')}")
    _sep("═")

    # ── Connect ───────────────────────────────────────────────────────────────
    _section("1 / CONNECT")
    hdx = OceanHDX()

    # ── SET commands (no-response) ────────────────────────────────────────────
    _section("2 / SET COMMANDS  (fire-and-forget, no ACK)")
    print(f"  SET_TRIG_MODE  → 0 (free-running)")
    hdx.set_trigger_mode(0)
    print(f"  SET_ITIME      → {args.itime} µs  ({args.itime/1000:.1f} ms)")
    hdx.set_integration_time(args.itime)
    print("  (SET commands send no response — silence is correct)")

    # ── GET: identification ───────────────────────────────────────────────────
    _section("3 / IDENTIFICATION")
    serial = hdx.get_serial()
    print(f"     → Serial string: '{serial}'")

    # ── GET: wavelength calibration ───────────────────────────────────────────
    _section("4 / WAVELENGTH CALIBRATION")
    wl = hdx.get_wavelength_coeffs()

    # Fallback linear axis if coefficients didn't work
    if wl is None or len(wl) != PIXEL_COUNT:
        print("  ! Using fallback linear wavelength axis (200–1100 nm)")
        wl = np.linspace(200.0, 1100.0, PIXEL_COUNT)

    # ── GET: nonlinearity coefficients ────────────────────────────────────────
    _section("5 / NONLINEARITY COEFFICIENTS")
    nl_coeffs = hdx.get_nonlinearity_coeffs()
    if nl_coeffs is None:
        print("  (Not available or not implemented on this unit)")

    # ── GET: TEC temperature ──────────────────────────────────────────────────
    _section("6 / TEC TEMPERATURE")
    te_temp = hdx.get_te_temperature()
    if te_temp is None:
        print("  (No TEC on this unit, or command not implemented)")

    # ── Multicast ─────────────────────────────────────────────────────────────
    _section("7 / MULTICAST PROBES")
    hdx.probe_multicast()

    # ── Spectrum: buffered (0x00100928) ───────────────────────────────────────
    _section("8 / SPECTRUM — BUFFERED  0x00100928")
    hdx._flush_pipeline("buffered spectrum")
    print("  Warmup (3 frames) ...")
    _warmup(hdx, n=3, msg_type=MSG_GET_SPECTRUM_BUF)
    try:
        spec_buf = hdx.get_spectrum(MSG_GET_SPECTRUM_BUF)
        print(f"  ✔  pixels={len(spec_buf)}  min={spec_buf.min():.0f}"
              f"  max={spec_buf.max():.0f}  mean={spec_buf.mean():.1f}")
    except Exception as e:
        spec_buf = None
        print(f"  ✘  {e}")

    # ── Spectrum: raw HDX (0x00101000) ────────────────────────────────────────
    _section("9 / SPECTRUM — RAW HDX   0x00101000")
    hdx._flush_pipeline("raw HDX spectrum")
    _warmup(hdx, n=2, msg_type=MSG_GET_SPECTRUM_RAW)
    try:
        spec_raw = hdx.get_spectrum(MSG_GET_SPECTRUM_RAW)
        print(f"  ✔  pixels={len(spec_raw)}  min={spec_raw.min():.0f}"
              f"  max={spec_raw.max():.0f}  mean={spec_raw.mean():.1f}")
    except Exception as e:
        spec_raw = None
        print(f"  ✘  {e}")

    # ── Spectrum: raw legacy (0x00101100) ─────────────────────────────────────
    _section("10 / SPECTRUM — RAW LEGACY 0x00101100")
    hdx._flush_pipeline("raw legacy spectrum")
    _warmup(hdx, n=2, msg_type=MSG_GET_SPECTRUM_RAW2)
    try:
        spec_raw2 = hdx.get_spectrum(MSG_GET_SPECTRUM_RAW2)
        print(f"  ✔  pixels={len(spec_raw2)}  min={spec_raw2.min():.0f}"
              f"  max={spec_raw2.max():.0f}  mean={spec_raw2.mean():.1f}")
    except Exception as e:
        spec_raw2 = None
        print(f"  ✘  {e}")

    hdx.close()

    # ── Analysis ──────────────────────────────────────────────────────────────
    # Prefer buffered spectrum; fall back in order
    # NOTE: cannot use `spec_buf or spec_raw` — that triggers numpy's
    # "truth value of array is ambiguous" error. Use explicit None checks.
    spec = spec_buf if spec_buf is not None else (
           spec_raw if spec_raw is not None else spec_raw2)
    if spec is None:
        print("\n  ✘  No spectrum could be acquired. Check device and integration time.")
        return

    _section("11 / SPECTRUM ANALYSIS")
    dark_est = float(np.median(np.sort(spec)[:50]))
    saturated = int(np.sum(spec >= 65000))
    print(f"  Pixels          : {len(spec)}")
    print(f"  Min             : {spec.min():.0f} counts")
    print(f"  Max             : {spec.max():.0f} counts")
    print(f"  Mean            : {spec.mean():.1f} counts")
    print(f"  Median (dark)   : {dark_est:.1f} counts  (50 darkest pixels)")
    print(f"  Saturated px    : {saturated}  (≥ 65000)")
    if spec.max() >= 65000:
        print("  ⚠  Signal saturated — reduce integration time")
    elif spec.max() < 500:
        print("  ⚠  Very low signal — increase integration time or check light")
    elif spec.max() > 1000:
        print("  ✔  Good signal level")

    # Peak wavelength
    peak_idx = int(np.argmax(spec))
    print(f"  Peak wavelength : {wl[peak_idx]:.1f} nm  (pixel {peak_idx})")

    # ── Compare buffered vs raw ────────────────────────────────────────────────
    if spec_buf is not None and spec_raw is not None:
        diff = spec_buf - spec_raw
        print(f"\n  Buffered vs Raw HDX difference:")
        print(f"    max |Δ| = {np.abs(diff).max():.1f} counts")
        print(f"    mean Δ  = {diff.mean():.2f} counts")

    # ── Save CSV ──────────────────────────────────────────────────────────────
    _section("12 / SAVE CSV")
    csv_path = Path(args.out)
    _save_csv(wl, spec, csv_path)

    # ── Plot ──────────────────────────────────────────────────────────────────
    if not args.no_plot:
        _section("13 / PLOT")
        print("  Opening matplotlib window ...")
        try:
            _plot(wl, spec, args.itime, saturated, dark_est)
        except Exception as e:
            print(f"  Plot failed: {e}")
            print("  (Install matplotlib or run with --no-plot)")

    _sep("═")
    print("  Diagnostic complete.")
    _sep("═")


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"\nFATAL: {exc}")
        traceback.print_exc()
        sys.exit(1)

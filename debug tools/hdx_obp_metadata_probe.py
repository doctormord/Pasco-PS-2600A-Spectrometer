"""
hdx_obp_metadata_probe.py  —  Gate probe for the HDX OBP fast-readout milestone

Run on the bench (HDX connected):
    python hdx_obp_metadata_probe.py
    python hdx_obp_metadata_probe.py --t1 50000 --t2 10000
    python hdx_obp_metadata_probe.py --ramp 12      # frames to capture after an itime change

WHY THIS EXISTS
---------------
The milestone "HDX real fast preview via OBP buffered/high-speed mode" assumes we
can read per-frame spectra WITH METADATA via 0x00100928 and tag each frame with its
TRUE integration time. Everything downstream depends on two facts we can only learn
from the device:

  (A) Does 0x00100928 ACK on THIS unit and return a frame?
      (The 2026-05-29 diagnostic showed it NACK 0x0002 — but it read the command
       cold, with no acquisition state set up. seabreeze uses 0x00100928 as the
       HDX's *normal* spectrum-with-metadata read, so the cold NACK is inconclusive.)

  (B) Does the metadata block carry the actual integration time per frame, and does
      that value TRACK a SET_ITIME change frame-by-frame (or ramp down slowly)?
      This is the ground truth for the "HDX ramps the exposure" finding. If the
      metadata itime is trustworthy, the real fix is: tag every frame by its true
      itime from metadata and scale by t_long / t_actual — no FX burst buffer needed.

The FX onboard burst buffer ("set buffering enabled" + "back-to-back spectra per
trigger") is documented by Ocean Insight as UNIQUE TO THE OCEAN FX, so we do not
assume the HDX implements it. A few candidate buffer-control IDs are probed at the
end, clearly marked as UNCONFIRMED — they may NACK harmlessly.

OUTPUT TO PASTE BACK
--------------------
The full console output. The key parts are:
  * section 3: does 0x00100928 ACK, and what is the payload length?
  * section 4: the metadata hex dump + which offset holds the integration time
  * section 5: the per-frame itime trace after a SET_ITIME change (ramp or step?)

OBP framing is copied verbatim from the validated _device_ocean.py / hdx_diagnostic.py.

Requires:  pip install pyusb libusb-package numpy
"""
from __future__ import annotations

import argparse
import struct
import sys
import time

import numpy as np
import usb.core
import usb.util
import usb.backend.libusb1
import libusb_package

# ── OBP framing (verbatim from the project) ───────────────────────────────────
OBP_START, OBP_PROTOCOL, OBP_END = 0xC0C1, 0x1100, 0xC2C3C4C5
FLAG_REQUEST_ACK, FLAG_NACK, FLAG_ACK, FLAG_RESPONSE = 0x0004, 0x0008, 0x0002, 0x0001
HEADER_SZ, FOOTER_SZ, BYTES_REM_OFFSET = 44, 20, 40

VID, PID, EP_OUT, EP_IN, TIMEOUT_MS = 0x2457, 0x2003, 0x01, 0x81, 15_000
PIXEL_COUNT = 2068

# ── Confirmed message IDs (from the working project driver) ───────────────────
MSG_SET_TRIG_MODE    = 0x00110110   # uint8
MSG_SET_ITIME        = 0x00110010   # uint32 µs
MSG_GET_SPECTRUM_BUF = 0x00100928   # spectrum WITH metadata  ← the one to verify
MSG_GET_SPECTRUM_RAW = 0x00101000   # raw HDX (known-good fallback, no metadata)

# ── UNCONFIRMED candidate FX-buffer control IDs (probed, not trusted) ─────────
# These are best-guess OBP "data buffer" IDs from the FX family. They are only
# tried to see whether the HDX ACKs ANY of them. Do NOT promote to the driver
# until a real ACK is observed here.
CANDIDATE_BUFFER_CMDS = [
    ("SET buffering enable=1 (cand 0x00100410)", 0x00100410, struct.pack("<B", 1)),
    ("SET back-to-back count (cand 0x00100420)", 0x00100420, struct.pack("<I", 1)),
    ("GET buffer element count (cand 0x00100400)", 0x00100400, b""),
    ("CLEAR buffer (cand 0x00100401)", 0x00100401, b""),
]


def _pack(msg_type: int, payload: bytes = b"") -> bytes:
    if len(payload) <= 16:
        imm_len, imm_data, ext, brem = len(payload), payload + b"\x00" * (16 - len(payload)), b"", FOOTER_SZ
    else:
        imm_len, imm_data, ext, brem = 0, b"\x00" * 16, payload, len(payload) + FOOTER_SZ
    hdr = struct.pack("<HHHHLL6sBB16sL", OBP_START, OBP_PROTOCOL, 0, 0,
                      msg_type, 0, b"\x00" * 6, 0, imm_len, imm_data, brem)
    return hdr + ext + struct.pack("<16sL", b"\x00" * 16, OBP_END)


def _unpack(data: bytes) -> tuple[int, bytes, int]:
    if len(data) < HEADER_SZ + FOOTER_SZ:
        raise ValueError(f"response too short: {len(data)} B")
    (_s, _p, flags, error, msg_type, _r, _res, _cs, imm_len, imm_data, brem
     ) = struct.unpack_from("<HHHHLL6sBB16sL", data, 0)
    if flags & FLAG_NACK:
        raise RuntimeError(f"NACK (error=0x{error:04X})")
    ext_len = max(0, brem - FOOTER_SZ)
    if imm_len > 0:
        payload = bytes(imm_data[:imm_len])
    elif ext_len > 0:
        payload = bytes(data[HEADER_SZ:HEADER_SZ + ext_len])
    else:
        payload = b""
    return msg_type, payload, flags


class HDX:
    def __init__(self) -> None:
        backend = usb.backend.libusb1.get_backend(
            find_library=lambda x: libusb_package.find_library(x))
        dev = usb.core.find(idVendor=VID, idProduct=PID, backend=backend)
        if dev is None:
            raise RuntimeError("HDX not found (cable / libusb / Zadig).")
        dev.set_configuration()
        usb.util.claim_interface(dev, 0)
        self._dev = dev
        print(f"  Connected: {dev.manufacturer} — {dev.product}  S/N {dev.serial_number}")

    def flush(self) -> None:
        while True:
            try:
                self._dev.read(EP_IN, 512, timeout=50)
            except Exception:
                break

    def send(self, msg: int, payload: bytes = b"") -> None:
        self._dev.write(EP_OUT, _pack(msg, payload), timeout=TIMEOUT_MS)

    def recv(self) -> bytes:
        buf = b""
        while len(buf) < HEADER_SZ:
            buf += bytes(self._dev.read(EP_IN, 512, timeout=TIMEOUT_MS))
        total = HEADER_SZ + struct.unpack_from("<L", buf, BYTES_REM_OFFSET)[0]
        while len(buf) < total:
            buf += bytes(self._dev.read(EP_IN, max(total - len(buf), 512), timeout=TIMEOUT_MS))
        return buf[:total]

    def query_raw(self, msg: int, payload: bytes = b"") -> bytes:
        """Return the raw payload of a GET (no metadata stripping)."""
        self.flush()
        self.send(msg, payload)
        _, payload_out, _ = _unpack(self.recv())
        return payload_out

    def set_trigger_mode(self, mode: int) -> None:
        self.send(MSG_SET_TRIG_MODE, struct.pack("<B", mode & 0xFF))

    def set_itime(self, us: int) -> None:
        self.send(MSG_SET_ITIME, struct.pack("<I", int(us)))

    def close(self) -> None:
        try:
            usb.util.release_interface(self._dev, 0)
            usb.util.dispose_resources(self._dev)
        except Exception:
            pass


def _sec(t: str) -> None:
    print("\n" + "-" * 64 + f"\n  {t}\n" + "-" * 64)


def _scan_for_value(meta: bytes, target_us: int):
    """Return list of (offset, width, value) where a LE uint matches target_us
    (exact, /1000 ms, *1000 ns) — candidates for the integration-time field."""
    hits = []
    targets = {target_us, max(1, target_us // 1000), target_us * 1000}
    for width, fmt in ((4, "<I"), (8, "<Q")):
        for off in range(0, len(meta) - width + 1):
            val = struct.unpack_from(fmt, meta, off)[0]
            if val in targets:
                hits.append((off, width, val))
    return hits


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--t1", type=int, default=50_000, help="first integration time µs")
    ap.add_argument("--t2", type=int, default=10_000, help="second integration time µs")
    ap.add_argument("--ramp", type=int, default=10, help="frames to trace after the itime change")
    args = ap.parse_args()

    _sec("1 / CONNECT + SETUP")
    hdx = HDX()
    hdx.set_trigger_mode(0)
    hdx.set_itime(args.t1)
    print(f"  trigger mode 0, integration time {args.t1} µs ({args.t1/1000:.1f} ms)")
    time.sleep(args.t1 / 1e6 + 0.05)

    _sec("2 / BASELINE — raw HDX 0x00101000 (known-good, no metadata)")
    try:
        for i in range(2):  # warmup
            hdx.query_raw(MSG_GET_SPECTRUM_RAW)
        raw = hdx.query_raw(MSG_GET_SPECTRUM_RAW)
        px = np.frombuffer(raw[:PIXEL_COUNT * 2], dtype="<u2").astype(float)
        print(f"  OK  payload={len(raw)} B  pixels={len(px)}  max={px.max():.0f}")
    except Exception as e:
        print(f"  FAIL  {e}")

    _sec("3 / TARGET — spectrum WITH metadata 0x00100928")
    meta_frame = None
    try:
        for i in range(3):  # warmup; the cold read is what NACKed in 2026-05-29
            try:
                hdx.query_raw(MSG_GET_SPECTRUM_BUF)
                print(f"  warmup {i+1}/3: OK")
            except Exception as e:
                print(f"  warmup {i+1}/3: {e}")
        meta_frame = hdx.query_raw(MSG_GET_SPECTRUM_BUF)
        extra = len(meta_frame) - PIXEL_COUNT * 2
        print(f"  OK  payload={len(meta_frame)} B  → metadata bytes ≈ {extra} "
              f"(pixels assume {PIXEL_COUNT}×2={PIXEL_COUNT*2})")
        if extra < 0:
            print("  ! payload shorter than one frame — pixel count differs or not a frame")
    except Exception as e:
        print(f"  FAIL  0x00100928 → {e}")
        print("  → If this NACKs even warm, the metadata path is unavailable on this")
        print("    unit and the milestone falls back to: no per-frame itime, FX-only.")

    _sec("4 / METADATA DECODE")
    if meta_frame and len(meta_frame) > PIXEL_COUNT * 2:
        extra = len(meta_frame) - PIXEL_COUNT * 2
        # Metadata is usually a leading block (QEPRO: 32 B). Dump both ends.
        head = meta_frame[:min(64, extra + 8)]
        print(f"  leading bytes (hex): {head.hex(' ')}")
        # Try leading-block layout first.
        head_meta = meta_frame[:extra]
        hits = _scan_for_value(head_meta, args.t1)
        if hits:
            print(f"  integration-time field candidates (value == {args.t1} µs and unit-scaled):")
            for off, width, val in hits:
                print(f"    offset {off:>3} ({width}B LE) = {val}")
        else:
            print(f"  no field in the leading {extra} B equals {args.t1} µs (exact/ms/ns).")
            print("  → metadata may be trailing, or itime not encoded; check the hex above.")
    else:
        print("  skipped (no metadata frame from section 3).")

    _sec(f"5 / RAMP TEST — change {args.t1}→{args.t2} µs, trace {args.ramp} frames")
    if meta_frame and len(meta_frame) > PIXEL_COUNT * 2:
        extra = len(meta_frame) - PIXEL_COUNT * 2
        # Use the best itime offset found; else scan all offsets each frame.
        hits = _scan_for_value(meta_frame[:extra], args.t1)
        off_w = (hits[0][0], hits[0][1]) if hits else None
        hdx.set_itime(args.t2)
        print(f"  SET_ITIME → {args.t2} µs; reading frames (raw_max | reported_itime):")
        for k in range(args.ramp):
            try:
                f = hdx.query_raw(MSG_GET_SPECTRUM_BUF)
                px = np.frombuffer(f[PIXEL_COUNT * -2:], dtype="<u2").astype(float)
                if off_w:
                    fmt = "<I" if off_w[1] == 4 else "<Q"
                    rep = struct.unpack_from(fmt, f, off_w[0])[0]
                    print(f"    frame {k:>2}: max={px.max():>6.0f}  itime={rep} µs")
                else:
                    print(f"    frame {k:>2}: max={px.max():>6.0f}  (no itime offset known)")
            except Exception as e:
                print(f"    frame {k:>2}: {e}")
        print("  → STEP (itime jumps to t2 next frame) = clean fast preview is possible.")
        print("  → RAMP (itime descends over many frames) = confirms the known problem;")
        print("    but if 'itime' is reported per frame, we can still scale each frame.")
    else:
        print("  skipped (needs a working metadata frame).")

    _sec("6 / EXPLORATORY — candidate FX buffer-control IDs (UNCONFIRMED)")
    for label, msg, payload in CANDIDATE_BUFFER_CMDS:
        try:
            out = hdx.query_raw(msg, payload)
            print(f"  ACK   {label}  → {len(out)} B")
        except Exception as e:
            print(f"  ----  {label}  → {e}")

    hdx.close()
    _sec("DONE — paste the full output back")


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"\nFATAL: {exc}")
        sys.exit(1)

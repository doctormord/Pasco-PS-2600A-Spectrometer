"""
hdx_metadata_decode.py  —  Decode the HDX spectrum-with-metadata frame 0x00100980

The full OBP sweep found that THIS HDX firmware exposes a metadata-bearing
spectrum read at 0x00100980 (4204 B = 4136 plain frame + 68 B metadata), NOT at
the FX id 0x00100928. This probe pins down the metadata layout so the driver can
tag each frame with its TRUE integration time.

Run on the bench:
    python hdx_metadata_decode.py
    python hdx_metadata_decode.py --t1 50000 --t2 10000 --ramp 14

What it does
------------
1. Reads several 0x00100980 frames and confirms a stable length.
2. Dumps the candidate metadata bytes (first and last 96 B) as hex.
3. Finds the integration-time field: scans the whole payload for a LE value
   equal to t1 (µs), then to t2 (µs); the offset that holds BOTH is the field.
4. RAMP TEST: after SET_ITIME(t2), reads N frames and prints the metadata itime
   per frame → tells us STEP (clean switch) vs RAMP (descending), now with the
   true per-frame exposure known either way.
5. Reads the buffer-control getters (0x00100800/0820/0822/0900) for context.

Paste the full output back. The key lines:
  * section 3: the itime-field offset + width
  * section 4: the per-frame itime trace (step or ramp)

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

OBP_START, OBP_PROTOCOL, OBP_END = 0xC0C1, 0x1100, 0xC2C3C4C5
FLAG_REQUEST_ACK, FLAG_NACK = 0x0004, 0x0008
HEADER_SZ, FOOTER_SZ, BYTES_REM_OFFSET = 44, 20, 40
VID, PID, EP_OUT, EP_IN, TIMEOUT_MS = 0x2457, 0x2003, 0x01, 0x81, 15_000
PIXEL_COUNT = 2068
PLAIN = PIXEL_COUNT * 2                       # 4136

MSG_SET_TRIG_MODE = 0x00110110
MSG_SET_ITIME     = 0x00110010
MSG_META          = 0x00100980                # ← the metadata frame on this firmware
MSG_RAW           = 0x00101000                # plain frame (sanity baseline)
BUFFER_GETTERS    = [0x00100800, 0x00100820, 0x00100822, 0x00100900, 0x00101010]


def _pack(msg: int, payload: bytes = b"") -> bytes:
    imm_len, imm_data, brem = len(payload), payload + b"\x00" * (16 - len(payload)), FOOTER_SZ
    hdr = struct.pack("<HHHHLL6sBB16sL", OBP_START, OBP_PROTOCOL, FLAG_REQUEST_ACK, 0,
                      msg, 0, b"\x00" * 6, 0, imm_len, imm_data, brem)
    return hdr + struct.pack("<16sL", b"\x00" * 16, OBP_END)


def _unpack(data: bytes) -> bytes:
    (_s, _p, flags, error, _msg, _reg,
     _res, _cs, imm_len, imm_data, brem
     ) = struct.unpack_from("<HHHHLL6sBB16sL", data, 0)
    if flags & FLAG_NACK:
        raise RuntimeError(f"NACK(0x{error:04X})")
    ext_len = max(0, brem - FOOTER_SZ)
    if imm_len > 0:
        return bytes(imm_data[:imm_len])
    return bytes(data[HEADER_SZ:HEADER_SZ + ext_len])


class HDX:
    def __init__(self) -> None:
        backend = usb.backend.libusb1.get_backend(
            find_library=lambda x: libusb_package.find_library(x))
        dev = usb.core.find(idVendor=VID, idProduct=PID, backend=backend)
        if dev is None:
            raise RuntimeError("HDX not found.")
        dev.set_configuration()
        usb.util.claim_interface(dev, 0)
        self._dev = dev
        print(f"  Connected: {dev.product}  S/N {dev.serial_number}")

    def flush(self) -> None:
        while True:
            try:
                self._dev.read(EP_IN, 512, timeout=40)
            except Exception:
                break

    def _recv(self) -> bytes:
        buf = b""
        while len(buf) < HEADER_SZ:
            buf += bytes(self._dev.read(EP_IN, 512, timeout=TIMEOUT_MS))
        total = HEADER_SZ + struct.unpack_from("<L", buf, BYTES_REM_OFFSET)[0]
        while len(buf) < total:
            buf += bytes(self._dev.read(EP_IN, max(total - len(buf), 512), timeout=TIMEOUT_MS))
        return buf[:total]

    def send(self, msg: int, payload: bytes = b"") -> None:
        self._dev.write(EP_OUT, _pack(msg, payload), timeout=TIMEOUT_MS)

    def read_frame(self, msg: int) -> bytes:
        self.flush()
        self.send(msg)
        return _unpack(self._recv())

    def set_trig(self, m: int) -> None:
        self.send(MSG_SET_TRIG_MODE, struct.pack("<B", m & 0xFF))

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


def _find_value(buf: bytes, target: int) -> list[tuple[int, int]]:
    """Offsets where a LE uint16/uint32/uint64 equals target exactly."""
    hits = []
    for width, fmt in ((2, "<H"), (4, "<I"), (8, "<Q")):
        for off in range(0, len(buf) - width + 1):
            if struct.unpack_from(fmt, buf, off)[0] == target:
                hits.append((off, width))
    return hits


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--t1", type=int, default=50_000)
    ap.add_argument("--t2", type=int, default=10_000)
    ap.add_argument("--ramp", type=int, default=14)
    args = ap.parse_args()

    _sec("1 / SETUP + STABLE READ of 0x00100980")
    hdx = HDX()
    hdx.set_trig(0)
    hdx.set_itime(args.t1)
    print(f"  trigger 0, itime {args.t1} µs")
    time.sleep(args.t1 / 1e6 + 0.05)
    f1 = None
    for i in range(4):
        try:
            f = hdx.read_frame(MSG_META)
            print(f"  read {i+1}: len={len(f)}")
            f1 = f
        except Exception as e:
            print(f"  read {i+1}: {e}")
    if f1 is None:
        print("  could not read 0x00100980 — aborting.")
        hdx.close()
        return
    extra = len(f1) - PLAIN
    print(f"  payload={len(f1)} B → metadata ≈ {extra} B (plain frame = {PLAIN})")

    _sec("2 / HEX DUMP (first 96 and last 96 bytes)")
    print(f"  head: {f1[:96].hex(' ')}")
    print(f"  tail: {f1[-96:].hex(' ')}")

    _sec("3 / LOCATE INTEGRATION-TIME FIELD (scan whole payload)")
    hdx.set_itime(args.t2)
    time.sleep(args.t2 / 1e6 + 0.05)
    for _ in range(3):                         # let the change settle
        f2 = hdx.read_frame(MSG_META)
    hits_t1 = set(_find_value(f1, args.t1))
    hits_t2 = set(_find_value(f2, args.t2))
    common = sorted(hits_t1 & hits_t2)
    if common:
        print(f"  integration-time field(s) holding {args.t1}→{args.t2}:")
        for off, width in common:
            print(f"    offset {off} ({width}B LE)   "
                  f"[{'leading meta' if off < extra else 'in/after pixels'}]")
        itime_off = common[0]
    else:
        itime_off = None
        print(f"  no offset held exactly {args.t1} then {args.t2}.")
        print(f"  t1-only matches: {sorted(hits_t1)}")
        print(f"  t2-only matches: {sorted(hits_t2)}")
        print("  → itime may be in ticks/other units; inspect the hex above.")

    # Known layout from the decode: 64-B leading metadata, pixels at 64, itime u32 @16.
    META_LEN, ITIME_OFF = 64, 16

    def _itime(frame: bytes) -> int:
        return struct.unpack_from("<I", frame, ITIME_OFF)[0]

    def _pmax(frame: bytes) -> float:
        return float(np.frombuffer(frame[META_LEN:META_LEN + PLAIN], dtype="<u2").max())

    _sec(f"4 / RAMP TEST — {args.t1}→{args.t2}, trace {args.ramp} frames")
    hdx.set_itime(args.t1)
    time.sleep(args.t1 / 1e6 + 0.05)
    for _ in range(3):
        hdx.read_frame(MSG_META)
    print("  baseline at t1 (expect itime=t1, steady max):")
    for k in range(2):
        f = hdx.read_frame(MSG_META)
        print(f"    base  {k}: max={_pmax(f):>6.0f}  itime={_itime(f)}")
    hdx.set_itime(args.t2)
    print(f"  SET_ITIME → {args.t2} µs; first {args.ramp} frames AFTER the change:")
    for k in range(args.ramp):
        try:
            f = hdx.read_frame(MSG_META)
            print(f"    frame {k:>2}: max={_pmax(f):>6.0f}  itime={_itime(f)}")
        except Exception as e:
            print(f"    frame {k:>2}: {e}")
    print("\n  Read it as:")
    print("   STEP  : itime & max both jump to t2 on frame 0  → read short frames directly.")
    print("   RAMP  : itime (and/or max) descend over K frames → scale each frame t_long/itime,")
    print("           but ONLY trustworthy if max tracks itime (field = true exposure).")
    print("   ECHO  : itime says t2 immediately but max stays high → field lies; metadata unusable.")

    _sec("5 / BUFFER-CONTROL GETTERS (context)")
    for g in BUFFER_GETTERS:
        try:
            d = hdx.read_frame(g)
            val = (struct.unpack_from("<I", d)[0] if len(d) >= 4
                   else d[0] if d else None)
            print(f"  0x{g:08X}  len={len(d)}  value={val}  raw={d.hex(' ')}")
        except Exception as e:
            print(f"  0x{g:08X}  {e}")

    hdx.close()
    _sec("DONE — paste the full output back")


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"\nFATAL: {exc}")
        sys.exit(1)

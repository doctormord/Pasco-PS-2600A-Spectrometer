"""
hdx_obp_command_sweep.py  —  Bounded, READ-ONLY OBP command sweep for HDX

Goal: settle empirically whether THIS HDX firmware (HDX00512) exposes ANY
spectrum/buffer read command that returns metadata — i.e. a payload longer than
one plain frame (2068 * 2 = 4136 B). If nothing in the data module does, the
"tag each frame by its true integration time" milestone is firmware-impossible
on this unit, and that's the final answer.

Run (requires explicit confirm; see SAFETY):
    python hdx_obp_command_sweep.py --confirm
    python hdx_obp_command_sweep.py --confirm --ranges 0x00100900-0x001009FF

SAFETY — read this
------------------
* The sweep ONLY touches the data/spectrum module 0x0010xxxx. It NEVER sends
  anything in the calibration module 0x0018xxxx (wavelength / nonlinearity
  coeffs) or other config modules — so it cannot corrupt your calibration.
* Requests are sent with REQUEST_ACK and an EMPTY payload. In OBP an unknown
  message replies NACK(0x0002); a known GET replies with data; a known SET
  replies ACK with no data. We only READ the reply — we do not act on anything.
* A handful of IDs in 0x0010xxxx could be runtime SET commands (e.g. "set
  buffering"); receiving them with an empty payload is benign and reset by a
  power cycle. The destructive writes (flash / calibration) live in OTHER
  modules that this sweep does not enter.
* Integration time is set to the minimum first so any working spectrum read
  returns quickly. Lamp state does not matter (reads are harmless).
* Still: this fires not-yet-known commands at the device. Run it when nothing
  critical depends on the unit's live state, and power-cycle afterwards.

What to look for in the output
------------------------------
* Any line tagged  >>> DATA  with length > 4136  → a metadata-bearing frame.
  THAT would reopen the milestone. Paste those lines back.
* Only NACK(0x0002) across the whole sweep → final confirmation that the
  metadata/buffer path does not exist on this firmware.

Requires:  pip install pyusb libusb-package numpy
"""
from __future__ import annotations

import argparse
import struct
import sys
import time

import usb.core
import usb.util
import usb.backend.libusb1
import libusb_package

# ── OBP framing (verbatim from the project) ───────────────────────────────────
OBP_START, OBP_PROTOCOL, OBP_END = 0xC0C1, 0x1100, 0xC2C3C4C5
FLAG_REQUEST_ACK, FLAG_NACK, FLAG_ACK, FLAG_RESPONSE = 0x0004, 0x0008, 0x0002, 0x0001
HEADER_SZ, FOOTER_SZ, BYTES_REM_OFFSET = 44, 20, 40

VID, PID, EP_OUT, EP_IN = 0x2457, 0x2003, 0x01, 0x81
PIXEL_COUNT = 2068
PLAIN_FRAME_BYTES = PIXEL_COUNT * 2          # 4136 — a read longer than this carries metadata

MSG_SET_TRIG_MODE = 0x00110110
MSG_SET_ITIME     = 0x00110010
MIN_ITIME_US      = 6_000

# Hard allow-list: ONLY the data module. Never the cal module 0x0018xxxx.
ALLOWED_HI = 0x0010
# Default sub-ranges to probe (inclusive). Curated neighborhoods of the known
# spectrum/buffer commands, not the whole 64k module (keeps it to a few minutes).
DEFAULT_RANGES = [
    (0x00100000, 0x001000FF),   # data/spectrum base
    (0x00100400, 0x001004FF),   # plausible buffer-control neighborhood
    (0x00100900, 0x001009FF),   # around 0x00100928 (metadata frame)
    (0x00101000, 0x001010FF),   # around the working raw-HDX read
    (0x00101100, 0x001011FF),   # legacy raw neighborhood
]


def _pack(msg_type: int, payload: bytes = b"", request_ack: bool = True) -> bytes:
    flags = FLAG_REQUEST_ACK if request_ack else 0
    imm_len, imm_data, ext, brem = len(payload), payload + b"\x00" * (16 - len(payload)), b"", FOOTER_SZ
    hdr = struct.pack("<HHHHLL6sBB16sL", OBP_START, OBP_PROTOCOL, flags, 0,
                      msg_type, 0, b"\x00" * 6, 0, imm_len, imm_data, brem)
    return hdr + ext + struct.pack("<16sL", b"\x00" * 16, OBP_END)


def _unpack(data: bytes) -> tuple[int, bytes, int]:
    (_s, _p, flags, error, msg_type, _r, _res, _cs, imm_len, imm_data, brem
     ) = struct.unpack_from("<HHHHLL6sBB16sL", data, 0)
    if flags & FLAG_NACK:
        raise RuntimeError(f"NACK(0x{error:04X})")
    ext_len = max(0, brem - FOOTER_SZ)
    payload = (bytes(imm_data[:imm_len]) if imm_len > 0
               else bytes(data[HEADER_SZ:HEADER_SZ + ext_len]) if ext_len > 0 else b"")
    return msg_type, payload, flags


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
                self._dev.read(EP_IN, 512, timeout=30)
            except Exception:
                break

    def send(self, msg: int, payload: bytes = b"", request_ack: bool = True) -> None:
        self._dev.write(EP_OUT, _pack(msg, payload, request_ack), timeout=2000)

    def probe(self, msg: int, timeout_ms: int = 350) -> tuple[str, int]:
        """Send GET-style request, read reply. Returns (status, payload_len)."""
        self.flush()
        try:
            self.send(msg)
        except Exception as e:
            return (f"WRITE_ERR({e})", 0)
        buf = b""
        try:
            while len(buf) < HEADER_SZ:
                buf += bytes(self._dev.read(EP_IN, 512, timeout=timeout_ms))
            total = HEADER_SZ + struct.unpack_from("<L", buf, BYTES_REM_OFFSET)[0]
            while len(buf) < total:
                buf += bytes(self._dev.read(EP_IN, max(total - len(buf), 512), timeout=timeout_ms))
        except Exception:
            return ("NO_REPLY", 0)        # likely a SET (fire-and-forget) or silent
        try:
            _, payload, flags = _unpack(buf[:total])
        except RuntimeError as e:
            return (str(e), 0)            # NACK(0x....)
        except Exception as e:
            return (f"PARSE_ERR({e})", 0)
        return ("ACK", len(payload))

    def close(self) -> None:
        try:
            usb.util.release_interface(self._dev, 0)
            usb.util.dispose_resources(self._dev)
        except Exception:
            pass


def _parse_ranges(spec: str | None) -> list[tuple[int, int]]:
    if not spec:
        return DEFAULT_RANGES
    out = []
    for part in spec.split(","):
        lo_s, hi_s = part.split("-")
        out.append((int(lo_s, 16), int(hi_s, 16)))
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--confirm", action="store_true",
                    help="required: acknowledge you read the SAFETY section")
    ap.add_argument("--ranges", default=None,
                    help="comma list like 0x00100900-0x001009FF (default: curated set)")
    ap.add_argument("--timeout", type=int, default=350, help="per-command USB timeout ms")
    args = ap.parse_args()

    if not args.confirm:
        print(__doc__)
        print("Refusing to run without --confirm (read the SAFETY section first).")
        return

    ranges = _parse_ranges(args.ranges)
    for lo, hi in ranges:
        if (lo >> 16) != ALLOWED_HI or (hi >> 16) != ALLOWED_HI:
            print(f"  REFUSED: range 0x{lo:08X}-0x{hi:08X} leaves the data module "
                  f"0x{ALLOWED_HI:04X}xxxx. Aborting for safety.")
            return

    total = sum(hi - lo + 1 for lo, hi in ranges)
    print(f"  Sweeping {total} IDs in {len(ranges)} range(s), data module only.\n")

    hdx = HDX()
    hdx.send(MSG_SET_TRIG_MODE, struct.pack("<B", 0))
    hdx.send(MSG_SET_ITIME, struct.pack("<I", MIN_ITIME_US))
    time.sleep(0.05)

    interesting: list[str] = []
    n = 0
    t0 = time.time()
    for lo, hi in ranges:
        for msg in range(lo, hi + 1):
            status, plen = hdx.probe(msg, timeout_ms=args.timeout)
            n += 1
            if status == "ACK":
                tag = ">>> DATA" if plen > 0 else "ACK(no-data, likely SET)"
                line = f"  0x{msg:08X}  {tag}  len={plen}"
                if plen > PLAIN_FRAME_BYTES:
                    line += "  *** LONGER THAN A PLAIN FRAME → METADATA?"
                print(line)
                interesting.append(line)
            elif status not in ("NACK(0x0002)", "NO_REPLY"):
                line = f"  0x{msg:08X}  {status}"
                print(line)
                interesting.append(line)
            if n % 256 == 0:
                print(f"    … {n}/{total}  ({time.time()-t0:.0f}s)")
    hdx.close()

    print("\n" + "-" * 64)
    print(f"  Swept {n} IDs in {time.time()-t0:.0f}s.")
    if interesting:
        print("  Non-trivial responses (paste these back):")
        for ln in interesting:
            print(ln)
    else:
        print("  Every ID returned NACK(0x0002) or no reply.")
        print("  → Final confirmation: no metadata/buffer read on this firmware.")
    print("-" * 64)


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"\nFATAL: {exc}")
        sys.exit(1)

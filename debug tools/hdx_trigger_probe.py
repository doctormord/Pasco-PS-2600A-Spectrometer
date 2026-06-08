"""
hdx_trigger_probe.py  —  Does the HDX support a deterministic software trigger?

The free-running fast-preview path (mode 0) needs flushing/draining and blocks,
because the device acquires asynchronously and buffers frames. PASCO / LR-2T are
snappy because they are request-response: one trigger → one frame at the set
exposure. This probe checks whether the HDX can do the same in trigger mode 1
(software) or 4 (single-shot).

Run on the bench:
    python hdx_trigger_probe.py

The decisive test, per mode:
  1. set integration time = 200 ms, GET once (warm up)
  2. set integration time = 20 ms
  3. immediately GET three times WITHOUT flushing, timing each and reading the
     metadata integration time the device reports.

Interpretation:
  * REQUEST-RESPONSE (what we want): the FIRST GET after the switch already
    reports meta_itime = 20000 and takes ~20 ms — the device integrated on
    demand at the new exposure. A clean trigger-based driver becomes possible.
  * FREE-RUNNING / buffered: the first GET(s) still report 200000 (stale frames
    from the queue) and/or return instantly — the mode does not give on-demand
    single-shot behaviour.
  * NACK on set-trigger-mode: the mode is not supported.

Paste the full output back.

Requires:  pip install pyusb libusb-package numpy
"""
from __future__ import annotations

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
META_ITIME_OFF = 16

MSG_SET_TRIG_MODE = 0x00110110
MSG_SET_ITIME     = 0x00110010
MSG_META          = 0x00100980


def _pack(msg: int, payload: bytes = b"", ack: bool = True) -> bytes:
    flags = FLAG_REQUEST_ACK if ack else 0
    imm_len, imm_data, brem = len(payload), payload + b"\x00" * (16 - len(payload)), FOOTER_SZ
    hdr = struct.pack("<HHHHLL6sBB16sL", OBP_START, OBP_PROTOCOL, flags, 0,
                      msg, 0, b"\x00" * 6, 0, imm_len, imm_data, brem)
    return hdr + struct.pack("<16sL", b"\x00" * 16, OBP_END)


def _unpack(data: bytes) -> bytes:
    (_s, _p, flags, error, _m, _r, _res, _cs, imm_len, imm_data, brem
     ) = struct.unpack_from("<HHHHLL6sBB16sL", data, 0)
    if flags & FLAG_NACK:
        raise RuntimeError(f"NACK(0x{error:04X})")
    ext_len = max(0, brem - FOOTER_SZ)
    return bytes(imm_data[:imm_len]) if imm_len > 0 else bytes(data[HEADER_SZ:HEADER_SZ + ext_len])


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

    def send(self, msg: int, payload: bytes = b"") -> None:
        self._dev.write(EP_OUT, _pack(msg, payload), timeout=TIMEOUT_MS)

    def get_meta(self) -> tuple[int, float]:
        """GET one metadata spectrum (no flush). Return (meta_itime_us, wall_s)."""
        t0 = time.monotonic()
        self.send(MSG_META)
        buf = b""
        while len(buf) < HEADER_SZ:
            buf += bytes(self._dev.read(EP_IN, 512, timeout=TIMEOUT_MS))
        total = HEADER_SZ + struct.unpack_from("<L", buf, BYTES_REM_OFFSET)[0]
        while len(buf) < total:
            buf += bytes(self._dev.read(EP_IN, max(total - len(buf), 512), timeout=TIMEOUT_MS))
        payload = _unpack(buf[:total])
        wall = time.monotonic() - t0
        itime = struct.unpack_from("<I", payload, META_ITIME_OFF)[0] if len(payload) >= 20 else -1
        return itime, wall

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


def main() -> None:
    hdx = HDX()
    for mode in (0, 1, 4):
        print("\n" + "-" * 60)
        print(f"  TRIGGER MODE {mode}")
        print("-" * 60)
        try:
            hdx.set_trig(mode)
        except Exception as e:
            print(f"  set_trig({mode}) -> {e}")
        time.sleep(0.05)
        # Warm up at 200 ms.
        hdx.set_itime(200_000)
        hdx.flush()
        try:
            it, w = hdx.get_meta()
            print(f"  warmup @200ms:  meta_itime={it}  wall={w*1000:.0f}ms")
        except Exception as e:
            print(f"  warmup GET -> {e}")
            continue
        # Switch to 20 ms and GET 3× WITHOUT flushing.
        hdx.set_itime(20_000)
        for i in range(3):
            try:
                it, w = hdx.get_meta()
                tag = "  <-- request-response!" if (i == 0 and it == 20000) else ""
                print(f"  after 20ms #{i}: meta_itime={it}  wall={w*1000:.0f}ms{tag}")
            except Exception as e:
                print(f"  after 20ms #{i}: {e}")
    hdx.set_trig(0)   # restore free-running
    hdx.close()
    print("\n  Done. If mode 1 or 4 shows meta_itime=20000 on '#0' with wall≈20ms,")
    print("  the HDX is request-response in that mode → clean trigger-based FP.")


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"\nFATAL: {exc}")
        sys.exit(1)

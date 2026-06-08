"""
probe_oceanhdx.py  —  SeaBreeze / OceanHDX diagnostic probe
Run with:  python probe_oceanhdx.py

Tests each layer independently so you can see exactly where detection fails.
"""

import sys

SEP  = "-" * 60
PASS = "  [OK]"
FAIL = "  [FAIL]"
INFO = "  [INFO]"


# ── 1. import checks ─────────────────────────────────────────────────────────
print(SEP)
print("STEP 1 — Python / library imports")
print(SEP)

try:
    import seabreeze
    print(f"{PASS} seabreeze imported (version: {getattr(seabreeze, '__version__', 'unknown')})")
except ImportError as e:
    print(f"{FAIL} Cannot import seabreeze: {e}")
    sys.exit(1)

try:
    import seabreeze.spectrometers as sb
    print(f"{PASS} seabreeze.spectrometers imported")
except ImportError as e:
    print(f"{FAIL} Cannot import seabreeze.spectrometers: {e}")
    sys.exit(1)

try:
    import usb.core
    import usb.backend
    print(f"{PASS} pyusb imported")
except ImportError:
    print(f"{INFO} pyusb not directly importable (may still work via seabreeze backend)")


# ── 2. backend in use ────────────────────────────────────────────────────────
print()
print(SEP)
print("STEP 2 — SeaBreeze backend")
print(SEP)

try:
    from seabreeze.backends import list_backends, use as sb_use
    backends = list_backends()
    print(f"{INFO} Available backends: {backends}")
except Exception:
    backends = []
    print(f"{INFO} Could not enumerate backends (older seabreeze version?)")

try:
    from seabreeze import _backends
    print(f"{INFO} Active backend module: {_backends}")
except Exception:
    pass


# ── 3. raw USB scan (pyusb) ──────────────────────────────────────────────────
print()
print(SEP)
print("STEP 3 — Raw USB scan for Ocean Insight VID 0x2457")
print(SEP)

OCEAN_VID = 0x2457
# Common Ocean HDX PIDs — add yours if different
KNOWN_PIDS = {
    0x4000: "Ocean HDX",
    0x101E: "USB2000+",
    0x1022: "QE65Pro",
    0x1024: "USB4000",
    0x1044: "Flame-S",
    0x2000: "STS",
    0x4200: "Ventana",
    0x4000: "Ocean HDX new",
}

try:
    import usb.core
    all_devices = list(usb.core.find(find_all=True, idVendor=OCEAN_VID))
    if not all_devices:
        print(f"{FAIL} No USB devices found with VID 0x{OCEAN_VID:04X}")
        print(f"       → Check cable, try a different USB port, verify device powers on.")
    else:
        for dev in all_devices:
            pid  = dev.idProduct
            name = KNOWN_PIDS.get(pid, "Unknown Ocean device")
            print(f"{PASS} Found: {name}  PID=0x{pid:04X}  "
                  f"Bus={dev.bus}  Address={dev.address}")
except ImportError:
    print(f"{INFO} pyusb not available — skipping raw USB scan")
except Exception as e:
    print(f"{FAIL} USB scan error: {e}")


# ── 4. seabreeze list_devices() ──────────────────────────────────────────────
print()
print(SEP)
print("STEP 4 — seabreeze.list_devices()")
print(SEP)

try:
    devices = sb.list_devices()
    if not devices:
        print(f"{FAIL} list_devices() returned an empty list.")
        print(f"       → If Step 3 found the hardware, this is a driver/permissions issue:")
        print(f"         Linux : re-run  seabreeze-os-setup  (or seabreeze --install-udev-rules)")
        print(f"                 then unplug/replug and run as your normal user (not root).")
        print(f"         macOS : no extra steps needed; check USB cable.")
        print(f"         Windows: confirm OmniDriver or libusb-win32 is installed.")
    else:
        print(f"{PASS} {len(devices)} device(s) found:")
        for i, d in enumerate(devices):
            print(f"       [{i}] {d}")
except Exception as e:
    print(f"{FAIL} list_devices() raised: {e}")
    devices = []


# ── 5. open & read the first device ─────────────────────────────────────────
print()
print(SEP)
print("STEP 5 — Open first device and read wavelengths + one spectrum")
print(SEP)

if not devices:
    print(f"{INFO} Skipped — no devices from Step 4.")
else:
    spec = None
    try:
        spec = sb.Spectrometer(devices[0])
        print(f"{PASS} Opened: {devices[0]}")

        wl = spec.wavelengths()
        print(f"{PASS} Wavelengths: {len(wl)} pixels  "
              f"[{wl[0]:.1f} – {wl[-1]:.1f} nm]")

        min_it = spec.minimum_integration_time_micros
        print(f"{INFO} Min integration time: {min_it} µs")

        spec.integration_time_micros(max(min_it, 10_000))
        intens = spec.intensities(correct_dark_counts=False,
                                  correct_nonlinearity=False)
        import numpy as np
        arr = np.array(intens)
        print(f"{PASS} Spectrum read OK — "
              f"min={arr.min():.0f}  max={arr.max():.0f}  mean={arr.mean():.0f}")

    except Exception as e:
        print(f"{FAIL} Error opening/reading device: {e}")
    finally:
        if spec:
            try:
                spec.close()
                print(f"{INFO} Device closed cleanly.")
            except Exception:
                pass


# ── 6. mirror your driver's scan() ──────────────────────────────────────────
print()
print(SEP)
print("STEP 6 — Simulate OceanHDX.scan() from your driver")
print(SEP)

try:
    result = [str(d) for d in sb.list_devices()]
    if result:
        print(f"{PASS} scan() would return: {result}")
    else:
        print(f"{FAIL} scan() returns [] — GUI will show nothing.")
        print(f"       → Root cause is in steps above; fix USB/driver layer first.")
except Exception as e:
    print(f"{FAIL} scan() simulation raised: {e}")


# ── summary ──────────────────────────────────────────────────────────────────
print()
print(SEP)
print("Done. Share the full output above to continue debugging.")
print(SEP)
print(seabreeze.__file__)

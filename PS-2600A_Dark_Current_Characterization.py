import ctypes
from ctypes import wintypes
import struct
import time
import random
import numpy as np
import csv
from datetime import datetime
import matplotlib.pyplot as plt

# ============================================================
# CONFIGURATION
# ============================================================

TEST_INTEGRATION_TIMES_US = [
    10, 50, 100, 250, 500,
    1_000, 2_000, 5_000,
    10_000, 50_000, 100_000, 500_000,
    1_000_000, 1_200_000, 1_400_000, 1_600_000, 1_800_000,
    2_000_000, 2_200_000, 2_400_000, 2_500_000, 2_600_000, 2_800_000,
    3_000_000,
]

NUM_CYCLES = 10
RANDOMIZE_ORDER_PER_CYCLE = True

# Only data points at or below this limit are used for the parametric
# linear regression. Above this the firmware compresses ADC output.
LINEAR_RANGE_CEILING_US = 2_500_000

# Optical black pixels live in header bytes 4-63 (words 2-31, 30 pixels).
# Words 0 and 1 (bytes 0-3) are always zero and are skipped.
OB_BYTE_START = 4
OB_BYTE_END   = 64
OB_PIXEL_COUNT = (OB_BYTE_END - OB_BYTE_START) // 2   # = 30

# ============================================================
# WAVELENGTH CALIBRATION
# ============================================================

c0, c1, c2, c3 = 130.755917, 0.262201464, 1.44855491e-05, -4.30320660e-09
wavelengths = np.array([c0 + c1*i + c2*(i**2) + c3*(i**3) for i in range(3648)])

PIXEL_300 = int(np.abs(wavelengths - 300).argmin())

print(f"Spectral dark region: pixels 0 to {PIXEL_300}  (up to ~300 nm)")
print(f"Optical black pixels: {OB_PIXEL_COUNT} pixels from header bytes {OB_BYTE_START}-{OB_BYTE_END}")

# ============================================================
# WINUSB SETUP
# ============================================================

setupapi = ctypes.WinDLL("setupapi", use_last_error=True)
kernel32  = ctypes.WinDLL("kernel32",  use_last_error=True)
winusb    = ctypes.WinDLL("winusb",    use_last_error=True)

class GUID(ctypes.Structure):
    _fields_ = [("Data1", wintypes.DWORD), ("Data2", wintypes.WORD),
                ("Data3", wintypes.WORD),  ("Data4", ctypes.c_ubyte * 8)]

class SP_DEVICE_INTERFACE_DATA(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.DWORD), ("InterfaceClassGuid", GUID),
                ("Flags", wintypes.DWORD),  ("Reserved", ctypes.c_void_p)]

class SP_DEVICE_INTERFACE_DETAIL_DATA_W(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.DWORD), ("DevicePath", wintypes.WCHAR * 1024)]

class WINUSB_SETUP_PACKET(ctypes.Structure):
    _fields_ = [("RequestType", ctypes.c_ubyte), ("Request",  ctypes.c_ubyte),
                ("Value",       ctypes.c_ushort), ("Index",   ctypes.c_ushort),
                ("Length",      ctypes.c_ushort)]

def guid_from_string(g):
    from uuid import UUID
    u = UUID(g)
    data4 = (ctypes.c_ubyte * 8)(*u.bytes[8:])
    return GUID(u.time_low, u.time_mid, u.time_hi_version, data4)

setupapi.SetupDiGetClassDevsW.argtypes = [ctypes.POINTER(GUID), wintypes.LPCWSTR, wintypes.HWND, wintypes.DWORD]
setupapi.SetupDiGetClassDevsW.restype  = ctypes.c_void_p
setupapi.SetupDiEnumDeviceInterfaces.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.POINTER(GUID), wintypes.DWORD, ctypes.POINTER(SP_DEVICE_INTERFACE_DATA)]
setupapi.SetupDiEnumDeviceInterfaces.restype  = wintypes.BOOL
setupapi.SetupDiGetDeviceInterfaceDetailW.argtypes = [ctypes.c_void_p, ctypes.POINTER(SP_DEVICE_INTERFACE_DATA), ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD), ctypes.c_void_p]
setupapi.SetupDiGetDeviceInterfaceDetailW.restype  = wintypes.BOOL
kernel32.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
kernel32.CreateFileW.restype  = wintypes.HANDLE
winusb.WinUsb_Initialize.argtypes    = [wintypes.HANDLE, ctypes.POINTER(ctypes.c_void_p)]
winusb.WinUsb_Initialize.restype     = wintypes.BOOL
winusb.WinUsb_ControlTransfer.argtypes = [ctypes.c_void_p, WINUSB_SETUP_PACKET, ctypes.c_void_p, wintypes.ULONG, ctypes.POINTER(wintypes.ULONG), ctypes.c_void_p]
winusb.WinUsb_ControlTransfer.restype  = wintypes.BOOL
winusb.WinUsb_ReadPipe.argtypes = [ctypes.c_void_p, ctypes.c_ubyte, ctypes.c_void_p, wintypes.ULONG, ctypes.POINTER(wintypes.ULONG), ctypes.c_void_p]
winusb.WinUsb_ReadPipe.restype  = wintypes.BOOL
winusb.WinUsb_Free.argtypes     = [ctypes.c_void_p]
kernel32.CloseHandle.argtypes   = [wintypes.HANDLE]

# ============================================================
# DEVICE ENUMERATION
# ============================================================

guid = guid_from_string("{DEE824EF-729B-4A0E-9C14-B7117D33A817}")
hdev = setupapi.SetupDiGetClassDevsW(ctypes.byref(guid), None, None, 0x02 | 0x10)

device_path = None
index = 0
while True:
    iface = SP_DEVICE_INTERFACE_DATA()
    iface.cbSize = ctypes.sizeof(iface)
    if not setupapi.SetupDiEnumDeviceInterfaces(hdev, None, ctypes.byref(guid), index, ctypes.byref(iface)):
        break
    req = wintypes.DWORD()
    setupapi.SetupDiGetDeviceInterfaceDetailW(hdev, ctypes.byref(iface), None, 0, ctypes.byref(req), None)
    buf    = ctypes.create_string_buffer(req.value)
    detail = ctypes.cast(buf, ctypes.POINTER(SP_DEVICE_INTERFACE_DETAIL_DATA_W))
    detail.contents.cbSize = 8 if ctypes.sizeof(ctypes.c_void_p) == 8 else 6
    if setupapi.SetupDiGetDeviceInterfaceDetailW(hdev, ctypes.byref(iface), detail, req, None, None):
        path = detail.contents.DevicePath
        if "vid_0945" in path.lower() and "pid_0002" in path.lower():
            device_path = path
            break
    index += 1

if not device_path:
    raise RuntimeError("PASCO PS-2600A not found.")

handle = kernel32.CreateFileW(device_path, 0x80000000 | 0x40000000, 1 | 2, None, 3, 0x40000000, None)
usb_handle = ctypes.c_void_p()
winusb.WinUsb_Initialize(handle, ctypes.byref(usb_handle))
print("Device connected.")

# ============================================================
# USB HELPERS
# ============================================================

def ctrl_out(req, value=0, index=0):
    setup       = WINUSB_SETUP_PACKET(0x40, req, value, index, 0)
    transferred = wintypes.ULONG()
    winusb.WinUsb_ControlTransfer(usb_handle, setup, None, 0, ctypes.byref(transferred), None)

def ctrl_in(req, length=1):
    setup       = WINUSB_SETUP_PACKET(0xC0, req, 0, 0, length)
    buf         = (ctypes.c_ubyte * length)()
    transferred = wintypes.ULONG()
    winusb.WinUsb_ControlTransfer(usb_handle, setup, buf, length, ctypes.byref(transferred), None)
    return bytes(buf)

def drain_pipe():
    buf  = (ctypes.c_ubyte * 28)()
    read = wintypes.ULONG()
    winusb.WinUsb_ReadPipe(usb_handle, 0x82, buf, 28, ctypes.byref(read), None)

def set_integration_time(microseconds):
    ctrl_out(2, value=microseconds & 0xFFFF, index=(microseconds >> 16) & 0xFFFF)

def read_full_frame(timeout_us):
    """
    Returns (pixel_array, ob_mean) where:
      pixel_array : np.array of 3648 uint16 spectral values
      ob_mean     : float, mean of the 30 optical black pixels from the header
    Returns (None, None) on timeout.
    """
    ctrl_out(9)
    max_polls = int((timeout_us / 1000.0 + 1000) / 2)
    for _ in range(max_polls):
        if ctrl_in(131, 1)[0] != 0:
            break
        time.sleep(0.002)
    else:
        return None, None

    buf  = (ctypes.c_ubyte * 7360)()
    read = wintypes.ULONG()
    winusb.WinUsb_ReadPipe(usb_handle, 0x82, buf, 7360, ctypes.byref(read), None)
    raw = bytes(buf)
    drain_pipe()

    # Optical black pixels from header
    ob_pixels = np.array(struct.unpack(
        f"<{OB_PIXEL_COUNT}H",
        raw[OB_BYTE_START:OB_BYTE_END]
    ), dtype=float)
    ob_mean = float(np.mean(ob_pixels))

    # Spectral pixels
    pixels = np.array(struct.unpack("<3648H", raw[64:64 + 7296]), dtype=float)

    return pixels, ob_mean

# ============================================================
# CYCLIC MEASUREMENT
# ============================================================

def run_characterization():
    accumulator = {us: {"spec": [], "ob": []} for us in TEST_INTEGRATION_TIMES_US}
    per_cycle_means = []

    print()
    print("=" * 65)
    print(f" DARK CURRENT CHARACTERIZATION  --  cyclic design")
    print(f" {NUM_CYCLES} cycles x {len(TEST_INTEGRATION_TIMES_US)} steps")
    print(f" Comparing spectral dark region vs optical black pixels")
    print("=" * 65)
    print()
    print("Cover the spectrometer input completely before proceeding.")
    print("Press Enter to start, or Ctrl+C to abort.")
    input()

    print("Warm-up pass (discarded) ...")
    for us in TEST_INTEGRATION_TIMES_US:
        set_integration_time(us)
        read_full_frame(us)
    print("Warm-up complete. Starting recorded cycles.\n")

    for cycle_idx in range(NUM_CYCLES):
        order = list(TEST_INTEGRATION_TIMES_US)
        if RANDOMIZE_ORDER_PER_CYCLE:
            random.shuffle(order)

        cycle_means = {}
        print(f"Cycle {cycle_idx + 1:>2} / {NUM_CYCLES}")

        for us in order:
            set_integration_time(us)
            read_full_frame(us)   # discard first frame after parameter change

            pixels, ob_mean = read_full_frame(us)
            if pixels is None:
                print(f"  WARNING: timeout at {us} us, skipping.")
                continue

            spec_dark = float(np.mean(pixels[0:PIXEL_300]))

            accumulator[us]["spec"].append(spec_dark)
            accumulator[us]["ob"].append(ob_mean)
            cycle_means[us] = {"spec": spec_dark, "ob": ob_mean}

        per_cycle_means.append(cycle_means)

        summary_steps = [10, 100_000, 1_000_000, 2_500_000]
        parts = []
        for s in summary_steps:
            if s in cycle_means and s in TEST_INTEGRATION_TIMES_US:
                cm = cycle_means[s]
                parts.append(f"{s // 1000 if s >= 1000 else s}{'ms' if s >= 1000 else 'us'}: "
                              f"spec={cm['spec']:.1f} ob={cm['ob']:.1f}")
        print("  " + "   ".join(parts))

    print("\nAll cycles complete.")

    results = []
    for us in TEST_INTEGRATION_TIMES_US:
        spec_samples = accumulator[us]["spec"]
        ob_samples   = accumulator[us]["ob"]
        if not spec_samples:
            continue
        results.append({
            "us":       us,
            "ms":       us / 1000.0,
            "spec_mean": float(np.mean(spec_samples)),
            "spec_std":  float(np.std(spec_samples)),
            "ob_mean":   float(np.mean(ob_samples)),
            "ob_std":    float(np.std(ob_samples)),
            "n":         len(spec_samples),
        })

    results.sort(key=lambda r: r["us"])
    return results, per_cycle_means

# ============================================================
# PARAMETRIC FIT (legacy model, now used for comparison only)
# ============================================================

def fit_parametric_model(results):
    linear = [r for r in results if r["us"] <= LINEAR_RANGE_CEILING_US]
    t_sec  = np.array([r["us"] / 1_000_000.0 for r in linear])
    adc    = np.array([r["spec_mean"]          for r in linear])
    coeffs = np.polyfit(t_sec, adc, 1)
    rate, bias = float(coeffs[0]), float(coeffs[1])
    pred   = np.polyval(coeffs, t_sec)
    ss_res = float(np.sum((adc - pred)          ** 2))
    ss_tot = float(np.sum((adc - np.mean(adc))  ** 2))
    r2     = 1.0 - ss_res / ss_tot if ss_tot > 0 else 0.0
    return bias, rate, r2

# ============================================================
# OPTICAL BLACK CORRELATION FIT
# ============================================================

def fit_ob_correlation(results):
    """
    Fit:  spec_dark = slope * ob_mean + offset
    If slope ~ 1.0 and offset ~ 0.0, the optical black pixels are a perfect
    proxy and can be used directly for per-frame dark subtraction.
    Returns (slope, offset, r_squared).
    """
    ob   = np.array([r["ob_mean"]   for r in results])
    spec = np.array([r["spec_mean"] for r in results])
    coeffs = np.polyfit(ob, spec, 1)
    slope, offset = float(coeffs[0]), float(coeffs[1])
    pred   = np.polyval(coeffs, ob)
    ss_res = float(np.sum((spec - pred)          ** 2))
    ss_tot = float(np.sum((spec - np.mean(spec)) ** 2))
    r2     = 1.0 - ss_res / ss_tot if ss_tot > 0 else 0.0
    return slope, offset, r2

# ============================================================
# OUTPUT
# ============================================================

def save_csv(results, per_cycle_means, timestamp):
    filename = f"DarkCurrent_{timestamp}_aggregated.csv"
    with open(filename, "w", newline="") as f:
        writer = csv.writer(f, delimiter=";")
        writer.writerow([
            "Integration_us", "Integration_ms",
            "SpecDark_mean", "SpecDark_std",
            "OpticalBlack_mean", "OpticalBlack_std",
            "OB_vs_Spec_delta", "N_cycles"
        ])
        for r in results:
            writer.writerow([
                r["us"], round(r["ms"], 4),
                round(r["spec_mean"], 3), round(r["spec_std"], 3),
                round(r["ob_mean"],   3), round(r["ob_std"],   3),
                round(r["spec_mean"] - r["ob_mean"], 3),
                r["n"],
            ])
    print(f"Aggregated data saved to : {filename}")

    filename_cyc = f"DarkCurrent_{timestamp}_per_cycle.csv"
    sorted_times = sorted(TEST_INTEGRATION_TIMES_US)
    with open(filename_cyc, "w", newline="") as f:
        writer = csv.writer(f, delimiter=";")
        writer.writerow(
            ["Cycle"] +
            [f"spec_{us}us" for us in sorted_times] +
            [f"ob_{us}us"   for us in sorted_times]
        )
        for idx, cm in enumerate(per_cycle_means):
            spec_row = [round(cm[us]["spec"], 3) if us in cm else float("nan") for us in sorted_times]
            ob_row   = [round(cm[us]["ob"],   3) if us in cm else float("nan") for us in sorted_times]
            writer.writerow([idx + 1] + spec_row + ob_row)
    print(f"Per-cycle data saved to  : {filename_cyc}")


def save_plots(results, per_cycle_means, bias, rate, slope, offset, ob_r2, timestamp):
    ms_x      = [r["ms"]       for r in results]
    spec_avg  = [r["spec_mean"] for r in results]
    spec_std  = [r["spec_std"]  for r in results]
    ob_avg    = [r["ob_mean"]   for r in results]
    ob_std    = [r["ob_std"]    for r in results]

    linear_r  = [r for r in results if r["us"] <= LINEAR_RANGE_CEILING_US]
    fit_ms    = [r["ms"] for r in linear_r]
    fit_adc   = [bias + rate * r["us"] / 1_000_000.0 for r in linear_r]

    fig, axes = plt.subplots(4, 1, figsize=(13, 20))

    # --- Plot 1: Spectral dark vs OB pixel mean over integration time ---
    ax = axes[0]
    ax.errorbar(ms_x, spec_avg, yerr=spec_std, fmt="o", markersize=5,
                capsize=3, color="steelblue", label="Spectral dark mean (0 to 300 nm)")
    ax.errorbar(ms_x, ob_avg, yerr=ob_std, fmt="s", markersize=5,
                capsize=3, color="darkorange", label=f"Optical black pixel mean ({OB_PIXEL_COUNT} pixels)")
    ax.plot(fit_ms, fit_adc, linestyle="-", color="red", linewidth=1.5,
            label=f"Parametric fit: Bias={bias:.2f}  Rate={rate:.2f} ADC/s")
    ax.axvline(x=LINEAR_RANGE_CEILING_US / 1000.0, color="red", linestyle=":",
               alpha=0.5, label=f"Linearity ceiling: {LINEAR_RANGE_CEILING_US // 1000} ms")
    ax.set_xscale("log")
    ax.set_title("Dark Current: Spectral region vs Optical Black pixels (log x-axis)")
    ax.set_xlabel("Integration time (ms, logarithmic)")
    ax.set_ylabel("ADC counts")
    ax.grid(True, which="both", linestyle="-", alpha=0.3)
    ax.legend(fontsize=9)

    # --- Plot 2: Delta between spectral dark and OB mean ---
    ax = axes[1]
    deltas = [r["spec_mean"] - r["ob_mean"] for r in results]
    ax.plot(ms_x, deltas, marker="o", markersize=5, color="purple")
    ax.axhline(y=np.mean(deltas), color="red", linestyle="--",
               label=f"Mean delta: {np.mean(deltas):.3f} ADC")
    ax.axhline(y=0, color="black", linestyle="-", alpha=0.3)
    ax.set_xscale("log")
    ax.set_title("Offset between Spectral dark mean and Optical Black mean\n"
                 "(flat line near zero = OB pixels are a perfect proxy)")
    ax.set_xlabel("Integration time (ms, logarithmic)")
    ax.set_ylabel("Spectral dark - OB mean (ADC)")
    ax.grid(True, which="both", linestyle="-", alpha=0.3)
    ax.legend(fontsize=9)

    # --- Plot 3: OB vs Spectral correlation scatter ---
    ax = axes[2]
    ob_all   = np.array(ob_avg)
    spec_all = np.array(spec_avg)
    fit_line = slope * ob_all + offset
    ax.scatter(ob_all, spec_all, color="steelblue", s=30, zorder=3, label="Data points")
    ax.plot(ob_all, fit_line, color="red", linewidth=2,
            label=f"Fit: spec = {slope:.4f} * OB + {offset:.3f}   R²={ob_r2:.6f}")
    ax.plot([ob_all.min(), ob_all.max()],
            [ob_all.min(), ob_all.max()],
            color="gray", linestyle="--", alpha=0.5, label="Ideal 1:1 line")
    ax.set_title("Optical Black vs Spectral Dark Correlation\n"
                 "(slope=1.0 and offset=0.0 means OB is a perfect drop-in replacement)")
    ax.set_xlabel("Optical black pixel mean (ADC)")
    ax.set_ylabel("Spectral dark region mean (ADC)")
    ax.grid(True, alpha=0.3)
    ax.legend(fontsize=9)

    # --- Plot 4: Thermal convergence ---
    ax = axes[3]
    probe_times = [t for t in [10, 100_000, 1_000_000, 2_500_000]
                   if t in TEST_INTEGRATION_TIMES_US]
    cycle_indices = list(range(1, len(per_cycle_means) + 1))
    for us in probe_times:
        series = [cm[us]["spec"] if us in cm else float("nan") for cm in per_cycle_means]
        label  = f"{us // 1000} ms" if us >= 1000 else f"{us} us"
        ax.plot(cycle_indices, series, marker="o", markersize=4, label=label)
    ax.set_title("Thermal Convergence: Spectral dark per cycle\n"
                 "(flat = sensor at equilibrium)")
    ax.set_xlabel("Cycle index")
    ax.set_ylabel("ADC counts")
    ax.grid(True)
    ax.legend(fontsize=9)

    plt.tight_layout()
    png = f"DarkCurrent_{timestamp}.png"
    plt.savefig(png, dpi=150)
    plt.close()
    print(f"Plot saved to            : {png}")


def determine_correction_strategy(slope, offset, ob_r2, mean_delta):
    """
    Based on the OB correlation fit, recommend the best correction strategy
    for the dashboard.
    """
    print()
    print("  Correction strategy recommendation:")
    print()

    if ob_r2 < 0.98:
        print("  R-squared below 0.98 — OB pixels do not reliably track the")
        print("  spectral dark region. Use the parametric model (Bias + Rate * t).")
        return "parametric"

    if abs(slope - 1.0) > 0.05:
        print(f"  Slope = {slope:.4f} (not close to 1.0) — OB pixels track the")
        print(f"  spectral region but with a gain difference. Apply:")
        print(f"    corrected = raw - ({slope:.4f} * ob_mean + {offset:.3f})")
        return "ob_scaled"

    if abs(offset) > 2.0:
        print(f"  Slope ~ 1.0 but offset = {offset:.3f} ADC — subtract OB mean")
        print(f"  plus a fixed offset:")
        print(f"    corrected = raw - (ob_mean + {offset:.3f})")
        return "ob_with_offset"

    print(f"  Slope = {slope:.4f}, offset = {offset:.3f}, R² = {ob_r2:.6f}")
    print(f"  OB pixels are a perfect proxy. Use direct subtraction:")
    print(f"    corrected = raw - ob_mean")
    print(f"  No Bias, no Rate, no integration time needed.")
    return "ob_direct"

# ============================================================
# ENTRY POINT
# ============================================================

try:
    print("Initializing sensor...")
    ctrl_out(1)

    results, per_cycle_means = run_characterization()
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")

    bias, rate, param_r2   = fit_parametric_model(results)
    slope, offset, ob_r2   = fit_ob_correlation(results)
    mean_delta             = float(np.mean([r["spec_mean"] - r["ob_mean"] for r in results]))

    save_csv(results, per_cycle_means, timestamp)
    save_plots(results, per_cycle_means, bias, rate, slope, offset, ob_r2, timestamp)

    print()
    print("=" * 65)
    print(" DARK CURRENT CHARACTERIZATION RESULTS")
    print("=" * 65)
    print()
    print(f"  Cycles completed         : {len(per_cycle_means)}")
    print(f"  Integration steps        : {len(results)}")
    print()
    print("  --- Parametric model (Bias + Rate * t) ---")
    print(f"  Bias                     : {bias:.2f} ADC")
    print(f"  Rate                     : {rate:.2f} ADC/s")
    print(f"  R-squared                : {param_r2:.6f}")
    print()
    print("  --- Optical black pixel model ---")
    print(f"  OB pixel count           : {OB_PIXEL_COUNT}")
    print(f"  Correlation slope        : {slope:.6f}  (ideal: 1.0)")
    print(f"  Correlation offset       : {offset:.4f} ADC  (ideal: 0.0)")
    print(f"  R-squared                : {ob_r2:.6f}  (ideal: 1.0)")
    print(f"  Mean delta (spec - OB)   : {mean_delta:.4f} ADC")
    print()

    strategy = determine_correction_strategy(slope, offset, ob_r2, mean_delta)

    print()
    print("  --- Dashboard config values (parametric fallback) ---")
    print()
    print(f"    DEFAULT_DARK_BIAS_ADC          = {bias:.2f}")
    print(f"    DEFAULT_DARK_RATE_ADC_PER_SEC  = {rate:.2f}")
    print()
    print("  --- Dashboard config values (optical black) ---")
    print()
    print(f"    OB_CORRECTION_SLOPE   = {slope:.6f}")
    print(f"    OB_CORRECTION_OFFSET  = {offset:.4f}")
    print()
    print("=" * 65)

except KeyboardInterrupt:
    print("\nAborted by user.")

finally:
    print("Releasing USB handles...")
    winusb.WinUsb_Free(usb_handle)
    kernel32.CloseHandle(handle)
    print("Device released cleanly.")
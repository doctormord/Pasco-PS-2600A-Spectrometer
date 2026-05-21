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

# Integration times to test, in microseconds.
TEST_INTEGRATION_TIMES_US = [
    10, 50, 100, 250, 500,
    1_000, 2_000, 5_000,
    10_000, 50_000, 100_000, 500_000,
    1_000_000, 1_200_000,1_400_000, 1_600_000, 1_800_000,
    2_000_000, 2_200_000, 2_300_00, 2_400_000, 2_500_000, 2_600_000, 2_800_000,
    3_000_000,
]

# Number of full cycles to run. Each cycle measures one frame at every
# integration time step. This spreads thermal drift evenly across all steps.
# N=20 cycles takes the same total time as before but the data is much cleaner.
NUM_CYCLES = 10

# If True, the order of integration times is shuffled independently for each
# cycle. This further decorrelates any residual drift pattern from the time
# axis. If False, the order is fixed and identical every cycle (still far
# better than the blocked design, and easier to follow in the terminal output).
RANDOMIZE_ORDER_PER_CYCLE = True

# Only data points at or below this limit are used for the linear regression.
# Above this threshold the device firmware compresses the ADC output and the
# linear dark current model no longer holds.
LINEAR_RANGE_CEILING_US = 2_500_000

# ============================================================
# WAVELENGTH CALIBRATION
# ============================================================

c0, c1, c2, c3 = 130.755917, 0.262201464, 1.44855491e-05, -4.30320660e-09
wavelengths = np.array([c0 + c1*i + c2*(i**2) + c3*(i**3) for i in range(3648)])

PIXEL_150 = int(np.abs(wavelengths - 150).argmin())
PIXEL_250 = int(np.abs(wavelengths - 250).argmin())
PIXEL_300 = int(np.abs(wavelengths - 300).argmin())

print(f"Pixel index for 150 nm: {PIXEL_150} | 250 nm: {PIXEL_250} | 300 nm: {PIXEL_300}")

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
    raise RuntimeError("PASCO PS-2600A not found. Check USB connection and WinUSB driver.")

handle = kernel32.CreateFileW(device_path, 0x80000000 | 0x40000000, 1 | 2, None, 3, 0x40000000, None)
usb_handle = ctypes.c_void_p()
winusb.WinUsb_Initialize(handle, ctypes.byref(usb_handle))

# ============================================================
# LOW-LEVEL USB HELPERS
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

def read_spectrum(timeout_us):
    ctrl_out(9)
    max_polls = int((timeout_us / 1000.0 + 1000) / 2)
    for _ in range(max_polls):
        if ctrl_in(131, 1)[0] != 0:
            break
        time.sleep(0.002)
    else:
        return None
    buf  = (ctypes.c_ubyte * 7360)()
    read = wintypes.ULONG()
    winusb.WinUsb_ReadPipe(usb_handle, 0x82, buf, 7360, ctypes.byref(read), None)
    raw = bytes(buf)
    drain_pipe()
    return np.array(struct.unpack("<3648H", raw[64:64 + 7296]))

# ============================================================
# CYCLIC MEASUREMENT ROUTINE
# ============================================================
#
# Design rationale:
#
# The blocked design (all N frames at time T1, then all N at T2, ...) confounds
# thermal drift with integration time. Short exposures are measured cold, long
# exposures are measured warm. The fitted slope then reflects both the true
# photon/dark-charge physics and the temperature rise of the sensor, making
# the extracted Rate parameter unreliable.
#
# The cyclic design measures one frame at every integration time in sequence,
# then repeats. Each time step collects its N samples distributed across the
# full duration of the session, so every step is exposed to the same range of
# thermal states. Thermal drift averages out symmetrically across all steps
# rather than accumulating on the long-exposure end.
#
# Optional per-cycle randomization of the step order decorrelates any residual
# monotonic drift that survives the cycling, at the cost of slightly more
# integration-time switching overhead.
#
# The per-cycle mean for each step is also saved separately, which lets you
# inspect the thermal convergence curve: if the sensor is still warming up
# through cycle 5 and stable by cycle 8, you can optionally discard the early
# cycles from the final fit.

def run_characterization():
    n_steps = len(TEST_INTEGRATION_TIMES_US)

    # accumulator[i] holds the list of p_avg values collected across all cycles
    # for integration time TEST_INTEGRATION_TIMES_US[i]
    accumulator = {us: [] for us in TEST_INTEGRATION_TIMES_US}

    # per_cycle_means[cycle_index][us] = mean p_avg for that step in that cycle
    per_cycle_means = []

    print()
    print("=" * 65)
    print(f" DARK CURRENT CHARACTERIZATION  --  cyclic design")
    print(f" {NUM_CYCLES} cycles x {n_steps} integration time steps")
    print(f" Randomize order per cycle: {RANDOMIZE_ORDER_PER_CYCLE}")
    print("=" * 65)
    print()
    print("Cover the spectrometer input completely before proceeding.")
    print("Press Enter to start, or Ctrl+C to abort.")
    input()

    # Warm-up: run the sensor for one full pass at mid-range exposure before
    # recording anything. This burns off the steepest part of the cold-start
    # thermal transient so the first recorded cycle is not an outlier.
    print("Warm-up pass (discarded) ...")
    for us in TEST_INTEGRATION_TIMES_US:
        set_integration_time(us)
        read_spectrum(us)
    print("Warm-up complete. Starting recorded cycles.\n")

    for cycle_idx in range(NUM_CYCLES):
        order = list(TEST_INTEGRATION_TIMES_US)
        if RANDOMIZE_ORDER_PER_CYCLE:
            random.shuffle(order)

        cycle_means = {}
        print(f"Cycle {cycle_idx + 1:>2} / {NUM_CYCLES}")

        for us in order:
            set_integration_time(us)
            # Discard one frame after switching integration time. The device
            # may have buffered a partial acquisition at the previous setting.
            read_spectrum(us)

            data = read_spectrum(us)
            if data is None:
                print(f"  WARNING: timeout at {us} us in cycle {cycle_idx + 1}, skipping.")
                continue

            p_avg = float(np.mean(data[0:PIXEL_300]))
            accumulator[us].append(p_avg)
            cycle_means[us] = p_avg

        per_cycle_means.append(cycle_means)

        # Print a one-line summary for this cycle using a few representative steps
        summary_steps = [10, 100_000, 1_000_000, 2_500_000]
        summary_str   = "  "
        for s in summary_steps:
            if s in cycle_means and s in TEST_INTEGRATION_TIMES_US:
                summary_str += f"{s // 1000:>5} ms: {cycle_means[s]:>6.1f}   "
        print(summary_str)

    print()
    print("All cycles complete.")

    # Build the aggregated result table (mean and std across cycles)
    results = []
    for us in TEST_INTEGRATION_TIMES_US:
        samples = accumulator[us]
        if not samples:
            continue
        results.append({
            "us":     us,
            "ms":     us / 1000.0,
            "p_avg":  float(np.mean(samples)),
            "p_std":  float(np.std(samples)),
            "n":      len(samples),
        })
        
        results.sort(key=lambda r: r["us"])

    return results, per_cycle_means

# ============================================================
# LINEAR FIT
# ============================================================

def fit_dark_model(results):
    """
    Fit  ADC(t) = Bias + Rate * t_seconds  using least squares.
    Only points within the confirmed linear range are included.
    Returns (bias, rate_per_second, r_squared).
    """
    linear = [r for r in results if r["us"] <= LINEAR_RANGE_CEILING_US]

    t_sec = np.array([r["us"] / 1_000_000.0 for r in linear])
    adc   = np.array([r["p_avg"]             for r in linear])

    coeffs       = np.polyfit(t_sec, adc, 1)
    rate         = float(coeffs[0])
    bias         = float(coeffs[1])

    predicted    = np.polyval(coeffs, t_sec)
    ss_res       = float(np.sum((adc - predicted)     ** 2))
    ss_tot       = float(np.sum((adc - np.mean(adc))  ** 2))
    r_squared    = 1.0 - ss_res / ss_tot if ss_tot > 0 else 0.0

    return bias, rate, r_squared

# ============================================================
# OUTPUT
# ============================================================

def save_csv(results, per_cycle_means, timestamp):
    # Main results
    filename_main = f"DarkCurrent_{timestamp}_aggregated.csv"
    with open(filename_main, "w", newline="") as f:
        writer = csv.writer(f, delimiter=";")
        writer.writerow([
            "Integration_us", "Integration_ms",
            "ADC_AVG_mean", "ADC_AVG_std", "N_cycles"
        ])
        for r in results:
            writer.writerow([
                r["us"], round(r["ms"], 4),
                round(r["p_avg"], 3), round(r["p_std"], 3), r["n"],
            ])
    print(f"Aggregated data saved to : {filename_main}")

    # Per-cycle time series — useful for inspecting thermal convergence
    filename_cycles = f"DarkCurrent_{timestamp}_per_cycle.csv"
    sorted_times = sorted(TEST_INTEGRATION_TIMES_US)
    with open(filename_cycles, "w", newline="") as f:
        writer = csv.writer(f, delimiter=";")
        header = ["Cycle"] + [f"{us}_us" for us in sorted_times]
        writer.writerow(header)
        for idx, cm in enumerate(per_cycle_means):
            row = [idx + 1] + [round(cm.get(us, float("nan")), 3) for us in sorted_times]
            writer.writerow(row)
    print(f"Per-cycle data saved to  : {filename_cycles}")

def save_plots(results, per_cycle_means, bias, rate, timestamp):
    ms_x    = [r["ms"]    for r in results]
    adc_avg = [r["p_avg"] for r in results]
    adc_std = [r["p_std"] for r in results]

    linear_results = [r for r in results if r["us"] <= LINEAR_RANGE_CEILING_US]
    fit_t          = np.array([r["us"] / 1_000_000.0 for r in linear_results])
    fit_adc        = bias + rate * fit_t
    fit_ms         = [r["ms"] for r in linear_results]

    fig, axes = plt.subplots(3, 1, figsize=(13, 15))

    # --- Plot 1: Full range, log x-axis, with error bars ---
    ax = axes[0]
    ax.errorbar(ms_x, adc_avg, yerr=adc_std, fmt="o", markersize=5,
                capsize=4, label="mean +/- 1 std across cycles")
    ax.plot(fit_ms, fit_adc, linestyle="-", color="red", linewidth=2,
            label=f"Linear fit (up to {LINEAR_RANGE_CEILING_US // 1000} ms)  "
                  f"Bias={bias:.2f}  Rate={rate:.2f} ADC/s")
    ax.axvline(x=LINEAR_RANGE_CEILING_US / 1000.0, color="red", linestyle=":",
               alpha=0.6, label=f"Linearity ceiling: {LINEAR_RANGE_CEILING_US // 1000} ms")
    ax.set_xscale("log")
    ax.set_title("Full Range: Dark Current vs Integration Time (log x-axis, cyclic measurement)")
    ax.set_xlabel("Integration time (ms, logarithmic)")
    ax.set_ylabel("Intensity (ADC counts)")
    ax.grid(True, which="both", linestyle="-", alpha=0.35)
    ax.legend(fontsize=9)

    # --- Plot 2: Linear zoom, non-linearity region ---
    ax = axes[1]
    zoom = [r for r in results if r["ms"] >= 1000]
    zoom_ms  = [r["ms"]    for r in zoom]
    zoom_adc = [r["p_avg"] for r in zoom]
    zoom_std = [r["p_std"] for r in zoom]
    ax.errorbar(zoom_ms, zoom_adc, yerr=zoom_std, fmt="o", markersize=5,
                capsize=4, color="steelblue")
    zoom_fit = [(r["ms"], bias + rate * r["us"] / 1_000_000.0)
                for r in zoom if r["us"] <= LINEAR_RANGE_CEILING_US]
    if zoom_fit:
        zf_ms, zf_adc = zip(*zoom_fit)
        ax.plot(zf_ms, zf_adc, linestyle="-", color="red", linewidth=2,
                label="Linear fit (extrapolated)")
    ax.axvline(x=LINEAR_RANGE_CEILING_US / 1000.0, color="red", linestyle=":",
               alpha=0.6, label=f"Linearity ceiling")
    ax.set_title("Detail View: Non-linearity onset (1000 ms to 4000 ms)")
    ax.set_xlabel("Integration time (ms)")
    ax.set_ylabel("Intensity (ADC counts)")
    ax.grid(True)
    ax.legend(fontsize=9)

    # --- Plot 3: Thermal convergence — per-cycle means at a few reference times ---
    ax = axes[2]
    probe_times = [t for t in [10, 100_000, 1_000_000, 2_500_000]
                   if t in TEST_INTEGRATION_TIMES_US]
    cycle_indices = list(range(1, len(per_cycle_means) + 1))

    for us in probe_times:
        series = [cm.get(us, float("nan")) for cm in per_cycle_means]
        ax.plot(cycle_indices, series, marker="o", markersize=4,
                label=f"{us // 1000} ms" if us >= 1000 else f"{us} us")

    ax.set_title("Thermal Convergence: Per-Cycle Mean per Integration Time\n"
                 "(flat lines = sensor has reached thermal equilibrium)")
    ax.set_xlabel("Cycle index")
    ax.set_ylabel("ADC counts (avg 0 to 300 nm region)")
    ax.grid(True)
    ax.legend(fontsize=9)

    plt.tight_layout()
    png_filename = f"DarkCurrent_{timestamp}.png"
    plt.savefig(png_filename, dpi=150)
    plt.close()
    print(f"Plot saved to            : {png_filename}")

# ============================================================
# ENTRY POINT
# ============================================================

try:
    print("Initializing sensor...")
    ctrl_out(1)

    results, per_cycle_means = run_characterization()
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")

    bias, rate, r2 = fit_dark_model(results)

    save_csv(results, per_cycle_means, timestamp)
    save_plots(results, per_cycle_means, bias, rate, timestamp)

    print()
    print("=" * 65)
    print(" DARK CURRENT MODEL FIT RESULTS")
    print("=" * 65)
    print()
    n_linear = sum(1 for r in results if r["us"] <= LINEAR_RANGE_CEILING_US)
    print(f"  Measurement design       : cyclic  (randomized={RANDOMIZE_ORDER_PER_CYCLE})")
    print(f"  Cycles completed         : {len(per_cycle_means)}")
    print(f"  Data points in fit       : {n_linear}  (up to {LINEAR_RANGE_CEILING_US // 1000} ms)")
    print(f"  R-squared                : {r2:.6f}")
    print()
    print("  Model:  ADC_dark(t) = Bias + Rate * t_seconds")
    print()
    print(f"  Bias  (ADC offset at t=0) : {bias:.2f} ADC counts")
    print(f"  Rate  (thermal slope)     : {rate:.2f} ADC counts / second")
    print()
    print("  Copy these values into the dashboard configuration block:")
    print()
    print(f"    DEFAULT_DARK_BIAS_ADC          = {bias:.2f}")
    print(f"    DEFAULT_DARK_RATE_ADC_PER_SEC  = {rate:.2f}")
    print()
    print("  Check the thermal convergence plot (plot 3) to verify the")
    print("  sensor had reached equilibrium before the majority of cycles.")
    print("  If the early cycles show a clear upward drift, re-run with a")
    print("  longer warm-up or discard the first few cycles manually from")
    print("  the per_cycle CSV before refitting.")
    print()
    print("=" * 65)

except KeyboardInterrupt:
    print("\nMeasurement run aborted by user.")

finally:
    print("Releasing USB handles...")
    winusb.WinUsb_Free(usb_handle)
    kernel32.CloseHandle(handle)
    print("Device released cleanly.")
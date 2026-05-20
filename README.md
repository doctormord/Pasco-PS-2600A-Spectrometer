# PASCO PS-2600A – Native WinUSB Spectrometer Dashboard
 
A Python application for direct, low-level USB access to the PASCO PS-2600A spectrometer on Windows,
bypassing the official PASCO software entirely. Provides a real-time spectral scope, time-lapse heatmap,
auto-exposure, dark current correction, peak detection, and CSV/PNG export — all in a single file.

![alt text](https://github.com/doctormord/Pasco-PS-2600A-Spectrometer/blob/main/images/GUI_Plot.png "GUI PLOT")
![alt text](https://github.com/doctormord/Pasco-PS-2600A-Spectrometer/blob/main/images/GUI_Waterfall.png "GUI Waterfall")
 
---
 
## Features
 
**Acquisition**
- Real-time live spectrum streaming at the maximum rate the hardware allows
- Adjustable integration time from 1 ms to 2500 ms
- Automatic exposure control with configurable target ADC level and adjustment ratio limits
- Frame averaging (1–100 frames) for noise reduction
**Display**
- Scope mode: live spectral curve with wavelength axis (nm) and ADC intensity axis
- Time-lapse heatmap (waterfall plot): 100-frame rolling history using an inferno colormap
- Auto-scaling Y axis based on visible data range
- Mouse cursor with live readout of wavelength and intensity at hover position
- Draggable measurement cursors (Measure Mode) with delta-wavelength and delta-intensity readout
**Processing**
- Dark current correction using a configurable bias offset and thermal rate model (ADC/s)
- Hot-pixel / despeckle filter using a configurable-width median kernel
- Peak detection returning the top 3 dominant spectral peaks with minimum distance enforcement
**Reference & Export**
- Overlay of reference spectra from a user-editable CSV library (hydrogen, mercury included by default)
- CSV export of current spectrum or full heatmap buffer
- PNG screenshot export of the active plot
**Hardware Layer**
- Pure Python ctypes interface to WinUSB — no libusb, no pyusb, no third-party USB wrapper
- Correct FILE_FLAG_OVERLAPPED handle creation as required by WinUsb_Initialize
- Full protocol implementation reverse-engineered from USB capture (see Protocol section below)
---
 
## Requirements
 
### Operating System
 
Windows 10 or Windows 11 (64-bit). The WinUSB driver stack (`winusb.dll`, `setupapi.dll`) is built into Windows
and does not require separate installation.
 
### Driver
 
The PASCO device ships with a vendor-specific driver that blocks direct WinUSB access. You must replace it:
 
1. Download [Zadig](https://zadig.akeo.ie/) and run it as Administrator.
2. Go to **Options** and enable **List All Devices**.
3. Select **Spectrometer** (VID `0945`, PID `0002`) from the dropdown.
4. Set the target driver to **WinUSB** and click **Replace Driver** (or **Install Driver**).
5. Confirm that the left-hand field now shows **WinUSB** as the active driver.
Note: after this substitution, the official PASCO SPARKvue and Capstone software will no longer recognize
the device. You can restore the original driver at any time through Device Manager by uninstalling the
WinUSB driver and reinstalling the vendor driver.

![alt text](https://github.com/doctormord/Pasco-PS-2600A-Spectrometer/blob/main/images/Zadig_USB.png "Zadig USB")
 
### Python
 
Python 3.10 or newer (64-bit build required — the ctypes pointer size check in the device enumeration
path assumes a 64-bit address space).
 
### Python Packages
 
Install all dependencies with:
 
```
pip install pyqt6 pyqtgraph numpy
```
 
| Package | Purpose |
|---|---|
| `pyqt6` | Main GUI framework |
| `pyqtgraph` | High-performance real-time plotting and heatmap rendering |
| `numpy` | Spectrum array operations, averaging, peak detection, median filter |
 
The following packages are used but are part of the Python standard library and require no installation:
`ctypes`, `struct`, `time`, `os`, `sys`, `csv`, `datetime`, `collections`.
 
---
 
## Installation and Launch
 
```
git clone https://github.com/yourname/pasco-ps2600a-dashboard
cd pasco-ps2600a-dashboard
pip install pyqt6 pyqtgraph numpy
python spectrometer.py
```
 
Connect the spectrometer via USB before or after launching. Use the **Scan USB** button to enumerate
available devices, select the entry matching your device, and click **Connect**.
 
---
 
## Wavelength Calibration
 
The pixel-to-wavelength mapping uses a cubic polynomial calibration embedded in the configuration block
at the top of the file:
 
```python
WAVELENGTH_COEFFS = [130.755917, 0.262201464, 1.44855491e-05, -4.30320660e-09]
```
 
The wavelength for pixel index `i` is computed as:
 
```
lambda(i) = C0 + C1*i + C2*i^2 + C3*i^3
```
 
This yields approximately 130 nm at pixel 0 and 1085 nm at pixel 3647. If your unit produces different
results against known spectral lines, update these four coefficients accordingly. The calibration array
is computed once at startup and reused for all subsequent operations.
 
---
 
## Reference Library
 
On first launch, a file named `reference_spectra.csv` is created automatically in the working directory.
It contains two synthetic reference spectra: hydrogen Balmer series and mercury discharge lines.
 
To add your own reference spectrum, append a column to the CSV. The format is semicolon-delimited with
a header row. The first column must be `Wavelength_nm`, followed by one column per reference source.
Each row corresponds to one pixel in order from pixel 0 to pixel 3647. The new entry will appear in the
**Library** dropdown at the next launch.
 
---
 
## USB Protocol — Reverse Engineering Notes
 
This section documents how the device communication protocol was determined. The official PASCO SDK is
not publicly documented at the USB transfer level, so the protocol was recovered entirely from live
USB traffic analysis.
 
### Tools Used
 
- **Wireshark** with **USBPcap** for live USB traffic capture on Windows
- Python `ctypes` for direct WinUSB access without any abstraction layer
### Device Identification
 
The spectrometer enumerates as VID `0x0945`, PID `0x0002`. It exposes a WinUSB-compatible interface
registered under the GUID `{DEE824EF-729B-4A0E-9C14-B7117D33A817}`, which is the standard Microsoft
OS descriptor GUID for WinUSB devices. The device also appeared under a secondary GUID
`{9a9a65ee-a425-482d-ae44-80da1a1210c6}` in early captures; the DEE824EF GUID proved more reliable
for enumeration.
 
### Initial Access Problem
 
Opening the device with `CreateFileW` returned error code 5 (`ERROR_ACCESS_DENIED`) even under
an Administrator account. Two things were required to resolve this:
 
First, the vendor driver had to be replaced with WinUSB via Zadig, as described above.
 
Second — and this is the non-obvious part — `CreateFileW` must be called with `FILE_FLAG_OVERLAPPED`
(`0x40000000`) in the `dwFlagsAndAttributes` parameter. Passing zero here causes `WinUsb_Initialize`
to fail or the subsequent handle to be rejected. This flag is documented as required by WinUSB but is
easy to miss. The flag was already defined as a constant in the original code but was mistakenly passed
as `0` in the actual call.
 
### Transfer Topology
 
All communication goes through two mechanisms:
 
- **Control transfers** on endpoint `0x00` (the default control pipe): used for command dispatch and
  legacy status polling.
- **Bulk IN transfers** on endpoint `0x82`: used for all data coming back from the device — both the
  main spectral payload and inter-frame synchronization packets.
### Command Set
 
The following vendor-class control transfers were identified:
 
| Request Code | Direction | wValue | wIndex | Purpose |
|---|---|---|---|---|
| `0x01` | OUT | 0 | 0 | Device init / soft reset |
| `0x02` | OUT | low 16 bits of time_us | high 16 bits of time_us | Set integration time in microseconds |
| `0x09` | OUT | 0 | 0 | Arm and trigger one acquisition |
| `0x83` (131) | IN | — | — | Poll acquisition status (legacy) |
 
Request `0x02` passes a 32-bit microsecond value split across `wValue` (low word) and `wIndex`
(high word). For integration times below 65535 µs this is equivalent to just setting `wValue`.
 
### The Actual Acquisition Cycle
 
Early attempts used the status polling byte from request `0x83` as the primary ready signal,
looping until the returned byte went non-zero before issuing a bulk read. This produced alternating
valid and all-zero frames — every second acquisition was empty.
 
The root cause was identified by capturing a full working session from the PASCO software in Wireshark
and examining the exact packet sequence around each 7387-byte bulk transfer.
 
The correct sequence per frame, as observed in the capture, is:
 
```
1.  BULK IN  0x82  28 bytes    drain packet — consume leftover bytes from previous cycle
2.  CTRL OUT 0x00  REQ=0x09    trigger new acquisition
3.  BULK IN  0x82  7360 bytes  main spectral payload
4.  BULK IN  0x82  28 bytes    trailing drain packet
5.  CTRL OUT 0x00  REQ=0x02    set integration time for next cycle
```
 
The device uses the bulk pipe itself as the synchronization mechanism. The 28-byte drain reads
are not status packets in any meaningful sense — they serve as flow-control acknowledgements that
tell the device firmware the host is ready for the next transfer. Skipping either drain read desynchronizes
the pipeline and causes the device to buffer the next frame's data behind a stale transfer, which
then reads as zeros.
 
The status polling via `0x83` is not used in the final implementation. It was present in early
PASCO captures in a different context and does not belong in the per-frame acquisition loop.
 
### Payload Structure
 
Each 7360-byte bulk transfer has the following layout:
 
```
Bytes 0–63      : Header (64 bytes) — device metadata, first 4 bytes are zero
Bytes 64–7359   : Spectral data (7296 bytes = 3648 pixels * 2 bytes/pixel)
```
 
The spectral data is packed as 3648 unsigned 16-bit little-endian integers. Each value is a raw ADC
count in the range 0–4095 (12-bit ADC). The 28-byte drain packets carry a small amount of pixel data
as well (visible in the Wireshark hex dump) but their content is discarded.
 
### Integration Time Encoding for Large Values
 
For integration times above 65535 µs the value must be split:
 
```python
low_word  = microseconds & 0xFFFF
high_word = (microseconds >> 16) & 0xFFFF
usb_control_transfer_out(CMD_SET_EXPOSURE, value=low_word, index=high_word)
```
 
This was inferred from the `wValue`/`wIndex` field sizes in the SETUP packet and confirmed by
testing: passing only `wValue` with a large time resulted in incorrect (truncated) exposure durations.
 
---
 
## Configuration Reference
 
All hardware and software parameters are collected in the configuration block at the top of the source
file. No values are hardcoded elsewhere.
 
| Constant | Default | Description |
|---|---|---|
| `START_INTEGRATION_TIME_US` | 20000 | Initial exposure time in microseconds |
| `MIN_INTEGRATION_TIME_US` | 1000 | Minimum allowed exposure time |
| `MAX_INTEGRATION_TIME_US` | 2500000 | Maximum allowed exposure time |
| `AUTO_EXP_TARGET_ADC` | 3400 | Target peak ADC value for auto-exposure |
| `AUTO_EXP_DEADZONE_ADC` | 100 | Auto-exposure does not adjust within this band |
| `AUTO_EXP_MIN_RATIO` | 0.2 | Minimum per-frame adjustment factor |
| `AUTO_EXP_MAX_RATIO` | 5.0 | Maximum per-frame adjustment factor |
| `AUTO_EXP_EMERGENCY_DROP_RATIO` | 0.2 | Immediate reduction factor on saturation |
| `DEFAULT_DARK_BIAS_ADC` | 60.5 | Constant dark current offset |
| `DEFAULT_DARK_RATE_ADC_PER_SEC` | 45.0 | Thermal dark current rate |
| `ADC_SATURATION_THRESHOLD` | 3800 | ADC value considered saturated |
| `PEAK_MIN_DISTANCE_PIXELS` | 100 | Minimum pixel separation between detected peaks |
| `MIN_PEAK_HEIGHT_ADC` | 15.0 | Minimum ADC value for peak detection |
| `HEATMAP_HISTORY_SIZE` | 100 | Number of frames stored in the waterfall buffer |
| `WAVELENGTH_COEFFS` | see above | Cubic polynomial calibration coefficients |
 
---
 

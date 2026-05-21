# PASCO PS-2600A – Native WinUSB Spectrometer Dashboard

A high-performance, real-time spectroscopy software suite for the PASCO PS-2600A spectrometer, providing direct low-level USB communication through the native Windows WinUSB stack — completely bypassing the official PASCO software ecosystem.

This project bridges the gap between raw hardware access and professional-grade laboratory analysis by combining reverse-engineered USB protocol control, high-speed real-time visualization, dark current modeling, spectral analysis, and export tooling into a single standalone Python application.

The software was built entirely from scratch through empirical USB traffic analysis and protocol reconstruction using Wireshark and USBPcap, as no public SDK documentation exists for raw USB transfers on the device.

---

# Screenshots

![GUI Plot](https://github.com/doctormord/Pasco-PS-2600A-Spectrometer/blob/main/images/GUI_Plot.png "GUI Plot")

![GUI Waterfall](https://github.com/doctormord/Pasco-PS-2600A-Spectrometer/blob/main/images/GUI_Waterfall.png "GUI Waterfall")

![Zadig Driver Setup](https://github.com/doctormord/Pasco-PS-2600A-Spectrometer/blob/main/images/Zadig_USB.png "Zadig Driver Setup")

---

# Key Features

---

## Multi-Threaded Architecture

- Dedicated acquisition thread fully isolated from the GUI thread
- Maintains a responsive interface even during very long integration times
- Stable operation up to the scientifically validated 2.5-second integration limit
- Continuous USB streaming without GUI blocking

---

## Acquisition Features

- Real-time live spectrum streaming at the maximum hardware-supported rate
- Adjustable integration time from 1 ms to 2500 ms
- Automatic exposure control
- Configurable ADC target levels
- Exposure adjustment ratio clamping
- Emergency saturation protection
- Frame averaging (1–100 frames) for noise reduction

---

## Visualization Modes

### Scope Mode

- High-speed live spectral waveform rendering
- Wavelength axis in nanometers
- ADC intensity axis
- Auto-scaling Y axis
- Real-time cursor hover readout
- Draggable measurement cursors
- Delta wavelength (`dx`) measurement
- Delta intensity (`dy`) measurement

### Time-Lapse Heatmap (Waterfall Plot)

- Rolling spectral history visualization
- 100-frame history buffer
- Inferno colormap rendering
- Real-time temporal evolution analysis
- Optimized GPU-accelerated pyqtgraph rendering

---

## Signal Processing

### Dark Current Correction

Real-time software-side subtraction using an empirical thermal dark current model:

```math
I_{dark} = Bias + Rate \cdot Time
```

Where:

- `Bias` = static sensor offset
- `Rate` = thermal accumulation rate
- `Time` = integration time

Features:

- Adjustable dark current bias
- Adjustable thermal rate
- Exposure-aware correction
- Scientifically linear correction model

### Hot Pixel / Despeckle Filter

- Median-kernel hot-pixel suppression
- Adjustable kernel width
- Real-time filtering
- Removes sensor artifacts and cosmic spike noise

---

## Analysis Tools

### Peak Finder

- Real-time dominant spectral peak detection
- Non-Maximum Suppression (NMS)
- Minimum distance enforcement
- Top 3 peak extraction
- Automatic peak labeling

### Spectral Reference Library

- CSV-based editable reference library
- Overlay support for comparison
- Hydrogen Balmer series included
- Mercury discharge lines included
- User-expandable reference database

---

## Export Features

### CSV Export

- Single-frame spectrum export
- Full heatmap history export
- Timestamped filenames
- Wavelength + ADC export

### PNG Export

- Scope screenshot export
- Heatmap screenshot export
- High-resolution snapshots

---

## Hardware Layer

- Pure Python ctypes interface to WinUSB
- No `pyusb`
- No `libusb`
- No third-party USB abstraction layer
- Direct WinUSB API access
- Full reverse-engineered protocol implementation
- Native overlapped I/O support

---

# Requirements

---

## Operating System

- Windows 10 (64-bit)
- Windows 11 (64-bit)

The required WinUSB components (`winusb.dll`, `setupapi.dll`) are already included in Windows.

---

## Python Version

- Python 3.10 or newer
- 64-bit Python required

Python 3.13 is fully supported and recommended.

---

# Driver Installation (CRITICAL)

The official PASCO vendor driver blocks direct low-level USB access.

The device MUST be reassigned to the WinUSB driver using Zadig.

---

## Installing WinUSB via Zadig

1. Download Zadig:
   https://zadig.akeo.ie/

2. Launch Zadig as Administrator

3. Open:

```text
Options → List All Devices
```

4. Select:

```text
Spectrometer (VID 0945, PID 0002)
```

5. Choose target driver:

```text
WinUSB
```

6. Click:

```text
Replace Driver
```

or

```text
Install Driver
```

7. Confirm WinUSB is now shown as the active driver.

---

## Important Note

After replacing the driver:

- PASCO SPARKvue will no longer recognize the device
- PASCO Capstone will no longer recognize the device

The original driver can be restored at any time through Device Manager.

---

# Python Dependencies

Install all required packages:

```bash
pip install pyqt6 pyqtgraph numpy
```

---

## External Packages

| Package | Purpose |
|---|---|
| `pyqt6` | Main GUI framework |
| `pyqtgraph` | High-speed plotting and heatmap rendering |
| `numpy` | Numerical processing and filtering |

---

## Standard Library Modules Used

No installation required:

```text
ctypes
struct
time
os
sys
csv
datetime
collections
threading
queue
```

---

# Installation

Clone the repository:

```bash
git clone https://github.com/doctormord/Pasco-PS-2600A-Spectrometer
cd Pasco-PS-2600A-Spectrometer
```

Install dependencies:

```bash
pip install pyqt6 pyqtgraph numpy
```

Launch the application:

```bash
python spectrometer.py
```

or:

```bash
python usb_test_pro.py
```

---

# First Launch

On first launch:

- `reference_spectra.csv` is generated automatically
- Default hydrogen and mercury spectra are included
- Connect the spectrometer before or after launch
- Use the **Scan USB** button to enumerate devices
- Select the PASCO spectrometer entry
- Click **Connect**

---

# Wavelength Calibration

The pixel-to-wavelength conversion uses a cubic calibration polynomial:

```python
WAVELENGTH_COEFFS = [
    130.755917,
    0.262201464,
    1.44855491e-05,
    -4.30320660e-09
]
```

The wavelength mapping is:

```math
\lambda(i) = C_0 + C_1 i + C_2 i^2 + C_3 i^3
```

Where:

- `i` = pixel index
- `λ(i)` = wavelength in nanometers

Approximate range:

- Pixel 0 → ~130 nm
- Pixel 3647 → ~1085 nm

The calibration table is computed once during startup and reused throughout runtime.

---

# Reference Spectrum Library

A file named:

```text
reference_spectra.csv
```

is automatically generated in the working directory.

---

## CSV Format

- Semicolon-delimited
- First column:

```text
Wavelength_nm
```

- Additional columns represent reference spectra

Example:

```csv
Wavelength_nm;Hydrogen;Mercury
130.0;0;0
130.3;0;0
...
```

Each row corresponds to one spectrometer pixel.

New spectra appear automatically in the GUI library selector after restart.

---

# USB Protocol Architecture & Reverse Engineering

The PASCO PS-2600A communicates through a vendor-specific USB protocol wrapped inside the Windows WinUSB stack.

No official low-level protocol documentation exists.

The complete communication protocol was reconstructed entirely from live USB traffic captures.

---

# Reverse Engineering Process

The protocol was reverse-engineered using:

- Wireshark
- USBPcap
- Differential USB traffic analysis
- Timestamp correlation
- Iterative packet replay testing

Traffic between the official PASCO software and the hardware was captured and filtered for:

```text
URB_CONTROL
URB_BULK
```

This allowed reconstruction of the internal acquisition state machine.

---

# Initial Access Problem

Early attempts to initialize the device consistently failed:

```text
ERROR_ACCESS_DENIED
```

when calling:

```text
CreateFileW
```

even under Administrator privileges.

The issue was ultimately traced to a critical WinUSB requirement:

```text
FILE_FLAG_OVERLAPPED
```

must be passed during device handle creation.

Without this flag:

- `WinUsb_Initialize()` fails
- handles are rejected
- asynchronous USB access breaks

This requirement is documented by Microsoft but easy to overlook.

---

# Device Identification

The spectrometer enumerates as:

| Parameter | Value |
|---|---|
| VID | `0x0945` |
| PID | `0x0002` |

Observed WinUSB interface GUID:

```text
{DEE824EF-729B-4A0E-9C14-B7117D33A817}
```

Secondary GUID observed during early captures:

```text
{9a9a65ee-a425-482d-ae44-80da1a1210c6}
```

---

# Transfer Topology

Communication uses:

---

## Control Transfers (`Endpoint 0x00`)

Used for:

- Device initialization
- Exposure control
- Triggering
- Legacy status polling

Transfer types:

```text
0x40 → Vendor OUT
0xC0 → Vendor IN
```

---

## Bulk Transfers (`Endpoint 0x82`)

Used for:

- Spectral payload transfer
- Synchronization packets
- Inter-frame drain packets

---

# Command Set

| Request Code | Direction | Purpose |
|---|---|---|
| `0x01` | OUT | Device initialization |
| `0x02` | OUT | Set integration time |
| `0x09` | OUT | Trigger acquisition |
| `0x83` (`131`) | IN | Legacy status polling |

---

# Integration Time Encoding

Integration time is transmitted in microseconds as a 32-bit integer.

Because USB setup packets only provide 16-bit `wValue` and `wIndex` fields,
the value must be split:

```python
low_word  = microseconds & 0xFFFF
high_word = (microseconds >> 16) & 0xFFFF
```

Transfer:

```python
usb_control_transfer_out(
    CMD_SET_EXPOSURE,
    value=low_word,
    index=high_word
)
```

---

# Actual Acquisition Cycle

The final validated acquisition sequence is:

```text
1. BULK IN  0x82  28 bytes    drain previous cycle
2. CTRL OUT 0x00  REQ=0x09    trigger acquisition
3. BULK IN  0x82  7360 bytes  spectral payload
4. BULK IN  0x82  28 bytes    trailing drain packet
5. CTRL OUT 0x00  REQ=0x02    set next exposure
```

---

# The 28-Byte Drain Packet Problem

One of the most critical discoveries during reverse engineering was that the device uses small 28-byte bulk transfers as synchronization acknowledgements.

These packets MUST be drained every cycle.

Skipping them causes:

- pipeline desynchronization
- stale frame buffering
- alternating zero frames
- ghosting artifacts
- dropped acquisitions

This issue was invisible until full USB captures were analyzed frame-by-frame.

---

# Payload Structure

Main payload size:

```text
7360 bytes
```

Structure:

| Bytes | Purpose |
|---|---|
| `0–63` | Header / metadata |
| `64–7359` | Spectral ADC payload |

---

## Spectral Data

The spectral payload contains:

```text
3648 unsigned 16-bit little-endian integers
```

Format:

```python
<H
```

ADC range:

```text
0–4095
```

(12-bit ADC)

---

# Legacy Polling Discovery

During early experimentation, request:

```text
0x83
```

was used for polling.

A non-zero return value appeared to indicate acquisition readiness.

However, later investigation revealed that the final acquisition synchronization is actually driven by the bulk pipe itself, not by status polling.

The polling mechanism was therefore removed from the final acquisition loop.

---

# Constraints & Sensor Linearity

---

# The Scientifically Valid 2.5 Second Limit

One of the most important discoveries during characterization of the PASCO PS-2600A was the existence of a hard linearity boundary at approximately:

```text
2500 ms
```

During dark-current analysis with a fully shielded optical path, the sensor initially followed an extremely clean linear accumulation model:

```math
I_{dark} = Bias + Rate \cdot Time
```

Up to approximately 2.5 seconds, the sensor response remained highly linear and predictable.

However, beyond this threshold, empirical measurements revealed:

- non-linear ADC compression
- signal flattening
- reduced accumulation slope
- apparent onboard clamping behavior

This strongly suggests that the firmware internally activates dynamic signal limiting to prevent ADC overflow during extremely long integrations.

---

## Why This Matters

The software’s dark current correction assumes strict linearity.

If integrations beyond 2.5 seconds were permitted:

- the correction model would overestimate thermal noise
- legitimate spectral peaks would be pushed negative
- measurements would lose scientific validity

For this reason:

```python
MAX_INTEGRATION_TIME_US = 2500000
```

is deliberately hardcoded as a strict operational ceiling.

The software intentionally confines operation to the spectrometer’s empirically validated linear regime.

---

# Configuration Reference

All major parameters are centralized in the configuration section of the source file.

---

| Constant | Default | Description |
|---|---|---|
| `START_INTEGRATION_TIME_US` | `20000` | Initial exposure time |
| `MIN_INTEGRATION_TIME_US` | `1000` | Minimum integration time |
| `MAX_INTEGRATION_TIME_US` | `2500000` | Maximum scientifically valid integration time |
| `AUTO_EXP_TARGET_ADC` | `3400` | Auto-exposure target |
| `AUTO_EXP_DEADZONE_ADC` | `100` | Auto-exposure deadzone |
| `AUTO_EXP_MIN_RATIO` | `0.2` | Minimum adjustment ratio |
| `AUTO_EXP_MAX_RATIO` | `5.0` | Maximum adjustment ratio |
| `AUTO_EXP_EMERGENCY_DROP_RATIO` | `0.2` | Saturation emergency reduction |
| `DEFAULT_DARK_BIAS_ADC` | `60.5` | Constant dark offset |
| `DEFAULT_DARK_RATE_ADC_PER_SEC` | `45.0` | Thermal dark accumulation |
| `ADC_SATURATION_THRESHOLD` | `3800` | Saturation threshold |
| `PEAK_MIN_DISTANCE_PIXELS` | `100` | Peak separation |
| `MIN_PEAK_HEIGHT_ADC` | `15.0` | Minimum peak intensity |
| `HEATMAP_HISTORY_SIZE` | `100` | Waterfall buffer size |
| `WAVELENGTH_COEFFS` | see above | Calibration polynomial |

---

# File Output

All exports are timestamped and saved in the working directory.

---

| Filename Pattern | Content |
|---|---|
| `spectrum_data_YYYYMMDD_HHMMSS.csv` | Current spectrum |
| `heatmap_data_YYYYMMDD_HHMMSS.csv` | Full waterfall history |
| `spectrum_plot_YYYYMMDD_HHMMSS.png` | Scope screenshot |
| `heatmap_plot_YYYYMMDD_HHMMSS.png` | Heatmap screenshot |
| `reference_spectra.csv` | Reference spectrum library |

---

# Technical Summary

This project demonstrates that the PASCO PS-2600A can be fully operated through native WinUSB access without relying on proprietary vendor software.

The implementation includes:

- complete reverse-engineered USB protocol support
- direct WinUSB communication
- real-time spectroscopy
- scientific dark-current correction
- dynamic visualization
- spectral analysis tooling
- CSV/PNG export infrastructure
- hardware-level synchronization handling

All functionality operates entirely in user-space Python with no proprietary SDK dependencies.

---

# License

This project is intended for educational, scientific, and reverse-engineering research purposes.

The PASCO PS-2600A hardware and associated trademarks belong to PASCO Scientific.

This repository is an independent community reverse-engineering effort and is not affiliated with PASCO Scientific.

````

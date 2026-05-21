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
- Real-time crosshair cursor with live wavelength and intensity readout
- Draggable measurement cursors (A / B)
- Delta wavelength (`dx`) measurement
- Delta intensity (`dy`) measurement

### Time-Lapse Heatmap (Waterfall Plot)

- Rolling spectral history visualization
- 100-frame history buffer
- Inferno colormap rendering
- Real-time temporal evolution analysis
- Optimized GPU-accelerated pyqtgraph rendering
- Full crosshair cursor readout: wavelength, frame index, and intensity at cursor position

---

## Signal Processing

### Dark Current Correction

The software supports two independent correction modes. The active mode is selectable in the GUI at runtime.

#### Mode 1 — Optical Black (default, recommended)

The PS-2600A transfer header contains 30 physically masked CCD pixels (optical black pixels) that are never exposed to light. These pixels accumulate dark charge under identical conditions as the spectral pixels — same silicon die, same temperature, same integration time — and are read out in every single frame as part of the standard USB transfer at no additional cost.

Their mean value provides a live, per-frame dark reference:

```math
I_{corrected} = \max(0,\ I_{raw} - (slope \cdot \overline{OB} + offset))
```

Where `slope` and `offset` are calibrated from a characterization run. For the reference unit:

```
OB_CORRECTION_SLOPE  = 1.008180
OB_CORRECTION_OFFSET = -0.6377
```

This approach automatically compensates for:

- Temperature-driven dark current drift during a session
- Warm-up transients after cold start
- Ambient temperature differences between sessions
- Long-term sensor aging

No integration time assumption is required. The correction is physically exact regardless of exposure duration or sensor temperature, because the reference is derived from the same physical frame.

#### Mode 2 — Parametric (fallback)

Classical two-parameter linear thermal model:

```math
I_{dark} = Bias + Rate \cdot Time
```

Where:

- `Bias` = static sensor offset at zero integration time
- `Rate` = thermal dark charge accumulation rate in ADC counts per second
- `Time` = current integration time in seconds

Values from the cyclic characterization run (DarkCurrent_20260521):

```
DEFAULT_DARK_BIAS_ADC         = 61.57
DEFAULT_DARK_RATE_ADC_PER_SEC = 40.94
```

This mode is provided as a fallback and for comparison. It produces accurate results within the validated linear regime but cannot compensate for temperature drift during a session.

#### Live Dark Level Display

A dedicated status field below the control panel displays for every acquired frame:

- Current optical black mean in ADC counts
- Active integration time
- Relative sensor temperature hint derived from the thermal component of the OB mean

This field provides a continuous indirect readout of sensor die temperature without any external thermometer.

### Hot Pixel / Despeckle Filter

- Median-kernel hot-pixel suppression
- Adjustable kernel width (3–15 pixels, odd values only)
- Real-time filtering
- Removes sensor artifacts and cosmic spike noise

---

## Analysis Tools

### Peak Finder

- Real-time dominant spectral peak detection
- Non-Maximum Suppression (NMS)
- Minimum distance enforcement (pixels)
- Top 3 peak extraction
- Automatic peak labeling with wavelength and intensity

### Spectral Reference Library

- CSV-based editable reference library
- Overlay support for direct comparison against live data
- Hydrogen Balmer series included
- Mercury discharge lines included
- User-expandable reference database

---

## Export Features

### CSV Export

- Single-frame spectrum export with embedded metadata (OB mean, integration time, correction mode)
- Full heatmap history export (frame × wavelength matrix)
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
python PS-2600A_Pro.py
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
| `0x83` (`131`) | IN | Legacy status polling (unused in final loop) |

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
| `0–3` | Padding (always zero) |
| `4–63` | 30 optical black pixels (uint16 LE) |
| `64–7359` | Spectral ADC payload (3648 × uint16 LE) |

---

## Optical Black Header Pixels

The first 64 bytes of every transfer were initially assumed to be device metadata. Systematic warm-up analysis revealed that bytes 4–63 carry 30 physically masked CCD pixels that track dark current identically to the spectral region.

Correlation analysis across 10 measurement cycles at 24 integration times confirmed:

```
Correlation slope  : 1.008180  (ideal: 1.0)
Correlation offset : -0.6377 ADC
R-squared          : 0.999943
```

These pixels are now used as the primary per-frame dark reference in Optical Black correction mode.

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

During early experimentation, request `0x83` was used for polling.

A non-zero return value appeared to indicate acquisition readiness.

However, later investigation revealed that the final acquisition synchronization is actually driven by the bulk pipe itself, not by status polling.

The polling mechanism was therefore removed from the final acquisition loop.

---

# Constraints & Sensor Linearity

---

# The 2.5 Second Integration Limit

Empirical characterization of the sensor established a hard integration time ceiling at:

```text
2500 ms
```

During dark-current analysis with a fully shielded optical path, the parametric dark model `I_dark = Bias + Rate * t` fitted the measured data with R² = 0.9958 across the full characterization range.

Beyond approximately 2.5 seconds, onboard firmware signal limiting behavior has been observed in some operating conditions. To ensure the spectral dynamic range remains fully available for actual signal peaks and to stay within the validated parametric model regime, the ceiling is retained.

Note that this limit is relevant primarily when using Parametric dark correction mode. In Optical Black mode, the correction is derived from the frame itself and does not depend on any integration time assumption, so model accuracy above 2.5 seconds is not a factor. The ceiling remains in place as a conservative operating boundary regardless of correction mode.

```python
MAX_INTEGRATION_TIME_US = 2_500_000
```

---

# Configuration Reference

All major parameters are centralized in the configuration section of the source file.

---

| Constant | Default | Description |
|---|---|---|
| `START_INTEGRATION_TIME_US` | `20000` | Initial exposure time (µs) |
| `MIN_INTEGRATION_TIME_US` | `1000` | Minimum integration time (µs) |
| `MAX_INTEGRATION_TIME_US` | `2500000` | Maximum integration time (µs) |
| `AUTO_EXP_TARGET_ADC` | `3400` | Auto-exposure ADC target |
| `AUTO_EXP_DEADZONE_ADC` | `100` | Auto-exposure deadzone |
| `AUTO_EXP_MIN_RATIO` | `0.2` | Minimum adjustment ratio |
| `AUTO_EXP_MAX_RATIO` | `5.0` | Maximum adjustment ratio |
| `AUTO_EXP_EMERGENCY_DROP_RATIO` | `0.2` | Saturation emergency reduction |
| `DEFAULT_DARK_BIAS_ADC` | `61.57` | Parametric model: bias offset |
| `DEFAULT_DARK_RATE_ADC_PER_SEC` | `40.94` | Parametric model: thermal rate |
| `OB_CORRECTION_SLOPE` | `1.008180` | Optical black calibration slope |
| `OB_CORRECTION_OFFSET` | `-0.6377` | Optical black calibration offset |
| `OB_PIXEL_COUNT` | `30` | Masked header pixels used as dark reference |
| `DEFAULT_DARK_MODE` | `optical_black` | Active correction mode at startup |
| `ADC_SATURATION_THRESHOLD` | `3800` | Saturation detection threshold |
| `PEAK_MIN_DISTANCE_PIXELS` | `100` | Minimum distance between detected peaks |
| `MIN_PEAK_HEIGHT_ADC` | `15.0` | Minimum peak intensity |
| `HEATMAP_HISTORY_SIZE` | `100` | Waterfall frame buffer depth |
| `WAVELENGTH_COEFFS` | see above | Cubic calibration polynomial |

---

# Dark Current Characterization Script

A standalone characterization script (`PS-2600A_Dark_Current_Characterization.py`) is included for measuring and validating the dark current parameters of a specific unit.

The script uses a cyclic measurement design to decouple thermal self-heating from integration time effects: instead of measuring all repetitions at one time step before advancing, one frame is acquired at each integration step per cycle, and the full cycle is repeated N times. Thermal drift is therefore distributed equally across all time steps rather than accumulating on the long-exposure end.

The script outputs:

- Fitted parametric model parameters (Bias, Rate, R²) for the Parametric correction mode
- Optical black correlation analysis (slope, offset, R²) confirming OB pixel validity
- A recommendation for which correction mode is appropriate based on the characterization data
- Per-cycle thermal convergence data to assess whether measurements were taken at thermal equilibrium

Run the characterization with the sensor input fully sealed (lens cap or equivalent) at the ambient temperature of normal operation. A full run at the default settings takes approximately 15 minutes.

---

# File Output

All exports are timestamped and saved in the working directory.

---

| Filename Pattern | Content |
|---|---|
| `spectrum_data_YYYYMMDD_HHMMSS.csv` | Current averaged spectrum with OB and correction metadata |
| `heatmap_data_YYYYMMDD_HHMMSS.csv` | Full waterfall history (frame × wavelength matrix) |
| `spectrum_plot_YYYYMMDD_HHMMSS.png` | Scope tab screenshot |
| `heatmap_plot_YYYYMMDD_HHMMSS.png` | Heatmap tab screenshot |
| `reference_spectra.csv` | Reference spectrum library |
| `DarkCurrent_YYYYMMDD_HHMMSS.png` | Characterization plots |
| `DarkCurrent_YYYYMMDD_HHMMSS_aggregated.csv` | Aggregated characterization data |
| `DarkCurrent_YYYYMMDD_HHMMSS_per_cycle.csv` | Per-cycle characterization data |

---

# Technical Summary

This project demonstrates that the PASCO PS-2600A can be fully operated through native WinUSB access without relying on proprietary vendor software.

The implementation includes:

- complete reverse-engineered USB protocol support
- direct WinUSB communication via ctypes
- real-time spectroscopy with live streaming
- dual-mode dark current correction: per-frame optical black and parametric fallback
- live sensor die temperature readout via optical black mean
- dynamic visualization in scope and waterfall modes with full cursor readout in both
- spectral peak analysis with NMS
- spectral reference library with live overlay
- CSV and PNG export infrastructure
- hardware-level synchronization via drain packet sequencing

All functionality operates entirely in user-space Python with no proprietary SDK dependencies.

---

# License

This project is intended for educational, scientific, and reverse-engineering research purposes.

The PASCO PS-2600A hardware and associated trademarks belong to PASCO Scientific.

This repository is an independent community reverse-engineering effort and is not affiliated with PASCO Scientific.

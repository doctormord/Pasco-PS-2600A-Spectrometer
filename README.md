# Multi-Device Spectrum Analyzer

A high-performance, real-time spectroscopy suite that turns four very different
spectrometers into one consistent instrument — with both a **PyQt6 desktop app**
and a **FastAPI/WebSocket web client** sharing a single, device-agnostic science
core. Every USB protocol was reverse-engineered from scratch; there are no vendor
SDKs in the loop.

Supported back-ends:

| Device | Bus / driver | Pixels | ADC | Range |
|---|---|---|---|---|
| **PASCO PS-2600A** | WinUSB (Zadig) / libusb on macOS·Linux | 3648 | 12-bit | ~300–1085 nm |
| **Ocean HDX-UV-VIS** | libusb (`libusb-package`), Ocean Binary Protocol | 2068 | 16-bit | 346–929 nm |
| **ASEQ / Lasertack LR-2T** | HID (`hidapi`) | 3653 | 16-bit | 297–981 nm |
| **Demo (virtual)** | none — pure numpy | 2048 | 16-bit | 340–850 nm |

The **Demo (virtual)** device needs no hardware, so the entire application — every
tab, metric and export — can be explored on any machine immediately.

> **Full reference:** a complete, illustrated manual lives in
> [`docs/manual.pdf`](docs/manual.pdf) (architecture, drivers, pixel↔wavelength
> mapping, calibration, the full signal-processing pipeline, every measurement,
> and the complete configuration reference). This README is the orientation +
> quick start; the manual is the deep reference of record.
---

# Screenshots

![GUI Plot Light Mode](https://github.com/doctormord/Pasco-PS-2600A-Spectrometer/blob/next/images/GUI_LightMode.png "GUI Plot Light Mode")
![GUI Plot Dark Mode](https://github.com/doctormord/Pasco-PS-2600A-Spectrometer/blob/next/images/GUI_DarkMode.png "GUI Plot Dark Mode")

![GUI Waterfall](https://github.com/doctormord/Pasco-PS-2600A-Spectrometer/blob/next/images/GUI_Waterfall.png "GUI Waterfall")

![GUI CIE / CRI](https://github.com/doctormord/Pasco-PS-2600A-Spectrometer/blob/next/images/GUI_CRI.png "GUI CIE Colorimetry")

![Zadig Driver Setup](https://github.com/doctormord/Pasco-PS-2600A-Spectrometer/blob/next/images/Zadig_USB.png "Zadig Driver Setup")

---

## Quick start (no hardware needed)

```bash
pip install -r requirements.txt

# Desktop GUI
python PS-2600A_Pro.py

# …or the web client (open http://localhost:8000)
python web_server.py
```

On first launch the app creates `spectrometer_config.json` (defaults) and
generates `reference_spectra.csv` (the reference-spectrum library). Pick
**Demo (virtual)** as the back-end, click **Connect**, and a live synthetic
spectrum starts streaming — no spectrometer required. To use real hardware, see
[Installation](#installation) and the per-device driver notes below.

---

## Feature overview

### Two front-ends, one core
- **Desktop** (`PS-2600A_Pro.py`) — PyQt6 + pyqtgraph.
- **Web** (`web_server.py`) — FastAPI + WebSocket server with a browser client
  (`static/index.html` / `app.js` / `styles.css`; a uPlot scope and canvas-drawn
  heatmap/CIE/radar). Works on phones, tablets and laptops on the same network.

Both call the identical engines (`color_science.py`, `filter_analysis.py`), so a
report exported from either front-end contains the same numbers.

### Tabs
- **Scope** — live trace with overlays: freeze-to-grey reference, difference vs.
  freeze, peak-hold envelope, persistence/afterglow, and a CSV/library background
  reference. Crosshair readout (pixel, wavelength, intensity) and draggable A/B
  measurement cursors.
- **Heatmap (waterfall)** — rolling time-lapse; **newest frame at the bottom,
  history scrolls upward**. Crosshair shows wavelength, ADC value and frame age.
- **Color · CRI** — four sub-tabs sharing one measurement: *Overview* (CCT, Duv,
  x/y, u'/v', SDCM, peak/dominant/centroid λ, FWHM, purity, S/P, RGB ratios),
  *CIE 1931* (chromaticity diagram + MacAdam ellipse), *Color Rendering* (CRI Ra,
  R1–R15), and *TM-30 · CQS* (IES TM-30-18 Rf/Rg, 16 hue-bin colour-vector
  graphic, 99-sample fidelity, local chroma shift; CQS Qa/Qf/Qg).
- **Replay** (web) — loads a heatmap CSV (or single-spectrum CSV) and replays it
  as a waterfall with a transport (play/scrub/speed/loop); current frame at the
  bottom, matching the live heatmap.
- **Filter** — optical-filter characterization: live transmission
  `T(λ) = sample / reference`, filter type, peak/centre λ, FWHM, 50 % edges,
  10–90 % edge steepness, blocking, optical density.

### Acquisition & processing (device-agnostic)
- Live streaming at the hardware-supported rate; per-device exposure bounds.
- Auto-exposure with configurable ADC target and emergency saturation drop-back.
- Frame averaging (noise ↓ √N) and a simple **Fast Preview** mode (short exposure
  scaled up) for a faster preview rate.
- Dark-current correction: optical-black (PASCO) or parametric, or median-of-
  darkest-pixels (HDX/LR-2T).
- Optional adaptive **temporal smoothing** (peak-preserving) and **spatial
  smoothing** (pixel-window); optional **spectral-response compensation**.
- Optional absolute **radiometric calibration** → lux / foot-candle readout.

### Colorimetry highlights
- Full CIE 1931 (2° observer): XYZ → x,y → u',v', CCT (McCamy) and Duv.
- **Off-locus CCT guard:** CCT is reported only for near-white sources
  (finite, 1000–25000 K, |Duv| ≤ 0.05); otherwise it shows “—” instead of an
  absurd value. x/y, Duv and the wavelengths remain valid.
- CRI Ra + R1–R15; IES TM-30-18 and CQS via the optional `colour-science` package.

### Filter highlights
- **Multi-frame baseline averaging** — *Set baseline* averages
  `filter_baseline_frames` frames (default 16) into the 100 % reference, cutting
  reference noise ~√N without losing spectral resolution. Optional display-only
  curve smoothing (`filter_display_smoothing`, default off) never touches the
  metrics, which always run on raw transmission.

### Export
- PDF / PNG / CSV reports for both colour and filter, from a shared renderer, each
  carrying a parameter header (spectrometer, serial, date/time, exposure,
  dark/baseline, peak ADC) so a saved report is self-describing.

---

## Project structure

Device-specific code stays in its `_device_*.py` file; signal/science is
device-agnostic; `spectrometer_core.py` holds no hardware code.

| Module | Responsibility |
|---|---|
| `PS-2600A_Pro.py` | Desktop entry point |
| `web_server.py` | Web entry point (FastAPI + uvicorn + WebSocket) |
| `device_manager.py` | `BaseSpectrometer` abstract base class + backend registry |
| `_device_pasco.py` | PASCO PS-2600A (WinUSB ctypes / pyusb) acquisition thread |
| `_device_ocean.py` | Ocean HDX (Ocean Binary Protocol over libusb) |
| `_device_lr2t.py` | ASEQ/Lasertack LR-2T (HID) |
| `_device_demo.py` | Hardware-free virtual device (numpy) |
| `_usb_linux.py` | pyusb backend for PASCO on macOS/Linux |
| `device_constants.py` | Device-agnostic timing / auto-exposure ratios; global bounds |
| `calibration_utils.py` | Polynomial pixel→λ mapping, line fitting, response gain |
| `fusion.py` | Adaptive temporal smoothing + emit stage |
| `processing.py` | Signal-processing pipeline (dark / average / spatial / response) |
| `color_science.py` | CIE XYZ, CCT, Duv, CRI, colour report/CSV, TM-30/CQS |
| `filter_analysis.py` | Optical-filter engine + filter report renderer |
| `gui_dashboard.py` | Main window: scope, heatmap, overlay bar, controls, routing |
| `gui_cie_tab.py` | Colour tab (Overview / CIE 1931 / Color Rendering / TM-30·CQS) |
| `gui_filter_tab.py` | Optical-filter tab |
| `gui_theme.py` | Palettes, stylesheets, custom widgets |
| `app_config.py` | JSON config singleton + `DEFAULT_CONFIG` |
| `static/` | Web front-end (`index.html`, `app.js`, `styles.css`) |
| `reference_spectra.csv` | Reference-spectrum library (semicolon-delimited) |
| `docs/` | The illustrated PDF manual + its LaTeX sources |

---

## Installation

See [`INSTALL.md`](INSTALL.md) for full per-platform instructions (Windows,
macOS Intel/Apple Silicon, Linux/Raspberry Pi). In brief:

```bash
pip install -r requirements.txt
```

Required: PyQt6, pyqtgraph, numpy, scipy. Optional but recommended:
**colour-science** (enables the TM-30/CQS sub-tab and `/api/tm30`),
**matplotlib** (CIE diagram fill + PDF/PNG reports). Hardware extras:
`pyusb` + `libusb-package` (Ocean HDX), `hidapi` (LR-2T); the web server also needs
`fastapi`, `uvicorn`, `websockets`.

### Per-device driver notes
- **PASCO PS-2600A (Windows)** — the vendor driver blocks raw USB; reassign the
  device to **WinUSB** with [Zadig](https://zadig.akeo.ie/) once (VID `0x0945`,
  PID `0x0002`). On macOS/Linux it uses libusb (`brew install libusb` /
  `apt install libusb-1.0-0`).
- **Ocean HDX** — `pip install pyusb libusb-package` (the latter bundles libusb,
  so no system install is needed). Linux: udev rule for VID `2457` / PID `2003`.
- **LR-2T** — `pip install hidapi`. Windows uses the default HID driver (no Zadig).
- **Demo** — nothing to install.

On Linux, install the bundled udev rule once for non-root USB access:

```bash
sudo cp udev/99-pasco-ps2600a.rules /etc/udev/rules.d/
sudo udevadm control --reload-rules && sudo udevadm trigger   # then replug
```

---

## Configuration

All settings live in `spectrometer_config.json`, created from
`app_config.DEFAULT_CONFIG` on first run; missing keys are filled from defaults at
load, and a corrupt file falls back to defaults instead of crashing. New code keys
must be added to `DEFAULT_CONFIG`. The **complete, annotated key reference** is in
`docs/manual.pdf` (the “Configuration reference” section). A few essentials:

| Key | Default | Description |
|---|---|---|
| `exposure_ms` | `20.0` | Main integration time |
| `auto_exposure` | `false` | Auto-exposure enabled |
| `frames_to_average` | `1` | Frame averaging count |
| `dark_correction` / `dark_mode` | `false` / `optical_black` | Dark subtraction + model |
| `spectral_response_compensation` | `false` | Apply response table |
| `radiometric_calibration_factor` | `0.0` | W/m²/nm per ADC (0 = uncalibrated) |
| `filter_baseline_frames` | `16` | Frames averaged into the filter 100 % reference |
| `filter_display_smoothing` | `0` | Display-only filter curve smoothing (0 = off) |
| `fast_preview_enabled` / `fast_preview_short_pct` | `false` / `10` | Fast Preview |

---

## Reference-spectrum library

`reference_spectra.csv` (semicolon-delimited; first column `Wavelength_nm`, every
further column one named spectrum) ships 25 illuminants: discharge lamps (H, Hg,
Na, Ne, Ar, He, Kr, Xe, Cd, Ne–Ar mix), fluorescent 2/3/4-band, a full LED set
(violet→red plus warm/neutral/cool white) and Solar AM1.5G. Any column can be
overlaid on the scope, and any column name can be used as the **Demo** device's
synthetic source (`demo_spectrum`). Delete the file to regenerate it.

---

## PASCO PS-2600A USB protocol (reverse-engineered)

The PASCO protocol has no public low-level documentation; it was reconstructed
from USB captures (Wireshark + USBPcap). This section is **PASCO-specific** — the
other back-ends use their own protocols in their `_device_*.py` files.

**Device:** VID `0x0945`, PID `0x0002`; WinUSB interface GUID
`{DEE824EF-729B-4A0E-9C14-B7117D33A817}`. `CreateFileW` must use
`FILE_FLAG_OVERLAPPED`, or `WinUsb_Initialize()` fails with `ERROR_ACCESS_DENIED`.

**Transfers:** control on EP `0x00` (`0x40` vendor OUT, `0xC0` vendor IN), bulk on
EP `0x82`. Commands: `0x01` init, `0x02` set integration time (µs as 32-bit split
across `wValue`/`wIndex`), `0x09` trigger.

**Acquisition cycle:** drain 28-byte bulk → trigger (`0x09`) → read 7360-byte
payload → trailing 28-byte drain → set next exposure (`0x02`). The 28-byte drain
packets are synchronisation acknowledgements that **must** be read every cycle, or
the pipeline desynchronises (alternating zero/ghost frames).

**Payload (7360 B):** bytes 0–31 dummy outputs; 32–57 thirteen optical-black
pixels; 58–63 margin; 64–7359 the 3648 active spectral pixels (uint16 LE,
0–4095). Thirty masked optical-black pixels track dark current identically to the
spectral region (correlation slope `1.00818`, offset `−0.6377`, R² `0.99994`) and
are the per-frame dark reference in optical-black mode.

**Limits:** integration 1 ms – 2.5 s (firmware signal-limiting beyond ~2.5 s; the
parametric dark model `bias + rate·t` fits R² ≈ 0.996 within range; bench unit
bias `61.57` ADC, rate `40.94` ADC/s).

---

## License

For educational, scientific and reverse-engineering research purposes. The PASCO
PS-2600A hardware and trademarks belong to PASCO Scientific; Ocean Insight, ASEQ
and Lasertack trademarks belong to their respective owners. This is an independent
community effort, not affiliated with any of them.


The PASCO PS-2600A hardware and associated trademarks belong to PASCO Scientific. This repository is an independent community reverse-engineering effort and is not affiliated with PASCO Scientific.

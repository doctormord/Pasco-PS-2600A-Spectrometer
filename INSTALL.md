# Installation Guide — Multi-Device Spectrum Analyzer

Platform-specific setup for Windows, macOS (Intel & Apple Silicon) and
Linux/Raspberry Pi, for all four back-ends (PASCO PS-2600A, Ocean HDX-UV-VIS,
ASEQ/Lasertack LR-2T, and the hardware-free Demo device).

> **Try it with zero hardware first.** Install the Python dependencies, launch
> the app, pick **Demo (virtual)** as the back-end and click **Connect** — the
> full UI streams a synthetic spectrum immediately. Then set up whichever real
> device you have using the driver notes below.

> For the complete technical reference (architecture, drivers, calibration,
> metrics, every config key) see [`docs/manual.pdf`](docs/manual.pdf).

---

## Prerequisites — all platforms

- Python 3.9+ (64-bit); 3.11+ recommended and fully tested.
- For real hardware, connect the device via a **data-capable** USB cable before
  scanning.

```bash
python --version        # if this prints Python 2.x, use python3 below
```

### Python dependencies

```bash
pip install -r requirements.txt
```

| Package | Required for | Notes |
|---|---|---|
| PyQt6, pyqtgraph, numpy, scipy | core (desktop) | always needed for the desktop GUI |
| fastapi, uvicorn, websockets | web client | `python web_server.py` |
| **colour-science** | TM-30/CQS | enables the *TM-30 · CQS* sub-tab and `/api/tm30`; everything else works without it |
| **matplotlib** | reports + CIE fill | PDF/PNG colour & filter reports, CIE diagram fill; CSV export works without it |
| pyusb + libusb-package | Ocean HDX | `libusb-package` bundles libusb — no system libusb needed |
| hid | LR-2T | Windows uses the default HID driver (no Zadig) |

The **Demo** device needs only numpy, so the app always runs even with no
hardware and no optional packages.

---

## Per-device drivers

### PASCO PS-2600A
The vendor driver blocks raw USB access and must be replaced once.

- **Windows — Zadig (critical):** download [Zadig](https://zadig.akeo.ie/), run as
  Administrator, `Options → List All Devices`, select **Spectrometer**
  (VID `0945`, PID `0002`), choose **WinUSB**, click **Replace Driver**. After
  this, PASCO SPARKvue/Capstone no longer see the device; restore the original
  driver any time via Device Manager. No admin rights are needed afterwards.
- **macOS / Linux:** uses libusb (see the platform sections below); no Zadig.

### Ocean HDX-UV-VIS

```bash
pip install pyusb libusb-package
```

`libusb-package` bundles the libusb backend, so no system libusb is required.
On Linux a udev rule for VID `2457` / PID `2003` may be needed for non-root
access (see the udev step under Linux).

### ASEQ / Lasertack LR-2T

```bash
pip install hid
```

On Windows the device uses the default HID driver — no Zadig. On Linux a udev
rule for VID `E220` / PID `0100` may be needed for non-root access.

### Demo (virtual)
Nothing to install. Always present in the back-end list.

---

## Windows

1. (PASCO only) install WinUSB via Zadig as above.
2. Python setup:
   ```powershell
   cd "C:\path\to\project"
   python -m venv venv
   venv\Scripts\activate
   pip install -r requirements.txt
   ```
3. Run the desktop GUI `python PS-2600A_Pro.py`, or the web client
   `python web_server.py` (then open `http://localhost:8000`). The web client
   does not require the WinUSB driver swap to start — but PASCO hardware still
   does, to be reachable.

---

## macOS — Apple Silicon (M1/M2/M3/M4)

```bash
# Homebrew (if needed)
/bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"
echo 'eval "$(/opt/homebrew/bin/brew shellenv)"' >> ~/.zprofile
eval "$(/opt/homebrew/bin/brew shellenv)"

brew install libusb python@3.13          # libusb is needed for pyusb (PASCO/Ocean)
cd /path/to/project
python3.13 -m venv venv && source venv/bin/activate
pip install -r requirements.txt
python PS-2600A_Pro.py
```

**Troubleshooting (`NoBackendError`):** the linker may not find libusb. Run:

```bash
export DYLD_LIBRARY_PATH=/opt/homebrew/lib:$DYLD_LIBRARY_PATH
python PS-2600A_Pro.py
```

Add the export to `~/.zprofile` to make it permanent.

---

## macOS — Intel

Same as Apple Silicon, but Homebrew lives in `/usr/local` (no PATH tweak needed)
and the troubleshooting export is:

```bash
export DYLD_LIBRARY_PATH=/usr/local/lib:$DYLD_LIBRARY_PATH
```

---

## Linux (Ubuntu / Debian / Raspberry Pi OS)

```bash
sudo apt update
sudo apt install python3 python3-venv python3-pip libusb-1.0-0   # Fedora: libusb1 · Arch: libusb
```

### udev rule (non-root USB access)

The bundled rule covers the USB devices. Without it, hardware is only reachable
as root.

```bash
sudo cp udev/99-pasco-ps2600a.rules /etc/udev/rules.d/
sudo udevadm control --reload-rules
sudo udevadm trigger
# unplug + replug the device
```

Add yourself to the `plugdev` group if needed (`sudo usermod -aG plugdev $USER`,
then log out/in). Verify a connected PASCO with `lsusb | grep 0945` (Ocean:
`2457`, LR-2T: `e220`).

### Python env & run

```bash
cd /path/to/project
python3 -m venv venv && source venv/bin/activate
pip install -r requirements.txt
python PS-2600A_Pro.py        # or: python web_server.py
```

**Raspberry Pi:** Pi OS (Bookworm) ships Python 3.11 which works. PyQt6 may take a
few minutes to compile on ARM; if it fails, `pip install --upgrade pip` then
`pip install pyqt6 --pre`. For headless use, prefer the web client.

---

## Web client layout note

`web_server.py` serves the front-end from a `static/` folder next to it
(`static/index.html`, `static/app.js`, `static/styles.css`). If you reorganise
the tree, keep those three files in `static/` or the server cannot find
`index.html`. See [`README_WEB.md`](README_WEB.md) for the full web setup,
API and offline-uPlot instructions.

---

## Verifying the installation

1. Launch the app (desktop or web).
2. Pick **Demo (virtual)** and **Connect** — a live synthetic spectrum should
   appear across all tabs with no hardware.
3. For real hardware, click **Scan** and confirm the device appears
   (PASCO: `vid_0945`/`pid_0002`; Ocean: `2457`/`2003`; LR-2T: `e220`/`0100`),
   then **Connect**.

If a real device never appears: re-check its driver (Windows PASCO → WinUSB in
Device Manager), the udev rule (Linux), or `DYLD_LIBRARY_PATH` (macOS), and try a
different data-capable cable/port.

---

## First launch

On first launch the app creates `spectrometer_config.json` (defaults) and
generates `reference_spectra.csv` (the reference-spectrum library). Delete
`reference_spectra.csv` to regenerate it after an upgrade. Configuration is saved
automatically as you change settings; a corrupt config file falls back to
defaults rather than crashing.

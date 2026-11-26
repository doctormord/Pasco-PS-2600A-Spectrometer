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

## Quick start (all platforms)

The program is started from Python only; there is no packaged executable.

```bash
python -m venv venv
# Windows: venv\Scripts\activate      macOS / Linux: source venv/bin/activate
pip install -r requirements.txt
python PS-2600A_Pro.py                       # desktop GUI
python PS-2600A_Pro.py --headless            # web UI + REST API, no Qt
python PS-2600A_Pro.py --data-dir PATH       # optional: use another data directory
```

`--data-dir` works with both modes (see [Data directory](#data-directory)).

---


> **Updating an existing installation (git):** up to this version
> `calibration_profiles.json` and `spectrometer_config.json` were part of the
> repository. A `git pull` removes them from the program folder. On the first
> start the migration restores them automatically from the git history into the
> data directory (logged as `restored_from_git` in `migration.log`). To be on the
> safe side, copy both files (and `dark_spectra.json`, `reference_sources.json`)
> out of the program folder before updating. Manual recovery:
> `git show 91d533b:calibration_profiles.json > calibration_profiles.json`.

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
| fastapi, uvicorn, websockets, pydantic | web client, REST API (`/api/v1`), headless server | desktop **Remote access**, `python PS-2600A_Pro.py --headless` |
| httpx | `examples/api_client.py`, MCP server, API tests | not needed to run the server |
| **mcp** (`pip install "mcp>=2.3"`) | optional: MCP server / Claude plugin | Python 3.10+; see [`mcp_server/README.md`](mcp_server/README.md) |
| **colour-science** | TM-30/CQS | enables the *TM-30 · CQS* sub-tab and `/api/tm30`; everything else works without it |
| **matplotlib** | reports + CIE fill | PDF/PNG colour & filter reports, CIE diagram fill; CSV export works without it |
| pyusb + libusb-package | Ocean HDX | `libusb-package` bundles libusb — no system libusb needed |
| hid | LR-2T | Windows uses the default HID driver (no Zadig) |

The **Demo** device needs only numpy, so the app always runs even with no
hardware and no optional packages.

**Headless server without PyQt** (for example a Raspberry Pi or a lab PC without
a display): `python PS-2600A_Pro.py --headless` never imports PyQt6, so
`PyQt6` and `pyqtgraph` can be left out of the environment. Install
`numpy scipy fastapi uvicorn websockets pydantic` (plus `matplotlib`,
`colour-science` and the device packages as needed).

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
   `python PS-2600A_Pro.py --headless` (then open `http://localhost:8000`;
   `python web_server.py` forwards to the same server). The web client
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
python PS-2600A_Pro.py        # or: python PS-2600A_Pro.py --headless
```

**Raspberry Pi:** Pi OS (Bookworm) ships Python 3.11 which works. PyQt6 may take a
few minutes to compile on ARM; if it fails, `pip install --upgrade pip` then
`pip install pyqt6 --pre`. For headless use, prefer the web client.

---

## Web client layout note

The server (`api.py`; started from the desktop app, `PS-2600A_Pro.py --headless`
or `web_server.py`) serves the front-end from a `static/` folder next to it
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

## Data directory

Runtime data never lives in the program folder. Settings, calibration data and
backups are kept per user:

| OS | Default location |
|---|---|
| Windows | `%APPDATA%\PS-2600A\` |
| macOS | `~/Library/Application Support/PS-2600A/` |
| Linux | `$XDG_CONFIG_HOME/ps-2600a/` (default `~/.config/ps-2600a/`) |

**Override** (highest priority first): the command-line option
`--data-dir PATH`, then the environment variable `PS2600A_DATA_DIR`. Both work
for the desktop app and for `--headless`.

**What lives there:**

- `spectrometer_config.json` — settings (created with defaults on first start)
- `calibration_profiles.json` — spectral calibration profiles (and the backup
  `calibration_profiles.v1.bak.json` of the old format)
- `dark_spectra.json` — stored per-pixel dark spectra per unit
- `reference_sources.json` — lamp library
- `linearity_profiles.json` — detector linearity records per unit (serial number)
- `protocols/` — calibration and linearity protocols (PDF/CSV)
- `backup/` — backups made by the migration
- `migration.json` (marker) and `migration.log`

**First-start migration.** If the data directory has no `migration.json`, the
application looks for runtime files in the program folder (the files above,
`*.bak.json`, `old_calib_data_*.json`, earlier `calib_*` / `linearity_*`
protocols) and **copies** them (never moves) into the data directory. An
existing file in the data directory is never overwritten; the program-folder
version is then kept next to it as `<name>.from-program-dir-<timestamp>.json`.
Every source file is also copied into `backup/migration-<timestamp>/`. The result
is recorded in `migration.json` and appended to `migration.log`. The originals
in the program folder stay untouched and are no longer used. Set
`PS2600A_SKIP_MIGRATION=1` to skip the migration.

**Templates.** The repository contains only `*.example.json` templates (empty
or default files); the real data files are not part of the repository and are
listed in `.gitignore`.

**Export folder.** Exports (CSV/PDF/PNG) default to `<Documents>/PS-2600A`
(the user's Documents folder), not to the program folder. It can be changed in
the settings (`export_dir`).

---

## First launch

On first launch the app creates the data directory (see above), migrates
existing runtime files from the program folder once, writes
`spectrometer_config.json` (defaults) and generates `reference_spectra.csv`
(the reference-spectrum library, program folder). Delete `reference_spectra.csv`
to regenerate it after an upgrade. Configuration is saved automatically as you
change settings; a corrupt config file falls back to defaults rather than
crashing.

## What is new in this version

- **Display modes** (Raw ADC, Corrected counts, Normalised, Count rate,
  Absolute) for scope and heatmap; spectrum and measurement exports and all evaluations use the corrected data; the time-lapse (heatmap, its export and autosave) records the display representation.
- **Spectral calibration**: darks before and after (time-interpolated),
  per-pixel checks with cause analysis, verification against the reference
  file with scope overlay.
- **Linearity calibration** per unit (LR-2T, PASCO); Ocean HDX: check only.
- **Data directory** with one-time migration (above).

Details: [`docs/manual.pdf`](docs/manual.pdf).

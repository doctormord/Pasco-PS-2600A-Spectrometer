# Web client — Multi-Device Spectrum Analyzer

A browser front-end for the same engine the desktop app uses. It streams live
spectra over a WebSocket and runs every analysis tab from any phone, tablet or
laptop on the same network. No GUI toolkit is required on the server — the web
server has no PyQt dependency.

| Front-end | Runs on | Start |
|---|---|---|
| **Desktop** (PyQt6) | Windows, macOS, Linux | `python PS-2600A_Pro.py` |
| **Web** (FastAPI + browser) | Windows, macOS, Linux, Raspberry Pi | `python web_server.py` → open `http://<host>:8000` |

Both front-ends drive the same device registry (`device_manager.py` +
`_device_*.py`) and the same science core (`color_science.py`,
`filter_analysis.py`, `processing.py`), so results match exactly. For the full
technical reference see [`docs/manual.pdf`](docs/manual.pdf).

---

## Running

```bash
pip install -r requirements.txt          # needs fastapi, uvicorn, websockets
python web_server.py                      # binds 0.0.0.0:8000 by default
```

CLI options:

```bash
python web_server.py --host 0.0.0.0 --port 8000 --reload
```

Open the printed LAN URL (e.g. `http://192.168.1.42:8000`) on any device on the
same WiFi, or `http://localhost:8000` locally. Pick a back-end (including
**Demo (virtual)**, which needs no hardware), connect, and the page starts
streaming.

> **Layout note:** `web_server.py` serves the front-end from a `static/`
> sub-folder next to it (`static/index.html`, `static/app.js`,
> `static/styles.css`) and mounts it at `/static`. Keep those three files in
> `static/` or the server will not find `index.html`.

---

## Tabs & features

- **Scope** — live spectrum with spectral-colour fill, peak labels and
  touch-draggable A/B measure cursors; top-bar crosshair readout (pixel /
  wavelength / intensity).
- **Heatmap** — rolling waterfall on a Canvas2D heatmap; **newest row at the
  bottom, scrolling upward**; hover/crosshair sets the readout and frame age.
- **Color · CRI** — *Overview* (CCT, Duv, x/y, u'/v', SDCM,
  peak/dominant/centroid λ, FWHM, purity, S/P, RGB ratios) with the CIE 1931
  chromaticity diagram (Planckian locus + iso-temperature markers) and the
  R1–R15 chart; plus a **TM-30 · CQS** sub-tab (Rf/Rg, 16-bin colour-vector
  graphic, 99-sample fidelity, local chroma shift, Qa/Qf/Qg) when
  `colour-science` is installed.
- **Replay** — load a heatmap CSV exported from the app (or a single-spectrum
  CSV) and replay it as a waterfall with a transport (play / scrub / speed /
  loop); the current frame is drawn at the bottom, earlier frames above.
- **Filter** — live transmission `T(λ) = sample / reference` with type,
  peak/centre λ, FWHM, 50 % edges, edge steepness, blocking and OD. *Set
  baseline* averages `filter_baseline_frames` frames (default 16) into the
  reference; optional display-only smoothing (`filter_display_smoothing`) never
  affects the metrics.

**Control sidebar** (collapsible sections): **Acquisition** (exposure, auto-
exposure, Fast Preview + short-frame %, frames to average, reference library),
**Smoothing** (adaptive temporal smoothing), **Display** (X range, auto/fixed Y,
snap-to-peak), **Processing** (dark correction, spatial smoothing, response
compensation). On phone-sized screens the sidebar slides in as a drawer.

**Export** — CSV download, PNG screenshot, and PDF/PNG/CSV colour & filter
reports (the report endpoints need `matplotlib`).

---

## HTTP / WebSocket API

`web_server.py` exposes:

| Endpoint | Purpose |
|---|---|
| `GET /` | Serves `static/index.html` |
| `WebSocket /ws/stream` | Live frame stream + config push (the client mirrors `Config.all()` into `State.cfg`) |
| `GET /api/status` | Server / connection status |
| `GET /api/backends` | Available device back-ends |
| `GET /api/devices` | Scan for connected devices |
| `POST /api/connect` · `POST /api/disconnect` | Connect / disconnect a device |
| `GET /api/config` · `POST /api/config` | Read / write configuration |
| `GET /api/references` · `GET /api/reference` | Reference-spectrum library list / fetch |
| `GET /api/heatmap` | Current waterfall history |
| `GET /api/export/csv` | Spectrum CSV export |
| `POST /api/filter` | Live filter metrics (wavelengths, reference, sample) |
| `POST /api/filter/report` · `POST /api/color/report` | PDF / PNG / CSV reports |
| `POST /api/tm30` | TM-30 + CQS (`{tm30, cqs, bin_colors}`); HTTP 503 without `colour-science` |

---

## Offline / no-internet operation

The page pulls three resources from a CDN: Google Fonts (Inter + JetBrains Mono),
and uPlot's CSS + JS from jsdelivr. To run fully offline, download the two uPlot
files into `static/` once and repoint the two `cdn.jsdelivr.net/...` URLs in
`static/index.html` to `/static/uPlot.min.css` and `/static/uPlot.iife.min.js`:

```bash
cd static
curl -O https://cdn.jsdelivr.net/npm/uplot@1.6.31/dist/uPlot.min.css
curl -O https://cdn.jsdelivr.net/npm/uplot@1.6.31/dist/uPlot.iife.min.js
```

Fonts fall back to system faces (Segoe UI / SF / Consolas) if Google Fonts is
unreachable.

---

## Raspberry Pi quick-start

```bash
sudo apt update
sudo apt install -y python3-pip python3-venv libusb-1.0-0
python3 -m venv ~/venv-spec && source ~/venv-spec/bin/activate
cd /path/to/project
pip install -r requirements.txt
# Linux non-root USB access (for real hardware):
sudo cp udev/99-pasco-ps2600a.rules /etc/udev/rules.d/
sudo udevadm control --reload-rules && sudo udevadm trigger   # then replug
python web_server.py
```

Then open `http://raspberrypi.local:8000` (or the printed IP) from any device on
the LAN. A Pi 4/5 streams the Color tab comfortably; on a Pi Zero 2 W, stick to
Scope + Heatmap or raise the exposure. The **Demo (virtual)** back-end lets you
verify the whole stack on the Pi with no spectrometer attached.

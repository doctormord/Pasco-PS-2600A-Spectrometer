# Web client and REST API — Multi-Device Spectrum Analyzer

A browser front-end, a versioned REST API (`/api/v1`) and an MCP server, all
on top of the same Qt-free engine (`engine.py`) the desktop app uses. The
browser streams live spectra over a WebSocket and runs every analysis tab from
any phone, tablet or laptop on the same network. The server has no PyQt
dependency and can run headless (no display, no Qt installed).

| Front-end | Runs on | Start |
|---|---|---|
| **Desktop** (PyQt6) | Windows, macOS, Linux | `python PS-2600A_Pro.py` (switch on **Remote access** to also serve the web UI / API) |
| **Headless server** (FastAPI + browser) | Windows, macOS, Linux, Raspberry Pi | `python PS-2600A_Pro.py --headless` → open `http://<host>:8000` |

Desktop, web, REST API and MCP use one engine, one device registry
(`device_manager.py` + `_device_*.py`) and one science core (`color_science.py`,
`filter_analysis.py`, `processing.py`), so results match exactly. One device is
opened by exactly one engine: the desktop app and a headless server cannot use
the same device at the same time. For the full technical reference see
[`docs/manual/manual.pdf`](docs/manual/manual.pdf); for the MCP server and the
Claude plugin see [`mcp_server/README.md`](mcp_server/README.md) and
[`claude-plugin/README.md`](claude-plugin/README.md).

---

## Running

```bash
pip install -r requirements.txt          # needs fastapi, uvicorn, websockets, httpx
```

### From the desktop app

Open the **Remote access** section of the sidebar and switch it on. The panel
shows the URL, port, access mode (local / LAN) and the token (see below). The
setting is remembered (`remote_enabled`), so the server starts again with the
app.

### Headless (no screen, no PyQt import)

```bash
python PS-2600A_Pro.py --headless [--host 127.0.0.1] [--lan] [--port 8000]
                       [--token T] [--require-token] [--no-connect]
                       [--backend "Demo (virtual)"] [--log-level warning]
```

| Option | Meaning |
|---|---|
| `--host H` / `--lan` | Bind address. Default `127.0.0.1`; `--lan` = `0.0.0.0` (all interfaces, token mandatory). |
| `--port P` | TCP port (default: `remote_port`, 8000). |
| `--token T` | Access token. Default: `remote_token` from the config; generated and stored on first use. |
| `--require-token` | Also require the token for a local (127.0.0.1) bind. |
| `--no-connect` | Do not auto-connect the first device found. |
| `--backend NAME` | Backend to auto-connect (default: first backend). |

The start banner prints the URL, the `/docs` URL and, when a token is required,
the token. `python web_server.py [--lan] [--host H] [--port P]` still works and
forwards to the same server (local only by default).

Open the printed URL on any device on the same network, or
`http://localhost:8000` locally. Pick a back-end (including **Demo (virtual)**,
which needs no hardware), connect, and the page starts streaming.

> **Layout note:** the server serves the front-end from the `static/`
> sub-folder (`static/index.html`, `static/app.js`, `static/styles.css`) and
> mounts it at `/static`. Keep those files in `static/`.

### Local vs. LAN

| Mode | Bind | Token |
|---|---|---|
| Local only (default) | `127.0.0.1` | optional (`remote_require_token_local`, `--require-token`) |
| LAN | `0.0.0.0` | always required |

### Access token

- **Desktop:** Remote access panel → **Show** / **Copy** / **Renew**. Renewing
  invalidates the old token immediately; clients must be updated.
- **Headless:** printed in the start banner; stored in `remote_token`.
- **Browser:** open `http://<host>:8000/?token=<token>`. The page stores the
  token in the browser and removes it from the address bar. Without a valid
  token the page stays read-only: live display and display-only settings work,
  nothing that steers the instrument.
- **REST / scripts:** `Authorization: Bearer <token>`. The WebSocket accepts
  `?token=`.

> **Security:** LAN access is plain, unencrypted HTTP. Anyone on the network
> can read the token. Use it only in a trusted lab network, or put a reverse
> proxy with TLS (nginx, Caddy, ...) in front. Never expose the port to the
> internet.

### Control and locking

Settings that change the acquisition (exposure, averaging, pause, dark, ...)
and jobs need **control**. Exactly one party holds it at a time: `local` (the
desktop GUI), `web`, or `remote:<client id>` (REST clients; header
`X-Client-Id`). A running job (measurement, series, dark, calibration) holds
control until it ends; a client can also take an explicit lease
(`POST /api/v1/control`).

- A conflicting command gets **HTTP 409** (`control_busy`).
- While a remote client or job has control, the desktop controls are locked and
  the status bar shows `Remote: <owner> ...` with **Cancel remote** (cancels
  the job / revokes the lease). The same actions are in the Remote access panel.
- The web UI shows the current control owner and is read-only while somebody
  else holds control.

---

## Tabs & features

- **Scope** — live spectrum with spectral-colour fill, peak labels and
  touch-draggable A/B measure cursors; top-bar crosshair readout (pixel /
  wavelength / intensity).
- **Heatmap** — rolling waterfall on a Canvas2D heatmap, buffered in the
  engine (buffer size, measure interval, clear, scrub, autosave to the server
  folder, CSV export of the visible / full buffer or one frame).
- **Color · CRI** — *Overview* (CCT, Duv, x/y, u'/v', SDCM,
  peak/dominant/centroid λ, FWHM, purity, S/P, RGB ratios, illuminance) with the
  CIE 1931 chromaticity diagram (Planckian locus + iso-temperature markers) and
  the R1–R15 chart; plus a **TM-30 · CQS** sub-tab (Rf/Rg, 16-bin colour-vector
  graphic, 99-sample fidelity, local chroma shift, Qa/Qf/Qg) when
  `colour-science` is installed.
- **Replay** — load a heatmap CSV exported from the app (or a single-spectrum
  CSV) and replay it as a waterfall with a transport (play / scrub / speed /
  loop).
- **Filter** — live transmission `T(λ) = sample / reference` with type,
  peak/centre λ, FWHM, 50 % edges, edge steepness, blocking and OD. *Set
  baseline* averages `filter_baseline_frames` frames (default 16) into the
  reference; optional display-only smoothing never affects the metrics.

**Control sidebar** (collapsible sections): **Acquisition** (exposure, auto-
exposure, Fast Preview + short-frame %, frames to average, reference library),
**Smoothing** (adaptive temporal smoothing), **Display** (X range, auto/fixed Y,
snap-to-peak, display unit, irradiance unit), **Processing** (dark correction
and modes, capture dark, spatial smoothing), **Calibration**. On phone-sized
screens the sidebar slides in as a drawer.

**Calibration in the browser** (same flow as the desktop assistant; every step
runs as a server-side job): *Calibration run…* (lamp calibration, relative or
absolute, and derivation of other optics; user steps such as cover / uncover are
shown in the browser and confirmed there; plausibility check; protocol PDF/CSV
download), *Calibration data* (profile table, select / copy / import / export,
lamps, stored darks), *Wavelength calibration…* (capture a line lamp, match, fit,
save with backup confirmation). Deleting profiles or dark spectra is not
offered over the API.

**Export** — CSV download, PNG screenshot, and PDF/PNG/CSV colour & filter
reports (the report endpoints need `matplotlib`). Files that the *server*
writes (autosave, saved reports, calibration protocols) go to its export folder
(`export_dir`); browser downloads go to the browser's download folder.

---

## HTTP / WebSocket API

Interactive documentation (OpenAPI / Swagger UI): **`/docs`** (schema:
`/openapi.json`). All `/api/v1` bodies and answers are typed (Pydantic).

### `/api/v1` — automation API

"Auth" means: needs the token whenever one is required (LAN, or
`remote_require_token_local`).

| Group | Endpoints | Auth |
|---|---|---|
| system | `GET /status`, `GET /backends`, `GET /devices` | no |
| device | `POST /connect`, `POST /disconnect`, `GET`/`PUT /acquisition`, `POST /auto-exposure` | PUT / POST |
| control | `GET /control`, `POST /control` (acquire / release lease) | POST |
| measurement | `POST /measure`, `POST /series`, `POST /dark` | yes |
| jobs | `GET /jobs`, `GET /jobs/{id}`, `POST /jobs/{id}/cancel`, `POST /jobs/{id}/continue` | POST |
| data | `GET /spectrum/latest`, `GET /measurements`, `GET /measurements/{id}` (+ `/export`), `GET /export/filename` | no |
| analysis | `POST /analyze/color`, `/analyze/peaks`, `/analyze/filter`, `/analyze/tm30` | no |
| reports | `POST /reports` (color / filter / measurement; pdf / png / csv) | no |
| calibration | profiles (list, select, copy, import, export), `GET /calibration/status`, references, darks, `POST /calibration/runs`, `POST /calibration/derivations`, `GET /calibration/runs/{job}/protocol` | select / copy / import / runs |
| wavelength | `/wavelength/lamps`, `/session`, `/capture`, `/match`, `/pairs`, `/fit`, `/save` | steering calls |
| heatmap | `GET`/`PUT /heatmap`, `/heatmap/clear`, `/heatmap/autosave`, `/heatmap/frame`, `/heatmap/export` | PUT / POST |

Errors are uniform: `{"error": {"code", "message", "details"}}` with HTTP 400
(`bad_request`), 401 (`unauthorized`), 404 (`not_found`), 409 (`control_busy`,
`needs_confirmation`), 422 (`validation_error`, `unprocessable`) and 503
(`not_connected`, `unavailable`).

Measuring (`POST /measure`): `integration_ms` (ms, `"auto"`, or null = current),
`frames`, `dark` = `auto` | `stored` | `capture` | `none`, optional `profile`,
`include_color`, `include_peaks`, `wait` (default true: answers with the
measurement; false: `202` + job). Job states: `queued`, `running`,
`waiting_for_user`, `done`, `failed`, `cancelled`. A job in `waiting_for_user`
(for example "cover the input") continues with `POST /jobs/{id}/continue`.

```bash
TOKEN=...   # from the app / start banner
curl -s http://127.0.0.1:8000/api/v1/status
curl -s -X POST http://127.0.0.1:8000/api/v1/connect -H "Authorization: Bearer $TOKEN" \
     -H "Content-Type: application/json" -d '{"backend": "Demo (virtual)"}'
curl -s -X POST http://127.0.0.1:8000/api/v1/measure -H "Authorization: Bearer $TOKEN" \
     -H "Content-Type: application/json" -d '{"integration_ms": "auto", "frames": 5}'
```

A complete Python example (`httpx` only) is `examples/api_client.py`.

### Legacy endpoints (unchanged, used by the web UI and existing scripts)

| Endpoint | Purpose |
|---|---|
| `GET /` | Serves `static/index.html` |
| `WebSocket /ws/stream` | Live frame stream + config push |
| `GET /api/status` | Server / connection status |
| `GET /api/backends` · `GET /api/devices` | Available back-ends / scan for devices |
| `POST /api/connect` · `POST /api/disconnect` | Connect / disconnect a device |
| `GET /api/config` · `POST /api/config` | Read / write configuration |
| `GET /api/references` · `GET /api/reference` | Reference-spectrum library list / fetch |
| `GET /api/heatmap` | Current waterfall history |
| `GET /api/export/csv` | Spectrum CSV export |
| `POST /api/filter` | Live filter metrics (wavelengths, reference, sample) |
| `POST /api/filter/report` · `POST /api/color/report` | PDF / PNG / CSV reports |
| `POST /api/tm30` | TM-30 + CQS (`{tm30, cqs, bin_colors}`); HTTP 503 without `colour-science` |
| `/api/calibration/...` | Profile list / status / select / copy / export / import, references, darks |

Steering legacy endpoints honour the same token rule.

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
python PS-2600A_Pro.py --headless --lan          # prints the URL and the token
```

Then open `http://raspberrypi.local:8000/?token=<token>` (or the printed IP)
from any device on the LAN. A Pi 4/5 streams the Color tab comfortably; on a Pi
Zero 2 W, stick to Scope + Heatmap or raise the exposure. The **Demo (virtual)**
back-end lets you verify the whole stack on the Pi with no spectrometer
attached. LAN traffic is unencrypted; see the security note above.

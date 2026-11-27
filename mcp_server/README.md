# PS-2600A MCP server

An [MCP](https://modelcontextprotocol.io) server that lets Claude (Claude Code, the Claude desktop app, any MCP client) operate the PS-2600A spectrometer application: connect a device, set exposure, measure, run series and calibrations, analyse colour and filters, create reports.

It is a thin layer over the application's REST API (`/api/v1`). It talks to the app **only over HTTP** (httpx), imports neither the engine nor PyQt, and can therefore run on another machine than the app.

## Requirements

```
pip install "mcp>=2.3" httpx
```

Minimum SDK version: `mcp>=2.3` (the server uses `mcp.server.mcpserver.MCPServer`).

## 1. Start the application with remote access

- Desktop: switch on **Remote access** in the sidebar. The panel shows the URL and the access token (copy it).
- Without screen: `python PS-2600A_Pro.py --headless` (see its `--help` for port, backend, token).

By default the API listens on `127.0.0.1:8000` only. For LAN access a token is mandatory.

## 2. Configuration (environment variables)

| Variable | Meaning | Default |
|---|---|---|
| `SPECTRO_API_URL` | Base URL of the app | `http://127.0.0.1:8000` |
| `SPECTRO_API_TOKEN` | Bearer token (required for LAN, or whenever the app requires a token) | none |
| `SPECTRO_CLIENT_ID` | Name of this client (control lease owner `remote:<id>`) | `mcp` |
| `SPECTRO_DATA_DIR` | Folder for full spectra, exports and reports | `<tempdir>/ps2600-mcp` |

Empty values count as "not set".

## 3. Set-up in Claude Code

### a) As a plugin (recommended: tools + agents + skills)

From a checkout of this repository:

```
claude --plugin-dir <repo>/claude-plugin
```

or via the local marketplace in the repository root:

```
claude plugin marketplace add <repo>
claude plugin install ps2600-spectrometer@ps2600-local
```

A marketplace installation copies the plugin, so the launcher no longer finds the repository next to it: set `SPECTRO_MCP_DIR=<repo>` (the folder that contains `mcp_server/`). Also set `SPECTRO_API_TOKEN` (and `SPECTRO_API_URL` if needed) in the environment Claude Code is started from, and `SPECTRO_PYTHON` if `python` is not the interpreter that has `mcp` and `httpx`.

### b) Directly as an MCP server

```
claude mcp add spectrometer \
  --env SPECTRO_API_URL=http://127.0.0.1:8000 \
  --env SPECTRO_API_TOKEN=<token> \
  --env SPECTRO_CLIENT_ID=claude \
  -- python <repo>/claude-plugin/scripts/run_mcp_server.py
```

(Or `python -m mcp_server` with the repository as working directory.)

## 4. Set-up in the Claude desktop app

Edit `claude_desktop_config.json`:

- Windows: `%APPDATA%\Claude\claude_desktop_config.json`
- macOS: `~/Library/Application Support/Claude/claude_desktop_config.json`

Use absolute paths:

```json
{
  "mcpServers": {
    "ps2600-spectrometer": {
      "command": "python",
      "args": ["C:\\path\\to\\Pasco-PS-2600A-Spectrometer\\claude-plugin\\scripts\\run_mcp_server.py"],
      "env": {
        "SPECTRO_API_URL": "http://127.0.0.1:8000",
        "SPECTRO_API_TOKEN": "<token from the app's Remote access panel>",
        "SPECTRO_CLIENT_ID": "claude-desktop",
        "SPECTRO_DATA_DIR": "C:\\Users\\<you>\\Documents\\ps2600-mcp"
      }
    }
  }
}
```

Restart the app afterwards. If `python` is not on the PATH, give the full path of the interpreter that has `mcp` and `httpx` installed.

## 5. Tools

| Tool | Purpose |
|---|---|
| `status` | Compact state: connection, exposure, calibration, control owner, active jobs with prompts |
| `list_devices` | Backends and devices |
| `connect` / `disconnect` | Open / close the spectrometer |
| `set_acquisition` | Exposure (ms), averaging, dark, units, filters, fusion |
| `auto_expose` | One-shot auto exposure to a target band |
| `capture_dark` | Capture a stored dark (job; user step on devices without shutter) |
| `measure` | Single measurement; compact result + CSV of the full spectrum |
| `start_series` / `job_status` / `cancel_job` / `continue_job` | Series and job control, including user steps |
| `get_spectrum` | Spectrum summary, thinned points (max 512), optional full CSV |
| `analyze_color` | CCT, Duv, CRI, lux (irradiance) or `luminance_cd_m2` (radiance; `quantity` says which), wavelengths |
| `find_peaks` | Peak detection |
| `analyze_filter` | Transmission of a filter (reference vs. sample) |
| `list_profiles` / `select_profile` / `calibration_status` | Calibration profiles |
| `list_references` / `start_calibration` | Calibration run (job with user steps); `list_references` shows `quantity` (irradiance / radiance) and a short `meta`; `optics_input` CC / sphere / bare / objective, an inadmissible combination is refused with the reason (422) |
| `start_sphere_characterization` | Integrating sphere as radiance reference with the CC (job with user steps `mount`, `cover`, `uncover`, `position` per height, `repeat`); result `status` saved / not_saved_checks_failed, `source_id`, d0, consistency, drift, uncertainty |

`status`, `calibration_status`, `get_spectrum` and `measure` report `quantity` (`irradiance`: unit `.../nm`, lux; `radiance`: unit `.../sr/nm`, `luminance_cd_m2`). Job results of calibration runs use the statuses `saved`, `saved_implausible`, `not_saved_implausible`, `not_saved_checks_failed` (a derivation also `drift_check_failed`).
| `export_measurement` | Measurement as CSV / JSON file |
| `create_report` | Colour / filter / measurement report (PDF, PNG, CSV) |

Resources: `spectrometer://status`, `spectrometer://config`, `spectrometer://profiles`.

There are no deleting tools. Every tool result is at most 8 KB of JSON (longer content is shortened and marked `"truncated": true`); full data goes to files in `SPECTRO_DATA_DIR` and the tool returns the path. A user step of a job (cover / uncover the light path) is reported as `waiting_for_user`; the assistant must ask the user and call `continue_job` only after the user confirmed.

## 6. Security

- The API speaks **unencrypted HTTP**. On a LAN anyone in the network can sniff the token. Use it only in trusted networks, or put a reverse proxy with TLS (nginx, Caddy, ...) in front and point `SPECTRO_API_URL` to the `https://` address.
- Keep the token secret; it allows steering the instrument (measure, calibration, profile selection). Regenerate it in the app if it leaked.
- The default bind is `127.0.0.1`. Do not expose the port to the internet.
- The server writes files only to `SPECTRO_DATA_DIR`.

## 7. Troubleshooting

- `Cannot connect to the spectrometer API`: the app is not running with Remote access on, or `SPECTRO_API_URL` is wrong.
- `unauthorized`: wrong or missing `SPECTRO_API_TOKEN`.
- `control_busy`: another client or the local GUI holds control of the device.
- Test the server interactively: `npx @modelcontextprotocol/inspector python -m mcp_server` (repository as working directory).

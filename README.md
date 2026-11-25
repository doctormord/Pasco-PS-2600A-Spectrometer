images go in here

## Remote access, REST API, MCP

The application can serve its own web interface and a versioned REST API
(`/api/v1`, OpenAPI at `/docs`): switch on **Remote access** in the desktop
sidebar, or run `python PS-2600A_Pro.py --headless` without a screen. The default
is local only (`127.0.0.1`); LAN access needs a token and is unencrypted HTTP.
An MCP server and a Claude Code plugin (agents and skills) let Claude run
measurements, calibrations and test reports through the same API.

- Web interface, REST API, token, control rules: [`README_WEB.md`](README_WEB.md)
- MCP server: [`mcp_server/README.md`](mcp_server/README.md)
- Claude plugin: [`claude-plugin/README.md`](claude-plugin/README.md)
- Python example client: [`examples/api_client.py`](examples/api_client.py)
- Installation and dependencies: [`INSTALL.md`](INSTALL.md)
- Hardware test checklist: [`docs/TEST_api_mcp.md`](docs/TEST_api_mcp.md)

## Start and data

Start from Python only (no packaged build): create a venv, `pip install -r
requirements.txt`, then `python PS-2600A_Pro.py` (desktop) or
`python PS-2600A_Pro.py --headless` (web/API); optional `--data-dir PATH`.
Settings, calibration profiles, dark spectra and protocols live in the per-user
data directory (`%APPDATA%\PS-2600A\` on Windows), with a one-time copy from the
program folder on first start: see [`INSTALL.md`](INSTALL.md#data-directory).

## Display, calibration, verification

- Display modes (Raw ADC, Corrected counts, Normalised, Count rate, Absolute)
  for scope and heatmap; spectrum and measurement exports and all evaluations use the corrected data; the time-lapse (heatmap, its export and autosave) records the display representation.
- Spectral calibration with dark before/after, per-pixel checks, cause model and
  verification against the lamp file (overlay in the scope).
- Detector linearity calibration per unit (LR-2T, PASCO; HDX check only).
- Details: [`docs/manual.pdf`](docs/manual.pdf); hardware retest checklist:
  [`docs/TEST_hardwaretest_fix.md`](docs/TEST_hardwaretest_fix.md).

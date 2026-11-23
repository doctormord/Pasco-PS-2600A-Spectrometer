# ps2600-spectrometer - Claude plugin

Plugin for [Claude Code](https://code.claude.com) that connects Claude to the PS-2600A spectrometer application.

Contents:

- `.mcp.json` - registers the MCP server `spectrometer` (tools: measure, calibrate, analyse, report)
- `agents/` - `measurement`, `calibration`, `test-report`
- `skills/` - `light-source-characterization`, `filter-transmission`, `stability-measurement`, `test-report-template`
- `scripts/run_mcp_server.py` - launcher that finds the `mcp_server` package
- `examples/claude_desktop_config.example.json` - configuration for the Claude desktop app

## Prerequisites

- Python 3.10+ with `pip install "mcp>=2.3" httpx`
- The PS-2600A app with **Remote access** switched on (or `python PS-2600A_Pro.py --headless`), and its access token
- A checkout of this repository (the plugin starts `mcp_server/` from it)

## Install

Directly from the checkout (for a session):

```
claude --plugin-dir <repo>/claude-plugin
```

Via the local marketplace (`<repo>/.claude-plugin/marketplace.json`):

```
claude plugin marketplace add <repo>
claude plugin install ps2600-spectrometer@ps2600-local
```

Note: a marketplace installation copies the plugin out of the repository. Then set `SPECTRO_MCP_DIR=<repo>` (the folder that contains `mcp_server/`), otherwise the launcher cannot find the server and says so on stderr.

Tool names inside Claude Code are `mcp__plugin_ps2600-spectrometer_spectrometer__<tool>` (for example `...__measure`).

## Environment variables

Set them in the environment Claude Code is started from.

| Variable | Meaning | Default |
|---|---|---|
| `SPECTRO_API_URL` | App URL | `http://127.0.0.1:8000` |
| `SPECTRO_API_TOKEN` | Access token (from the Remote access panel) | empty |
| `SPECTRO_CLIENT_ID` | Client name | `claude` |
| `SPECTRO_DATA_DIR` | Folder for spectra, exports and reports | `<tempdir>/ps2600-mcp` |
| `SPECTRO_PYTHON` | Python interpreter with `mcp` and `httpx` | `python` |
| `SPECTRO_MCP_DIR` | Folder containing `mcp_server/` (needed after a marketplace install) | checkout next to the plugin |

## Claude desktop app

See `examples/claude_desktop_config.example.json` and [`../mcp_server/README.md`](../mcp_server/README.md) (section "Set-up in the Claude desktop app"; `claude_desktop_config.json` locations for Windows and macOS). Agents and skills are Claude Code features; the desktop app only gets the tools.

## Safety

The tools can steer the instrument. Calibration user steps (cover / uncover the light path) always need the user's confirmation. Over a LAN the API is unencrypted HTTP - use a TLS reverse proxy (see the MCP server README).

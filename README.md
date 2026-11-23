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

"""Launcher for the PS-2600A MCP server.

Finds the `mcp_server` package and runs `mcp_server.server.main()` (stdio).
Search order for the directory that contains `mcp_server/`:
  1. env SPECTRO_MCP_DIR
  2. <plugin root>/..   (repo checkout, e.g. `claude --plugin-dir <repo>/claude-plugin`)
Empty environment values count as "not set" (the plugin's .mcp.json passes
empty strings for unset variables).
"""
import os
import sys


def _clean_env() -> None:
    for key in [k for k, v in os.environ.items()
                if k.startswith("SPECTRO_") and not v.strip()]:
        del os.environ[key]


def _find_package_dir() -> str | None:
    candidates = []
    env_dir = os.environ.get("SPECTRO_MCP_DIR", "").strip()
    if env_dir:
        candidates.append(os.path.abspath(os.path.expanduser(env_dir)))
    here = os.path.dirname(os.path.abspath(__file__))
    candidates.append(os.path.abspath(os.path.join(here, "..", "..")))
    for c in candidates:
        if os.path.isfile(os.path.join(c, "mcp_server", "server.py")):
            return c
    return None


def main() -> int:
    _clean_env()
    base = _find_package_dir()
    if base is None:
        sys.stderr.write(
            "ps2600 MCP launcher: cannot find the 'mcp_server' package.\n"
            "Set SPECTRO_MCP_DIR to the folder that contains mcp_server/ "
            "(your checkout of the PS-2600A repository), or run Claude Code with "
            "--plugin-dir <repo>/claude-plugin from that checkout.\n")
        return 2
    if base not in sys.path:
        sys.path.insert(0, base)
    try:
        from mcp_server.server import main as serve
    except ImportError as e:
        sys.stderr.write(
            f"ps2600 MCP launcher: import failed ({e}).\n"
            "Install the requirements:  pip install \"mcp>=2.3\" httpx\n")
        return 3
    serve()
    return 0


if __name__ == "__main__":
    sys.exit(main())

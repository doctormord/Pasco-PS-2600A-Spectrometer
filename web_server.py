"""
PASCO PS-2600A Spectrum Analyzer — standalone web server (compatibility entry).

The server itself is api.create_app() over the shared engine.Engine; this
module keeps the historic entry point and module-level names
(`app`, `state`, `_disconnect`, `_build_frame_payload`, `_fused_emit`) for
existing scripts and tests. New code should use

    python PS-2600A_Pro.py --headless [--host --port --token]

Usage (unchanged):
    python web_server.py               # 127.0.0.1:8000 (local only)
    python web_server.py --lan         # all interfaces, token required
    python web_server.py --host 0.0.0.0 --port 80
"""
from __future__ import annotations

import sys

import device_manager as dm
from api import ServerSettings, build_frame_payload, create_app
from engine import Engine, HEATMAP_DEFAULT_SIZE as HEATMAP_HISTORY_SIZE  # noqa: F401

dm.init_registry()

# Module-level engine + app (import-compatible with the former singleton).
engine = Engine()
state = engine                      # former ServerState → the Engine (same field names)
app = create_app(engine, ServerSettings(auto_connect=True))


def _disconnect() -> None:
    engine.disconnect(owner=None)


def _connect(device_path: str, backend_name: str | None = None) -> None:
    engine.connect(backend_name, device_path, owner=None)


def _build_frame_payload(downsample: int = 1) -> dict:
    return build_frame_payload(engine, downsample=downsample)


def _fused_emit(pixels, ob_mean: float, integration_us: int) -> None:
    engine.ingest_frame(pixels, ob_mean, integration_us)


def main(argv: list[str] | None = None) -> int:
    from server_runner import headless_main
    return headless_main(argv)


if __name__ == "__main__":
    sys.exit(main())

"""
server_runner.py — run the API server (uvicorn) in a background thread with a
clean start/stop, and the headless entry point (no Qt import).

    runner = ServerRunner(engine, settings)
    runner.start()            # returns once the socket is listening (or raises)
    runner.url                # http://127.0.0.1:8000
    runner.stop()             # graceful shutdown, joins the thread

    python PS-2600A_Pro.py --headless [--host H] [--port P] [--token T] [--lan]
"""
from __future__ import annotations

import argparse
import socket
import sys
import threading
import time

import uvicorn

from api import ServerSettings, create_app, is_loopback, new_token
from app_config import Config


def local_ip() -> str:
    """Best-effort LAN IP (no internet needed)."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("10.255.255.255", 1))
        return s.getsockname()[0]
    except Exception:                                            # noqa: BLE001
        return "127.0.0.1"
    finally:
        s.close()


def port_free(host: str, port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            s.bind(("0.0.0.0" if host in ("0.0.0.0", "") else host, int(port)))
            return True
        except OSError:
            return False


class ServerRunner:
    """uvicorn.Server in a daemon thread. Thread-safe start/stop."""

    def __init__(self, engine, settings: ServerSettings, *, log_level: str = "warning"):
        self.engine = engine
        self.settings = settings
        self.log_level = log_level
        self._server: uvicorn.Server | None = None
        self._thread: threading.Thread | None = None
        self._error: BaseException | None = None
        self._lock = threading.Lock()

    @property
    def running(self) -> bool:
        return bool(self._thread and self._thread.is_alive()
                    and self._server is not None and self._server.started)

    @property
    def url(self) -> str:
        host = self.settings.host
        shown = local_ip() if host in ("0.0.0.0", "") else host
        return f"http://{shown}:{self.settings.port}"

    @property
    def local_url(self) -> str:
        return f"http://127.0.0.1:{self.settings.port}"

    def start(self, timeout: float = 10.0) -> None:
        with self._lock:
            if self.running:
                return
            if self.settings.require_token and not self.settings.token:
                raise ValueError("A token is required for this bind address.")
            if not port_free(self.settings.host, self.settings.port):
                raise OSError(f"Port {self.settings.port} is already in use.")
            app = create_app(self.engine, self.settings)
            cfg = uvicorn.Config(app, host=self.settings.host, port=int(self.settings.port),
                                 log_level=self.log_level, lifespan="on")
            server = uvicorn.Server(cfg)
            server.install_signal_handlers = lambda: None     # not the main thread
            self._server = server
            self._error = None

            def run():
                try:
                    server.run()
                except BaseException as e:                       # noqa: BLE001
                    self._error = e
            self._thread = threading.Thread(target=run, daemon=True, name="api-server")
            self._thread.start()
            end = time.monotonic() + timeout
            while time.monotonic() < end:
                if server.started:
                    return
                if not self._thread.is_alive():
                    break
                time.sleep(0.05)
            err = self._error
            self._stop_locked()
            raise RuntimeError(f"Server did not start: {err!r}" if err else
                               "Server did not start in time.")

    def stop(self, timeout: float = 10.0) -> None:
        with self._lock:
            self._stop_locked(timeout)

    def _stop_locked(self, timeout: float = 10.0) -> None:
        srv, th = self._server, self._thread
        if srv is not None:
            srv.should_exit = True
        if th is not None:
            th.join(timeout=timeout)
            if th.is_alive() and srv is not None:
                srv.force_exit = True
                th.join(timeout=2.0)
        self._server = None
        self._thread = None


# ══════════════════════════════════════════════════════════════════════════
#  Headless entry point
# ══════════════════════════════════════════════════════════════════════════

def build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="PS-2600A spectrometer — headless API server")
    p.add_argument("--headless", action="store_true", help=argparse.SUPPRESS)
    p.add_argument("--host", default=None,
                   help="bind address (default 127.0.0.1; 0.0.0.0 = LAN, token required)")
    p.add_argument("--lan", action="store_true", help="bind all interfaces (= --host 0.0.0.0)")
    p.add_argument("--port", type=int, default=None, help="TCP port (default remote_port, 8000)")
    p.add_argument("--token", default=None,
                   help="API token (default: remote_token from the config; generated if empty)")
    p.add_argument("--require-token", action="store_true",
                   help="also require the token for a local (127.0.0.1) bind")
    p.add_argument("--no-connect", action="store_true",
                   help="do not auto-connect the first device on start")
    p.add_argument("--backend", default=None, help="backend to auto-connect (e.g. 'Demo (virtual)')")
    p.add_argument("--log-level", default="warning")
    return p


def headless_main(argv: list[str] | None = None) -> int:
    """Engine + API without any Qt import."""
    args = build_arg_parser().parse_args(argv)
    import device_manager as dm
    from engine import Engine

    host = "0.0.0.0" if args.lan else (args.host or "127.0.0.1")
    token = args.token or Config.get("remote_token", "") or ""
    if not token:
        token = new_token()
        Config.set("remote_token", token)
    settings = ServerSettings(
        host=host, port=int(args.port or Config.get("remote_port", 8000)), token=token,
        require_token_local=bool(args.require_token or Config.get("remote_require_token_local", False)),
        auto_connect=False, owns_engine=True)
    dm.init_registry()
    engine = Engine()
    if not args.no_connect:
        try:
            backend = args.backend or engine.list_backends()[0]
            devs = engine.scan(backend)
            if devs:
                engine.connect(backend, devs[0], owner=None)
                print(f"[server] Connected ({backend}): {devs[0]}")
            else:
                print(f"[server] No {backend} device found (connect via the API / browser).")
        except Exception as e:                                   # noqa: BLE001
            print(f"[server] Auto-connect skipped: {e!r}")

    shown = local_ip() if not is_loopback(host) else "127.0.0.1"
    print()
    print("  ┌─────────────────────────────────────────────────────")
    print("  │  PS-2600A spectrometer — headless server")
    print(f"  │  Web UI / API:  http://{shown}:{settings.port}")
    print(f"  │  API docs:      http://{shown}:{settings.port}/docs")
    if settings.require_token:
        print(f"  │  Token:         {token}")
        print("  │  (Authorization: Bearer <token>; web UI: ?token=<token>)")
    if settings.lan:
        print("  │  LAN access is unencrypted HTTP — use a TLS reverse proxy")
        print("  │  outside a trusted lab network.")
    print("  └─────────────────────────────────────────────────────")
    print()
    app = create_app(engine, settings)
    try:
        uvicorn.run(app, host=host, port=settings.port, log_level=args.log_level)
    finally:
        engine.close()
    return 0


if __name__ == "__main__":
    sys.exit(headless_main())

"""
api.py — FastAPI application over the shared Engine (Qt-free).

    app = create_app(engine, ServerSettings(...))

* Legacy endpoints under /api/... and the WebSocket /ws/stream keep their
  paths and response formats (web UI, existing scripts).
* The versioned automation API lives under /api/v1 (api_v1.py, Pydantic
  models in api_models.py), documented at /docs.
* Steering endpoints require `Authorization: Bearer <token>` when
  settings.require_token is set (always for a non-loopback bind). The
  WebSocket accepts the token as `?token=`.

Nothing here keeps device or pipeline state: everything is read from / sent
to the Engine (docs/PLAN_api_mcp.md §4).
"""
from __future__ import annotations

import asyncio
import hmac
import io
import json
import os
import secrets
import tempfile
import threading
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

import numpy as np
from fastapi import Depends, FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles

import calibration_profiles as calprof
import color_science as cs
import processing
from app_config import Config
from engine import (
    Engine, EngineError, EngineEvent, FrameEvent, NeedsConfirmation, json_safe,
)
from filter_analysis import analyze_filter, metrics_rows, render_report
from spectrometer_core import ensure_reference_library_exists, load_reference_library

STATIC_DIR = Path(__file__).parent / "static"
LOOPBACK_HOSTS = ("127.0.0.1", "localhost", "::1")


# ══════════════════════════════════════════════════════════════════════════
#  Settings / token
# ══════════════════════════════════════════════════════════════════════════

def new_token() -> str:
    return secrets.token_urlsafe(24)


def is_loopback(host: str) -> bool:
    return (host or "").strip() in LOOPBACK_HOSTS


@dataclass
class ServerSettings:
    host: str = "127.0.0.1"
    port: int = 8000
    token: str | None = None
    # Token check for steering endpoints. Forced on for a non-loopback bind.
    require_token_local: bool = False
    auto_connect: bool = False           # headless: connect the first device on start
    owns_engine: bool = False            # close the engine on shutdown
    serve_ui: bool = True
    extra: dict = field(default_factory=dict)

    @property
    def require_token(self) -> bool:
        return (not is_loopback(self.host)) or bool(self.require_token_local)

    @property
    def lan(self) -> bool:
        return not is_loopback(self.host)

    @classmethod
    def from_config(cls, cfg=Config, **overrides) -> "ServerSettings":
        """Settings from the remote_* config keys (token generated + stored
        on first use)."""
        tok = cfg.get("remote_token", "") or ""
        if not tok:
            tok = new_token()
            cfg.set("remote_token", tok)
        lan = bool(cfg.get("remote_lan", False))
        s = cls(host="0.0.0.0" if lan else "127.0.0.1",
                port=int(cfg.get("remote_port", 8000)),
                token=tok,
                require_token_local=bool(cfg.get("remote_require_token_local", False)))
        for k, v in overrides.items():
            if v is not None:
                setattr(s, k, v)
        return s


def _bearer(request_headers, query_token: str | None = None) -> str | None:
    auth = request_headers.get("authorization") or ""
    if auth.lower().startswith("bearer "):
        return auth[7:].strip()
    return query_token


def token_ok(settings: ServerSettings, presented: str | None) -> bool:
    if not settings.require_token:
        return True
    if not settings.token or not presented:
        return False
    return hmac.compare_digest(str(settings.token).encode("utf-8"),
                               str(presented).encode("utf-8"))


# ── Config exposed to / writable by remote clients (review K1/K2) ─────────
# Never sent to any client.
SECRET_KEYS = frozenset({"remote_token"})
# Never writable remotely: server security settings and data-file locations.
REMOTE_FORBIDDEN_KEYS = frozenset({
    "remote_token", "remote_lan", "remote_port", "remote_enabled",
    "remote_require_token_local", "calibration_profiles_file",
    "reference_sources_file", "dark_spectra_file", "heatmap_autosave_dir",
    "linearity_profiles_file",
})
# Writable remotely only when token protection is active (server paths).
REMOTE_TOKEN_ONLY_KEYS = frozenset({"export_dir"})


def public_config(cfg) -> dict:
    """Config for clients, without secrets."""
    return {k: v for k, v in cfg.all().items() if k not in SECRET_KEYS}


def check_remote_config(settings: "ServerSettings", payload: dict) -> None:
    """Raise PermissionError for keys a remote client must not set."""
    bad = sorted(k for k in payload if k in REMOTE_FORBIDDEN_KEYS)
    if not settings.require_token:
        bad += sorted(k for k in payload if k in REMOTE_TOKEN_ONLY_KEYS)
    if bad:
        raise PermissionError("Not settable remotely: " + ", ".join(bad))


def origin_ok(settings: "ServerSettings", headers) -> bool:
    """Cross-site protection: a browser Origin must be this server (same
    host:port) or loopback. Requests without Origin (scripts, MCP) pass."""
    origin = headers.get("origin")
    if not origin:
        return True
    from urllib.parse import urlsplit
    try:
        o = urlsplit(origin)
    except ValueError:
        return False
    host_hdr = (headers.get("host") or "").lower()
    if o.netloc.lower() == host_hdr:
        return True
    return (o.hostname or "") in LOOPBACK_HOSTS and str(o.port or "") == str(settings.port)


def host_ok(settings: "ServerSettings", headers) -> bool:
    """DNS-rebinding guard for loopback binds: Host must be a loopback name."""
    if settings.lan:
        return True
    host = (headers.get("host") or "").rsplit(":", 1)[0].strip("[]").lower()
    return host in LOOPBACK_HOSTS or host in ("testserver", "")


class ApiError(Exception):
    """Uniform /api/v1 error → {"error": {"code", "message", "details"}}."""

    def __init__(self, status: int, code: str, message: str, details: Any = None):
        super().__init__(message)
        self.status, self.code, self.message, self.details = status, code, message, details


def client_owner(request: Request) -> str:
    """Control owner of a remote caller: remote:<X-Client-Id or 'api'>."""
    cid = (request.headers.get("x-client-id") or "api").strip()[:40] or "api"
    return f"remote:{cid}"


# ══════════════════════════════════════════════════════════════════════════
#  Frame payload (legacy WS format)
# ══════════════════════════════════════════════════════════════════════════
_COUNT_UNITS = ("counts", "counts/s")


def _round_counts(v: float) -> float:
    return round(v, 2)


def _round_sig(v: float) -> float:
    return float(f"{v:.6g}")


def build_frame_payload(engine: Engine, ev: FrameEvent | None = None,
                        downsample: int = 1) -> dict:
    """Compact JSON payload for the live stream (unchanged format)."""
    if ev is None:
        ev = engine.last_frame
    if ev is not None and ev.pixels.size == engine.wavelength_array.size:
        px, wl, unit = ev.pixels, ev.wavelengths, ev.unit
        ob, t_us, fps, peaks, color = (ev.ob_mean, ev.integration_us, ev.fps,
                                       ev.peaks, ev.color)
        dark_source = ev.dark_source
    else:
        px, wl, unit = engine.current_pixels, engine.wavelength_array, engine.current_unit
        ob, t_us, fps = engine.latest_ob_mean, engine.latest_integration_us, engine.fps
        peaks, color, dark_source = engine.current_peaks, engine.color_metrics, engine.current_dark_source
    if downsample > 1 and px.size > downsample:
        px, wl = px[::downsample], wl[::downsample]
    px = np.asarray(px, dtype=float)
    unit = unit or "counts"
    rnd = _round_counts if unit in _COUNT_UNITS else _round_sig
    if engine.calib.active_v2:
        finite = np.isfinite(px)
        intens = [rnd(float(v)) if f else None for v, f in zip(px, finite)]
    else:
        px = np.nan_to_num(px, nan=0.0, posinf=0.0, neginf=0.0)
        intens = [rnd(float(v)) for v in px]
    wl = np.nan_to_num(np.asarray(wl, dtype=float), nan=0.0, posinf=0.0, neginf=0.0)
    calib_status = json_safe(dict(engine.calib.status))
    spec = engine.spec_range_nm
    return {
        "type": "frame",
        "wavelengths": [round(float(w), 2) for w in wl],
        "intensities": intens,
        "unit": unit,
        "unit_label": processing.unit_label(unit),
        "calibration": calib_status,
        "valid_range_nm": calib_status.get("valid_range_nm"),
        "valid_segments_nm": calib_status.get("valid_segments_nm"),
        "dark_source": dark_source,
        "ob_mean": round(float(ob), 3),
        "supports_ob": bool(engine.supports_ob),
        "spec_min": None if spec[0] is None else round(float(spec[0]), 2),
        "spec_max": None if spec[1] is None else round(float(spec[1]), 2),
        "dark_label": "OB mean" if engine.supports_ob else "Baseline",
        "integration_us": int(t_us),
        "integration_ms": round(t_us / 1000.0, 2),
        "fps": round(float(fps), 2),
        "peaks": json_safe(peaks),
        "sensor_temp_delta": engine.sensor_temp_delta(),
        "color": None if color is None else json_safe(
            {k: v for k, v in color.items()}),
        "lux_calibration": json_safe(ev.lux_calibration) if ev is not None else None,
    }


# ══════════════════════════════════════════════════════════════════════════
#  Broadcast hub (engine thread → asyncio loop → WebSocket clients)
# ══════════════════════════════════════════════════════════════════════════

class Hub:
    def __init__(self, engine: Engine, settings: ServerSettings):
        self.engine = engine
        self.settings = settings
        self.loop: asyncio.AbstractEventLoop | None = None
        self.clients: set[WebSocket] = set()
        self.authorized: set[WebSocket] = set()
        self.lock = threading.Lock()
        self._frame_pending = False
        self._unsub = engine.subscribe(self._on_event)

    def close(self) -> None:
        if self._unsub:
            self._unsub()
            self._unsub = None

    def _schedule(self, coro_fn, *args) -> bool:
        loop = self.loop
        if loop is None or not self.clients or loop.is_closed():
            return False
        try:
            asyncio.run_coroutine_threadsafe(coro_fn(*args), loop)
            return True
        except RuntimeError:
            return False

    def _on_event(self, ev: EngineEvent) -> None:
        if ev.kind == "frame":
            with self.lock:                  # coalesce: slow clients never queue up
                if self._frame_pending:
                    return
                self._frame_pending = True
            if not self._schedule(self._send_frame):
                with self.lock:
                    self._frame_pending = False
        elif ev.kind == "config":
            self._schedule(self.broadcast, {"type": "config", "config": ev.data})
        elif ev.kind == "connection":
            self._schedule(self.broadcast, {"type": "status", **json_safe(ev.data)})
        elif ev.kind in ("job", "control", "calibration", "autosave", "axis"):
            if ev.kind == "axis":
                self._schedule(self.broadcast, {"type": "axis", "data": {
                    "n": int(len(ev.data["wavelengths"]))}})
                return
            self._schedule(self.broadcast, {"type": ev.kind, "data": json_safe(ev.data)})

    async def _send_frame(self) -> None:
        with self.lock:
            self._frame_pending = False
        try:
            payload = build_frame_payload(self.engine)
        except Exception as e:                                   # noqa: BLE001
            print(f"[api] frame payload error: {e!r}")
            return
        await self.broadcast(payload)

    async def broadcast(self, payload: dict) -> None:
        raw = json.dumps(payload)
        dead = []
        for ws in list(self.clients):
            try:
                await ws.send_text(raw)
            except Exception:                                    # noqa: BLE001
                dead.append(ws)
        if dead:
            with self.lock:
                for ws in dead:
                    self.clients.discard(ws)
                    self.authorized.discard(ws)


# ══════════════════════════════════════════════════════════════════════════
#  App factory
# ══════════════════════════════════════════════════════════════════════════

def create_app(engine: Engine, settings: ServerSettings | None = None) -> FastAPI:
    settings = settings or ServerSettings()
    hub = Hub(engine, settings)

    @asynccontextmanager
    async def lifespan(application):
        hub.loop = asyncio.get_running_loop()
        ensure_reference_library_exists()
        if settings.auto_connect and not engine.is_connected:
            try:
                await asyncio.to_thread(_auto_connect, engine)
            except Exception as e:                               # noqa: BLE001
                print(f"[server] Auto-connect skipped: {e!r}")
        yield
        hub.close()
        if settings.owns_engine:
            await asyncio.to_thread(engine.close)

    app = FastAPI(
        title="PS-2600A Spectrometer API",
        version="1.0",
        description=("Remote control and automated measurements. Versioned API "
                     "under /api/v1; legacy web-UI endpoints under /api. Steering "
                     "endpoints need `Authorization: Bearer <token>` when token "
                     "protection is active (always for LAN access)."),
        lifespan=lifespan)
    app.state.engine = engine
    app.state.settings = settings
    app.state.hub = hub

    @app.middleware("http")
    async def _guard(request: Request, call_next):
        # DNS rebinding (loopback bind) and cross-site requests from a browser
        # page of another origin (CSRF) are refused.
        if not host_ok(settings, request.headers):
            return JSONResponse(status_code=400, content={"error": {
                "code": "bad_host", "message": "Invalid Host header.", "details": None}})
        if request.method not in ("GET", "HEAD", "OPTIONS") and not origin_ok(
                settings, request.headers):
            return JSONResponse(status_code=403, content={"error": {
                "code": "forbidden_origin", "message": "Cross-origin request refused.",
                "details": None}})
        return await call_next(request)

    def require_token(request: Request) -> None:
        if not token_ok(settings, _bearer(request.headers, request.query_params.get("token"))):
            raise HTTPException(401, "Missing or invalid token.",
                                headers={"WWW-Authenticate": "Bearer"})

    def legacy_err(e: EngineError):
        return HTTPException(e.status, str(e))

    if settings.serve_ui and STATIC_DIR.exists():
        app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

        @app.get("/", response_class=HTMLResponse, include_in_schema=False)
        async def root():
            return (STATIC_DIR / "index.html").read_text(encoding="utf-8")

    _add_legacy_routes(app, engine, hub, settings, require_token, legacy_err)
    try:
        from api_v1 import add_v1_routes
    except ImportError:
        add_v1_routes = None
    if add_v1_routes is not None:
        add_v1_routes(app, engine, settings, require_token)
    return app


def _app_version() -> str:
    try:
        from version import app_version
        return str(app_version())
    except Exception:                                            # noqa: BLE001
        return "unknown"


def _auto_connect(engine: Engine) -> None:
    names = engine.list_backends()
    default_backend = names[0] if names else None
    if not default_backend:
        return
    devices = engine.scan(default_backend)
    if devices:
        engine.connect(default_backend, devices[0], owner=None)
        print(f"[server] Auto-connected ({default_backend}): {devices[0]}")
    else:
        print(f"[server] No {default_backend} device found at startup "
              f"(connect from the browser).")


# ══════════════════════════════════════════════════════════════════════════
#  Legacy routes (/api/..., /ws/stream) — paths and formats unchanged
# ══════════════════════════════════════════════════════════════════════════

def _csv_response(rows, filename):
    import csv as _csv
    sio = io.StringIO()
    w = _csv.writer(sio)
    w.writerow(["Metric", "Value"])
    for k, v in rows:
        w.writerow([k, v])
    return Response(content=sio.getvalue(), media_type="text/csv",
                    headers={"Content-Disposition": f'attachment; filename="{filename}"'})


def _add_legacy_routes(app: FastAPI, engine: Engine, hub: Hub, settings: ServerSettings,
                       require_token, legacy_err) -> None:
    auth = [Depends(require_token)]
    WEB = "remote:web"

    @app.get("/api/status", tags=["legacy"])
    async def api_status():
        return {
            "connected": engine.is_connected,
            "device_path": engine.connected_device_path,
            "backend": engine.connected_backend_name or "—",
            "fps": round(engine.fps, 2),
            "integration_ms": round(engine.latest_integration_us / 1000.0, 2),
            "ob_mean": round(engine.latest_ob_mean, 3),
            "wavelength_min": float(engine.wavelength_array[0]),
            "wavelength_max": float(engine.wavelength_array[-1]),
            "pixel_count": int(engine.pixel_count),
            "heatmap_size": int(engine.heatmap_buffer.shape[1]),
            "token_required": settings.require_token,
            "control": engine.control_info(),
            "int_bounds_us": list(engine.int_bounds_us()),
            "autosave": engine.autosave_info(),
            "version": _app_version(),
        }

    @app.get("/api/references", tags=["legacy"])
    async def api_references():
        names, _, _ = load_reference_library()
        return {"names": list(names)}

    @app.get("/api/reference", tags=["legacy"])
    async def api_reference(name: str):
        names, _, columns = load_reference_library(engine.wavelength_array)
        if name not in columns:
            raise HTTPException(404, f"reference '{name}' not found")
        return {"name": name,
                "wavelengths": [float(x) for x in engine.wavelength_array],
                "values": [float(v) for v in columns[name]]}

    def _filter_inputs(payload):
        try:
            wl = np.asarray(payload["wavelengths"], dtype=float)
            ref = np.asarray(payload["reference"], dtype=float)
            smp = np.asarray(payload["sample"], dtype=float)
        except (KeyError, TypeError, ValueError):
            raise HTTPException(400, "expected numeric wavelengths, reference, sample arrays")
        try:
            res = analyze_filter(wl, ref, smp)
        except ValueError as e:
            raise HTTPException(400, str(e))
        res["rows"] = metrics_rows(res)
        return res, smp

    @app.post("/api/filter", tags=["legacy"])
    async def api_filter(payload: dict):
        res, _ = _filter_inputs(payload)
        return json_safe(res)

    @app.post("/api/filter/report", tags=["legacy"])
    async def api_filter_report(payload: dict):
        fmt = str(payload.get("format", "pdf")).lower()
        if fmt not in ("pdf", "png", "csv"):
            fmt = "pdf"
        res, smp = _filter_inputs(payload)
        if fmt == "csv":
            return _csv_response(metrics_rows(res, full=True), "filter_report.csv")
        buf = io.BytesIO()
        try:
            render_report(buf, res, fmt=fmt, meta=engine.report_meta(
                peak_adc=float(np.max(smp)) if smp.size else None))
        except ImportError:
            raise HTTPException(503, "matplotlib is not installed on the server")
        media = "application/pdf" if fmt == "pdf" else "image/png"
        return Response(content=buf.getvalue(), media_type=media, headers={
            "Content-Disposition": f'attachment; filename="filter_report.{fmt}"'})

    @app.post("/api/color/report", tags=["legacy"])
    async def api_color_report(payload: dict):
        fmt = str(payload.get("format", "pdf")).lower()
        if fmt not in ("pdf", "png", "csv"):
            fmt = "pdf"
        try:
            wl = np.asarray(payload["wavelengths"], dtype=float)
            inten = np.asarray(payload["intensities"], dtype=float)
        except (KeyError, TypeError, ValueError):
            raise HTTPException(400, "expected numeric wavelengths and intensities arrays")
        lux_cal = float(payload.get("lux_calibration", 0.0) or 0.0)
        if fmt == "csv":
            return _csv_response(cs.color_report_rows(wl, inten, lux_cal), "color_report.csv")
        buf = io.BytesIO()
        try:
            cs.render_color_report(buf, wl, inten, fmt=fmt, lux_calibration=lux_cal,
                                   meta=engine.report_meta(
                                       peak_adc=float(np.max(inten)) if inten.size else None))
        except ImportError:
            raise HTTPException(503, "matplotlib is not installed on the server")
        media = "application/pdf" if fmt == "pdf" else "image/png"
        return Response(content=buf.getvalue(), media_type=media, headers={
            "Content-Disposition": f'attachment; filename="color_report.{fmt}"'})

    @app.post("/api/tm30", tags=["legacy"])
    async def api_tm30(payload: dict):
        try:
            wl = np.asarray(payload["wavelengths"], dtype=float)
            inten = np.asarray(payload["intensities"], dtype=float)
        except (KeyError, TypeError, ValueError):
            raise HTTPException(400, "expected numeric wavelengths and intensities arrays")
        tm = cs.tm30_metrics(wl, inten)
        if tm is None:
            raise HTTPException(503, "TM-30 needs the optional 'colour-science' package on the server")
        return json_safe({"tm30": tm, "cqs": cs.cqs_metrics(wl, inten),
                          "bin_colors": cs.TM30_BIN_COLORS})

    @app.get("/api/backends", tags=["legacy"])
    async def api_backends():
        return {"backends": engine.list_backends(), "selected": engine.selected_backend()}

    @app.get("/api/devices", tags=["legacy"])
    async def api_devices(backend: Optional[str] = None):
        backend = backend or engine.selected_backend()
        devices = await asyncio.to_thread(engine.scan, backend)
        if engine.connected_device_path and engine.connected_device_path not in devices:
            devices.insert(0, engine.connected_device_path)
        return {"devices": devices, "backend": backend}

    @app.post("/api/connect", tags=["legacy"], dependencies=auth)
    async def api_connect(payload: dict):
        backend = payload.get("backend") or engine.selected_backend()
        try:
            info = await asyncio.to_thread(engine.connect, backend,
                                           payload.get("device_path"), owner=WEB)
        except EngineError as e:
            raise legacy_err(e)
        except Exception as e:                                   # noqa: BLE001
            raise HTTPException(500, str(e))
        return {"connected": True, "device_path": info["device_path"],
                "backend": info["backend"]}

    @app.post("/api/disconnect", tags=["legacy"], dependencies=auth)
    async def api_disconnect():
        try:
            await asyncio.to_thread(engine.disconnect, owner=WEB)
        except EngineError as e:
            raise legacy_err(e)
        return {"connected": False}

    @app.get("/api/config", tags=["legacy"])
    async def api_config_get():
        return public_config(engine.cfg)

    @app.post("/api/config", tags=["legacy"], dependencies=auth)
    async def api_config_set(payload: dict):
        try:
            check_remote_config(settings, payload)
        except PermissionError as e:
            raise HTTPException(403, str(e))
        try:
            await asyncio.to_thread(engine.configure, payload, owner=WEB)
        except EngineError as e:
            raise legacy_err(e)
        return public_config(engine.cfg)

    # ── calibration (legacy) ──────────────────────────────────────────────
    @app.get("/api/calibration/profiles", tags=["legacy"])
    async def api_calibration_profiles(include_old: int = 0):
        return profiles_payload(engine, bool(include_old), raw=True)

    @app.get("/api/calibration/status", tags=["legacy"])
    async def api_calibration_status():
        return engine.calibration_status()

    @app.post("/api/calibration/select", tags=["legacy"], dependencies=auth)
    async def api_calibration_select(payload: dict):
        try:
            res = await asyncio.to_thread(
                engine.select_profile, str(payload.get("selection", calprof.SEL_NONE)),
                confirm=bool(payload.get("confirm")), owner=WEB)
        except NeedsConfirmation as e:
            return JSONResponse(status_code=409, content=e.details)
        except EngineError as e:
            raise legacy_err(e)
        return json_safe(res)

    @app.post("/api/calibration/profile/delete", tags=["legacy"], dependencies=auth)
    async def api_calibration_profile_delete(payload: dict):
        try:
            engine.check_control(WEB)
        except EngineError as e:
            raise legacy_err(e)
        dev = engine.device_name
        pid = str(payload.get("id", "")).strip()
        name = str(payload.get("name", "")).strip()
        if not pid and not name:
            raise HTTPException(400, "Profile id required.")
        stored = (engine.cfg.get("response_correction_active", {}) or {}).get(dev)
        if pid:
            deleted = calprof.delete_profile_id(pid)
            was_active = pid in (stored, calprof.active_selection(dev))
        else:
            was_active = name == stored or calprof.active_selection(dev) == name
            deleted = calprof.delete_profile(dev, name)
        if dev and was_active:
            calprof.set_active_selection(dev, calprof.SEL_NONE)
        st = engine.rebuild_calibration()
        return json_safe({"ok": bool(deleted), "device": dev,
                          "selection": calprof.active_selection(dev), "status": st})

    @app.post("/api/calibration/profile/copy", tags=["legacy"], dependencies=auth)
    async def api_calibration_profile_copy(payload: dict):
        try:
            engine.check_control(WEB)
        except EngineError as e:
            raise legacy_err(e)
        dev, serial, _fp = engine.calibration_context()
        pid = str(payload.get("id", "")).strip()
        if not dev or not pid:
            raise HTTPException(400, "Device and profile id required.")
        try:
            cp = calprof.copy_profile_to_device(pid, serial, dev)
        except KeyError:
            raise HTTPException(404, f"Profile '{pid}' not found.")
        except ValueError as e:
            raise HTTPException(400, str(e))
        return {"ok": True, "id": cp["id"], "label": calprof.profile_label(cp)}

    @app.get("/api/calibration/profile/export", tags=["legacy"])
    async def api_calibration_profile_export(id: str):
        p = calprof.get_profile_by_id(id)
        if p is None:
            raise HTTPException(404, f"Profile '{id}' not found.")
        return Response(content=json.dumps(p, indent=1, allow_nan=False),
                        media_type="application/json", headers={
                            "Content-Disposition":
                            f'attachment; filename="calibration_profile_{id}.json"'})

    @app.post("/api/calibration/profile/import", tags=["legacy"], dependencies=auth)
    async def api_calibration_profile_import(payload: dict):
        try:
            engine.check_control(WEB)
        except EngineError as e:
            raise legacy_err(e)
        try:
            p = import_profile_payload(payload)
        except (ValueError, KeyError, TypeError, AttributeError) as e:
            raise HTTPException(400, f"Import failed: {e}")
        return {"ok": True, "id": p["id"], "label": calprof.profile_label(p)}

    @app.get("/api/calibration/references", tags=["legacy"])
    async def api_calibration_references():
        return reference_list()

    @app.get("/api/calibration/darks", tags=["legacy"])
    async def api_calibration_darks():
        import calibration_capture as cc
        dev, serial, _fp = engine.calibration_context()
        return json_safe(cc.list_darks(dev, serial) if dev else [])

    @app.get("/api/heatmap", tags=["legacy"])
    async def api_heatmap():
        h = engine.heatmap_snapshot()
        buf = h["buffer"]
        return {"wavelengths": [float(w) for w in h["wavelengths"]],
                "frames": [[float(v) for v in buf[:, i]] for i in range(buf.shape[1])],
                "saturation": engine.adc_saturation}

    @app.get("/api/export/csv", tags=["legacy"])
    async def api_export_csv(tab: str = "spectrum"):
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        if tab == "heatmap":
            body, name = heatmap_csv(engine), f"heatmap_{ts}.csv"
        else:
            body, name = spectrum_csv(engine), f"spectrum_{ts}.csv"
        return Response(content=body, media_type="text/csv",
                        headers={"Content-Disposition": f'attachment; filename="{name}"'})

    # ── WebSocket ─────────────────────────────────────────────────────────
    @app.websocket("/ws/stream")
    async def ws_stream(ws: WebSocket):
        if not origin_ok(settings, ws.headers) or not host_ok(settings, ws.headers):
            await ws.close(code=1008)                # cross-site WebSocket hijacking
            return
        await ws.accept()
        hub.loop = asyncio.get_running_loop()
        authorized = token_ok(settings, _bearer(ws.headers, ws.query_params.get("token")))
        with hub.lock:
            hub.clients.add(ws)
            if authorized:
                hub.authorized.add(ws)
        try:
            backend = engine.selected_backend()
            devices = await asyncio.to_thread(engine.scan, backend)
            if engine.connected_device_path and engine.connected_device_path not in devices:
                devices.insert(0, engine.connected_device_path)
            hello = {
                "type": "hello",
                "connected": bool(engine.is_connected),
                "device_path": engine.connected_device_path,
                "backend": engine.connected_backend_name or backend,
                "backends": list(engine.list_backends()),
                "devices": list(devices),
                "config": json_safe(public_config(engine.cfg)),
                "wavelengths_full": [round(float(w), 2) for w in engine.wavelength_array[::4]],
                "token_required": settings.require_token,
                "authorized": authorized,
                "control": engine.control_info(),
                "int_bounds_us": list(engine.int_bounds_us()),
            }
            try:
                await ws.send_text(json.dumps(hello))
            except Exception as e:                               # noqa: BLE001
                print(f"[server] hello send failed: {e!r}; sending minimal hello")
                await ws.send_text(json.dumps({
                    "type": "hello", "connected": bool(engine.is_connected),
                    "device_path": engine.connected_device_path, "backend": backend,
                    "backends": list(engine.list_backends()), "devices": list(devices),
                    "config": {}, "wavelengths_full": []}))
            while True:
                msg = await ws.receive_text()
                try:
                    data = json.loads(msg)
                except Exception:                                # noqa: BLE001
                    continue
                t = data.get("type")
                if t == "ping":
                    await ws.send_text(json.dumps({"type": "pong"}))
                    continue
                if t == "auth":
                    authorized = token_ok(settings, data.get("token"))
                    with hub.lock:
                        (hub.authorized.add if authorized else hub.authorized.discard)(ws)
                    await ws.send_text(json.dumps({"type": "auth", "authorized": authorized}))
                    continue
                if t in ("set_config", "pause", "connect", "disconnect") and not authorized:
                    await ws.send_text(json.dumps({"type": "error", "code": "unauthorized",
                                                   "message": "Token required."}))
                    continue
                try:
                    if t == "set_config":
                        payload = data.get("payload", {}) or {}
                        check_remote_config(settings, payload)
                        await asyncio.to_thread(engine.configure, payload, owner=WEB)
                    elif t == "pause":
                        engine.set_paused(bool(data.get("paused", False)), owner=WEB)
                    elif t == "connect":
                        await asyncio.to_thread(
                            engine.connect, data.get("backend") or engine.selected_backend(),
                            data.get("device_path"), owner=WEB)
                        await ws.send_text(json.dumps({"type": "status", "connected": True}))
                    elif t == "disconnect":
                        await asyncio.to_thread(engine.disconnect, owner=WEB)
                        await ws.send_text(json.dumps({"type": "status", "connected": False}))
                except EngineError as e:
                    await ws.send_text(json.dumps({"type": "error", "code": e.code,
                                                   "message": str(e)}))
                except PermissionError as e:
                    await ws.send_text(json.dumps({"type": "error", "code": "forbidden",
                                                   "message": str(e)}))
                except Exception as e:                           # noqa: BLE001
                    await ws.send_text(json.dumps({"type": "error", "message": str(e)}))
        except WebSocketDisconnect:
            pass
        except Exception as e:                                   # noqa: BLE001
            print(f"[server] websocket handler error: {e!r}")
        finally:
            with hub.lock:
                hub.clients.discard(ws)
                hub.authorized.discard(ws)


# ══════════════════════════════════════════════════════════════════════════
#  Shared helpers (legacy + v1)
# ══════════════════════════════════════════════════════════════════════════

def _profile_item(engine: Engine, p: dict, dev, serial, fp) -> dict:
    st = calprof.profile_status(p, dev, serial, fp, engine.nl_active())
    return {"value": p["id"], "label": calprof.profile_label(p),
            "state": st["state"], "message": st["message"],
            "type": p.get("type"), "version": p.get("version"),
            "created": p.get("created"),
            "valid_range_nm": p.get("valid_range_nm"),
            "valid_segments_nm": p.get("valid_segments_nm"),
            "plausible": not calprof.is_implausible(p),
            "unit": p.get("unit")}


def profiles_payload(engine: Engine, include_old: bool = False, *, raw: bool = False) -> dict:
    """Selectable response-correction options of the active device (None /
    Device default / saved profiles of THIS unit) plus copy candidates of
    other units. raw=True adds the legacy 'profiles' field (/api only)."""
    dev, serial, fp = engine.calibration_context()
    has_default = getattr(engine.acquisition, "RESPONSE_TABLE", None) is not None
    items = [{"value": calprof.SEL_NONE, "label": calprof.SEL_NONE,
              "state": "builtin", "message": "", "type": None, "version": None,
              "created": None, "valid_range_nm": None, "unit": "counts"}]
    if has_default:
        items.append({"value": calprof.SEL_DEVICE_DEFAULT,
                      "label": calprof.SEL_DEVICE_DEFAULT, "state": "builtin",
                      "message": "", "type": None, "version": None,
                      "created": None, "valid_range_nm": None, "unit": None})
    profs = (calprof.profiles_for_device(dev, serial, fp, nl_active=engine.nl_active(),
                                         include_old=include_old) if dev else [])
    items += [_profile_item(engine, p, dev, serial, fp) for p in profs]
    others = calprof.profiles_other_units(dev, serial) if dev else []
    sel = calprof.active_selection(dev) if dev else calprof.SEL_NONE
    out = {
        "device": dev, "serial": serial, "fingerprint": fp,
        "selection": sel, "selection_label": calprof.selection_label(sel),
        "status": engine.calibration_status(), "items": items,
        "other_units": [{"id": p["id"], "label": calprof.profile_label(p),
                         "serial": (p.get("device") or {}).get("serial")}
                        for p in others],
    }
    if raw:
        out["profiles"] = calprof.list_profiles(dev) if dev else []
    return json_safe(out)


def import_profile_payload(payload: dict) -> dict:
    tmp = None
    try:
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False,
                                         encoding="utf-8") as f:
            json.dump(payload, f)
            tmp = f.name
        return calprof.import_profile(tmp)
    finally:
        if tmp:
            try:
                os.remove(tmp)
            except OSError:
                pass


def reference_list() -> list[dict]:
    import reference_sources as rs
    return json_safe([{"id": s.id, "name": s.name, "serial": s.serial, "unit": s.unit,
                       "valid_min_nm": s.valid_min_nm, "valid_max_nm": s.valid_max_nm,
                       "input_optic": s.input_optic, "builtin": bool(s.builtin)}
                      for s in rs.ReferenceLibrary().list()])


def heatmap_csv(engine: Engine) -> str:
    h = engine.heatmap_snapshot()
    buf = io.StringIO()
    buf.write(";".join(["Frame"] + [f"{wl:.2f}" for wl in h["wavelengths"]]) + "\n")
    for fi in range(h["buffer"].shape[1]):
        buf.write(";".join([str(fi)] + [f"{v:.2f}" for v in h["buffer"][:, fi]]) + "\n")
    return buf.getvalue()


def spectrum_csv(engine: Engine) -> str:
    cfg = engine.cfg
    buf = io.StringIO()
    buf.write(f"# Optical Black mean (ADC);{engine.latest_ob_mean:.3f}\n")
    buf.write(f"# Integration time (ms);{engine.latest_integration_us / 1000.0:.1f}\n")
    buf.write(f"# Dark correction;{('ON' if cfg.get('dark_correction') else 'OFF')}\n")
    buf.write(f"# Dark mode;{cfg.get('dark_mode')}\n")
    buf.write(f"# Response compensation;{('ON' if cfg.get('spectral_response_compensation') else 'OFF')}\n")
    unit = engine.current_unit or "counts"
    if engine.calib.active_v2:
        st = engine.calib.status
        buf.write(f"# Unit;{unit}\n")
        buf.write(f"# Calibration profile;{st.get('label', '')}\n")
        vr = st.get("valid_range_nm")
        if vr:
            buf.write(f"# Valid range (nm);{float(vr[0]):.1f}–{float(vr[1]):.1f}\n")
        segs = st.get("valid_segments_nm")
        if segs:
            buf.write("# Valid segments (nm);" + ", ".join(
                f"{float(a):.1f}–{float(b):.1f}" for a, b in segs) + "\n")
        if st.get("implausible"):
            buf.write("# Plausibility;FAILED (saved on request)\n")
    col = "ADC_Counts" if unit == "counts" else f"Value_{unit}"
    buf.write(f"Pixel_ID;Wavelength_nm;{col}\n")
    for i, (wl, v) in enumerate(zip(engine.wavelength_array, engine.current_pixels)):
        if not np.isfinite(v):
            buf.write(f"{i};{wl:.2f};\n")
        elif unit == "counts":
            buf.write(f"{i};{wl:.2f};{v:.2f}\n")
        else:
            buf.write(f"{i};{wl:.2f};{v:.6g}\n")
    return buf.getvalue()

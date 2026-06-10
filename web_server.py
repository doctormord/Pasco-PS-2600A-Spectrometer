"""
PASCO PS-2600A Spectrum Analyzer — Web GUI server.

Runs the spectrometer hardware in a background thread and serves a
responsive HTML5 frontend over WiFi. Works on Windows, Linux, and
Raspberry Pi.

Usage:
    python web_server.py            # bind 0.0.0.0:8000, all interfaces
    python web_server.py --port 80  # custom port

Then open  http://<host-ip>:8000  from any phone, tablet, or laptop on
the same WiFi network.
"""
from __future__ import annotations
import asyncio
import io
import json
import os
import sys
import time
import socket
import argparse
import threading
from collections import deque
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

import numpy as np

from fastapi import FastAPI, WebSocket, WebSocketDisconnect, HTTPException
from fastapi.responses import (
    HTMLResponse, JSONResponse, PlainTextResponse, Response, FileResponse,
)
from fastapi.staticfiles import StaticFiles
import uvicorn

# ── Project modules ──
import spectrometer_core as core
from spectrometer_core import (
    HEATMAP_HISTORY_SIZE,
    DARK_MODE_OPTICAL_BLACK, DARK_MODE_PARAMETRIC,
    ensure_reference_library_exists, load_reference_library,
)
from _device_pasco import (
    PASCO_PIXEL_COUNT            as DEFAULT_PIXEL_COUNT,
    PASCO_START_INTEGRATION_US   as START_INTEGRATION_TIME_US,
    PASCO_MIN_INTEGRATION_US     as MIN_INTEGRATION_TIME_US,
    PASCO_MAX_INTEGRATION_US     as MAX_INTEGRATION_TIME_US,
    PASCO_ADC_SATURATION         as DEFAULT_ADC_SATURATION,
    PASCO_OB_TEMP_REFERENCE_ADC  as OB_TEMP_REFERENCE_ADC,
    PASCO_DARK_BIAS_ADC          as DEFAULT_DARK_BIAS_ADC,
    PASCO_DARK_RATE_ADC_PER_SEC  as DEFAULT_DARK_RATE_ADC_PER_SEC,
    PASCO_OB_CORRECTION_SLOPE    as DEFAULT_OB_CORRECTION_SLOPE,
    PASCO_OB_CORRECTION_OFFSET   as DEFAULT_OB_CORRECTION_OFFSET,
    PASCO_SPECTRAL_RESPONSE_GAIN as DEFAULT_SPECTRAL_RESPONSE_GAIN,
    PASCO_WAVELENGTH_ARRAY       as DEFAULT_WAVELENGTH_ARRAY,
    free_usb_resources,
)
import device_manager as dm
from device_manager import BaseSpectrometer
from app_config import Config
import processing
import color_science as cs
import fusion
from filter_analysis import analyze_filter, metrics_rows, render_report
from calibration_utils import build_response_gain
import calibration_profiles as calprof


# ============================================================
# Helpers
# ============================================================
def _json_safe(obj):
    """Recursively coerce numpy scalars/arrays and non-finite floats into
    JSON-serializable Python types so json.dumps never raises."""
    if isinstance(obj, dict):
        return {k: _json_safe(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_json_safe(v) for v in obj]
    if isinstance(obj, np.ndarray):
        return [_json_safe(v) for v in obj.tolist()]
    if isinstance(obj, np.generic):
        obj = obj.item()
    if isinstance(obj, float):
        return obj if np.isfinite(obj) else None
    return obj


# ============================================================
# State (singleton)
# ============================================================
class ServerState:
    def __init__(self):
        self.acquisition: BaseSpectrometer | None = None
        self.connected_device_path:  str | None = None
        self.connected_backend_name: str | None = None
        self.frames_avg = max(1, int(Config.get("frames_to_average", 1)))
        self.spectrum_history: deque[np.ndarray] = deque(maxlen=self.frames_avg)

        # ── Active device parameters ────────────────────────────────────
        # These describe the *currently connected* device.  Until a device
        # connects they fall back to the PASCO constants so the dropdowns,
        # status, and a hello frame all work before connection.  _connect()
        # repopulates them from the live backend; _disconnect() restores
        # these defaults.
        self.pixel_count:      int        = int(DEFAULT_PIXEL_COUNT)
        self.wavelength_array: np.ndarray = DEFAULT_WAVELENGTH_ARRAY.copy()
        self.response_gain:    np.ndarray = DEFAULT_SPECTRAL_RESPONSE_GAIN
        self.ob_slope:         float      = DEFAULT_OB_CORRECTION_SLOPE
        self.ob_offset:        float      = DEFAULT_OB_CORRECTION_OFFSET
        self.adc_saturation:   float      = float(DEFAULT_ADC_SATURATION)
        self.supports_ob:      bool       = True

        # Latest processed state (held for WS broadcast + export)
        self.current_pixels: np.ndarray = np.zeros(self.pixel_count)
        self.current_peaks: list[dict] = []
        self.latest_ob_mean: float = 0.0
        self.latest_integration_us: int = START_INTEGRATION_TIME_US

        # ── Fusion stage (matches the desktop GUI data path) ────────────
        # The driver tags frames long/short/standard; fusion owns the
        # display decision and emits a clean stream via _fused_emit.
        # _fused_emit is defined below this class, so bind it lazily (the
        # lambda resolves the name at emit time, not at construction time).
        self.fusion = fusion.SpectrumFusion(
            emit_callback=lambda px, dk, it: _fused_emit(px, dk, it))
        self.fusion.configure(**{k: Config.get(k) for k in fusion.DEFAULTS
                                 if Config.get(k, None) is not None})

        # Heatmap rolling buffer
        self.heatmap_linear_waves = np.linspace(
            self.wavelength_array[0], self.wavelength_array[-1],
            self.pixel_count)
        self.heatmap_buffer = np.zeros((self.pixel_count, HEATMAP_HISTORY_SIZE))
        # FPS rolling window
        self._fps_history: deque[float] = deque(maxlen=20)
        self._last_frame_time: float | None = None
        self.fps: float = 0.0
        # Push frames to all connected WebSocket clients
        self.clients: set[WebSocket] = set()
        self.clients_lock = threading.Lock()
        # Color metrics (computed every Nth frame to save CPU)
        self.color_metrics_period_frames = 8
        self._frame_counter = 0
        self.color_metrics: dict | None = None
        # Event loop for cross-thread async scheduling
        self.loop: asyncio.AbstractEventLoop | None = None

    @property
    def is_connected(self) -> bool:
        # The backend object (BaseSpectrometer) is the connection handle; it
        # owns its own acquisition thread internally.  Treat "has a live
        # backend + a device path" as connected.  (The old code called
        # acquisition.is_alive(), which only exists on threading.Thread, not
        # on the backend — that raised AttributeError on /api/status.)
        return self.acquisition is not None and self.connected_device_path is not None


state = ServerState()

# Fast-preview / fusion diagnostic toggle. Off unless FP_DEBUG=1 in the env.
_FP_DEBUG = os.environ.get("FP_DEBUG", "") not in ("", "0", "false", "False")


# ============================================================
# Frame handling (runs in the acquisition thread)
# ============================================================
def on_frame(raw_pixels: np.ndarray, ob_mean: float, integration_us: int,
             kind: str = fusion.FRAME_STANDARD, ratio: float = 1.0) -> None:
    """Driver frame callback (acquisition thread).

    Matches the BaseSpectrometer contract:
        on_frame(pixels, dark, integration_us, kind="standard", ratio=1.0)

    Frames are handed to the fusion stage, which owns all display decisions
    (fast-preview reconstruction always; temporal smoothing when enabled) and
    calls _fused_emit with a clean, single-rate stream — exactly as the
    desktop GUI does. This is what previously crashed: the server's old
    on_frame accepted only 3 positional args while the driver forwards 5.
    """
    state.fusion.submit(raw_pixels, ob_mean, integration_us, kind, ratio)
    # Opt-in diagnostic for the fast-preview ADC investigation.
    # Enable by starting the server with  FP_DEBUG=1 python web_server.py
    # Prints, per frame: the reported dark, the peak ADC, and the equivalent
    # exposure so a correctly-scaled FP frame (peak near the normal-exposure
    # level) is easy to verify.
    if _FP_DEBUG:
        try:
            pk = float(np.max(raw_pixels))
            print(f"[fp] dark={ob_mean:8.1f} peak={pk:9.1f} "
                  f"t={integration_us/1000:.0f}ms", flush=True)
        except Exception:
            pass


def _fused_emit(pixels: np.ndarray, ob_mean: float, integration_us: int) -> None:
    """Fusion output sink. Runs the processing pipeline, updates shared state,
    and broadcasts to WebSocket clients. May run on the fusion emit thread."""
    # FPS
    now = time.perf_counter()
    if state._last_frame_time is not None:
        dt = now - state._last_frame_time
        if dt > 0:
            state._fps_history.append(1.0 / dt)
            state.fps = float(np.mean(state._fps_history))
    state._last_frame_time = now

    # Frame averaging (resized when the user changes the spinbox)
    if state.spectrum_history.maxlen != state.frames_avg:
        state.spectrum_history = deque(state.spectrum_history, maxlen=state.frames_avg)
    state.spectrum_history.append(pixels)
    averaged = np.mean(state.spectrum_history, axis=0)

    state.latest_ob_mean = ob_mean
    state.latest_integration_us = integration_us

    # Pull live config knobs
    dark_correction = bool(Config.get("dark_correction", False))
    dark_mode       = Config.get("dark_mode", DARK_MODE_OPTICAL_BLACK)
    hot_pixel       = bool(Config.get("hot_pixel_filter", False))
    smooth          = int(Config.get("smoothing_width", 5))
    spatial_smooth  = bool(Config.get("spatial_smoothing", False))
    spatial_width   = int(Config.get("spatial_smoothing_width", 5))
    response_comp   = bool(Config.get("spectral_response_compensation", False))
    peak_on         = bool(Config.get("show_peaks", False))

    result = processing.process_frame(
        averaged, ob_mean, integration_us,
        wavelengths=state.wavelength_array,
        response_gain=state.response_gain,
        ob_slope=state.ob_slope,
        ob_offset=state.ob_offset,
        dark_correction=dark_correction,
        dark_mode=dark_mode,
        dark_bias_adc=float(Config.get("dark_bias_adc", DEFAULT_DARK_BIAS_ADC)),
        dark_rate_adc_per_s=float(Config.get("dark_rate_adc_per_s",
                                             DEFAULT_DARK_RATE_ADC_PER_SEC)),
        hot_pixel_filter=hot_pixel,
        smoothing_width=smooth,
        spatial_smoothing=spatial_smooth,
        spatial_smoothing_width=spatial_width,
        response_compensation=response_comp,
        peak_detection=peak_on,
        peak_params={k: Config.get(k) for k in (
            "peak_savgol_window", "peak_savgol_order", "peak_prominence",
            "peak_min_distance_px", "peak_min_height", "peak_max_count",
        )},
    )
    state.current_pixels = result["pixels"]
    state.current_peaks  = result["peaks"]

    # Heatmap rolling buffer
    linear = np.interp(state.heatmap_linear_waves,
                       state.wavelength_array, state.current_pixels)
    state.heatmap_buffer = np.roll(state.heatmap_buffer, 1, axis=1)
    state.heatmap_buffer[:, 0] = linear

    # Color metrics — throttle (heavy CRI math)
    state._frame_counter += 1
    if state._frame_counter % state.color_metrics_period_frames == 0:
        try:
            m = cs.measure_all(state.wavelength_array, state.current_pixels)
            # Strip non-JSON-friendly floats
            state.color_metrics = {k: (None if isinstance(v, float) and not np.isfinite(v)
                                       else v)
                                   for k, v in m.items() if k != "Ri"}
            state.color_metrics["Ri"] = [
                None if not np.isfinite(r) else float(r) for r in m["Ri"]]
        except Exception:
            state.color_metrics = None

    # Broadcast — schedule on the async event loop from this worker thread.
    if state.loop and state.clients:
        asyncio.run_coroutine_threadsafe(_broadcast_frame(), state.loop)


def _dev_int_bounds_us() -> tuple[int, int]:
    """Integration bounds (µs) of the connected device, with a global fallback
    when nothing is connected. Lets the exposure clamps follow the device
    (e.g. HDX below 1 ms) instead of the fixed PASCO limits."""
    b = state.acquisition
    return (int(getattr(b, "min_integration_us", MIN_INTEGRATION_TIME_US)),
            int(getattr(b, "max_integration_us", MAX_INTEGRATION_TIME_US)))


def on_auto_exposure(new_ms: float) -> None:
    # PASCO passes milliseconds. Clamp to the hardware ceiling so a runaway
    # value can never be stored, then notify clients so the Exposure field
    # tracks the live auto-exposure value instead of going stale.
    new_ms = float(new_ms)
    lo_us, hi_us = _dev_int_bounds_us()
    new_ms = max(lo_us / 1000.0, min(hi_us / 1000.0, new_ms))
    Config.set("exposure_ms", new_ms)
    state.spectrum_history.clear()
    state.fusion.reset()
    if state.loop and state.clients:
        asyncio.run_coroutine_threadsafe(
            _broadcast_config({"exposure_ms": new_ms}), state.loop)


async def _broadcast_config(partial: dict) -> None:
    """Push a partial config update to all clients (e.g. live auto-exposure)."""
    raw = json.dumps({"type": "config", "config": partial})
    dead: list[WebSocket] = []
    for ws in list(state.clients):
        try:
            await ws.send_text(raw)
        except Exception:
            dead.append(ws)
    if dead:
        with state.clients_lock:
            for ws in dead:
                state.clients.discard(ws)


def on_connection_lost() -> None:
    """Called from the acquisition thread when a USB read fails. We must NOT
    run _disconnect() inline here: _disconnect() -> backend.disconnect() ->
    thread.join(), and joining the *current* thread raises
    'cannot join current thread'. Schedule the cleanup on the event loop so
    the join happens from outside the dying acquisition thread."""
    print("[server] Hardware reports connection lost.")
    if state.loop is not None:
        state.loop.call_soon_threadsafe(_disconnect)
    else:
        # No loop yet (shouldn't happen post-startup) — best-effort, guarded.
        _disconnect()


# ============================================================
# WebSocket broadcasts
# ============================================================
def _build_frame_payload(downsample: int = 1) -> dict:
    """Compact JSON payload for the live stream. By default sends the FULL
    spectrum (no downsampling) so the scope line and the waterfall heatmap keep
    the device's native pixel resolution. (Was previously fixed at 4× for phone
    bandwidth, which capped the heatmap at ~512 columns.) Pass downsample>1 only
    where a coarse copy is explicitly wanted."""
    px = state.current_pixels
    wl = state.wavelength_array
    if downsample > 1 and px.size > downsample:
        px_ds = px[::downsample]
        wl_ds = wl[::downsample]
    else:
        px_ds = px
        wl_ds = wl
    # Replace any NaN/inf with finite numbers. A single NaN in the intensity
    # array serializes to JSON NaN/null and breaks the uPlot line into
    # invisible fragments. nan_to_num guarantees a clean, plottable array.
    px_ds = np.nan_to_num(np.asarray(px_ds, dtype=float),
                          nan=0.0, posinf=0.0, neginf=0.0)
    wl_ds = np.nan_to_num(np.asarray(wl_ds, dtype=float),
                          nan=0.0, posinf=0.0, neginf=0.0)
    return {
        "type": "frame",
        "wavelengths": [round(float(w), 2) for w in wl_ds],
        "intensities": [round(float(v), 2) for v in px_ds],
        "ob_mean": round(state.latest_ob_mean, 3),
        # Device-aware dark readout. PASCO has real optical-black pixels, so the
        # value is a true OB mean and the parametric Bias/Rate fields are
        # meaningful. LR-2T / HDX have no OB — the value is an estimated
        # baseline (median of the darkest pixels) and Bias/Rate do not apply.
        "supports_ob": bool(state.supports_ob),
        "dark_label": "OB mean" if state.supports_ob else "Baseline",
        "integration_us": int(state.latest_integration_us),
        "integration_ms": round(state.latest_integration_us / 1000.0, 2),
        "fps": round(state.fps, 2),
        "peaks": state.current_peaks,
        "sensor_temp_delta": round(state.latest_ob_mean -
                                   (DEFAULT_DARK_RATE_ADC_PER_SEC *
                                    state.latest_integration_us / 1_000_000.0) -
                                   OB_TEMP_REFERENCE_ADC, 2)
                              if (state.supports_ob and state.latest_ob_mean > 0)
                              else None,
        "color": state.color_metrics,
    }


async def _broadcast_frame() -> None:
    payload = _build_frame_payload()
    raw = json.dumps(payload)
    dead: list[WebSocket] = []
    for ws in list(state.clients):
        try:
            await ws.send_text(raw)
        except Exception:
            dead.append(ws)
    if dead:
        with state.clients_lock:
            for ws in dead:
                state.clients.discard(ws)


# ============================================================
# Hardware control wrappers
# ============================================================
def _active_device_name() -> str:
    """Name of the connected device, or the selected backend when idle."""
    return state.connected_backend_name or Config.get("selected_backend", "")


def _rebuild_response_gain(backend=None) -> None:
    """(Re)build state.response_gain from the active per-device response-
    correction selection. Shared by connect and the calibration endpoints so the
    web client applies a profile live, exactly like the desktop dropdown. During
    connect, state.acquisition is not assigned yet, so the backend is passed in
    explicitly for its built-in RESPONSE_TABLE."""
    dev = _active_device_name()
    src = backend if backend is not None else state.acquisition
    default_table = getattr(src, "RESPONSE_TABLE", None)
    selection = calprof.active_selection(dev)
    state.response_gain, _eff = calprof.build_gain_for_selection(
        state.wavelength_array, dev, selection,
        device_default_table=default_table)


def _apply_device_params(backend: BaseSpectrometer, backend_name: str | None = None) -> None:
    """Read the connected backend's wavelength axis, pixel count, response
    table and OB constants into shared state so processing, the heatmap,
    color math, payloads, and export all use the *active* device — not a
    hardcoded PASCO axis."""
    wl = np.asarray(backend.wavelength_array, dtype=float)
    state.wavelength_array = wl
    state.pixel_count      = int(backend.PIXEL_COUNT)
    state.supports_ob      = bool(getattr(backend, "SUPPORTS_OB", False))
    state.adc_saturation   = float(getattr(backend, "ADC_SATURATION",
                                           DEFAULT_ADC_SATURATION))

    # Response gain follows the per-device calibration selection (shared with
    # the desktop client): "None" → ones, "Device default" → the driver's
    # RESPONSE_TABLE, or a saved profile from calibration_profiles.json. The
    # legacy spectral_response_compensation boolean is kept in sync by the
    # selection setter; processing.py only reads that flag + this gain.
    if backend_name is None:
        backend_name = Config.get("selected_backend", "")
    state.connected_backend_name = backend_name
    _rebuild_response_gain(backend)

    # OB correction constants only meaningful for OB devices; neutral otherwise.
    if state.supports_ob:
        state.ob_slope  = DEFAULT_OB_CORRECTION_SLOPE
        state.ob_offset = DEFAULT_OB_CORRECTION_OFFSET
    else:
        state.ob_slope  = 1.0
        state.ob_offset = 0.0

    # Resize the spectrum/heatmap buffers to the new pixel count.
    state.current_pixels = np.zeros(state.pixel_count)
    state.heatmap_linear_waves = np.linspace(wl[0], wl[-1], state.pixel_count)
    state.heatmap_buffer = np.zeros((state.pixel_count, HEATMAP_HISTORY_SIZE))
    state.spectrum_history.clear()


def _reset_device_params() -> None:
    """Restore PASCO defaults so the UI still has a sane axis when idle."""
    state.pixel_count      = int(DEFAULT_PIXEL_COUNT)
    state.wavelength_array = DEFAULT_WAVELENGTH_ARRAY.copy()
    state.response_gain    = DEFAULT_SPECTRAL_RESPONSE_GAIN
    state.ob_slope         = DEFAULT_OB_CORRECTION_SLOPE
    state.ob_offset        = DEFAULT_OB_CORRECTION_OFFSET
    state.adc_saturation   = float(DEFAULT_ADC_SATURATION)
    state.supports_ob      = True
    state.current_pixels   = np.zeros(state.pixel_count)
    state.heatmap_linear_waves = np.linspace(
        state.wavelength_array[0], state.wavelength_array[-1], state.pixel_count)
    state.heatmap_buffer = np.zeros((state.pixel_count, HEATMAP_HISTORY_SIZE))


def _connect(device_path: str, backend_name: str | None = None) -> None:
    if backend_name is None:
        backend_name = Config.get("selected_backend", dm.list_backends()[0] if dm.REGISTRY else "PASCO PS-2600A")
    cls = dm.get_backend_class(backend_name)
    if cls is None:
        raise RuntimeError(f"Backend '{backend_name}' not available on this system.")

    backend = cls(
        on_frame           = on_frame,
        on_auto_exp        = on_auto_exposure,
        on_connection_lost = on_connection_lost,
    )
    backend.connect(device_path)

    ms = float(Config.get("exposure_ms", START_INTEGRATION_TIME_US / 1000.0))
    us = max(backend.min_integration_us,
             min(backend.max_integration_us, int(ms * 1000)))
    backend.current_integration_time_us = us
    backend.set_integration_time_us(us)
    backend.is_auto_exposure_active    = bool(Config.get("auto_exposure", False))
    backend.fast_preview_enabled       = bool(Config.get("fast_preview_enabled", False))
    backend.fast_preview_short_pct     = max(1, min(50, int(Config.get("fast_preview_short_pct", 10))))

    # Adopt the device's axis / response / OB params and arm fusion BEFORE
    # start() so the first frame lands on matching buffers (the HDX
    # connect-order race lesson — see BACKLOG "Bugs Fixed").
    _apply_device_params(backend, backend_name)
    state.fusion.configure(**{k: Config.get(k) for k in fusion.DEFAULTS
                              if Config.get(k, None) is not None})
    state.fusion.reset()
    state.fusion.start()

    backend.start()

    state.acquisition            = backend
    state.connected_device_path  = device_path
    state.connected_backend_name = backend_name


def _disconnect() -> None:
    if state.acquisition is not None:
        try:
            state.acquisition.disconnect()
        except RuntimeError as e:
            # "cannot join current thread" can occur if cleanup is reached
            # from within the acquisition thread during shutdown. The thread
            # is already unwinding; swallow it so it doesn't spam a traceback.
            if "cannot join current thread" not in str(e):
                raise
        except Exception as e:
            print(f"[server] disconnect cleanup error (ignored): {e!r}")
        state.acquisition = None
    state.fusion.stop()
    state.fusion.reset()
    state.connected_device_path  = None
    state.connected_backend_name = None
    state.spectrum_history.clear()
    _reset_device_params()


# ============================================================
# FastAPI app
# ============================================================
# Initialise device registry at module load so api_backends()
# and api_devices() work before the lifespan context starts.
dm.init_registry()

app = FastAPI(title="PS-2600A Spectrum Analyzer")

STATIC_DIR = Path(__file__).parent / "static"
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


@app.get("/", response_class=HTMLResponse)
async def root():
    return (STATIC_DIR / "index.html").read_text(encoding="utf-8")


@app.get("/api/status")
async def api_status():
    return {
        "connected": state.is_connected,
        "device_path": state.connected_device_path,
        "backend": state.connected_backend_name or "—",
        "fps": round(state.fps, 2),
        "integration_ms": round(state.latest_integration_us / 1000.0, 2),
        "ob_mean": round(state.latest_ob_mean, 3),
        "wavelength_min": float(state.wavelength_array[0]),
        "wavelength_max": float(state.wavelength_array[-1]),
        "pixel_count": int(state.pixel_count),
        "heatmap_size": int(HEATMAP_HISTORY_SIZE),
    }


@app.get("/api/references")
async def api_references():
    """Names of the available reference spectra (shared library file)."""
    names, _, _ = load_reference_library()
    return {"names": list(names)}


@app.get("/api/reference")
async def api_reference(name: str):
    """One reference spectrum (0..1), interpolated onto the active device axis."""
    names, _, columns = load_reference_library(state.wavelength_array)
    if name not in columns:
        raise HTTPException(404, f"reference '{name}' not found")
    return {
        "name": name,
        "wavelengths": [float(x) for x in state.wavelength_array],
        "values": [float(v) for v in columns[name]],
    }


@app.post("/api/filter")
async def api_filter(payload: dict):
    """Characterize a filter from a baseline (reference) and sample spectrum on a
    shared axis. Body: {wavelengths, reference, sample}. Returns the
    analyze_filter() result plus pre-formatted display rows. The transmission
    array carries nulls (NaN) where the reference has no usable light."""
    try:
        wl  = np.asarray(payload["wavelengths"], dtype=float)
        ref = np.asarray(payload["reference"],   dtype=float)
        smp = np.asarray(payload["sample"],      dtype=float)
    except (KeyError, TypeError, ValueError):
        raise HTTPException(400, "expected numeric wavelengths, reference, sample arrays")
    try:
        res = analyze_filter(wl, ref, smp)
    except ValueError as e:
        raise HTTPException(400, str(e))
    res["rows"] = metrics_rows(res)
    return _json_safe(res)


@app.post("/api/filter/report")
async def api_filter_report(payload: dict):
    """Render a downloadable filter report (plot + full metrics table, white
    background) from a baseline + sample spectrum. Body: {wavelengths, reference,
    sample, format}. format is 'pdf' (default) or 'png'. Uses the same shared
    renderer as the desktop app."""
    fmt = str(payload.get("format", "pdf")).lower()
    if fmt not in ("pdf", "png", "csv"):
        fmt = "pdf"
    try:
        wl  = np.asarray(payload["wavelengths"], dtype=float)
        ref = np.asarray(payload["reference"],   dtype=float)
        smp = np.asarray(payload["sample"],      dtype=float)
    except (KeyError, TypeError, ValueError):
        raise HTTPException(400, "expected numeric wavelengths, reference, sample arrays")
    try:
        res = analyze_filter(wl, ref, smp)
    except ValueError as e:
        raise HTTPException(400, str(e))
    res["rows"] = metrics_rows(res)
    if fmt == "csv":
        return _csv_response(metrics_rows(res, full=True), "filter_report.csv")
    buf = io.BytesIO()
    try:
        render_report(buf, res, fmt=fmt,
                      meta=_report_meta(peak_adc=float(np.max(smp)) if smp.size else None))
    except ImportError:
        raise HTTPException(503, "matplotlib is not installed on the server")
    buf.seek(0)
    media = "application/pdf" if fmt == "pdf" else "image/png"
    return Response(
        content=buf.read(), media_type=media,
        headers={"Content-Disposition": f'attachment; filename="filter_report.{fmt}"'})


def _report_meta(peak_adc: float | None = None) -> dict:
    """Assemble the acquisition-parameter block shown in PDF/PNG reports from the
    live backend state. Missing values are dropped by the renderer."""
    from datetime import datetime
    b = state.acquisition
    us = state.latest_integration_us
    ob = state.latest_ob_mean
    if peak_adc is None:
        px = getattr(state, "current_pixels", None)
        if px is not None and len(px):
            peak_adc = float(np.max(px))
    return {
        "Spectrometer":    state.connected_backend_name or (type(b).__name__ if b else None),
        "Serial":          getattr(b, "serial", None),
        "Date/time":       datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "Exposure":        (f"{us/1000:.1f} ms" if us else None),
        "Dark / baseline": (f"{ob:.0f} ADC" if ob is not None else None),
        "Peak ADC":        (f"{peak_adc:.0f}" if peak_adc is not None else None),
    }


def _csv_response(rows, filename):
    """Build a 2-column Metric,Value CSV download response from (label,value) rows."""
    import csv as _csv
    sio = io.StringIO()
    w = _csv.writer(sio)
    w.writerow(["Metric", "Value"])
    for k, v in rows:
        w.writerow([k, v])
    return Response(
        content=sio.getvalue(), media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'})


@app.post("/api/color/report")
async def api_color_report(payload: dict):
    """Render a colour / CRI report from a spectrum. Body: {wavelengths,
    intensities, format, lux_calibration?}. format is 'pdf' (default), 'png' or
    'csv'. Same shared renderer as the desktop CIE tab."""
    fmt = str(payload.get("format", "pdf")).lower()
    if fmt not in ("pdf", "png", "csv"):
        fmt = "pdf"
    try:
        wl    = np.asarray(payload["wavelengths"], dtype=float)
        inten = np.asarray(payload["intensities"], dtype=float)
    except (KeyError, TypeError, ValueError):
        raise HTTPException(400, "expected numeric wavelengths and intensities arrays")
    lux_cal = float(payload.get("lux_calibration", 0.0) or 0.0)
    if fmt == "csv":
        rows = cs.color_report_rows(wl, inten, lux_cal)
        return _csv_response(rows, "color_report.csv")
    buf = io.BytesIO()
    try:
        cs.render_color_report(buf, wl, inten, fmt=fmt, lux_calibration=lux_cal,
                               meta=_report_meta(peak_adc=float(np.max(inten)) if inten.size else None))
    except ImportError:
        raise HTTPException(503, "matplotlib is not installed on the server")
    buf.seek(0)
    media = "application/pdf" if fmt == "pdf" else "image/png"
    return Response(
        content=buf.read(), media_type=media,
        headers={"Content-Disposition": f'attachment; filename="color_report.{fmt}"'})


@app.post("/api/tm30")
async def api_tm30(payload: dict):
    """TM-30-18 + CQS for a spectrum. Body: {wavelengths, intensities}. Returns
    {tm30:{Rf,Rg,Rs,bins,Rfhj,Rcshj,Rhshj,avg_test,avg_ref,...}, cqs:{...}}.
    HTTP 503 if the optional 'colour-science' package isn't installed."""
    try:
        wl    = np.asarray(payload["wavelengths"], dtype=float)
        inten = np.asarray(payload["intensities"], dtype=float)
    except (KeyError, TypeError, ValueError):
        raise HTTPException(400, "expected numeric wavelengths and intensities arrays")
    tm = cs.tm30_metrics(wl, inten)
    if tm is None:
        raise HTTPException(
            503, "TM-30 needs the optional 'colour-science' package on the server")
    cq = cs.cqs_metrics(wl, inten)
    return _json_safe({"tm30": tm, "cqs": cq,
                       "bin_colors": cs.TM30_BIN_COLORS})


@app.get("/api/backends")
async def api_backends():
    """List all available backend names."""
    return {"backends": dm.list_backends(),
            "selected": Config.get("selected_backend", dm.list_backends()[0] if dm.REGISTRY else "")}


@app.get("/api/devices")
async def api_devices(backend: Optional[str] = None):
    if backend is None:
        backend = Config.get("selected_backend",
                             dm.list_backends()[0] if dm.REGISTRY else "")
    cls = dm.get_backend_class(backend)
    devices: list[str] = []
    if cls is not None:
        try:
            devices = cls.scan()
        except Exception:
            devices = []
    # Always surface the currently connected path
    if state.connected_device_path and state.connected_device_path not in devices:
        devices.insert(0, state.connected_device_path)
    return {"devices": devices, "backend": backend}


@app.post("/api/connect")
async def api_connect(payload: dict):
    device_path  = payload.get("device_path")
    backend_name = payload.get("backend") or Config.get(
        "selected_backend", dm.list_backends()[0] if dm.REGISTRY else "")

    # Already connected to the same device+backend — confirm without restart
    if (state.is_connected
            and device_path == state.connected_device_path
            and backend_name == state.connected_backend_name):
        return {"connected": True, "device_path": device_path,
                "backend": backend_name}

    if state.is_connected:
        _disconnect()
        await asyncio.sleep(0.4)

    if not device_path:
        cls = dm.get_backend_class(backend_name)
        if cls is None:
            raise HTTPException(404, f"Backend '{backend_name}' not available.")
        try:
            devices = cls.scan()
        except Exception:
            devices = []
        if not devices:
            raise HTTPException(404, f"No {backend_name} found on USB bus.")
        device_path = devices[0]

    Config.set("selected_backend", backend_name)
    try:
        _connect(device_path, backend_name)
    except Exception as e:
        raise HTTPException(500, str(e))
    return {"connected": True, "device_path": device_path, "backend": backend_name}


@app.post("/api/disconnect")
async def api_disconnect():
    _disconnect()
    return {"connected": False}


@app.get("/api/config")
async def api_config_get():
    return Config.all()


@app.post("/api/config")
async def api_config_set(payload: dict):
    """Bulk-update config values. Persisted to disk."""
    Config.update(payload)
    # Push side-effects to live hardware
    if "exposure_ms" in payload and state.acquisition is not None:
        lo_us, hi_us = _dev_int_bounds_us()
        us = max(lo_us, min(hi_us, int(float(payload["exposure_ms"]) * 1000)))
        # set_integration_time_us is the BaseSpectrometer-level method every
        # backend implements; it applies to hardware internally. (The old code
        # called acquisition.apply_hardware_exposure_time(), which only exists
        # on the PASCO acquisition *thread*, not on the backend object.)
        state.acquisition.set_integration_time_us(us)
        state.spectrum_history.clear()
        state.fusion.reset()
    if "auto_exposure" in payload and state.acquisition is not None:
        state.acquisition.is_auto_exposure_active = bool(payload["auto_exposure"])
        state.spectrum_history.clear()
        state.fusion.reset()
    if "frames_to_average" in payload:
        n = max(1, int(payload["frames_to_average"]))
        state.frames_avg = n
        state.spectrum_history = deque(state.spectrum_history, maxlen=n)
    if "pause" in payload and state.acquisition is not None:
        state.acquisition.is_measurement_paused = bool(payload["pause"])
    # Live fusion parameters
    fusion_keys = {k: payload[k] for k in payload if k in fusion.DEFAULTS}
    if fusion_keys:
        state.fusion.configure(**fusion_keys)
    return Config.all()


@app.get("/api/calibration/profiles")
async def api_calibration_profiles():
    """Response-correction options for the active device: the selectable items
    (None / Device default if the driver ships a table / saved profiles) and the
    current selection. Mirrors the desktop dropdown."""
    dev = _active_device_name()
    has_default = getattr(state.acquisition, "RESPONSE_TABLE", None) is not None
    return {
        "device":    dev,
        "selection": calprof.active_selection(dev),
        "items":     calprof.selection_items(dev, has_default),
        "profiles":  calprof.list_profiles(dev),
    }


@app.post("/api/calibration/select")
async def api_calibration_select(payload: dict):
    """Set the active response-correction selection for the current device and
    rebuild the live gain. selection in {None, Device default, <profile name>}."""
    dev = _active_device_name()
    if not dev:
        raise HTTPException(400, "No device selected.")
    selection = str(payload.get("selection", calprof.SEL_NONE))
    valid = calprof.selection_items(
        dev, getattr(state.acquisition, "RESPONSE_TABLE", None) is not None)
    if selection not in valid:
        raise HTTPException(400, f"Unknown selection '{selection}'.")
    calprof.set_active_selection(dev, selection)
    _rebuild_response_gain()
    return {"ok": True, "device": dev, "selection": selection}


@app.post("/api/calibration/profile/delete")
async def api_calibration_profile_delete(payload: dict):
    """Delete a saved response-correction profile for the current device. If it
    was active, fall back to None and rebuild the gain."""
    dev = _active_device_name()
    name = str(payload.get("name", "")).strip()
    if not name:
        raise HTTPException(400, "Profile name required.")
    deleted = calprof.delete_profile(dev, name)
    if calprof.active_selection(dev) == name:
        calprof.set_active_selection(dev, calprof.SEL_NONE)
    _rebuild_response_gain()
    return {"ok": bool(deleted), "device": dev,
            "selection": calprof.active_selection(dev)}


@app.get("/api/heatmap")
async def api_heatmap():
    """Returns the full heatmap matrix (frame x wavelength) as JSON. Used by
    the heatmap tab when it activates."""
    return {
        "wavelengths": [float(w) for w in state.heatmap_linear_waves],
        "frames":      [[float(v) for v in state.heatmap_buffer[:, i]]
                        for i in range(HEATMAP_HISTORY_SIZE)],
        "saturation": state.adc_saturation,
    }


@app.get("/api/export/csv")
async def api_export_csv(tab: str = "spectrum"):
    """Return CSV bytes for the current spectrum or heatmap."""
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    buf = io.StringIO()
    if tab == "heatmap":
        header = ["Frame"] + [f"{wl:.2f}" for wl in state.heatmap_linear_waves]
        buf.write(";".join(header) + "\n")
        for fi in range(HEATMAP_HISTORY_SIZE):
            row = [str(fi)] + [f"{v:.2f}" for v in state.heatmap_buffer[:, fi]]
            buf.write(";".join(row) + "\n")
        name = f"heatmap_{ts}.csv"
    else:
        buf.write(f"# Optical Black mean (ADC);{state.latest_ob_mean:.3f}\n")
        buf.write(f"# Integration time (ms);{state.latest_integration_us / 1000.0:.1f}\n")
        buf.write(f"# Dark correction;{('ON' if Config.get('dark_correction') else 'OFF')}\n")
        buf.write(f"# Dark mode;{Config.get('dark_mode')}\n")
        buf.write(f"# Response compensation;{('ON' if Config.get('spectral_response_compensation') else 'OFF')}\n")
        buf.write("Pixel_ID;Wavelength_nm;ADC_Counts\n")
        for i, (wl, v) in enumerate(zip(state.wavelength_array, state.current_pixels)):
            buf.write(f"{i};{wl:.2f};{v:.2f}\n")
        name = f"spectrum_{ts}.csv"
    return Response(
        content=buf.getvalue(),
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{name}"'},
    )


@app.websocket("/ws/stream")
async def ws_stream(ws: WebSocket):
    await ws.accept()
    with state.clients_lock:
        state.clients.add(ws)
    # Send a hello frame so the client gets initial state immediately.
    # Include backends, device list, and connection state in hello so
    # the browser can populate all dropdowns without extra API calls.
    try:
        hello_backend = Config.get("selected_backend",
                                   dm.list_backends()[0] if dm.REGISTRY else "")
        hello_devices: list[str] = []
        hello_cls = dm.get_backend_class(hello_backend)
        if hello_cls is not None:
            try:
                hello_devices = hello_cls.scan()
            except Exception:
                hello_devices = []
        if (state.connected_device_path
                and state.connected_device_path not in hello_devices):
            hello_devices.insert(0, state.connected_device_path)

        hello = {
            "type":       "hello",
            "connected":  bool(state.is_connected),
            "device_path": state.connected_device_path,
            "backend":    state.connected_backend_name or hello_backend,
            "backends":   list(dm.list_backends()),
            "devices":    list(hello_devices),
            "config":     _json_safe(Config.all()),
            "wavelengths_full": [round(float(w), 2) for w in state.wavelength_array[::4]],
        }
        try:
            await ws.send_text(json.dumps(hello))
        except Exception as e:
            # Never let a hello hiccup kill the socket — the dropdowns depend
            # on it. Log loudly and fall back to a minimal hello.
            print(f"[server] hello serialize/send failed: {e!r}; sending minimal hello")
            await ws.send_text(json.dumps({
                "type": "hello",
                "connected": bool(state.is_connected),
                "device_path": state.connected_device_path,
                "backend": hello_backend,
                "backends": list(dm.list_backends()),
                "devices": list(hello_devices),
                "config": {},
                "wavelengths_full": [],
            }))
        while True:
            msg = await ws.receive_text()
            try:
                data = json.loads(msg)
            except Exception:
                continue
            # Inbound control messages
            t = data.get("type")
            if t == "set_config":
                Config.update(data.get("payload", {}))
                # Apply same side-effects as REST endpoint
                payload = data.get("payload", {})
                if "exposure_ms" in payload and state.acquisition is not None:
                    lo_us, hi_us = _dev_int_bounds_us()
                    us = max(lo_us,
                             min(hi_us,
                                 int(float(payload["exposure_ms"]) * 1000)))
                    state.acquisition.set_integration_time_us(us)
                    state.spectrum_history.clear()
                    state.fusion.reset()
                if "auto_exposure" in payload and state.acquisition is not None:
                    state.acquisition.is_auto_exposure_active = bool(payload["auto_exposure"])
                    state.spectrum_history.clear()
                    state.fusion.reset()
                if "frames_to_average" in payload:
                    n = max(1, int(payload["frames_to_average"]))
                    state.frames_avg = n
                    state.spectrum_history = deque(state.spectrum_history, maxlen=n)
                if "fast_preview_enabled" in payload and state.acquisition is not None:
                    state.acquisition.fast_preview_enabled = bool(payload["fast_preview_enabled"])
                    state.fusion.reset()
                if "fast_preview_short_pct" in payload and state.acquisition is not None:
                    state.acquisition.fast_preview_short_pct = max(1, min(50, int(payload["fast_preview_short_pct"])))
                    state.fusion.reset()
                # Live fusion parameters (smoothing, change-sigma, emit rate…)
                fusion_keys = {k: payload[k] for k in payload if k in fusion.DEFAULTS}
                if fusion_keys:
                    state.fusion.configure(**fusion_keys)
            elif t == "pause":
                if state.acquisition is not None:
                    state.acquisition.is_measurement_paused = bool(data.get("paused", False))
            elif t == "connect":
                # Trigger a connect attempt from the WS (used by mobile clients)
                try:
                    device_path = data.get("device_path")
                    backend_name = data.get("backend") or Config.get(
                        "selected_backend", dm.list_backends()[0] if dm.REGISTRY else "")
                    cls = dm.get_backend_class(backend_name)
                    if not device_path and cls:
                        devs = cls.scan()
                        device_path = devs[0] if devs else None
                    if device_path:
                        _connect(device_path, backend_name)
                        await ws.send_text(json.dumps({"type": "status", "connected": True}))
                    else:
                        raise RuntimeError("No device found")
                except Exception as e:
                    await ws.send_text(json.dumps({"type": "error", "message": str(e)}))
            elif t == "disconnect":
                _disconnect()
                await ws.send_text(json.dumps({"type": "status", "connected": False}))
            elif t == "ping":
                await ws.send_text(json.dumps({"type": "pong"}))
    except WebSocketDisconnect:
        pass
    except Exception as e:
        print(f"[server] websocket handler error: {e!r}")
    finally:
        with state.clients_lock:
            state.clients.discard(ws)


# ============================================================
# Lifecycle  (lifespan replaces deprecated on_event)
# ============================================================


@asynccontextmanager
async def _lifespan(application):
    # startup
    state.loop = asyncio.get_running_loop()
    dm.init_registry()
    ensure_reference_library_exists()
    print(f"[server] Backends available: {dm.list_backends()}")
    try:
        default_backend = dm.list_backends()[0] if dm.REGISTRY else None
        if default_backend:
            cls = dm.get_backend_class(default_backend)
            devices = cls.scan() if cls else []
            if devices and not state.is_connected:
                _connect(devices[0], default_backend)
                print(f"[server] Auto-connected ({default_backend}): {devices[0]}")
            elif not devices:
                print(f"[server] No {default_backend} device found at startup "
                      f"(connect from the browser).")
    except Exception as e:
        import traceback
        print(f"[server] Auto-connect skipped: {e!r}")
        traceback.print_exc()
    yield
    # shutdown
    _disconnect()


# Wire the lifespan into the already-created app
app.router.lifespan_context = _lifespan


# ============================================================
# Entry point
# ============================================================
def _get_local_ip() -> str:
    """Best-effort LAN IP discovery (no internet needed)."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("10.255.255.255", 1))
        ip = s.getsockname()[0]
    except Exception:
        ip = "127.0.0.1"
    finally:
        s.close()
    return ip


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--reload", action="store_true",
                        help="Auto-reload on code changes (development)")
    args = parser.parse_args()

    ip = _get_local_ip()
    print()
    print(f"  ┌─────────────────────────────────────────────────────")
    print(f"  │  PASCO PS-2600A Web GUI")
    print(f"  │  Open from any device on your WiFi LAN:")
    print(f"  │     http://{ip}:{args.port}")
    print(f"  │  Local access:")
    print(f"  │     http://localhost:{args.port}")
    print(f"  └─────────────────────────────────────────────────────")
    print()

    uvicorn.run(app, host=args.host, port=args.port, log_level="warning",
                reload=args.reload)
    return 0


if __name__ == "__main__":
    sys.exit(main())

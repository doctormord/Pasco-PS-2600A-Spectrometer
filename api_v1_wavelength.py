"""
api_v1_wavelength.py — /api/v1 group "wavelength": wavelength calibration with
a line lamp (desktop-parity P-11). The logic lives in wavelength_calibration.py;
this module only holds the per-engine session, the capture and the HTTP shape.

Workflow: POST /wavelength/session -> POST /wavelength/capture -> POST
/wavelength/match (-> PUT /wavelength/pairs) -> POST /wavelength/fit (live
preview for all clients) -> POST /wavelength/save {confirm:true}, or
DELETE /wavelength/session to discard the preview.
"""
from __future__ import annotations

import os
from typing import Optional

import numpy as np
from fastapi import APIRouter, Request
from pydantic import BaseModel, ConfigDict, Field

import wavelength_calibration as wlcal
from api import ApiError
from engine import json_safe

PREVIEW_POINTS = 600


class CaptureRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    frames: int = Field(16, ge=1, le=1000, description="Frames to average")
    integration_ms: Optional[float] = Field(
        None, gt=0, description="Integration time; null = current device setting")


class MatchRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    lamp: str
    accumulate: bool = Field(False, description="Append to existing pairs (multi-lamp fit)")


class PairsRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    pairs: list[list[float]] = Field(..., description="[[pixel, nm], …]")


class FitRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    degree: int = Field(wlcal.DEFAULT_DEGREE, ge=1, le=5)


class SaveRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    confirm: bool = Field(False, description="Must be true: saving replaces the calibration")


def _decimate_max(y: np.ndarray, n: int) -> np.ndarray:
    """Indices of at most n points, the maximum of each bin (keeps lamp lines)."""
    if y.size <= n:
        return np.arange(y.size)
    edges = np.linspace(0, y.size, n + 1).astype(int)
    return np.array([a + int(np.argmax(y[a:b])) for a, b in zip(edges[:-1], edges[1:]) if b > a])


def register(router: APIRouter, ctx) -> None:
    engine = ctx.engine
    auth = ctx.auth
    state: dict = {"session": None}

    def _session(required: bool = True):
        s = state["session"]
        if s is not None and s.backend is not engine.acquisition:
            state["session"] = s = None            # device changed: stale session
        if s is None and required:
            raise ApiError(409, "conflict",
                           "No wavelength session. Start one with POST /wavelength/session.")
        return s

    def _info(s) -> dict:
        wl = np.asarray(s.backend.wavelength_array, float)
        return {"active": True, "device": s.backend_name, "saved": s.saved,
                "config_key": s.config_key(), "n_pixels": int(wl.size),
                "wavelength_min": float(wl[0]), "wavelength_max": float(wl[-1]),
                "has_capture": s.capture is not None, "n_peaks": len(s.peaks or []),
                "pairs": [[p, n] for p, n in s.pairs], "last_fit": s.last_fit}

    def _peaks(s) -> list[dict]:
        if s.capture is None or not s.peaks:
            return []
        wl, y = s.capture
        idx = np.arange(wl.size)
        return [{"pixel": float(p), "wavelength": float(np.interp(p, idx, wl)),
                 "height": float(np.interp(p, idx, y))} for p in s.peaks]

    def _fail(e: Exception):
        code = "wavelength_not_supported" if isinstance(e, wlcal.NotSupported) else "unprocessable"
        return ApiError(422, code, str(e))

    @router.get("/wavelength/lamps", tags=["wavelength"],
                summary="Calibration lamps and their reference lines (nm)")
    async def get_lamps():
        return {"lamps": [{"name": k, "lines": v} for k, v in wlcal.lamps().items()]}

    @router.get("/wavelength/session", tags=["wavelength"],
                summary="State of the current wavelength session")
    async def get_session():
        s = _session(required=False)
        return json_safe(_info(s) if s else {"active": False})

    @router.post("/wavelength/session", tags=["wavelength"], dependencies=auth,
                 summary="Start (or resume) a session; snapshots the current axis")
    async def start_session(request: Request):
        engine.check_control(ctx.owner(request))
        b = engine.acquisition
        if b is None or not engine.is_connected:
            raise ApiError(503, "not_connected", "No device connected.")
        s = _session(required=False)
        resumed = s is not None
        if s is None:
            s = state["session"] = wlcal.WavelengthSession(b, engine.device_name)
        return json_safe({**_info(s), "resumed": resumed})

    @router.delete("/wavelength/session", tags=["wavelength"], dependencies=auth,
                   summary="Close the session; an unsaved preview is rolled back")
    async def delete_session(request: Request):
        engine.check_control(ctx.owner(request))
        s = _session()
        axis = await ctx.call(s.discard)
        if axis is not None:
            await ctx.call(engine.update_axis, axis)
        state["session"] = None
        return {"active": False, "rolled_back": axis is not None, "saved": s.saved}

    def _do_capture(frames: int, integration_ms) -> dict:
        s = _session()
        b = s.backend
        t_us = (int(b.current_integration_time_us) if integration_ms is None
                else int(round(float(integration_ms) * 1000)))
        lo, hi = engine.int_bounds_us()
        t_us = max(lo, min(hi, t_us))
        cap, dark_mean = engine._capture(None, frames, t_us)
        mean = np.asarray(cap.mean, float)
        dark = None
        try:
            arr, _src = engine.calib.dark_for(cap.integration_us)
            if arr is not None and np.asarray(arr).shape == mean.shape:
                dark = np.asarray(arr, float)
        except Exception:                                        # noqa: BLE001
            dark = None
        net = np.clip(mean - (dark if dark is not None else float(dark_mean)), 0.0, None)
        wl = np.asarray(cap.wl, float)
        s.set_capture(wl, net)
        peaks = s.detect_peaks() if s.has_signal else []
        sel = _decimate_max(net, PREVIEW_POINTS)
        return {"n_frames": int(cap.n_frames), "integration_ms": cap.integration_us / 1000.0,
                "dark": "per_pixel" if dark is not None else "scalar",
                "has_signal": s.has_signal, "peak_counts": float(np.max(net)) if net.size else 0.0,
                "n_saturated": int(cap.n_saturated), "n_peaks": len(peaks),
                "peaks": _peaks(s),
                "preview": {"wavelengths": wl[sel].tolist(), "values": net[sel].tolist()},
                "warning": None if s.has_signal else
                "No signal captured. Check the lamp and the integration time."}

    @router.post("/wavelength/capture", tags=["wavelength"], dependencies=auth,
                 summary="Average N frames of the line lamp, subtract the dark, detect peaks (blocking)")
    async def capture(body: CaptureRequest, request: Request):
        engine.check_control(ctx.owner(request))
        _session()
        return json_safe(await ctx.call(_do_capture, body.frames, body.integration_ms))

    @router.post("/wavelength/match", tags=["wavelength"], dependencies=auth,
                 summary="Assign a lamp's known lines to the detected peaks")
    async def match(body: MatchRequest, request: Request):
        engine.check_control(ctx.owner(request))
        s = _session()
        try:
            info = s.match(body.lamp, accumulate=body.accumulate)
        except wlcal.WavelengthError as e:
            raise _fail(e)
        info["pairs"] = [[p, n] for p, n in info["pairs"]]
        return json_safe(info)

    @router.put("/wavelength/pairs", tags=["wavelength"], dependencies=auth,
                summary="Replace the (pixel, nm) pairs (user-edited)")
    async def put_pairs(body: PairsRequest, request: Request):
        engine.check_control(ctx.owner(request))
        s = _session()
        try:
            pairs = s.set_pairs(body.pairs)
        except wlcal.WavelengthError as e:
            raise _fail(e)
        return {"pairs": [[p, n] for p, n in pairs], "n_total": len(pairs)}

    def _do_fit(degree: int) -> dict:
        s = _session()
        try:
            res = s.fit(degree)
        except wlcal.WavelengthError as e:
            raise _fail(e)
        wl = res.pop("wavelengths")
        engine.update_axis(wl)            # accepted: new axis; rejected: restored axis (no-op)
        res["axis"] = {"wavelength_min": float(wl[0]), "wavelength_max": float(wl[-1]),
                       "n_pixels": int(wl.size)}
        return res

    @router.post("/wavelength/fit", tags=["wavelength"], dependencies=auth,
                 summary="Fit the polynomial; accepted fits are applied live, "
                         "rejected ones (422, details.reasons) roll back")
    async def fit(body: FitRequest, request: Request):
        engine.check_control(ctx.owner(request))
        _session()
        res = await ctx.call(_do_fit, body.degree)
        if not res["accepted"]:
            raise ApiError(422, "fit_rejected",
                           "Fit rejected — " + "; ".join(res["reasons"])
                           + ". The previous axis is kept.", json_safe(res))
        return json_safe(res)

    @router.post("/wavelength/save", tags=["wavelength"], dependencies=auth,
                 summary="Back up the old calibration, then persist the fitted polynomial")
    async def save(body: SaveRequest, request: Request):
        engine.check_control(ctx.owner(request))
        s = _session()
        try:
            out = await ctx.call(s.save, body.confirm)
        except wlcal.WavelengthError as e:
            raise _fail(e)
        out["backup_file"] = os.path.basename(out["backup_path"])
        out["saved"] = True
        return json_safe(out)

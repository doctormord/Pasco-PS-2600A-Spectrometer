"""
api_v1_display.py — /api/v1 group: heatmap (time-lapse) buffer, autosave,
export naming. Used by the web UI for desktop parity (docs/PARITAET_*.md
P-01..P-07); all state lives in the engine.
"""
from __future__ import annotations

import io
from datetime import datetime
from typing import Literal, Optional

import numpy as np
from fastapi import APIRouter, Query, Request
from fastapi.responses import Response
from pydantic import BaseModel, ConfigDict, Field

from api import ApiError
from engine import json_safe


class HeatmapSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")
    buffer_size: Optional[int] = Field(None, ge=10, le=100000, description="Frames (step 10); clears the buffer")
    measure_interval_ms: Optional[int] = Field(None, ge=0, description="0 = every frame")


class AutosaveRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    active: bool


def register(router: APIRouter, ctx) -> None:
    engine = ctx.engine
    auth = ctx.auth

    def _info() -> dict:
        h = engine.heatmap_snapshot()
        times = h["times"]
        n = h["filled"]
        span = None
        if n > 1 and np.isfinite(times[0]) and np.isfinite(times[n - 1]):
            span = float(times[0] - times[n - 1])
        return {"size": h["size"], "filled": n,
                "measure_interval_ms": int(engine.cfg.get("heatmap_measure_interval_ms", 0) or 0),
                "wavelength_min": float(h["wavelengths"][0]),
                "wavelength_max": float(h["wavelengths"][-1]),
                "n_wavelengths": int(h["wavelengths"].size),
                "span_s": span, "autosave": engine.autosave_info()}

    @router.get("/heatmap", tags=["heatmap"], summary="Time-lapse buffer state")
    async def heatmap():
        return json_safe(_info())

    @router.put("/heatmap", tags=["heatmap"], dependencies=auth,
                summary="Buffer size (clears) and measurement interval")
    async def put_heatmap(body: HeatmapSettings, request: Request):
        d = {}
        if body.buffer_size is not None:
            d["heatmap_buffer_size"] = body.buffer_size
        if body.measure_interval_ms is not None:
            d["heatmap_measure_interval_ms"] = body.measure_interval_ms
        if not d:
            raise ApiError(400, "bad_request", "No setting given.")
        await ctx.call(engine.configure, d, owner=ctx.owner(request))
        return json_safe(_info())

    @router.post("/heatmap/clear", tags=["heatmap"], dependencies=auth)
    async def clear(request: Request):
        engine.clear_heatmap()
        return json_safe(_info())

    @router.post("/heatmap/autosave", tags=["heatmap"], dependencies=auth,
                 summary="Start/stop writing every logged column to a CSV on the server")
    async def autosave(body: AutosaveRequest, request: Request):
        if body.active:
            try:
                info = await ctx.call(engine.start_autosave)
            except OSError as e:
                raise ApiError(422, "unprocessable", f"Could not open the autosave file: {e}")
            return json_safe(info)
        return json_safe(engine.stop_autosave())

    @router.get("/heatmap/frame", tags=["heatmap"],
                summary="One logged column (index 0 = newest) with its age")
    async def frame(index: int = Query(0, ge=0)):
        h = engine.heatmap_snapshot()
        if h["filled"] == 0:
            raise ApiError(404, "not_found", "The time-lapse buffer is empty.")
        i = min(index, h["filled"] - 1)
        t = h["times"][i]
        import time as _t
        age = float(_t.monotonic() - t) if np.isfinite(t) else None
        return json_safe({"index": i, "age_s": age,
                          "wavelengths": [round(float(w), 3) for w in h["wavelengths"]],
                          "values": h["buffer"][:, i]})

    @router.get("/heatmap/export", tags=["heatmap"],
                summary="CSV of the buffer: full, a range of frames, or one frame")
    async def export(scope: Literal["full", "range", "frame"] = "full",
                     start: int = Query(0, ge=0), end: Optional[int] = Query(None, ge=0),
                     index: int = Query(0, ge=0)):
        h = engine.heatmap_snapshot()
        n = h["filled"]
        if scope == "frame":
            idx = [min(index, max(n - 1, 0))] if n else []
        elif scope == "range":
            hi = n if end is None else min(n, end)
            idx = list(range(min(start, hi), hi))
        else:
            idx = list(range(n))
        buf = io.StringIO()
        buf.write(";".join(["Time_Frame"] + [f"{w:.2f}" for w in h["wavelengths"]]) + "\n")
        for fi in idx:
            buf.write(";".join([str(fi)] + [f"{v:.2f}" for v in h["buffer"][:, fi]]) + "\n")
        name = filename("heatmap", "csv")
        return Response(buf.getvalue(), media_type="text/csv", headers={
            "Content-Disposition": f'attachment; filename="{name}"'})

    def filename(kind: str, ext: str) -> str:
        import export_paths
        meta = export_paths.meta_from_report(engine.report_meta(), engine.wavelength_array)
        try:
            return export_paths.build_filename(kind, ext, meta)
        except Exception:                                        # noqa: BLE001
            return f"{kind}_{datetime.now():%Y%m%d_%H%M%S}.{ext}"

    @router.get("/export/filename", tags=["data"],
                summary="Filename the export template yields (export_filename_template)")
    async def export_filename(kind: str = "spectrum", ext: str = "csv",
                              template: Optional[str] = None):
        import export_paths
        if template is not None:
            saved = engine.cfg.get("export_filename_template", export_paths.DEFAULT_TEMPLATE)
            try:
                engine.cfg.set("export_filename_template", template or export_paths.DEFAULT_TEMPLATE,
                               save=False)
                name = filename(kind, ext)
            finally:
                engine.cfg.set("export_filename_template", saved, save=False)
        else:
            name = filename(kind, ext)
        d, err = export_paths.ensure_export_dir()
        return {"filename": name, "export_dir": d, "warning": err,
                "tokens": export_paths.TOKEN_HELP}

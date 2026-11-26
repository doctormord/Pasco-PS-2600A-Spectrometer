"""
api_v1.py — versioned automation API (/api/v1) over the shared Engine.

Core groups live here (system, device, acquisition, control, measurement,
jobs, data). Further groups register themselves through `GROUPS`:
  api_v1_analysis.py     analysis + reports + measurement export helpers
  api_v1_calibration.py  calibration profiles, references, darks, runs

Each group module exposes   register(router: APIRouter, ctx: V1Context) -> None.
Models: api_models.py. Error format: {"error": {"code", "message", "details"}}.
"""
from __future__ import annotations

import asyncio
import importlib
from dataclasses import dataclass
from typing import Any, Callable

import numpy as np
from fastapi import APIRouter, Depends, FastAPI, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, Response

import api_models as M
import processing
from api import ApiError, ServerSettings, client_owner
from engine import (
    Engine, EngineError, JOB_FINAL, JOB_WAITING, json_safe,
    measurement_to_csv, measurement_to_json,
)

PREFIX = "/api/v1"
GROUPS = ("api_v1_analysis", "api_v1_calibration", "api_v1_display", "api_v1_wavelength")


@dataclass
class V1Context:
    """What group modules need."""
    engine: Engine
    settings: ServerSettings
    auth: list                      # dependencies=[...] for steering endpoints
    owner: Callable[[Request], str]

    async def call(self, fn, *args, **kwargs):
        """Run a (possibly blocking) engine command in a worker thread and map
        EngineError → ApiError."""
        try:
            return await asyncio.to_thread(fn, *args, **kwargs)
        except EngineError as e:
            raise ApiError(e.status, e.code, str(e), e.details)

    def job_response(self, job, *, measurement_key: str | None = None) -> JSONResponse:
        """202 + job while not final, else 200 with the job (and result)."""
        d = job.to_dict()
        status = 200 if job.status in JOB_FINAL else 202
        return JSONResponse(status_code=status, content=json_safe(
            {"job": d, "location": f"{PREFIX}/jobs/{job.id}"}),
            headers={"Location": f"{PREFIX}/jobs/{job.id}"})

    async def wait_job(self, job_id: str, timeout_s: float):
        return await asyncio.to_thread(self.engine.jobs.wait, job_id, timeout_s, True)


def _err(status: int, code: str, message: str, details: Any = None) -> JSONResponse:
    return JSONResponse(status_code=status,
                        content={"error": {"code": code, "message": message,
                                           "details": json_safe(details)}})


_HTTP_CODES = {400: "bad_request", 401: "unauthorized", 403: "forbidden", 404: "not_found",
               405: "method_not_allowed", 409: "conflict", 422: "unprocessable",
               503: "unavailable"}


def install_error_handlers(app: FastAPI) -> None:
    from starlette.exceptions import HTTPException as StarletteHTTPException
    from fastapi.exception_handlers import (
        http_exception_handler, request_validation_exception_handler)

    @app.exception_handler(ApiError)
    async def _api_error(request: Request, exc: ApiError):
        return _err(exc.status, exc.code, exc.message, exc.details)

    @app.exception_handler(EngineError)
    async def _engine_error(request: Request, exc: EngineError):
        return _err(exc.status, exc.code, str(exc), exc.details)

    @app.exception_handler(RequestValidationError)
    async def _validation(request: Request, exc: RequestValidationError):
        if request.url.path.startswith(PREFIX):
            return _err(422, "validation_error", "Request validation failed.",
                        json_safe(exc.errors()))
        return await request_validation_exception_handler(request, exc)

    @app.exception_handler(StarletteHTTPException)
    async def _http(request: Request, exc: StarletteHTTPException):
        if request.url.path.startswith(PREFIX):
            r = _err(exc.status_code, _HTTP_CODES.get(exc.status_code, "error"),
                     str(exc.detail))
            if exc.headers:
                r.headers.update(exc.headers)
            return r
        return await http_exception_handler(request, exc)


def resolve_spectrum(engine: Engine, inp) -> tuple[np.ndarray, np.ndarray, str, dict | None]:
    """(wavelengths, values, unit, measurement|None) from a SpectrumInput:
    measurement id, explicit arrays, or the latest live spectrum."""
    if getattr(inp, "measurement_id", None):
        m = engine.get_measurement(inp.measurement_id)
        return (np.asarray(m["wavelengths"], float), np.asarray(m["values"], float),
                m["unit"], m)
    if getattr(inp, "wavelengths", None) is not None and getattr(inp, "values", None) is not None:
        wl = np.asarray(inp.wavelengths, float)
        v = np.asarray([np.nan if x is None else x for x in inp.values], float)
        if wl.size != v.size or wl.size < 3:
            raise ApiError(400, "bad_request", "wavelengths and values must have the same length (≥ 3).")
        return wl, v, inp.unit or "counts", None
    ev = engine.last_frame
    if ev is None:
        raise ApiError(503, "not_connected", "No live spectrum yet (connect a device or "
                                             "pass measurement_id / data).")
    return ev.wavelengths, ev.pixels, ev.unit, None


def add_v1_routes(app: FastAPI, engine: Engine, settings: ServerSettings,
                  require_token) -> None:
    install_error_handlers(app)
    auth = [Depends(require_token)]
    ctx = V1Context(engine=engine, settings=settings, auth=auth, owner=client_owner)
    r = APIRouter(prefix=PREFIX, responses=M.ERROR_RESPONSES)

    # ── system ────────────────────────────────────────────────────────────
    @r.get("/status", response_model=M.StatusResponse, tags=["system"],
           summary="Device, connection, t_int, averaging, profile, unit, control, jobs")
    async def status():
        st = engine.status()
        st["api_version"] = "1"
        st["token_required"] = settings.require_token
        return st

    @r.get("/backends", response_model=M.BackendsResponse, tags=["system"])
    async def backends():
        return {"backends": engine.list_backends(), "selected": engine.selected_backend()}

    @r.get("/devices", response_model=M.DevicesResponse, tags=["system"])
    async def devices(backend: str | None = None):
        b = backend or engine.selected_backend()
        return {"backend": b, "devices": await asyncio.to_thread(engine.scan, b)}

    # ── device ────────────────────────────────────────────────────────────
    @r.post("/connect", response_model=M.ConnectionResponse, tags=["device"], dependencies=auth)
    async def connect(body: M.ConnectRequest, request: Request):
        return await ctx.call(engine.connect, body.backend, body.device_path,
                              owner=client_owner(request))

    @r.post("/disconnect", response_model=M.ConnectionResponse, tags=["device"], dependencies=auth)
    async def disconnect(request: Request):
        return await ctx.call(engine.disconnect, owner=client_owner(request))

    @r.get("/acquisition", tags=["device"], summary="Current acquisition / processing settings")
    async def get_acquisition():
        keys = M.AcquisitionSettings.model_fields.keys()
        cfg = engine.cfg
        out = {k: cfg.get(k) for k in keys if k != "paused"}
        out["paused"] = engine.paused
        out["integration_bounds_us"] = list(engine.int_bounds_us())
        return json_safe(out)

    @r.put("/acquisition", response_model=M.AcquisitionResponse, tags=["device"], dependencies=auth)
    async def put_acquisition(body: M.AcquisitionSettings, request: Request):
        d = body.model_dump(exclude_none=True)
        if "paused" in d:
            d["pause"] = d.pop("paused")
        if not d:
            raise ApiError(400, "bad_request", "No setting given.")
        applied = await ctx.call(engine.configure, d, owner=client_owner(request))
        st = engine.status()
        st["token_required"] = settings.require_token
        return {"applied": json_safe(applied), "status": st}

    @r.post("/auto-exposure", response_model=M.AutoExposureResponse, tags=["device"],
            dependencies=auth, summary="One-shot auto exposure to the target band (blocking)")
    async def auto_exposure(body: M.AutoExposureRequest, request: Request):
        if body.target_frac_lo >= body.target_frac_hi:
            raise ApiError(422, "validation_error", "target_frac_lo must be < target_frac_hi.")
        return await ctx.call(engine.auto_expose, (body.target_frac_lo, body.target_frac_hi),
                              apply=body.apply, owner=client_owner(request))

    @r.get("/control", response_model=M.ControlInfo, tags=["device"])
    async def get_control():
        return engine.control_info()

    @r.post("/control", response_model=M.ControlInfo, tags=["device"], dependencies=auth,
            summary="Acquire / release an explicit control lease")
    async def post_control(body: M.ControlRequest, request: Request):
        owner = client_owner(request)
        if body.action == "acquire":
            return await ctx.call(engine.acquire_control, owner, body.reason, force=body.force)
        return await ctx.call(engine.release_control, owner, force=body.force)

    # ── measurement ───────────────────────────────────────────────────────
    async def _submit_and_wait(submit, params: dict, request: Request, wait: bool,
                               timeout_s: float) -> JSONResponse:
        job = await ctx.call(submit, params, owner=client_owner(request))
        if wait:
            job = await ctx.wait_job(job.id, timeout_s)
        content: dict = {"job": job.to_dict(brief=True),
                         "location": f"{PREFIX}/jobs/{job.id}"}
        if job.status == "done" and isinstance(job.result, dict) and job.result.get("measurement_id"):
            content["measurement"] = measurement_to_json(
                engine.get_measurement(job.result["measurement_id"]))
        elif job.status in JOB_FINAL:
            content["job"] = job.to_dict()
        if job.status == "failed" and job.error:
            e = job.error
            code = e.get("code", "job_failed")
            status = {"unprocessable": 422, "not_connected": 503, "control_busy": 409,
                      "not_found": 404}.get(code, 500 if code == "job_failed" else 400)
            return _err(status, code, e.get("message", "job failed"),
                        {"job": job.to_dict()})
        status = 200 if job.status in JOB_FINAL else 202
        return JSONResponse(status_code=status, content=json_safe(content),
                            headers={"Location": f"{PREFIX}/jobs/{job.id}"})

    @r.post("/measure", response_model=M.MeasureResponse, tags=["measurement"], dependencies=auth,
            summary="Single measurement (job). wait=true answers with the measurement; "
                    "202 + job while running or waiting for a user step")
    async def measure(body: M.MeasureRequest, request: Request):
        p = body.model_dump()
        if p.get("integration_ms") == "auto":
            p["integration_ms"] = None
            p["auto_exposure"] = True
        return await _submit_and_wait(engine.submit_measure, p, request, body.wait, body.timeout_s)

    @r.post("/series", tags=["measurement"], dependencies=auth,
            summary="Measurement series (job): count × measurement every interval_s")
    async def series(body: M.SeriesRequest, request: Request):
        p = body.model_dump()
        if p.get("integration_ms") == "auto":
            p["integration_ms"] = None
            p["auto_exposure"] = True
        return await _submit_and_wait(engine.submit_series, p, request, body.wait, body.timeout_s)

    @r.post("/dark", tags=["measurement"], dependencies=auth,
            summary="Capture + store a per-pixel dark at t_int (job; user step 'cover' "
                    "unless the device has a shutter)")
    async def dark(body: M.DarkRequest, request: Request):
        return await _submit_and_wait(engine.submit_dark, body.model_dump(), request,
                                      body.wait, body.timeout_s)

    # ── jobs ──────────────────────────────────────────────────────────────
    def _own_job(job_id: str, request: Request) -> None:
        """Only the client that started a job may confirm its user steps or
        cancel it remotely (the desktop can always cancel)."""
        j = engine.jobs.get(job_id)
        owner = client_owner(request)
        if j.owner != owner:
            raise ApiError(409, "control_busy",
                           f"Job '{job_id}' belongs to '{j.owner}'.",
                           {"owner": j.owner, "job_id": job_id})
    @r.get("/jobs", response_model=list[M.JobBrief], tags=["jobs"])
    async def jobs(active: bool = False):
        js = engine.jobs.active() if active else engine.jobs.list()
        return json_safe([j.to_dict(brief=True) for j in reversed(js)])

    @r.get("/jobs/{job_id}", response_model=M.Job, tags=["jobs"])
    async def job(job_id: str, wait_s: float = Query(0.0, ge=0, le=600)):
        j = engine.jobs.get(job_id)
        if wait_s > 0 and j.status not in JOB_FINAL and j.status != JOB_WAITING:
            j = await ctx.wait_job(job_id, wait_s)
        return json_safe(j.to_dict())

    @r.post("/jobs/{job_id}/cancel", response_model=M.Job, tags=["jobs"], dependencies=auth)
    async def cancel(job_id: str, request: Request):
        _own_job(job_id, request)
        j = engine.jobs.cancel(job_id)
        return json_safe(j.to_dict())

    @r.post("/jobs/{job_id}/continue", response_model=M.Job, tags=["jobs"], dependencies=auth,
            summary="Confirm a user step (cover / uncover …) of a waiting job")
    async def cont(job_id: str, request: Request):
        _own_job(job_id, request)
        j = engine.jobs.continue_job(job_id)
        await asyncio.sleep(0.05)
        return json_safe(j.to_dict())

    # ── data ──────────────────────────────────────────────────────────────
    @r.get("/spectrum/latest", response_model=M.SpectrumResponse, tags=["data"],
           summary="Latest live spectrum (optional λ window and downsampling)")
    async def spectrum_latest(downsample: int = Query(1, ge=1, le=1000),
                              wl_min: float | None = None, wl_max: float | None = None,
                              max_points: int | None = Query(None, ge=2, le=100000)):
        ev = engine.last_frame
        if ev is None:
            raise ApiError(503, "not_connected", "No live spectrum yet.")
        wl, v = ev.wavelengths, ev.pixels
        sel = np.ones(wl.size, bool)
        if wl_min is not None:
            sel &= wl >= wl_min
        if wl_max is not None:
            sel &= wl <= wl_max
        wl, v = wl[sel], v[sel]
        step = int(downsample)
        if max_points and wl.size > max_points:
            step = max(step, int(np.ceil(wl.size / max_points)))
        wl, v = wl[::step], v[::step]
        st = engine.calibration_status()
        from datetime import datetime
        return json_safe({
            "seq": ev.seq,
            "timestamp": datetime.fromtimestamp(ev.timestamp).astimezone().isoformat(
                timespec="milliseconds"),
            "unit": ev.unit, "unit_label": processing.unit_label(ev.unit),
            "integration_us": ev.integration_us, "fps": ev.fps,
            "n_points": int(wl.size), "downsample": step,
            "wavelengths": [round(float(x), 3) for x in wl],
            "values": v, "valid_range_nm": st.get("valid_range_nm"),
            "peaks": ev.peaks, "calibration": st})

    @r.get("/measurements", response_model=list[M.MeasurementSummary], tags=["data"])
    async def measurements(limit: int = Query(100, ge=1, le=1000)):
        out = []
        for m in reversed(engine.list_measurements()[-limit:]):
            a = m["acquisition"]
            out.append({"id": m["id"], "timestamp": m["timestamp"], "label": m.get("label", ""),
                        "unit": m["unit"], "integration_ms": a["integration_ms"],
                        "frames": a["frames"], "peak_frac": a["peak_frac"]})
        return json_safe(out)

    @r.get("/measurements/{mid}", response_model=M.Measurement, tags=["data"])
    async def measurement(mid: str, arrays: bool = True):
        return measurement_to_json(engine.get_measurement(mid), include_arrays=arrays)

    @r.get("/measurements/{mid}/export", tags=["data"],
           summary="Export a measurement as CSV or JSON (file download)")
    async def measurement_export(mid: str, format: str = Query("csv", pattern="^(csv|json)$")):
        m = engine.get_measurement(mid)
        if format == "json":
            import json
            body = json.dumps(measurement_to_json(m), indent=1)
            return Response(body, media_type="application/json", headers={
                "Content-Disposition": f'attachment; filename="{mid}.json"'})
        from engine import CSV_MEDIA_TYPE, csv_bytes
        return Response(csv_bytes(measurement_to_csv(m)), media_type=CSV_MEDIA_TYPE, headers={
            "Content-Disposition": f'attachment; filename="{mid}.csv"'})

    # ── further groups ────────────────────────────────────────────────────
    for name in GROUPS:
        try:
            mod = importlib.import_module(name)
        except ImportError as e:
            if getattr(e, "name", None) == name:
                print(f"[api] optional API group '{name}' not available — skipped")
                continue
            raise
        mod.register(r, ctx)

    app.include_router(r)

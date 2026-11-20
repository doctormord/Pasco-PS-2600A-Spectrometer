"""
api_v1_calibration.py — /api/v1 calibration endpoints.

  GET  /calibration/profiles            selectable profiles of the connected device
  GET  /calibration/status
  POST /calibration/select              activate a profile (auth)
  POST /calibration/profiles/copy       copy a profile from another unit (auth)
  GET  /calibration/profiles/{id}/export
  POST /calibration/profiles/import     (auth)
  GET  /calibration/references
  GET  /calibration/darks
  POST /calibration/runs                start a calibration job (auth, 202)
  GET  /calibration/runs/{job_id}/protocol?format=pdf|csv
  POST /calibration/derivations         derive new optics from a known profile (auth, 202)
  POST /calibration/references          import a lamp file (auth)
  DELETE /calibration/references/{id}   remove an imported lamp (auth; not an MCP tool)

Deleting profiles / darks is intentionally not offered. User steps (cover /
uncover) of a run use the generic job endpoints (continue / cancel).
"""
from __future__ import annotations

import asyncio
import json
import os
from typing import Literal, Optional, Union

import api
from fastapi import APIRouter, Query, Request
from fastapi.responses import FileResponse, Response
from pydantic import BaseModel, ConfigDict, Field

import api_models as M
import calibration_profiles as calprof
from api import ApiError, import_profile_payload, reference_list
from api_v1 import V1Context
from engine import EngineError, NeedsConfirmation, NotFound, json_safe


def register(router: APIRouter, ctx: V1Context) -> None:
    engine = ctx.engine
    auth = ctx.auth

    @router.get("/calibration/profiles", tags=["calibration"],
                summary="Selectable calibration profiles of the connected device")
    async def get_profiles(include_old: bool = False):
        return await asyncio.to_thread(api.profiles_payload, engine, include_old)

    @router.get("/calibration/status", tags=["calibration"])
    async def get_status():
        return json_safe(engine.calibration_status())

    @router.post("/calibration/select", tags=["calibration"], dependencies=auth,
                 summary="Activate a calibration profile ('None', 'Device default' or id)")
    async def select(body: M.SelectProfileRequest, request: Request):
        try:
            res = await asyncio.to_thread(engine.select_profile, body.selection,
                                          confirm=body.confirm, owner=ctx.owner(request))
        except NeedsConfirmation as e:
            d = e.details if isinstance(e.details, dict) else {}
            raise ApiError(409, "needs_confirmation", str(e),
                           {"state": d.get("state"), "message": d.get("message", str(e))})
        except EngineError as e:
            raise ApiError(e.status, e.code, str(e), e.details)
        return json_safe(res)

    @router.post("/calibration/profiles/copy", tags=["calibration"], dependencies=auth,
                 summary="Copy a profile of another unit to the connected device")
    async def copy_profile(body: M.CopyProfileRequest, request: Request):
        await ctx.call(engine.check_control, ctx.owner(request))
        dev, serial, _fp = engine.calibration_context()
        pid = body.id.strip()
        if not dev or not pid:
            raise ApiError(400, "bad_request", "Device and profile id required.")
        try:
            cp = await asyncio.to_thread(calprof.copy_profile_to_device, pid, serial, dev)
        except KeyError:
            raise ApiError(404, "not_found", f"Profile '{pid}' not found.")
        except ValueError as e:
            raise ApiError(400, "bad_request", str(e))
        return {"ok": True, "id": cp["id"], "label": calprof.profile_label(cp)}

    @router.get("/calibration/profiles/{profile_id}/export", tags=["calibration"],
                summary="Profile as JSON file download")
    async def export_profile(profile_id: str):
        p = await asyncio.to_thread(calprof.get_profile_by_id, profile_id)
        if p is None:
            raise ApiError(404, "not_found", f"Profile '{profile_id}' not found.")
        return Response(content=json.dumps(p, indent=1, allow_nan=False),
                        media_type="application/json", headers={
                            "Content-Disposition":
                            f'attachment; filename="calibration_profile_{profile_id}.json"'})

    @router.post("/calibration/profiles/import", tags=["calibration"], dependencies=auth,
                 summary="Import a profile (body = profile JSON as exported)")
    async def import_profile(body: dict):
        try:
            p = await asyncio.to_thread(import_profile_payload, body)
        except (ValueError, KeyError, TypeError, AttributeError, OSError) as e:
            raise ApiError(400, "bad_request", f"Import failed: {e}")
        return {"ok": True, "id": p["id"], "label": calprof.profile_label(p)}

    @router.get("/calibration/references", tags=["calibration"],
                summary="Reference sources (lamps) available for a calibration run")
    async def references():
        return await asyncio.to_thread(reference_list)

    @router.get("/calibration/darks", tags=["calibration"],
                summary="Stored dark spectra (metadata) of the connected device")
    async def darks():
        import calibration_capture as cc

        def work():
            dev, serial, _fp = engine.calibration_context()
            return json_safe(cc.list_darks(dev, serial) if dev else [])
        return await asyncio.to_thread(work)

    @router.post("/calibration/runs", tags=["calibration"], dependencies=auth,
                 status_code=202,
                 summary="Start a calibration run (job; user steps via /jobs/{id}/continue)")
    async def start_run(body: M.CalibrationRunRequest, request: Request):
        params = body.model_dump()
        if params.get("integration_ms") == "auto":
            params["integration_ms"] = None
        job = await ctx.call(engine.submit_calibration, params, owner=ctx.owner(request))
        return ctx.job_response(job)

    @router.get("/calibration/runs/{job_id}/protocol", tags=["calibration"],
                summary="Protocol file (PDF or CSV) written by a calibration job")
    async def protocol(job_id: str, format: str = Query("pdf", pattern="^(pdf|csv)$")):
        try:
            job = engine.jobs.get(job_id)
        except NotFound:
            raise ApiError(404, "not_found", f"Job '{job_id}' not found.")
        if job.kind != "calibration":
            raise ApiError(404, "not_found", f"Job '{job_id}' is not a calibration job.")
        res = job.result if isinstance(job.result, dict) else {}
        paths = [p for p in (res.get("protocol") or [])
                 if str(p).lower().endswith("." + format)]
        if not paths or not os.path.isfile(paths[0]):
            raise ApiError(404, "not_found", f"No {format} protocol for job '{job_id}'.")
        path = paths[0]
        return FileResponse(path, media_type="application/pdf" if format == "pdf" else "text/csv",
                            filename=os.path.basename(path))

    # ── derivation (new optics from a known profile) ──────────────────────
    @router.post("/calibration/derivations", tags=["calibration"], dependencies=auth,
                 status_code=202,
                 summary="Derive the relative profile of new optics from a known profile "
                         "(job; user steps mount_known / cover / uncover / swap_optics)")
    async def start_derivation(body: DerivationRequest, request: Request):
        params = body.model_dump()
        if params.get("integration_ms") == "auto":
            params["integration_ms"] = None
        job = await ctx.call(engine.submit_derivation, params, owner=ctx.owner(request))
        return ctx.job_response(job)

    # ── reference sources (lamp import / removal) ─────────────────────────
    @router.post("/calibration/references", tags=["calibration"], dependencies=auth,
                 summary="Import a calibrated lamp (LMP / CSV text) into the reference library")
    async def import_reference(body: ReferenceImport, request: Request):
        import tempfile
        import reference_sources as rs
        engine.check_control(ctx.owner(request))
        ext = os.path.splitext(body.filename)[1].lower() or ".csv"
        if ext not in (".lmp", ".csv", ".txt"):
            raise ApiError(422, "unprocessable", "File type must be .lmp, .csv or .txt.")

        def work():
            d = tempfile.mkdtemp()
            path = os.path.join(d, os.path.basename(body.filename) or ("ref" + ext))
            try:
                with open(path, "w", encoding="utf-8") as f:
                    f.write(body.content)
                return rs.ReferenceLibrary().import_file(
                    path, name=body.name, serial=body.serial, unit=body.unit,
                    valid_min_nm=body.valid_min_nm, valid_max_nm=body.valid_max_nm,
                    input_optic=body.input_optic, notes=body.notes)
            finally:
                try:
                    os.remove(path)
                    os.rmdir(d)
                except OSError:
                    pass
        try:
            src = await asyncio.to_thread(work)
        except ValueError as e:
            raise ApiError(400, "bad_request", f"Import failed: {e}")
        return json_safe({"ok": True, "id": src.id, "name": src.name,
                          "valid_min_nm": src.valid_min_nm, "valid_max_nm": src.valid_max_nm})

    @router.delete("/calibration/references/{source_id}", tags=["calibration"],
                   dependencies=auth,
                   summary="Remove an imported reference source (built-ins cannot be "
                           "removed; not offered as an MCP tool)")
    async def delete_reference(source_id: str, request: Request):
        import reference_sources as rs
        engine.check_control(ctx.owner(request))
        lib = rs.ReferenceLibrary()
        src = lib.get(source_id)
        if src is None:
            raise ApiError(404, "not_found", f"Reference '{source_id}' not found.")
        if src.builtin:
            raise ApiError(400, "bad_request", "Built-in reference sources cannot be removed.")
        return {"ok": bool(lib.remove(source_id))}


class DerivationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    known_profile_id: str
    optics: M.OpticsSpec
    integration_ms: Optional[Union[float, Literal["auto"]]] = "auto"
    frames: Optional[int] = Field(None, ge=2, le=1000)
    allow_implausible: bool = False
    activate: bool = True


class ReferenceImport(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(..., min_length=1)
    serial: str = ""
    unit: str = "uW/cm2/nm"
    valid_min_nm: Optional[float] = None
    valid_max_nm: Optional[float] = None
    input_optic: str = ""
    notes: str = ""
    filename: str = Field("reference.csv", description="Original file name (.lmp or .csv)")
    content: str = Field(..., description="File content (text: LMP or 2-column CSV)")

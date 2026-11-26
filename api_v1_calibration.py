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
  POST /calibration/verifications       verify a profile against a reference (auth, 202)
  GET  /calibration/verification        last verification result
  GET  /calibration/reference-overlay   reference curve scaled to the current display
  GET  /calibration/linearity           linearity state + records of the connected unit
  POST /calibration/linearity/runs      linearity calibration / check (auth, 202)
  POST /calibration/linearity/select    activate a linearity record or switch off (auth)
  GET  /calibration/linearity/runs/{job_id}/protocol?format=pdf|csv

Deleting profiles / darks is intentionally not offered. User steps (cover /
uncover) of a run use the generic job endpoints (continue / cancel).
"""
from __future__ import annotations

import asyncio
import json
import os
from typing import Annotated, Literal, Optional, Union

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
    async def import_profile(body: dict, request: Request):
        engine.check_control(ctx.owner(request))
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
        if job.kind not in ("calibration", "derivation"):
            raise ApiError(404, "not_found", f"Job '{job_id}' is not a calibration job.")
        res = job.result if isinstance(job.result, dict) else {}
        paths = [p for p in (res.get("protocol") or [])
                 if str(p).lower().endswith("." + format)]
        if not paths or not os.path.isfile(paths[0]):
            raise ApiError(404, "not_found", f"No {format} protocol for job '{job_id}'.")
        path = paths[0]
        return FileResponse(path, media_type="application/pdf" if format == "pdf" else "text/csv",
                            filename=os.path.basename(path))

    # ── verification (hardware test point 6) ──────────────────────────────
    @router.post("/calibration/verifications", tags=["calibration"], dependencies=auth,
                 status_code=202,
                 summary="Measure the reference source with a profile through the live "
                         "pipeline and compare with the reference file (job)")
    async def start_verification(body: M.VerificationRequest, request: Request):
        job = await ctx.call(engine.submit_verification, body.model_dump(),
                             owner=ctx.owner(request))
        return ctx.job_response(job)

    @router.get("/calibration/verification", tags=["calibration"],
                summary="Last verification result (deviation per 50-nm band)")
    async def last_verification(curve: bool = False):
        v = engine.last_verification
        if v is None:
            raise ApiError(404, "not_found", "No verification has been run yet.")
        return json_safe(v if curve else {k: x for k, x in v.items() if k != "curve"})

    @router.get("/calibration/reference-overlay", tags=["calibration"],
                summary="Reference curve on the axis, scaled to the current display")
    async def reference_overlay(reference_id: Optional[str] = None):
        rid = reference_id or engine.cfg.get("scope_reference_id", "")
        ev = engine.last_frame
        if ev is None:
            raise ApiError(404, "not_found", "No frame yet.")
        disp = getattr(ev, "display", None)
        dunit = getattr(ev, "display_unit", None) or ev.unit
        y = engine.reference_overlay(rid, ev.wavelengths,
                                     ev.pixels if disp is None else disp, dunit)
        if y is None:
            raise ApiError(404, "not_found", f"Reference {rid!r} not available.")
        return json_safe({"reference_id": rid, "unit": dunit,
                          "wavelengths": [round(float(w), 3) for w in ev.wavelengths],
                          "values": y})

    # ── linearity (hardware test point 5) ─────────────────────────────────
    @router.get("/calibration/linearity", tags=["calibration"],
                summary="Linearity state and stored records of the connected unit")
    async def linearity_state():
        import calibration_linearity as cl
        dev, serial, _fp = engine.calibration_context()
        entries = [cl.entry_summary(e) for e in cl.entries_for(dev, serial)] if dev else []
        return json_safe({"device": dev, "serial": serial,
                          "state": engine.linearity_info,
                          "active_id": cl.active_id(dev, serial) if dev else None,
                          "entries": entries})

    @router.post("/calibration/linearity/runs", tags=["calibration"], dependencies=auth,
                 status_code=202,
                 summary="Linearity calibration (or check on devices with a factory "
                         "linearisation) as a job with cover/uncover user steps")
    async def start_linearity(body: M.LinearityRunRequest, request: Request):
        params = body.model_dump()
        if params.get("integration_ms") == "auto":
            params["integration_ms"] = None
        job = await ctx.call(engine.submit_linearity, params, owner=ctx.owner(request))
        return ctx.job_response(job)

    @router.post("/calibration/linearity/select", tags=["calibration"], dependencies=auth,
                 summary="Activate a linearity record of this unit (null = off)")
    async def select_linearity(body: M.LinearitySelectRequest, request: Request):
        info = await ctx.call(engine.select_linearity, body.entry_id,
                              owner=ctx.owner(request))
        return json_safe({"ok": True, "state": info})

    @router.get("/calibration/linearity/runs/{job_id}/protocol", tags=["calibration"],
                summary="Protocol file (PDF or CSV) written by a linearity job")
    async def linearity_protocol(job_id: str,
                                 format: str = Query("pdf", pattern="^(pdf|csv)$")):
        try:
            job = engine.jobs.get(job_id)
        except NotFound:
            raise ApiError(404, "not_found", f"Job {job_id!r} not found.")
        if job.kind != "linearity":
            raise ApiError(404, "not_found", f"Job {job_id!r} is not a linearity job.")
        res = job.result if isinstance(job.result, dict) else {}
        paths = [p for p in (res.get("protocol") or [])
                 if str(p).lower().endswith("." + format)]
        if not paths or not os.path.isfile(paths[0]):
            raise ApiError(404, "not_found", f"No {format} protocol for job {job_id!r}.")
        return FileResponse(paths[0], media_type="application/pdf" if format == "pdf"
                            else "text/csv", filename=os.path.basename(paths[0]))

    # ── derivation (new optics from a known profile) ──────────────────────
    @router.post("/calibration/derivations", tags=["calibration"], dependencies=auth,
                 status_code=202,
                 summary="Derive the relative profile of new optics from a known profile "
                         "(job; user steps mount_known / cover / uncover / swap_optics "
                         "/ swap_back via /jobs/{id}/continue)")
    async def start_derivation(body: DerivationRequest, request: Request):
        params = body.model_dump()
        legacy = params.pop("integration_ms", None)      # both sides, if not set per side
        for k in ("integration_known_ms", "integration_new_ms"):
            v = params.get(k)
            if v is None:
                v = legacy
            params[k] = None if v in (None, "auto") else float(v)
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


_PosMs = Annotated[float, Field(gt=0)]


class DerivationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    known_profile_id: str
    optics: M.OpticsSpec
    integration_known_ms: Optional[Union[_PosMs, Literal["auto"]]] = Field(
        None, description="Integration time with the known optics (ms, or 'auto' = "
                            "auto exposure with that optics mounted; default auto)")
    integration_new_ms: Optional[Union[_PosMs, Literal["auto"]]] = Field(
        None, description="Integration time with the new optics (ms, or 'auto'; default auto)")
    integration_ms: Optional[Union[_PosMs, Literal["auto"]]] = Field(
        None, description="Legacy: one value for both sides; used only where the "
                          "side-specific value is not set")
    frames: Optional[int] = Field(None, ge=2, le=1000)
    allow_implausible: bool = False
    save_on_failed_checks: bool = Field(
        False, description="Save even if the source-drift or consistency checks failed")
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

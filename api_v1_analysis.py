"""
api_v1_analysis.py — /api/v1 analysis + report endpoints.

  POST /analyze/color   colour metrics (CCT, Duv, CRI, lux, ...) of a spectrum
  POST /analyze/peaks   peak detection
  POST /analyze/filter  filter transmission characterisation
  POST /analyze/tm30    TM-30 + CQS (needs the optional colour-science package)
  POST /reports         colour / filter / measurement report (file download,
                        or save=true -> written to the export folder, JSON path)

Note on ReportRequest.language: the CSV / report field labels are not
translated; the value is only recorded as "Language" in the report metadata.
"""
from __future__ import annotations

import asyncio
import io
import os

import numpy as np
from fastapi import APIRouter, Request
from fastapi.responses import Response

import api_models as M
import color_science as cs
import export_paths
import processing
from api import ApiError
from api_v1 import V1Context, resolve_spectrum
from engine import json_safe, measurement_to_csv
from filter_analysis import analyze_filter, metrics_rows, render_report

PREVIEW_POINTS = 200
_MEDIA = {"pdf": "application/pdf", "png": "image/png", "csv": "text/csv"}


def _source(body, m: dict | None) -> dict:
    if m is not None:
        return {"measurement_id": m["id"]}
    if getattr(body, "wavelengths", None) is not None:
        return {"data": True}
    return {"live": True}


def _rows_csv(rows) -> str:
    import csv
    sio = io.StringIO()
    w = csv.writer(sio)
    w.writerow(["Metric", "Value"])
    for k, v in rows:
        w.writerow([k, v])
    return sio.getvalue()


def _lux_cal(engine, unit: str, override: float | None = None) -> float:
    if override is not None:
        return float(override)
    v = engine.lux_calibration(unit)
    return float(v) if v else 0.0


def _filter_inputs(engine, body: M.FilterRequest) -> tuple[np.ndarray, np.ndarray, np.ndarray, dict]:
    """(wl, reference, sample, info) from two measurement ids or explicit arrays."""
    if body.reference_id or body.sample_id:
        if not (body.reference_id and body.sample_id):
            raise ApiError(400, "bad_request", "reference_id and sample_id are both required.")
        ref_m = engine.get_measurement(body.reference_id)
        smp_m = engine.get_measurement(body.sample_id)
        if ref_m["unit"] != smp_m["unit"]:
            raise ApiError(422, "unprocessable",
                           f"Reference ({ref_m['unit']}) and sample ({smp_m['unit']}) "
                           "have different units.")
        wl = np.asarray(ref_m["wavelengths"], float)
        ref = np.asarray(ref_m["values"], float)
        swl = np.asarray(smp_m["wavelengths"], float)
        smp = np.asarray(smp_m["values"], float)
        resampled = False
        if swl.shape != wl.shape or not np.allclose(swl, wl):
            smp = np.interp(wl, swl, smp)
            resampled = True
        return wl, ref, smp, {"reference_id": body.reference_id, "sample_id": body.sample_id,
                              "sample_resampled": resampled, "unit": ref_m["unit"]}
    if body.wavelengths is None or body.reference is None or body.sample is None:
        raise ApiError(400, "bad_request", "Give reference_id + sample_id, or wavelengths + "
                                           "reference + sample.")
    try:
        wl = np.asarray(body.wavelengths, float)
        ref = np.asarray(body.reference, float)
        smp = np.asarray(body.sample, float)
    except (TypeError, ValueError):
        raise ApiError(400, "bad_request", "Arrays must be numeric.")
    return wl, ref, smp, {"data": True}


def _analyze_filter(wl, ref, smp) -> dict:
    try:
        return analyze_filter(wl, ref, smp)
    except ValueError as e:
        raise ApiError(400, "bad_request", str(e))


def _preview(wl: np.ndarray, T: np.ndarray, n: int = PREVIEW_POINTS) -> list[dict]:
    if wl.size == 0:
        return []
    idx = np.unique(np.linspace(0, wl.size - 1, min(n, wl.size)).round().astype(int))
    return [{"wavelength_nm": float(wl[i]), "transmission": T[i]} for i in idx]


def register(router: APIRouter, ctx: V1Context) -> None:
    engine = ctx.engine

    @router.post("/analyze/color", tags=["analysis"],
                 summary="Colour metrics of a measurement, explicit data or the live spectrum")
    async def analyze_color(body: M.ColorRequest):
        def work():
            wl, values, unit, m = resolve_spectrum(engine, body)
            if body.lux_calibration is not None:
                spd, _ = processing.photometric_inputs(values, unit)
                met = cs.measure_all(wl, spd, lux_calibration=float(body.lux_calibration))
            else:
                met = engine.color_metrics_for(wl, values, unit)
            met = dict(met)
            met.pop("tcs_swatches", None)
            return {"source": _source(body, m), "unit": unit, "metrics": met}
        return json_safe(await ctx.call(work))

    @router.post("/analyze/peaks", tags=["analysis"], summary="Peak detection")
    async def analyze_peaks(body: M.PeaksRequest):
        def work():
            wl, values, unit, m = resolve_spectrum(engine, body)
            v = np.nan_to_num(values, nan=0.0, posinf=0.0, neginf=0.0)
            peaks = processing.detect_peaks(
                v, wl, savgol_window=body.savgol_window, savgol_order=body.savgol_order,
                min_prominence=body.min_prominence, min_distance_px=body.min_distance_px,
                min_height=body.min_height, max_count=body.max_count)
            return {"source": _source(body, m), "unit": unit, "peaks": [
                {"index": int(p["index"]), "wavelength_nm": float(p["wavelength_nm"]),
                 "value": float(p.get("intensity_adc", v[int(p["index"])]))} for p in peaks]}
        return json_safe(await ctx.call(work))

    @router.post("/analyze/filter", tags=["analysis"],
                 summary="Filter transmission metrics (reference vs. sample)")
    async def analyze_filter_ep(body: M.FilterRequest):
        def work():
            wl, ref, smp, info = _filter_inputs(engine, body)
            res = _analyze_filter(wl, ref, smp)
            rows = metrics_rows(res)
            T = np.asarray(res["transmission"], float)
            out = {k: v for k, v in res.items() if k not in ("transmission", "wavelengths")}
            out.update(source=info, n_points=int(T.size), rows=rows,
                       transmission_preview=_preview(np.asarray(res["wavelengths"], float), T))
            return out
        return json_safe(await ctx.call(work))

    @router.post("/analyze/tm30", tags=["analysis"],
                 summary="IES TM-30 and CQS (needs the optional colour-science package)")
    async def analyze_tm30(body: M.SpectrumInput):
        def work():
            wl, values, unit, m = resolve_spectrum(engine, body)
            spd, _ = processing.photometric_inputs(values, unit)
            tm = cs.tm30_metrics(wl, spd)
            if tm is None:
                raise ApiError(503, "unavailable", "TM-30 needs the optional 'colour-science' "
                                                   "package on the server.")
            return {"source": _source(body, m), "unit": unit, "tm30": tm,
                    "cqs": cs.cqs_metrics(wl, spd), "bin_colors": cs.TM30_BIN_COLORS}
        return json_safe(await ctx.call(work))

    # ── reports ───────────────────────────────────────────────────────────
    def _meta_for_measurement(m: dict, base: dict) -> dict:
        a, dev, cal = m.get("acquisition", {}), m.get("device", {}), m.get("calibration", {})
        meta = dict(base)
        meta["Spectrometer"] = dev.get("backend") or meta.get("Spectrometer")
        meta["Serial"] = dev.get("serial") or meta.get("Serial")
        meta["Date/time"] = m.get("timestamp")
        if a.get("integration_ms") is not None:
            meta["Exposure"] = f"{float(a['integration_ms']):.1f} ms"
        meta["Calibration"] = cal.get("label")
        meta["Measurement id"] = m["id"]
        return meta

    def build_report(body: M.ReportRequest) -> tuple[bytes, str, dict, np.ndarray | None]:
        """(content, ext, meta, wl) — blocking."""
        fmt = body.format
        if body.kind == "measurement":
            if fmt != "csv":
                raise ApiError(422, "unprocessable", "kind 'measurement' only supports csv.")
            if not body.measurement_id:
                raise ApiError(400, "bad_request", "measurement_id required.")
            m = engine.get_measurement(body.measurement_id)
            meta = _meta_for_measurement(m, engine.report_meta())
            meta["Language"] = body.language
            return (measurement_to_csv(m).encode("utf-8"), "csv", meta,
                    np.asarray(m["wavelengths"], float))

        if body.kind == "color":
            sel = M.SpectrumInput(measurement_id=body.measurement_id)
            wl, values, unit, m = resolve_spectrum(engine, sel)
            spd, lc = processing.photometric_inputs(values, unit)
            lux = float(lc) if lc is not None else _lux_cal(engine, unit)
            peak = float(np.max(spd)) if spd.size else None
            meta = engine.report_meta(peak_adc=peak if unit == "counts" else None)
            if m is not None:
                meta = _meta_for_measurement(m, meta)
            meta["Language"] = body.language
            if fmt == "csv":
                data = _rows_csv(cs.color_report_rows(wl, spd, lux)).encode("utf-8")
                return data, "csv", meta, wl
            buf = io.BytesIO()
            try:
                cs.render_color_report(buf, wl, spd, fmt=fmt, lux_calibration=lux, meta=meta)
            except ImportError:
                raise ApiError(503, "unavailable", "matplotlib is not installed on the server.")
            return buf.getvalue(), fmt, meta, wl

        # filter
        wl, ref, smp, info = _filter_inputs(
            engine, M.FilterRequest(reference_id=body.reference_id, sample_id=body.sample_id))
        res = _analyze_filter(wl, ref, smp)
        peak = float(np.nanmax(smp)) if np.any(np.isfinite(smp)) else None
        meta = engine.report_meta(peak_adc=peak if info.get("unit") == "counts" else None)
        meta["Language"] = body.language
        if fmt == "csv":
            return (_rows_csv(metrics_rows(res, full=True)).encode("utf-8"), "csv", meta, wl)
        buf = io.BytesIO()
        try:
            render_report(buf, res, fmt=fmt, meta=meta)
        except ImportError:
            raise ApiError(503, "unavailable", "matplotlib is not installed on the server.")
        return buf.getvalue(), fmt, meta, wl

    @router.post("/reports", tags=["reports"],
                 summary="Colour / filter / measurement report as file download "
                         "(save=true: store in the export folder, answer JSON)")
    async def reports(body: M.ReportRequest, request: Request):
        content, ext, meta, wl = await ctx.call(build_report, body)
        stem = {"color": "color_report", "filter": "filter_report",
                "measurement": body.measurement_id or "measurement"}[body.kind]
        if body.save:
            def save():
                path, err = export_paths.export_path(
                    stem if body.kind != "measurement" else "measurement", ext,
                    export_paths.meta_from_report(meta, wl))
                os.makedirs(os.path.dirname(path), exist_ok=True)
                with open(path, "wb") as f:
                    f.write(content)
                return path, err
            path, err = await asyncio.to_thread(save)
            out = {"path": path, "kind": body.kind, "format": ext}
            if err:
                out["warning"] = err
            return json_safe(out)
        return Response(content=content, media_type=_MEDIA[ext], headers={
            "Content-Disposition": f'attachment; filename="{stem}.{ext}"'})

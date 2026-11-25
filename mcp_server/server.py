"""MCP server for the PS-2600A spectrometer application.

A thin layer over the REST API (/api/v1): every tool is one or a few HTTP calls
(see client.py). Results are compact JSON (<= 8 KB); full spectra, exports and
reports are written to SPECTRO_DATA_DIR and returned as file paths.

This module imports neither the engine, the API implementation nor any Qt
binding - it can run on another machine than the app.

Environment: SPECTRO_API_URL, SPECTRO_API_TOKEN, SPECTRO_CLIENT_ID,
SPECTRO_DATA_DIR (default <tempdir>/ps2600-mcp).
"""
import functools
import json
import math
import os
import re
import sys
import tempfile
from datetime import datetime
from typing import Any, Literal

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ResourceError, ToolError
from mcp.types import ToolAnnotations

from .client import ApiError, SpectroClient, env

SERVER_NAME = "ps2600-spectrometer"
MAX_RESULT_BYTES = 8000          # hard limit per tool result (JSON, UTF-8)
SPECTRUM_PREVIEW_POINTS = 32
MAX_SPECTRUM_POINTS = 512
SERIES_LIST_MAX = 50
FINAL_STATES = ("done", "failed", "cancelled")

INSTRUCTIONS = (
    "Controls a spectrometer (PASCO PS-2600A, Ocean HDX, ASEQ LR-2T or Demo) through the "
    "PS-2600A application's REST API. Typical flow: status() -> connect() if needed -> "
    "auto_expose() -> measure() -> analyze_color() / find_peaks() / create_report(). "
    "Long operations (series, dark, calibration) are jobs: poll job_status(); when a job is "
    "waiting_for_user, ask the user to perform the step and call continue_job() only after "
    "their confirmation. Wavelengths are in nm, integration times in ms. Full spectra and "
    "reports are saved to files; results only carry summaries."
)


# ---------------------------------------------------------------- helpers
def data_dir() -> str:
    d = env("SPECTRO_DATA_DIR") or os.path.join(tempfile.gettempdir(), "ps2600-mcp")
    os.makedirs(d, exist_ok=True)
    return d


def _safe(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]", "_", str(name))[:80] or "x"


def _stamp() -> str:
    return datetime.now().strftime("%Y%m%d-%H%M%S")


def _sig(v: float, digits: int = 6) -> float | None:
    if v is None or isinstance(v, bool):
        return v
    if math.isnan(v) or math.isinf(v):
        return None
    if v == 0:
        return 0.0
    return float(f"{v:.{digits}g}")


def _clean(o: Any) -> Any:
    """JSON-safe copy: NaN/inf -> null, floats to 6 significant digits."""
    if isinstance(o, float):
        return _sig(o)
    if isinstance(o, dict):
        return {str(k): _clean(v) for k, v in o.items()}
    if isinstance(o, (list, tuple)):
        return [_clean(v) for v in o]
    if o is None or isinstance(o, (bool, int, str)):
        return o
    return str(o)


def _dumps(o: Any) -> str:
    return json.dumps(o, separators=(",", ":"), ensure_ascii=False)


def _lists(o: Any):
    """(container, key) of every list longer than one element, depth first."""
    if isinstance(o, dict):
        items = list(o.items())
    elif isinstance(o, list):
        items = list(enumerate(o))
    else:
        return
    for k, v in items:
        if isinstance(v, list) and len(v) > 1:
            yield o, k
        if isinstance(v, (dict, list)):
            yield from _lists(v)


def _cut_strings(o: Any, n: int = 1200) -> None:
    items = o.items() if isinstance(o, dict) else enumerate(o) if isinstance(o, list) else ()
    for k, v in list(items):
        if isinstance(v, str) and len(v) > n:
            o[k] = v[:n] + "..."
        elif isinstance(v, (dict, list)):
            _cut_strings(v, n)


def fit(obj: Any, limit: int = MAX_RESULT_BYTES) -> str:
    """JSON text of `obj`, guaranteed <= `limit` bytes. If it does not fit, the
    longest lists are halved and the result is marked "truncated"."""
    obj = _clean(obj)
    text = _dumps(obj)
    if len(text.encode("utf-8")) <= limit:
        return text
    _cut_strings(obj)
    if isinstance(obj, dict):
        obj["truncated"] = True
        obj["truncated_note"] = "Result shortened to fit 8 KB; use save_full / export for all data."
    for _ in range(80):
        best = None
        for cont, key in _lists(obj):
            size = len(_dumps(cont[key]))
            if best is None or size > best[0]:
                best = (size, cont, key)
        if best is None:
            break
        _, cont, key = best
        cont[key] = cont[key][:max(1, len(cont[key]) // 2)]
        text = _dumps(obj)
        if len(text.encode("utf-8")) <= limit:
            return text
    text = _dumps(obj)
    if len(text.encode("utf-8")) <= limit:
        return text
    return _dumps({"truncated": True, "partial": text[:limit - 200]})


def _finite(v) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool) \
        and not math.isnan(v) and not math.isinf(v)


def _job_view(j: dict) -> dict:
    """Brief job dict plus a 'next' instruction for the assistant."""
    out = {k: j.get(k) for k in ("id", "kind", "status", "progress", "step", "message")}
    if isinstance(out["progress"], (int, float)):
        out["progress"] = round(out["progress"], 2)
    prompt = j.get("prompt")
    if prompt:
        out["prompt"] = prompt
    if j.get("error"):
        out["error"] = j["error"]
    st = j.get("status")
    if st == "waiting_for_user" and prompt:
        out["next"] = (f"Ask the user to: {prompt.get('message', '')} "
                       f"(step '{prompt.get('action', '')}'). After they confirm, call "
                       f"continue_job(job_id='{j.get('id')}').")
    elif st in ("queued", "running"):
        out["next"] = f"Still running - call job_status(job_id='{j.get('id')}') again."
    return out


def _status_view(st: dict) -> dict:
    cal = st.get("calibration") or {}
    ctl = st.get("control") or {}
    jobs = [{"id": j.get("id"), "kind": j.get("kind"), "status": j.get("status"),
             "progress": round(j.get("progress") or 0, 2), "prompt": j.get("prompt")}
            for j in (st.get("jobs") or []) if j.get("status") not in FINAL_STATES]
    return {
        "connected": st.get("connected"), "backend": st.get("backend"),
        "serial": st.get("serial"), "exposure_ms": st.get("exposure_ms"),
        "auto_exposure": st.get("auto_exposure"),
        "frames_to_average": st.get("frames_to_average"), "unit": st.get("unit"),
        "calibration": {"label": cal.get("label"), "state": cal.get("state"),
                        "type": cal.get("type"), "valid_range_nm": cal.get("valid_range_nm")},
        "control": {"owner": ctl.get("owner"), "reason": ctl.get("reason")} if ctl.get("owner")
        else {"owner": None},
        "jobs": jobs,
    }


def _profile_items(payload: dict) -> list[dict]:
    keys = ("value", "label", "state", "type", "unit", "valid_range_nm", "plausible")
    return [{k: it.get(k) for k in keys} for it in payload.get("items", [])]


def _color_view(c: dict | None) -> dict | None:
    if not c:
        return None
    ri = c.get("Ri") or []
    return {"cct": c.get("cct"), "duv": c.get("duv"), "x": c.get("x"), "y": c.get("y"),
            "Ra": c.get("Ra"), "R9": ri[8] if len(ri) > 8 else None, "lux": c.get("lux"),
            "peak_nm": c.get("peak_nm"), "dominant_nm": c.get("dominant_nm"),
            "centroid_nm": c.get("centroid_nm"), "fwhm_nm": c.get("fwhm_nm")}


def _thin(wl: list, vals: list, max_points: int) -> list[list[float]]:
    """<= max_points [wavelength, value] pairs (finite values only), evenly spread,
    always containing the maximum."""
    pts = [(w, v) for w, v in zip(wl, vals) if _finite(w) and _finite(v)]
    n = len(pts)
    k = max(2, min(int(max_points), MAX_SPECTRUM_POINTS))
    if n <= k:
        idx = list(range(n))
    else:
        idx = sorted({round(i * (n - 1) / (k - 1)) for i in range(k)})
        imax = max(range(n), key=lambda i: pts[i][1])
        if imax not in idx:
            near = min(idx, key=lambda i: abs(i - imax))
            idx.remove(near)
            idx.append(imax)
            idx.sort()
    return [[round(pts[i][0], 2), _sig(pts[i][1], 5)] for i in idx]


def _trapz(wl: list, vals: list) -> float | None:
    pts = [(w, v) for w, v in zip(wl, vals) if _finite(w) and _finite(v)]
    if len(pts) < 2:
        return None
    return sum((pts[i + 1][0] - pts[i][0]) * (pts[i + 1][1] + pts[i][1]) / 2
               for i in range(len(pts) - 1))


def _measurement_view(m: dict, csv_path: str | None, csv_error: str | None = None) -> dict:
    a = m.get("acquisition") or {}
    dev = m.get("device") or {}
    cal = m.get("calibration") or {}
    peaks = [{"wavelength_nm": p.get("wavelength_nm"),
              "value": p.get("intensity_adc", p.get("value")),
              "prominence": p.get("prominence")} for p in (m.get("peaks") or [])[:5]]
    out = {
        "id": m.get("id"), "timestamp": m.get("timestamp"), "label": m.get("label", ""),
        "device": {"backend": dev.get("backend"), "serial": dev.get("serial")},
        "acquisition": {k: a.get(k) for k in ("integration_ms", "frames", "peak_frac",
                                              "n_saturated", "dark", "dark_source")},
        "unit": m.get("unit"), "valid_range_nm": m.get("valid_range_nm"),
        "calibration": {"label": cal.get("label"), "state": cal.get("state"),
                        "type": cal.get("type")},
        "color": _color_view(m.get("color")), "peaks": peaks,
        "warnings": m.get("warnings") or [],
        "spectrum_preview": _thin(m.get("wavelengths") or [], m.get("values") or [],
                                  SPECTRUM_PREVIEW_POINTS),
        "csv_path": csv_path,
    }
    if csv_error:
        out["csv_error"] = csv_error
    return out


def _compact_result(res: Any, max_items: int = SERIES_LIST_MAX) -> Any:
    """Job result with long lists replaced by a count and the first items."""
    if isinstance(res, dict):
        return {k: _compact_result(v, max_items) for k, v in res.items()}
    if isinstance(res, list):
        if len(res) > max_items:
            return {"n": len(res), "first": [_compact_result(v, max_items) for v in res[:3]]}
        return [_compact_result(v, max_items) for v in res]
    return res


def _series_summary(res: dict) -> dict:
    ids = res.get("measurement_ids") or []
    rows = res.get("summary") or []
    out: dict = {"n": len(rows) or len(ids)}
    if len(rows) <= SERIES_LIST_MAX:
        out["measurements"] = [{"id": r.get("id"), "cct": r.get("cct"), "lux": r.get("lux"),
                                "peak_frac": r.get("peak_frac"), "peak_nm": r.get("peak_nm")}
                               for r in rows]
    else:
        out["note"] = (f"{len(rows)} measurements; only statistics shown. Fetch single ones "
                       "with get_spectrum / export_measurement (ids: first and last below).")
        out["first_id"] = rows[0].get("id")
        out["last_id"] = rows[-1].get("id")
    stats = {}
    for key in ("cct", "lux", "peak_frac"):
        vals = [r[key] for r in rows if _finite(r.get(key))]
        if vals:
            stats[key] = {"min": min(vals), "max": max(vals), "mean": sum(vals) / len(vals),
                          "n": len(vals)}
    out["stats"] = stats
    for k, v in res.items():
        if k not in ("measurement_ids", "summary"):
            out[k] = _compact_result(v)
    return out


def _job_with_result(j: dict) -> dict:
    out = _job_view(j)
    res = j.get("result")
    if res is not None and j.get("status") == "done":
        if j.get("kind") == "series" and isinstance(res, dict):
            out["result"] = _series_summary(res)
        else:
            out["result"] = _compact_result(res)
    return out


def _write(path: str, data: bytes) -> dict:
    with open(path, "wb") as f:
        f.write(data)
    return {"path": path, "bytes": len(data)}


# ------------------------------------------------------------ server build
def build_server(client: SpectroClient | None = None) -> MCPServer:
    """MCP server with all tools; `client` defaults to one built from the environment."""
    api = client or SpectroClient()
    server = MCPServer(SERVER_NAME, instructions=INSTRUCTIONS)

    def tool(name: str, description: str, *, read_only: bool = False,
             destructive: bool = False, idempotent: bool = False):
        ann = ToolAnnotations(read_only_hint=read_only,
                              destructive_hint=False if read_only else destructive,
                              idempotent_hint=idempotent, open_world_hint=False)

        def deco(fn):
            @functools.wraps(fn)
            async def wrapper(*args, **kwargs):
                try:
                    result = await fn(*args, **kwargs)
                except ApiError as e:
                    raise ToolError(str(e)) from None
                except OSError as e:
                    raise ToolError(f"File error: {e}") from None
                return fit(result)
            server.tool(name=name, description=description, annotations=ann,
                        structured_output=False)(wrapper)
            return fn
        return deco

    def resource(uri: str, description: str):
        def deco(fn):
            @functools.wraps(fn)
            async def wrapper():
                try:
                    return fit(await fn())
                except ApiError as e:
                    raise ResourceError(str(e)) from None
            server.resource(uri, name=uri.split("://")[1], description=description,
                            mime_type="application/json")(wrapper)
            return fn
        return deco

    async def get_status_view() -> dict:
        return _status_view(await api.get("/status"))

    async def job_wait(job_id: str, wait_s: float) -> dict:
        return await api.request("GET", f"/jobs/{job_id}", params={"wait_s": wait_s},
                                 timeout_s=wait_s + 15)

    async def save_export(mid: str, fmt: str) -> dict:
        data = await api.download("GET", f"/measurements/{mid}/export", params={"format": fmt})
        return _write(os.path.join(data_dir(), f"{_safe(mid)}.{fmt}"), data)

    # ---- device / acquisition ---------------------------------------------
    @tool("status",
          "Compact state of the spectrometer app: connection, backend, serial, exposure_ms, "
          "auto_exposure, frames_to_average, unit, active calibration (label, state, type, "
          "valid_range_nm), control owner and active jobs with prompts. Call this first.",
          read_only=True)
    async def status() -> dict:
        return await get_status_view()

    @tool("list_devices",
          "List available spectrometer backends and the devices found for one backend "
          "(default: the currently selected backend). Use before connect().",
          read_only=True)
    async def list_devices(backend: str | None = None) -> dict:
        b = await api.get("/backends")
        d = await api.get("/devices", backend=backend)
        return {"backends": b.get("backends"), "selected": b.get("selected"),
                "backend": d.get("backend"), "devices": d.get("devices")}

    @tool("connect",
          "Connect to a spectrometer. backend: name from list_devices (default: selected "
          "backend); device_path: device id from list_devices (default: first device found). "
          "Takes the control lease for this client.", idempotent=True)
    async def connect(backend: str | None = None, device_path: str | None = None) -> dict:
        return await api.post("/connect", {"backend": backend, "device_path": device_path})

    @tool("disconnect", "Disconnect the spectrometer and release control.", idempotent=True)
    async def disconnect() -> dict:
        return await api.post("/disconnect")

    @tool("set_acquisition",
          "Change live acquisition / processing settings; only given fields change. "
          "exposure_ms: integration time in ms (clamped to the device range); auto_exposure: "
          "continuous auto exposure of the live view; frames_to_average: 1-1000; paused; "
          "dark_correction; dark_mode: optical_black|parametric|stored; display_unit: "
          "counts|counts_per_s; irradiance_unit: uW/cm2/nm|W/m2/nm|mW/m2/nm; hot_pixel_filter; "
          "spatial_smoothing; fusion_enabled (multi-exposure fusion). Returns what was applied "
          "plus the compact status.", idempotent=True)
    async def set_acquisition(
            exposure_ms: float | None = None, auto_exposure: bool | None = None,
            frames_to_average: int | None = None, paused: bool | None = None,
            dark_correction: bool | None = None,
            dark_mode: Literal["optical_black", "parametric", "stored"] | None = None,
            display_unit: Literal["counts", "counts_per_s"] | None = None,
            irradiance_unit: Literal["uW/cm2/nm", "W/m2/nm", "mW/m2/nm"] | None = None,
            hot_pixel_filter: bool | None = None, spatial_smoothing: bool | None = None,
            fusion_enabled: bool | None = None) -> dict:
        body = {k: v for k, v in dict(
            exposure_ms=exposure_ms, auto_exposure=auto_exposure,
            frames_to_average=frames_to_average, paused=paused,
            dark_correction=dark_correction, dark_mode=dark_mode, display_unit=display_unit,
            irradiance_unit=irradiance_unit, hot_pixel_filter=hot_pixel_filter,
            spatial_smoothing=spatial_smoothing, fusion_enabled=fusion_enabled).items()
            if v is not None}
        if not body:
            raise ApiError("Give at least one setting to change.", code="bad_request")
        res = await api.put("/acquisition", body)
        return {"applied": res.get("applied"), "status": _status_view(res.get("status") or {})}

    @tool("auto_expose",
          "One-shot auto exposure: finds the integration time (ms) that puts the signal peak "
          "inside the target band (fractions of full scale, default 0.70-0.85). apply=True "
          "keeps the result as live exposure. Run before measure() when the light level changed. "
          "Returns exposure_ms, converged, peak_frac.")
    async def auto_expose(target_frac_lo: float = 0.70, target_frac_hi: float = 0.85,
                          apply: bool = True) -> dict:
        return await api.post("/auto-exposure", {"target_frac_lo": target_frac_lo,
                                                 "target_frac_hi": target_frac_hi,
                                                 "apply": apply}, timeout_s=120)

    @tool("capture_dark",
          "Capture and store a per-pixel dark spectrum at the given integration time "
          "(integration_ms, default: current exposure) and frame count (frames, default "
          "device setting). Starts a job; devices without a shutter first wait for the user "
          "to cover the light path (status waiting_for_user -> ask the user, then "
          "continue_job). Block the light, then continue, and unblock it again afterwards.")
    async def capture_dark(integration_ms: float | None = None,
                           frames: int | None = None) -> dict:
        res = await api.post("/dark", {"integration_ms": integration_ms, "frames": frames,
                                       "wait": False})
        job = await job_wait(res["job"]["id"], 2.0)
        return {"job": _job_view(job)}

    # ---- measurements -------------------------------------------------------
    def _measure_body(integration_ms, frames, dark, profile, label, include_color,
                      include_peaks) -> dict:
        body: dict = {"frames": frames, "dark": dark, "label": label,
                      "include_color": include_color, "include_peaks": include_peaks}
        if integration_ms is not None:
            body["integration_ms"] = integration_ms
        if profile:
            body["profile"] = profile
        return body

    @tool("measure",
          "Take one measurement (stored on the server). integration_ms: ms, 'auto' (auto "
          "exposure to 70-85 % of full scale) or omit for the current exposure; frames: frames "
          "averaged; dark: auto (like live view) | stored (stored dark at this t_int) | capture "
          "(capture new dark first; may need a user step) | none (raw counts, no calibration); "
          "profile: calibration selection for this measurement only. Returns a compact result: "
          "acquisition (peak_frac should be 0.3-0.9, n_saturated must be 0), colour metrics "
          "(cct, duv, x, y, Ra, R9, lux, peak/dominant/centroid nm, fwhm), top peaks, warnings, "
          "a 32-point preview, and csv_path of the full spectrum. If the job waits for a user "
          "step, a job + instruction is returned instead - then use continue_job.")
    async def measure(integration_ms: float | Literal["auto"] | None = None, frames: int = 1,
                      dark: Literal["auto", "stored", "capture", "none"] = "auto",
                      profile: str | None = None, label: str = "",
                      include_color: bool = True, include_peaks: bool = True,
                      timeout_s: float = 120, confirm_profile: bool = False) -> dict:
        body = _measure_body(integration_ms, frames, dark, profile, label, include_color,
                             include_peaks)
        body.update(wait=True, timeout_s=timeout_s, confirm_profile=confirm_profile)
        try:
            res = await api.post("/measure", body, timeout_s=timeout_s + 20)
        except ApiError as e:
            if e.code == "needs_confirmation":
                return {"needs_confirmation": True, "message": str(e),
                        "next": "Ask the user, then repeat with confirm_profile=True."}
            raise
        m = res.get("measurement")
        if not m:
            return {"job": _job_view(res.get("job") or {})}
        csv_path = csv_error = None
        try:
            csv_path = (await save_export(m["id"], "csv"))["path"]
        except (ApiError, OSError) as e:
            csv_error = str(e)
        return _measurement_view(m, csv_path, csv_error)

    @tool("start_series",
          "Start a measurement series as a background job: count measurements, one every "
          "interval_s seconds (start to start; 0 = back to back). Measurement parameters as in "
          "measure(). Returns the job id; follow with job_status(job_id). Use for stability / "
          "drift measurements.")
    async def start_series(count: int, interval_s: float = 0,
                           integration_ms: float | Literal["auto"] | None = None,
                           frames: int = 1,
                           dark: Literal["auto", "stored", "capture", "none"] = "auto",
                           profile: str | None = None, label: str = "",
                           include_color: bool = True, include_peaks: bool = True) -> dict:
        body = _measure_body(integration_ms, frames, dark, profile, label, include_color,
                             include_peaks)
        body.update(count=count, interval_s=interval_s, wait=False)
        res = await api.post("/series", body)
        return {"job": _job_view(res["job"])}

    @tool("job_status",
          "Status of a job (series, dark, calibration, measurement): status, progress 0-1, "
          "step, message, prompt, error and - when done - a compact result. For series: up to "
          "50 entries (id, cct, lux, peak_frac, peak_nm) or only min/max/mean statistics. If "
          "status is waiting_for_user, ask the user to do the step, then continue_job.",
          read_only=True)
    async def job_status(job_id: str) -> dict:
        return _job_with_result(await api.get(f"/jobs/{job_id}"))

    @tool("cancel_job", "Cancel a running or waiting job. Measurements already stored are kept.",
          destructive=True, idempotent=True)
    async def cancel_job(job_id: str) -> dict:
        out = _job_with_result(await api.post(f"/jobs/{job_id}/cancel"))
        out.pop("next", None)           # a cancelled job needs no user step any more
        return out

    @tool("continue_job",
          "Confirm a user step of a job that is waiting_for_user (cover / uncover the light "
          "path ...). Call ONLY after the user confirmed they did it. Waits up to 3 s and "
          "returns the new job state.")
    async def continue_job(job_id: str) -> dict:
        await api.post(f"/jobs/{job_id}/continue")
        return _job_with_result(await job_wait(job_id, 3.0))

    @tool("get_spectrum",
          "Spectrum summary: n points, wavelength range (nm), maximum with wavelength, "
          "integral (value*nm), unit, valid_range_nm, plus <= max_points thinned [nm, value] "
          "points (hard limit 512). measurement_id omitted = latest live spectrum. wl_min / "
          "wl_max (nm) restrict the window. save_full=True writes all points to a CSV file "
          "and returns its path. Never returns the full pixel array.", read_only=True)
    async def get_spectrum(measurement_id: str | None = None, max_points: int = 64,
                           wl_min: float | None = None, wl_max: float | None = None,
                           save_full: bool = False) -> dict:
        if measurement_id:
            s = await api.get(f"/measurements/{measurement_id}")
            wl, vals = s.get("wavelengths") or [], s.get("values") or []
            if wl_min is not None or wl_max is not None:
                keep = [i for i, w in enumerate(wl)
                        if (wl_min is None or w >= wl_min) and (wl_max is None or w <= wl_max)]
                wl, vals = [wl[i] for i in keep], [vals[i] for i in keep]
            source = {"measurement_id": measurement_id}
        else:
            s = await api.get("/spectrum/latest", wl_min=wl_min, wl_max=wl_max)
            wl, vals = s.get("wavelengths") or [], s.get("values") or []
            source = {"live": True, "timestamp": s.get("timestamp")}
        pts = [(w, v) for w, v in zip(wl, vals) if _finite(w) and _finite(v)]
        out: dict = {"source": source, "unit": s.get("unit"), "unit_label": s.get("unit_label"),
                     "valid_range_nm": s.get("valid_range_nm"), "n": len(pts)}
        if pts:
            wmax, vmax = max(pts, key=lambda p: p[1])[0], max(p[1] for p in pts)
            out["wavelength_range_nm"] = [pts[0][0], pts[-1][0]]
            out["max"] = {"value": vmax, "wavelength_nm": wmax}
            out["integral"] = _trapz(wl, vals)
        out["points"] = _thin(wl, vals, max_points)
        if save_full:
            if measurement_id:
                out["csv_path"] = (await save_export(measurement_id, "csv"))["path"]
            else:
                lines = [f"# live spectrum {s.get('timestamp')}", f"# unit: {s.get('unit_label')}",
                         "wavelength_nm,value"]
                lines += [f"{w:.4f},{v:.8g}" for w, v in pts]
                path = os.path.join(data_dir(), f"live_{_stamp()}.csv")
                out["csv_path"] = _write(path, ("\n".join(lines) + "\n").encode())["path"]
        return out

    # ---- analysis -----------------------------------------------------------
    @tool("analyze_color",
          "Colour metrics of a stored measurement (measurement_id) or the live spectrum "
          "(omit): CCT (K), Duv, x/y, CRI Ra and R1-R15, R9, lux (needs absolute calibration or "
          "a lux factor), dominant / peak / centroid wavelength (nm), FWHM (nm), purity, SDCM, "
          "S/P ratio.", read_only=True)
    async def analyze_color(measurement_id: str | None = None) -> dict:
        res = await api.post("/analyze/color", {"measurement_id": measurement_id}
                             if measurement_id else {})
        met = {k: v for k, v in (res.get("metrics") or {}).items()
               if not (isinstance(v, list) and len(v) > 20) and k != "tcs_swatches"}
        return {"source": res.get("source"), "unit": res.get("unit"), "metrics": met}

    @tool("find_peaks",
          "Peak detection in a measurement (measurement_id) or the live spectrum (omit). "
          "min_prominence in value units (default 50 counts), max_count peaks (default 10). "
          "Returns wavelength_nm and value per peak.", read_only=True)
    async def find_peaks(measurement_id: str | None = None, min_prominence: float = 50,
                         max_count: int = 10) -> dict:
        body: dict = {"min_prominence": min_prominence, "max_count": max_count}
        if measurement_id:
            body["measurement_id"] = measurement_id
        return await api.post("/analyze/peaks", body)

    @tool("analyze_filter",
          "Filter transmission analysis from two stored measurements: reference_id (no "
          "filter, 100 %) and sample_id (with filter), taken with the same t_int and dark "
          "strategy. Returns metric rows (e.g. mean transmission, cut-on / cut-off nm) and a "
          "<= 50-point transmission preview.", read_only=True)
    async def analyze_filter(reference_id: str, sample_id: str) -> dict:
        res = await api.post("/analyze/filter", {"reference_id": reference_id,
                                                 "sample_id": sample_id})
        prev = res.get("transmission_preview") or []
        if len(prev) > 50:
            idx = sorted({round(i * (len(prev) - 1) / 49) for i in range(50)})
            res["transmission_preview"] = [prev[i] for i in idx]
        return _compact_result(res, 60)

    # ---- calibration --------------------------------------------------------
    @tool("list_profiles",
          "Calibration profiles selectable for the connected device: value (id to pass to "
          "select_profile), label, state, type (relative/absolute), unit, valid_range_nm, "
          "plausible - plus the current selection. include_old=True also lists old profiles.",
          read_only=True)
    async def list_profiles(include_old: bool = False) -> dict:
        p = await api.get("/calibration/profiles", include_old=include_old)
        return {"device": p.get("device"), "serial": p.get("serial"),
                "selection": p.get("selection"), "items": _profile_items(p),
                "other_units": len(p.get("other_units") or [])}

    @tool("select_profile",
          "Activate a calibration profile: a profile id from list_profiles, 'None' or "
          "'Device default'. If the profile does not fit the device state the API asks for "
          "confirmation; this tool then returns the message - repeat with confirm=True only "
          "after the user agreed.", idempotent=True)
    async def select_profile(selection: str, confirm: bool = False) -> dict:
        try:
            res = await api.post("/calibration/select", {"selection": selection,
                                                         "confirm": confirm})
        except ApiError as e:
            if e.code == "needs_confirmation":
                return {"needs_confirmation": True, "message": e.message,
                        "next": "Explain this to the user; if they agree call "
                                "select_profile(selection, confirm=True)."}
            raise
        return res

    @tool("calibration_status",
          "State of the active calibration: label, state, type, unit, valid_range_nm, message.",
          read_only=True)
    async def calibration_status() -> dict:
        return await api.get("/calibration/status")

    @tool("list_references",
          "Reference light sources (calibration lamps) available for start_calibration: id, "
          "name, serial, unit (absolute references have an irradiance unit), valid range, "
          "recommended input optic.", read_only=True)
    async def list_references() -> dict:
        return {"references": await api.get("/calibration/references")}

    @tool("start_calibration",
          "Start a spectral calibration run as a job. kind: relative | absolute (absolute needs "
          "an absolute reference and optics_input CC or sphere); reference_id from "
          "list_references; optics_name: free text name of the optic; optics_input: CC (cosine "
          "corrector) | sphere | bare (bare fibre); integration_ms: ms or 'auto'. The run "
          "pauses for user steps (cover / uncover the light path): tell the user what the "
          "prompt says and call continue_job only after confirmation. Never set "
          "allow_implausible / save_on_failed_checks without explicit user consent. Follow "
          "with job_status; the result reports status (saved / not_saved_implausible / "
          "not_saved_checks_failed), plausibility, checks and the protocol path.")
    async def start_calibration(
            reference_id: str, optics_name: str,
            kind: Literal["relative", "absolute"] = "relative",
            optics_input: Literal["CC", "sphere", "bare"] = "CC",
            integration_ms: float | Literal["auto"] = "auto",
            allow_implausible: bool = False, save_on_failed_checks: bool = False) -> dict:
        body = {"kind": kind, "reference_id": reference_id,
                "optics": {"name": optics_name, "input": optics_input},
                "integration_ms": integration_ms, "allow_implausible": allow_implausible,
                "save_on_failed_checks": save_on_failed_checks}
        res = await api.post("/calibration/runs", body)
        job = await job_wait(res["job"]["id"], 3.0)
        return {"job": _job_view(job),
                "instructions": "Calibration is a multi-step job. Whenever status is "
                                "waiting_for_user, relay the prompt to the user (cover = block "
                                "the light path / cover the lamp, uncover = release it) and call "
                                "continue_job only after they confirm. Poll job_status until "
                                "done, then report status, plausibility and the protocol path."}

    @tool("verify_profile",
          "Verify a saved calibration profile: the reference source is measured with the "
          "profile through the live pipeline (stored dark at the profile's t_int) and compared "
          "with the reference file, normalised 550-650 nm, per 50-nm band and overall (rms, "
          "max in %). The reference lamp must be in place (uncovered). Returns the job; follow "
          "with job_status.")
    async def verify_profile(profile_id: str, reference_id: str,
                             integration_ms: float | None = None) -> dict:
        body = {"profile_id": profile_id, "reference_id": reference_id}
        if integration_ms is not None:
            body["integration_ms"] = integration_ms
        res = await api.post("/calibration/verifications", body)
        job = await job_wait(res["job"]["id"], 5.0)
        return {"job": _job_view(job)}

    @tool("linearity_status",
          "Detector linearity correction of the connected unit: state (active / off / factory "
          "= device linearises itself / none), the active record and the stored records with "
          "date, valid range and max deviation.", read_only=True)
    async def linearity_status() -> dict:
        return await api.get("/calibration/linearity")

    @tool("start_linearity",
          "Start a detector linearity calibration as a job (stable broadband lamp, 6-10 "
          "integration times between ~10 and 90 % full scale, darks before/after, repeated "
          "reference step against lamp drift). Devices with a factory linearisation (Ocean "
          "HDX) only get a check measurement. The job pauses for cover / uncover user steps: "
          "relay the prompt and call continue_job only after the user confirms.")
    async def start_linearity(steps: int | None = None,
                              integration_ms: float | Literal["auto"] = "auto",
                              activate: bool = True) -> dict:
        body = {"integration_ms": integration_ms, "activate": activate}
        if steps is not None:
            body["steps"] = steps
        res = await api.post("/calibration/linearity/runs", body)
        job = await job_wait(res["job"]["id"], 3.0)
        return {"job": _job_view(job),
                "instructions": "Multi-step job: relay waiting_for_user prompts (cover / "
                                "uncover), continue_job after confirmation, poll job_status "
                                "until done, then report status, max deviation and the "
                                "protocol path."}

    # ---- export / reports ---------------------------------------------------
    @tool("export_measurement",
          "Save a stored measurement (full spectrum + metadata) as csv or json in the data "
          "directory (SPECTRO_DATA_DIR); returns the file path.")
    async def export_measurement(measurement_id: str,
                                 format: Literal["csv", "json"] = "csv") -> dict:
        return await save_export(measurement_id, format)

    @tool("create_report",
          "Create a report file in the data directory and return path and size. kind: color "
          "(colour report of measurement_id, or the live spectrum if omitted) | filter "
          "(reference_id + sample_id) | measurement (measurement_id, csv only); format: pdf | "
          "png | csv (pdf/png need matplotlib on the server); language: de | en.")
    async def create_report(kind: Literal["color", "filter", "measurement"] = "color",
                            format: Literal["pdf", "png", "csv"] = "pdf",
                            measurement_id: str | None = None, reference_id: str | None = None,
                            sample_id: str | None = None,
                            language: Literal["de", "en"] = "de") -> dict:
        body = {"kind": kind, "format": format, "language": language,
                "measurement_id": measurement_id, "reference_id": reference_id,
                "sample_id": sample_id}
        data = await api.download("POST", "/reports", json={k: v for k, v in body.items()
                                                            if v is not None}, timeout_s=120)
        tag = _safe(measurement_id or sample_id or "live")
        path = os.path.join(data_dir(), f"{kind}_report_{tag}_{_stamp()}.{format}")
        return {**_write(path, data), "kind": kind, "format": format}

    # ---- resources ----------------------------------------------------------
    @resource("spectrometer://status", "Compact device and job status (same as status()).")
    async def res_status() -> dict:
        return await get_status_view()

    @resource("spectrometer://config",
              "Current acquisition / processing settings (GET /acquisition).")
    async def res_config() -> dict:
        return await api.get("/acquisition")

    @resource("spectrometer://profiles", "Compact list of selectable calibration profiles.")
    async def res_profiles() -> dict:
        p = await api.get("/calibration/profiles")
        return {"selection": p.get("selection"), "items": _profile_items(p)}

    return server


def main() -> None:
    """Entry point: serve MCP over stdio (stdout carries the protocol, logs go to stderr)."""
    try:
        server = build_server()
        server.run("stdio")
    except KeyboardInterrupt:
        pass
    except Exception as e:  # startup problem: clear message on stderr, no traceback
        print(f"ps2600 MCP server failed: {e}", file=sys.stderr)
        sys.exit(1)

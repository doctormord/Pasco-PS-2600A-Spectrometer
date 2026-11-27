"""
engine.py — shared, Qt-free measurement core.

ONE Engine instance owns the spectrometer: device life cycle, fusion /
averaging, the processing pipeline (processing.process_frame with
processing.pipeline_kwargs), the calibration runtime, the heatmap ring buffer,
throttled colour metrics, control ownership and the job system (single
measurement, measurement series, calibration run).

Front-ends never re-implement any of this. They
  * subscribe a listener   engine.subscribe(callback)  → callback(EngineEvent)
    (called from engine threads; the Qt GUI marshals to its thread with a
    signal bridge, the API server with asyncio.run_coroutine_threadsafe),
  * issue commands         engine.connect(...), engine.configure({...}),
    engine.select_profile(...), engine.jobs.submit_measure(...) …
    (thread-safe; each takes an `owner` used for the control check),
  * read snapshots         engine.status(), engine.last_frame, …

Rules (docs/PLAN_api_mcp.md §2): this module and everything it imports must
not import PyQt — enforced by tests/test_qt_free.py.

Event kinds (EngineEvent.kind → data):
  "frame"          FrameEvent
  "auto_exposure"  {"exposure_ms": float}
  "config"         {key: value, …} effective changes
  "connection"     {"connected": bool, "backend", "device_path", "reason"}
  "calibration"    calibration status dict
  "axis"           {"wavelengths": ndarray}
  "control"        control_info() dict
  "job"            Job.to_dict(brief=True)
  "measurement"    {"id": str, "kind": str}
"""
from __future__ import annotations

import collections
import io
import os
import threading
import time
import traceback
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable

import numpy as np

import calibration_capture as cc
import calibration_linearity as callin
import calibration_profiles as calprof
import color_science as cs
import device_manager as dm
import fusion
import processing
from app_config import Config
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
)

LOCAL = "local"                      # control owner of the desktop GUI
HEATMAP_DEFAULT_SIZE = 100           # = spectrometer_core.HEATMAP_HISTORY_SIZE
COLOR_METRICS_EVERY_N_FRAMES = 5     # throttle of the (heavy) CRI math
MAX_MEASUREMENTS = 500               # in-memory measurement store
MAX_JOBS = 200

# Config keys whose change alters the acquisition / processing of the live
# stream. Changing them needs control (HTTP 409 / locked GUI otherwise).
CONTROL_KEYS = frozenset({
    "exposure_ms", "auto_exposure", "frames_to_average", "pause",
    "fast_preview_enabled", "fast_preview_short_pct",
    "dark_correction", "dark_mode", "dark_bias_adc", "dark_rate_adc_per_s",
    "hot_pixel_filter", "smoothing_width", "spatial_smoothing",
    "spatial_smoothing_width", "display_unit", "irradiance_unit",
    "radiometric_calibration_factor", "response_correction_active",
    "heatmap_buffer_size", "heatmap_measure_interval_ms",
    # resets the shared time-lapse buffer → needs control like display_unit
    "display_mode",
    *fusion.DEFAULTS.keys(),
})
DISPLAY_MODE_VALUES = (processing.DISPLAY_AUTO,) + processing.DISPLAY_MODES


class _WithoutProfile:
    """The calibration runtime seen WITHOUT its v2 profile ('Raw ADC' display
    while a profile is active): same stored darks, no applier, no gain."""
    applier = None
    legacy_gain = None
    active_v2 = False

    def __init__(self, rt):
        self._rt = rt

    def dark_for(self, integration_us):
        return self._rt.dark_for(integration_us)


# ══════════════════════════════════════════════════════════════════════════
#  Errors
# ══════════════════════════════════════════════════════════════════════════

class EngineError(RuntimeError):
    """Command failed. `code` maps to the API error code / HTTP status."""
    status = 400
    code = "bad_request"

    def __init__(self, message: str, *, details: Any = None):
        super().__init__(message)
        self.details = details


class NotConnected(EngineError):
    status = 503
    code = "not_connected"


class NotFound(EngineError):
    status = 404
    code = "not_found"


class ControlBusy(EngineError):
    status = 409
    code = "control_busy"


class NeedsConfirmation(EngineError):
    status = 409
    code = "needs_confirmation"


class Unprocessable(EngineError):
    status = 422
    code = "unprocessable"


class JobCancelled(Exception):
    pass


# ══════════════════════════════════════════════════════════════════════════
#  Events
# ══════════════════════════════════════════════════════════════════════════

@dataclass
class EngineEvent:
    kind: str
    data: Any = None


@dataclass
class FrameEvent:
    """One processed display frame (immutable snapshot — arrays are copies)."""
    seq: int
    timestamp: float                     # time.time()
    wavelengths: np.ndarray
    pixels: np.ndarray                   # processed values (NaN = masked)
    raw_averaged: np.ndarray             # averaged raw counts (before processing)
    pre_response: np.ndarray             # dark-corrected, response-uncorrected
    unit: str
    dark_source: str | None
    peaks: list
    ob_mean: float
    integration_us: int
    fps: float
    color: dict | None                   # latest colour metrics (may be stale)
    color_fresh: bool                    # True if computed for THIS frame
    lux_calibration: float | None        # photometric_inputs lux factor
    heatmap_pushed: bool
    heatmap_column: np.ndarray | None    # the pushed column (linear axis)
    heatmap_time: float | None           # monotonic time of the push
    # display representation (processing.display_transform); pixels/unit stay
    # the corrected data every evaluation uses
    display: np.ndarray | None = None
    display_unit: str | None = None
    display_mode: str | None = None
    exposure: dict | None = None         # drive level in ADC (processing.exposure_level)
    quantity: str = "irradiance"         # photometric quantity of `unit` (lx vs. cd/m²)


# ══════════════════════════════════════════════════════════════════════════
#  Jobs
# ══════════════════════════════════════════════════════════════════════════

JOB_QUEUED = "queued"
JOB_RUNNING = "running"
JOB_WAITING = "waiting_for_user"
JOB_DONE = "done"
JOB_FAILED = "failed"
JOB_CANCELLED = "cancelled"
JOB_FINAL = (JOB_DONE, JOB_FAILED, JOB_CANCELLED)


def now_iso() -> str:
    """Local time with UTC offset, ms resolution."""
    return datetime.now().astimezone().isoformat(timespec="milliseconds")


@dataclass
class Job:
    id: str
    kind: str                              # "measure" | "series" | "calibration" | "dark"
    owner: str
    params: dict
    status: str = JOB_QUEUED
    progress: float = 0.0
    step: str = ""
    message: str = ""
    created: str = field(default_factory=now_iso)
    started: str | None = None
    finished: str | None = None
    result: Any = None
    error: dict | None = None
    prompt: dict | None = None             # {"message", "action"} while waiting
    log: list = field(default_factory=list)
    # internal
    _cancel: threading.Event = field(default_factory=threading.Event, repr=False)
    _continue: threading.Event = field(default_factory=threading.Event, repr=False)
    _fn: Callable | None = field(default=None, repr=False)

    def to_dict(self, brief: bool = False) -> dict:
        d = {"id": self.id, "kind": self.kind, "owner": self.owner,
             "status": self.status, "progress": round(float(self.progress), 4),
             "step": self.step, "message": self.message,
             "created": self.created, "started": self.started,
             "finished": self.finished, "prompt": self.prompt,
             "error": self.error}
        if not brief:
            d["params"] = self.params
            d["result"] = self.result
            d["log"] = list(self.log)
        return d


class JobContext:
    """Handed to a job function: progress reporting, cancellation, user steps."""

    def __init__(self, manager: "JobManager", job: Job):
        self._m = manager
        self.job = job

    def check(self) -> None:
        if self.job._cancel.is_set():
            raise JobCancelled()

    def progress(self, frac: float | None = None, message: str | None = None,
                 step: str | None = None) -> None:
        self.check()
        j = self.job
        if frac is not None:
            j.progress = float(max(0.0, min(1.0, frac)))
        if message is not None:
            j.message = message
            j.log.append({"t": now_iso(), "message": message})
        if step is not None:
            j.step = step
        self._m._notify(j)

    def sleep(self, seconds: float) -> None:
        """Interruptible sleep (cancel aborts it)."""
        end = time.monotonic() + max(0.0, float(seconds))
        while True:
            self.check()
            rest = end - time.monotonic()
            if rest <= 0:
                return
            self.job._cancel.wait(min(rest, 0.1))

    def wait_for_user(self, message: str, action: str = "continue") -> None:
        """Block in status waiting_for_user until POST /jobs/{id}/continue
        (or the GUI) continues, or the job is cancelled."""
        j = self.job
        j._continue.clear()
        j.prompt = {"message": message, "action": action}
        j.status = JOB_WAITING
        j.log.append({"t": now_iso(), "message": f"waiting for user: {message}"})
        self._m._notify(j)
        while not j._continue.wait(0.1):
            self.check()
        self.check()
        j.prompt = None
        j.status = JOB_RUNNING
        self._m._notify(j)


class JobManager:
    """Sequential job runner (one device → one job at a time)."""

    def __init__(self, engine: "Engine"):
        self.engine = engine
        self._jobs: "collections.OrderedDict[str, Job]" = collections.OrderedDict()
        self._lock = threading.Lock()
        self._queue: "collections.deque[str]" = collections.deque()
        self._wake = threading.Event()
        self._thread: threading.Thread | None = None
        self._stop = False

    # ── public ────────────────────────────────────────────────────────────
    def submit(self, kind: str, fn: Callable[[JobContext], Any], params: dict,
               owner: str) -> Job:
        self.engine.check_control(owner)
        job = Job(id=f"{kind[:3]}-{uuid.uuid4().hex[:10]}", kind=kind,
                  owner=owner, params=dict(params or {}), _fn=fn)
        with self._lock:
            self._jobs[job.id] = job
            while len(self._jobs) > MAX_JOBS:
                old_id, old = next(iter(self._jobs.items()))
                if old.status not in JOB_FINAL:
                    break
                self._jobs.pop(old_id)
            self._queue.append(job.id)
            self._ensure_thread()
        self._notify(job)
        self._wake.set()
        return job

    def get(self, job_id: str) -> Job:
        with self._lock:
            j = self._jobs.get(job_id)
        if j is None:
            raise NotFound(f"Job '{job_id}' not found.")
        return j

    def list(self) -> list[Job]:
        with self._lock:
            return list(self._jobs.values())

    def active(self) -> list[Job]:
        return [j for j in self.list() if j.status not in JOB_FINAL]

    def running(self) -> Job | None:
        for j in self.list():
            if j.status in (JOB_RUNNING, JOB_WAITING):
                return j
        return None

    def cancel(self, job_id: str) -> Job:
        j = self.get(job_id)
        if j.status in JOB_FINAL:
            return j
        j._cancel.set()
        if j.status == JOB_QUEUED:
            with self._lock:
                try:
                    self._queue.remove(j.id)
                except ValueError:
                    pass
            self._finish(j, JOB_CANCELLED, message="cancelled before start")
        return j

    def cancel_all(self, reason: str = "cancelled") -> None:
        for j in self.active():
            j.message = reason
            self.cancel(j.id)

    def continue_job(self, job_id: str) -> Job:
        j = self.get(job_id)
        if j.status != JOB_WAITING:
            raise EngineError(f"Job '{job_id}' is not waiting for the user "
                              f"(status {j.status}).")
        j._continue.set()
        return j

    def wait(self, job_id: str, timeout: float | None = None,
             until_waiting: bool = True) -> Job:
        """Block until the job is final (or waiting for the user)."""
        j = self.get(job_id)
        end = None if timeout is None else time.monotonic() + timeout
        while j.status not in JOB_FINAL and not (until_waiting and j.status == JOB_WAITING):
            if end is not None and time.monotonic() >= end:
                break
            time.sleep(0.02)
        return j

    def shutdown(self) -> None:
        self.cancel_all("engine shutdown")
        self._stop = True
        self._wake.set()
        if self._thread is not None:
            self._thread.join(timeout=5.0)
            self._thread = None

    # ── internal ──────────────────────────────────────────────────────────
    def _ensure_thread(self) -> None:
        if self._thread is None or not self._thread.is_alive():
            self._stop = False
            self._thread = threading.Thread(target=self._run, daemon=True,
                                            name="engine-jobs")
            self._thread.start()

    def _notify(self, job: Job) -> None:
        self.engine._emit("job", job.to_dict(brief=True))

    def _finish(self, job: Job, status: str, *, result=None, error=None,
                message: str | None = None) -> None:
        job.status = status
        job.finished = now_iso()
        job.prompt = None
        if result is not None:
            job.result = result
        if error is not None:
            job.error = error
        if message:
            job.message = message
        if status == JOB_DONE:
            job.progress = 1.0
        self._notify(job)

    def _run(self) -> None:
        while not self._stop:
            self._wake.wait(0.5)
            self._wake.clear()
            while not self._stop:
                with self._lock:
                    job_id = self._queue.popleft() if self._queue else None
                    job = self._jobs.get(job_id) if job_id else None
                if job is None:
                    break
                if job.status != JOB_QUEUED:
                    continue
                self._execute(job)

    def _execute(self, job: Job) -> None:
        eng = self.engine
        try:
            had_lease = eng.take_job_control(job)
        except ControlBusy as e:
            self._finish(job, JOB_FAILED, error={"code": e.code, "message": str(e)})
            return
        job.status = JOB_RUNNING
        job.started = now_iso()
        self._notify(job)
        ctx = JobContext(self, job)
        try:
            res = job._fn(ctx)
            if job._cancel.is_set():
                raise JobCancelled()
            self._finish(job, JOB_DONE, result=res, message=job.message or "done")
        except JobCancelled:
            self._finish(job, JOB_CANCELLED, message=job.message or "cancelled")
        except EngineError as e:
            self._finish(job, JOB_FAILED,
                         error={"code": e.code, "message": str(e), "details": e.details})
        except Exception as e:                                  # noqa: BLE001
            traceback.print_exc()
            self._finish(job, JOB_FAILED,
                         error={"code": "job_failed", "message": f"{type(e).__name__}: {e}"})
        finally:
            eng.release_job_control(job, had_lease)


# ══════════════════════════════════════════════════════════════════════════
#  Helpers
# ══════════════════════════════════════════════════════════════════════════

def json_safe(obj):
    """numpy → Python, non-finite floats → None (recursive)."""
    if isinstance(obj, dict):
        return {k: json_safe(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [json_safe(v) for v in obj]
    if isinstance(obj, np.ndarray):
        return [json_safe(v) for v in obj.tolist()]
    if isinstance(obj, np.generic):
        obj = obj.item()
    if isinstance(obj, float):
        return obj if np.isfinite(obj) else None
    return obj


class _CfgOverlay:
    """Config.get with per-call overrides (measurement parameters)."""

    def __init__(self, base_get, overrides: dict):
        self._get = base_get
        self._o = overrides

    def __call__(self, key, default=None):
        if key in self._o:
            return self._o[key]
        return self._get(key, default)


def heatmap_size_from_config(cfg) -> int:
    try:
        n = int(cfg.get("heatmap_buffer_size", HEATMAP_DEFAULT_SIZE))
    except (TypeError, ValueError):
        n = HEATMAP_DEFAULT_SIZE
    return max(10, (n // 10) * 10)


# ══════════════════════════════════════════════════════════════════════════
#  Engine
# ══════════════════════════════════════════════════════════════════════════

class Engine:
    """See module docstring. Attribute names of the live state intentionally
    match the former web_server.ServerState (compatibility for scripts/tests).
    """

    def __init__(self, cfg=None, *, init_registry: bool = True):
        self.cfg = cfg if cfg is not None else Config
        if init_registry and not dm.REGISTRY:
            dm.init_registry()
        self._lock = threading.RLock()             # device / config commands
        self._frame_lock = threading.RLock()       # processed live state
        self._listeners: list[Callable[[EngineEvent], None]] = []
        self._listeners_lock = threading.Lock()

        # device
        self.acquisition: dm.BaseSpectrometer | None = None
        self.connected_device_path: str | None = None
        self.connected_backend_name: str | None = None
        self._deliberate_disconnect = False

        # averaging
        self.frames_avg = max(1, int(self.cfg.get("frames_to_average", 1) or 1))
        self.spectrum_history: collections.deque = collections.deque(maxlen=self.frames_avg)

        # active device parameters (PASCO defaults until something connects)
        self.pixel_count = int(DEFAULT_PIXEL_COUNT)
        self.wavelength_array = np.asarray(DEFAULT_WAVELENGTH_ARRAY, float).copy()
        self.response_gain = DEFAULT_SPECTRAL_RESPONSE_GAIN
        self.ob_slope = float(DEFAULT_OB_CORRECTION_SLOPE)
        self.ob_offset = float(DEFAULT_OB_CORRECTION_OFFSET)
        self.adc_saturation = float(DEFAULT_ADC_SATURATION)
        self.supports_ob = True
        self.spec_range_nm: tuple = (None, None)

        # processed state
        self.calib = calprof.CalibrationRuntime()
        self.linearity_info: dict = {"state": callin.STATE_NONE, "label": "n/a"}
        self.last_verification: dict | None = None
        self._overlay_cache: dict = {}
        self.current_pixels = np.zeros(self.pixel_count)
        self.current_raw = np.zeros(self.pixel_count)
        self.current_pre_response = np.zeros(self.pixel_count)
        self.current_peaks: list = []
        self.current_unit = "counts"
        self.current_display = np.zeros(self.pixel_count)
        self.current_display_unit = "counts"
        self.current_display_mode = processing.DISPLAY_RAW
        self.current_dark_source: str | None = None
        self.latest_ob_mean = 0.0
        self.latest_integration_us = int(START_INTEGRATION_TIME_US)
        self.color_metrics: dict | None = None
        self.color_metrics_period_frames = COLOR_METRICS_EVERY_N_FRAMES
        self._frame_counter = 0
        self.frame_seq = 0
        self.last_frame: FrameEvent | None = None
        self._fps_history: collections.deque = collections.deque(maxlen=20)
        self._last_frame_time: float | None = None
        self.fps = 0.0

        # heatmap
        self.heatmap_size = heatmap_size_from_config(self.cfg)
        self._heatmap_measure_ms = max(0, int(self.cfg.get("heatmap_measure_interval_ms", 0) or 0))
        self._heatmap_last_push = 0.0
        self.heatmap_filled = 0
        self._autosave_writer = None
        self._reset_heatmap()

        # fusion (display smoothing) → _fused_emit
        self.fusion = fusion.SpectrumFusion(emit_callback=self._fused_emit)
        self._configure_fusion()

        # control + jobs + measurements
        self._control_owner: str | None = None
        self._control_reason = ""
        self._control_since: str | None = None
        self._control_job: str | None = None
        self._lease_owner: str | None = None       # explicit lease (not a job)
        self.jobs = JobManager(self)
        self.measurements: "collections.OrderedDict[str, dict]" = collections.OrderedDict()
        self._meas_lock = threading.Lock()

    # ── listeners ─────────────────────────────────────────────────────────
    def subscribe(self, callback: Callable[[EngineEvent], None]) -> Callable[[], None]:
        with self._listeners_lock:
            self._listeners.append(callback)

        def unsubscribe():
            with self._listeners_lock:
                if callback in self._listeners:
                    self._listeners.remove(callback)
        return unsubscribe

    def _emit(self, kind: str, data: Any = None) -> None:
        with self._listeners_lock:
            listeners = list(self._listeners)
        ev = EngineEvent(kind, data)
        for cb in listeners:
            try:
                cb(ev)
            except Exception as e:                              # noqa: BLE001
                print(f"[engine] listener error ({kind}): {e!r}")

    # ── properties ────────────────────────────────────────────────────────
    @property
    def is_connected(self) -> bool:
        return self.acquisition is not None and self.connected_device_path is not None

    @property
    def device_name(self) -> str:
        """Connected backend, or the selected one when idle."""
        return self.connected_backend_name or self.cfg.get("selected_backend", "") or ""

    def int_bounds_us(self) -> tuple[int, int]:
        b = self.acquisition
        return (int(getattr(b, "min_integration_us", MIN_INTEGRATION_TIME_US)),
                int(getattr(b, "max_integration_us", MAX_INTEGRATION_TIME_US)))

    # ══════════════════════════════════════════════════════════════════════
    #  Control ownership
    # ══════════════════════════════════════════════════════════════════════
    def control_info(self) -> dict:
        return {"owner": self._control_owner, "reason": self._control_reason,
                "since": self._control_since, "job_id": self._control_job,
                "lease": self._lease_owner}

    def check_control(self, owner: str | None) -> None:
        """Raise ControlBusy if someone else holds control. owner None =
        engine-internal (always allowed)."""
        cur = self._control_owner
        if owner is None or cur is None or cur == owner:
            return
        raise ControlBusy(
            f"Control is held by '{cur}'"
            + (f" ({self._control_reason})" if self._control_reason else "")
            + ".", details=self.control_info())

    def acquire_control(self, owner: str, reason: str = "", *, force: bool = False) -> dict:
        """Explicit lease (e.g. remote client, or the desktop while the
        calibration dialog is open)."""
        with self._lock:
            if not force or (self._control_owner == LOCAL and owner != LOCAL):
                # force never breaks the desktop's own lease (calibration
                # wizard drives the device directly meanwhile)
                self.check_control(owner)
            if force and self._control_job and self._control_owner != owner:
                self.jobs.cancel(self._control_job)
            self._lease_owner = owner
            self._set_control(owner, reason or "lease", None)
        return self.control_info()

    def release_control(self, owner: str | None, *, force: bool = False) -> dict:
        with self._lock:
            if (force and self._lease_owner == LOCAL
                    and owner is not None and owner != LOCAL):
                raise ControlBusy("The desktop holds control (calibration dialog); "
                                  "it cannot be revoked remotely.",
                                  details=self.control_info())
            if force or owner == self._lease_owner:
                self._lease_owner = None
                if self._control_job is None:
                    self._set_control(None, "", None)
        return self.control_info()

    def take_job_control(self, job: Job) -> bool:
        with self._lock:
            self.check_control(job.owner)
            had_lease = self._lease_owner == job.owner
            self._set_control(job.owner, f"{job.kind} job {job.id}", job.id)
            return had_lease

    def release_job_control(self, job: Job, had_lease: bool) -> None:
        with self._lock:
            if self._control_job != job.id:
                return
            if had_lease and self._lease_owner == job.owner:
                self._set_control(job.owner, "lease", None)
            else:
                self._set_control(None, "", None)

    def _set_control(self, owner, reason, job_id) -> None:
        changed = (owner, reason, job_id) != (self._control_owner,
                                              self._control_reason, self._control_job)
        self._control_owner = owner
        self._control_reason = reason
        self._control_job = job_id
        self._control_since = now_iso() if owner else None
        if changed:
            self._emit("control", self.control_info())

    # ══════════════════════════════════════════════════════════════════════
    #  Device life cycle
    # ══════════════════════════════════════════════════════════════════════
    def list_backends(self) -> list[str]:
        return dm.list_backends()

    def selected_backend(self) -> str:
        names = dm.list_backends()
        sel = self.cfg.get("selected_backend", None)
        return sel if sel in names else (names[0] if names else "")

    def scan(self, backend_name: str | None = None) -> list[str]:
        backend_name = backend_name or self.selected_backend()
        cls = dm.get_backend_class(backend_name)
        devices: list[str] = []
        if cls is not None:
            try:
                devices = list(cls.scan())
            except Exception:                                    # noqa: BLE001
                devices = []
        if (self.connected_device_path and backend_name == self.connected_backend_name
                and self.connected_device_path not in devices):
            devices.insert(0, self.connected_device_path)
        return devices

    def connect(self, backend_name: str | None = None, device_path: str | None = None,
                *, owner: str | None = LOCAL) -> dict:
        with self._lock:
            self.check_control(owner)
            backend_name = backend_name or self.selected_backend()
            cls = dm.get_backend_class(backend_name)
            if cls is None:
                raise NotFound(f"Backend '{backend_name}' not available on this system.")
            if (self.is_connected and backend_name == self.connected_backend_name
                    and (device_path is None or device_path == self.connected_device_path)):
                return self.connection_info()
            if self.is_connected:
                self.disconnect(owner=owner)
                time.sleep(0.3)                   # let the USB handle settle
            if not device_path:
                try:
                    devices = list(cls.scan())
                except Exception:                                # noqa: BLE001
                    devices = []
                if not devices:
                    raise NotFound(f"No {backend_name} found.")
                device_path = devices[0]

            backend = cls(on_frame=self._on_driver_frame,
                          on_auto_exp=self._on_auto_exposure,
                          on_connection_lost=self._on_connection_lost)
            # device-specific settings BEFORE connect() (calibration file load)
            if hasattr(backend, "wl_offset_nm"):
                backend.wl_offset_nm = float(self.cfg.get("lr2t_wl_offset_nm", 0.0))
            if hasattr(backend, "flip_pixels"):
                backend.flip_pixels = bool(self.cfg.get("lr2t_flip_pixels", True))
            backend.connect(device_path)

            ms = float(self.cfg.get("exposure_ms", START_INTEGRATION_TIME_US / 1000.0))
            us = max(int(backend.min_integration_us),
                     min(int(backend.max_integration_us), int(ms * 1000)))
            backend.current_integration_time_us = us
            backend.set_integration_time_us(us)
            backend.is_auto_exposure_active = bool(self.cfg.get("auto_exposure", False))
            backend.fast_preview_enabled = bool(self.cfg.get("fast_preview_enabled", False))
            backend.fast_preview_short_pct = max(
                1, min(50, int(self.cfg.get("fast_preview_short_pct", 10))))

            # Axis / response / OB params and fusion armed BEFORE start() (a
            # fast device can deliver its first frame instantly).
            self._apply_device_params(backend, backend_name)
            self._configure_fusion()
            self.fusion.reset()
            self.fusion.start()
            self.acquisition = backend
            self.connected_device_path = device_path
            self.connected_backend_name = backend_name
            self._deliberate_disconnect = False
            backend.start()
            # re-push: some drivers send their start value to the hardware
            backend.set_integration_time_us(us)
            self.cfg.set("selected_backend", backend_name)
            info = self.connection_info()
        self._emit("connection", dict(info, reason="connect"))
        self._emit("calibration", self.calibration_status())
        return info

    def disconnect(self, *, owner: str | None = LOCAL, reason: str = "disconnect") -> dict:
        with self._lock:
            self.check_control(owner)
            b = self.acquisition
            if b is not None:
                self._deliberate_disconnect = True
                try:
                    b.disconnect()
                except RuntimeError as e:
                    if "cannot join current thread" not in str(e):
                        print(f"[engine] disconnect error (ignored): {e!r}")
                except Exception as e:                           # noqa: BLE001
                    print(f"[engine] disconnect cleanup error (ignored): {e!r}")
                self.acquisition = None
            self.fusion.stop()
            self.fusion.reset()
            self.connected_device_path = None
            self.connected_backend_name = None
            self.spectrum_history.clear()
            self._reset_device_params()
            info = self.connection_info()
        self._emit("connection", dict(info, reason=reason))
        self._emit("calibration", self.calibration_status())
        return info

    def connection_info(self) -> dict:
        b = self.acquisition
        return {"connected": self.is_connected,
                "backend": self.connected_backend_name,
                "device_path": self.connected_device_path,
                "serial": getattr(b, "serial", None) if b is not None else None}

    def close(self) -> None:
        """Shut down jobs and autosave, release the device."""
        self.jobs.shutdown()
        self.stop_autosave()
        try:
            self.disconnect(owner=None, reason="shutdown")
        except Exception as e:                                   # noqa: BLE001
            print(f"[engine] close: {e!r}")

    def _apply_device_params(self, backend, backend_name: str) -> None:
        with self._frame_lock:
            wl = np.asarray(backend.wavelength_array, dtype=float)
            self.wavelength_array = wl
            self.pixel_count = int(wl.size)
            self.supports_ob = bool(getattr(backend, "SUPPORTS_OB", False))
            try:
                self.spec_range_nm = tuple(getattr(backend, "spec_range_nm", (None, None)))
            except Exception:                                    # noqa: BLE001
                self.spec_range_nm = (None, None)
            self.adc_saturation = float(getattr(backend, "ADC_SATURATION",
                                                DEFAULT_ADC_SATURATION))
            self.connected_backend_name = backend_name
            if self.supports_ob:
                self.ob_slope = float(DEFAULT_OB_CORRECTION_SLOPE)
                self.ob_offset = float(DEFAULT_OB_CORRECTION_OFFSET)
            else:
                self.ob_slope, self.ob_offset = 1.0, 0.0
            self.current_pixels = np.zeros(self.pixel_count)
            self.current_raw = np.zeros(self.pixel_count)
            self.current_pre_response = np.zeros(self.pixel_count)
            self.current_unit = "counts"
            self.last_frame = None
            self.spectrum_history.clear()
            self._reset_heatmap()
            self._install_linearity(backend, backend_name)
            self._rebuild_calibration(backend)

    def _reset_device_params(self) -> None:
        with self._frame_lock:
            self.pixel_count = int(DEFAULT_PIXEL_COUNT)
            self.wavelength_array = np.asarray(DEFAULT_WAVELENGTH_ARRAY, float).copy()
            self.response_gain = DEFAULT_SPECTRAL_RESPONSE_GAIN
            self.ob_slope = float(DEFAULT_OB_CORRECTION_SLOPE)
            self.ob_offset = float(DEFAULT_OB_CORRECTION_OFFSET)
            self.adc_saturation = float(DEFAULT_ADC_SATURATION)
            self.supports_ob = True
            self.spec_range_nm = (None, None)
            self.calib = calprof.CalibrationRuntime()
            self.linearity_info = {"state": callin.STATE_NONE, "label": "n/a"}
            self.current_unit = "counts"
            self.current_pixels = np.zeros(self.pixel_count)
            self.current_raw = np.zeros(self.pixel_count)
            self.current_pre_response = np.zeros(self.pixel_count)
            self.current_peaks = []
            self.color_metrics = None
            self.last_frame = None
            self.fps = 0.0
            self._fps_history.clear()
            self._last_frame_time = None
            self._reset_heatmap()

    # ── driver callbacks (acquisition thread) ─────────────────────────────
    def _on_driver_frame(self, pixels, dark=0.0, integration_us=0,
                         kind=fusion.FRAME_STANDARD, ratio=1.0) -> None:
        self.fusion.submit(pixels, dark, integration_us, kind, ratio)

    def _on_auto_exposure(self, new_ms: float) -> None:
        lo_us, hi_us = self.int_bounds_us()
        new_ms = max(lo_us / 1000.0, min(hi_us / 1000.0, float(new_ms)))
        self.cfg.set("exposure_ms", new_ms)
        self.spectrum_history.clear()
        self.fusion.reset()
        self._emit("auto_exposure", {"exposure_ms": new_ms})
        self._emit("config", {"exposure_ms": new_ms})

    def _on_connection_lost(self) -> None:
        """Acquisition thread reports a dead device. Never join the current
        thread: clean up from a helper thread."""
        if self._deliberate_disconnect:
            return
        print("[engine] Hardware reports connection lost.")

        def cleanup():
            self.jobs.cancel_all("connection lost")
            try:
                self.disconnect(owner=None, reason="lost")
            except Exception as e:                               # noqa: BLE001
                print(f"[engine] cleanup after connection loss: {e!r}")
        threading.Thread(target=cleanup, daemon=True, name="engine-lost").start()

    # ══════════════════════════════════════════════════════════════════════
    #  Configuration
    # ══════════════════════════════════════════════════════════════════════
    def _configure_fusion(self) -> None:
        self.fusion.configure(**{k: self.cfg.get(k, fusion.DEFAULTS[k])
                                 for k in fusion.DEFAULTS
                                 if self.cfg.get(k, None) is not None})

    def configure(self, partial: dict, *, owner: str | None = LOCAL,
                  persist: bool = True) -> dict:
        """Set config values and apply their live side effects. Acquisition /
        processing keys need control. Returns the effective changes (also
        broadcast as a "config" event)."""
        partial = dict(partial or {})
        if not partial:
            return {}
        with self._lock:
            if CONTROL_KEYS.intersection(partial):
                self.check_control(owner)
            eff = dict(partial)
            b = self.acquisition
            pause = eff.pop("pause", None)
            # manual exposure turns auto exposure off (desktop behaviour)
            if ("exposure_ms" in eff and "auto_exposure" not in eff
                    and b is not None and getattr(b, "is_auto_exposure_active", False)):
                eff["auto_exposure"] = False
            if "frames_to_average" in eff:
                eff["frames_to_average"] = max(1, int(eff["frames_to_average"]))
            if "display_mode" in eff:
                dm = str(eff["display_mode"] or processing.DISPLAY_AUTO).strip().lower()
                if dm not in DISPLAY_MODE_VALUES:
                    raise EngineError(f"Unknown display mode '{dm}' "
                                      f"(allowed: {', '.join(DISPLAY_MODE_VALUES)}).")
                eff["display_mode"] = dm
            if "fast_preview_short_pct" in eff:
                eff["fast_preview_short_pct"] = max(1, min(50, int(eff["fast_preview_short_pct"])))
            if "heatmap_buffer_size" in eff:
                eff["heatmap_buffer_size"] = heatmap_size_from_config(
                    {"heatmap_buffer_size": eff["heatmap_buffer_size"]})
            if "exposure_ms" in eff:
                lo_us, hi_us = self.int_bounds_us()
                us = max(lo_us, min(hi_us, int(float(eff["exposure_ms"]) * 1000)))
                eff["exposure_ms"] = us / 1000.0
            self.cfg.update(eff, save=persist)

            if "exposure_ms" in eff and b is not None:
                us = int(round(eff["exposure_ms"] * 1000))
                b.current_integration_time_us = us
                b.set_integration_time_us(us)
                self.spectrum_history.clear()
                self.fusion.reset()
            if "auto_exposure" in eff and b is not None:
                b.is_auto_exposure_active = bool(eff["auto_exposure"])
                self.spectrum_history.clear()
                self.fusion.reset()
            if "frames_to_average" in eff:
                n = eff["frames_to_average"]
                self.frames_avg = n
                self.spectrum_history = collections.deque(self.spectrum_history, maxlen=n)
            if pause is not None:
                if b is not None:
                    b.is_measurement_paused = bool(pause)
                eff["pause"] = bool(pause)
            if "fast_preview_enabled" in eff and b is not None:
                b.fast_preview_enabled = bool(eff["fast_preview_enabled"])
                b.fast_preview_short_pct = max(1, min(50, int(self.cfg.get("fast_preview_short_pct", 10))))
                if not eff["fast_preview_enabled"]:
                    us = int(float(self.cfg.get("exposure_ms", 20.0)) * 1000)
                    lo_us, hi_us = self.int_bounds_us()
                    b.set_integration_time_us(max(lo_us, min(hi_us, us)))
                self.fusion.reset()
            if "fast_preview_short_pct" in eff and b is not None:
                b.fast_preview_short_pct = eff["fast_preview_short_pct"]
                self.fusion.reset()
            fkeys = {k: eff[k] for k in eff if k in fusion.DEFAULTS}
            if fkeys:
                self.fusion.configure(**fkeys)
                if "fusion_enabled" in fkeys:
                    self.fusion.reset()
            if "irradiance_unit" in eff:
                with self._frame_lock:
                    self.calib.refresh()
            if "heatmap_buffer_size" in eff:
                self.set_heatmap_size(eff["heatmap_buffer_size"], persist=False)
            if "display_mode" in eff:
                # the time-lapse holds display values: never mix units/modes
                with self._frame_lock:
                    self._reset_heatmap()
            if "heatmap_measure_interval_ms" in eff:
                self._heatmap_measure_ms = max(0, int(eff["heatmap_measure_interval_ms"] or 0))
        self._emit("config", json_safe(eff))
        if "irradiance_unit" in eff:
            self._emit("calibration", self.calibration_status())
        return eff

    def set_paused(self, paused: bool, *, owner: str | None = LOCAL) -> None:
        self.configure({"pause": bool(paused)}, owner=owner, persist=False)

    @property
    def paused(self) -> bool:
        return bool(getattr(self.acquisition, "is_measurement_paused", False))

    def reset_averaging(self) -> None:
        self.spectrum_history.clear()

    # ══════════════════════════════════════════════════════════════════════
    #  Calibration state
    # ══════════════════════════════════════════════════════════════════════
    def _rebuild_calibration(self, backend=None) -> None:
        dev = self.device_name
        src = backend if backend is not None else self.acquisition
        with self._frame_lock:
            default_table = getattr(src, "RESPONSE_TABLE", None)
            selection = calprof.active_selection(dev) if dev else calprof.SEL_NONE
            self.response_gain, _eff = calprof.build_gain_for_selection(
                self.wavelength_array, dev, selection,
                device_default_table=default_table)
            if src is not None:
                self.calib.configure(dev, src, self.wavelength_array)
            else:
                self.calib.backend_name = dev
                self.calib.wl = np.asarray(self.wavelength_array, float)
                self.calib.refresh()

    def _install_linearity(self, backend, backend_name: str) -> None:
        """Install the unit's linearity correction on the backend (applied to
        every frame directly after the driver, see calibration_linearity)."""
        if backend is None:
            self.linearity_info = {"state": callin.STATE_NONE, "label": "n/a"}
            return
        try:
            fn, info = callin.runtime_for(backend, backend_name)
        except Exception as e:                                   # noqa: BLE001
            print(f"[engine] linearity correction not loaded: {e!r}")
            fn, info = None, {"state": callin.STATE_NONE, "label": "off",
                              "error": str(e)}
        try:
            backend.set_linearizer(fn)
        except AttributeError:
            pass
        backend.linearity_info = info
        self.linearity_info = info

    def reload_linearity(self) -> dict:
        """Re-read the active linearity correction (after a linearity run or a
        selection change) and re-resolve the calibration (linearity hint)."""
        with self._lock:
            b = self.acquisition
            self._install_linearity(b, self.device_name)
            self.spectrum_history.clear()
            self.fusion.reset()
            self._rebuild_calibration()
        st = self.calibration_status()
        self._emit("calibration", st)
        return dict(self.linearity_info)

    def select_linearity(self, entry_id: str | None, *, owner: str | None = LOCAL) -> dict:
        """Activate a stored linearity correction of this unit (None = off)."""
        self.check_control(owner)
        b = self._require_backend()
        try:
            callin.set_active(self.device_name, getattr(b, "serial", None), entry_id)
        except KeyError:
            raise NotFound(f"Linearity record '{entry_id}' not found.")
        except ValueError as e:
            raise Unprocessable(str(e))
        return self.reload_linearity()

    def rebuild_calibration(self) -> dict:
        """Re-resolve the active selection (after a profile was saved, deleted,
        imported or selected) and apply it live."""
        with self._lock:
            self._rebuild_calibration()
        st = self.calibration_status()
        self._emit("calibration", st)
        return st

    def display_modes(self) -> list[dict]:
        """Selectable display modes now ({value, label, available})."""
        ap = self.calib.applier
        avail = set(processing.available_display_modes(None if ap is None else ap.type))
        out = [{"value": processing.DISPLAY_AUTO, "label": "Auto", "available": True}]
        out += [{"value": m, "label": processing.DISPLAY_LABELS[m], "available": m in avail}
                for m in processing.DISPLAY_MODES]
        return out

    def calibration_status(self) -> dict:
        st = json_safe(dict(self.calib.status))
        st["display_unit"] = self.current_unit
        st.setdefault("quantity", processing.unit_quantity(self.current_unit)
                      if processing.is_absolute_unit(self.current_unit) else "irradiance")
        st["photometric_quantity"] = processing.photometric_quantity(self.current_unit)
        st["linearity"] = json_safe(dict(self.linearity_info or {}))
        return st

    def calibration_context(self) -> tuple[str, str | None, str | None]:
        """(device, serial, wavelength fingerprint)."""
        return (self.device_name, getattr(self.acquisition, "serial", None),
                self.calib.fingerprint)

    def nl_active(self):
        return calprof.device_nl_active(self.acquisition)

    def select_profile(self, selection: str, *, confirm: bool = False,
                       owner: str | None = LOCAL) -> dict:
        """Set the active response-correction selection (None / Device default
        / profile id). A profile whose wavelength fingerprint or linearisation
        changed needs confirm=True (NeedsConfirmation otherwise)."""
        self.check_control(owner)
        dev, serial, fp = self.calibration_context()
        nl = self.nl_active()
        if not dev:
            raise EngineError("No device selected.")
        selection = str(selection)
        has_default = getattr(self.acquisition, "RESPONSE_TABLE", None) is not None
        valid = calprof.selection_items(dev, has_default, serial, fp, include_old=True,
                                        nl_active=nl)
        if selection not in valid:
            raise EngineError(f"Unknown selection '{selection}'.")
        p = calprof.get_profile_by_id(selection)
        if p is not None:
            st = calprof.profile_status(p, dev, serial, fp, nl)
            if (st["state"] in calprof.CONFIRM_STATES
                    and not calprof.fingerprint_override_ok(selection, fp, nl)):
                if not confirm:
                    raise NeedsConfirmation(st["message"], details={
                        "needs_confirmation": True, "state": st["state"],
                        "message": st["message"]})
                if fp or nl is not None:
                    calprof.confirm_fingerprint_override(selection, fp, nl)
        calprof.set_active_selection(dev, selection)
        st = self.rebuild_calibration()
        return {"ok": True, "device": dev, "selection": selection, "status": st}

    def update_axis(self, wl) -> bool:
        """Adopt a new per-pixel wavelength axis (live wavelength calibration).
        Returns True if it changed."""
        wl = np.asarray(wl, dtype=float)
        with self._frame_lock:
            if wl.size != self.pixel_count or np.array_equal(wl, self.wavelength_array):
                return False
            self.wavelength_array = wl
            self.heatmap_linear_waves = np.linspace(wl[0], wl[-1], self.pixel_count)
            self.calib.update_axis(wl, self.acquisition)
        self._emit("axis", {"wavelengths": wl.copy()})
        self._emit("calibration", self.calibration_status())
        return True

    def capture_dark(self, n_frames: int | None = None, *, integration_us: int | None = None,
                     owner: str | None = LOCAL) -> cc.Capture:
        """Blocking per-pixel dark capture at the current (or given) t_int,
        stored for this device. Light must be blocked unless the backend has a
        shutter. Run from a worker thread in a GUI."""
        self.check_control(owner)
        b = self.acquisition
        if b is None:
            raise NotConnected("No device connected.")
        n = max(2, int(n_frames or self.cfg.get("calibration_capture_frames", 16)))
        d = cc.capture_dark(b, n, integration_us=integration_us,
                            backend_name=self.device_name)
        cc.save_dark(d)
        self.calib.invalidate_darks()
        self._emit("calibration", self.calibration_status())
        return d

    def auto_expose(self, target_frac: tuple[float, float] = (0.70, 0.85), *,
                    apply: bool = True, owner: str | None = LOCAL) -> dict:
        """One-shot auto exposure (blocking): iterate short captures until the
        raw peak sits in the target band. apply=True keeps the result as the
        live exposure (continuous auto exposure off)."""
        self.check_control(owner)
        b = self._require_backend()
        prev = int(b.current_integration_time_us)
        import calibration_workflow as cw
        # the target band stays below the saturation warning threshold
        lo, hi = cw.WorkflowConfig(
            target_frac=(float(target_frac[0]), float(target_frac[1])),
            saturation_warn_frac=cw._sat_warn_frac()).target_frac
        t, cap, conv = cc.auto_expose(b, target_frac=(lo, hi),
                                      backend_name=self.device_name)
        if apply:
            self.configure({"exposure_ms": t / 1000.0, "auto_exposure": False}, owner=owner)
        elif int(b.current_integration_time_us) != prev:
            b.set_integration_time_us(prev)
        return {"integration_us": int(t), "exposure_ms": t / 1000.0, "converged": bool(conv),
                "peak_frac": None if cap is None else float(cap.peak_frac)}

    # ══════════════════════════════════════════════════════════════════════
    #  Live pipeline (fusion emit thread / acquisition thread)
    # ══════════════════════════════════════════════════════════════════════
    def _reset_heatmap(self) -> None:
        wl = self.wavelength_array
        n = int(wl.size)
        self.heatmap_linear_waves = np.linspace(wl[0], wl[-1], n)
        self.heatmap_buffer = np.zeros((n, self.heatmap_size))
        # False = masked pixel (outside the valid segments of the profile at
        # push time); exported as an empty cell. The image itself shows 0.
        self.heatmap_valid = np.ones((n, self.heatmap_size), bool)
        self.heatmap_times = np.full(self.heatmap_size, np.nan)
        self.heatmap_unit = None
        self.heatmap_mode = None
        self.heatmap_filled = 0

    def set_heatmap_size(self, n: int, *, persist: bool = True) -> int:
        n = heatmap_size_from_config({"heatmap_buffer_size": n})
        with self._frame_lock:
            self.heatmap_size = n
            self._reset_heatmap()
        if persist:
            self.cfg.set("heatmap_buffer_size", n)
        return n

    def clear_heatmap(self) -> None:
        with self._frame_lock:
            self._reset_heatmap()
            self._heatmap_last_push = 0.0

    # ── time-lapse autosave (crash-safe CSV of every logged heatmap column) ──
    def start_autosave(self, meta: dict | None = None) -> dict:
        """Start writing every pushed heatmap column to a CSV in the export
        folder (export_paths). Returns {"path", "warning"}. Raises OSError."""
        import export_paths
        from autosave import HeatmapAutosaveWriter
        self.stop_autosave()
        if meta is None:
            meta = export_paths.meta_from_report(self.report_meta(), self.wavelength_array)
        path, path_err = export_paths.export_path("timelapse_autosave", "csv", meta)
        writer = HeatmapAutosaveWriter(
            path, self.heatmap_linear_waves,
            fsync_interval_s=self.cfg.get("heatmap_autosave_fsync_s", 20))
        self._autosave_writer = writer
        self.cfg.set("heatmap_autosave_enabled", True)
        info = {"active": True, "path": path, "warning": path_err}
        self._emit("autosave", info)
        return info

    def stop_autosave(self) -> dict:
        w = getattr(self, "_autosave_writer", None)
        self._autosave_writer = None
        if w is not None:
            try:
                w.close()
            except Exception as e:                               # noqa: BLE001
                print(f"[engine] autosave close: {e!r}")
            self.cfg.set("heatmap_autosave_enabled", False)
            self._emit("autosave", {"active": False, "path": w.path})
        return {"active": False, "path": None if w is None else w.path}

    def autosave_info(self) -> dict:
        w = getattr(self, "_autosave_writer", None)
        return {"active": w is not None, "path": None if w is None else w.path}

    def _process(self, averaged, ob_mean, integration_us, wl, cfg_get=None,
                 runtime=None) -> dict:
        kwargs = processing.pipeline_kwargs(
            cfg_get or self.cfg.get, self.calib if runtime is None else runtime,
            integration_us=integration_us,
            ob_slope=self.ob_slope, ob_offset=self.ob_offset,
            default_bias=DEFAULT_DARK_BIAS_ADC,
            default_rate=DEFAULT_DARK_RATE_ADC_PER_SEC)
        return processing.process_frame(averaged, ob_mean, integration_us,
                                        wavelengths=wl, **kwargs), kwargs

    def lux_calibration(self, unit: str) -> float | None:
        """Photometric factor: 1.0 for an absolute profile (E(λ) in W/(m²·nm)
        → lux, or L(λ) in W/(m²·sr·nm) → cd/m²), else the configured
        radiometric factor (irradiance, lux)."""
        if processing.is_absolute_unit(unit):
            return 1.0
        try:
            return float(self.cfg.get("radiometric_calibration_factor", 0.0) or 0.0)
        except (TypeError, ValueError):
            return 0.0

    def color_metrics_for(self, wl, values, unit: str) -> dict:
        """measure_all() with the shared photometric inputs (desktop CIE tab
        and web identical)."""
        spd, lux_cal = processing.photometric_inputs(values, unit)
        if lux_cal is None:
            lux_cal = self.lux_calibration(unit)
        return cs.measure_all(np.asarray(wl, float), spd, lux_calibration=lux_cal,
                              quantity=processing.photometric_quantity(unit))

    def _fused_emit(self, pixels, ob_mean: float, integration_us: int) -> None:
        """Fusion output sink → process → notify listeners."""
        self.ingest_frame(pixels, ob_mean, integration_us, notify=True)

    def ingest_frame(self, pixels, ob_mean: float, integration_us: int, *,
                     notify: bool = True) -> FrameEvent:
        """Run one (fused) frame through averaging + pipeline + heatmap +
        colour metrics. Returns the FrameEvent (also broadcast if notify)."""
        px = np.asarray(pixels, dtype=float)
        with self._frame_lock:
            now = time.perf_counter()
            if self._last_frame_time is not None:
                dt = now - self._last_frame_time
                if dt > 0:
                    self._fps_history.append(1.0 / dt)
                    self.fps = float(np.mean(self._fps_history))
            self._last_frame_time = now

            if px.size != self.wavelength_array.size:
                # pixel-count change (device switch race) → resync the axis
                b = self.acquisition
                wl = (np.asarray(b.wavelength_array, float) if b is not None
                      else np.linspace(380, 1050, px.size))
                if wl.size != px.size:
                    wl = np.linspace(float(wl[0]), float(wl[-1]), px.size)
                self.wavelength_array = wl
                self.pixel_count = int(wl.size)
                self._reset_heatmap()
                self.spectrum_history.clear()
                self._rebuild_calibration()
            if self.spectrum_history.maxlen != self.frames_avg:
                self.spectrum_history = collections.deque(self.spectrum_history,
                                                          maxlen=self.frames_avg)
            self.spectrum_history.append(px)
            if any(len(f) != px.size for f in self.spectrum_history):
                self.spectrum_history.clear()
                self.spectrum_history.append(px)
            averaged = np.mean(self.spectrum_history, axis=0)
            self.latest_ob_mean = float(ob_mean)
            self.latest_integration_us = int(integration_us)
            wl = self.wavelength_array

            result, _kw = self._process(averaged, ob_mean, integration_us, wl)
            self.current_raw = averaged
            self.current_pixels = result["pixels"]
            self.current_pre_response = result["pre_response"]
            self.current_peaks = result["peaks"]
            self.current_unit = result["unit"]
            self.current_dark_source = result["dark_source"]
            # display representation (only what is plotted; evaluations use
            # current_pixels = corrected data)
            applier = self.calib.applier
            mode = processing.effective_display_mode(
                self.cfg.get("display_mode", processing.DISPLAY_AUTO),
                None if applier is None else applier.type,
                str(self.cfg.get("display_unit", "counts")))
            raw_counts = None
            if mode == processing.DISPLAY_RAW and applier is not None:
                rr, _ = self._process(averaged, ob_mean, integration_us, wl,
                                      runtime=_WithoutProfile(self.calib))
                raw_counts = rr["pixels"]
            dvals, dunit = processing.display_transform(
                mode, values=result["pixels"], unit=result["unit"],
                integration_us=integration_us, calibration=applier, raw_counts=raw_counts)
            self.current_display = dvals
            self.current_display_unit = dunit
            self.current_display_mode = mode
            b = self.acquisition
            exposure = processing.exposure_level(
                averaged, full_scale=float(getattr(b, "adc_full_scale", 0.0) or 0.0)
                if b is not None else 0.0,
                saturation_level=float(getattr(b, "adc_saturation_level", self.adc_saturation))
                if b is not None else self.adc_saturation)
            disp = np.nan_to_num(dvals, nan=0.0)

            # heatmap (optional measurement-interval decimation)
            pushed, col, ptime = False, None, None
            mono = time.monotonic()
            interval = self._heatmap_measure_ms / 1000.0
            if interval <= 0.0 or (mono - self._heatmap_last_push) >= interval:
                self._heatmap_last_push = mono
                if self.heatmap_buffer.shape[0] != wl.size or (
                        self.heatmap_filled and (self.heatmap_unit, self.heatmap_mode)
                        != (dunit, mode)):
                    # never mix units / display modes in the time-lapse (also
                    # an automatic raw <-> corrected switch by a profile change)
                    self._reset_heatmap()
                self.heatmap_unit, self.heatmap_mode = dunit, mode
                col = np.interp(self.heatmap_linear_waves, wl, disp)
                vcol = np.interp(self.heatmap_linear_waves, wl,
                                 np.isfinite(dvals).astype(float)) > 0.5
                self.heatmap_buffer = np.roll(self.heatmap_buffer, 1, axis=1)
                self.heatmap_buffer[:, 0] = col
                if self.heatmap_valid.shape != self.heatmap_buffer.shape:
                    self.heatmap_valid = np.ones(self.heatmap_buffer.shape, bool)
                self.heatmap_valid = np.roll(self.heatmap_valid, 1, axis=1)
                self.heatmap_valid[:, 0] = vcol
                if self.heatmap_times.size != self.heatmap_buffer.shape[1]:
                    self.heatmap_times = np.full(self.heatmap_buffer.shape[1], np.nan)
                self.heatmap_times = np.roll(self.heatmap_times, 1)
                self.heatmap_times[0] = mono
                self.heatmap_filled = min(self.heatmap_filled + 1,
                                          self.heatmap_buffer.shape[1])
                pushed, ptime = True, mono
                aw = self._autosave_writer
                if aw is not None:
                    try:
                        aw.write_frame(datetime.now().isoformat(timespec="milliseconds"),
                                       np.where(vcol, col, np.nan))
                    except Exception as e:                       # noqa: BLE001
                        print(f"[engine] autosave write failed: {e!r}")

            # colour metrics (throttled)
            self._frame_counter += 1
            fresh = False
            if self._frame_counter % max(1, int(self.color_metrics_period_frames)) == 0:
                try:
                    self.color_metrics = self.color_metrics_for(wl, self.current_pixels,
                                                                self.current_unit)
                    fresh = True
                except Exception:                                # noqa: BLE001
                    self.color_metrics = None
            self.frame_seq += 1
            ev = FrameEvent(
                seq=self.frame_seq, timestamp=time.time(),
                wavelengths=wl.copy(), pixels=np.array(self.current_pixels, float),
                raw_averaged=np.array(averaged, float),
                pre_response=np.array(self.current_pre_response, float),
                unit=self.current_unit, dark_source=self.current_dark_source,
                peaks=list(self.current_peaks), ob_mean=float(ob_mean),
                integration_us=int(integration_us), fps=float(self.fps),
                color=self.color_metrics, color_fresh=fresh,
                lux_calibration=self.lux_calibration(self.current_unit),
                heatmap_pushed=pushed,
                heatmap_column=None if col is None else col.copy(),
                heatmap_time=ptime,
                display=np.array(dvals, float), display_unit=dunit,
                display_mode=mode, exposure=exposure,
                quantity=processing.photometric_quantity(self.current_unit))
            self.last_frame = ev
        if notify:
            self._emit("frame", ev)
        return ev

    def capture_snapshot(self) -> tuple[np.ndarray, np.ndarray]:
        """(wavelengths, dark-corrected response-uncorrected spectrum) of the
        latest frame — what the wavelength wizard averages."""
        with self._frame_lock:
            return (np.array(self.wavelength_array, float),
                    np.array(self.current_pre_response, float))

    def heatmap_snapshot(self) -> dict:
        with self._frame_lock:
            return {"wavelengths": self.heatmap_linear_waves.copy(),
                    "buffer": self.heatmap_buffer.copy(),
                    "valid": self.heatmap_valid.copy(),
                    "unit": self.heatmap_unit, "mode": self.heatmap_mode,
                    "times": self.heatmap_times.copy(),
                    "filled": int(self.heatmap_filled),
                    "size": int(self.heatmap_buffer.shape[1])}

    # ══════════════════════════════════════════════════════════════════════
    #  Status / metadata
    # ══════════════════════════════════════════════════════════════════════
    def sensor_temp_delta(self) -> float | None:
        if not (self.supports_ob and self.latest_ob_mean > 0):
            return None
        return round(self.latest_ob_mean
                     - DEFAULT_DARK_RATE_ADC_PER_SEC * self.latest_integration_us / 1e6
                     - OB_TEMP_REFERENCE_ADC, 2)

    def status(self) -> dict:
        b = self.acquisition
        cfg = self.cfg
        running = self.jobs.active()
        return json_safe({
            "connected": self.is_connected,
            "backend": self.connected_backend_name,
            "selected_backend": self.selected_backend(),
            "device_path": self.connected_device_path,
            "serial": getattr(b, "serial", None) if b is not None else None,
            "pixel_count": int(self.pixel_count),
            "wavelength_min": float(self.wavelength_array[0]),
            "wavelength_max": float(self.wavelength_array[-1]),
            "spec_range_nm": list(self.spec_range_nm),
            "integration_us": int(getattr(b, "current_integration_time_us",
                                          self.latest_integration_us)),
            "integration_bounds_us": list(self.int_bounds_us()),
            "exposure_ms": float(cfg.get("exposure_ms", 20.0)),
            "auto_exposure": bool(getattr(b, "is_auto_exposure_active",
                                          cfg.get("auto_exposure", False))),
            "frames_to_average": int(self.frames_avg),
            "paused": self.paused,
            "fast_preview": bool(cfg.get("fast_preview_enabled", False)),
            "dark_correction": bool(cfg.get("dark_correction", False)),
            "dark_mode": cfg.get("dark_mode", "optical_black"),
            "display_unit": cfg.get("display_unit", "counts"),
            "display_mode": cfg.get("display_mode", processing.DISPLAY_AUTO),
            "display_mode_effective": self.current_display_mode,
            "display_modes": self.display_modes(),
            "display_value_unit": self.current_display_unit,
            "irradiance_unit": cfg.get("irradiance_unit", "uW/cm2/nm"),
            "unit": self.current_unit,
            "unit_label": processing.unit_label(self.current_unit),
            "quantity": processing.photometric_quantity(self.current_unit),
            "fps": round(float(self.fps), 2),
            "ob_mean": round(float(self.latest_ob_mean), 3),
            "supports_ob": bool(self.supports_ob),
            "supports_shutter": bool(getattr(b, "SUPPORTS_SHUTTER", False)),
            "adc_full_scale": float(getattr(b, "adc_full_scale", 0.0)) if b is not None else None,
            "adc_saturation_level": (float(b.adc_saturation_level) if b is not None else None),
            "calibration": self.calibration_status(),
            "control": self.control_info(),
            "jobs": [j.to_dict(brief=True) for j in running],
            "frame_seq": int(self.frame_seq),
            "heatmap_size": int(self.heatmap_buffer.shape[1]),
        })

    def report_meta(self, peak_adc: float | None = None) -> dict:
        """Acquisition block for colour / filter reports."""
        b = self.acquisition
        us = getattr(b, "current_integration_time_us", None) if b is not None else None
        us = us or self.latest_integration_us
        ob = self.latest_ob_mean
        if peak_adc is None:
            px = self.current_raw
            if px is not None and len(px):
                peak_adc = float(np.nanmax(px)) if np.any(np.isfinite(px)) else None
        st = self.calib.status
        prof = st.get("label") if st.get("state") not in (None, "none") else None
        return {
            "Spectrometer":    self.connected_backend_name or (type(b).__name__ if b else None),
            "Serial":          getattr(b, "serial", None),
            "Date/time":       datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "Exposure":        (f"{us/1000:.1f} ms" if us else None),
            "Dark / baseline": (f"{ob:.0f} ADC" if ob is not None else None),
            "Peak ADC":        (f"{peak_adc:.0f}" if peak_adc is not None else None),
            "Calibration":     prof,
            "Linearity correction": callin.onoff_na(self.linearity_info),
        }

    # ── export (shared desktop + web; see module-level spectrum_csv) ──────
    def _dark_applied_text(self, src: str | None) -> str:
        return {None: "none (no dark subtracted)",
                "stored": "stored per-pixel dark (same t_int)",
                "stored_model": "stored per-pixel dark (b + r·t model)",
                "optical_black": "optical-black / baseline scalar",
                "parametric": "parametric scalar (bias + rate·t)"}.get(src, str(src))

    def _smoothing_text(self) -> str:
        c = self.cfg
        parts = []
        if c.get("fusion_enabled", False):
            parts.append(f"temporal (strength {float(c.get('fusion_smoothing', 0.85)):.2f})")
        if c.get("hot_pixel_filter", False):
            parts.append(f"median despeckle {int(c.get('smoothing_width', 5))} px")
        if c.get("spatial_smoothing", False):
            parts.append(f"spatial boxcar {int(c.get('spatial_smoothing_width', 5))} px")
        return ", ".join(parts) if parts else "off"

    def export_meta_rows(self, ev: "FrameEvent | None" = None) -> list:
        """Header rows of an export: the settings ACTUALLY applied to the data
        of frame `ev` (default: the latest frame)."""
        ev = ev if ev is not None else self.last_frame
        b = self.acquisition
        st = dict(self.calib.status or {})
        prof = self.calib.profile
        unit = (ev.unit if ev is not None else self.current_unit) or "counts"
        t_us = ev.integration_us if ev is not None else self.latest_integration_us
        dsrc = ev.dark_source if ev is not None else self.current_dark_source
        lin = self.linearity_info or {}
        rows = [
            ("Spectrometer", self.connected_backend_name or (type(b).__name__ if b else "")),
            ("Serial", getattr(b, "serial", None) or ""),
            ("Date/time", _iso_from_epoch(ev.timestamp if ev is not None else None)),
            ("Exported", now_iso()),
            ("Integration time (ms)", f"{t_us / 1000.0:.3f}"),
            ("Frames averaged", int(self.frames_avg)),
            ("Smoothing", self._smoothing_text()),
            ("Dark correction", _onoff(dsrc is not None)),
            ("Dark mode (applied)", self._dark_applied_text(dsrc)),
            ("Optical Black mean (ADC)", f"{(ev.ob_mean if ev is not None else self.latest_ob_mean):.3f}"),
            ("NL correction (device)", {True: "on", False: "off"}.get(
                calprof.device_nl_active(b), "n/a")),
            ("Linearity correction", callin.onoff_na(lin)
             + (f" ({lin.get('id')}, {str(lin.get('created') or '')[:10]})"
                if lin.get("state") == callin.STATE_ACTIVE else "")),
        ]
        if prof is not None and self.calib.active_v2:
            rows += [("Calibration profile", st.get("label", "")),
                     ("Profile id", prof.get("id")),
                     ("Profile name", (prof.get("optics") or {}).get("name", "")),
                     ("Profile version", prof.get("version")),
                     ("Profile type", prof.get("type")),
                     ("Profile state", st.get("state"))]
            if st.get("linearity_note"):
                rows.append(("Profile note", st["linearity_note"]))
        else:
            rows.append(("Calibration profile", st.get("label") if self.calib.legacy_gain
                         is not None else "None"))
            if st.get("legacy_note"):
                rows.append(("Profile note", st["legacy_note"]))
        if self.calib.legacy_gain is not None:
            # legacy gain path only: the old switch is reported when it is in effect
            rows.append(("Response compensation", "on (legacy gain table)"))
        mode = getattr(ev, "display_mode", None) if ev is not None else None
        dunit = getattr(ev, "display_unit", None) if ev is not None else None
        if mode:
            rows.append(("Display mode", processing.DISPLAY_LABELS.get(mode, mode)
                         if hasattr(processing, "DISPLAY_LABELS") else mode))
        if dunit:
            rows.append(("Display unit", processing.unit_label(dunit)))
        rows.append(("Unit", unit))
        if self.calib.active_v2 and processing.is_absolute_unit(unit):
            rows.append(("Quantity", processing.unit_quantity(unit)))
        if self.calib.active_v2:
            vr = st.get("valid_range_nm")
            if vr:
                rows.append(("Valid range (nm)", f"{float(vr[0]):.1f}–{float(vr[1]):.1f}"))
            segs = st.get("valid_segments_nm")
            rows.append(("Valid segments (nm)", _segments_text(segs) or
                         (f"{float(vr[0]):.1f}–{float(vr[1]):.1f}" if vr else "")))
            rows.append(("Masked pixels", MASKED_NOTE))
            if st.get("implausible"):
                rows.append(("Plausibility", "FAILED (saved on request)"))
        return rows

    def export_snapshot(self, ev: "FrameEvent | None" = None) -> dict:
        """Data + header of one frame for export (desktop + web identical)."""
        ev = ev if ev is not None else self.last_frame
        with self._frame_lock:
            if ev is not None:
                d = {"wavelengths": np.array(ev.wavelengths, float),
                     "values": np.array(ev.pixels, float), "unit": ev.unit,
                     "raw": np.array(ev.raw_averaged, float),
                     "display": (None if getattr(ev, "display", None) is None
                                 else np.array(ev.display, float)),
                     "display_unit": getattr(ev, "display_unit", None),
                     "display_mode": getattr(ev, "display_mode", None)}
            else:
                d = {"wavelengths": np.array(self.wavelength_array, float),
                     "values": np.array(self.current_pixels, float),
                     "unit": self.current_unit, "raw": np.array(self.current_raw, float),
                     "display": None, "display_unit": None, "display_mode": None}
        d["meta"] = self.export_meta_rows(ev)
        return d

    def export_spectrum_csv(self, ev: "FrameEvent | None" = None) -> str:
        return spectrum_csv(self.export_snapshot(ev))

    def _heatmap_meta(self, h: dict) -> list:
        meta = [r for r in self.export_meta_rows() if r[0] in (
            "Spectrometer", "Serial", "Exported", "Integration time (ms)",
            "Calibration profile", "Profile id", "Linearity correction")]
        mode = h.get("mode")
        meta += [("Display mode", processing.DISPLAY_LABELS.get(mode, mode) if mode else ""),
                 ("Unit", h.get("unit") or ""),
                 ("Content", "display representation of each logged frame "
                             "(the time-lapse records what is plotted)")]
        if h.get("valid") is not None and not bool(np.all(h["valid"])):
            meta.append(("Masked pixels", MASKED_NOTE))
        return meta

    def export_heatmap_csv(self, indices=None) -> str:
        h = self.heatmap_snapshot()
        return heatmap_csv_text(h, indices, self._heatmap_meta(h))

    def export_heatmap_frame_csv(self, index: int) -> str:
        """One logged time-lapse frame as a (wavelength, value) CSV with the
        same header conventions (desktop 'export this frame')."""
        h = self.heatmap_snapshot()
        i = max(0, min(int(index), max(int(h["filled"]) - 1, 0)))
        t = h["times"][i]
        meta = self._heatmap_meta(h) + [
            ("Time-lapse frame", i),
            ("Seconds before export", f"{time.monotonic() - t:.1f}" if np.isfinite(t)
             else "unknown")]
        unit = h.get("unit") or "counts"
        buf = io.StringIO()
        for k, v in meta:
            buf.write(f"# {k};{'' if v is None else str(v).replace(';', ',')}\n")
        buf.write(f"Wavelength_nm;{_value_column(unit)}\n")
        col, ok = h["buffer"][:, i], h["valid"][:, i]
        for w, v, good in zip(h["wavelengths"], col, ok):
            buf.write(f"{w:.3f};{'' if not (good and np.isfinite(v)) else f'{v:.6g}'}\n")
        return buf.getvalue()

    # ══════════════════════════════════════════════════════════════════════
    #  Measurements (jobs, via frame taps — not the display pipeline)
    # ══════════════════════════════════════════════════════════════════════
    def _require_backend(self):
        b = self.acquisition
        if b is None:
            raise NotConnected("No device connected.")
        return b

    def _capture(self, ctx: JobContext | None, n: int, integration_us: int,
                 kind: str = "light"):
        """Cancel-aware capture of n clean frames. Returns (Capture, dark_mean)."""
        b = self._require_backend()
        s = cc.CaptureSession(b, n, integration_us=integration_us, kind=kind,
                              backend_name=self.device_name)
        s.start()
        try:
            while not s.finished:
                if ctx is not None:
                    ctx.check()
                if s.timed_out:
                    break
                time.sleep(0.02)
        finally:
            s._teardown()
        cap = s.finish()
        return cap, (s.acc.dark_mean if s.acc else 0.0)

    def _measure_once(self, ctx: JobContext | None, p: dict, *,
                      integration_us: int | None = None) -> dict:
        """One measurement (see API MeasureRequest). Returns the stored record."""
        b = self._require_backend()
        frames = max(1, int(p.get("frames") or 1))
        dark_mode = str(p.get("dark") or "auto")
        if dark_mode not in ("auto", "stored", "capture", "none"):
            raise EngineError(f"Unknown dark mode '{dark_mode}'.")
        warnings: list[str] = []
        prev_us = int(b.current_integration_time_us)
        auto = False
        try:
            # integration time
            if integration_us is None:
                if p.get("integration_ms") in (None, "auto") and p.get("auto_exposure"):
                    auto = True
                    if ctx:
                        ctx.progress(message="auto exposure", step="auto_exposure")
                    import calibration_workflow as cw
                    # target band capped at the saturation warning threshold
                    t, _cap, conv = cc.auto_expose(
                        b, target_frac=cw.WorkflowConfig.from_config().target_frac,
                        backend_name=self.device_name)
                    if not conv:
                        warnings.append("Auto exposure did not reach the target band.")
                    integration_us = int(t)
                elif p.get("integration_ms") not in (None, "auto"):
                    lo, hi = self.int_bounds_us()
                    integration_us = max(lo, min(hi, int(float(p["integration_ms"]) * 1000)))
                else:
                    integration_us = prev_us
            # dark
            if dark_mode == "capture":
                self._dark_step(ctx, integration_us, frames=max(frames, int(
                    self.cfg.get("calibration_capture_frames", 16))))
            if ctx:
                ctx.progress(message=f"capturing {frames} frame(s) at "
                                     f"{integration_us/1000:.3f} ms", step="capture")
            cap, ob = self._capture(ctx, frames, integration_us)
        finally:
            try:
                if int(b.current_integration_time_us) != prev_us:
                    b.set_integration_time_us(prev_us)
            except Exception:                                    # noqa: BLE001
                pass

        runtime = self.calib
        overrides: dict = {"show_peaks": bool(p.get("include_peaks", True))}
        if dark_mode in ("stored", "capture"):
            arr, _src = runtime.dark_for(cap.integration_us)
            if arr is None:
                raise Unprocessable(
                    f"No stored dark spectrum for t_int = {cap.integration_us/1000:.3f} ms "
                    f"(use dark='capture').")
            overrides.update(dark_correction=True, dark_mode=processing.DARK_MODE_STORED)
        elif dark_mode == "none":
            overrides.update(dark_correction=False)
            if runtime.active_v2:
                runtime = None                       # raw counts, no profile
                warnings.append("dark='none': calibration profile NOT applied "
                                "(a calibrated value needs a dark subtraction).")
        out, _kw = self._process(cap.mean, ob, cap.integration_us, cap.wl,
                                 cfg_get=_CfgOverlay(self.cfg.get, overrides),
                                 runtime=runtime if runtime is not None else calprof.CalibrationRuntime())
        unit = out["unit"]
        st = dict(runtime.status) if runtime is not None else {"state": "none", "label": calprof.SEL_NONE}
        if cap.n_saturated:
            warnings.append(f"{cap.n_saturated} pixel(s) saturated — reduce the "
                            f"integration time.")
        if cap.peak_frac < 0.05:
            warnings.append(f"Low signal: peak at {cap.peak_frac*100:.1f} % of full scale.")
        if st.get("dark_warning"):
            warnings.append(st["dark_warning"])
        rec = {
            "id": f"m-{datetime.now().strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:6]}",
            "timestamp": now_iso(),
            "label": str(p.get("label") or ""),
            "device": {"backend": self.device_name,
                       "serial": getattr(b, "serial", None),
                       "device_path": self.connected_device_path,
                       "wl_fingerprint": cap.wl_fingerprint,
                       "nl_correction": calprof.device_nl_active(b),
                       "linearity": callin.profile_linearity_tag(self.linearity_info)},
            "acquisition": {"integration_us": int(cap.integration_us),
                            "integration_ms": cap.integration_us / 1000.0,
                            "auto_exposure": auto, "frames": int(cap.n_frames),
                            "dark": dark_mode, "dark_source": out["dark_source"],
                            "dark_scalar": round(float(ob), 3),
                            "peak_counts": float(cap.peak_counts),
                            "peak_frac": float(cap.peak_frac),
                            "n_saturated": int(cap.n_saturated),
                            "saturation_level": float(cap.saturation_level),
                            "full_scale": float(cap.full_scale)},
            "calibration": {"state": st.get("state"), "label": st.get("label"),
                            "profile_id": st.get("profile_id"),
                            "type": st.get("type"),
                            "valid_range_nm": st.get("valid_range_nm"),
                            "valid_segments_nm": st.get("valid_segments_nm"),
                            "implausible": bool(st.get("implausible", False))},
            "unit": unit, "unit_label": processing.unit_label(unit),
            "quantity": processing.photometric_quantity(unit),
            "valid_range_nm": st.get("valid_range_nm"),
            "wavelengths": np.asarray(cap.wl, float),
            "values": np.asarray(out["pixels"], float),
            "raw_mean": np.asarray(cap.mean, float),
            "peaks": out["peaks"],
            "warnings": warnings,
            "color": None,
        }
        if p.get("include_color", True):
            try:
                m = self.color_metrics_for(cap.wl, out["pixels"], unit)
                m.pop("tcs_swatches", None)
                rec["color"] = json_safe(m)
            except Exception as e:                               # noqa: BLE001
                warnings.append(f"Colour metrics failed: {e}")
        self._store_measurement(rec)
        return rec

    def _dark_step(self, ctx: JobContext | None, integration_us: int, frames: int) -> cc.Capture:
        b = self._require_backend()
        needs_cover = not getattr(b, "SUPPORTS_SHUTTER", False)
        if needs_cover:
            if ctx is None:
                raise Unprocessable("Dark capture needs the light path blocked; "
                                    "run it as a job.")
            ctx.wait_for_user("Block the light path / cover the input, then continue. "
                              "A dark spectrum is captured next.", action="cover")
        if ctx:
            ctx.progress(message=f"capturing dark ({frames} frames)", step="dark")
        d = cc.capture_dark(b, max(2, int(frames)), integration_us=integration_us,
                            backend_name=self.device_name)
        cc.save_dark(d)
        self.calib.invalidate_darks()
        if needs_cover and ctx is not None:
            ctx.wait_for_user("Uncover the input / release the light path, then "
                              "continue.", action="uncover")
        return d

    def _store_measurement(self, rec: dict) -> None:
        with self._meas_lock:
            self.measurements[rec["id"]] = rec
            while len(self.measurements) > MAX_MEASUREMENTS:
                self.measurements.popitem(last=False)
        self._emit("measurement", {"id": rec["id"], "kind": "measure"})

    def get_measurement(self, mid: str) -> dict:
        with self._meas_lock:
            m = self.measurements.get(mid)
        if m is None:
            raise NotFound(f"Measurement '{mid}' not found.")
        return m

    def list_measurements(self) -> list[dict]:
        with self._meas_lock:
            return list(self.measurements.values())

    def _with_profile(self, profile: str | None, confirm: bool, owner: str):
        """Context helper: temporarily switch the active selection."""
        eng = self

        class _Ctx:
            def __enter__(self_inner):
                self_inner.prev = None
                if profile:
                    dev = eng.device_name
                    cur = calprof.active_selection(dev)
                    if cur != profile:
                        self_inner.prev = cur
                        eng.select_profile(profile, confirm=confirm, owner=owner)
                return self_inner

            def __exit__(self_inner, *exc):
                if self_inner.prev is not None:
                    try:
                        calprof.set_active_selection(eng.device_name, self_inner.prev)
                        eng.rebuild_calibration()
                    except Exception as e:                       # noqa: BLE001
                        print(f"[engine] restoring profile failed: {e!r}")
                return False
        return _Ctx()

    # ── job submitters ────────────────────────────────────────────────────
    def submit_measure(self, params: dict, *, owner: str) -> Job:
        self._require_backend()
        p = dict(params or {})

        def run(ctx: JobContext):
            with self._with_profile(p.get("profile"), bool(p.get("confirm_profile")), ctx.job.owner):
                rec = self._measure_once(ctx, p)
            ctx.progress(1.0, "measurement stored", step="done")
            return {"measurement_id": rec["id"]}
        return self.jobs.submit("measure", run, p, owner)

    def submit_series(self, params: dict, *, owner: str) -> Job:
        self._require_backend()
        p = dict(params or {})
        count = int(p.get("count") or 0)
        if count < 1:
            raise Unprocessable("count must be ≥ 1.")
        interval = max(0.0, float(p.get("interval_s") or 0.0))

        def run(ctx: JobContext):
            ids: list[str] = []
            summary: list[dict] = []
            mp = dict(p)
            with self._with_profile(p.get("profile"), bool(p.get("confirm_profile")), ctx.job.owner):
                t_fixed = None
                if mp.get("dark") == "capture":
                    # one dark for the whole series, at the series' t_int
                    b = self._require_backend()
                    if mp.get("integration_ms") not in (None, "auto"):
                        lo, hi = self.int_bounds_us()
                        t_fixed = max(lo, min(hi, int(float(mp["integration_ms"]) * 1000)))
                    elif mp.get("auto_exposure"):
                        t_fixed, _c, _ok = cc.auto_expose(b, backend_name=self.device_name)
                    else:
                        t_fixed = int(b.current_integration_time_us)
                    self._dark_step(ctx, t_fixed, max(int(mp.get("frames") or 1), int(
                        self.cfg.get("calibration_capture_frames", 16))))
                    mp["dark"] = "stored"
                t0 = time.monotonic()
                for i in range(count):
                    ctx.progress(i / count, f"measurement {i + 1}/{count}", step="measure")
                    rec = self._measure_once(ctx, mp, integration_us=t_fixed)
                    ids.append(rec["id"])
                    c = rec.get("color") or {}
                    summary.append({"id": rec["id"], "timestamp": rec["timestamp"],
                                    "integration_ms": rec["acquisition"]["integration_ms"],
                                    "peak_frac": rec["acquisition"]["peak_frac"],
                                    "cct": c.get("cct"), "lux": c.get("lux"),
                                    "peak_nm": c.get("peak_nm")})
                    if i + 1 < count and interval > 0:
                        ctx.sleep(max(0.0, t0 + (i + 1) * interval - time.monotonic()))
            ctx.progress(1.0, f"{count} measurement(s) stored", step="done")
            return {"measurement_ids": ids, "summary": summary}
        return self.jobs.submit("series", run, p, owner)

    def submit_dark(self, params: dict, *, owner: str) -> Job:
        b = self._require_backend()
        p = dict(params or {})

        def run(ctx: JobContext):
            t = p.get("integration_ms")
            lo, hi = self.int_bounds_us()
            us = (max(lo, min(hi, int(float(t) * 1000))) if t not in (None, "")
                  else int(b.current_integration_time_us))
            n = int(p.get("frames") or self.cfg.get("calibration_capture_frames", 16))
            d = self._dark_step(ctx, us, n)
            self._emit("calibration", self.calibration_status())
            return {"integration_us": int(d.integration_us), "n_frames": int(d.n_frames),
                    "mean_counts": float(np.mean(d.mean)), "timestamp": d.timestamp}
        return self.jobs.submit("dark", run, p, owner)

    def submit_calibration(self, params: dict, *, owner: str) -> Job:
        """Calibration run (relative / absolute) with the fixed wizard order:
        wavelength check → t_int → darks before (cover) → calibration spectrum
        + repeat (uncover) → t/2 capture → darks after (cover / uncover) →
        per-pixel checks + cause model → compute + plausibility → save
        (+ activate) → verification through the live pipeline → protocol
        (PDF + CSV)."""
        import calibration_workflow as cw
        import reference_sources as rs
        b = self._require_backend()
        p = dict(params or {})
        ref = rs.ReferenceLibrary().get(str(p.get("reference_id", "")))
        if ref is None:
            raise NotFound(f"Reference source '{p.get('reference_id')}' not found.")
        optics = dict(p.get("optics") or {})
        wf_cfg = cw.WorkflowConfig.from_config()
        if p.get("frames"):
            wf_cfg.n_frames = max(2, int(p["frames"]))
        try:
            wf = cw.CalibrationWorkflow(b, self.device_name, kind=str(p.get("kind", "relative")),
                                        optics=optics, reference=ref, cfg=wf_cfg)
        except ValueError as e:
            raise Unprocessable(str(e))

        def run(ctx: JobContext):
            res: dict = {"status": "failed", "profile_ids": [], "protocol": []}
            steps = 10

            def prog(i, msg, step):
                ctx.progress(i / steps, msg, step=step)
            try:
                prog(0, "checking the wavelength calibration", "wavelength_check")
                chk = wf.check_wavelength()
                res["wavelength_check"] = json_safe({k: v for k, v in chk.items()
                                                     if k in ("ok", "fingerprint", "messages")})
                if not chk.get("ok"):
                    raise Unprocessable("; ".join(chk.get("messages", []))
                                        or "Wavelength axis unusable.")
                prog(1, "setting the integration time", "integration")
                us = (None if p.get("integration_ms") in (None, "auto")
                      else int(float(p["integration_ms"]) * 1000))
                info = wf.set_integration(us, auto=us is None)
                res["integration"] = json_safe(info)
                prog(2, "dark spectra", "dark")
                if wf.needs_user_cover:
                    ctx.wait_for_user("Block the light path / cover the lamp. The dark "
                                      "spectra are captured next.", action="cover")
                ctx.check()
                dk = wf.capture_darks()
                res["dark"] = json_safe(dk)
                self.calib.invalidate_darks()
                if wf.needs_user_cover:
                    ctx.wait_for_user("Release the light path / uncover the lamp. The "
                                      "calibration spectrum is captured next.",
                                      action="uncover")
                prog(3, "calibration spectrum + stability", "calibration_capture")
                cal = wf.capture_calibration(sleep=ctx.sleep)
                res["stability"] = json_safe(cal.get("stability"))
                prog(4, "integration-time cross check", "crosscheck")
                cx = wf.crosscheck_integration()
                prog(5, "dark spectra after the light captures", "dark_after")
                if wf.needs_user_cover:
                    ctx.wait_for_user("Block the light path / cover the lamp again. The "
                                      "dark spectra AFTER the light captures are taken "
                                      "next.", action="cover")
                ctx.check()
                ev = wf.capture_darks_after()
                self.calib.invalidate_darks()
                if wf.needs_user_cover:
                    ctx.wait_for_user("Release the light path / uncover the lamp (for "
                                      "the verification measurement).", action="uncover")
                res["dark_drift"] = json_safe(ev.get("drift"))
                res["stability"] = json_safe(wf.checks.get("stability_detail"))
                res["crosscheck"] = json_safe({
                    "skipped": cx.get("skipped", False), "pass": wf.checks.get("itime_pass"),
                    "crosscheck": wf.checks.get("itime_detail")})
                res["causes"] = json_safe({k: {"dominant": v.get("dominant"),
                                               "message": v.get("message")}
                                           for k, v in wf.causes.items()})
                prog(6, "computing", "compute")
                try:
                    r = wf.compute()
                except ValueError as e:
                    res.update(status="failed", error=str(e))
                    return res
                res["valid_segments_nm"] = json_safe(r.get("segments_nm"))
                res["gain_span"] = json_safe(r.get("gain_span"))
                res["plausibility"] = json_safe(wf.plausibility)
                res["checks"] = json_safe(wf.checks)
                prog(7, "saving", "save")
                activate = bool(p.get("activate", True))
                if not wf.plausible:
                    if not p.get("allow_implausible"):
                        res["status"] = "not_saved_implausible"
                        return res
                    saved = wf.save(allow_implausible=True)
                    activate = False
                    res["status"] = "saved_implausible"
                elif (any(wf.checks.get(k) is False for k in ("stability_pass", "itime_pass"))
                      and not p.get("save_on_failed_checks")):
                    res["status"] = "not_saved_checks_failed"
                    return res
                else:
                    saved = wf.save()
                    res["status"] = "saved"
                res["profile_ids"] = [s["id"] for s in saved]
                if activate:
                    calprof.set_active_selection(wf.backend_name, saved[0]["id"])
                    res["activated"] = saved[0]["id"]
                self.rebuild_calibration()
                if p.get("verify", True):
                    prog(8, "verification: reference through the live pipeline",
                         "verification")
                    try:
                        ver = self.verify_profile(saved[0]["id"], ref.id, ctx=ctx,
                                                  owner=None)
                        wf.verification = ver
                        res["verification"] = json_safe(
                            {k: v for k, v in ver.items() if k != "curve"})
                    except JobCancelled:
                        raise
                    except Exception as e:                       # noqa: BLE001
                        wf.warnings.append(f"Verification failed: {e}")
                        res["verification_error"] = str(e)
                return res
            finally:
                res["warnings"] = list(wf.warnings)
                if wf.result is not None or res.get("status") == "failed":
                    try:
                        ctx.progress(9 / steps, "writing the protocol", step="protocol")
                    except JobCancelled:
                        pass
                    res["protocol"], res["protocol_message"] = self._write_protocol(wf)
                ctx.job.result = res
        return self.jobs.submit("calibration", run, p, owner)

    # ── verification of a saved profile (point 6 of the hardware test) ────
    def verify_profile(self, profile_id: str, reference_id: str, *,
                       frames: int | None = None, integration_ms: float | None = None,
                       ctx: JobContext | None = None, owner: str | None = LOCAL) -> dict:
        """Measure the reference source WITH profile `profile_id` through the
        live pipeline (frame-tap capture → processing.process_frame with the
        same kwargs as the live display, stored dark at t_int) and compare it
        with the reference file (calibration_workflow.verify_against_reference:
        normalised, per 50-nm band and overall; absolute profiles also
        unnormalised). The result is kept as last_verification (scope overlay)."""
        import calibration_workflow as cw
        import reference_sources as rs
        self.check_control(owner)
        self._require_backend()
        ref = rs.ReferenceLibrary().get(str(reference_id))
        if ref is None:
            raise NotFound(f"Reference source '{reference_id}' not found.")
        prof = calprof.get_profile_by_id(str(profile_id))
        if prof is None:
            raise NotFound(f"Profile '{profile_id}' not found.")
        if prof.get("apply") != calprof.APPLY_V2:
            raise Unprocessable("Only v2 profiles (wizard) can be verified.")
        if (prof.get("type") == calprof.TYPE_ABSOLUTE and ref.unit != rs.UNIT_RELATIVE
                and calprof.profile_quantity(prof) != ref.quantity):
            raise Unprocessable(
                f"The profile measures {calprof.profile_quantity(prof)}, the reference "
                f"source '{ref.name}' is {ref.quantity} — verify against a source of "
                f"the same measurement quantity.")
        t_us = (int(float(integration_ms) * 1000) if integration_ms
                else int((prof.get("acquisition") or {}).get("integration_us") or 0))
        mp = {"frames": int(frames or self.cfg.get("calibration_capture_frames", 16)),
              "dark": "stored", "include_color": False, "include_peaks": False,
              "label": f"verification {profile_id}"}
        if t_us:
            mp["integration_ms"] = t_us / 1000.0
        if ctx is not None:
            ctx.progress(message="verification: measuring the reference source",
                         step="verification")
        with self._with_profile(str(profile_id), True, owner):
            rec = self._measure_once(ctx, mp)
        wl = np.asarray(rec["wavelengths"], float)
        vals = np.asarray(rec["values"], float)
        kind = prof.get("type", calprof.TYPE_RELATIVE)
        unit = rec["unit"] if kind == calprof.TYPE_ABSOLUTE else None
        e = ref.on_axis(wl, unit=unit) if unit else ref.on_axis(wl)
        lo, hi = (self.cfg.get("calibration_verify_norm_lo_nm", 550.0),
                  self.cfg.get("calibration_verify_norm_hi_nm", 650.0))
        ver = cw.verify_against_reference(
            wl, vals, e, kind=kind, band_nm=float(self.cfg.get("calibration_verify_band_nm", 50.0)),
            norm_range_nm=(float(lo), float(hi)),
            warn_pct=float(self.cfg.get("calibration_verify_warn_pct", 2.0)))
        ver.update({"profile_id": prof["id"], "profile_label": calprof.profile_label(prof),
                    "reference_id": ref.id, "reference_name": ref.name,
                    "measurement_id": rec["id"], "timestamp": rec["timestamp"],
                    "integration_us": rec["acquisition"]["integration_us"],
                    "unit": rec["unit"], "type": kind,
                    "warnings": list(rec.get("warnings") or [])})
        self.last_verification = ver
        self.cfg.set("scope_reference_id", ref.id, save=False)
        self._emit("verification", json_safe({k: v for k, v in ver.items() if k != "curve"}))
        return ver

    def submit_verification(self, params: dict, *, owner: str) -> Job:
        self._require_backend()
        p = dict(params or {})

        def run(ctx: JobContext):
            ver = self.verify_profile(str(p.get("profile_id", "")),
                                      str(p.get("reference_id", "")),
                                      frames=p.get("frames"),
                                      integration_ms=p.get("integration_ms"), ctx=ctx,
                                      owner=ctx.job.owner)
            ctx.progress(1.0, "verification done", step="done")
            return json_safe(ver)
        return self.jobs.submit("verification", run, p, owner)

    # ── reference overlay for the scope (desktop + web identical) ─────────
    def reference_overlay(self, reference_id: str | None, wl, display_values,
                          display_unit: str) -> np.ndarray | None:
        """Reference curve on the wavelength axis, scaled to the CURRENT
        display: absolute display → the reference in the display unit
        (unscaled); any other mode → least-squares scale onto the displayed
        values over the finite (valid) pixels. None if not available."""
        import reference_sources as rs
        if not reference_id:
            return None
        wl = np.asarray(wl, float)
        key = (str(reference_id), wl.size, float(wl[0]) if wl.size else 0.0,
               float(wl[-1]) if wl.size else 0.0, display_unit if
               processing.is_absolute_unit(display_unit) else "")
        # cache value: (curve, unscaled) — kept together, so concurrent
        # callers (desktop + web) never mix the curve of one reference with
        # the quantity check of another
        hit = self._overlay_cache.get(key)
        if hit is None:
            ref = rs.ReferenceLibrary().get(str(reference_id))
            if ref is None:
                return None
            # unscaled only for the SAME measurement quantity (irradiance
            # reference on an irradiance display, radiance on radiance);
            # otherwise the curve is scaled onto the display like a relative one
            q = processing.unit_quantity(ref.unit)
            unscaled = (processing.is_absolute_unit(display_unit)
                        and q == processing.unit_quantity(display_unit))
            try:
                e = ref.on_axis(wl, unit=display_unit) if unscaled else ref.on_axis(wl)
            except Exception:                                    # noqa: BLE001
                e, unscaled = ref.on_axis(wl), False
            hit = (np.asarray(e, float), bool(unscaled))
            self._overlay_cache = {key: hit}
        e, unscaled = hit
        if unscaled:
            return e
        v = np.asarray(display_values, float)
        m = np.isfinite(v) & np.isfinite(e) & (e > 0)
        if np.count_nonzero(m) < 3:
            return None
        den = float(np.sum(e[m] ** 2))
        scale = float(np.sum(v[m] * e[m]) / den) if den > 0 else 0.0
        return e * scale

    # ── linearity calibration job ────────────────────────────────────────
    def submit_linearity(self, params: dict, *, owner: str) -> Job:
        """Linearity calibration of the connected unit (or a check on devices
        with a factory linearisation): plan → darks before (cover) → light
        steps incl. repeated reference (uncover) → darks after (cover /
        uncover) → fit → save (+ activate) → protocol (PDF + CSV)."""
        b = self._require_backend()
        p = dict(params or {})
        cfg = callin.LinearityConfig.from_config()
        if p.get("steps"):
            cfg.n_steps = int(min(10, max(6, int(p["steps"]))))
        if p.get("frames"):
            cfg.n_frames = max(2, int(p["frames"]))
        wf = callin.LinearityWorkflow(b, self.device_name, cfg)

        def run(ctx: JobContext):
            res: dict = {"status": "failed", "mode": wf.mode, "protocol": []}
            try:
                ctx.progress(0.02, "probing the source", step="plan")
                us = (None if p.get("integration_ms") in (None, "auto")
                      else int(float(p["integration_ms"]) * 1000))
                res["plan"] = json_safe(wf.plan(us))
                if wf.needs_user_cover:
                    ctx.wait_for_user("Block the light path / cover the input. Dark "
                                      "spectra at all integration times are captured "
                                      "next.", action="cover")
                wf.capture_darks("before", progress=lambda f, m: ctx.progress(
                    0.05 + 0.15 * f, m, step="dark_before"))
                if wf.needs_user_cover:
                    ctx.wait_for_user("Release the light path / uncover the input. The "
                                      "light steps are captured next (keep the source "
                                      "and the fibre untouched).", action="uncover")
                wf.capture_steps(sleep=ctx.sleep, check=ctx.check,
                                 progress=lambda f, m: ctx.progress(0.2 + 0.5 * f, m,
                                                                    step="steps"))
                if wf.needs_user_cover:
                    ctx.wait_for_user("Block the light path / cover the input again. "
                                      "The dark spectra AFTER the steps are captured "
                                      "next.", action="cover")
                wf.capture_darks("after", progress=lambda f, m: ctx.progress(
                    0.7 + 0.15 * f, m, step="dark_after"))
                if wf.needs_user_cover:
                    ctx.wait_for_user("Release the light path / uncover the input.",
                                      action="uncover")
                ctx.progress(0.87, "fitting the correction", step="compute")
                try:
                    res["result"] = json_safe(wf.compute())
                except ValueError as e:
                    res["error"] = str(e)
                    return res
                if p.get("save", True):
                    saved = wf.save(activate=bool(p.get("activate", True)))
                    res["entry_id"] = saved["id"]
                    res["status"] = "saved" if wf.mode == callin.MODE_CORRECTION else "check_saved"
                    self.reload_linearity()
                    res["linearity"] = json_safe(self.linearity_info)
                else:
                    res["status"] = "computed"
                return res
            finally:
                res["warnings"] = list(wf.warnings)
                if wf.model is not None or res.get("error"):
                    res["protocol"], res["protocol_message"] = self._write_linearity_protocol(wf)
                ctx.job.result = res
        return self.jobs.submit("linearity", run, p, owner)

    def _write_linearity_protocol(self, wf) -> tuple[list[str], str]:
        try:
            import calibration_report as crp
            import export_paths
            rec = wf.record()
            d, note = export_paths.calibration_protocol_dir()
            base = crp.default_linearity_basename(rec)
            paths = crp.write_linearity_protocol(rec, os.path.join(d, base + ".pdf"),
                                                 os.path.join(d, base + ".csv"))
            return list(paths or []), note or ""
        except Exception as e:                                   # noqa: BLE001
            return [], f"Protocol not written: {e}"


    def submit_derivation(self, params: dict, *, owner: str) -> Job:
        """Derive the relative profile of a NEW optics configuration from a
        KNOWN v2 profile (same diffuse source, both optics) — the desktop
        wizard's derivation, with its user steps as waiting_for_user:
        mount known optics → t_int known → (cover) dark → (uncover) light →
        swap optics → t_int new → (cover) dark → (uncover) light → (cover)
        dark after → swap back → repeat light → (cover) dark known after →
        (uncover) → compute → save → protocol (PDF + CSV). Not plausible:
        saved only with allow_implausible and never activated. Failed drift /
        consistency checks: saved only with save_on_failed_checks and then
        activated (if 'activate'), like the calibration run.

        Parameters: integration_known_us / integration_new_us or
        integration_known_ms / integration_new_ms (None / "auto" = auto
        exposure with that side's optics mounted); integration_ms is the
        legacy value for both sides."""
        import calibration_workflow as cw
        b = self._require_backend()
        p = dict(params or {})
        wf_cfg = cw.WorkflowConfig.from_config()
        if p.get("frames"):
            wf_cfg.n_frames = max(2, int(p["frames"]))
        try:
            dw = cw.DerivationWorkflow(b, self.device_name,
                                       known_profile_id=str(p.get("known_profile_id", "")),
                                       optics=dict(p.get("optics") or {}), cfg=wf_cfg)
        except KeyError:
            raise NotFound(f"Profile '{p.get('known_profile_id')}' not found.")
        except ValueError as e:
            raise Unprocessable(str(e))

        def side_us(which: str):
            """Fixed integration time [µs] of one side or None (auto)."""
            v = p.get(f"integration_{which}_us")
            if v is None:
                ms = p.get(f"integration_{which}_ms")
                if ms is None:
                    ms = p.get("integration_ms")
                v = None if ms in (None, "auto") else float(ms) * 1000.0
            return None if v is None else max(1, int(round(float(v))))

        def cover(ctx: JobContext, what: str):
            if dw.needs_user_cover:
                ctx.wait_for_user(f"Block the light path / cover the source. The dark "
                                  f"spectrum ({what}) is captured next.", action="cover")
            ctx.check()

        def uncover(ctx: JobContext, what: str):
            if dw.needs_user_cover:
                ctx.wait_for_user(f"Release the light path / uncover the source. The "
                                  f"{what} is captured next.", action="uncover")
            ctx.check()

        def run(ctx: JobContext):
            res: dict = {"status": "failed", "profile_ids": [], "protocol": [],
                         "source_hint": cw.DERIVATION_SOURCE_HINT}
            try:
                ctx.wait_for_user("Mount the KNOWN optics on the source and release "
                                  "the light path (the integration time is set "
                                  "with the light on). "
                                  + cw.DERIVATION_SOURCE_HINT + " Then continue.",
                                  action="mount_known")
                ctx.progress(0.05, "integration time (known optics)",
                             step="integration_known")
                ik = dw.set_integration("known", side_us("known"))
                res["integration_known_us"] = int(ik["integration_us"])
                cover(ctx, "known optics")
                ctx.progress(0.12, "dark spectrum (known optics)", step="dark_known")
                dw.capture_dark_for("known")
                self.calib.invalidate_darks()
                uncover(ctx, "light spectrum with the known optics")
                ctx.progress(0.2, "light spectrum (known optics)", step="light_known")
                dw.capture_light_for("known")
                res["peak_frac_known"] = json_safe(dw.lights["known"].peak_frac)

                ctx.wait_for_user("Mount the NEW optics on the same source (same position, "
                                  "same lamp, unchanged power) and keep the light path "
                                  "open, then continue.",
                                  action="swap_optics")
                ctx.progress(0.3, "integration time (new optics)", step="integration_new")
                inw = dw.set_integration("new", side_us("new"))
                res["integration_new_us"] = int(inw["integration_us"])
                cover(ctx, "new optics")
                ctx.progress(0.38, "dark spectrum (new optics)", step="dark_new")
                dw.capture_dark_for("new")
                self.calib.invalidate_darks()
                uncover(ctx, "light spectrum with the new optics")
                ctx.progress(0.46, "light spectrum (new optics)", step="light_new")
                dw.capture_light_for("new")
                res["peak_frac_new"] = json_safe(dw.lights["new"].peak_frac)
                cover(ctx, "new optics, after the light spectrum")
                ctx.progress(0.54, "dark spectrum after (new optics)",
                             step="dark_new_after")
                dw.capture_dark_for("new", "after")
                self.calib.invalidate_darks()

                ctx.wait_for_user("Mount the KNOWN optics again on the same source "
                                  "(same position) and release the light path, then "
                                  "continue.", action="swap_back")
                ctx.progress(0.65, "repeat with the known optics (source drift)",
                             step="repeat_known")
                dw.capture_light_for("repeat")
                res["peak_frac_repeat"] = json_safe(dw.lights["repeat"].peak_frac)
                cover(ctx, "known optics, after the repeat")
                ctx.progress(0.75, "dark spectrum after (known optics)",
                             step="dark_known_after")
                dw.capture_dark_for("known", "after")
                self.calib.invalidate_darks()
                if dw.needs_user_cover:
                    ctx.wait_for_user("Release the light path / uncover the source. "
                                      "The result is computed next.", action="uncover")
                ctx.check()

                ctx.progress(0.85, "computing", step="compute")
                try:
                    r = dw.compute()
                except ValueError as e:
                    res["error"] = str(e)
                    return res
                res["valid_segments_nm"] = json_safe(r.get("segments_nm"))
                res["plausibility"] = json_safe(dw.plausibility)
                res["checks"] = json_safe(dw.checks)
                res["drift"] = (json_safe({k: v for k, v in dw.drift.items()
                                           if k != "per_pixel_pct"}) if dw.drift else None)
                ds = dw.drift_status()
                res["drift_status"] = json_safe(ds)
                ctx.progress(0.92, "saving", step="save")
                activate = bool(p.get("activate", True))
                if dw.drift_failed and not p.get("save_on_failed_checks"):
                    # drift is the primary cause: reported before plausibility
                    res["status"] = "drift_check_failed"
                    return res
                if not dw.plausible:
                    if not p.get("allow_implausible"):
                        res["status"] = "not_saved_implausible"
                        return res
                    saved = dw.save(allow_implausible=True)
                    activate = False
                    res["status"] = "saved_implausible"
                elif not dw.checks_passed and not p.get("save_on_failed_checks"):
                    res["status"] = "not_saved_checks_failed"
                    return res
                else:
                    saved = dw.save()
                    res["status"] = "saved"
                res["profile_ids"] = [saved["id"]]
                if activate:
                    calprof.set_active_selection(dw.backend_name, saved["id"])
                    res["activated"] = saved["id"]
                self.rebuild_calibration()
                return res
            finally:
                res["warnings"] = list(dw.warnings)
                res["dark_drift"] = json_safe(dw.dark_drift)
                for w, key in (("known", "integration_known_us"),
                               ("new", "integration_new_us")):
                    if key not in res and dw.itime.get(w) is not None:
                        res[key] = int(dw.itime[w])
                # legacy field (one t_int before per-side times): the new optics'
                it = dw.integration_us
                res["integration_us"] = it.get("new") if isinstance(it, dict) else it
                if dw.result is not None or res.get("error"):
                    res["protocol"], res["protocol_message"] = \
                        self._write_derivation_protocol(dw)
                ctx.job.result = res
        return self.jobs.submit("derivation", run, p, owner)

    def _write_derivation_protocol(self, dw) -> tuple[list[str], str]:
        try:
            import calibration_report as crp
            import export_paths
            if not hasattr(crp, "write_derivation_protocol"):
                return [], "Protocol not written: derivation protocol not available."
            rec = dw.record()
            d, note = export_paths.calibration_protocol_dir()
            base = crp.default_derivation_basename(rec)
            paths = crp.write_derivation_protocol(rec, os.path.join(d, base + ".pdf"),
                                                  os.path.join(d, base + ".csv"))
            return list(paths or []), note or ""
        except Exception as e:                                   # noqa: BLE001
            return [], f"Protocol not written: {e}"

    def submit_sphere_characterization(self, params: dict, *, owner: str) -> Job:
        """Characterise an integrating sphere as a radiance reference with the
        CC and the active absolute irradiance profile (calibration_sphere).
        User steps (waiting_for_user, action): mount (CC at the port, smallest
        distance) -> integration time -> cover -> dark before -> uncover -> one
        'position' step per height -> 'repeat' (first distance again) -> cover
        -> dark after -> compute -> save -> protocol (PDF + CSV).

        Parameters: heights_mm (list, at least the configured minimum),
        integration_ms / integration_us (None / "auto" = auto exposure at the
        first distance), frames, sphere {name, serial, port_radius_mm,
        edge_depth_mm (11.0), cc_ref_offset_mm (0), u_radius_mm, u_distance_mm,
        cc_uncertainty_pct, notes}, save_on_failed_checks."""
        import calibration_sphere as cs_
        b = self._require_backend()
        p = dict(params or {})
        sc_cfg = cs_.SphereConfig.from_config()
        if p.get("frames"):
            sc_cfg.n_frames = max(2, int(p["frames"]))
        try:
            heights = [float(h) for h in (p.get("heights_mm") or [])]
        except (TypeError, ValueError):
            raise Unprocessable("heights_mm must be a list of numbers (mm).")
        if len(heights) < sc_cfg.min_positions:
            raise Unprocessable(f"At least {sc_cfg.min_positions} distances (heights_mm) "
                                f"are needed.")
        if len({round(h, 6) for h in heights}) != len(heights):
            raise Unprocessable("The distances (heights_mm) must all differ.")
        try:
            sc = cs_.SphereCharacterization(b, self.device_name, cfg=sc_cfg,
                                            sphere=dict(p.get("sphere") or {}))
            for h in heights:
                if h < -sc.sphere["edge_depth_mm"]:
                    raise ValueError("The CC cannot lie below the knife-edge plane.")
        except ValueError as e:
            raise Unprocessable(str(e))
        if p.get("integration_us") is not None:
            t_us = max(1, int(round(float(p["integration_us"]))))
        elif p.get("integration_ms") in (None, "auto"):
            t_us = None
        else:
            t_us = max(1, int(round(float(p["integration_ms"]) * 1000.0)))
        n = len(heights)

        def run(ctx: JobContext):
            res: dict = {"status": "failed", "protocol": [],
                         "source_hint": cs_.SPHERE_SOURCE_HINT,
                         "quantity": "radiance", "source_id": None}
            try:
                ctx.wait_for_user(
                    f"Mount the cosine corrector on the sphere port at the smallest "
                    f"distance (height {heights[0]:g} mm above the housing top). "
                    + cs_.SPHERE_SOURCE_HINT + " Then continue.", action="mount")
                ctx.progress(0.05, "integration time", step="integration")
                info = sc.set_integration(t_us, auto=t_us is None)
                res["integration_us"] = int(info["integration_us"])
                if sc.needs_user_cover:
                    ctx.wait_for_user("Block the light path / cover the port. The dark "
                                      "spectrum is captured next.", action="cover")
                ctx.check()
                ctx.progress(0.1, "dark spectrum (before)", step="dark_before")
                sc.capture_dark("before")
                self.calib.invalidate_darks()
                if sc.needs_user_cover:
                    ctx.wait_for_user("Release the light path / uncover the port. The "
                                      "first distance is captured next.", action="uncover")
                ctx.check()
                for i, h in enumerate(heights):
                    ctx.wait_for_user(f"Set the cosine corrector to the height {h:g} mm "
                                      f"(position {i + 1} of {n}), then continue.",
                                      action="position")
                    ctx.progress(0.15 + 0.5 * i / n, f"position {i + 1}/{n} ({h:g} mm)",
                                 step="position")
                    sc.capture_position(h)
                ctx.wait_for_user(f"Set the cosine corrector back to the first height "
                                  f"({heights[0]:g} mm) for the repeat (source drift), "
                                  f"then continue.", action="repeat")
                ctx.progress(0.7, "repeat of the first distance", step="repeat")
                sc.capture_repeat()
                if sc.needs_user_cover:
                    ctx.wait_for_user("Block the light path / cover the port again. The "
                                      "dark spectrum AFTER the measurements is taken "
                                      "next.", action="cover")
                ctx.check()
                ctx.progress(0.78, "dark spectrum (after)", step="dark_after")
                sc.capture_dark("after")
                self.calib.invalidate_darks()
                ctx.progress(0.85, "computing", step="compute")
                try:
                    r = sc.compute()
                except (ValueError, RuntimeError) as e:
                    res["error"] = str(e)
                    return res
                res.update(d0_mm=json_safe(r["d0_mm"]),
                           d0_plausible=bool(r["d0_plausible"]), method=r["method"],
                           consistency=json_safe({
                               "pass": r["consistency_pass"],
                               "max_pct": r["consistency_max_pct"],
                               "overall": r["overall"],
                               "bands": [{k: v for k, v in bd.items()
                                          if not isinstance(v, (list, np.ndarray))}
                                         for bd in r["bands"]]}),
                           drift=json_safe(sc.drift),
                           uncertainty=json_safe(r["uncertainty"]),
                           valid_range_nm=json_safe(r["valid_range_nm"]),
                           checks=json_safe(sc.checks))
                ctx.progress(0.92, "saving", step="save")
                if not sc.checks_passed and not p.get("save_on_failed_checks"):
                    res["status"] = "not_saved_checks_failed"
                    res["failed_checks"] = sc.failed_checks()
                    return res
                src = sc.save(allow_failed_checks=True)
                res["source_id"] = src.id
                res["status"] = "saved"
                if not sc.checks_passed:
                    res["saved_checks_failed"] = True
                    res["failed_checks"] = sc.failed_checks()
                self._overlay_cache = {}
                return res
            finally:
                res["warnings"] = list(sc.warnings)
                if sc.result is not None or res.get("error"):
                    try:
                        ctx.progress(0.96, "writing the protocol", step="protocol")
                    except JobCancelled:
                        pass
                    res["protocol"], res["protocol_message"] = \
                        self._write_sphere_protocol(sc)
                ctx.job.result = res
        return self.jobs.submit("sphere", run, p, owner)

    def _write_sphere_protocol(self, sc) -> tuple[list[str], str]:
        try:
            import calibration_report as crp
            import export_paths
            if not hasattr(crp, "write_sphere_protocol"):
                return [], "Protocol not written: sphere protocol not available."
            rec = sc.record()
            d, note = export_paths.calibration_protocol_dir()
            base = crp.default_sphere_basename(rec)
            paths = crp.write_sphere_protocol(rec, os.path.join(d, base + ".pdf"),
                                              os.path.join(d, base + ".csv"))
            return list(paths or []), note or ""
        except Exception as e:                                   # noqa: BLE001
            return [], f"Protocol not written: {e}"

    def _write_protocol(self, wf) -> tuple[list[str], str]:
        try:
            import calibration_report as crp
            import export_paths
            rec = wf.record()
            d, note = export_paths.calibration_protocol_dir()
            base = crp.default_protocol_basename(rec)
            paths = crp.write_protocol(rec, os.path.join(d, base + ".pdf"),
                                       os.path.join(d, base + ".csv"))
            return list(paths or []), note or ""
        except Exception as e:                                   # noqa: BLE001
            return [], f"Protocol not written: {e}"


# ══════════════════════════════════════════════════════════════════════════
#  Export helpers (shared by API / GUI)
# ══════════════════════════════════════════════════════════════════════════

def measurement_to_json(rec: dict, *, include_arrays: bool = True) -> dict:
    out = {k: v for k, v in rec.items() if k not in ("wavelengths", "values", "raw_mean")}
    if include_arrays:
        out["wavelengths"] = rec["wavelengths"]
        out["values"] = rec["values"]
    return json_safe(out)


def measurement_to_csv(rec: dict) -> str:
    buf = io.StringIO()
    acq, dev, cal = rec["acquisition"], rec["device"], rec["calibration"]
    lin = dev.get("linearity") or {}
    nl = dev.get("nl_correction")
    meta = [("Measurement id", rec["id"]), ("Timestamp", rec["timestamp"]),
            ("Label", rec.get("label", "")),
            ("Spectrometer", dev.get("backend")), ("Serial", dev.get("serial")),
            ("Integration time (ms)", f"{acq['integration_ms']:.3f}"),
            ("Frames averaged", acq["frames"]), ("Dark", f"{acq['dark']} ({acq['dark_source']})"),
            ("NL correction (device)", {True: "on", False: "off"}.get(nl, "n/a")),
            ("Linearity correction", callin.onoff_na(lin) + (
                f" ({lin.get('id')})" if lin.get("active") else "")),
            ("Peak (fraction of full scale)", f"{acq['peak_frac']:.4f}"),
            ("Saturated pixels", acq["n_saturated"]),
            ("Calibration profile", cal.get("label")),
            ("Profile id", cal.get("profile_id") or ""), ("Profile type", cal.get("type") or ""),
            ("Unit", rec["unit"])]
    if processing.is_absolute_unit(rec["unit"]):
        meta.append(("Quantity", processing.unit_quantity(rec["unit"])))
    vr = rec.get("valid_range_nm")
    if vr:
        meta.append(("Valid range (nm)", f"{float(vr[0]):.1f}-{float(vr[1]):.1f}"))
        segs = cal.get("valid_segments_nm")
        if segs:
            meta.append(("Valid segments (nm)", _segments_text(segs)))
        meta.append(("Masked pixels", MASKED_NOTE))
    for k, v in meta:
        buf.write(f"# {k};{'' if v is None else str(v).replace(';', ',')}\n")
    unit = rec["unit"]
    col = "ADC_Counts" if unit == "counts" else f"Value_{unit}"
    buf.write(f"Pixel_ID;Wavelength_nm;{col}\n")
    for i, (w, v) in enumerate(zip(rec["wavelengths"], rec["values"])):
        if not np.isfinite(v):
            buf.write(f"{i};{w:.3f};\n")
        elif unit == "counts":
            buf.write(f"{i};{w:.3f};{v:.2f}\n")
        else:
            buf.write(f"{i};{w:.3f};{v:.6g}\n")
    return buf.getvalue()


# ══════════════════════════════════════════════════════════════════════════
#  Spectrum / heatmap / measurement export (ONE implementation, desktop + web)
# ══════════════════════════════════════════════════════════════════════════
#
#  CSV: ';' separated, UTF-8 with BOM (Excel recognises the encoding; µ, ², –
#  survive), header lines '# key;value' documenting the settings ACTUALLY
#  applied to the exported data. Masked pixels (outside the valid segments of
#  a profile) are written as empty cells.

CSV_ENCODING = "utf-8-sig"
CSV_MEDIA_TYPE = "text/csv; charset=utf-8"
MASKED_NOTE = ("empty cell = masked pixel (outside the valid segments of the "
               "calibration profile)")


def csv_bytes(text: str) -> bytes:
    """CSV text → bytes with UTF-8 BOM (HTTP responses)."""
    return text.encode(CSV_ENCODING)


def write_csv_text(path: str, text: str) -> str:
    """Write CSV text with UTF-8 BOM (desktop saves)."""
    with open(path, "w", encoding=CSV_ENCODING, newline="") as f:
        f.write(text)
    return path


def _segments_text(segs) -> str:
    if not segs:
        return ""
    return ", ".join(f"{float(a):.1f}–{float(b):.1f}" for a, b in segs)


def _onoff(v) -> str:
    return "on" if v else "off"


def _iso_from_epoch(ts: float | None) -> str:
    if ts is None:
        return now_iso()
    return datetime.fromtimestamp(float(ts)).astimezone().isoformat(timespec="seconds")


def _value_column(unit: str) -> str:
    return "ADC_Counts" if unit == "counts" else f"Value_{unit}"


def _fmt_value(v: float, unit: str) -> str:
    if not np.isfinite(v):
        return ""
    if unit in ("counts", processing_unit_counts_corrected()):
        return f"{v:.2f}"
    return f"{v:.6g}"


def processing_unit_counts_corrected() -> str:
    return getattr(processing, "UNIT_COUNTS_CORRECTED", "counts_corrected")


def spectrum_csv(snapshot: dict) -> str:
    """CSV text of an export snapshot (Engine.export_snapshot)."""
    buf = io.StringIO()
    for k, v in snapshot["meta"]:
        v = "" if v is None else str(v)
        buf.write(f"# {k};{v.replace(';', ',')}\n")
    wl = np.asarray(snapshot["wavelengths"], float)
    vals = np.asarray(snapshot["values"], float)
    unit = snapshot["unit"] or "counts"
    cols = [("Pixel_ID", None), ("Wavelength_nm", None), (_value_column(unit), (vals, unit))]
    disp = snapshot.get("display")
    dunit = snapshot.get("display_unit")
    if disp is not None and dunit and (dunit != unit or not np.array_equal(
            np.asarray(disp, float), vals, equal_nan=True)):
        cols.append((f"Display_{dunit}", (np.asarray(disp, float), dunit)))
    raw = snapshot.get("raw")
    if raw is not None and snapshot.get("include_raw", True):
        cols.append(("Raw_ADC", (np.asarray(raw, float), "counts")))
    buf.write(";".join(c for c, _ in cols) + "\n")
    for i, w in enumerate(wl):
        row = [str(i), f"{w:.3f}"]
        for _c, spec in cols[2:]:
            arr, u = spec
            row.append(_fmt_value(float(arr[i]), u) if i < arr.size else "")
        buf.write(";".join(row) + "\n")
    return buf.getvalue()


def heatmap_csv_text(h: dict, indices=None, meta: list | None = None) -> str:
    """CSV of the time-lapse buffer (heatmap_snapshot()); indices = frame
    columns (0 = newest), default all filled."""
    buf = io.StringIO()
    for k, v in meta or []:
        buf.write(f"# {k};{'' if v is None else str(v).replace(';', ',')}\n")
    idx = list(range(int(h["filled"]))) if indices is None else list(indices)
    buf.write(";".join(["Time_Frame"] + [f"{w:.2f}" for w in h["wavelengths"]]) + "\n")
    valid = h.get("valid")
    for fi in idx:
        col = h["buffer"][:, fi]
        ok = valid[:, fi] if valid is not None else np.ones(col.size, bool)
        buf.write(";".join([str(fi)] + [("" if not (g and np.isfinite(v)) else f"{v:.6g}")
                                        for v, g in zip(col, ok)]) + "\n")
    return buf.getvalue()

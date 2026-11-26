"""
api_models.py — Pydantic models of the /api/v1 automation API.

Conventions
* All bodies/answers are JSON; wavelengths in nm, times in ms unless the field
  name says _us / _s; timestamps ISO 8601 with UTC offset.
* Errors: HTTP 400/401/404/409/422/503 with
      {"error": {"code": str, "message": str, "details": any}}
  codes: bad_request, unauthorized, not_found, control_busy,
  needs_confirmation, unprocessable, validation_error, not_connected,
  unavailable, internal_error.
* Steering endpoints need `Authorization: Bearer <token>` (when token
  protection is active). Optional header `X-Client-Id` names the caller; the
  control owner is then "remote:<X-Client-Id>" (default "remote:api").
"""
from __future__ import annotations

from typing import Any, Literal, Optional, Union

from pydantic import BaseModel, ConfigDict, Field


class _M(BaseModel):
    model_config = ConfigDict(extra="forbid")


class _Open(BaseModel):
    model_config = ConfigDict(extra="allow")


# ── errors ────────────────────────────────────────────────────────────────
class ErrorBody(_M):
    code: str
    message: str
    details: Any = None


class ErrorResponse(_M):
    error: ErrorBody


ERROR_RESPONSES = {
    400: {"model": ErrorResponse, "description": "Bad request"},
    401: {"model": ErrorResponse, "description": "Missing or invalid token"},
    404: {"model": ErrorResponse, "description": "Not found"},
    409: {"model": ErrorResponse, "description": "Control held by another client / confirmation needed"},
    422: {"model": ErrorResponse, "description": "Validation error / not processable"},
    503: {"model": ErrorResponse, "description": "No device connected / optional component missing"},
}


# ── system ────────────────────────────────────────────────────────────────
class ControlInfo(_M):
    owner: Optional[str] = None
    reason: str = ""
    since: Optional[str] = None
    job_id: Optional[str] = None
    lease: Optional[str] = None


class JobBrief(_Open):
    id: str
    kind: str
    owner: str
    status: Literal["queued", "running", "waiting_for_user", "done", "failed", "cancelled"]
    progress: float
    step: str = ""
    message: str = ""
    created: str
    started: Optional[str] = None
    finished: Optional[str] = None
    prompt: Optional[dict] = None
    error: Optional[dict] = None


class Job(JobBrief):
    params: dict = {}
    result: Any = None
    log: list = []


class StatusResponse(_Open):
    """Engine.status(): device, connection, t_int, averaging, profile, unit,
    control, running jobs …"""
    connected: bool
    backend: Optional[str] = None
    device_path: Optional[str] = None
    serial: Optional[str] = None
    integration_us: int
    exposure_ms: float
    auto_exposure: bool
    frames_to_average: int
    unit: str
    calibration: dict
    control: ControlInfo
    jobs: list[JobBrief]
    api_version: str = "1"
    token_required: bool = False


class BackendsResponse(_M):
    backends: list[str]
    selected: str


class DevicesResponse(_M):
    backend: str
    devices: list[str]


# ── device / acquisition ─────────────────────────────────────────────────
class ConnectRequest(_M):
    backend: Optional[str] = Field(None, description="Backend name (GET /backends); default: selected")
    device_path: Optional[str] = Field(None, description="Device id (GET /devices); default: first found")


class ConnectionResponse(_M):
    connected: bool
    backend: Optional[str] = None
    device_path: Optional[str] = None
    serial: Optional[str] = None


class AcquisitionSettings(_M):
    """PUT /acquisition — only the given fields change."""
    exposure_ms: Optional[float] = Field(None, gt=0, description="Integration time; clamped to the device")
    auto_exposure: Optional[bool] = Field(None, description="Continuous auto exposure of the live view")
    frames_to_average: Optional[int] = Field(None, ge=1, le=1000)
    paused: Optional[bool] = None
    fast_preview_enabled: Optional[bool] = None
    fast_preview_short_pct: Optional[int] = Field(None, ge=1, le=50)
    dark_correction: Optional[bool] = None
    dark_mode: Optional[Literal["optical_black", "parametric", "stored"]] = None
    dark_bias_adc: Optional[float] = None
    dark_rate_adc_per_s: Optional[float] = None
    hot_pixel_filter: Optional[bool] = None
    smoothing_width: Optional[int] = Field(None, ge=3, le=101)
    spatial_smoothing: Optional[bool] = None
    spatial_smoothing_width: Optional[int] = Field(None, ge=3, le=51)
    display_unit: Optional[Literal["counts", "counts_per_s"]] = None
    display_mode: Optional[Literal["auto", "raw", "corrected", "normalised", "count_rate",
                                   "absolute"]] = Field(
        None, description="Display mode of scope/heatmap (only what is plotted; "
                          "evaluations and exports always use the corrected data)")
    irradiance_unit: Optional[Literal["uW/cm2/nm", "W/m2/nm", "mW/m2/nm"]] = None
    fusion_enabled: Optional[bool] = None
    fusion_smoothing: Optional[float] = Field(None, ge=0, le=0.99)
    show_peaks: Optional[bool] = None
    heatmap_buffer_size: Optional[int] = Field(None, ge=10, le=100000)
    heatmap_measure_interval_ms: Optional[int] = Field(None, ge=0)


class AcquisitionResponse(_Open):
    applied: dict
    status: StatusResponse


class AutoExposureRequest(_M):
    target_frac_lo: float = Field(0.70, gt=0, lt=1)
    target_frac_hi: float = Field(0.85, gt=0, le=1)
    apply: bool = Field(True, description="Keep the found t_int as live exposure")


class AutoExposureResponse(_M):
    integration_us: int
    exposure_ms: float
    converged: bool
    peak_frac: Optional[float] = None


class ControlRequest(_M):
    action: Literal["acquire", "release"]
    reason: str = ""
    force: bool = False


# ── measurement ──────────────────────────────────────────────────────────
DarkMode = Literal["auto", "stored", "capture", "none"]


class MeasureRequest(_M):
    integration_ms: Optional[Union[float, Literal["auto"]]] = Field(
        None, description="Fixed t_int in ms, 'auto' (auto exposure to 70–85 % FS) "
                          "or null (current live t_int)")
    frames: int = Field(1, ge=1, le=1000, description="Frames averaged (frame taps, raw)")
    dark: DarkMode = Field("auto", description=(
        "auto = like the live view; stored = stored per-pixel dark at this t_int "
        "(422 if missing); capture = capture a new dark first (user step "
        "'cover' unless the device has a shutter); none = no dark subtraction "
        "(raw counts, no calibration profile)"))
    profile: Optional[str] = Field(None, description="Calibration selection to use (profile id, "
                                                     "'None', 'Device default'); restored afterwards")
    confirm_profile: bool = False
    include_color: bool = True
    include_peaks: bool = True
    label: str = ""
    wait: bool = Field(True, description="Wait for the result (else 202 + job)")
    timeout_s: float = Field(120.0, gt=0, le=3600)


class SeriesRequest(MeasureRequest):
    count: int = Field(..., ge=1, le=100000)
    interval_s: float = Field(0.0, ge=0, description="Start-to-start interval")
    wait: bool = False


class DarkRequest(_M):
    integration_ms: Optional[float] = Field(None, gt=0, description="Default: current t_int")
    frames: Optional[int] = Field(None, ge=2, le=1000)
    wait: bool = False
    timeout_s: float = Field(120.0, gt=0, le=3600)


class Peak(_Open):
    index: int
    wavelength_nm: float
    intensity_adc: float


class Measurement(_Open):
    id: str
    timestamp: str
    label: str = ""
    device: dict
    acquisition: dict
    calibration: dict
    unit: str
    unit_label: str
    valid_range_nm: Optional[list[float]] = None
    wavelengths: Optional[list[float]] = None
    values: Optional[list[Optional[float]]] = None
    peaks: list = []
    color: Optional[dict] = None
    warnings: list[str] = []


class MeasurementSummary(_Open):
    id: str
    timestamp: str
    label: str = ""
    unit: str
    integration_ms: float
    frames: int
    peak_frac: float


class JobAccepted(_M):
    job: JobBrief
    location: str


class MeasureResponse(_Open):
    """Result (wait=true, finished) or the job (still running / waiting)."""
    job: JobBrief
    measurement: Optional[Measurement] = None


# ── data ─────────────────────────────────────────────────────────────────
class SpectrumResponse(_Open):
    seq: int
    timestamp: Optional[str] = None
    unit: str
    unit_label: str
    integration_us: int
    fps: float
    n_points: int
    downsample: int
    wavelengths: list[float]
    values: list[Optional[float]]
    valid_range_nm: Optional[list[float]] = None
    peaks: list = []
    calibration: dict = {}


# ── analysis ─────────────────────────────────────────────────────────────
class SpectrumInput(_M):
    """Either a stored measurement id, the latest live spectrum, or explicit data."""
    measurement_id: Optional[str] = None
    wavelengths: Optional[list[float]] = None
    values: Optional[list[Optional[float]]] = None
    unit: Optional[str] = Field(None, description="Unit of explicit values (default counts)")


class ColorRequest(SpectrumInput):
    lux_calibration: Optional[float] = Field(None, description="Override the lux factor")


class PeaksRequest(SpectrumInput):
    savgol_window: int = Field(11, ge=5)
    savgol_order: int = Field(3, ge=1)
    min_prominence: float = 50.0
    min_distance_px: int = 40
    min_height: float = 15.0
    max_count: int = Field(10, ge=1, le=200)


class FilterRequest(_M):
    reference_id: Optional[str] = Field(None, description="Measurement id of the 100 % reference")
    sample_id: Optional[str] = Field(None, description="Measurement id of the sample")
    wavelengths: Optional[list[float]] = None
    reference: Optional[list[float]] = None
    sample: Optional[list[float]] = None


# ── calibration ──────────────────────────────────────────────────────────
class SelectProfileRequest(_M):
    selection: str = Field(..., description="profile id, 'None' or 'Device default'")
    confirm: bool = False


class CopyProfileRequest(_M):
    id: str


class OpticsSpec(_M):
    name: str = Field(..., min_length=1)
    input: Literal["CC", "sphere", "bare"]
    notes: str = ""


class CalibrationRunRequest(_M):
    kind: Literal["relative", "absolute"] = "relative"
    reference_id: str
    optics: OpticsSpec
    integration_ms: Optional[Union[float, Literal["auto"]]] = "auto"
    frames: Optional[int] = Field(None, ge=2, le=1000)
    allow_implausible: bool = Field(False, description="Save a result that failed the "
                                    "plausibility check (flagged, not activated)")
    save_on_failed_checks: bool = Field(False, description="Save although the stability / "
                                        "t_int cross check failed")
    activate: bool = True
    verify: bool = Field(True, description="After saving: measure the reference source "
                                           "with the new profile through the live pipeline "
                                           "and report the deviation per 50-nm band")


class VerificationRequest(_M):
    profile_id: str
    reference_id: str
    integration_ms: Optional[float] = Field(None, gt=0, description="Default: t_int of the profile")
    frames: Optional[int] = Field(None, ge=1, le=1000)


class LinearityRunRequest(_M):
    integration_ms: Optional[Union[float, Literal["auto"]]] = Field(
        "auto", description="Probe exposure; 'auto' = auto exposure to ~50 % FS")
    steps: Optional[int] = Field(None, ge=6, le=10, description="Number of integration times")
    frames: Optional[int] = Field(None, ge=2, le=1000)
    save: bool = True
    activate: bool = Field(True, description="Activate the new correction (not for check "
                                             "measurements on devices with factory linearisation)")


class LinearitySelectRequest(_M):
    entry_id: Optional[str] = Field(None, description="Linearity record to activate; null = off")


# ── reports ──────────────────────────────────────────────────────────────
class ReportRequest(_M):
    kind: Literal["color", "filter", "measurement"] = "color"
    format: Literal["pdf", "png", "csv"] = "pdf"
    measurement_id: Optional[str] = None
    reference_id: Optional[str] = None
    sample_id: Optional[str] = None
    language: Literal["de", "en"] = "de"
    save: bool = Field(False, description="Also write the file to the export folder; "
                                          "answer then contains the path")

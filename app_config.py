"""
JSON-backed user preferences.

The file is created with DEFAULT_CONFIG values if it doesn't exist.
Use Config.get('key') / Config.set('key', value) — set() persists
automatically. Unknown keys are tolerated (so adding new options is
non-destructive for existing user files).
"""

from __future__ import annotations
import json
import os
from typing import Any


CONFIG_FILENAME = "spectrometer_config.json"


DEFAULT_CONFIG: dict[str, Any] = {
    "config_version": 2,
    # Appearance
    "theme": "dark",                         # "dark" or "light"

    # Acquisition
    "exposure_ms": 20.0,
    "frames_to_average": 1,
    "auto_exposure": False,
    "reference_library": "None",

    # Display
    "x_min_nm": 380,
    "x_max_nm": 1050,
    "auto_y": True,
    "y_max_adc": 4000,
    "show_peaks": False,
    "measure_mode": False,

    # Processing
    "dark_correction": False,
    "dark_mode": "optical_black",            # or "parametric"
    "dark_bias_adc": 61.57,
    "dark_rate_adc_per_s": 40.94,
    "hot_pixel_filter": False,
    "smoothing_width": 5,
    # Spatial (pixel-window) smoothing: moving-average along the wavelength
    # axis to visually de-noise the trace (incl. fixed-pattern structure the
    # temporal filter cannot remove). Separate from the despeckle median above.
    # Wider window = smoother but broader/lower peaks; keep below peak FWHM.
    "spatial_smoothing": False,
    "spatial_smoothing_width": 5,            # odd, 3–51
    "spectral_response_compensation": False,

    # ── Calibration profiles (calibration_profiles.py) ────────────────────
    # Per-device response-correction selection, set by the GUI dropdown:
    #   {device_name: "None" | "Device default" | "<profile name>"}.
    # "None" => no correction (gain = ones, behaviour unchanged). The legacy
    # boolean spectral_response_compensation above is kept in sync automatically
    # (None -> off, anything else -> on) so processing.py needs no awareness.
    "response_correction_active": {},
    # Saved [lambda, sensitivity] tables live in this separate JSON registry
    # (large arrays — kept out of the main config).
    "calibration_profiles_file": "calibration_profiles.json",
    # Frames averaged into one calibration capture (wavelength peak finding and
    # response measurement). A response correction divides two measured spectra,
    # so noise propagates in quadrature and blows up at weak band edges where
    # gain = 1/S amplifies it — average like the filter baseline (noise ~/sqrt N).
    # Only genuinely new frames are counted, so this is sqrt(N) regardless of the
    # live frame rate.
    "calibration_capture_frames": 16,

    # ── Spectral calibration v2 (relative / absolute, see docs/PLAN_...) ───
    # Library of imported calibrated lamps (reference_sources.py).
    "reference_sources_file": "reference_sources.json",
    # Per-pixel dark spectra, per device (backend + serial) and integration
    # time (calibration_capture.py). Used by the calibration wizard and by the
    # "Stored dark" dark mode of the main pipeline.
    "dark_spectra_file": "dark_spectra.json",
    # SNR criterion for the valid range: C_cal > k * sigma_dark (per pixel).
    "calibration_snr_k": 10.0,
    # Pixels whose gain (1/S, peak-normalised) would exceed this are masked.
    "calibration_max_gain": 50.0,
    # Optional moving-average smoothing of S / K (pixels; 0/1 = off).
    "calibration_smooth_px": 5,
    # Auto-exposure target for the calibration capture (fraction of full scale).
    "calibration_target_frac_lo": 0.70,
    "calibration_target_frac_hi": 0.85,
    # Automatic checks (wizard): two captures this many seconds apart, and a
    # second capture at ~half t_int with its own dark.
    "calibration_stability_delay_s": 5.0,
    "calibration_stability_threshold_pct": 1.0,
    "calibration_itime_threshold_pct": 2.0,
    # Plausibility (broadband) check before a profile is saved
    # (calibration_profiles.broadband_check). A failed check blocks saving
    # unless the user confirms explicitly; such a profile is flagged and not
    # activated automatically.
    "calibration_min_coverage": 0.5,          # valid / (device ∩ reference) pixels
    # max/min gain over the valid range — reported as a HINT only (a halogen
    # lamp on a Si detector spans 50–100x between 380 and 950 nm); line
    # sources are detected via coverage, roughness and isolated peaks.
    "calibration_max_gain_span": 200.0,
    "calibration_max_roughness": 0.05,        # p95 |S/S_smoothed - 1|
    "calibration_max_isolated_frac": 0.2,     # share of valid pixels in isolated peaks
    "calibration_min_segment_nm": 5.0,        # narrower valid runs count as isolated
    "calibration_max_gap_nm": 2.0,            # masked gaps up to this width are bridged
    "calibration_min_largest_segment_frac": 0.5,
    "calibration_roughness_px": 21,           # smoothing window of the roughness metric
    # Per-pixel stability / t/2 cross check: a pixel fails when its deviation
    # exceeds k·σ (dark + shot noise of the captures) + the check threshold.
    # Failing pixels are masked and limit the valid segments.
    "calibration_pixel_check_k": 4.0,
    # Cause model of the checks (dark drift / time offset or lamp drift /
    # nonlinearity): a term is reported when its effect reaches this (%).
    "calibration_cause_min_pct": 0.3,
    # Dark before/after each light capture: warn when the dark level changed
    # by more than this share of the net peak signal (%) (and > 3 σ).
    "calibration_dark_drift_warn_pct": 0.5,
    # Dark and light count as "same integration time" within this tolerance (%).
    "calibration_itime_match_tol_pct": 0.5,
    # "Dark unusually bright" warning: dark peak fraction > this factor times
    # the light peak fraction (light probably not blocked during the dark).
    "calibration_dark_bright_frac": 0.5,
    # Derivation of an optics configuration from a known one: the known optics
    # is measured again at the end; a source drift (p95 over the tested
    # pixels, %) above this fails the drift check, failing pixels are masked.
    "calibration_derivation_drift_pct": 2.0,
    # Sphere as a radiance reference (calibration_sphere.py): number of
    # distances (>= 3), band width of the evaluation, consistency limit of L
    # over the distances (%, per band and overall), plausible |d0| of the fit
    # (mm), drift limit of the repeat of the first distance (p95, %), default
    # relative standard uncertainty of the CC calibration (%, k = 1).
    "sphere_min_positions": 3,
    "sphere_band_nm": 50.0,
    "sphere_consistency_max_pct": 2.0,
    "sphere_d0_max_mm": 1.0,
    "sphere_drift_max_pct": 1.0,
    "sphere_cc_uncertainty_pct": 3.0,
    # Saturation warning threshold (fraction of full scale, raw ADC peak):
    # exposure meter (desktop + web) and calibration runs. The auto-exposure
    # target band (calibration_target_frac_hi) is capped at this value.
    "saturation_warn_frac": 0.90,
    # Verification after saving a profile: reference source measured through
    # the live pipeline, normalised over this range, deviation per band.
    "calibration_verify_norm_lo_nm": 550.0,
    "calibration_verify_norm_hi_nm": 650.0,
    "calibration_verify_band_nm": 50.0,
    "calibration_verify_warn_pct": 2.0,
    # Reference curve overlay in the scope (reference source id, on/off).
    "scope_reference_id": "",
    "scope_reference_overlay": False,

    # ── Linearity calibration (calibration_linearity.py) ──────────────────
    # Records + active correction per unit (backend + serial).
    "linearity_profiles_file": "linearity_profiles.json",
    "linearity_steps": 8,                     # 6–10 integration times
    "linearity_frac_lo": 0.10,                # raw peak of the shortest step (FS)
    "linearity_frac_hi": 0.90,                # longest step (capped below saturation)
    "linearity_ref_frac": 0.50,               # repeated reference step (lamp drift)
    "linearity_ref_every": 3,
    "linearity_capture_frames": 16,
    "linearity_degree": 3,
    "linearity_warn_residual_pct": 0.3,
    "linearity_warn_lamp_drift_pct": 1.0,
    # Calibration dialog geometry [x, y, w, h] (clamped to the current screen).
    "calibration_dialog_geometry": None,
    "main_window_geometry": None,
    # Display mode (Darstellungsart) of scope + heatmap: auto | raw | corrected
    # | normalised | count_rate | absolute (processing.display_transform).
    # auto = corrected with an active profile, else raw (or count_rate with
    # display_unit counts_per_s). Evaluations always use the corrected data.
    "display_mode": "auto",
    # Manual y-axis limits per display mode {mode: {"auto": bool, "min": x,
    # "max": y}} (scope) and {mode: {"auto": bool, "max": y}} (heatmap).
    "y_limits_by_mode": {},
    "heatmap_limits_by_mode": {},
    # Display unit of the main plot when NO v2 profile is active:
    # "counts" (unchanged legacy behaviour) or "counts_per_s".
    "display_unit": "counts",
    # Irradiance unit for absolute profiles: "uW/cm2/nm" or "W/m2/nm".
    "irradiance_unit": "uW/cm2/nm",
    # {profile_id: wl_fingerprint} — profiles the user explicitly chose to apply
    # although the wavelength calibration changed since they were made.
    "calibration_fingerprint_overrides": {},
    # Persisted degree-3 wavelength polynomials from the wavelength wizard.
    # null = use the device's factory/default axis. Pixel -> lambda:
    #   lambda(i) = C0 + C1*i + C2*i**2 + C3*i**3.
    "pasco_wl_poly_coeffs": None,
    "lr2t_wl_poly_coeffs": None,
    "ocean_wl_poly_coeffs": None,

    # Absolute radiometric calibration factor: W/m²/nm per ADC count.
    # 0.0 means uncalibrated — lux and foot-candle displays will show "—".
    # Derive this by measuring a lamp with known spectral irradiance at a
    # known distance and fitting the scale factor to match the reference.
    "radiometric_calibration_factor": 0.0,

    # ASEQ / Lasertrack LR-2T wavelength fine-tune offset (nm).
    # Positive values shift the displayed spectrum toward red.
    # Measure against known lines (e.g. Ne 640.2 nm) and adjust.
    "lr2t_wl_offset_nm": 0.0,

    # ── Time-lapse heatmap ────────────────────────────────────────────────
    "heatmap_buffer_size": 100,          # rolling history length (frames), step 10
    "heatmap_measure_interval_ms": 0,    # 0 = every frame; else log one frame per N ms
    "heatmap_autosave_enabled": False,
    "heatmap_autosave_dir": "",          # "" = the export folder
    "heatmap_autosave_fsync_s": 20,      # periodic fsync interval (crash safety)

    # ── Central export location + filename composition (all tabs/saves) ──
    "export_dir": "",                    # "" = <Documents>/PS-2600A (never the program folder)
    "export_filename_template": "{kind}_{datetime}",

    # ── Out-of-spec shading (per device) ──────────────────────────────────
    # Wavelength range (nm) the manufacturer actually guarantees. The sensor
    # still returns data outside it; the GUI and web UI shade those edges in
    # translucent red. null (either bound) → that side is not shaded. Set these
    # to your datasheet's guaranteed range. Demo has a built-in sample band.
    "lr2t_spec_min_nm": None,
    "lr2t_spec_max_nm": None,
    "ocean_spec_min_nm": None,
    "ocean_spec_max_nm": None,
    "pasco_spec_min_nm": None,
    "pasco_spec_max_nm": None,
    "demo_spec_min_nm": 380.0,
    "demo_spec_max_nm": 780.0,

    # ASEQ pixel order: False = device sends UV first (no flip needed).
    # Set to True if the spectrum appears mirrored after connecting.
    # ASQ_SPC5636146 confirmed: False is correct.
    "lr2t_flip_pixels": False,

    # ── Ocean Optics HDX-UV-VIS (_device_ocean.py) ────────────────────────
    # The HDX returns ~2068 pixels in raw mode. Wavelength + nonlinearity
    # coefficients are read from the device at connect time; these keys are
    # tunables layered on top of that hardware calibration.
    "ocean_pixel_count": 2068,               # default; auto-updated from device
    "ocean_use_device_wavelength": True,     # use device WL coeffs vs fallback
    "ocean_wl_offset_nm": 0.0,               # fine-tune offset (nm), like LR-2T
    "ocean_wl_fallback_min_nm": 200.0,       # linear axis if WL read fails
    "ocean_wl_fallback_max_nm": 1100.0,
    "ocean_nonlinearity_correction": True,   # apply device NL polynomial
    "ocean_dark_estimate_pixels": 50,        # median of N darkest px = dark level
    "ocean_adc_saturation": 60000,           # 16-bit; AE emergency-drop threshold
    "ocean_ae_target_adc": 50000,            # auto-exposure target peak ADC
    "ocean_ae_deadzone": 2000,               # AE no-change band around target
    "ocean_trigger_mode": 0,                 # 0 = free-running
    "ocean_min_integration_us": 6000,        # HDX hardware minimum (6 ms)
    "ocean_max_integration_us": 10000000,    # 10 s
    "lr2t_min_integration_us": 1000,         # ASEQ LR-2T minimum (1 ms)
    "lr2t_max_integration_us": 10000000,     # 10 s
    "lr2t_discard_frames_after_itime": 1,    # drop N stale frames after itime change (AE guard)
    "demo_min_integration_us": 1000,         # virtual demo device: 1 ms
    "demo_max_integration_us": 1000000,      # virtual demo device: 1000 ms
    "demo_fullscale_ms": 1000.0,             # exposure where peak ≈ full scale (lower → saturates earlier)
    "demo_spectrum": "led_white",            # built-in (led_white/led_cool/tungsten/flat) or a reference_spectra.csv column name
    "demo_dark_bias": 500.0,                 # fixed dark offset (counts)
    "demo_dark_current": 1.0,                # dark current (counts per ms exposure)
    "demo_soft_knee": 0.85,                  # soft-saturation knee (fraction of full scale; 1.0 = hard clip)
    "demo_read_noise": 12.0,                 # demo read-noise sigma (counts)
    "demo_shot_noise": 1.0,                  # demo shot-noise scale (×sqrt(signal))
    "demo_serial": "DEMO-0001",              # virtual serial (change to emulate a 2nd unit)
    "demo_sensitivity": "none",              # "none" (flat, unchanged) | "synthetic" (known S(λ) for calibration tests)
    "demo_nonlinearity": 0.0,                # test effect: response n·(1 + β·n/FS); 0 = linear
    "demo_dark_drift": 0.0,                  # test effect: dark offset drift (counts per second)

    # ── Filter tab ──
    "filter_baseline_frames": 16,            # frames averaged into the 100% reference on "Set baseline" (1 = single frame)
    "filter_display_smoothing": 0,           # live T(λ) curve smoothing window in points (0/1 = off); metrics stay on raw data
    "ocean_spectrum_command": "metadata",    # metadata | raw_hdx | buffered | raw_legacy
    "ocean_usb_timeout_ms": 15000,           # generous for spectrum transfers
    "ocean_flush_timeout_ms": 50,            # IN-pipe drain read timeout
    "ocean_loop_sleep_factor": 0.5,          # loop sleep = factor × integration
    "ocean_loop_sleep_min_s": 0.005,
    "ocean_loop_sleep_max_s": 0.20,
    "ocean_fp_loop_sleep_s": 0.001,          # FP throttle: small fixed yield (no t_short scaling)
    "ocean_discard_frames_after_itime": 1,   # drop N stale frames after itime change
    "ocean_fp_long_refresh_s": 3.0,          # HDX fast-preview: long-reference refresh interval (s)
    # Optional spectral response table: list of [wavelength_nm, sensitivity]
    # pairs (sensitivity normalised to 1.0 at peak). null = no response
    # correction. Populate from sensitivity_calibration.py to enable the GUI's
    # response-compensation control for the HDX.
    "ocean_response_table": None,

    # Fast Preview mode (simple).
    # Shortens the hardware exposure to (short_pct / 100) of the main
    # integration time, reads frames back-to-back as fast as the device allows,
    # and scales the net signal (above dark) back up by t_long / t_short so the
    # trace sits at the normal-exposure level. No long/short interleave.
    #   short_time = long_time × (short_pct / 100), clamped to the device min.
    "fast_preview_enabled":   False,
    "fast_preview_short_pct": 10,     # short frame = N% of the main exposure
    "fast_preview_n_short":   4,      # DEPRECATED no-op (no interleave); kept for compat

    # ── Display smoothing (formerly "fusion") ─────────────────────────────
    # Optional device-agnostic temporal noise softening for the live trace.
    # Adaptive per-pixel: heavy smoothing on stable pixels, instant backoff
    # where the signal changes (peaks never blurred, never mixed across
    # wavelength). Independent of Fast Preview. All logic lives in fusion.py.
    "fusion_enabled":      False,
    "fusion_smoothing":    0.85,   # 0..1 temporal smoothing on stable pixels
    "fusion_change_sigma": 4.0,    # deviation (σ) at which smoothing backs off
    "fusion_noise_floor":  8.0,    # read-noise term (ADC) in the noise model
    "fusion_noise_gain":   1.0,    # shot-noise coefficient (ADC per √signal)
    "fusion_emit_hz":      0.0,    # output rate; 0 = auto from frame cadence
    "fusion_emit_max_hz":  60.0,   # cap for auto mode
    "fusion_emit_min_hz":  2.0,    # floor for auto mode

    # Peak detection (new — Savitzky-Golay + prominence)
    "peak_savgol_window": 11,                # odd, >= 5
    "peak_savgol_order": 3,
    "peak_prominence": 50.0,                 # ADC counts
    "peak_min_height": 15.0,                 # ADC counts
    "peak_min_distance_px": 40,
    "peak_max_count": 3,

    # ── Remote access (REST API / web UI / MCP, docs/PLAN_api_mcp.md) ─────
    # Desktop "Remote access" panel and the headless server read these.
    "remote_enabled": False,                 # start the server with the app
    "remote_port": 8000,
    "remote_lan": False,                     # False = 127.0.0.1 only; True = all interfaces (token forced)
    "remote_token": "",                      # generated on first use
    "remote_require_token_local": False,     # also require the token on 127.0.0.1
    "report_language": "de",                 # de | en (report contents)

    # Measure cursors (last positions)
    "measure_a_nm": 500.0,
    "measure_b_nm": 600.0,
}


# Defaults that changed: a stored value equal to the OLD default is replaced
# by the new one once (config_version). Values the user changed are kept.
CONFIG_VERSION = 2
_CHANGED_DEFAULTS = {
    2: {"calibration_max_gain_span": (40.0, 200.0)},
}


def _migrate_defaults(data: dict, ver: int) -> bool:
    """Apply changed defaults for a file written with config_version `ver`."""
    if ver >= CONFIG_VERSION:
        return False
    for v in range(ver + 1, CONFIG_VERSION + 1):
        for key, (old, new) in _CHANGED_DEFAULTS.get(v, {}).items():
            try:
                if key in data and float(data[key]) == float(old):
                    data[key] = new
            except (TypeError, ValueError):
                pass
    data["config_version"] = CONFIG_VERSION
    return True


class _Config:
    def __init__(self, path: str = CONFIG_FILENAME):
        # relative names live in the user data directory (data_paths)
        if not os.path.isabs(path):
            import data_paths
            path = data_paths.resolve(path)
        self.path = path
        self._data: dict[str, Any] = {}
        self.load()

    def load(self) -> None:
        if not os.path.exists(self.path):
            self._data = dict(DEFAULT_CONFIG)
            self.save()
            return
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                user = json.load(f)
            # Merge defaults with whatever the user has (defaults fill gaps)
            user = user or {}
            self._data = {**DEFAULT_CONFIG, **user}
            if _migrate_defaults(self._data, int(user.get("config_version", 1) or 1)):
                self.save()
        except Exception as e:
            print(f"[config] failed to read {self.path}: {e} — using defaults")
            self._data = dict(DEFAULT_CONFIG)

    def save(self) -> None:
        try:
            d = os.path.dirname(os.path.abspath(self.path))
            os.makedirs(d, exist_ok=True)
            with open(self.path, "w", encoding="utf-8") as f:
                json.dump(self._data, f, indent=2, sort_keys=True)
        except Exception as e:
            print(f"[config] failed to write {self.path}: {e}")

    def get(self, key: str, default: Any = None) -> Any:
        return self._data.get(key, DEFAULT_CONFIG.get(key, default))

    def set(self, key: str, value: Any, *, save: bool = True) -> None:
        self._data[key] = value
        if save:
            self.save()

    def update(self, mapping: dict[str, Any], *, save: bool = True) -> None:
        self._data.update(mapping)
        if save:
            self.save()

    def all(self) -> dict[str, Any]:
        return dict(self._data)


# Module-level singleton — import this from anywhere. The user data directory
# is prepared first (one-time migration out of the program folder, templates).
import data_paths as _data_paths            # noqa: E402

_data_paths.initialize()
Config = _Config(_data_paths.config_path())

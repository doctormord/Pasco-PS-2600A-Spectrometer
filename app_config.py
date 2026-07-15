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

    # Measure cursors (last positions)
    "measure_a_nm": 500.0,
    "measure_b_nm": 600.0,
}


class _Config:
    def __init__(self, path: str = CONFIG_FILENAME):
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
            self._data = {**DEFAULT_CONFIG, **(user or {})}
        except Exception as e:
            print(f"[config] failed to read {self.path}: {e} — using defaults")
            self._data = dict(DEFAULT_CONFIG)

    def save(self) -> None:
        try:
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


# Module-level singleton — import this from anywhere.
Config = _Config()

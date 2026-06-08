"""
device_constants.py — Shared constants used by all spectrometer backends.

Device-specific constants (ADC range, pixel count, wavelength calibration,
command codes) belong in the individual _device_*.py files.  This module
contains only values that are truly device-agnostic: timing limits and
auto-exposure control ratios.
"""

# ── Integration time bounds (µs) ─────────────────────────────────────────
# These are the GUI-level limits.  Each backend may further clamp to its
# own hardware-imposed range (e.g. PASCO max = 2 500 000 µs).
GLOBAL_MIN_INTEGRATION_TIME_US: int   = 100          # absolute sanity floor (0.1 ms)
GLOBAL_MAX_INTEGRATION_TIME_US: int   = 10_000_000
# NOTE: the real per-device minimum/maximum come from each backend's
# *_min_integration_us / *_max_integration_us config keys (Ocean, LR-2T) or its
# fixed hardware constants (PASCO). The globals above are only an absolute
# backstop so a bad config value can't drive the hardware to 0 / absurd values.

# ── Auto-exposure control ratios ────────────────────────────────────────
# Applied by all backends that implement auto-exposure.
# Each backend supplies its own target ADC level and deadzone.
AUTO_EXP_MIN_RATIO:              float = 0.1
AUTO_EXP_MAX_RATIO:              float = 8.0
AUTO_EXP_EMERGENCY_DROP_RATIO:   float = 0.2

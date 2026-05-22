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

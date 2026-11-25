"""Shared pytest fixtures.

Every test runs against an isolated config + calibration store in a temp dir,
so the user's spectrometer_config.json / calibration_profiles.json are never
touched.
"""
from __future__ import annotations

import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

DOCS = os.path.join(ROOT, "docs")
LMP_FILE = os.path.join(DOCS, "1808001-NIR-CCVISNIR.lmp")


@pytest.fixture(autouse=True)
def isolated_config(tmp_path, monkeypatch):
    """Point the Config singleton and all calibration stores at tmp_path."""
    import app_config
    cfg = app_config.Config
    saved_path, saved_data = cfg.path, dict(cfg._data)
    cfg.path = str(tmp_path / "spectrometer_config.json")
    cfg._data = dict(app_config.DEFAULT_CONFIG)
    cfg._data["calibration_profiles_file"] = str(tmp_path / "calibration_profiles.json")
    cfg._data["reference_sources_file"] = str(tmp_path / "reference_sources.json")
    cfg._data["dark_spectra_file"] = str(tmp_path / "dark_spectra.json")
    cfg._data["linearity_profiles_file"] = str(tmp_path / "linearity_profiles.json")
    # Exports / reports saved by the server (save=true, autosave) → tmp,
    # never the program folder.
    cfg._data["export_dir"] = str(tmp_path / "exports")
    # Protocol fallback folder (user data dir) → tmp, never the real profile.
    import export_paths
    monkeypatch.setattr(export_paths, "user_data_dir",
                        lambda: str(tmp_path / "userdata" / "PS-2600A"))
    try:
        yield cfg
    finally:
        cfg.path, cfg._data = saved_path, saved_data


@pytest.fixture
def lmp_path():
    return LMP_FILE

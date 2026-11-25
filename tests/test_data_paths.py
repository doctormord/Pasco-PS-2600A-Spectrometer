"""Hardware-test point 15: user data directory, overrides, one-time migration
(copy, never move / overwrite, backup, log), templates, export folder."""
from __future__ import annotations

import json
import os

import pytest

import data_paths as dp


def test_default_locations():
    assert dp.default_data_dir("win32", {"APPDATA": r"C:\U\AppData\Roaming"}, "C:\\U") \
        .endswith(os.path.join("Roaming", "PS-2600A"))
    assert dp.default_data_dir("linux", {"XDG_CONFIG_HOME": "/x/cfg"}, "/home/u") \
        == os.path.join("/x/cfg", "ps-2600a")
    assert dp.default_data_dir("linux", {}, "/home/u") == os.path.join("/home/u", ".config",
                                                                        "ps-2600a")
    assert dp.default_data_dir("darwin", {}, "/Users/u") == os.path.join(
        "/Users/u", "Library", "Application Support", "PS-2600A")


def test_tests_run_in_temporary_data_dir():
    # conftest sets PS2600A_DATA_DIR before any import: real data is never used
    real = dp.default_data_dir()
    assert os.path.normcase(dp.data_dir()) != os.path.normcase(real)
    import app_config
    assert not dp.is_inside(app_config.Config.path, dp.program_dir())


def test_env_and_cli_override(tmp_path, monkeypatch):
    monkeypatch.setattr(dp, "_override", None)
    monkeypatch.setenv(dp.ENV_VAR, str(tmp_path / "env"))
    assert dp.data_dir() == str(tmp_path / "env")
    rest = dp.parse_cli(["--headless", "--data-dir", str(tmp_path / "cli"), "--port", "1"])
    try:
        assert rest == ["--headless", "--port", "1"]
        assert dp.data_dir() == str(tmp_path / "cli")
        assert os.environ[dp.ENV_VAR] == str(tmp_path / "cli")
        assert dp.resolve("x.json") == os.path.join(str(tmp_path / "cli"), "x.json")
        assert dp.resolve(str(tmp_path / "abs.json")) == str(tmp_path / "abs.json")
    finally:
        dp.set_data_dir(None)
    rest = dp.parse_cli([f"--data-dir={tmp_path / 'eq'}"])
    try:
        assert rest == [] and dp.data_dir() == str(tmp_path / "eq")
    finally:
        dp.set_data_dir(None)


def _program(tmp_path):
    src = tmp_path / "program"
    src.mkdir()
    (src / "spectrometer_config.json").write_text(json.dumps({
        "theme": "light", "calibration_profiles_file": str(src / "calibration_profiles.json"),
        "export_dir": str(src / "exports")}), encoding="utf-8")
    (src / "calibration_profiles.json").write_text('{"schema": 2, "profiles": [1]}')
    (src / "dark_spectra.json").write_text('{"darks": []}')
    (src / "reference_sources.json").write_text('{"sources": []}')
    (src / "calibration_profiles.v1.bak.json").write_text('{"old": 1}')
    (src / "old_calib_data_20261125_121734.json").write_text('{"x": 1}')
    (src / "calib_Demo_x.pdf").write_bytes(b"%PDF")
    (src / "spectrometer_config.example.json").write_text("{}")
    return src


def test_migration_copies_keeps_originals_backup_log(tmp_path):
    src = _program(tmp_path)
    dst = tmp_path / "data"
    before = {p.name: p.read_bytes() for p in src.iterdir()}
    rep = dp.migrate(str(src), str(dst), log=None)
    assert rep["status"] == "migrated"
    names = {f["name"] for f in rep["files"]}
    assert {"spectrometer_config.json", "calibration_profiles.json", "dark_spectra.json",
            "reference_sources.json", "calibration_profiles.v1.bak.json",
            "old_calib_data_20261125_121734.json"} <= names
    assert "spectrometer_config.example.json" not in names
    # originals untouched (copied, not moved)
    assert {p.name: p.read_bytes() for p in src.iterdir()} == before
    assert (dst / "calibration_profiles.json").read_text() == '{"schema": 2, "profiles": [1]}'
    assert (dst / "protocols" / "calib_Demo_x.pdf").exists()
    assert os.path.isdir(rep["backup"]) and os.path.exists(
        os.path.join(rep["backup"], "dark_spectra.json"))
    # config paths no longer point into the program folder
    cfg = json.loads((dst / "spectrometer_config.json").read_text())
    assert cfg["calibration_profiles_file"] == "calibration_profiles.json"
    assert cfg["export_dir"] == "" and cfg["theme"] == "light"
    assert (dst / dp.MARKER).exists() and "migrated" in (dst / dp.LOG).read_text()
    # only once
    assert dp.migrate(str(src), str(dst), log=None)["status"] == "already_migrated"


def test_migration_never_overwrites_existing_data(tmp_path):
    src = _program(tmp_path)
    dst = tmp_path / "data"
    dst.mkdir()
    (dst / "calibration_profiles.json").write_text('{"newer": true}')
    rep = dp.migrate(str(src), str(dst), log=None)
    assert (dst / "calibration_profiles.json").read_text() == '{"newer": true}'
    act = next(f["action"] for f in rep["files"] if f["name"] == "calibration_profiles.json")
    assert act.startswith("kept_existing")
    kept = [p for p in dst.iterdir() if p.name.startswith("calibration_profiles.from-program-dir")]
    assert kept and kept[0].read_text() == '{"schema": 2, "profiles": [1]}'


def test_seed_from_examples(tmp_path):
    src = tmp_path / "program"
    src.mkdir()
    (src / "calibration_profiles.example.json").write_text('{"schema": 2, "profiles": []}')
    dst = tmp_path / "data"
    assert dp.seed_from_examples(str(dst), str(src)) == ["calibration_profiles.json"]
    assert dp.seed_from_examples(str(dst), str(src)) == []


def test_repository_ships_templates_only():
    root = dp.program_dir()
    for n in ("spectrometer_config", "calibration_profiles", "reference_sources"):
        assert os.path.isfile(os.path.join(root, f"{n}.example.json"))
    ignore = open(os.path.join(root, ".gitignore"), encoding="utf-8").read()
    for n in dp.RUNTIME_FILES:
        assert n in ignore
    with open(os.path.join(root, "spectrometer_config.example.json"), encoding="utf-8") as f:
        ex = json.load(f)
    import app_config
    assert ex == json.loads(json.dumps(app_config.DEFAULT_CONFIG))


def test_export_dir_never_program_folder(isolated_config):
    import export_paths
    isolated_config._data["export_dir"] = ""
    d, err = export_paths.ensure_export_dir()
    assert not dp.is_inside(d, dp.program_dir())
    assert os.path.normcase(d) == os.path.normcase(dp.default_export_dir()) or err
    isolated_config._data["export_dir"] = os.path.join(dp.program_dir(), "exports_x")
    d, err = export_paths.ensure_export_dir()
    assert not dp.is_inside(d, dp.program_dir()) and err
    d2, _note = export_paths.calibration_protocol_dir()
    assert not dp.is_inside(d2, dp.program_dir())


def test_stores_resolve_into_data_dir(isolated_config, monkeypatch):
    import calibration_capture as cc
    import calibration_linearity as cl
    import calibration_profiles as cp
    for k in ("calibration_profiles_file", "dark_spectra_file", "linearity_profiles_file"):
        isolated_config._data[k] = os.path.basename(isolated_config._data[k])
    d = dp.data_dir()
    assert cp._registry_path() == os.path.join(d, "calibration_profiles.json")
    assert cc._dark_path() == os.path.join(d, "dark_spectra.json")
    assert cl._path() == os.path.join(d, "linearity_profiles.json")

"""Shared pytest fixtures.

Every test runs against an isolated config + calibration store in a temp dir,
so the user's spectrometer_config.json / calibration_profiles.json are never
touched.
"""
from __future__ import annotations

import os
import sys
import tempfile

import pytest

# Runtime data directory for the WHOLE test session (and subprocesses):
# a temporary folder, set before any module imports app_config, so the real
# user data (%APPDATA%\PS-2600A …) and the program folder are never touched.
# The one-time migration is skipped (tests/test_data_paths.py tests it
# explicitly with temporary folders).
_SESSION_DATA = tempfile.mkdtemp(prefix="ps2600a-test-data-")
os.environ["PS2600A_DATA_DIR"] = _SESSION_DATA
os.environ["PS2600A_SKIP_MIGRATION"] = "1"

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

DOCS = os.path.join(ROOT, "docs")
LMP_FILE = os.path.join(DOCS, "1808001-NIR-CCVISNIR.lmp")


# ── Qt: ONE QApplication for the whole session ───────────────────────────
# Root cause of the sporadic access violations of the full suite (finding 20):
# every GUI module had its own module-scoped `qapp` fixture, and three tests
# created the application inline. When the module (or test) that had CREATED
# the QApplication finished, the last Python reference was dropped and PyQt
# destroyed the QApplication while widgets, pyqtgraph items and engine
# threads of earlier tests were still alive; the next GUI test then created a
# SECOND QApplication in the same process. Qt does not support that (dangling
# style / font / screen objects) — the crash surfaced later at a random place
# (widget teardown, garbage collection), depending on the test order.
# Now the application is created once, referenced from this module for the
# lifetime of the process, and all GUI tests use it.
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
_QT_APP = None


def qt_app():
    """The session-wide QApplication (created on first use, never destroyed
    before the interpreter exits).

    Third cause of finding 20: Python's cyclic garbage collector runs in
    whichever thread happens to allocate — demo acquisition, fusion, engine
    jobs, the uvicorn server thread. When it collected Qt widgets / pyqtgraph
    items there, their C++ destructors ran outside the GUI thread and the
    process died (segfault, no Python traceback). As in the application
    (PS-2600A_Pro.py, pyqtgraph GarbageCollector), automatic GC is disabled
    once Qt objects can exist and run in the main thread after every test
    (_collect_in_main_thread)."""
    global _QT_APP
    if _QT_APP is None:
        import gc
        from PyQt6.QtWidgets import QApplication
        _QT_APP = QApplication.instance() or QApplication([])
        gc.disable()
    return _QT_APP


@pytest.fixture(autouse=True)
def _collect_in_main_thread():
    yield
    if _QT_APP is not None:
        import gc
        gc.collect()
        _QT_APP.processEvents()


@pytest.fixture(scope="session")
def qapp():
    pytest.importorskip("PyQt6")
    app = qt_app()
    yield app
    _close_top_level_widgets(app)


def _close_top_level_widgets(app) -> None:
    """Orderly end of the session: close and delete the remaining top-level
    widgets while the application still exists (no widget outlives it)."""
    import gc
    try:
        for w in list(app.topLevelWidgets()):
            try:
                w.close()
                w.deleteLater()
            except RuntimeError:                     # already deleted
                pass
        app.processEvents()
        gc.collect()
        app.processEvents()
    except Exception:                                # noqa: BLE001
        pass


def pytest_sessionfinish(session, exitstatus):
    if _QT_APP is not None:
        _close_top_level_widgets(_QT_APP)


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

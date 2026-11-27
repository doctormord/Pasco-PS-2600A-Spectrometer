"""Regression tests from the review of fix/calibration-ui: demo-spectrum
restore on every exit path, main-window geometry, rendered status colours."""
from __future__ import annotations

import os
import time

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PyQt6")
pytest.importorskip("pyqtgraph")

from PyQt6.QtGui import QColor  # noqa: E402
from PyQt6.QtWidgets import QApplication, QMessageBox  # noqa: E402

import reference_sources as rs  # noqa: E402
from test_gui_calibration import FakeDashboard  # noqa: E402


# qapp: the session-wide QApplication from tests/conftest.py


@pytest.fixture
def msgbox(monkeypatch):
    Std = QMessageBox.StandardButton
    monkeypatch.setattr(QMessageBox, "question", staticmethod(lambda *a, **k: Std.Yes))
    for n in ("information", "warning", "critical"):
        monkeypatch.setattr(QMessageBox, n, staticmethod(lambda *a, **k: Std.Ok))


@pytest.fixture
def neon_demo(isolated_config, tmp_path):
    from _device_demo import DemoSpectrometer
    isolated_config._data.update({
        "demo_spectrum": "Neon (Ne)", "demo_sensitivity": "synthetic",
        "demo_fullscale_ms": 100.0, "calibration_stability_delay_s": 0.05,
        "export_dir": str(tmp_path / "exports")})
    b = DemoSpectrometer(on_frame=lambda *a: None)
    b.connect("Demo Spectrometer")
    b.start()
    yield b
    b.disconnect()


def _start(b):
    import gui_calibration as gc
    dlg = gc.CalibrationDialog(FakeDashboard(b))
    w = dlg.tab_wizard
    w.cmb_optics.setEditText("Fibre")
    w.cmb_input.setCurrentText("CC")
    w.cmb_ref.setCurrentIndex(w.cmb_ref.findData(rs.DEMO_SOURCE_ID))
    w.spn_frames.setValue(4)
    w.start()
    return dlg, w


def _wait(app, w, timeout=120):
    t0 = time.monotonic()
    while w.running and time.monotonic() - t0 < timeout:
        app.processEvents()
        time.sleep(0.01)
    assert not w.running


def test_demo_spectrum_restored_after_cancel(qapp, msgbox, neon_demo):
    dlg, w = _start(neon_demo)
    assert neon_demo.spectrum_name == "cal_lamp"        # switched for the run
    w._cancel_clicked()
    _wait(qapp, w)
    assert w.last_status == "cancelled"
    assert neon_demo.spectrum_name == "Neon (Ne)"


def test_demo_spectrum_restored_after_worker_error(qapp, msgbox, neon_demo, monkeypatch):
    import calibration_workflow as cw

    def boom(self):
        raise RuntimeError("simulated device failure")
    monkeypatch.setattr(cw.CalibrationWorkflow, "capture_darks", boom)
    dlg, w = _start(neon_demo)
    _wait(qapp, w)
    assert w.last_status == "error" and "simulated" in (w.last_error or "")
    assert neon_demo.spectrum_name == "Neon (Ne)"


def test_demo_spectrum_restored_when_dialog_closed(qapp, msgbox, neon_demo):
    import gui_calibration as gc
    dlg = gc.CalibrationDialog(FakeDashboard(neon_demo))
    w = dlg.tab_wizard
    w._apply_demo_spectrum(neon_demo, rs.ReferenceLibrary().get(rs.DEMO_SOURCE_ID))
    assert neon_demo.spectrum_name == "cal_lamp"
    dlg.done(0)
    assert neon_demo.spectrum_name == "Neon (Ne)"


def test_main_window_geometry_clamped_and_scrollable(qapp):
    """The main window uses place_window with these parameters and a scroll
    area as central widget. (Constructing the full DashboardWindow here leaves
    Qt state behind that crashes later tests at teardown, so the placement is
    checked on a QMainWindow with the same call and the wiring in the source.)"""
    import inspect
    import gui_dashboard
    from PyQt6.QtWidgets import QMainWindow
    from gui_layout import place_window
    src = inspect.getsource(gui_dashboard.DashboardWindow)
    assert 'place_window(self, None, Config.get("main_window_geometry", None)' in src
    assert 'setObjectName("mainScroll")' in src and "setCentralWidget(main_scroll)" in src
    assert 'Config.set("main_window_geometry"' in src
    win = QMainWindow()
    place_window(win, None, [5000, 5000, 4000, 3000],
                 min_w=1000, min_h=600, max_w=1480, max_h=900)
    avail = win.screen().availableGeometry()
    assert avail.contains(win.geometry()), (win.geometry(), avail)
    assert win.minimumWidth() <= avail.width() and win.minimumHeight() <= avail.height()
    win2 = QMainWindow()
    place_window(win2, None, None, min_w=1000, min_h=600, max_w=1480, max_h=900)
    g = win2.geometry()
    assert g.width() <= 1480 and g.height() <= 900 and avail.contains(g)


@pytest.mark.parametrize("theme_name", ["DarkPalette", "LightPalette"])
def test_step_list_status_colours_are_rendered(qapp, msgbox, theme_name):
    """The theme must not override the per-item status colours (regression:
    'color' on ::item hid them)."""
    import gui_theme as gt
    import gui_calibration as gc
    old_ss, old_pal, old_theme = qapp.styleSheet(), qapp.palette(), gt.Theme
    pal = getattr(gt, theme_name)
    try:
        gt.apply_app_theme(qapp, pal)
        dlg = gc.CalibrationDialog(FakeDashboard())
        w = dlg.tab_wizard
        dlg.show()
        dlg.tabs.setCurrentIndex(1)
        for i in range(len(w._step_names)):
            w._state(i, "failed", "x")
        qapp.processEvents()
        img = w.lst_steps.viewport().grab().toImage()
        target = QColor(pal.DANGER)
        hits = 0
        for y in range(0, img.height(), 1):
            for x in range(0, img.width(), 2):
                c = img.pixelColor(x, y)
                if (abs(c.red() - target.red()) < 40 and abs(c.green() - target.green()) < 40
                        and abs(c.blue() - target.blue()) < 40):
                    hits += 1
        assert hits > 20, f"status colour {pal.DANGER} not rendered ({hits} px)"
        dlg.done(0)
    finally:
        qapp.setStyleSheet(old_ss)
        qapp.setPalette(old_pal)
        gt.set_active_palette(old_theme)

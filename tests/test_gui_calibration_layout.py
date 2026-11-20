"""Layout / geometry / theme tests for the calibration dialog (offscreen)."""
from __future__ import annotations

import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PyQt6")
pytest.importorskip("pyqtgraph")

from PyQt6.QtCore import Qt  # noqa: E402
from PyQt6.QtGui import QPalette  # noqa: E402
from PyQt6.QtWidgets import (  # noqa: E402
    QAbstractItemView, QAbstractScrollArea, QApplication, QGraphicsView,
    QMessageBox, QWidget,
)

import gui_layout as gl  # noqa: E402
import gui_theme as gt  # noqa: E402
from test_gui_calibration import FakeDashboard, BACKEND  # noqa: E402


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


@pytest.fixture
def msgbox(monkeypatch):
    Std = QMessageBox.StandardButton
    monkeypatch.setattr(QMessageBox, "question", staticmethod(lambda *a, **k: Std.Yes))
    for n in ("information", "warning", "critical"):
        monkeypatch.setattr(QMessageBox, n, staticmethod(lambda *a, **k: Std.Ok))


@pytest.fixture
def themed_app(qapp):
    old_pal = QPalette(qapp.palette())
    old_theme = gt.Theme
    yield qapp
    qapp.setStyleSheet("")
    qapp.setPalette(old_pal)
    gt.set_active_palette(old_theme)


def _dialog(dash=None):
    import gui_calibration as gc
    return gc.CalibrationDialog(dash or FakeDashboard())


def _profile(name="legacy one", created=None):
    import numpy as np
    import calibration_profiles as calprof
    wl = np.linspace(400, 800, 50)
    meta = {"wl_min_nm": 400.0, "wl_max_nm": 800.0}
    if created:
        meta["created"] = created
    return calprof.save_profile(BACKEND, name, np.column_stack([wl, np.ones(50)]), meta)


# -- 1. pure geometry -------------------------------------------------------
def test_clamp_rect_too_large_respects_frame():
    avail = (0, 0, 1366, 728)
    x, y, w, h = gl.clamp_rect((-50, -50, 5000, 4000), avail, frame_top=32, frame_side=1)
    assert w <= 1366 - 2 and h <= 728 - 32 - 1
    assert x >= 1 and y >= 32
    assert x + w <= 1366 - 1 and y + h <= 728 - 1


def test_clamp_rect_pulls_offscreen_window_in():
    avail = (0, 0, 1366, 728)
    x, y, w, h = gl.clamp_rect((3000, 2000, 800, 600), avail, frame_top=30, frame_side=1)
    assert (w, h) == (800, 600)
    assert x + w <= 1366 and y + h <= 728
    assert x >= 0 and y >= 30


def test_clamp_rect_negative_secondary_monitor():
    avail = (0, 0, 1366, 728)
    x, y, w, h = gl.clamp_rect((-2000, 100, 800, 600), avail, frame_top=30, frame_side=1)
    assert x >= 1


def test_clamp_rect_screen_wins_over_minimum():
    avail = (0, 0, 640, 400)
    x, y, w, h = gl.clamp_rect((10, 10, 900, 900), avail, min_w=760, min_h=560,
                               frame_top=30, frame_side=1)
    assert w <= 640 and h <= 400 - 30
    assert x >= 0 and y >= 30


@pytest.mark.parametrize("avail", [(0, 0, 1366, 728), (0, 0, 1536, 824), (0, 0, 1280, 680)])
def test_initial_rect_fits_centered(avail):
    ft, fs = 32, 1
    x, y, w, h = gl.initial_rect(avail, min_w=gl.MIN_DIALOG_W, min_h=gl.MIN_DIALOG_H,
                                 frame_top=ft, frame_side=fs)
    ax, ay, aw, ah = avail
    assert w + 2 * fs <= 0.90 * aw + 1 and h + ft + fs <= 0.90 * ah + 1
    assert y - ft >= ay and y + h + fs <= ay + ah
    assert x - fs >= ax and x + w + fs <= ax + aw
    assert abs((x - ax) - (aw - w) / 2) <= fs + 1
    assert abs((y - ay - ft) - (ah - ft - fs - h) / 2) <= 2


def test_min_dialog_size_fits_1366x728():
    assert gl.MIN_DIALOG_W + 2 <= 1366
    assert gl.MIN_DIALOG_H + 32 + 1 <= 728


# -- 2. dialog geometry -----------------------------------------------------
def test_saved_geometry_clamped_into_screen(qapp, msgbox, isolated_config):
    isolated_config._data["calibration_dialog_geometry"] = [5000, 5000, 4000, 3000]
    dlg = _dialog()
    avail = dlg.screen().availableGeometry()
    assert avail.contains(dlg.geometry())
    dlg.close()


def test_geometry_saved_on_done(qapp, msgbox, isolated_config):
    dlg = _dialog()
    dlg.show()
    qapp.processEvents()
    dlg.done(0)
    g = isolated_config.get("calibration_dialog_geometry", None)
    assert isinstance(g, list) and len(g) == 4
    assert all(isinstance(v, int) for v in g)


# -- 3. no layout jump ------------------------------------------------------
def test_long_texts_do_not_change_layout(qapp, msgbox):
    dlg = _dialog()
    dlg.tabs.setCurrentIndex(1)
    dlg.show()
    qapp.processEvents()
    w = dlg.tab_wizard
    geo, size = dlg.geometry(), dlg.size()
    heights = {n: getattr(w, n).height() for n in ("lbl_result", "lbl_ref_info", "lbl_status")}

    path = "C:/" + "very_long_dir/" * 80 + "file.pdf"
    w.lbl_status.setText("x" * 2000)
    w.lbl_protocol.setText(path)
    w.lbl_result.setText("<br>".join(["line"] * 50))
    for i in range(w.lst_steps.count()):
        w._state(i, "error", "y" * 500)
    for _ in range(w.cmb_ref.count()):
        w.cmb_ref.setCurrentIndex((w.cmb_ref.currentIndex() + 1) % w.cmb_ref.count())
        qapp.processEvents()
    w.lbl_ref_info.setText("<br>".join(["info"] * 30))
    qapp.processEvents()

    assert dlg.geometry() == geo
    assert dlg.size() == size
    for n, h in heights.items():
        assert getattr(w, n).height() == h, n
    assert not w.lst_steps.horizontalScrollBar().isVisible()

    assert w.lbl_status.toolTip() == "x" * 2000
    assert w.lbl_protocol.toolTip() == path
    w.lbl_protocol.copy_text()
    assert QApplication.clipboard().text() == path
    dlg.close()


# -- 4. no white areas in the dark theme ------------------------------------
def _white_ratio(img):
    total = img.width() * img.height()
    white = 0
    for y in range(img.height()):
        for x in range(img.width()):
            c = img.pixelColor(x, y)
            if c.red() >= 240 and c.green() >= 240 and c.blue() >= 240:
                white += 1
    return white / total


def test_dark_theme_no_white_areas(themed_app, msgbox):
    app = themed_app
    gt.apply_app_theme(app, gt.DarkPalette)
    _profile()
    dlg = _dialog()
    dlg.show()
    app.processEvents()
    for i in range(dlg.tabs.count()):
        dlg.tabs.setCurrentIndex(i)
        app.processEvents()
        img = dlg.grab().toImage()
        if img.isNull() or img.width() == 0:
            pytest.skip("grab() not usable on this offscreen platform")
        r = _white_ratio(img)
        assert r < 0.005, f"tab {i}: {r:.2%} near-white pixels"
        for wd in dlg.findChildren(QWidget):
            if (isinstance(wd, QAbstractScrollArea) and not isinstance(wd, QGraphicsView)
                    and wd.isVisible()):
                vp = wd.viewport()
                c = vp.palette().color(vp.backgroundRole())
                assert not (c.red() >= 240 and c.green() >= 240 and c.blue() >= 240), \
                    f"tab {i}: {type(wd).__name__} {wd.objectName()!r} viewport is white"
    dlg.close()


def test_light_theme_renders(themed_app, msgbox):
    app = themed_app
    gt.apply_app_theme(app, gt.LightPalette)
    dlg = _dialog()
    dlg.show()
    for i in range(dlg.tabs.count()):
        dlg.tabs.setCurrentIndex(i)
        app.processEvents()
        assert not dlg.grab().toImage().isNull()
    dlg.close()


# -- 5. tables --------------------------------------------------------------
def test_profiles_table_behaviour(qapp, msgbox):
    _profile("older profile", created="2024-01-01T10:00:00")
    long_name = "newer profile " + "N" * 80
    _profile(long_name, created="2025-06-01T10:00:00")
    dlg = _dialog()
    t = dlg.tab_profiles
    t.refresh()
    tb = t.table
    assert tb.rowCount() == 2
    assert not tb.verticalHeader().isVisible()
    assert tb.editTriggers() == QAbstractItemView.EditTrigger.NoEditTriggers
    assert tb.selectionBehavior() == QAbstractItemView.SelectionBehavior.SelectRows
    assert tb.isSortingEnabled()
    h = tb.horizontalHeader()
    assert h.sortIndicatorSection() == t.COLS.index("Created")
    assert h.sortIndicatorOrder() == Qt.SortOrder.DescendingOrder
    assert long_name in tb.item(0, 0).toolTip()
    assert "older profile" in tb.item(1, 0).toolTip()
    dlg.close()

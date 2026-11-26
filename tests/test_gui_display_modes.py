"""GUI tests for the display modes (hardware test points 9-13): selector, axis
label, exposure bar + raw axis, per-mode Y limits, readouts with "–" for masked
pixels, and the calibration dialog's result pane. Offscreen, Demo backend."""
from __future__ import annotations

import dataclasses
import os
import time

import numpy as np
import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PyQt6")
pytest.importorskip("pyqtgraph")

from PyQt6.QtCore import QPointF  # noqa: E402
from PyQt6.QtWidgets import QApplication, QMessageBox  # noqa: E402

import calibration_profiles as cp  # noqa: E402
import calibration_workflow as cw  # noqa: E402
import processing  # noqa: E402
import reference_sources as rs  # noqa: E402
from test_gui_calibration import FakeDashboard, BACKEND  # noqa: E402


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


def _pump(app, sec):
    end = time.time() + sec
    while time.time() < end:
        app.processEvents()
        time.sleep(0.01)


_DEMO_CFG = {"demo_spectrum": "cal_lamp", "demo_sensitivity": "synthetic",
             "demo_fullscale_ms": 100.0, "calibration_stability_delay_s": 0.1,
             "calibration_capture_frames": 6}


@pytest.fixture(scope="module")
def _window(qapp, tmp_path_factory):
    """ONE DashboardWindow for the whole module (a third window created in the
    same process crashes Qt at teardown, see test_calibration_ui_regressions);
    config + stores are isolated like the autouse fixture does per test."""
    import app_config
    cfg = app_config.Config
    old_path, old_data = cfg.path, dict(cfg._data)
    tmp = tmp_path_factory.mktemp("display_modes")
    cfg.path = str(tmp / "spectrometer_config.json")
    cfg._data = dict(app_config.DEFAULT_CONFIG)
    for k, f in (("calibration_profiles_file", "calibration_profiles.json"),
                 ("reference_sources_file", "reference_sources.json"),
                 ("dark_spectra_file", "dark_spectra.json"),
                 ("linearity_profiles_file", "linearity_profiles.json")):
        cfg._data[k] = str(tmp / f)
    cfg._data["export_dir"] = str(tmp / "exports")
    cfg._data.update(_DEMO_CFG)
    from gui_dashboard import DashboardWindow
    w = DashboardWindow()
    try:
        w.combo_backend.setCurrentText(BACKEND)
        w.refresh_device_list()
        w.connect_device()
        t0 = time.time()
        while w.engine.last_frame is None and time.time() - t0 < 10:
            _pump(qapp, 0.05)
        assert w.engine.last_frame is not None
        w.set_measurement_paused(True)            # frames are driven by the tests
        _pump(qapp, 0.3)
        yield w
    finally:
        w.close()
        qapp.processEvents()
        cfg.path, cfg._data = old_path, old_data


@pytest.fixture
def dash(_window, qapp, isolated_config):
    """The shared window in a clean state (fresh per-test config / stores)."""
    w = _window
    isolated_config._data.update(_DEMO_CFG)
    w.engine.rebuild_calibration()                # no profile in the fresh store
    w.freeze_xy = None
    w.overlay_diff = w.overlay_peak_hold = w.overlay_persist = False
    w.peak_hold_env = None
    w.persist_ring = []
    w.is_measure_mode_enabled = False
    w.toggle_scope_reference(False)
    w._display_mode = None                        # re-evaluate the mode / limits
    w._modes_sig = None
    w._cfg({"display_mode": "auto"})
    _show(w)
    return w


def _show(w, mode=None):
    """Set the display mode and render ONE fresh engine frame in it."""
    if mode is not None:
        w._cfg({"display_mode": mode})
    last = w.engine.last_frame
    ev = w.engine.ingest_frame(last.raw_averaged, last.ob_mean, last.integration_us,
                               notify=False)
    w._render_frame(ev)
    return ev


def _profile(w):
    """A saved + active relative profile on the connected Demo unit."""
    b = w.engine.acquisition
    wf = cw.CalibrationWorkflow(
        b, BACKEND, kind="relative", optics={"name": "Fibre", "input": "bare"},
        reference=rs.ReferenceLibrary().get(rs.DEMO_SOURCE_ID),
        cfg=cw.WorkflowConfig(n_frames=6, stability_delay_s=0.1))
    wf.run_all(integration_us=int(b.current_integration_time_us))
    cp.set_active_selection(BACKEND, wf.saved[0]["id"])
    w.engine.rebuild_calibration()
    return wf


def _axis_label(w):
    return w.plot_canvas.getAxis("left").labelText


# ── 9. selector ──────────────────────────────────────────────────────────────
def test_mode_selector_lists_modes_and_greys_unavailable(dash):
    cb = dash.combo_display_mode
    labels = [cb.itemText(i) for i in range(cb.count())]
    assert labels[0] == "Auto"
    assert labels == [m["label"] for m in dash.engine.display_modes()]
    enabled = {cb.itemData(i): cb.model().item(i).isEnabled() for i in range(cb.count())}
    assert enabled["auto"] and enabled["raw"] and enabled["normalised"]
    assert enabled["count_rate"]
    assert not enabled["corrected"] and not enabled["absolute"]      # no profile
    _profile(dash)
    _show(dash)                                   # the next frame refreshes the list
    enabled = {cb.itemData(i): cb.model().item(i).isEnabled() for i in range(cb.count())}
    assert enabled["corrected"] and not enabled["absolute"]


def test_switching_mode_updates_axis_label_and_plotted_data(dash):
    _show(dash, "raw")
    assert _axis_label(dash) == "Raw ADC [counts]"
    raw_max = float(np.nanmax(dash.spectrum_curve.yData))
    assert raw_max > 50
    dash.combo_display_mode.setCurrentIndex(dash.combo_display_mode.findData("normalised"))
    dash._display_mode_picked()                   # the user's choice
    assert dash.engine.cfg.get("display_mode") == "normalised"
    ev = _show(dash)
    assert ev.display_mode == "normalised"
    assert _axis_label(dash) == "Normalised"
    assert float(np.nanmax(dash.spectrum_curve.yData)) == pytest.approx(1.0)
    # evaluations keep the corrected data
    assert dash.current_averaged_pixels is ev.pixels
    _show(dash, "count_rate")
    assert "counts/s" in _axis_label(dash) and "Count rate" in _axis_label(dash)
    t = dash.engine.last_frame.integration_us / 1e6
    assert float(np.nanmax(dash.spectrum_curve.yData)) == pytest.approx(raw_max / t, rel=0.2)


def test_reference_overlay_follows_displayed_values(dash):
    dash.engine.cfg.set("scope_reference_id", rs.DEMO_SOURCE_ID, save=False)
    dash.sync_scope_reference()
    dash.toggle_scope_reference(True)
    _show(dash, "normalised")
    assert dash.scope_ref_curve.isVisible()
    y = dash.scope_ref_curve.getData()[1]
    assert 0.3 < np.nanmax(y) < 3.0                          # on the normalised scale
    dash.toggle_scope_reference(False)


def test_overlays_are_reset_when_the_mode_changes(dash):
    _show(dash, "raw")
    dash.toggle_freeze()
    dash.toggle_peak_hold()
    assert dash.freeze_xy is not None
    _show(dash, "normalised")
    assert dash.freeze_xy is None and not dash.overlay_diff
    assert dash.peak_hold_env is not None and dash.peak_hold_env.max() <= 1.0 + 1e-9
    assert not dash.freeze_curve.isVisible()


# ── 10. exposure bar / raw axis ──────────────────────────────────────────────
def test_exposure_bar_and_raw_axis_in_corrected_mode(dash):
    _show(dash, "raw")
    assert dash.lbl_exposure.isHidden() and not dash.raw_curve.isVisible()
    _profile(dash)
    ev = _show(dash, "corrected")
    assert ev.display_mode == "corrected"
    assert not dash.lbl_exposure.isHidden() and not dash.bar_exposure.isHidden()
    assert dash.lbl_exposure.text().startswith("Peak ")
    assert "% FS" in dash.lbl_exposure.text()
    assert dash.bar_exposure.value() == int(round(ev.exposure["peak_pct_fs"]))
    assert dash.raw_curve.isVisible()                          # default on
    assert dash.plot_canvas.getPlotItem().getAxis("right").isVisible()
    # colour class follows the drive level (theme tokens)
    for pct, n_sat, cls in ((40.0, 0, "ok"), (90.0, 0, "warn"), (99.0, 0, "danger"),
                            (50.0, 3, "danger")):
        ex = dict(ev.exposure, peak_pct_fs=pct, n_saturated=n_sat, saturated=bool(n_sat))
        dash._update_exposure(dataclasses.replace(ev, exposure=ex))
        assert dash._exposure_cls == cls
    assert "saturated" in dash.lbl_exposure.text()
    # toggle
    dash.btn_raw_axis.click()
    assert not dash.raw_curve.isVisible()
    dash.btn_raw_axis.click()
    _show(dash)
    assert dash.raw_curve.isVisible()


# ── 11. per-mode limits ──────────────────────────────────────────────────────
def test_y_limits_per_mode_persist_and_autoscale_on_switch(dash, isolated_config):
    _show(dash, "raw")
    dash.spinbox_y_max.setValue(1234)
    assert not dash.is_auto_y_axis_enabled
    ent = isolated_config.get("y_limits_by_mode")["raw"]
    assert ent["auto"] is False and ent["max"] == 1234
    assert isolated_config.get("heatmap_limits_by_mode")["raw"]["max"] == 1234
    _show(dash)
    assert dash.plot_canvas.getViewBox().viewRange()[1][1] == pytest.approx(1234)
    # another mode: auto-scale once (normalised data → about 1.1)
    _show(dash, "normalised")
    assert dash.is_auto_y_axis_enabled and dash.button_auto_y_axis.isChecked()
    top = dash.plot_canvas.getViewBox().viewRange()[1][1]
    assert 1.0 < top < 2.0
    assert dash.image_item.levels[1] < 2.0                    # heatmap level follows
    # set a manual limit there, switch to count rate and back
    dash.spinbox_y_max.setValue(0.5)
    assert isolated_config.get("y_limits_by_mode")["normalised"]["max"] == 0.5
    _show(dash, "count_rate")
    assert dash.is_auto_y_axis_enabled
    assert dash.plot_canvas.getViewBox().viewRange()[1][1] > 100.0
    _show(dash, "raw")
    assert not dash.is_auto_y_axis_enabled
    assert dash.spinbox_y_max.value() == 1234
    assert dash.plot_canvas.getViewBox().viewRange()[1][1] == pytest.approx(1234)
    _show(dash, "normalised")
    assert dash.spinbox_y_max.value() == pytest.approx(0.5)
    assert dash.plot_canvas.getViewBox().viewRange()[1][1] == pytest.approx(0.5)
    # auto again → stored as auto
    dash.button_auto_y_axis.setChecked(True)
    assert isolated_config.get("y_limits_by_mode")["normalised"]["auto"] is True


# ── 12. readouts ─────────────────────────────────────────────────────────────
def _hover(w, idx):
    vb = w.plot_canvas.getPlotItem().vb
    y0, y1 = vb.viewRange()[1]
    sp = vb.mapViewToScene(QPointF(float(w.active_wavelengths[idx]), 0.5 * (y0 + y1)))
    w._last_cursor_idx = -1
    w.handle_mouse_movement([sp])


def test_readout_shows_dash_for_masked_pixels_and_unit(dash):
    _show(dash, "raw")
    n = len(dash.active_wavelengths)
    i = n // 2
    _hover(dash, i)
    assert dash.readout_int._val.text().strip().endswith("counts")
    assert "nan" not in dash.readout_int._val.text().lower()
    # masked pixel (NaN in the displayed data)
    d = np.array(dash.current_display_pixels, float)
    d[i] = np.nan
    dash.current_display_pixels = d
    _hover(dash, i)
    assert "–" in dash.readout_int._val.text()
    assert "nan" not in dash.readout_int._val.text().lower()
    # measure mode A/B: a masked pixel gives "–" for the difference
    dash.is_measure_mode_enabled = True
    dash.measure_line_a.setPos(float(dash.active_wavelengths[i]))
    dash.measure_line_b.setPos(float(dash.active_wavelengths[i + 40]))
    dash.update_measurement_display()
    assert "Δy –" in dash.measure_label.textItem.toPlainText()
    dash.is_measure_mode_enabled = False
    # display unit shown in a normalised view
    _show(dash, "normalised")
    _hover(dash, i + 10)
    txt = dash.readout_int._val.text()
    assert "counts" not in txt and "." in txt


def test_heatmap_frame_csv_header_uses_display_unit():
    import inspect
    import gui_dashboard
    src = inspect.getsource(gui_dashboard.DashboardWindow._hm_export_frame_csv)
    # shared engine export (review W8): header column follows the unit there
    assert "export_heatmap_frame_csv" in src and "csv.writer" not in src
    import engine
    assert engine._value_column("counts") == "ADC_Counts"
    assert engine._value_column("counts_corrected") == "Value_counts_corrected"


# ── 13. calibration dialog: result pane never clipped ────────────────────────
def _doc_height(pane) -> float:
    """Height the pane's text needs at the pane's width (explicit layout)."""
    doc = pane.document().clone()
    doc.setDefaultFont(pane.font())
    doc.setTextWidth(pane.viewport().width())
    return float(doc.size().height())


def test_calibration_result_pane_has_its_own_space(qapp, isolated_config):
    import gui_calibration as gc
    dlg = gc.CalibrationDialog(FakeDashboard())
    w = dlg.tab_wizard
    dlg.show()
    dlg.resize(dlg.minimumSizeHint().expandedTo(dlg.minimumSize()))
    lines = [f"Valid segments: 380–780 nm · gain span 3.1×",
             "Plausibility: passed — coverage 100 %",
             "Stability: 0.40 % — passed · 0 px masked",
             "Integration cross check (t/2): 0.80 % — passed · 2 px masked",
             "Dark drift at 100.00 ms: +0.3 counts"]
    w.lbl_result.setText("<br>".join(lines))
    _pump(qapp, 0.2)
    pane = w.lbl_result
    assert pane.height() >= pane.minimumHeight() > 0
    assert pane.verticalScrollBar().value() == 0              # first row on top
    # the whole text fits the pane (sizeHint of the document vs geometry)
    assert _doc_height(pane) > 0
    assert _doc_height(pane) <= pane.viewport().height() + 1
    # not covered by the plots: the splitter stacks plots above the pane
    assert w.plot_out.geometry().bottom() < pane.geometry().top()
    assert w.plot_in.geometry().bottom() < w.plot_out.geometry().top()
    assert w.plots_split.indexOf(pane) == 2
    # a long result scrolls inside the pane instead of growing it
    h = pane.height()
    pane.setText("<br>".join(["row"] * 60))
    _pump(qapp, 0.1)
    assert pane.height() == h
    assert _doc_height(pane) > pane.viewport().height()          # scrolls
    dlg.close()

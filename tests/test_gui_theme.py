"""Tests for gui_theme: contrast, stylesheet coverage, offscreen render."""
from __future__ import annotations

import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PyQt6")
pytest.importorskip("pyqtgraph")

from PyQt6.QtWidgets import (  # noqa: E402
    QApplication, QListWidget, QPlainTextEdit, QTableWidget,
    QTableWidgetItem, QVBoxLayout, QWidget, QAbstractItemView,
)
from PyQt6.QtCore import Qt  # noqa: E402
from PyQt6.QtGui import QPalette  # noqa: E402

import gui_theme as gt  # noqa: E402

PALETTES = [gt.DarkPalette, gt.LightPalette]


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


@pytest.fixture
def themed_app(qapp):
    old_pal = QPalette(qapp.palette())
    old_theme = gt.Theme
    gt.apply_app_theme(qapp, gt.DarkPalette)
    yield qapp
    qapp.setStyleSheet("")
    qapp.setPalette(old_pal)
    gt.set_active_palette(old_theme)


def test_contrast_ratio_basics():
    assert gt.contrast_ratio("#000000", "#ffffff") == pytest.approx(21.0)
    assert gt.contrast_ratio("#777777", "#777777") == pytest.approx(1.0)


@pytest.mark.parametrize("pal", PALETTES)
@pytest.mark.parametrize("tok", ["FG1", "FG2", "FG3", "OK", "WARN", "DANGER", "ACCENT"])
@pytest.mark.parametrize("bg", ["BG1", "BG2"])
def test_text_contrast(pal, tok, bg):
    r = gt.contrast_ratio(getattr(pal, tok), getattr(pal, bg))
    assert r >= 4.5, f"{pal.__name__}.{tok} on {bg}: {r:.2f}"


@pytest.mark.parametrize("pal", PALETTES)
def test_stylesheet_coverage(pal):
    css = gt.build_stylesheet(pal)
    for sel in ["QHeaderView::section", "QHeaderView::section:vertical",
                "QHeaderView::section:horizontal", "QTableCornerButton",
                "QListView", "QListWidget", "QPlainTextEdit",
                "QScrollBar:horizontal", "QScrollBar:vertical",
                "QAbstractScrollArea::corner", "QToolTip", "QMenu",
                "QCheckBox::indicator", "QSplitter::handle", "QDialog",
                "QWidget#calTabPage"]:
        assert sel in css, sel
    assert "QGraphicsView" not in css


def test_qpalette(qapp):
    q = gt.build_qpalette(gt.DarkPalette)
    assert q.color(QPalette.ColorRole.Base).name() == gt.DarkPalette.BG1
    assert q.color(QPalette.ColorGroup.Disabled,
                   QPalette.ColorRole.Text).name() == gt.DarkPalette.FG4


def test_render_no_white_areas(themed_app):
    app = themed_app
    w = QWidget()
    lay = QVBoxLayout(w)
    table = QTableWidget(3, 3)
    for r in range(3):
        for c in range(3):
            table.setItem(r, c, QTableWidgetItem(f"{r},{c}"))
    table.setHorizontalHeaderLabels(["A", "B", "C"])
    table.verticalHeader().setVisible(True)
    lst = QListWidget()
    lst.addItems(["x" * 300 + str(i) for i in range(3)])
    lst.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOn)
    lst.setHorizontalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)
    txt = QPlainTextEdit("hello")
    for x in (table, lst, txt):
        lay.addWidget(x)
    w.resize(600, 400)
    w.show()
    app.processEvents()
    img = w.grab().toImage()
    w.close()
    if img.isNull() or img.width() == 0:
        pytest.skip("widget.grab() not usable on this offscreen platform")
    total = img.width() * img.height()
    white = 0
    for y in range(img.height()):
        for x in range(img.width()):
            c = img.pixelColor(x, y)
            if c.red() >= 245 and c.green() >= 245 and c.blue() >= 245:
                white += 1
    assert white / total < 0.005, f"{white / total:.2%} near-white pixels"

"""Tests for gui_tables (offscreen). Skipped without PyQt6."""
from __future__ import annotations

import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PyQt6")

from PyQt6.QtCore import Qt  # noqa: E402
from PyQt6.QtGui import QKeySequence  # noqa: E402
from PyQt6.QtWidgets import (  # noqa: E402
    QAbstractItemView, QApplication, QHeaderView, QTableWidget,
)

import gui_tables as gt  # noqa: E402

UR = Qt.ItemDataRole.UserRole


# qapp: the session-wide QApplication from tests/conftest.py


@pytest.fixture
def table(qapp):
    t = QTableWidget(0, 3)
    t.setHorizontalHeaderLabels(["Date", "Name", "Note"])
    gt.configure_readonly_table(t, stretch_col=2, sort_col=0)
    return t


def _rows():
    return [
        {"cells": ["2026-01-01", "a", "alpha long text"], "sort": ["2026-01-01", None, None],
         "data": {0: {UR: "id1"}}},
        {"cells": ["2026-03-01", "b", "beta"], "sort": ["2026-03-01", None, None],
         "data": {0: {UR: "id3"}}},
        {"cells": ["-", "c", "gamma"], "sort": [None, None, None], "tooltip": "row tip",
         "data": {0: {UR: "idN"}}},
        {"cells": ["2026-02-01", "d", "delta"], "sort": ["2026-02-01", None, None],
         "data": {0: {UR: "id2"}}},
    ]


def test_configuration(table):
    assert table.verticalHeader().isHidden()
    assert table.selectionBehavior() == QAbstractItemView.SelectionBehavior.SelectRows
    assert table.selectionMode() == QAbstractItemView.SelectionMode.SingleSelection
    assert table.editTriggers() == QAbstractItemView.EditTrigger.NoEditTriggers
    assert table.textElideMode() == Qt.TextElideMode.ElideRight
    assert not table.wordWrap()
    h = table.horizontalHeader()
    assert h.sectionResizeMode(0) == QHeaderView.ResizeMode.ResizeToContents
    assert h.sectionResizeMode(1) == QHeaderView.ResizeMode.ResizeToContents
    assert h.sectionResizeMode(2) == QHeaderView.ResizeMode.Stretch
    assert not h.stretchLastSection()
    assert table.isSortingEnabled()


def test_sort_desc_none_last(table):
    gt.fill_table(table, _rows())
    col = [table.item(r, 0).text() for r in range(4)]
    assert col == ["2026-03-01", "2026-02-01", "2026-01-01", "-"]
    table.sortByColumn(0, Qt.SortOrder.AscendingOrder)
    col = [table.item(r, 0).text() for r in range(4)]
    assert col == ["2026-01-01", "2026-02-01", "2026-03-01", "-"]


def test_text_fallback_case_insensitive(table):
    gt.fill_table(table, [{"cells": ["b", "", ""]}, {"cells": ["A", "", ""]}])
    table.sortByColumn(0, Qt.SortOrder.AscendingOrder)
    assert [table.item(r, 0).text() for r in range(2)] == ["A", "b"]


def test_tooltips(table):
    gt.fill_table(table, _rows())
    seen = set()
    for r in range(4):
        name = table.item(r, 1).text()
        seen.add(name)
        if name == "c":
            assert table.item(r, 2).toolTip() == "row tip"
        elif name == "a":
            assert table.item(r, 2).toolTip() == "alpha long text"
    assert seen == {"a", "b", "c", "d"}


def test_selection_kept_and_row_data(table):
    gt.fill_table(table, _rows())
    assert gt.selected_row_data(table) is None
    table.selectRow(1)  # sorted descending: row 1 is 2026-02-01 -> id2
    assert gt.selected_row_data(table) == "id2"
    gt.fill_table(table, list(reversed(_rows())))
    assert gt.selected_row_data(table) == "id2"
    assert gt.selected_row_data(table, col=1) is None


def test_copy(table, qapp):
    gt.fill_table(table, _rows())
    table.selectRow(0)
    gt.copy_selection_to_clipboard(table)
    assert QApplication.clipboard().text() == "2026-03-01\tb\talpha long text".replace(
        "alpha long text", "beta")
    assert table._copy_shortcut.key() == QKeySequence(QKeySequence.StandardKey.Copy)

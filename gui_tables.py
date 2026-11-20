"""Uniform configuration of read-only tables (instrument-standard look).

Device independent, no styling: colours and fonts come from the central theme.
"""
from __future__ import annotations

from PyQt6.QtCore import QEvent, QObject, Qt
from PyQt6.QtGui import QGuiApplication, QKeySequence, QShortcut
from PyQt6.QtWidgets import (
    QAbstractItemView,
    QHeaderView,
    QTableWidget,
    QTableWidgetItem,
)

SORT_ROLE: int = int(Qt.ItemDataRole.UserRole) + 32


def _is_descending(item: QTableWidgetItem) -> bool:
    table = item.tableWidget()
    if table is None:
        return False
    return table.horizontalHeader().sortIndicatorOrder() == Qt.SortOrder.DescendingOrder


class SortableItem(QTableWidgetItem):
    """Table item comparing by data(SORT_ROLE), falling back to text().

    Items whose sort key is None always end up at the bottom, independent of
    the sort order.
    """

    def __lt__(self, other: QTableWidgetItem) -> bool:
        a = self.data(SORT_ROLE)
        b = other.data(SORT_ROLE)
        has_a = a is not None
        has_b = b is not None
        if has_a != has_b:
            # Qt uses "<" for ascending and swaps the operands for descending,
            # so the None position has to be inverted for descending order.
            if _is_descending(self):
                return not has_a
            return has_a
        if has_a:
            try:
                return bool(a < b)
            except TypeError:
                return str(a).lower() < str(b).lower()
        return self.text().lower() < other.text().lower()


class _MinStretchFilter(QObject):
    """Makes the stretch column fixed-width if it would become too narrow."""

    def __init__(self, table: QTableWidget, col: int, min_px: int) -> None:
        super().__init__(table)
        self._table, self._col, self._min = table, col, min_px
        self._busy = False

    def eventFilter(self, obj, event):  # noqa: N802
        if event.type() == QEvent.Type.Resize and not self._busy:
            self._busy = True
            try:
                self._apply()
            finally:
                self._busy = False
        return False

    def _apply(self) -> None:
        header = self._table.horizontalHeader()
        others = sum(header.sectionSize(c) for c in range(header.count()) if c != self._col)
        avail = self._table.viewport().width() - others
        if avail < self._min:
            header.setSectionResizeMode(self._col, QHeaderView.ResizeMode.Fixed)
            header.resizeSection(self._col, self._min)
        elif header.sectionResizeMode(self._col) != QHeaderView.ResizeMode.Stretch:
            header.setSectionResizeMode(self._col, QHeaderView.ResizeMode.Stretch)


def configure_readonly_table(
    table: QTableWidget,
    *,
    stretch_col: int,
    sort_col: int | None = None,
    sort_order=Qt.SortOrder.DescendingOrder,
    min_stretch_px: int = 160,
) -> None:
    """Apply the standard read-only table behaviour.

    The minimum width of the stretch column is enforced by logic (not via
    setMinimumSectionSize, which would affect all columns): below the minimum
    the column becomes fixed-width and the horizontal scrollbar takes over.
    """
    table.verticalHeader().setVisible(False)
    table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
    table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
    table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
    table.setTextElideMode(Qt.TextElideMode.ElideRight)
    table.setWordWrap(False)
    table.setAlternatingRowColors(True)
    table.setCornerButtonEnabled(False)
    table.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)

    header = table.horizontalHeader()
    header.setStretchLastSection(False)
    header.setHighlightSections(False)
    for c in range(table.columnCount()):
        header.setSectionResizeMode(
            c,
            QHeaderView.ResizeMode.Stretch if c == stretch_col
            else QHeaderView.ResizeMode.ResizeToContents,
        )
    table.viewport().installEventFilter(_MinStretchFilter(table, stretch_col, min_stretch_px))

    if sort_col is not None:
        header.setSortIndicator(sort_col, sort_order)
        table.setSortingEnabled(True)
        table.sortByColumn(sort_col, sort_order)
    else:
        table.setSortingEnabled(False)

    sc = QShortcut(QKeySequence(QKeySequence.StandardKey.Copy), table)
    sc.setContext(Qt.ShortcutContext.WidgetWithChildrenShortcut)
    sc.activated.connect(lambda: copy_selection_to_clipboard(table))
    table._copy_shortcut = sc  # keep a reference


def fill_table(table: QTableWidget, rows: list[dict]) -> None:
    """Fill the table from row descriptions.

    Each row: {"cells": [str, ...], "sort": [Any|None, ...] (optional),
    "tooltip": str|None (optional), "data": {col: {role: value}} (optional)}.
    Sorting is suspended while filling; sort indicator and selection (matched
    by UserRole data in column 0) are restored afterwards.
    """
    header = table.horizontalHeader()
    was_sorting = table.isSortingEnabled()
    section = header.sortIndicatorSection()
    order = header.sortIndicatorOrder()
    keep = selected_row_data(table)

    table.setSortingEnabled(False)
    table.clearContents()
    table.setRowCount(len(rows))
    for r, row in enumerate(rows):
        cells = row.get("cells", [])
        sort = row.get("sort")
        tip = row.get("tooltip")
        extra = row.get("data") or {}
        for c, text in enumerate(cells):
            item = SortableItem(str(text))
            item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEditable)
            item.setToolTip(tip if tip else str(text))
            if sort is not None and c < len(sort) and sort[c] is not None:
                item.setData(SORT_ROLE, sort[c])
            for role, value in (extra.get(c) or {}).items():
                item.setData(role, value)
            table.setItem(r, c, item)

    if was_sorting:
        header.setSortIndicator(section, order)
        table.setSortingEnabled(True)
        table.sortByColumn(section, order)

    if keep is not None:
        for r in range(table.rowCount()):
            it = table.item(r, 0)
            if it is not None and it.data(Qt.ItemDataRole.UserRole) == keep:
                table.selectRow(r)
                break


def selected_row_data(table: QTableWidget, role=Qt.ItemDataRole.UserRole, col: int = 0):
    """Return the role value in column col of the selected row, else None."""
    sm = table.selectionModel()
    rows = sm.selectedRows() if sm is not None else []
    if not rows:
        return None
    item = table.item(rows[0].row(), col)
    return item.data(role) if item is not None else None


def copy_selection_to_clipboard(table: QTableWidget) -> None:
    """Copy the selected row as tab-separated text to the clipboard."""
    sm = table.selectionModel()
    rows = sm.selectedRows() if sm is not None else []
    if not rows:
        return
    r = rows[0].row()
    texts = []
    for c in range(table.columnCount()):
        it = table.item(r, c)
        texts.append(it.text() if it is not None else "")
    QGuiApplication.clipboard().setText("\t".join(texts))

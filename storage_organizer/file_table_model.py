"""ファイル一覧を表示する表モデル。

QAbstractTableModel は画面に見えている行だけを描画するので、
数十万件でも表示が重くなりにくい。
"""

from __future__ import annotations

from PySide6.QtCore import QAbstractTableModel, QModelIndex, Qt

from .formatting import format_bytes_exact, format_datetime, format_size
from .scanner import FileRecord

COL_NAME, COL_SIZE, COL_CATEGORY, COL_MTIME, COL_FOLDER = range(5)
HEADERS = ("ファイル名", "サイズ", "種類", "最終更新日時", "保存場所")

_SORT_KEYS = {
    COL_NAME: lambda r: r.name.casefold(),
    COL_SIZE: lambda r: r.size,
    COL_CATEGORY: lambda r: r.category,
    COL_MTIME: lambda r: r.mtime,
    COL_FOLDER: lambda r: r.folder.casefold(),
}


class FileTableModel(QAbstractTableModel):
    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._rows: list[FileRecord] = []

    # --- 必須メソッド ---------------------------------------------------
    def rowCount(self, parent: QModelIndex = QModelIndex()) -> int:
        return 0 if parent.isValid() else len(self._rows)

    def columnCount(self, parent: QModelIndex = QModelIndex()) -> int:
        return 0 if parent.isValid() else len(HEADERS)

    def data(self, index: QModelIndex, role: int = Qt.ItemDataRole.DisplayRole):
        if not index.isValid():
            return None
        record = self._rows[index.row()]
        column = index.column()

        if role == Qt.ItemDataRole.DisplayRole:
            if column == COL_NAME:
                return record.name
            if column == COL_SIZE:
                return format_size(record.size)
            if column == COL_CATEGORY:
                return record.category
            if column == COL_MTIME:
                return format_datetime(record.mtime)
            if column == COL_FOLDER:
                return record.folder
        elif role == Qt.ItemDataRole.ToolTipRole:
            if column == COL_SIZE:
                return format_bytes_exact(record.size)
            if column in (COL_NAME, COL_FOLDER):
                return record.path
        elif role == Qt.ItemDataRole.TextAlignmentRole:
            if column == COL_SIZE:
                return int(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        elif role == Qt.ItemDataRole.UserRole:
            return record
        return None

    def headerData(self, section: int, orientation: Qt.Orientation,
                   role: int = Qt.ItemDataRole.DisplayRole):
        if role == Qt.ItemDataRole.DisplayRole and orientation == Qt.Orientation.Horizontal:
            return HEADERS[section]
        return super().headerData(section, orientation, role)

    # --- 並び替え -------------------------------------------------------
    def sort(self, column: int, order: Qt.SortOrder = Qt.SortOrder.AscendingOrder) -> None:
        key = _SORT_KEYS.get(column)
        if key is None:
            return
        self.layoutAboutToBeChanged.emit()
        self._rows.sort(key=key, reverse=(order == Qt.SortOrder.DescendingOrder))
        self.layoutChanged.emit()

    # --- データ操作 -----------------------------------------------------
    def append_records(self, records: list[FileRecord]) -> None:
        if not records:
            return
        first = len(self._rows)
        self.beginInsertRows(QModelIndex(), first, first + len(records) - 1)
        self._rows.extend(records)
        self.endInsertRows()

    def clear(self) -> None:
        self.beginResetModel()
        self._rows = []
        self.endResetModel()

    def record_at(self, row: int) -> FileRecord:
        return self._rows[row]

"""メインウィンドウ。"""

from __future__ import annotations

from PySide6.QtCore import Qt, QThread, Slot
from PySide6.QtGui import QFontMetrics
from PySide6.QtWidgets import (
    QAbstractItemView,
    QFileDialog,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QMainWindow,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QTableView,
    QVBoxLayout,
    QWidget,
)

from .file_table_model import COL_CATEGORY, COL_MTIME, COL_NAME, COL_SIZE, FileTableModel
from .formatting import format_bytes_exact, format_duration, format_size
from .scan_worker import ScanWorker
from .scanner import ScanProgress, ScanResult

SIZE_NOTE = "※ サイズはファイルサイズの合計です。実際のディスク使用量とは異なる場合があります。"
MAX_TOOLTIP_SKIPS = 20


class ElidedLabel(QLabel):
    """長いパスを中央省略（…）で1行に収めるラベル。全文はツールチップに出す。"""

    def __init__(self, text: str = "", parent=None) -> None:
        super().__init__(parent)
        self._full_text = ""
        self.setMinimumWidth(100)
        self.set_full_text(text)

    def set_full_text(self, text: str) -> None:
        self._full_text = text
        self.setToolTip(text)
        self._update_elided()

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self._update_elided()

    def _update_elided(self) -> None:
        metrics = QFontMetrics(self.font())
        self.setText(metrics.elidedText(self._full_text, Qt.TextElideMode.ElideMiddle,
                                        max(self.width(), 100)))


class MainWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("ストレージ整理支援 - フォルダ容量調査")
        self.resize(1100, 700)

        self._selected_folder: str | None = None
        self._thread: QThread | None = None
        self._worker: ScanWorker | None = None
        self._last_result: ScanResult | None = None

        self._build_ui()
        self._set_idle_state()

    # --- 画面構築 -------------------------------------------------------
    def _build_ui(self) -> None:
        central = QWidget(self)
        layout = QVBoxLayout(central)

        # 1行目: フォルダ選択とスキャン操作
        top = QHBoxLayout()
        self.select_button = QPushButton("フォルダを選択…")
        self.select_button.clicked.connect(self._choose_folder)
        self.folder_label = ElidedLabel("フォルダが選択されていません")
        self.scan_button = QPushButton("スキャン開始")
        self.scan_button.clicked.connect(self._start_scan)
        self.cancel_button = QPushButton("キャンセル")
        self.cancel_button.clicked.connect(self._cancel_scan)
        top.addWidget(self.select_button)
        top.addWidget(self.folder_label, 1)
        top.addWidget(self.scan_button)
        top.addWidget(self.cancel_button)
        layout.addLayout(top)

        # 2行目: 進捗（総件数が分からないため割合は出さず、動作中表示だけにする）
        progress_row = QHBoxLayout()
        self.progress_bar = QProgressBar()
        self.progress_bar.setRange(0, 0)  # 不確定モード（パーセントなし）
        self.progress_bar.setTextVisible(False)
        self.progress_bar.setFixedWidth(160)
        self.current_dir_label = ElidedLabel("")
        progress_row.addWidget(self.progress_bar)
        progress_row.addWidget(self.current_dir_label, 1)
        layout.addLayout(progress_row)

        # 3行目: 集計
        self.summary_label = QLabel()
        self.summary_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        layout.addWidget(self.summary_label)

        # 表
        self.model = FileTableModel(self)
        self.table = QTableView()
        self.table.setModel(self.model)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setAlternatingRowColors(True)
        self.table.setWordWrap(False)
        self.table.verticalHeader().setVisible(False)
        # 行の高さを固定すると大量行でも描画が速い
        self.table.verticalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Fixed)
        self.table.verticalHeader().setDefaultSectionSize(22)
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        header.setStretchLastSection(True)
        header.setSortIndicatorShown(True)
        self.table.setColumnWidth(COL_NAME, 320)
        self.table.setColumnWidth(COL_SIZE, 100)
        self.table.setColumnWidth(COL_CATEGORY, 190)
        self.table.setColumnWidth(COL_MTIME, 140)
        layout.addWidget(self.table, 1)

        note = QLabel(SIZE_NOTE)
        note.setStyleSheet("color: gray;")
        layout.addWidget(note)

        self.setCentralWidget(central)
        self._update_summary(files=0, total_size=0, skipped=0, links=0)

    # --- 状態切り替え ---------------------------------------------------
    def _set_idle_state(self) -> None:
        self.select_button.setEnabled(True)
        self.scan_button.setEnabled(self._selected_folder is not None)
        self.cancel_button.setEnabled(False)
        self.progress_bar.setVisible(False)
        self.table.setSortingEnabled(self.model.rowCount() > 0)

    def _set_scanning_state(self) -> None:
        self.select_button.setEnabled(False)
        self.scan_button.setEnabled(False)
        self.cancel_button.setEnabled(True)
        self.progress_bar.setVisible(True)
        # 走査中は行が追加され続けるので並び替えを止め、完了後に容量順で並べる
        self.table.setSortingEnabled(False)

    def is_scanning(self) -> bool:
        return self._thread is not None

    # --- 操作 -----------------------------------------------------------
    @Slot()
    def _choose_folder(self) -> None:
        folder = QFileDialog.getExistingDirectory(self, "スキャンするフォルダを選択")
        if folder:
            self.set_folder(folder)

    def set_folder(self, folder: str) -> None:
        self._selected_folder = folder
        self.folder_label.set_full_text(folder)
        self.scan_button.setEnabled(not self.is_scanning())

    @Slot()
    def _start_scan(self) -> None:
        if self._selected_folder is None or self.is_scanning():
            return
        self.model.clear()
        self._update_summary(files=0, total_size=0, skipped=0, links=0)
        self.current_dir_label.set_full_text("スキャンを開始しています…（並び替えは完了後にできます）")
        self._set_scanning_state()

        self._thread = QThread(self)
        self._worker = ScanWorker(self._selected_folder)
        self._worker.moveToThread(self._thread)
        self._thread.started.connect(self._worker.run)
        self._worker.batch_ready.connect(self._on_batch)
        self._worker.progress.connect(self._on_progress)
        self._worker.scan_finished.connect(self._on_finished)
        self._worker.scan_failed.connect(self._on_failed)
        self._worker.scan_finished.connect(self._thread.quit)
        self._worker.scan_failed.connect(self._thread.quit)
        self._thread.finished.connect(self._on_thread_finished)
        self._thread.start()

    @Slot()
    def _cancel_scan(self) -> None:
        if self._worker is not None:
            self._worker.cancel()
            self.cancel_button.setEnabled(False)
            self.current_dir_label.set_full_text("キャンセルしています…")

    # --- ワーカーからの通知 ---------------------------------------------
    @Slot(object)
    def _on_batch(self, records) -> None:
        self.model.append_records(records)

    @Slot(object)
    def _on_progress(self, progress: ScanProgress) -> None:
        self._update_summary(progress.files, progress.total_size, progress.skipped,
                             progress.links, dirs=progress.dirs)
        if self.cancel_button.isEnabled():
            self.current_dir_label.set_full_text(f"調査中: {progress.current_dir}")

    @Slot(object)
    def _on_finished(self, result: ScanResult) -> None:
        self._last_result = result
        state = "キャンセルしました（途中までの結果を表示）" if result.cancelled else "スキャン完了"
        self.current_dir_label.set_full_text(
            f"{state}  /  所要時間 {format_duration(result.elapsed)}  /  "
            f"調査したフォルダ {result.dirs:,} 個"
        )
        self._update_summary(result.files, result.total_size, result.skipped, result.links,
                             dirs=result.dirs, skipped_details=result.skipped_details)
        # 完了後は容量の大きい順に並べる
        self.table.horizontalHeader().setSortIndicator(COL_SIZE, Qt.SortOrder.DescendingOrder)
        self.table.setSortingEnabled(True)

    @Slot(str)
    def _on_failed(self, message: str) -> None:
        self.current_dir_label.set_full_text("スキャン中にエラーが発生しました")
        QMessageBox.warning(self, "スキャンエラー", message)

    @Slot()
    def _on_thread_finished(self) -> None:
        if self._worker is not None:
            self._worker.deleteLater()
        if self._thread is not None:
            self._thread.deleteLater()
        self._worker = None
        self._thread = None
        self._set_idle_state()

    # --- 表示更新 -------------------------------------------------------
    def _update_summary(self, files: int, total_size: int, skipped: int, links: int,
                        dirs: int | None = None,
                        skipped_details: list[tuple[str, str]] | None = None) -> None:
        text = (
            f"調査済みファイル: {files:,} 件　　"
            f"ファイルサイズ合計: {format_size(total_size)}　　"
            f"アクセスできずスキップ: {skipped:,} か所　　"
            f"たどらなかったリンク・ジャンクション: {links:,} 個"
        )
        self.summary_label.setText(text)

        tooltip = [f"ファイルサイズ合計: {format_bytes_exact(total_size)}"]
        if dirs is not None:
            tooltip.append(f"調査したフォルダ: {dirs:,} 個")
        if skipped_details:
            tooltip.append("")
            tooltip.append("スキップした場所（先頭のみ）:")
            for path, reason in skipped_details[:MAX_TOOLTIP_SKIPS]:
                tooltip.append(f"  {path}  ({reason})")
            if skipped > MAX_TOOLTIP_SKIPS:
                tooltip.append(f"  …ほか {skipped - MAX_TOOLTIP_SKIPS:,} か所")
        self.summary_label.setToolTip("\n".join(tooltip))

    # --- 終了処理 -------------------------------------------------------
    def closeEvent(self, event) -> None:
        if self._worker is not None:
            self._worker.cancel()
        if self._thread is not None:
            self._thread.quit()
            self._thread.wait(5000)
        super().closeEvent(event)

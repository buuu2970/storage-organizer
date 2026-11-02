"""メインウィンドウと「ファイル調査」ページ（既存のスキャン画面）。"""

from __future__ import annotations

from PySide6.QtCore import Qt, QThread, QTimer, Signal, Slot
from PySide6.QtGui import QFontMetrics
from PySide6.QtWidgets import (
    QAbstractItemView,
    QFileDialog,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QStackedWidget,
    QTableView,
    QVBoxLayout,
    QWidget,
)

from . import pages
from .db import Database
from .file_table_model import COL_CATEGORY, COL_MTIME, COL_NAME, COL_SIZE, FileTableModel
from .formatting import format_bytes_exact, format_duration, format_size
from .organizer import OrganizerPage
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


class ScanPage(QWidget):
    """フォルダを選んでサブフォルダまでスキャンし、ファイルを一覧表示する（既存機能）。"""

    scan_completed = Signal(object, object)  # ScanResult, list[FileRecord]

    def __init__(self, parent=None) -> None:
        super().__init__(parent)

        self._selected_folder: str | None = None
        self._thread: QThread | None = None
        self._worker: ScanWorker | None = None
        self._last_result: ScanResult | None = None

        self._build_ui()
        self._set_idle_state()

    # --- 画面構築 -------------------------------------------------------
    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)

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
        self.scan_completed.emit(result, self.model.records())

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
    def shutdown(self) -> None:
        if self._worker is not None:
            self._worker.cancel()
        if self._thread is not None:
            self._thread.quit()
            self._thread.wait(5000)


# =============================================================================
# メインウィンドウ
# =============================================================================
ORGANIZER_CATEGORIES = ["すべて", "重複", "類似画像", "録画確認候補", "大容量", "保護", "保留", "削除候補"]


class MainWindow(QMainWindow):
    def __init__(self, db: Database | None = None, start_autoscan: bool = True) -> None:
        super().__init__()
        self.setWindowTitle("録画・画像の整理支援（RecSort AI 試作）")
        self.resize(1360, 860)
        self.ask_initial_learning = True  # 従来のフォルダ指定スキャン後に初期学習を案内するか（テストでは False）
        self.ctx = pages.AppContext(db or Database())
        c = self.ctx
        self.organizer = OrganizerPage(c.db, c.tools)
        self.scan_page = ScanPage()
        self.dashboard = pages.DashboardPage(c)
        self.video_list = pages.VideoListPage(c)
        self.detail = pages.VideoDetailPage(c)
        self.genre_page = pages.GenrePage(c)
        self.initial = pages.InitialLearningPage(c)
        self.unclassified = pages.UnclassifiedPage(c)
        self.curation = pages.CurationPage(c)
        self.review = pages.ReviewPage(c)
        self.duplicates = pages.DuplicatePage(c)
        self.compress = pages.CompressPage(c)
        self.settings = pages.SettingsPage(c)
        # 左のメニュー: (見出し) / (整理の分類) / (画面)
        nav_spec = [("header", "整理"), *[("category", name) for name in ORGANIZER_CATEGORIES],
                    ("header", "動画AI（試作）"),
                    ("page", "ダッシュボード", self.dashboard), ("page", "動画一覧", self.video_list),
                    ("page", "動画詳細", self.detail), ("page", "ジャンル管理", self.genre_page),
                    ("page", "初期AI学習", self.initial), ("page", "未分類動画", self.unclassified),
                    ("page", "厳選", self.curation), ("page", "再確認待ち", self.review),
                    ("page", "動画の重複グループ", self.duplicates), ("page", "圧縮", self.compress),
                    ("header", "その他"),
                    ("page", "フォルダ指定の容量調査", self.scan_page), ("page", "設定", self.settings)]
        self.page_widgets = [self.organizer] + [spec[2] for spec in nav_spec if spec[0] == "page"]

        central = QWidget()
        outer = QVBoxLayout(central)
        body = QHBoxLayout()
        self.nav = QListWidget()
        self.nav.setFixedWidth(170)
        self._nav_targets: list = []
        for spec in nav_spec:
            if spec[0] == "header":
                item = QListWidgetItem(spec[1])
                item.setFlags(Qt.ItemFlag.NoItemFlags)
                font = item.font()
                font.setBold(True)
                item.setFont(font)
                self._nav_targets.append(None)
            elif spec[0] == "category":
                item = QListWidgetItem("　" + spec[1])
                self._nav_targets.append(("category", spec[1]))
            else:
                item = QListWidgetItem("　" + spec[1])
                self._nav_targets.append(("page", spec[2]))
            self.nav.addItem(item)
        self.stack = QStackedWidget()
        for w in self.page_widgets:
            self.stack.addWidget(w)
        body.addWidget(self.nav)
        body.addWidget(self.stack, 1)
        outer.addLayout(body, 1)
        self.task_bar = pages.TaskBar(c)
        outer.addWidget(self.task_bar)
        self.setCentralWidget(central)

        self.nav.currentRowChanged.connect(self._on_nav)
        self.scan_page.scan_completed.connect(self._on_scan_completed)
        c.open_video_requested.connect(self.open_video)
        c.open_compress_requested.connect(self.open_compress)
        self.detail.back_requested.connect(lambda: self.show_page(self.video_list))
        self.organizer.runner.finished.connect(lambda _: c.data_changed.emit())
        self.show_category("すべて")
        if start_autoscan and c.db.get_setting("auto_scan_on_start", "1") != "0":
            QTimer.singleShot(300, self.organizer.start_scan)  # 前回の結果を表示してから更新を始める

    def _on_nav(self, row: int) -> None:
        target = self._nav_targets[row] if 0 <= row < len(self._nav_targets) else None
        if target is None:
            return
        if target[0] == "category":
            self.stack.setCurrentWidget(self.organizer)
            self.organizer.set_category(target[1])
        else:
            self.stack.setCurrentWidget(target[1])

    def show_category(self, name: str) -> None:
        self.nav.setCurrentRow(self._nav_targets.index(("category", name)))

    def show_page(self, widget) -> None:
        self.nav.setCurrentRow(self._nav_targets.index(("page", widget)))

    def open_video(self, video_id: int) -> None:
        self.detail.load(video_id)
        self.show_page(self.detail)

    def open_compress(self, video_id: int) -> None:
        self.compress.select_video(video_id)
        self.show_page(self.compress)

    def _on_scan_completed(self, result, records) -> None:
        videos = [r for r in records if r.category == "動画"]
        added, invalidated = self.ctx.db.register_videos(
            videos, result.root, complete_scan=not result.cancelled and result.skipped == 0)
        self.ctx.data_changed.emit()
        note = f"動画 {len(videos)} 本を登録しました（新規 {added} 本"
        note += f"、内容が変わったため解析をやり直す動画 {invalidated} 本）" if invalidated else "）"
        self.scan_page.current_dir_label.set_full_text(
            self.scan_page.current_dir_label.toolTip() + "  /  " + note)
        if (videos and self.ask_initial_learning
                and self.ctx.db.get_setting("initial_state") is None):
            box = QMessageBox(self)
            box.setWindowTitle("初期AI学習")
            box.setText("あなたの動画の分類方法をAIに教えませんか？\n"
                        "特徴の異なる動画を最大5本選ぶので、ジャンルを付けてください。")
            start = box.addButton("初期学習を開始", QMessageBox.ButtonRole.AcceptRole)
            box.addButton("今はスキップ", QMessageBox.ButtonRole.RejectRole)
            box.exec()
            if box.clickedButton() is start:
                self.show_page(self.initial)
                self.initial.start()
            else:
                self.ctx.db.set_setting("initial_state", "skipped")
                self.ctx.data_changed.emit()

    def closeEvent(self, event) -> None:
        self.organizer.shutdown()
        self.scan_page.shutdown()
        self.ctx.shutdown()
        for page in (self.detail, self.initial, self.compress):
            page.player.stop()
        self.ctx.db.close()
        super().closeEvent(event)

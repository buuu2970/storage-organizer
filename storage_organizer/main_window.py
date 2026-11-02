"""メインウィンドウと「ファイル調査」ページ（既存のスキャン画面）。"""

from __future__ import annotations

from PySide6.QtCore import QSize, Qt, QThread, QTimer, Signal, Slot
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
    QScrollArea,
    QStackedWidget,
    QTabWidget,
    QTableView,
    QVBoxLayout,
    QWidget,
)

from . import pages
from .db import Database
from .file_table_model import COL_CATEGORY, COL_MTIME, COL_NAME, COL_SIZE, FileTableModel
from .formatting import format_bytes_exact, format_duration, format_size
from . import theme
from .organizer import HomePage, LocationsDialog, MediaBrowser, MediaState, ScanStatus
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
NAV_ITEMS = [("home", "ホーム"), ("files", "ファイル一覧"), ("sparkle", "整理のおすすめ"), ("video", "動画AI"),
             ("settings", "設定")]
RECOMMEND_KEYS = ["重複", "類似画像", "録画確認候補", "大容量", "削除候補"]


class TitledPage(QWidget):
    """タイトルと説明を付けて既存の画面を包む。"""

    def __init__(self, title: str, caption: str, inner: QWidget) -> None:
        super().__init__()
        self.setObjectName("Page")
        v = QVBoxLayout(self)
        v.setContentsMargins(theme.S3, theme.S2, theme.S3, theme.S1)
        v.setSpacing(theme.S1)
        t = QLabel(title)
        theme.set_role(t, "title")
        c = QLabel(caption)
        theme.set_role(c, "sub")
        c.setWordWrap(True)
        v.addWidget(t)
        v.addWidget(c)
        v.addWidget(inner, 1)
        self.inner = inner


class VideoAIPage(QWidget):
    """動画AI（ジャンル分類・初期学習・未分類・厳選・圧縮など）をまとめた画面。動画詳細は一覧から開く。"""

    def __init__(self, tabs: list[tuple[str, QWidget]], detail: QWidget) -> None:
        super().__init__()
        self.setObjectName("Page")
        v = QVBoxLayout(self)
        v.setContentsMargins(theme.S3, theme.S2, theme.S3, theme.S1)
        v.setSpacing(theme.S1)
        t = QLabel("動画AI（試作）")
        theme.set_role(t, "title")
        c = QLabel("動画のジャンル分類と厳選を学習する試作機能です。動画の詳細は「動画一覧」でダブルクリックして開きます。")
        theme.set_role(c, "sub")
        c.setWordWrap(True)
        v.addWidget(t)
        v.addWidget(c)
        self.stack = QStackedWidget()
        self.tabs = QTabWidget()
        self.tabs.setDocumentMode(True)
        self.tabs.setUsesScrollButtons(True)
        for name, w in tabs:
            self.tabs.addTab(w, name)
        self.detail = detail
        self.stack.addWidget(self.tabs)
        self.stack.addWidget(detail)
        v.addWidget(self.stack, 1)

    def show_widget(self, widget: QWidget) -> None:
        if widget is self.detail:
            self.stack.setCurrentWidget(self.detail)
        else:
            self.stack.setCurrentWidget(self.tabs)
            self.tabs.setCurrentWidget(widget)

    def current_page(self) -> QWidget:
        return self.detail if self.stack.currentWidget() is self.detail else self.tabs.currentWidget()


class MainWindow(QMainWindow):
    def __init__(self, db: Database | None = None, start_autoscan: bool = True) -> None:
        super().__init__()
        self.setWindowTitle("RecSort — 録画・画像の整理支援")
        self.resize(1280, 800)
        self.setMinimumSize(880, 520)
        self.ask_initial_learning = True  # 従来のフォルダ指定スキャン後に初期学習を案内するか（テストでは False）
        self.ctx = pages.AppContext(db or Database())
        c = self.ctx
        self.state = MediaState(c.db, c.tools)
        self.home = HomePage(self.state)
        self.files = MediaBrowser(self.state, "files")
        self.recommend = MediaBrowser(self.state, "recommend")
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
        self.video_ai = VideoAIPage([
            ("概要", self.dashboard), ("動画一覧", self.video_list), ("初期学習", self.initial),
            ("未分類", self.unclassified), ("厳選", self.curation), ("再確認待ち", self.review),
            ("ジャンル管理", self.genre_page), ("動画の重複", self.duplicates), ("圧縮", self.compress),
            ("フォルダから登録", self.scan_page)], self.detail)
        settings_scroll = QScrollArea()
        settings_scroll.setWidgetResizable(True)
        settings_scroll.setWidget(self.settings)
        self.settings_page = TitledPage("設定", "FFmpeg の場所、自動スキャンの対象・対象外、AI のリセットなど。",
                                        settings_scroll)
        self.sections = [self.home, self.files, self.recommend, self.video_ai, self.settings_page]
        self.page_widgets = self.sections

        central = QWidget()
        central.setObjectName("Page")
        body = QHBoxLayout(central)
        body.setContentsMargins(0, 0, 0, 0)
        body.setSpacing(0)
        side = QWidget()
        sv = QVBoxLayout(side)
        sv.setContentsMargins(theme.S1, theme.S2, theme.S1, theme.S1)
        self.brand = QLabel("RecSort")
        theme.set_role(self.brand, "subtitle")
        self.brand.setContentsMargins(theme.S1, 0, 0, theme.S1)
        self.nav = QListWidget()
        self.nav.setObjectName("Nav")
        self.nav.setIconSize(QSize(20, 20))
        self.nav.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        for icon_name, text in NAV_ITEMS:
            item = QListWidgetItem(theme.icon(icon_name, theme.TEXT, 20), text)
            item.setToolTip(text)
            item.setData(Qt.ItemDataRole.UserRole, text)
            self.nav.addItem(item)
        sv.addWidget(self.brand)
        sv.addWidget(self.nav, 1)
        self.side = side
        self.stack = QStackedWidget()
        for w in self.sections:
            self.stack.addWidget(w)
        body.addWidget(side)
        body.addWidget(self.stack, 1)
        self.setCentralWidget(central)
        self.scan_status = ScanStatus(self.state)
        self.task_bar = pages.TaskBar(c)
        self.statusBar().addWidget(self.scan_status, 1)
        self.statusBar().addPermanentWidget(self.task_bar)
        self.statusBar().setSizeGripEnabled(False)
        self._compact = None
        self._apply_compact(self.width() < 1100)

        self.nav.currentRowChanged.connect(self._on_nav)
        self.scan_status.rescan.clicked.connect(self.start_scan)
        self.home.start_organizing.connect(self.show_category)
        for w in (self.home, self.files, self.recommend):
            w.request_locations.connect(self.open_locations)
        self.home.request_files.connect(lambda: self.show_section(self.files))
        self.recommend.request_files.connect(lambda: self.show_section(self.files))
        self.files.request_files.connect(lambda: self.show_section(self.files))
        self.scan_page.scan_completed.connect(self._on_scan_completed)
        c.open_video_requested.connect(self.open_video)
        c.open_compress_requested.connect(self.open_compress)
        self.detail.back_requested.connect(lambda: self.show_page(self.video_list))
        self.state.runner.finished.connect(lambda _: c.data_changed.emit())
        self.nav.setCurrentRow(0)
        # 初回（一度も調査していない）は「調べる場所を選ぶ」から始める。2回目以降は起動時に自動で更新する
        if (start_autoscan and c.db.get_setting("auto_scan_on_start", "1") != "0"
                and c.db.get_setting("scan_gen") is not None):
            QTimer.singleShot(300, self.start_scan)

    # --- 画面の切り替え -------------------------------------------------------
    def _on_nav(self, row: int) -> None:
        if 0 <= row < len(self.sections):
            self.stack.setCurrentWidget(self.sections[row])
            if self.sections[row] is not self.files:
                self.files.stop_media()
            if self.sections[row] is not self.recommend:
                self.recommend.stop_media()

    def show_section(self, widget) -> None:
        self.nav.setCurrentRow(self.sections.index(widget))

    def show_page(self, widget) -> None:
        """既存の画面へ移動する（動画AIの各画面・設定・整理画面）。"""
        if widget in self.sections:
            self.show_section(widget)
        elif widget is self.settings:
            self.show_section(self.settings_page)
        else:
            self.video_ai.show_widget(widget)
            self.show_section(self.video_ai)

    def show_category(self, name: str) -> None:
        """整理の分類（重複・類似画像など）やファイルの判断状態へ移動する。"""
        if name in RECOMMEND_KEYS:
            self.recommend.set_category(name)
            self.show_section(self.recommend)
        else:
            self.files.set_category(name)
            self.show_section(self.files)

    def open_locations(self) -> None:
        if LocationsDialog(self.ctx.db, self).exec():
            self.start_scan()

    def start_scan(self) -> None:
        self.state.start_scan()

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self._apply_compact(self.width() < 1100)

    def _apply_compact(self, compact: bool) -> None:
        """画面幅が狭いときは左メニューをアイコンだけにする。"""
        if compact == self._compact:
            return
        self._compact = compact
        self.side.setFixedWidth(64 if compact else 200)
        self.brand.setText("" if compact else "RecSort")
        for i in range(self.nav.count()):
            item = self.nav.item(i)
            item.setText("" if compact else item.data(Qt.ItemDataRole.UserRole))

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
        self.files.stop_media()
        self.recommend.stop_media()
        self.state.close()
        self.scan_page.shutdown()
        self.ctx.shutdown()
        for page in (self.detail, self.initial, self.compress):
            page.player.stop()
        self.ctx.db.close()
        super().closeEvent(event)

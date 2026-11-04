"""メインウィンドウと「ファイル調査」ページ（既存のスキャン画面）。"""

from __future__ import annotations

from PySide6.QtCore import QCoreApplication, QSize, Qt, QThread, QTimer, Signal, Slot
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
    QTableView,
    QVBoxLayout,
    QWidget,
)

from . import media
from .store import Store
from .file_table_model import COL_CATEGORY, COL_MTIME, COL_NAME, COL_SIZE, FileTableModel
from .formatting import format_bytes_exact, format_duration, format_size
from . import theme
from .organizer import (CapacityPage, FlowPage, HomePage, LocationsDialog, MediaBrowser, MediaState, PlanPage,
                        ScanStatus, SettingsPage)
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
NAV_ITEMS = [("home", "ホーム"), ("target", "整理候補"), ("files", "ファイル一覧"), ("sparkle", "その他の整理"),
             ("settings", "設定"), ("video", "動画AI（試作）")]
RECOMMEND_KEYS = ["重複", "類似画像", "録画確認候補", "大容量", "削除候補"]


class TitledPage(QWidget):
    """タイトルと説明を付けて画面を包む。"""

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


NAV_ITEMS = [("home", "ホーム"), ("target", "整理する"), ("files", "ファイルを見る"), ("settings", "設定")]
RECOMMEND_KEYS = ["重複", "類似画像", "録画確認候補", "大容量", "削除候補"]


class MainWindow(QMainWindow):
    def __init__(self, db: Store | None = None, start_autoscan: bool = True) -> None:
        super().__init__()
        from . import __version__
        self.setWindowTitle(f"RecSort {__version__} — 録画の整理")
        self.resize(1280, 800)
        self.setMinimumSize(880, 520)
        self.store = db or Store()
        self.tools = lambda: media.find_ffmpeg(self.store.get_setting("ffmpeg_dir"))
        self.state = MediaState(self.store, self.tools)
        self.home = HomePage(self.state)
        self.plan = PlanPage(self.state)
        self.flow = FlowPage(self.state, self.plan)
        self.capacity = CapacityPage(self.state)
        self.files = MediaBrowser(self.state, "files")
        self.recommend = MediaBrowser(self.state, "recommend")
        self.settings = SettingsPage(self.state)
        settings_scroll = QScrollArea()
        settings_scroll.setWidgetResizable(True)
        settings_scroll.setWidget(self.settings)
        self.settings_page = TitledPage("設定", f"RecSort バージョン {__version__}。データの保存先: {self.store.data_dir}",
                                        settings_scroll)
        # メニューに出す 4 つの画面と、そこから開く補助の画面（容量の内訳 → ホーム、画像の整理 → ファイルを見る）
        self.nav_pages = [self.home, self.flow, self.files, self.settings_page]
        self.sub_pages = {self.capacity: self.home, self.recommend: self.files}
        self.sections = self.nav_pages + list(self.sub_pages)
        self.page_widgets = self.sections
        self.files.add_header_link("画像の整理（補助）", lambda: self.show_section(self.recommend))
        self.recommend.add_header_link("ファイルを見るへ戻る", lambda: self.show_section(self.files))
        self.capacity.add_back_link("ホームへ戻る", lambda: self.show_section(self.home))

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
        self.statusBar().addWidget(self.scan_status, 1)
        self.statusBar().setSizeGripEnabled(False)
        self._compact = None
        self._apply_compact(self.width() < 1100)

        self.nav.currentRowChanged.connect(self._on_nav)
        self.scan_status.rescan.clicked.connect(self.start_scan)
        self.scan_status.capacity_requested.connect(lambda: self.show_section(self.capacity))
        self.home.start_plan.connect(lambda gb, drive: self.show_plan(gb if gb > 0 else 0.0, drive))
        self.home.open_group.connect(self.open_group)
        self.home.request_capacity.connect(lambda: self.show_section(self.capacity))
        self.flow.result.go_home.connect(lambda: self.show_section(self.home))
        for w in (self.home, self.plan, self.files, self.recommend, self.capacity):
            w.request_locations.connect(self.open_locations)
        for w in (self.home, self.plan):
            w.request_settings.connect(lambda: self.show_section(self.settings_page))
        self.home.request_files.connect(lambda: self.show_section(self.files))
        self.recommend.request_files.connect(lambda: self.show_section(self.files))
        self.files.request_files.connect(lambda: self.show_section(self.files))
        self.settings.changed.connect(self._settings_changed)
        self.nav.setCurrentRow(0)
        # 初回（一度も調査していない）は「PCを調べる」から始める。2回目以降は起動時に自動で更新する
        if (start_autoscan and self.store.get_setting("auto_scan_on_start", "1") != "0"
                and self.store.get_setting("scan_gen") is not None):
            QTimer.singleShot(300, self.start_scan)

    # 互換（テスト・既存の呼び出し）
    @property
    def ctx(self):
        return self

    @property
    def db(self) -> Store:
        return self.store

    @property
    def data_dir(self):
        return self.store.data_dir

    # --- 画面の切り替え -------------------------------------------------------
    def _on_nav(self, row: int) -> None:
        if 0 <= row < len(self.nav_pages):
            self._show(self.nav_pages[row])

    def _show(self, widget) -> None:
        self.stack.setCurrentWidget(widget)
        for page in (self.files, self.recommend, self.flow):
            if widget is not page:
                page.stop_media()

    def show_section(self, widget) -> None:
        if widget in self.nav_pages:
            self.nav.setCurrentRow(self.nav_pages.index(widget))
            self._show(widget)
            return
        parent = self.sub_pages[widget]  # 補助の画面: メニューは元の項目を選択中のままにする
        self.nav.blockSignals(True)
        self.nav.setCurrentRow(self.nav_pages.index(parent))
        self.nav.blockSignals(False)
        self._show(widget)

    def show_page(self, widget) -> None:
        if widget in self.sections:
            self.show_section(widget)
        elif widget is self.settings:
            self.show_section(self.settings_page)
        elif widget is self.plan:
            self.show_plan(None)

    def open_group(self, kind: str, key) -> None:
        """ホームのおすすめから、比較して選ぶ画面へ進む。"""
        self.show_section(self.flow)
        self.flow.open_compare(kind, key)

    def show_plan(self, target_gb: float | None = None, drive: str | None = None) -> None:
        """整理候補を表示する（ホームで入力した目標容量・ドライブで提案する）。"""
        if target_gb is not None:
            self.plan.set_target_gb(target_gb, drive)
        self.show_section(self.flow)
        self.flow.show_candidates()

    def show_category(self, name: str) -> None:
        if name in RECOMMEND_KEYS:
            self.recommend.set_category(name)
            self.show_section(self.recommend)
        else:
            self.files.set_category(name)
            self.show_section(self.files)

    def _settings_changed(self) -> None:
        """設定（FFmpeg の場所など）の変更後に、案内と提案を更新する。"""
        self.home.refresh_status()
        self.plan.replan()

    def open_locations(self) -> None:
        if LocationsDialog(self.store, self).exec():
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

    def closeEvent(self, event) -> None:
        final = self.flow.final
        if final.is_running():  # ごみ箱へ移動中: ファイル単位で止め、処理中のファイルが終わるまで待つ
            final.cancel()
            final.wait(120000)
            QCoreApplication.processEvents()
        for page in (self.files, self.recommend, self.flow):
            page.stop_media()
        self.flow.compare.stop()
        self.state.close()
        self.store.close()
        super().closeEvent(event)

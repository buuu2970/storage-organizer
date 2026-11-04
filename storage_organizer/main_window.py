"""メインウィンドウ（ホーム・ファイル一覧・整理のおすすめ・削除候補・設定の 5 画面）。"""

from __future__ import annotations

from PySide6.QtCore import QCoreApplication, QSize, Qt, QTimer
from PySide6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QScrollArea,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from . import media
from .store import Store
from . import theme
from .organizer import (CandidatesPage, CapacityPage, FlowPage, HomePage, LocationsDialog, MediaBrowser, MediaState,
                        PlanPage, ScanStatus, SettingsPage)

SIZE_NOTE = "※ サイズはファイルサイズの合計です。実際のディスク使用量とは異なる場合があります。"


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


# メニュー（利用の流れの順: 何が容量を使っているか → 一覧 → おすすめ → 削除候補 → 設定）
NAV_ITEMS = [("home", "ホーム"), ("files", "ファイル一覧"), ("sparkle", "整理のおすすめ"), ("trash", "削除候補"),
             ("settings", "設定")]


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
        self.candidates = CandidatesPage(self.state)
        self.settings = SettingsPage(self.state)
        settings_scroll = QScrollArea()
        settings_scroll.setWidgetResizable(True)
        settings_scroll.setWidget(self.settings)
        self.settings_page = TitledPage("設定", f"RecSort バージョン {__version__}。データの保存先: {self.store.data_dir}",
                                        settings_scroll)
        # メニューに出す 5 つの画面と、そこから開く補助の画面（容量の内訳 → ホーム）
        self.nav_pages = [self.home, self.files, self.flow, self.candidates, self.settings_page]
        self.sub_pages = {self.capacity: self.home}
        self.sections = self.nav_pages + list(self.sub_pages)
        self.page_widgets = self.sections
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
        self.home.request_candidates.connect(self.show_candidates)
        self.flow.to_candidates.connect(self.show_candidates)
        self.candidates.go_home.connect(lambda: self.show_section(self.home))
        self.candidates.go_recommend.connect(lambda: self.show_plan(None))
        for w in (self.home, self.plan, self.files, self.capacity, self.candidates):
            w.request_locations.connect(self.open_locations)
        for w in (self.home, self.plan):
            w.request_settings.connect(lambda: self.show_section(self.settings_page))
        self.home.request_files.connect(lambda: self.show_section(self.files))
        self.files.request_files.connect(lambda: self.show_section(self.files))
        self.candidates.request_files.connect(lambda: self.show_section(self.files))
        self.settings.changed.connect(self._settings_changed)
        self.nav.setCurrentRow(0)
        # 起動時に自動で調査する（初回は調べる範囲を確認してから始める）
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
        for page in (self.files, self.flow, self.candidates):
            if widget is not page:
                page.stop_media()
        if widget is self.candidates:
            self.candidates.show_list()

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
        """整理のおすすめを表示する（ホームで入力した目標容量・ドライブで提案する）。"""
        if target_gb is not None:
            self.plan.set_target_gb(target_gb, drive)
        self.show_section(self.flow)
        self.flow.show_candidates()

    def show_candidates(self) -> None:
        self.show_section(self.candidates)

    def show_category(self, name: str) -> None:
        if name == "削除候補":
            self.show_candidates()
            return
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
        final = self.candidates.final
        if final.is_running():  # ごみ箱へ移動中: ファイル単位で止め、処理中のファイルが終わるまで待つ
            final.cancel()
            final.wait(120000)
            QCoreApplication.processEvents()
        for page in (self.files, self.flow, self.candidates):
            page.stop_media()
        self.flow.compare.stop()
        self.state.close()
        self.store.close()
        super().closeEvent(event)

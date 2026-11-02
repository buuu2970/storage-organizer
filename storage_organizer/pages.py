"""RecSort AI の追加画面（試作）。

重い処理（解析・ハッシュ・再学習・圧縮）は AppContext.run_task で別スレッド実行する。
"""

from __future__ import annotations

import os
from functools import partial
from pathlib import Path

from PySide6.QtCore import (QAbstractTableModel, QCoreApplication, QModelIndex, QObject, Qt, QThread, QUrl,
                            Signal)
from PySide6.QtGui import QPixmap
from PySide6.QtWidgets import (
    QAbstractItemView, QButtonGroup, QCheckBox, QComboBox, QFileDialog, QFormLayout, QGridLayout,
    QGroupBox, QHBoxLayout, QHeaderView, QInputDialog, QLabel, QLineEdit, QListWidget,
    QListWidgetItem, QMessageBox, QProgressBar, QPushButton, QRadioButton, QScrollArea, QSlider,
    QSpinBox, QSplitter, QTableView, QTreeWidget, QTreeWidgetItem, QVBoxLayout, QWidget,
)

from . import ai, media
from . import samples as samples_mod
from .db import ANSWERED, CURATION_CHOICES, INITIAL_STATES, SKIPPED, Database, loads
from .samples import INITIAL_SAMPLE_COUNT, STATUS_TEXT, SampleStore
from .formatting import format_bytes_exact, format_datetime, format_size
from .scan_worker import TaskWorker

AI_NOTE = ("AI判定は試作です。映像・音声の簡易な数値特徴（明るさ・動き・色・音量など）の類似度で推定しており、"
           "音楽・歌唱・会話などの意味は判定していません。")


# =============================================================================
# 共有状態とバックグラウンド処理
# =============================================================================
class AppContext(QObject):
    data_changed = Signal()
    open_video_requested = Signal(int)
    open_compress_requested = Signal(int)
    task_state_changed = Signal(bool)
    task_progress = Signal(object)

    def __init__(self, db: Database, samples_root=None, profile_name: str = "") -> None:
        super().__init__()
        self.db = db
        # 初期学習用サンプル（全プロファイル共通。回答はこのプロファイルの db に保存する）
        self.samples_root = Path(samples_root) if samples_root else db.data_dir / "samples"
        self.samples = SampleStore(self.samples_root)
        self.profile_name = profile_name
        self._thread: QThread | None = None
        self._worker: TaskWorker | None = None
        self._on_done = None
        self._on_error = None
        self.task_label = ""

    @property
    def data_dir(self):
        return self.db.data_dir

    def tools(self) -> media.FFmpegTools:
        return media.find_ffmpeg(self.db.get_setting("ffmpeg_dir"))

    def max_frames(self) -> int:
        return int(self.db.get_setting("max_frames", "40"))

    def is_busy(self) -> bool:
        return self._thread is not None

    def run_task(self, label: str, fn, on_done=None, on_error=None, parent=None) -> bool:
        if self.is_busy():
            QMessageBox.information(parent, "処理中", f"「{self.task_label}」の実行中です。終わるまでお待ちください。")
            return False
        self.task_label = label
        self._on_done, self._on_error = on_done, on_error
        self._thread = QThread(self)
        self._worker = TaskWorker(fn)
        self._worker.moveToThread(self._thread)
        self._thread.started.connect(self._worker.run)
        self._worker.progress.connect(self.task_progress.emit)
        self._worker.task_finished.connect(self._finished)
        self._worker.task_failed.connect(self._failed)
        self._worker.task_finished.connect(self._thread.quit)
        self._worker.task_failed.connect(self._thread.quit)
        self._thread.finished.connect(self._cleanup)
        self._thread.start()
        self.task_state_changed.emit(True)
        return True

    def cancel_task(self) -> None:
        if self._worker is not None:
            self._worker.cancel()

    def _finished(self, result) -> None:
        if self._on_done:
            self._on_done(result)
        self.data_changed.emit()

    def _failed(self, message: str) -> None:
        if self._on_error:
            self._on_error(message)
        else:
            QMessageBox.warning(None, "エラー", f"「{self.task_label}」でエラーが発生しました。\n{message}")
        self.data_changed.emit()

    def _cleanup(self) -> None:
        for obj in (self._worker, self._thread):
            if obj is not None:
                obj.deleteLater()
        self._worker = self._thread = None
        self.task_state_changed.emit(False)

    def shutdown(self) -> None:
        if self._worker is not None:
            self._worker.cancel()
        if self._thread is not None:
            self._thread.quit()
            self._thread.wait(10000)

    def detach(self) -> None:
        """プロファイル切り替え用: 実行中の処理を止め、完了時の処理を呼ばないようにして待つ。

        処理は開始時のプロファイルのデータフォルダにしか書き込まないため、
        次のプロファイルへ結果が混ざることはない。
        """
        self._on_done = self._on_error = None
        thread = self._thread
        self.shutdown()
        if thread is not None:
            thread.wait()
        QCoreApplication.processEvents()
        self.samples.close()

    # --- よく使う処理 ------------------------------------------------------
    def analyze(self, video_ids: list[int], on_done=None, parent=None) -> bool:
        tools = self.tools()
        if not tools.available:
            QMessageBox.warning(parent, "FFmpeg が必要です",
                                "解析には FFmpeg（ffmpeg と ffprobe）が必要です。README の手順で導入し、"
                                "設定画面で場所を確認してください。基本機能はそのまま使えます。")
            return False
        if not video_ids:
            QMessageBox.information(parent, "解析", "解析する動画がありません。")
            return False
        data_dir, frames = self.data_dir, self.max_frames()
        return self.run_task("動画の解析", lambda c, r: ai.task_analyze(data_dir, tools, video_ids, frames, c, r),
                             on_done, parent=parent)

    def retrain(self, parent=None, on_done=None) -> bool:
        data_dir = self.data_dir

        def done(result):
            ev = result.get("evaluation") or {}
            text = result.get("message", "")
            if ev:
                text += "\n\n" + describe_evaluation(ev)
            if result.get("classified"):
                c = result["classified"]
                text += f"\n\n再評価: 分類済み {c['分類済み']} 本 / 未分類 {c['未分類']} 本"
            (QMessageBox.information if result.get("adopted") else QMessageBox.warning)(
                parent, "再学習" + ("（採用）" if result.get("adopted") else "（以前のモデルを維持）"), text)
            if on_done:
                on_done(result)
        samples_root = self.samples_root
        return self.run_task("再学習", lambda c, r: ai.task_retrain(data_dir, c, r, samples_root),
                             done, parent=parent)

    def analyze_samples(self, on_done=None, parent=None, quiet: bool = False) -> bool:
        """共通サンプルの解析（特徴量キャッシュ）。回答はしなくても再生・分類はできる。"""
        tools = self.tools()
        if not tools.available:
            if not quiet:
                QMessageBox.warning(parent, "FFmpeg が必要です",
                                    "初期サンプルの解析（学習に使う特徴量の計算）には FFmpeg が必要です。"
                                    "回答の入力と保存はそのままできます。")
            return False
        root, frames = self.samples_root, self.max_frames()
        return self.run_task("初期サンプルの解析",
                             lambda c, r: samples_mod.task_analyze_samples(root, tools, frames, c, r),
                             on_done, parent=parent)


def initial_state_text(ctx: AppContext) -> str:
    """初期学習の進捗（未実施・途中・完了・スキップ）と回答数。"""
    state = INITIAL_STATES.get(ctx.db.initial_state(), "未実施")
    ids = {r["sample_id"] for r in ctx.samples.samples()}
    answers = ctx.db.sample_answers()
    answered = sum(1 for s, a in answers.items() if s in ids and a["status"] == ANSWERED)
    skipped = sum(1 for s, a in answers.items() if s in ids and a["status"] == SKIPPED)
    if not ids:
        return state
    return f"{state}（回答済み {answered} / {len(ids)} 本・スキップ {skipped} 本）"


def describe_evaluation(ev: dict) -> str:
    if ev.get("precision") is None and "status" in ev:
        return f"評価: {ev['status']}（教師データ {ev.get('n', 0)} 件）"
    p = "-" if ev.get("precision") is None else f"{ev['precision']:.2f}"
    r = "-" if ev.get("recall") is None else f"{ev['recall']:.2f}"
    return (f"評価（1件ずつ外して予測）: 適合率 {p} / 再現率 {r} / 誤った提案 {ev.get('wrong_suggestions', 0)} 件 / "
            f"保留率 {ev.get('abstain_rate', 0):.0%}\n{ev.get('status', '')}")


# =============================================================================
# 共通部品
# =============================================================================
_pixmap_cache: dict[tuple, QPixmap] = {}


def thumb_pixmap(path: str | None, w: int, h: int) -> QPixmap | None:
    if not path or not os.path.exists(path):
        return None
    key = (path, os.path.getmtime(path), w, h)
    if key not in _pixmap_cache:
        pm = QPixmap(path)
        if pm.isNull():
            return None
        _pixmap_cache[key] = pm.scaled(w, h, Qt.AspectRatioMode.KeepAspectRatio,
                                       Qt.TransformationMode.SmoothTransformation)
    return _pixmap_cache[key]


class BasePage(QWidget):
    """表示されたときだけ再読み込みする（隠れている間の無駄な更新を避ける）。"""

    def __init__(self, ctx: AppContext, parent=None) -> None:
        super().__init__(parent)
        self.ctx = ctx
        self._dirty = True
        ctx.data_changed.connect(self._on_data_changed)

    def _on_data_changed(self) -> None:
        self._dirty = True
        if self.isVisible():
            self.refresh()
            self._dirty = False

    def showEvent(self, event) -> None:
        super().showEvent(event)
        if self._dirty:
            self.refresh()
            self._dirty = False

    def refresh(self) -> None:
        pass


def note_label(text: str) -> QLabel:
    label = QLabel(text)
    label.setWordWrap(True)
    label.setStyleSheet("color: #886000;")
    return label


class GenreChecklist(QWidget):
    """ジャンルの複数選択。"""

    def __init__(self, db: Database, parent=None) -> None:
        super().__init__(parent)
        self.db = db
        self._boxes: dict[int, QCheckBox] = {}
        self._grid = QGridLayout(self)
        self._grid.setContentsMargins(0, 0, 0, 0)
        self.reload()

    def reload(self) -> None:
        checked = self.checked_ids()
        for box in self._boxes.values():
            box.deleteLater()
        self._boxes.clear()
        for i, g in enumerate(self.db.genres()):
            box = QCheckBox(g["name"] + ("（学習対象外）" if g["exclude_from_training"] else ""))
            box.setChecked(g["id"] in checked)
            self._grid.addWidget(box, i // 3, i % 3)
            self._boxes[g["id"]] = box

    def checked_ids(self) -> set[int]:
        return {gid for gid, box in self._boxes.items() if box.isChecked()}

    def set_checked(self, ids) -> None:
        ids = set(ids)
        for gid, box in self._boxes.items():
            box.setChecked(gid in ids)


class VideoPlayer(QWidget):
    """Qt Multimedia による再生・停止・シーク。"""

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        self.error_label = QLabel()
        self.error_label.setStyleSheet("color: #b00020;")
        self.error_label.setWordWrap(True)
        try:
            from PySide6.QtMultimedia import QAudioOutput, QMediaPlayer
            from PySide6.QtMultimediaWidgets import QVideoWidget
        except ImportError as e:  # Qt Multimedia が使えない環境でも他の機能は動かす
            self.player = None
            self.error_label.setText(f"動画プレビューを利用できません: {e}")
            layout.addWidget(self.error_label)
            return
        self.player = QMediaPlayer(self)
        self.audio = QAudioOutput(self)
        self.player.setAudioOutput(self.audio)
        self.video = QVideoWidget()
        self.video.setMinimumSize(320, 180)
        self.player.setVideoOutput(self.video)
        self.play_button = QPushButton("再生")
        self.stop_button = QPushButton("停止")
        self.slider = QSlider(Qt.Orientation.Horizontal)
        self.time_label = QLabel("0:00 / 0:00")
        controls = QHBoxLayout()
        for w in (self.play_button, self.stop_button, self.slider, self.time_label):
            controls.addWidget(w)
        layout.addWidget(self.video, 1)
        layout.addLayout(controls)
        layout.addWidget(self.error_label)
        self.play_button.clicked.connect(self.toggle)
        self.stop_button.clicked.connect(self.player.stop)
        self.slider.sliderMoved.connect(self.player.setPosition)
        self.player.positionChanged.connect(self._on_position)
        self.player.durationChanged.connect(lambda d: self.slider.setRange(0, d))
        self.player.playbackStateChanged.connect(self._on_state)
        self.player.errorOccurred.connect(self._on_error)

    def set_source(self, path: str | None) -> None:
        if self.player is None:
            return
        self.player.stop()
        self.error_label.clear()
        self.player.setSource(QUrl.fromLocalFile(path) if path else QUrl())

    def toggle(self) -> None:
        if self.player.playbackState() == self.player.PlaybackState.PlayingState:
            self.player.pause()
        else:
            self.player.play()

    def stop(self) -> None:
        if self.player is not None:
            self.player.stop()

    def _on_position(self, pos: int) -> None:
        if not self.slider.isSliderDown():
            self.slider.setValue(pos)
        fmt = lambda ms: f"{ms // 60000}:{ms // 1000 % 60:02d}"  # noqa: E731
        self.time_label.setText(f"{fmt(pos)} / {fmt(self.player.duration())}")

    def _on_state(self, state) -> None:
        playing = state == self.player.PlaybackState.PlayingState
        self.play_button.setText("一時停止" if playing else "再生")

    def _on_error(self, error, message: str = "") -> None:
        self.error_label.setText(f"再生できません（非対応の形式か、ファイルを読み込めません）: {message}")


# =============================================================================
# 動画の表
# =============================================================================
def build_video_rows(db: Database) -> list[dict]:
    names = db.genre_names()
    user = db.all_user_genres()
    tags = db.all_adopted_tags()
    dups = ai.duplicate_groups(db)
    rows = []
    for r in db.videos():
        ug = user.get(r["id"], set())
        ai_ids = [e["genre_id"] for e in loads(r["ai_genres"], [])] if not ug else []
        if ug:
            genre_text = "、".join(names[g] for g in sorted(ug) if g in names)
            ai_text = "手動分類済み"
        else:
            genre_text = ("AI: " + "、".join(names.get(g, "?") for g in ai_ids)) if ai_ids else ""
            ai_text = {"分類済み": f"AI分類 (v{r['ai_model_version']})", "未分類": "未分類"}.get(
                r["ai_state"], "未判定")
        rec = ai.recommend(r, dups)
        rec_reason = rec["reason"] + "".join(f" ／ 追加候補: {n}（{why}）" for n, why in rec["extra"])
        thumbs = loads(r["thumbs"], [])
        rows.append({
            "id": r["id"], "name": r["name"], "size": r["size"], "ext": r["ext"], "mtime": r["mtime"],
            "folder": r["folder"], "path": r["path"], "genres": genre_text, "user_genres": ug,
            "ai_genres": set(ai_ids), "tags": "、".join(tags.get(r["id"], [])),
            "tag_list": tags.get(r["id"], []), "favorite": bool(r["favorite"]),
            "protected": bool(r["protected"]), "ai": ai_text, "ai_state": r["ai_state"],
            "analysis": r["analysis_state"], "rec": rec["main"], "rec_reason": rec_reason,
            "answer": "", "suggest": "", "suggest_reason": "", "thumb": thumbs[len(thumbs) // 2] if thumbs else None,
            "cloud_only": bool(r["cloud_only"]),
        })
    return rows


VCOLS = [("thumb", ""), ("name", "ファイル名"), ("size", "サイズ"), ("ext", "形式"),
         ("mtime", "最終更新日時"), ("folder", "保存場所"), ("genres", "ジャンル"), ("tags", "タグ"),
         ("favorite", "★"), ("protected", "保護"), ("ai", "AI判定"), ("rec", "整理推薦"),
         ("rec_reason", "推薦理由"), ("answer", "あなたの回答"), ("suggest", "厳選AIの提案"),
         ("suggest_reason", "厳選AIの理由")]
NON_CURATION_HIDDEN = ("answer", "suggest", "suggest_reason")


class VideoTableModel(QAbstractTableModel):
    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.rows: list[dict] = []
        self._sort: tuple[int, Qt.SortOrder] | None = None

    def rowCount(self, parent=QModelIndex()):
        return 0 if parent.isValid() else len(self.rows)

    def columnCount(self, parent=QModelIndex()):
        return 0 if parent.isValid() else len(VCOLS)

    def headerData(self, section, orientation, role=Qt.ItemDataRole.DisplayRole):
        if role == Qt.ItemDataRole.DisplayRole and orientation == Qt.Orientation.Horizontal:
            return VCOLS[section][1]
        return None

    def data(self, index, role=Qt.ItemDataRole.DisplayRole):
        if not index.isValid():
            return None
        row = self.rows[index.row()]
        key = VCOLS[index.column()][0]
        if role == Qt.ItemDataRole.DisplayRole:
            if key == "thumb":
                return None if row["thumb"] else ("クラウドのみ" if row["cloud_only"] else row["analysis"])
            if key == "size":
                return format_size(row["size"])
            if key == "mtime":
                return format_datetime(row["mtime"])
            if key == "favorite":
                return "★" if row["favorite"] else ""
            if key == "protected":
                return "保護" if row["protected"] else ""
            return row[key]
        if role == Qt.ItemDataRole.DecorationRole and key == "thumb":
            return thumb_pixmap(row["thumb"], 80, 45)
        if role == Qt.ItemDataRole.ToolTipRole:
            if key == "rec":
                return row["rec_reason"]
            if key in ("rec_reason", "answer", "suggest_reason", "genres"):
                return row[key] or None
            if key == "suggest":
                return row["suggest_reason"] or None
            if key == "size":
                return format_bytes_exact(row["size"])
            if key in ("name", "folder"):
                return row["path"]
        if role == Qt.ItemDataRole.TextAlignmentRole and key == "size":
            return int(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        return None

    def set_rows(self, rows: list[dict]) -> None:
        self.beginResetModel()
        self.rows = rows
        if self._sort:
            self._apply_sort(*self._sort)
        self.endResetModel()

    def sort(self, column, order=Qt.SortOrder.AscendingOrder):
        if VCOLS[column][0] == "thumb":
            return
        self._sort = (column, order)
        self.layoutAboutToBeChanged.emit()
        self._apply_sort(column, order)
        self.layoutChanged.emit()

    def _apply_sort(self, column, order):
        key = VCOLS[column][0]
        self.rows.sort(key=lambda r: (str(r[key]).casefold() if isinstance(r[key], str) else r[key]),
                       reverse=order == Qt.SortOrder.DescendingOrder)

    def video_id(self, row: int) -> int:
        return self.rows[row]["id"]


def make_video_table(model: VideoTableModel, hidden: tuple[str, ...] = ()) -> QTableView:
    table = QTableView()
    table.setModel(model)
    table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
    table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
    table.setAlternatingRowColors(True)
    table.setWordWrap(False)
    table.setSortingEnabled(True)
    table.verticalHeader().setVisible(False)
    table.verticalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Fixed)
    table.verticalHeader().setDefaultSectionSize(50)
    table.horizontalHeader().setStretchLastSection(True)
    widths = {"thumb": 90, "name": 220, "size": 80, "ext": 50, "mtime": 120, "folder": 200,
              "genres": 160, "tags": 120, "favorite": 30, "protected": 40, "ai": 110, "rec": 80,
              "rec_reason": 240, "answer": 130, "suggest": 90, "suggest_reason": 320}
    for i, (key, _) in enumerate(VCOLS):
        table.setColumnWidth(i, widths.get(key, 80))
        table.setColumnHidden(i, key in hidden)
    table.sortByColumn([k for k, _ in VCOLS].index("size"), Qt.SortOrder.DescendingOrder)
    return table


def selected_ids(table: QTableView, model: VideoTableModel) -> list[int]:
    return [model.video_id(i.row()) for i in table.selectionModel().selectedRows()]


# =============================================================================
# ダッシュボード
# =============================================================================
class DashboardPage(BasePage):
    def __init__(self, ctx, parent=None):
        super().__init__(ctx, parent)
        layout = QVBoxLayout(self)
        self.stats_label = QLabel()
        self.stats_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.ai_label = QLabel()
        self.ai_label.setWordWrap(True)
        self.tool_label = QLabel()
        self.tool_label.setWordWrap(True)
        box1, box2, box3 = QGroupBox("登録済みの動画"), QGroupBox("AI（試作）"), QGroupBox("環境")
        QVBoxLayout(box1).addWidget(self.stats_label)
        v2 = QVBoxLayout(box2)
        v2.addWidget(self.ai_label)
        v2.addWidget(note_label(AI_NOTE))
        QVBoxLayout(box3).addWidget(self.tool_label)
        buttons = QHBoxLayout()
        self.analyze_button = QPushButton("未解析の動画を解析")
        self.retrain_button = QPushButton("再学習")
        buttons.addWidget(self.analyze_button)
        buttons.addWidget(self.retrain_button)
        buttons.addStretch()
        layout.addWidget(note_label("使い方: 「ファイル調査」でフォルダをスキャンすると動画が登録されます。"
                                    "ファイルの変更・削除は行いません。"))
        for w in (box1, box2, box3):
            layout.addWidget(w)
        layout.addLayout(buttons)
        layout.addStretch()
        self.analyze_button.clicked.connect(self._analyze)
        self.retrain_button.clicked.connect(lambda: self.ctx.retrain(self))

    def refresh(self):
        s = self.ctx.db.stats()
        self.stats_label.setText(
            f"動画 {s['videos']:,} 本（ファイルサイズ合計 {format_size(s['total_size'])}）\n"
            f"解析済み {s['analyzed']:,} 本 / 解析失敗 {s['failed']:,} 本\n"
            f"手動分類 {s['manual']:,} 本 / AI分類 {s['ai_classified']:,} 本 / 未分類 {s['unclassified']:,} 本\n"
            f"お気に入り {s['favorite']:,} 本 / 保護 {s['protected']:,} 本"
            + (f"\n見つからなくなった動画 {s['missing']:,} 本（一覧には表示しません）" if s["missing"] else ""))
        model = self.ctx.db.active_model()
        state = initial_state_text(self.ctx)
        if self.ctx.db.initial_state() != "done":
            state += "（初期学習を終えるまで、ジャンル分類の精度は十分ではありません）"
        if model:
            ev = loads(model["evaluation"], {})
            text = (f"採用中のモデル: v{model['version']}（教師データ {model['n_samples']} 件）\n"
                    + describe_evaluation(ev))
        else:
            text = "採用中のモデル: なし（初期学習または再学習で作成されます）"
        curate = [f"{ai.scope_label(self.ctx.db, sc)} v{m['version']}（{m['n_samples']}件）"
                  for sc, m in self.ctx.db.active_models().items() if sc != "classify"]
        self.ai_label.setText(f"初期学習: {state}\nジャンル分類AI — {text}\n未反映の教師データ: {s['pending']} 件"
                              f"（「再学習」で反映）\n厳選AI: {'、'.join(curate) if curate else '未学習'}"
                              f"\n再確認待ちの厳選回答: {s['review']} 件")
        tools = self.ctx.tools()
        self.tool_label.setText(
            f"FFmpeg: {tools.ffmpeg or '見つかりません'}\nFFprobe: {tools.ffprobe or '見つかりません'}\n"
            + ("" if tools.available else "→ 解析・サムネイル・重複以外の圧縮には FFmpeg が必要です。")
            + (f"\nプロファイル: {self.ctx.profile_name}" if self.ctx.profile_name else "")
            + f"\nデータ保存先: {self.ctx.data_dir}"
            + f"\n初期学習用サンプル: {self.ctx.samples.sample_dir() or '未指定（初期AI学習の画面で指定）'}")

    def _analyze(self):
        ids = [r["id"] for r in self.ctx.db.videos()
               if r["analysis_state"] in ("未解析", "失敗") and not r["cloud_only"]]
        self.ctx.analyze(ids, parent=self)


# =============================================================================
# 動画一覧
# =============================================================================
class VideoListPage(BasePage):
    def __init__(self, ctx, parent=None):
        super().__init__(ctx, parent)
        layout = QVBoxLayout(self)
        filters = QHBoxLayout()
        self.search = QLineEdit()
        self.search.setPlaceholderText("ファイル名・保存場所で検索")
        self.genre_filter = QComboBox()
        self.tag_filter = QComboBox()
        self.state_filter = QComboBox()
        self.state_filter.addItems(["すべて", "お気に入り", "保護", "手動分類済み", "AI分類", "未分類",
                                    "未解析", "解析失敗", "重複確認", "圧縮候補"])
        for label, w in (("検索", self.search), ("ジャンル", self.genre_filter),
                         ("タグ", self.tag_filter), ("状態", self.state_filter)):
            filters.addWidget(QLabel(label))
            filters.addWidget(w, 2 if w is self.search else 1)
        layout.addLayout(filters)
        self.model = VideoTableModel(self)
        self.table = make_video_table(self.model, hidden=("rec_reason", *NON_CURATION_HIDDEN))
        layout.addWidget(self.table, 1)
        bottom = QHBoxLayout()
        self.count_label = QLabel()
        self.open_button = QPushButton("詳細を開く")
        self.analyze_button = QPushButton("選択した動画を解析")
        self.analyze_all_button = QPushButton("未解析をすべて解析")
        bottom.addWidget(self.count_label, 1)
        for b in (self.open_button, self.analyze_button, self.analyze_all_button):
            bottom.addWidget(b)
        layout.addLayout(bottom)
        self._all_rows: list[dict] = []
        self.search.textChanged.connect(self.apply_filter)
        for combo in (self.genre_filter, self.tag_filter, self.state_filter):
            combo.currentIndexChanged.connect(self.apply_filter)
        self.table.doubleClicked.connect(lambda idx: self.ctx.open_video_requested.emit(
            self.model.video_id(idx.row())))
        self.open_button.clicked.connect(self._open_selected)
        self.analyze_button.clicked.connect(
            lambda: self.ctx.analyze(selected_ids(self.table, self.model), parent=self))
        self.analyze_all_button.clicked.connect(lambda: self.ctx.analyze(
            [r["id"] for r in self._all_rows if r["analysis"] in ("未解析", "失敗") and not r["cloud_only"]],
            parent=self))

    def refresh(self):
        self._all_rows = build_video_rows(self.ctx.db)
        self._reload_combo(self.genre_filter, [(g["id"], g["name"]) for g in self.ctx.db.genres()])
        self._reload_combo(self.tag_filter, [(t["name"], t["name"]) for t in self.ctx.db.tags()])
        self.apply_filter()

    @staticmethod
    def _reload_combo(combo: QComboBox, items) -> None:
        current = combo.currentData()
        combo.blockSignals(True)
        combo.clear()
        combo.addItem("すべて", None)
        for data, text in items:
            combo.addItem(text, data)
        i = combo.findData(current)
        combo.setCurrentIndex(max(i, 0))
        combo.blockSignals(False)

    def apply_filter(self):
        text = self.search.text().strip().casefold()
        gid, tag, state = self.genre_filter.currentData(), self.tag_filter.currentData(), \
            self.state_filter.currentText()
        state_test = {
            "お気に入り": lambda r: r["favorite"], "保護": lambda r: r["protected"],
            "手動分類済み": lambda r: bool(r["user_genres"]), "AI分類": lambda r: r["ai_state"] == "分類済み"
            and not r["user_genres"], "未分類": lambda r: r["ai_state"] == "未分類" and not r["user_genres"],
            "未解析": lambda r: r["analysis"] == "未解析", "解析失敗": lambda r: r["analysis"] == "失敗",
            "重複確認": lambda r: r["rec"] == "重複確認", "圧縮候補": lambda r: r["rec"] == "圧縮候補",
        }.get(state, lambda r: True)
        rows = [r for r in self._all_rows
                if (not text or text in r["name"].casefold() or text in r["folder"].casefold())
                and (gid is None or gid in r["user_genres"] or gid in r["ai_genres"])
                and (tag is None or tag in r["tag_list"]) and state_test(r)]
        self.model.set_rows(rows)
        self.count_label.setText(f"{len(rows):,} / {len(self._all_rows):,} 本  "
                                 f"（表示中の合計 {format_size(sum(r['size'] for r in rows))}）")

    def _open_selected(self):
        ids = selected_ids(self.table, self.model)
        if ids:
            self.ctx.open_video_requested.emit(ids[0])


# =============================================================================
# 動画詳細
# =============================================================================
class VideoDetailPage(BasePage):
    back_requested = Signal()

    def __init__(self, ctx, parent=None):
        super().__init__(ctx, parent)
        self.video_id: int | None = None
        layout = QVBoxLayout(self)
        top = QHBoxLayout()
        self.back_button = QPushButton("← 動画一覧へ")
        self.title = QLabel("動画が選択されていません")
        self.title.setStyleSheet("font-weight: bold;")
        self.reanalyze_button = QPushButton("再解析")
        self.compress_button = QPushButton("圧縮へ")
        top.addWidget(self.back_button)
        top.addWidget(self.title, 1)
        top.addWidget(self.reanalyze_button)
        top.addWidget(self.compress_button)
        layout.addLayout(top)
        splitter = QSplitter()
        left = QWidget()
        lv = QVBoxLayout(left)
        self.player = VideoPlayer()
        lv.addWidget(self.player, 1)
        thumbs = QHBoxLayout()
        self.thumb_labels = [QLabel() for _ in range(3)]
        for t in self.thumb_labels:
            t.setFixedSize(160, 90)
            t.setAlignment(Qt.AlignmentFlag.AlignCenter)
            t.setStyleSheet("background: #222; color: #aaa;")
            thumbs.addWidget(t)
        lv.addLayout(thumbs)
        self.path_label = QLabel()
        self.path_label.setWordWrap(True)
        self.path_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        lv.addWidget(self.path_label)
        splitter.addWidget(left)

        right = QWidget()
        rv = QVBoxLayout(right)
        flags = QHBoxLayout()
        self.favorite_box = QCheckBox("お気に入り")
        self.protect_box = QCheckBox("保護（整理候補・圧縮の対象外）")
        flags.addWidget(self.favorite_box)
        flags.addWidget(self.protect_box)
        rv.addLayout(flags)
        genre_box = QGroupBox("ジャンル（ユーザーの確定）")
        gv = QVBoxLayout(genre_box)
        self.genres = GenreChecklist(ctx.db)
        self.save_genres_button = QPushButton("ジャンルを保存（教師データに登録）")
        gv.addWidget(self.genres)
        gv.addWidget(self.save_genres_button)
        suggest = QHBoxLayout()
        self.ai_suggest_label = QLabel()
        self.ai_suggest_label.setWordWrap(True)
        self.approve_ai_button = QPushButton("AIの提案を承認")
        self.reject_ai_button = QPushButton("AIの提案を却下")
        suggest.addWidget(self.ai_suggest_label, 1)
        suggest.addWidget(self.approve_ai_button)
        suggest.addWidget(self.reject_ai_button)
        gv.addLayout(suggest)
        gv.addWidget(note_label("AIのジャンルは確定前の提案です。承認すると確定ジャンル（教師データ）になります。"
                                "修正する場合は上で選び直して保存してください。"))
        rv.addWidget(genre_box)
        tag_box = QGroupBox("タグ")
        tv = QVBoxLayout(tag_box)
        self.tag_list = QListWidget()
        self.tag_list.setMaximumHeight(110)
        tag_add = QHBoxLayout()
        self.new_tag = QLineEdit()
        self.new_tag.setPlaceholderText("新しいタグ")
        self.add_tag_button = QPushButton("追加")
        tag_add.addWidget(self.new_tag)
        tag_add.addWidget(self.add_tag_button)
        self.ai_tag_list = QListWidget()
        self.ai_tag_list.setMaximumHeight(80)
        ai_tag_buttons = QHBoxLayout()
        self.ai_tag_buttons = {s: QPushButton(s) for s in ("採用", "却下", "保留")}
        ai_tag_buttons.addWidget(QLabel("AIのタグ候補:"))
        for b in self.ai_tag_buttons.values():
            ai_tag_buttons.addWidget(b)
        tv.addWidget(QLabel("手動タグ（チェックで設定）"))
        tv.addWidget(self.tag_list)
        tv.addLayout(tag_add)
        tv.addLayout(ai_tag_buttons)
        tv.addWidget(self.ai_tag_list)
        tv.addWidget(note_label("AIが候補にするのは測定できるタグ（黒画面・静止画が多い・無音区間が多い・長時間）"
                                "だけです。歌唱・会話・BGM・字幕の判定は未実装です。"))
        rv.addWidget(tag_box)
        self.info = QLabel()
        self.info.setWordWrap(True)
        self.info.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.info.setAlignment(Qt.AlignmentFlag.AlignTop)
        rv.addWidget(self.info, 1)
        self.info.setMinimumWidth(1)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        scroll.setWidget(right)
        splitter.addWidget(scroll)
        splitter.setSizes([560, 520])
        layout.addWidget(splitter, 1)

        self.back_button.clicked.connect(self.back_requested.emit)
        self.reanalyze_button.clicked.connect(
            lambda: self.video_id and self.ctx.analyze([self.video_id], parent=self))
        self.compress_button.clicked.connect(
            lambda: self.video_id and self.ctx.open_compress_requested.emit(self.video_id))
        self.favorite_box.toggled.connect(partial(self._set_flag, "favorite"))
        self.protect_box.toggled.connect(partial(self._set_flag, "protected"))
        self.save_genres_button.clicked.connect(self._save_genres)
        self.approve_ai_button.clicked.connect(self._approve_ai)
        self.reject_ai_button.clicked.connect(self._reject_ai)
        self.tag_list.itemChanged.connect(self._tag_toggled)
        self.add_tag_button.clicked.connect(self._add_tag)
        for status, b in self.ai_tag_buttons.items():
            b.clicked.connect(partial(self._set_ai_tag, status))

    def load(self, video_id: int) -> None:
        changed = video_id != self.video_id
        self.video_id = video_id
        row = self.ctx.db.video(video_id)
        if row is None:
            return
        if changed:
            self.player.set_source(None if row["cloud_only"] else row["path"])
            self.genres.reload()
            self.genres.set_checked(self.ctx.db.user_genres(video_id))
        self._fill(row)

    def refresh(self):
        if self.video_id is not None:
            self.genres.reload()
            self.load(self.video_id)

    def hideEvent(self, event):
        self.player.stop()
        super().hideEvent(event)

    def _fill(self, row) -> None:
        db = self.ctx.db
        self.title.setText(row["name"] + ("　（保護中）" if row["protected"] else ""))
        self.path_label.setText(f"{row['path']}\n{format_size(row['size'])}（{format_bytes_exact(row['size'])}）"
                                f" / 更新 {format_datetime(row['mtime'])}"
                                + ("\n※ クラウド上のみのファイルのため、解析・再生は行いません" if row["cloud_only"] else ""))
        thumbs = loads(row["thumbs"], [])
        for i, label in enumerate(self.thumb_labels):
            pm = thumb_pixmap(thumbs[i], 160, 90) if i < len(thumbs) else None
            label.setPixmap(pm) if pm else label.setText("サムネイルなし\n（解析で作成）")
        for box, key in ((self.favorite_box, "favorite"), (self.protect_box, "protected")):
            box.blockSignals(True)
            box.setChecked(bool(row[key]))
            box.blockSignals(False)
        self.compress_button.setEnabled(not row["protected"])
        # タグ
        tags = {t["id"]: t for t in db.video_tags(row["id"])}
        self.tag_list.blockSignals(True)
        self.tag_list.clear()
        for t in db.tags():
            item = QListWidgetItem(t["name"])
            item.setData(Qt.ItemDataRole.UserRole, t["name"])
            item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            vt = tags.get(t["id"])
            item.setCheckState(Qt.CheckState.Checked if vt and vt["status"] == "採用" else Qt.CheckState.Unchecked)
            self.tag_list.addItem(item)
        self.tag_list.blockSignals(False)
        self.ai_tag_list.clear()
        for vt in tags.values():
            if vt["source"] == "ai":
                item = QListWidgetItem(f"{vt['name']}（{vt['status']}）")
                item.setData(Qt.ItemDataRole.UserRole, vt["id"])
                self.ai_tag_list.addItem(item)
        # AI・推薦・重複・解析
        names = db.genre_names()
        user = db.user_genres(row["id"])
        proposal = [e["genre_id"] for e in loads(row["ai_genres"], []) if e["genre_id"] in names]
        has_proposal = bool(proposal) and not user
        self.ai_suggest_label.setText(
            "AIの提案: " + "、".join(names[g] for g in proposal) if has_proposal
            else ("AIの提案: なし（確定済み）" if user else "AIの提案: なし"))
        self.approve_ai_button.setEnabled(has_proposal)
        self.reject_ai_button.setEnabled(has_proposal)
        lines = []
        if user:
            lines.append("<b>AI判定:</b> 手動分類済みのため、AIは結果を上書きしません")
        else:
            state = row["ai_state"]
            gl = [names.get(e["genre_id"], "?") + f"（{e['score']:.2f}）" for e in loads(row["ai_genres"], [])]
            lines.append(f"<b>AI判定:</b> {state}" + (f" — {'、'.join(gl)}" if gl else ""))
            if row["ai_reason"]:
                lines.append(f"理由: {row['ai_reason']}")
            cands = loads(row["ai_candidates"], [])
            if cands and state == "未分類":
                lines.append("参考候補: " + "、".join(f"{names.get(c['genre_id'], '?')}（{c['score']:.2f}）"
                                                    for c in cands))
        dups = ai.duplicate_groups(db)
        rec = ai.recommend(row, dups)
        lines.append(f"<b>整理推薦:</b> {rec['main']} — {rec['reason']}")
        for name, reason in rec["extra"]:
            lines.append(f"　追加候補: {name} — {reason}")
        group = dups.get(row["sha256"]) if row["sha256"] else None
        if group:
            others = [db.video(v)["path"] for v in group if v != row["id"]]
            lines.append("<b>重複:</b> 内容が完全一致する動画<br>" + "<br>".join(f"　{p}" for p in others))
        elif row["sha256"]:
            lines.append("<b>重複:</b> 完全一致する動画はありません")
        elif db.conn.execute("SELECT COUNT(*) FROM videos WHERE size=? AND id<>? AND missing=0",
                             (row["size"], row["id"])).fetchone()[0] == 0:
            lines.append("<b>重複:</b> 同じサイズの動画がないため、完全一致する動画はありません")
        else:
            lines.append("<b>重複:</b> 未確認（重複グループ画面で検出を実行してください）")
        lines.append(f"<b>解析状態:</b> {row['analysis_state']}"
                     + (f"<br>　{row['analysis_error']}" if row["analysis_error"] else ""))
        desc = ai.describe_features(loads(row["features"]), loads(row["meta"]))
        lines.append(desc.replace("\n", "<br>"))
        self.info.setText("<br>".join(lines))

    def _set_flag(self, column: str, value: bool) -> None:
        if self.video_id is None:
            return
        if column == "protected" and not value:
            if QMessageBox.question(self, "保護の解除", "保護を解除しますか？\n解除すると厳選・圧縮の対象にできます。") \
                    != QMessageBox.StandardButton.Yes:
                self.load(self.video_id)
                return
        self.ctx.db.set_flag(self.video_id, column, value)
        self.ctx.data_changed.emit()

    def _save_genres(self) -> None:
        if self.video_id is None:
            return
        if self.ctx.db.set_user_genres(self.video_id, self.genres.checked_ids(), source="修正"):
            QMessageBox.information(self, "保存しました",
                                    "ジャンルを保存しました。教師データとして登録し、次回の「再学習」で反映します。"
                                    if self.genres.checked_ids() else "ジャンルの設定を外しました。")
            self.ctx.data_changed.emit()

    def _approve_ai(self) -> None:
        if self.video_id is None:
            return
        names = self.ctx.db.genre_names()
        ids = {e["genre_id"] for e in loads(self.ctx.db.video(self.video_id)["ai_genres"], [])} & set(names)
        if ids and self.ctx.db.set_user_genres(self.video_id, ids, source="AI提案の承認"):
            self.genres.set_checked(ids)
            self.ctx.data_changed.emit()

    def _reject_ai(self) -> None:
        if self.video_id is None:
            return
        self.ctx.db.reject_ai_genres(self.video_id)
        self.ctx.data_changed.emit()

    def _tag_toggled(self, item: QListWidgetItem) -> None:
        name = item.data(Qt.ItemDataRole.UserRole)
        on = item.checkState() == Qt.CheckState.Checked
        db = self.ctx.db
        current = {t["name"]: t for t in db.video_tags(self.video_id)}
        if not on and name in current and current[name]["source"] == "ai":
            db.set_ai_tag_status(self.video_id, current[name]["id"], "却下")
        else:
            db.set_manual_tag(self.video_id, name, on)
        self.ctx.data_changed.emit()

    def _add_tag(self) -> None:
        name = self.new_tag.text().strip()
        if name and self.video_id is not None:
            self.ctx.db.set_manual_tag(self.video_id, name, True)
            self.new_tag.clear()
            self.ctx.data_changed.emit()

    def _set_ai_tag(self, status: str) -> None:
        item = self.ai_tag_list.currentItem()
        if item is None or self.video_id is None:
            return
        self.ctx.db.set_ai_tag_status(self.video_id, item.data(Qt.ItemDataRole.UserRole), status)
        self.ctx.data_changed.emit()


# =============================================================================
# ジャンル・タグ管理
# =============================================================================
class GenrePage(BasePage):
    def __init__(self, ctx, parent=None):
        super().__init__(ctx, parent)
        layout = QHBoxLayout(self)
        for kind in ("genre", "tag"):
            box = QGroupBox("ジャンル" if kind == "genre" else "タグ")
            v = QVBoxLayout(box)
            lst = QListWidget()
            buttons = QHBoxLayout()
            for text, fn in (("追加", self._add), ("名前を変更", self._rename), ("削除", self._delete)):
                b = QPushButton(text)
                b.clicked.connect(partial(fn, kind))
                buttons.addWidget(b)
            v.addWidget(lst)
            v.addLayout(buttons)
            layout.addWidget(box)
            setattr(self, f"{kind}_list", lst)
        self.genre_list.parentWidget().layout().addWidget(note_label(
            "ジャンルは内部IDで管理しているので、名前を変えても動画との関連付けは失われません。"
            "「その他・不明」はAIの学習対象にしません。"))

    def refresh(self):
        counts = self.ctx.db.genre_counts()
        self.genre_list.clear()
        for g in self.ctx.db.genres():
            item = QListWidgetItem(f"{g['name']}（手動分類 {counts.get(g['id'], 0)} 本）"
                                   + ("［学習対象外］" if g["exclude_from_training"] else ""))
            item.setData(Qt.ItemDataRole.UserRole, (g["id"], g["name"]))
            self.genre_list.addItem(item)
        self.tag_list.clear()
        for t in self.ctx.db.tags():
            item = QListWidgetItem(t["name"])
            item.setData(Qt.ItemDataRole.UserRole, (t["id"], t["name"]))
            self.tag_list.addItem(item)

    def _current(self, kind):
        item = getattr(self, f"{kind}_list").currentItem()
        return item.data(Qt.ItemDataRole.UserRole) if item else None

    def _add(self, kind):
        label = "ジャンル" if kind == "genre" else "タグ"
        name, ok = QInputDialog.getText(self, f"{label}の追加", f"新しい{label}の名前")
        if ok and name.strip():
            try:
                self.ctx.db.add_genre(name) if kind == "genre" else self.ctx.db.tag_id(name.strip())
            except Exception as e:
                QMessageBox.warning(self, "追加できません", f"同じ名前があるか、名前が正しくありません。\n{e}")
            self.ctx.data_changed.emit()

    def _rename(self, kind):
        cur = self._current(kind)
        if not cur:
            return
        name, ok = QInputDialog.getText(self, "名前を変更", "新しい名前", text=cur[1])
        if ok and name.strip() and name.strip() != cur[1]:
            try:
                (self.ctx.db.rename_genre if kind == "genre" else self.ctx.db.rename_tag)(cur[0], name)
            except Exception as e:
                QMessageBox.warning(self, "変更できません", f"同じ名前が既にあります。\n{e}")
            self.ctx.data_changed.emit()

    def _delete(self, kind):
        cur = self._current(kind)
        if not cur:
            return
        extra = "\nこのジャンルが付いた動画の関連付けと、AIの学習対象からも外れます。" if kind == "genre" else ""
        if QMessageBox.question(self, "削除の確認", f"「{cur[1]}」を削除しますか？{extra}\n（動画ファイルは削除されません）") \
                == QMessageBox.StandardButton.Yes:
            (self.ctx.db.delete_genre if kind == "genre" else self.ctx.db.delete_tag)(cur[0])
            self.ctx.data_changed.emit()


# =============================================================================
# 初期AI学習
# =============================================================================
class InitialLearningPage(BasePage):
    """初期12本によるジャンル分類AIの初期学習。

    - 開発者が用意した12本（全プロファイル共通）を固定の順番で全部見せる
    - 回答（ジャンル・スキップ）は「回答を保存して次へ」などで確定したときにプロファイルの DB に保存する
    - 途中で終了しても、次回は続きから再開できる。回答済みの動画も後から修正できる
    - 厳選（残す／整理候補）はここでは聞かない
    """

    def __init__(self, ctx, parent=None):
        super().__init__(ctx, parent)
        self.sample_ids: list[str] = []
        self.index = 0
        self._retrain_after_analysis = False
        layout = QVBoxLayout(self)
        layout.addWidget(note_label(
            f"最初に{INITIAL_SAMPLE_COUNT}本の動画を分類して、あなた専用のジャンル分類AIを作成します。"
            "各動画にあなたが考えるジャンルを付けてください（複数選択可・新しいジャンルも追加できます）。"
            "最後まで再生しなくても回答できます。回答はジャンル分類AIの学習だけに使い、"
            "厳選（残す／整理候補）の学習には使いません。少量のデータなので分類精度は保証できません。"))
        top = QHBoxLayout()
        self.status_label = QLabel()
        self.status_label.setWordWrap(True)
        self.folder_button = QPushButton("サンプルフォルダを指定…")
        self.start_button = QPushButton("初期学習を開始")
        self.skip_all_button = QPushButton("初期学習をスキップ")
        top.addWidget(self.status_label, 1)
        for b in (self.folder_button, self.start_button, self.skip_all_button):
            top.addWidget(b)
        layout.addLayout(top)
        self.error_label = QLabel()
        self.error_label.setWordWrap(True)
        self.error_label.setStyleSheet("color: #b00020;")
        layout.addWidget(self.error_label)

        self.step = QWidget()
        sv = QVBoxLayout(self.step)
        sv.setContentsMargins(0, 0, 0, 0)
        self.step_label = QLabel()
        self.step_label.setStyleSheet("font-weight: bold;")
        sv.addWidget(self.step_label)
        body = QSplitter()
        self.sample_list = QListWidget()  # 12本の一覧（クリックで移動・回答済みの修正）
        self.sample_list.setMaximumWidth(260)
        body.addWidget(self.sample_list)
        self.player = VideoPlayer()
        body.addWidget(self.player)
        side = QWidget()
        side_layout = QVBoxLayout(side)
        self.thumb = QLabel()
        self.thumb.setFixedSize(240, 135)
        self.thumb.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.thumb.setStyleSheet("background: #222; color: #aaa;")
        self.info = QLabel()
        self.info.setWordWrap(True)
        self.sample_error = QLabel()
        self.sample_error.setWordWrap(True)
        self.sample_error.setStyleSheet("color: #b00020;")
        self.genres = GenreChecklist(ctx.db)
        add_row = QHBoxLayout()
        self.new_genre = QLineEdit()
        self.new_genre.setPlaceholderText("新しいジャンル名")
        self.add_genre_button = QPushButton("ジャンルを追加")
        add_row.addWidget(self.new_genre, 1)
        add_row.addWidget(self.add_genre_button)
        side_layout.addWidget(self.thumb)
        side_layout.addWidget(self.info)
        side_layout.addWidget(self.sample_error)
        side_layout.addWidget(QLabel("ジャンル（複数選択可）"))
        side_layout.addWidget(self.genres)
        side_layout.addLayout(add_row)
        side_layout.addStretch()
        body.addWidget(side)
        body.setSizes([220, 520, 380])
        sv.addWidget(body, 1)
        nav = QHBoxLayout()
        self.prev_button = QPushButton("前へ")
        self.skip_button = QPushButton("この動画をスキップ")
        self.next_button = QPushButton("回答を保存して次へ")
        self.suspend_button = QPushButton("途中保存して終了")
        self.interim_button = QPushButton("回答済みの分で暫定学習")
        self.finish_button = QPushButton("完了して学習する")
        for b in (self.prev_button, self.skip_button, self.next_button, self.suspend_button,
                  self.interim_button, self.finish_button):
            nav.addWidget(b)
        sv.addLayout(nav)
        layout.addWidget(self.step, 1)
        self.step.setVisible(False)

        self.folder_button.clicked.connect(self.choose_folder)
        self.start_button.clicked.connect(self.start)
        self.skip_all_button.clicked.connect(self.skip_all)
        self.prev_button.clicked.connect(lambda: self.go_to(self.index - 1))
        self.next_button.clicked.connect(self.save_and_next)
        self.skip_button.clicked.connect(self.skip_current)
        self.suspend_button.clicked.connect(self.suspend)
        self.interim_button.clicked.connect(self.interim_train)
        self.finish_button.clicked.connect(self.finish)
        self.add_genre_button.clicked.connect(self.add_genre)
        self.new_genre.returnPressed.connect(self.add_genre)
        self.sample_list.currentRowChanged.connect(self._on_list_row)

    # --- 状態表示 ---------------------------------------------------------
    def refresh(self):
        db = self.ctx.db
        state = db.initial_state()
        self.status_label.setText("初期学習: " + initial_state_text(self.ctx))
        self.start_button.setText("前回の続きから再開" if state == "in_progress" and not self.step.isVisible()
                                  else "初期学習を開始")
        self.start_button.setEnabled(not self.step.isVisible())
        if not self.ctx.samples.samples():
            self.error_label.setText(
                "初期学習用のサンプル動画が見つかりません。「サンプルフォルダを指定…」で、"
                f"{INITIAL_SAMPLE_COUNT}本の動画が入ったフォルダを選んでください"
                f"（既定の場所: {samples_mod.project_sample_dir()}）。")
        else:
            problems = [f"{r['name']}: {STATUS_TEXT.get(r['status'], r['status'])}"
                        for r in self.ctx.samples.samples() if r["status"] != "ok"]
            self.error_label.setText(("読み込めないサンプルがあります（スキップできます）: " + " ／ ".join(problems))
                                     if problems else "")
        if self.step.isVisible():
            self.genres.reload()
            self._fill_list()
            self._show()

    def hideEvent(self, event):
        self.player.stop()
        super().hideEvent(event)

    def _rows(self) -> dict:
        return {r["sample_id"]: r for r in self.ctx.samples.samples()}

    def _answer_state(self, row, answer) -> str:
        if answer is None:
            return "未回答"
        if answer["status"] == ANSWERED and answer["content_hash"] != row["content_hash"]:
            return "要再確認（動画が入れ替わりました）"
        return answer["status"]

    def _fill_list(self):
        rows, answers = self._rows(), self.ctx.db.sample_answers()
        names = self.ctx.db.genre_names()
        self.sample_list.blockSignals(True)
        self.sample_list.clear()
        for i, sid in enumerate(self.sample_ids):
            row = rows.get(sid)
            if row is None:
                continue
            ans = answers.get(sid)
            state = self._answer_state(row, ans)
            detail = "、".join(names[g] for g in sorted(ans["genres"]) if g in names) \
                if ans and state == ANSWERED else ""
            item = QListWidgetItem(f"{i + 1}. {row['name']}\n　{state}" + (f"：{detail}" if detail else ""))
            item.setToolTip(row["path"])
            self.sample_list.addItem(item)
        self.sample_list.setCurrentRow(self.index)
        self.sample_list.blockSignals(False)

    # --- 開始・サンプル指定 ---------------------------------------------------
    def choose_folder(self):
        folder = QFileDialog.getExistingDirectory(self, f"初期学習用の動画（{INITIAL_SAMPLE_COUNT}本）があるフォルダ")
        if folder:
            self.register_folder(folder)

    def register_folder(self, folder: str) -> bool:
        try:
            result = self.ctx.samples.register_folder(folder)
        except OSError as e:
            QMessageBox.warning(self, "サンプル動画", f"{e}\n\n動画が入ったフォルダを指定し直してください。")
            return False
        text = f"{result.count} 本を初期学習用サンプルとして登録しました。"
        if result.changed:
            text += f"\n内容が入れ替わった動画 {len(result.changed)} 本は、以前の回答を使わず再確認にします。"
        if result.warnings:
            text += "\n\n" + "\n".join(result.warnings)
        QMessageBox.information(self, "サンプル動画", text)
        self.ctx.data_changed.emit()
        return True

    def start(self):
        store = self.ctx.samples
        try:
            store.ensure_registered()
        except OSError:
            pass
        rows = store.samples()
        if not rows:
            QMessageBox.warning(self, "初期学習",
                                "初期学習用のサンプル動画が見つかりません。\n"
                                "「サンプルフォルダを指定…」で動画が入ったフォルダを選んでください。")
            self.refresh()
            return
        self.sample_ids = [r["sample_id"] for r in rows]
        if self.ctx.db.initial_state() != "done":
            self.ctx.db.set_setting("initial_state", "in_progress")
        saved = int(self.ctx.db.get_setting("initial_index", "0") or 0)
        self.index = min(max(saved, 0), len(self.sample_ids) - 1)
        self.genres.reload()
        self.step.setVisible(True)
        if store.needs_analysis() and not self.ctx.is_busy():
            # 学習用の特徴量を裏で計算する（回答の入力はそのまま続けられる）
            self.ctx.analyze_samples(on_done=self._after_analysis, parent=self, quiet=True)
        self.ctx.data_changed.emit()

    def _after_analysis(self, _result):
        if self._retrain_after_analysis:
            self._retrain_after_analysis = False
            self.ctx.retrain(self)

    # --- 表示 -------------------------------------------------------------
    def current_id(self) -> str | None:
        return self.sample_ids[self.index] if self.sample_ids else None

    def _show(self):
        sid = self.current_id()
        row = self.ctx.samples.sample(sid) if sid else None
        if row is None:
            return
        ans = self.ctx.db.sample_answer(sid)
        state = self._answer_state(row, ans)
        self.step_label.setText(f"{self.index + 1} / {len(self.sample_ids)}　{row['name']}　［{state}］")
        meta = loads(row["meta"], {})
        duration = f"{meta['duration']:.0f} 秒" if meta.get("duration") else "未解析（再生すると表示されます）"
        self.info.setText(f"ファイル名: {row['name']}\n再生時間: {duration}\n回答: {state}")
        thumbs = loads(row["thumbs"], [])
        pm = thumb_pixmap(thumbs[len(thumbs) // 2], 240, 135) if thumbs else None
        if pm:
            self.thumb.setPixmap(pm)
        else:
            self.thumb.clear()
            self.thumb.setText("サムネイルなし\n（解析後に表示）")
        if row["status"] != "ok":
            self.sample_error.setText(f"この動画は使えません: {STATUS_TEXT.get(row['status'], row['status'])}"
                                      f"{'（' + row['status_detail'] + '）' if row['status_detail'] else ''}。"
                                      "スキップしてください。")
            self.player.set_source(None)
        else:
            self.sample_error.setText(f"解析できませんでした（学習には使えません）: {row['analysis_error']}"
                                      if row["analysis_state"] == "失敗" else "")
            self.player.set_source(row["path"])
        # 入れ替わった動画には古い回答を流用しない
        self.genres.set_checked(ans["genres"] if ans and state == ANSWERED else set())
        self.prev_button.setEnabled(self.index > 0)
        self.next_button.setEnabled(row["status"] == "ok")
        self.sample_list.blockSignals(True)
        self.sample_list.setCurrentRow(self.index)
        self.sample_list.blockSignals(False)

    def go_to(self, index: int):
        if not self.sample_ids:
            return
        self.index = max(0, min(len(self.sample_ids) - 1, index))
        self.ctx.db.set_setting("initial_index", str(self.index))
        self._show()

    def _on_list_row(self, row: int):
        if row >= 0 and row != self.index:
            self.go_to(row)

    # --- 回答 -------------------------------------------------------------
    def _save(self, status: str, genres=()) -> None:
        sid = self.current_id()
        row = self.ctx.samples.sample(sid)
        self.ctx.db.save_sample_answer(sid, status, genres, row["content_hash"])

    def _advance(self):
        self._fill_list()
        if self.index < len(self.sample_ids) - 1:
            self.go_to(self.index + 1)
        else:
            self._show()
            self.ctx.data_changed.emit()
            QMessageBox.information(self, "初期学習", "最後の動画です。「完了して学習する」で学習します。"
                                    "回答済みの動画は左の一覧から選んで修正できます。")
            return
        self.ctx.data_changed.emit()

    def save_and_next(self):
        ids = self.genres.checked_ids()
        if not ids:
            QMessageBox.information(self, "初期学習", "ジャンルを1つ以上選ぶか、「この動画をスキップ」を押してください。")
            return
        self._save(ANSWERED, ids)
        self._advance()

    def skip_current(self):
        self._save(SKIPPED)
        self._advance()

    def add_genre(self):
        name = self.new_genre.text().strip()
        if not name:
            return
        try:
            gid = self.ctx.db.add_genre(name)
        except ValueError as e:
            QMessageBox.warning(self, "ジャンルを追加できません", str(e))
            return
        checked = self.genres.checked_ids() | {gid}
        self.genres.reload()
        self.genres.set_checked(checked)
        self.new_genre.clear()

    def suspend(self):
        """途中保存して終了。選びかけのジャンルがあれば回答として保存する。"""
        if self.sample_ids:
            ids = self.genres.checked_ids()
            row = self.ctx.samples.sample(self.current_id())
            if ids and row["status"] == "ok":
                self._save(ANSWERED, ids)
            self.ctx.db.set_setting("initial_index", str(self.index))
        if self.ctx.db.initial_state() != "done":
            self.ctx.db.set_setting("initial_state", "in_progress")
        self._close_step()

    def _close_step(self):
        self.step.setVisible(False)
        self.player.stop()
        self.ctx.data_changed.emit()

    def skip_all(self):
        self.ctx.db.set_setting("initial_state", "skipped")
        self._close_step()

    def _answered_count(self) -> int:
        rows = self._rows()
        return sum(1 for sid, a in self.ctx.db.sample_answers().items()
                   if sid in rows and self._answer_state(rows[sid], a) == ANSWERED)

    def _train(self) -> None:
        if self.ctx.samples.needs_analysis():
            self._retrain_after_analysis = True
            if not self.ctx.is_busy():
                if not self.ctx.analyze_samples(on_done=self._after_analysis, parent=self):
                    self._retrain_after_analysis = False
                    self.ctx.retrain(self)
            return
        if self.ctx.is_busy() and self.ctx.task_label == "初期サンプルの解析":
            self._retrain_after_analysis = True  # 解析が終わってから学習する
            return
        self.ctx.retrain(self)

    def interim_train(self):
        """回答済みの分だけで暫定の分類基準を作る（足りなければ作らない）。"""
        if not self._answered_count():
            QMessageBox.information(self, "初期学習", "回答済みの動画がまだありません。")
            return
        self._train()

    def finish(self):
        if not self._answered_count():
            QMessageBox.information(self, "初期学習",
                                    "回答済みの動画がありません。少なくとも1本にジャンルを付けてください"
                                    "（回答しない場合は「初期学習をスキップ」）。")
            return
        self.ctx.db.set_setting("initial_state", "done")
        self._close_step()
        self._train()


# =============================================================================
# 未分類動画
# =============================================================================
class UnclassifiedPage(BasePage):
    def __init__(self, ctx, parent=None):
        super().__init__(ctx, parent)
        layout = QVBoxLayout(self)
        layout.addWidget(note_label("AIが十分な根拠を得られなかった動画です。都合のよいときにまとめて分類してください。"
                                    "確定した分類は教師データとして登録し、「再学習」で反映します。"))
        splitter = QSplitter()
        self.model = VideoTableModel(self)
        self.table = make_video_table(self.model, hidden=("ext", "mtime", "folder", "tags", "favorite",
                                                          "protected", "rec", "rec_reason",
                                                          *NON_CURATION_HIDDEN))
        splitter.addWidget(self.table)
        side = QWidget()
        sv = QVBoxLayout(side)
        self.info = QLabel("動画を選んでください")
        self.info.setWordWrap(True)
        self.genres = GenreChecklist(ctx.db)
        self.confirm_button = QPushButton("このジャンルで確定")
        self.next_button = QPushButton("スキップして次へ")
        self.retrain_button = QPushButton()
        sv.addWidget(self.info)
        sv.addWidget(self.genres)
        sv.addWidget(self.confirm_button)
        sv.addWidget(self.next_button)
        sv.addStretch()
        sv.addWidget(self.retrain_button)
        splitter.addWidget(side)
        splitter.setSizes([600, 400])
        layout.addWidget(splitter, 1)
        self.table.selectionModel().selectionChanged.connect(self._on_select)
        self.table.doubleClicked.connect(lambda idx: self.ctx.open_video_requested.emit(
            self.model.video_id(idx.row())))
        self.confirm_button.clicked.connect(self._confirm)
        self.next_button.clicked.connect(self._next)
        self.retrain_button.clicked.connect(lambda: self.ctx.retrain(self))

    def refresh(self):
        rows = [r for r in build_video_rows(self.ctx.db)
                if r["ai_state"] == "未分類" and not r["user_genres"]]
        self.model.set_rows(rows)
        self.genres.reload()
        self.retrain_button.setText(f"再学習（未反映の教師データ {self.ctx.db.pending_training_count()} 件）")
        if rows:
            self.table.selectRow(0)
        else:
            self.info.setText("未分類の動画はありません")

    def _current_id(self):
        ids = selected_ids(self.table, self.model)
        return ids[0] if ids else None

    def _on_select(self, *_):
        vid = self._current_id()
        if vid is None:
            return
        row = self.ctx.db.video(vid)
        names = self.ctx.db.genre_names()
        cands = loads(row["ai_candidates"], [])
        self.info.setText(f"<b>{row['name']}</b><br>未分類の理由: {row['ai_reason'] or '-'}<br>"
                          + ("AIの参考候補: " + "、".join(f"{names.get(c['genre_id'], '?')}（{c['score']:.2f}）"
                                                       for c in cands) if cands else "AIの参考候補: なし"))
        self.genres.set_checked(set())

    def _confirm(self):
        vid = self._current_id()
        ids = self.genres.checked_ids()
        if vid is None or not ids:
            QMessageBox.information(self, "未分類動画", "動画とジャンルを選んでください。")
            return
        self.ctx.db.set_user_genres(vid, ids, source="未分類一覧")
        self.ctx.data_changed.emit()

    def _next(self):
        row = self.table.currentIndex().row()
        if row + 1 < self.model.rowCount():
            self.table.selectRow(row + 1)


# =============================================================================
# 厳選（ジャンル別厳選AI・全動画厳選AI）
# =============================================================================
class CurationPage(BasePage):
    def __init__(self, ctx, parent=None):
        super().__init__(ctx, parent)
        self._models = None
        layout = QVBoxLayout(self)
        layout.addWidget(note_label(
            "厳選の回答（残す／整理候補／保留）は判断の記録で、削除は行いません。ジャンル別と全動画の回答は"
            "別々の教師データとして保存し、混ぜません。「保留」は学習に使いません。保護中の動画は情報表示のみです。"
            "「整理推薦」は重複・圧縮などの客観的な情報、「厳選AIの提案」はあなたの回答から学んだ個人的な判断です。"
            "厳選AIは試作で、似た回答の動画に近いかどうかだけで判断します。"))
        mode = QHBoxLayout()
        self.genre_mode = QRadioButton("ジャンル別厳選")
        self.all_mode = QRadioButton("全動画厳選")
        self.all_mode.setChecked(True)
        group = QButtonGroup(self)
        group.addButton(self.genre_mode)
        group.addButton(self.all_mode)
        self.include_ai = QCheckBox("AIの分類結果も対象に含める")
        self.search = QLineEdit()
        self.search.setPlaceholderText("ファイル名で検索")
        self.answer_filter = QComboBox()
        self.answer_filter.addItems(["回答: すべて", "未回答", "残す", "整理候補", "保留"])
        for w in (self.genre_mode, self.all_mode, self.include_ai):
            mode.addWidget(w)
        mode.addWidget(self.search, 1)
        mode.addWidget(self.answer_filter)
        layout.addLayout(mode)
        self.genre_box = QGroupBox("対象ジャンル（いずれかに該当＝OR条件）")
        gv = QVBoxLayout(self.genre_box)
        self.genres = GenreChecklist(ctx.db)
        self.genre_apply = QPushButton("ジャンルの選択を反映")
        gv.addWidget(self.genres)
        gv.addWidget(self.genre_apply)
        layout.addWidget(self.genre_box)
        splitter = QSplitter()
        self.model = VideoTableModel(self)
        self.table = make_video_table(self.model, hidden=("ext", "folder", "tags", "mtime", "ai", "favorite"))
        header = self.table.horizontalHeader()
        keys = [k for k, _ in VCOLS]
        for key in ("suggest_reason", "suggest", "answer"):  # 回答とAI提案をファイル名の直後に並べる
            header.moveSection(header.visualIndex(keys.index(key)),
                               header.visualIndex(keys.index("size")) + 1)
        self.table.setColumnWidth(keys.index("name"), 170)
        self.table.setColumnWidth(keys.index("answer"), 200)
        self.table.setColumnWidth(keys.index("suggest_reason"), 260)
        splitter.addWidget(self.table)
        self.individual_box = QGroupBox("選択中の動画のジャンル別回答（個別回答は共通回答より優先）")
        iv = QVBoxLayout(self.individual_box)
        self.individual_label = QLabel()
        self.individual_label.setWordWrap(True)
        self.individual_form = QFormLayout()
        self.individual_save = QPushButton("個別回答を保存")
        iv.addWidget(self.individual_label)
        iv.addLayout(self.individual_form)
        iv.addWidget(self.individual_save)
        iv.addStretch()
        splitter.addWidget(self.individual_box)
        splitter.setSizes([960, 280])
        layout.addWidget(splitter, 1)
        bottom = QHBoxLayout()
        self.summary = QLabel()
        self.summary.setWordWrap(True)
        bottom.addWidget(self.summary, 1)
        self.choice_buttons = {}
        for choice in (*CURATION_CHOICES, "回答を解除"):
            b = QPushButton(choice)
            b.clicked.connect(partial(self._apply, None if choice == "回答を解除" else choice))
            bottom.addWidget(b)
            self.choice_buttons[choice] = b
        layout.addLayout(bottom)
        tools = QHBoxLayout()
        self.retrain_button = QPushButton("厳選AIを再学習（変更があった学習範囲のみ）")
        self.reset_button = QPushButton("学習リセット…")
        tools.addWidget(self.retrain_button)
        tools.addWidget(self.reset_button)
        tools.addStretch()
        layout.addLayout(tools)
        self._combos: dict[int, QComboBox] = {}
        self.genre_mode.toggled.connect(self.refresh)
        self.include_ai.toggled.connect(self.refresh)
        self.search.textChanged.connect(self.refresh)
        self.answer_filter.currentIndexChanged.connect(self.refresh)
        self.genre_apply.clicked.connect(self.refresh)
        self.table.selectionModel().selectionChanged.connect(self._fill_individual)
        self.table.doubleClicked.connect(lambda idx: self.ctx.open_video_requested.emit(
            self.model.video_id(idx.row())))
        self.individual_save.clicked.connect(self._save_individual)
        self.retrain_button.clicked.connect(self._retrain)
        self.reset_button.clicked.connect(lambda: open_reset_dialog(self.ctx, self))

    @property
    def mode(self) -> str:
        return "genre" if self.genre_mode.isChecked() else "all"

    def refresh(self):
        db = self.ctx.db
        genre = self.mode == "genre"
        self.genre_box.setVisible(genre)
        self.individual_box.setVisible(genre)
        self.include_ai.setVisible(genre)
        rows = build_video_rows(db)
        selected = self.genres.checked_ids()
        if genre:
            rows = ai.select_curation_rows(rows, selected, self.include_ai.isChecked())
        text = self.search.text().strip().casefold()
        if text:
            rows = [r for r in rows if text in r["name"].casefold()]
        names = db.genre_names()
        if genre:
            labels = db.effective_labels("genre")
            for r in rows:
                parts, values = [], set()
                for gid in sorted(ai.target_genres(r, selected, self.include_ai.isChecked())):
                    lb = labels.get((r["id"], gid))
                    if gid not in r["user_genres"]:
                        parts.append(f"{names.get(gid, '?')}: ジャンル未確定")
                        continue
                    if lb is None:
                        parts.append(f"{names.get(gid, '?')}: 未回答")
                        values.add("")
                        continue
                    tag = "個別" if lb["source"] == "individual" else "共通"
                    review = "・再確認待ち" if lb["needs_review"] else ""
                    parts.append(f"{names.get(gid, '?')}: {lb['label']}（{tag}{review}）")
                    values.add(lb["label"])
                r["answer"] = " ／ ".join(parts)
                r["answer_values"] = values
        else:
            labels = db.effective_labels("all")
            for r in rows:
                lb = labels.get(r["id"])
                r["answer"] = lb["label"] if lb else ""
                r["answer_values"] = {lb["label"] if lb else ""}
        f = self.answer_filter.currentText()
        if f == "未回答":
            rows = [r for r in rows if "" in r["answer_values"]]
        elif f in CURATION_CHOICES:
            rows = [r for r in rows if f in r["answer_values"]]
        self._models = ai.load_curation_models(db)
        preds = ai.predict_for_mode(db, rows, self.mode, selected, self.include_ai.isChecked(), self._models)
        for r in rows:
            r["suggest"] = preds[r["id"]]["suggest"]
            r["suggest_reason"] = preds[r["id"]]["reason"]
        self.model.set_rows(rows)
        scope_rows = db.effective_labels("genre" if genre else "all")
        counts = {c: sum(1 for lb in scope_rows.values() if lb["label"] == c) for c in CURATION_CHOICES}
        trained = [ai.scope_label(db, s) for s in self._models
                   if (s == "curate_all") != genre]
        self.summary.setText(
            f"表示 {len(rows)} 本 / この学習範囲の回答: 残す {counts['残す']}・整理候補 {counts['整理候補']}・"
            f"保留 {counts['保留']} / 再確認待ち {db.stats()['review']} 件\n"
            f"学習済み: {'、'.join(trained) if trained else 'なし（教師データ不足の範囲は「保留」と表示）'}")
        self._fill_individual()

    def _apply(self, choice):
        """共通回答: 選択した動画に1回の回答を適用する。"""
        ids = selected_ids(self.table, self.model)
        db = self.ctx.db
        skipped_protected = skipped_unconfirmed = 0
        selected = self.genres.checked_ids()
        for vid in ids:
            if db.video(vid)["protected"]:
                skipped_protected += 1
                continue
            if self.mode == "all":
                db.answer(vid, "all", None, choice, "all")
                continue
            row = next(r for r in self.model.rows if r["id"] == vid)
            for gid in ai.target_genres(row, selected, self.include_ai.isChecked()):
                if gid in row["user_genres"]:  # 確定したジャンルだけに記録する
                    db.answer(vid, "genre", gid, choice, "common")
                else:
                    skipped_unconfirmed += 1
        notes = []
        if skipped_protected:
            notes.append(f"保護中の動画 {skipped_protected} 本は対象外にしました（情報表示のみ）。")
        if skipped_unconfirmed:
            notes.append(f"ジャンルが未確定（AIの分類のみ）の {skipped_unconfirmed} 件は記録していません。"
                         "先にジャンルを確定してください。")
        if notes:
            QMessageBox.information(self, "厳選", "\n".join(notes))
        self.ctx.data_changed.emit()

    def _fill_individual(self, *_):
        while self.individual_form.rowCount():
            self.individual_form.removeRow(0)
        self._combos = {}
        ids = selected_ids(self.table, self.model)
        if self.mode != "genre" or not ids:
            self.individual_label.setText("動画を1本選ぶと、ジャンルごとに回答を変えられます。")
            self.individual_save.setEnabled(False)
            return
        db = self.ctx.db
        row = next(r for r in self.model.rows if r["id"] == ids[0])
        if row["protected"]:
            self.individual_label.setText(f"{row['name']}: 保護中のため回答できません")
            self.individual_save.setEnabled(False)
            return
        labels = db.effective_labels("genre")
        names = db.genre_names()
        targets = [g for g in sorted(ai.target_genres(row, self.genres.checked_ids(),
                                                      self.include_ai.isChecked()))
                   if g in row["user_genres"]]
        self.individual_label.setText(f"{row['name']}" + ("" if targets else "\n確定したジャンルがありません"))
        for gid in targets:
            combo = QComboBox()
            combo.addItems(["（共通回答に従う）", *CURATION_CHOICES])
            lb = labels.get((row["id"], gid))
            if lb is not None and lb["source"] == "individual":
                combo.setCurrentText(lb["label"])
            self.individual_form.addRow(names.get(gid, "?"), combo)
            self._combos[gid] = combo
        self.individual_save.setEnabled(bool(targets))

    def _save_individual(self):
        ids = selected_ids(self.table, self.model)
        if not ids or not self._combos:
            return
        vid = ids[0]
        db = self.ctx.db
        for gid, combo in self._combos.items():
            value = combo.currentText()
            cur = db.effective_labels("genre", gid).get(vid)
            if value in CURATION_CHOICES:
                db.answer(vid, "genre", gid, value, "individual")
            elif cur is not None and cur["source"] == "individual":
                db.answer(vid, "genre", gid, None, "individual")  # 個別回答をやめる
        self.ctx.data_changed.emit()

    def _retrain(self):
        data_dir = self.ctx.data_dir
        scopes = None

        def done(results):
            if not results:
                QMessageBox.information(self, "厳選AIの再学習", "教師データがありません。厳選で回答してください。")
                return
            lines = []
            for name, r in results.items():
                line = f"{name}: {r['status']} — {r['message']}"
                ev = r.get("evaluation")
                if ev:
                    acc = "-" if ev.get("accuracy") is None else f"{ev['accuracy']:.2f}"
                    line += f"\n　評価（1件ずつ外して予測）: 正解率 {acc} / 保留率 {ev['hold_rate']:.0%}（{ev['status']}）"
                lines.append(line)
            QMessageBox.information(self, "厳選AIの再学習", "\n".join(lines))
        self.ctx.run_task("厳選AIの再学習", lambda c, r: ai.task_retrain_curation(data_dir, scopes, c, r),
                          done, parent=self)


# =============================================================================
# 再確認待ち（ジャンル変更の影響を受けたジャンル別回答）
# =============================================================================
class ReviewPage(BasePage):
    def __init__(self, ctx, parent=None):
        super().__init__(ctx, parent)
        layout = QVBoxLayout(self)
        layout.addWidget(note_label(
            "動画のジャンルが追加・削除されたため、学習から一時的に外しているジャンル別の回答です。"
            "確認または修正すると学習対象に戻ります。回答を別のジャンルへ自動で移すことはありません。"
            "全動画厳選の回答はジャンル変更の影響を受けません。"))
        self.tree = QTreeWidget()
        self.tree.setHeaderLabels(["動画", "ジャンル", "回答", "現在のジャンル所属"])
        self.tree.setColumnWidth(0, 260)
        self.tree.setColumnWidth(1, 160)
        layout.addWidget(self.tree, 1)
        buttons = QHBoxLayout()
        self.confirm_button = QPushButton("このまま確認して学習に戻す")
        self.change_combo = QComboBox()
        self.change_combo.addItems(CURATION_CHOICES)
        self.change_button = QPushButton("回答を変更して戻す")
        self.empty_label = QLabel()
        buttons.addWidget(self.confirm_button)
        buttons.addWidget(self.change_combo)
        buttons.addWidget(self.change_button)
        buttons.addWidget(self.empty_label, 1)
        layout.addLayout(buttons)
        self.confirm_button.clicked.connect(self._confirm)
        self.change_button.clicked.connect(self._change)

    def refresh(self):
        db = self.ctx.db
        names = db.genre_names()
        user = db.all_user_genres()
        self.tree.clear()
        queue = db.review_queue()
        for lb in queue:
            member = "属している" if lb["genre_id"] in user.get(lb["video_id"], set()) else "属していない"
            item = QTreeWidgetItem([lb["video_name"], names.get(lb["genre_id"], "削除済みジャンル"),
                                    lb["label"], member])
            item.setData(0, Qt.ItemDataRole.UserRole, (lb["id"], lb["video_id"], lb["genre_id"], lb["source"]))
            self.tree.addTopLevelItem(item)
        self.empty_label.setText("" if queue else "再確認待ちの回答はありません")

    def _current(self):
        item = self.tree.currentItem()
        return item.data(0, Qt.ItemDataRole.UserRole) if item else None

    def _confirm(self):
        cur = self._current()
        if cur:
            self.ctx.db.confirm_label(cur[0])
            self.ctx.data_changed.emit()

    def _change(self):
        cur = self._current()
        if not cur:
            return
        try:
            self.ctx.db.answer(cur[1], "genre", cur[2], self.change_combo.currentText(),
                               "individual" if cur[3] == "individual" else "common")
        except PermissionError as e:
            QMessageBox.information(self, "再確認", str(e))
        self.ctx.data_changed.emit()


# =============================================================================
# 学習リセット
# =============================================================================
def open_reset_dialog(ctx: AppContext, parent=None, choose=None) -> bool:
    """リセットする学習範囲を選び、確認してからモデルだけを初期化する。

    choose はテスト用（選択済みの学習範囲リストを返す関数）。
    """
    from PySide6.QtWidgets import QDialog, QDialogButtonBox
    db = ctx.db
    scopes = ["curate_all"] + [f"curate_genre:{g['id']}" for g in db.genres()]
    if choose is None:
        dialog = QDialog(parent)
        dialog.setWindowTitle("学習リセット")
        v = QVBoxLayout(dialog)
        v.addWidget(note_label("リセットするAIを選んでください。初期化するのはモデルだけで、教師データ・回答履歴・"
                               "動画ファイル・ジャンル設定・お気に入り・保護設定は変更しません。"))
        lst = QListWidget()
        for scope in scopes + ["classify"]:
            text = ai.scope_label(db, scope) + ("（明示的に選んだ場合のみ）" if scope == "classify" else "")
            if scope in db.active_models():
                text += "　［学習済み］"
            item = QListWidgetItem(text)
            item.setData(Qt.ItemDataRole.UserRole, scope)
            item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            item.setCheckState(Qt.CheckState.Unchecked)
            lst.addItem(item)
        v.addWidget(lst)
        bb = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        bb.accepted.connect(dialog.accept)
        bb.rejected.connect(dialog.reject)
        v.addWidget(bb)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return False
        chosen = [lst.item(i).data(Qt.ItemDataRole.UserRole) for i in range(lst.count())
                  if lst.item(i).checkState() == Qt.CheckState.Checked]
    else:
        chosen = choose()
    if not chosen:
        return False
    plan = ai.reset_plan(db, chosen)
    text = ("【初期化するモデル】\n" + "\n".join(plan["reset_models"])
            + "\n\n【保持する教師データ・回答履歴】\n" + "\n".join(plan["kept_labels"])
            + ("\n\n【消去する分類結果】\n" + "\n".join(plan["cleared_predictions"])
               if plan["cleared_predictions"] else "")
            + "\n\n【影響を受けないモデル】\n" + ("\n".join(plan["untouched"]) or "なし")
            + "\n\n動画ファイル・ジャンル設定・お気に入り・保護設定は変更しません。リセットしますか？")
    if QMessageBox.question(parent, "リセットの確認", text) != QMessageBox.StandardButton.Yes:
        return False
    ai.reset_models(db, chosen)
    ctx.data_changed.emit()
    QMessageBox.information(parent, "学習リセット", "リセットしました。必要に応じて手動で再学習してください。")
    return True


# =============================================================================
# 重複グループ
# =============================================================================
class DuplicatePage(BasePage):
    def __init__(self, ctx, parent=None):
        super().__init__(ctx, parent)
        layout = QVBoxLayout(self)
        layout.addWidget(note_label("ファイルサイズで候補を絞ってから SHA-256 で内容の完全一致を確認します。"
                                    "クラウド上のみのファイルは読み込みません。"))
        top = QHBoxLayout()
        self.run_button = QPushButton("重複を検出")
        self.summary = QLabel()
        self.summary.setWordWrap(True)
        top.addWidget(self.run_button)
        top.addWidget(self.summary, 1)
        layout.addLayout(top)
        self.tree = QTreeWidget()
        self.tree.setHeaderLabels(["動画", "サイズ", "状態", "保存場所"])
        self.tree.setColumnWidth(0, 260)
        layout.addWidget(self.tree, 1)
        self.run_button.clicked.connect(self._run)
        self.tree.itemDoubleClicked.connect(self._open)

    def _run(self):
        data_dir = self.ctx.data_dir

        def done(result):
            msg = f"候補 {result['candidates']} 本を確認し、重複グループ {result['groups']} 件を見つけました。"
            if result["cancelled"]:
                msg = "キャンセルしました（計算済みの結果は保存しています）。\n" + msg
            if result["errors"]:
                msg += "\n読み取れなかった動画:\n" + "\n".join(result["errors"][:10])
            QMessageBox.information(self, "重複検出", msg)
        self.ctx.run_task("重複検出", lambda c, r: ai.task_find_duplicates(data_dir, c, r), done, parent=self)

    def refresh(self):
        db = self.ctx.db
        self.tree.clear()
        wasted = 0
        groups = ai.duplicate_groups(db)
        for i, ids in enumerate(groups.values(), 1):
            rows = [db.video(v) for v in ids]
            inodes = set()
            for r in rows:
                try:
                    st = os.stat(r["path"])
                    inodes.add((st.st_dev, st.st_ino))
                except OSError:
                    inodes.add(r["path"])
            size = rows[0]["size"]
            group_wasted = size * (len(inodes) - 1)
            wasted += group_wasted
            note = "" if len(inodes) == len(rows) else "（同じ実体を指すハードリンクを含む）"
            top = QTreeWidgetItem([f"グループ {i}: {len(rows)} 本", format_size(size),
                                   f"重複分 {format_size(group_wasted)}{note}", ""])
            for r in rows:
                child = QTreeWidgetItem([r["name"], format_size(r["size"]),
                                         "保護中" if r["protected"] else "", r["folder"]])
                child.setData(0, Qt.ItemDataRole.UserRole, r["id"])
                top.addChild(child)
            self.tree.addTopLevelItem(top)
            top.setExpanded(True)
        unchecked = sum(1 for r in db.videos() if not r["sha256"])
        self.summary.setText(
            f"重複グループ {len(groups)} 件 / 重複分の容量 {format_size(wasted)}"
            "（1本ずつ残した場合。整理候補にするかは保護状態と内容を確認して判断してください）"
            + (f"\nハッシュ未計算の動画 {unchecked} 本（サイズが他と一致しないものは計算不要です）" if unchecked else ""))

    def _open(self, item, _col):
        vid = item.data(0, Qt.ItemDataRole.UserRole)
        if vid:
            self.ctx.open_video_requested.emit(vid)


# =============================================================================
# 圧縮
# =============================================================================
class CompressPage(BasePage):
    def __init__(self, ctx, parent=None):
        super().__init__(ctx, parent)
        self.last_output: str | None = None
        layout = QVBoxLayout(self)
        layout.addWidget(note_label("元の動画は上書き・削除しません。圧縮結果は別ファイルとして出力し、"
                                    "検証に通ってから完成ファイルにします。保護中の動画は圧縮できません。"
                                    "圧縮しただけでは容量は空きません（元の動画はそのまま残ります）。"))
        form = QFormLayout()
        self.video_combo = QComboBox()
        self.preset_combo = QComboBox()
        self.preset_combo.addItems(list(media.PRESETS))
        self.preset_combo.setCurrentText("バランス")
        out_row = QHBoxLayout()
        self.out_dir = QLineEdit()
        browse = QPushButton("参照…")
        out_row.addWidget(self.out_dir, 1)
        out_row.addWidget(browse)
        form.addRow("動画", self.video_combo)
        form.addRow("プリセット", self.preset_combo)
        form.addRow("出力先フォルダ", out_row)
        layout.addLayout(form)
        self.state_label = QLabel()
        self.state_label.setWordWrap(True)
        layout.addWidget(self.state_label)
        buttons = QHBoxLayout()
        self.start_button = QPushButton("圧縮開始")
        self.play_button = QPushButton("圧縮結果を再生")
        self.play_button.setEnabled(False)
        buttons.addWidget(self.start_button)
        buttons.addWidget(self.play_button)
        buttons.addStretch()
        layout.addLayout(buttons)
        self.result_label = QLabel()
        self.result_label.setWordWrap(True)
        layout.addWidget(self.result_label)
        self.player = VideoPlayer()
        layout.addWidget(self.player, 1)
        browse.clicked.connect(self._browse)
        self.video_combo.currentIndexChanged.connect(self._update_state)
        self.start_button.clicked.connect(self._start)
        self.play_button.clicked.connect(self._play_output)

    def _play_output(self) -> None:
        if self.last_output and self.player.player is not None:
            self.player.set_source(self.last_output)
            self.player.player.play()

    def select_video(self, video_id: int) -> None:
        self.refresh()
        i = self.video_combo.findData(video_id)
        if i >= 0:
            self.video_combo.setCurrentIndex(i)

    def refresh(self):
        current = self.video_combo.currentData()
        self.video_combo.blockSignals(True)
        self.video_combo.clear()
        for r in self.ctx.db.videos():
            self.video_combo.addItem(f"{r['name']}（{format_size(r['size'])}）"
                                     + ("［保護中］" if r["protected"] else ""), r["id"])
        i = self.video_combo.findData(current)
        self.video_combo.setCurrentIndex(max(i, 0))
        self.video_combo.blockSignals(False)
        if not self.out_dir.text():
            self.out_dir.setText(self.ctx.db.get_setting("compress_dir", "") or "")
        self._update_state()

    def hideEvent(self, event):
        self.player.stop()
        super().hideEvent(event)

    def _browse(self):
        d = QFileDialog.getExistingDirectory(self, "出力先フォルダを選択")
        if d:
            self.out_dir.setText(d)

    def _update_state(self):
        vid = self.video_combo.currentData()
        if vid is None:
            self.state_label.setText("動画がありません")
            self.start_button.setEnabled(False)
            return
        row = self.ctx.db.video(vid)
        meta = loads(row["meta"], {})
        reason = ai.compression_reason(meta, row["size"])
        if row["protected"]:
            text, ok = "保護中のため圧縮できません。動画詳細で保護を解除してください。", False
        elif row["cloud_only"]:
            text, ok = "クラウド上のみのファイルのため圧縮できません。", False
        else:
            text = f"圧縮候補の理由: {reason}" if reason else "特に圧縮候補ではありません（容量が減るとは限りません）"
            ok = True
        self.state_label.setText(text)
        self.start_button.setEnabled(ok and self.ctx.tools().available)

    def _start(self):
        vid, out_dir, preset = self.video_combo.currentData(), self.out_dir.text().strip(), \
            self.preset_combo.currentText()
        if not out_dir or not os.path.isdir(out_dir):
            QMessageBox.warning(self, "圧縮", "出力先フォルダを選んでください。")
            return
        self.ctx.db.set_setting("compress_dir", out_dir)
        tools, data_dir = self.ctx.tools(), self.ctx.data_dir

        def done(result):
            self.last_output = result["output"]
            self.play_button.setEnabled(True)
            text = (f"完了: {result['output']}\n圧縮前 {format_size(result['source_size'])} → "
                    f"圧縮後 {format_size(result['output_size'])}")
            if result["larger"]:
                text += "\n⚠ 圧縮結果が元の動画以上の大きさです。この動画は圧縮に向いていない可能性があります。"
            self.result_label.setText(text)

        def failed(message):
            text = "キャンセルしました（元の動画は変更していません）" if message in ("", "Cancelled") else \
                f"圧縮できませんでした（元の動画は変更していません）: {message}"
            self.result_label.setText(text)
        self.result_label.setText("圧縮中…")
        self.ctx.run_task("圧縮", lambda c, r: ai.task_compress(data_dir, tools, vid, out_dir, preset, c, r),
                          done, failed, parent=self)


# =============================================================================
# 設定
# =============================================================================
class SettingsPage(BasePage):
    def __init__(self, ctx, parent=None):
        super().__init__(ctx, parent)
        layout = QVBoxLayout(self)
        form = QFormLayout()
        ff_row = QHBoxLayout()
        self.ffmpeg_dir = QLineEdit()
        self.ffmpeg_dir.setPlaceholderText("空欄なら PATH から探します")
        browse = QPushButton("参照…")
        check = QPushButton("確認")
        for w in (self.ffmpeg_dir, browse, check):
            ff_row.addWidget(w)
        self.ffmpeg_state = QLabel()
        self.frames = QSpinBox()
        self.frames.setRange(6, media.MAX_FRAMES)
        self.data_label = QLabel()
        self.data_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        form.addRow("FFmpeg のフォルダ", ff_row)
        form.addRow("", self.ffmpeg_state)
        form.addRow("映像の最大抽出枚数", self.frames)
        form.addRow("データ保存先", self.data_label)
        layout.addLayout(form)
        save = QPushButton("設定を保存")
        reset = QPushButton("初期学習の状態をリセット（回答・教師データは残します）")
        model_reset = QPushButton("学習リセット（AIを選んで初期化）…")
        layout.addWidget(save)
        layout.addWidget(reset)
        layout.addWidget(model_reset)
        model_reset.clicked.connect(lambda: open_reset_dialog(self.ctx, self))
        layout.addWidget(note_label(
            "AIの実装範囲: 実装済み＝映像（明るさ・黒画面・静止・動き・色）と音声（音量・無音率・簡易な音質）の"
            "数値特徴、教師データとの類似度による複数ジャンル推定、未分類判定、手動再学習、"
            "ジャンル別厳選AI・全動画厳選AI（回答との類似度による残す候補／整理候補／保留）。"
            "未実装＝音楽・歌唱・会話の意味分類、字幕判定、事前学習済みモデル（CLIP・CLAP など）、"
            "動画の内容を外部へ送信する処理はありません（ネットワーク通信を行いません）。"))
        layout.addStretch()
        browse.clicked.connect(self._browse)
        check.clicked.connect(self._check)
        save.clicked.connect(self._save)
        reset.clicked.connect(self._reset)

    def refresh(self):
        self.ffmpeg_dir.setText(self.ctx.db.get_setting("ffmpeg_dir", "") or "")
        self.frames.setValue(self.ctx.max_frames())
        self.data_label.setText(str(self.ctx.data_dir))
        self._check()

    def _browse(self):
        d = QFileDialog.getExistingDirectory(self, "ffmpeg.exe があるフォルダ")
        if d:
            self.ffmpeg_dir.setText(d)

    def _check(self):
        tools = media.find_ffmpeg(self.ffmpeg_dir.text().strip() or None)
        self.ffmpeg_state.setText("利用できます" if tools.available else "見つかりません（ffmpeg と ffprobe が必要）")

    def _save(self):
        self.ctx.db.set_setting("ffmpeg_dir", self.ffmpeg_dir.text().strip() or None)
        self.ctx.db.set_setting("max_frames", str(self.frames.value()))
        self.ctx.data_changed.emit()
        QMessageBox.information(self, "設定", "保存しました。")

    def _reset(self):
        self.ctx.db.set_setting("initial_state", None)
        self.ctx.db.set_setting("initial_index", None)
        self.ctx.data_changed.emit()


# =============================================================================
# 下部の処理状況バー
# =============================================================================
class TaskBar(QWidget):
    def __init__(self, ctx: AppContext, parent=None):
        super().__init__(parent)
        self.ctx = ctx
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        self.label = QLabel("待機中")
        self.bar = QProgressBar()
        self.bar.setFixedWidth(200)
        self.cancel_button = QPushButton("処理をキャンセル")
        layout.addWidget(self.label, 1)
        layout.addWidget(self.bar)
        layout.addWidget(self.cancel_button)
        ctx.task_state_changed.connect(self._on_state)
        ctx.task_progress.connect(self._on_progress)
        self.cancel_button.clicked.connect(self._cancel)
        self._on_state(False)

    def _on_state(self, busy: bool):
        self.bar.setVisible(busy)
        self.cancel_button.setVisible(busy)
        self.cancel_button.setEnabled(True)
        if busy:
            self.bar.setRange(0, 0)  # 総量が分かるまでは割合を出さない
            self.label.setText(f"{self.ctx.task_label}を開始しています…")
        else:
            self.label.setText("待機中")

    def _on_progress(self, info: dict):
        self.label.setText(info.get("text", ""))
        total, done = info.get("total"), info.get("done")
        if total and done is not None:  # 総量が分かっているときだけ割合を表示
            self.bar.setRange(0, 1000)
            self.bar.setValue(int(1000 * min(done / total, 1)))
        else:
            self.bar.setRange(0, 0)

    def _cancel(self):
        self.ctx.cancel_task()
        self.cancel_button.setEnabled(False)
        self.label.setText("キャンセルしています…")

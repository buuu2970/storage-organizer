"""整理画面（担当: UI）。

中央にサムネイル一覧、右にプレビュー・詳細・推薦理由・比較を表示する。
調査は起動時に自動で始まり、見つかったものから順次表示する。ごみ箱への移動は確認画面から行う。
"""

from __future__ import annotations

import os
import subprocess
from functools import partial

from PySide6.QtCore import (QAbstractListModel, QModelIndex, QObject, QRunnable, QSize, Qt, QThread,
                            QThreadPool, QTimer, Signal)
from PySide6.QtGui import QImageReader, QPixmap
from PySide6.QtWidgets import (
    QAbstractItemView, QComboBox, QDialog, QDialogButtonBox, QDoubleSpinBox, QFrame,
    QHBoxLayout, QLabel, QLineEdit, QListView, QMessageBox, QPushButton, QScrollArea,
    QSplitter, QStackedWidget, QTableWidget, QTableWidgetItem, QTabWidget, QVBoxLayout, QWidget,
)

from . import autoscan, candidates, media, trash
from .db import Database, loads
from .formatting import format_bytes_exact, format_size
from .scan_worker import TaskWorker

THUMB = 128


# =============================================================================
# サムネイル（必要になったものだけ作り、キャッシュする）
# =============================================================================
class _ThumbJob(QRunnable):
    def __init__(self, loader, media_id, path, kind, out, tools):
        super().__init__()
        self.loader, self.media_id, self.path, self.kind, self.out, self.tools = \
            loader, media_id, path, kind, out, tools

    def run(self):
        ok = False
        try:
            if self.kind == "image":
                from PIL import Image
                with Image.open(self.path) as img:
                    img.draft("RGB", (THUMB * 2, THUMB * 2))
                    img = img.convert("RGB")
                    img.thumbnail((THUMB * 2, THUMB * 2))
                    img.save(self.out, "JPEG", quality=80)
                ok = True
            elif self.tools.available:
                info = media.probe(self.tools, self.path)
                paths = media.make_thumbnails(self.tools, self.path, info.get("duration", 0),
                                              self.out[:-4], None)
                if paths:
                    os.replace(paths[min(1, len(paths) - 1)], self.out)
                    for p in paths:
                        if os.path.exists(p):
                            os.remove(p)
                    ok = True
        except Exception:
            ok = False
        self.loader.done.emit(self.media_id, self.out if ok else "")


class ThumbnailLoader(QObject):
    done = Signal(int, str)
    updated = Signal(int)

    def __init__(self, db: Database, tools_fn) -> None:
        super().__init__()
        self.db = db
        self.tools_fn = tools_fn
        self.pool = QThreadPool()
        self.pool.setMaxThreadCount(2)
        self.cache: dict[int, QPixmap | None] = {}
        self.pending: set[int] = set()
        self.done.connect(self._on_done)

    def pixmap(self, row: dict) -> QPixmap | None:
        mid = row["id"]
        if mid in self.cache:
            return self.cache[mid]
        if row["cloud_only"] or mid in self.pending:
            return None
        out = str(self.db.thumb_dir / f"m{mid}_{int(row['mtime'])}_{row['size']}.jpg")
        if os.path.exists(out):
            self.cache[mid] = QPixmap(out).scaled(THUMB, THUMB, Qt.AspectRatioMode.KeepAspectRatio,
                                                  Qt.TransformationMode.SmoothTransformation)
            return self.cache[mid]
        self.pending.add(mid)
        self.pool.start(_ThumbJob(self, mid, row["path"], row["kind"], out, self.tools_fn()))
        return None

    def _on_done(self, mid: int, path: str) -> None:
        self.pending.discard(mid)
        pm = QPixmap(path) if path else QPixmap()
        self.cache[mid] = None if pm.isNull() else pm.scaled(
            THUMB, THUMB, Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation)
        self.updated.emit(mid)


# =============================================================================
# 一覧のモデル
# =============================================================================
SORTS = ["容量が大きい順", "撮影・録画日時が新しい順", "撮影・録画日時が古い順", "更新日時が新しい順",
         "更新日時が古い順", "動画時間が長い順", "1分あたりの容量が大きい順", "見返していない順"]


def sort_rows(rows: list[dict], how: str) -> list[dict]:
    """日時が不明なものは、ほかの日時で代用せず末尾に並べる。"""
    def unknown_last(key, reverse):
        known = [r for r in rows if r[key] is not None]
        unknown = [r for r in rows if r[key] is None]
        return sorted(known, key=lambda r: r[key], reverse=reverse) + unknown
    if how == "撮影・録画日時が新しい順":
        return unknown_last("taken_at", True)
    if how == "撮影・録画日時が古い順":
        return unknown_last("taken_at", False)
    if how == "更新日時が新しい順":
        return sorted(rows, key=lambda r: r["mtime"], reverse=True)
    if how == "更新日時が古い順":
        return sorted(rows, key=lambda r: r["mtime"])
    if how == "動画時間が長い順":
        return unknown_last("duration", True)
    if how == "1分あたりの容量が大きい順":
        return unknown_last("per_minute", True)
    if how == "見返していない順":  # このアプリで確認したことがないもの → 古く確認したもの
        never = [r for r in rows if r["last_viewed"] is None]
        seen = sorted((r for r in rows if r["last_viewed"] is not None), key=lambda r: r["last_viewed"])
        return never + seen
    return sorted(rows, key=lambda r: r["size"], reverse=True)


def row_dict(r, ctx: candidates.Context) -> dict:
    return {"id": r["id"], "name": r["name"], "path": r["path"], "folder": r["folder"], "size": r["size"],
            "mtime": r["mtime"], "taken_at": r["taken_at"], "kind": r["kind"], "cloud_only": bool(r["cloud_only"]),
            "duration": candidates.duration(r), "per_minute": candidates.per_minute(r),
            "last_viewed": r["last_viewed"], "protected": bool(r["protected"]), "decision": r["decision"],
            "group": None}


class MediaListModel(QAbstractListModel):
    def __init__(self, loader: ThumbnailLoader, parent=None) -> None:
        super().__init__(parent)
        self.items: list[dict] = []
        self.loader = loader
        loader.updated.connect(self._thumb_updated)

    def rowCount(self, parent=QModelIndex()):
        return 0 if parent.isValid() else len(self.items)

    def data(self, index, role=Qt.ItemDataRole.DisplayRole):
        if not index.isValid():
            return None
        it = self.items[index.row()]
        if role == Qt.ItemDataRole.DisplayRole:
            marks = ("🔒" if it["protected"] else "") + {"保留": "⏸", "削除候補": "🗑", "残す": "✓"}.get(
                it["decision"] or "", "")
            if it.get("group_label"):
                return f"{marks}{it['group_label']}"
            return f"{marks}{it['name']}\n{format_size(it['size'])}"
        if role == Qt.ItemDataRole.DecorationRole:
            pm = self.loader.pixmap(it)
            if pm is None:
                pm = QPixmap(THUMB, THUMB)
                pm.fill(Qt.GlobalColor.darkGray)
            return pm
        if role == Qt.ItemDataRole.ToolTipRole:
            return it["path"] + ("\n（オンライン専用ファイル: 内容は読み込みません）" if it["cloud_only"] else "")
        return None

    def set_items(self, items: list[dict]) -> None:
        self.beginResetModel()
        self.items = items
        self._index = {it["id"]: i for i, it in enumerate(items)}
        self.endResetModel()

    def _thumb_updated(self, mid: int) -> None:
        i = getattr(self, "_index", {}).get(mid)
        if i is not None:
            idx = self.index(i)
            self.dataChanged.emit(idx, idx, [Qt.ItemDataRole.DecorationRole])


# =============================================================================
# 自動スキャンの実行
# =============================================================================
class AutoScanRunner(QObject):
    progress = Signal(object)
    finished = Signal(object)
    state_changed = Signal(bool)

    def __init__(self, data_dir, tools_fn) -> None:
        super().__init__()
        self.data_dir, self.tools_fn = data_dir, tools_fn
        self.thread: QThread | None = None
        self.worker: TaskWorker | None = None
        self.ctl: autoscan.Controller | None = None

    def is_running(self) -> bool:
        return self.thread is not None

    def start(self) -> bool:
        if self.is_running():
            return False
        self.ctl = ctl = autoscan.Controller()
        data_dir, tools = self.data_dir, self.tools_fn()
        self.thread = QThread(self)
        self.worker = TaskWorker(lambda c, r: autoscan.task_autoscan(data_dir, tools, ctl, r))
        self.worker.moveToThread(self.thread)
        self.thread.started.connect(self.worker.run)
        self.worker.progress.connect(self.progress.emit)
        self.worker.task_finished.connect(self.finished.emit)
        self.worker.task_failed.connect(lambda m: self.finished.emit({"error": m}))
        self.worker.task_finished.connect(self.thread.quit)
        self.worker.task_failed.connect(self.thread.quit)
        self.thread.finished.connect(self._cleanup)
        self.thread.start()
        self.state_changed.emit(True)
        return True

    def _cleanup(self) -> None:
        for obj in (self.worker, self.thread):
            if obj is not None:
                obj.deleteLater()
        self.worker = self.thread = None
        self.state_changed.emit(False)

    def pause(self):
        if self.ctl:
            self.ctl.pause()

    def resume(self):
        if self.ctl:
            self.ctl.resume()

    def cancel(self):
        if self.ctl:
            self.ctl.cancel()

    def shutdown(self):
        self.cancel()
        if self.thread is not None:
            self.thread.quit()
            self.thread.wait(15000)


# =============================================================================
# 整理画面
# =============================================================================
class OrganizerPage(QWidget):
    def __init__(self, db: Database, tools_fn, parent=None) -> None:
        super().__init__(parent)
        self.db = db
        self.tools_fn = tools_fn
        self.category = "すべて"
        self.ctx: candidates.Context | None = None
        self.loader = ThumbnailLoader(db, tools_fn)
        self.runner = AutoScanRunner(db.data_dir, tools_fn)
        self._last_progress: dict = {}
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)

        # 上部: 調査状況
        top = QHBoxLayout()
        self.status_label = QLabel()
        self.status_label.setWordWrap(True)
        self.pause_button = QPushButton("一時停止")
        self.cancel_button = QPushButton("キャンセル")
        self.rescan_button = QPushButton("再スキャン")
        self.scope_button = QPushButton("調査範囲…")
        self.summary_button = QPushButton("容量の内訳…")
        top.addWidget(self.status_label, 1)
        for b in (self.pause_button, self.cancel_button, self.rescan_button, self.scope_button, self.summary_button):
            top.addWidget(b)
        layout.addLayout(top)
        self.totals_label = QLabel()
        layout.addWidget(self.totals_label)

        # 中央: 一覧
        splitter = QSplitter()
        center = QWidget()
        cv = QVBoxLayout(center)
        cv.setContentsMargins(0, 0, 0, 0)
        bar = QHBoxLayout()
        self.title_label = QLabel()
        self.title_label.setStyleSheet("font-weight: bold;")
        self.search = QLineEdit()
        self.search.setPlaceholderText("ファイル名・保存場所で検索")
        self.sort_combo = QComboBox()
        self.sort_combo.addItems(SORTS)
        bar.addWidget(self.title_label)
        bar.addWidget(self.search, 1)
        bar.addWidget(self.sort_combo)
        cv.addLayout(bar)
        self.model = MediaListModel(self.loader, self)
        self.view = QListView()
        self.view.setViewMode(QListView.ViewMode.IconMode)
        self.view.setResizeMode(QListView.ResizeMode.Adjust)
        self.view.setUniformItemSizes(True)
        self.view.setIconSize(QSize(THUMB, THUMB))
        self.view.setGridSize(QSize(THUMB + 40, THUMB + 48))
        self.view.setWordWrap(True)
        self.view.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.view.setModel(self.model)
        cv.addWidget(self.view, 1)
        actions = QHBoxLayout()
        self.count_label = QLabel()
        self.decision_buttons = {}
        actions.addWidget(self.count_label, 1)
        for name in ("残す", "削除候補", "保留", "判断を解除", "絶対に残す", "保護を解除"):
            b = QPushButton(name)
            b.clicked.connect(partial(self.apply_decision, name))
            actions.addWidget(b)
            self.decision_buttons[name] = b
        cv.addLayout(actions)
        trash_row = QHBoxLayout()
        self.target_button = QPushButton("目標容量から提案…")
        self.trash_label = QLabel()
        self.trash_button = QPushButton("削除候補をごみ箱へ移動…")
        trash_row.addWidget(self.target_button)
        trash_row.addWidget(self.trash_label, 1)
        trash_row.addWidget(self.trash_button)
        cv.addLayout(trash_row)
        splitter.addWidget(center)

        # 右: プレビュー・詳細・比較
        self.right = QStackedWidget()
        self.detail = DetailPanel(self)
        self.compare = ComparePanel(self)
        empty = QLabel("ファイルを選ぶと、プレビュー・詳細・推薦理由を表示します。")
        empty.setWordWrap(True)
        empty.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.right.addWidget(empty)
        self.right.addWidget(self.detail)
        self.right.addWidget(self.compare)
        splitter.addWidget(self.right)
        splitter.setSizes([760, 480])
        layout.addWidget(splitter, 1)

        self.refresh_timer = QTimer(self)
        self.refresh_timer.setSingleShot(True)
        self.refresh_timer.timeout.connect(self.refresh)
        self.pause_button.clicked.connect(self.toggle_pause)
        self.cancel_button.clicked.connect(self.runner.cancel)
        self.rescan_button.clicked.connect(self.start_scan)
        self.scope_button.clicked.connect(lambda: ScopeDialog(self.db, self).exec())
        self.summary_button.clicked.connect(lambda: SummaryDialog(self.db, self).exec())
        self.search.textChanged.connect(self.refresh)
        self.sort_combo.currentIndexChanged.connect(self.refresh)
        self.view.selectionModel().selectionChanged.connect(self.on_select)
        self.target_button.clicked.connect(lambda: TargetDialog(self).exec())
        self.trash_button.clicked.connect(self.open_trash_dialog)
        self.runner.progress.connect(self.on_progress)
        self.runner.finished.connect(self.on_finished)
        self.runner.state_changed.connect(self.update_status)
        self.update_status()
        self.refresh()

    # --- 調査 ---------------------------------------------------------------
    def start_scan(self) -> None:
        if self.runner.start():
            self.pause_button.setText("一時停止")

    def toggle_pause(self) -> None:
        if not self.runner.is_running():
            return
        if self.runner.ctl.paused:
            self.runner.resume()
            self.pause_button.setText("一時停止")
        else:
            self.runner.pause()
            self.pause_button.setText("再開")
        self.update_status()

    def on_progress(self, info: dict) -> None:
        self._last_progress = info
        self.update_status()
        if info.get("refresh") and not self.refresh_timer.isActive():
            self.refresh_timer.start(1500)  # 見つかったものを順次表示（まとめて更新）

    def on_finished(self, result: dict) -> None:
        self._last_progress = {}
        if result.get("error"):
            QMessageBox.warning(self, "調査", f"調査中にエラーが発生しました: {result['error']}")
        self.refresh()
        self.update_status(result)

    def update_status(self, result: dict | None = None) -> None:
        running = self.runner.is_running()
        self.pause_button.setEnabled(running)
        self.cancel_button.setEnabled(running)
        self.rescan_button.setEnabled(not running)
        info = self._last_progress
        skips = self.db.conn.execute("SELECT COUNT(*) FROM scan_skips WHERE kind='error'").fetchone()[0]
        not_done = self.db.conn.execute(
            "SELECT COUNT(*) FROM scan_locations WHERE status IN ('未調査','中断','調査中')").fetchone()[0]
        if running and self.runner.ctl and self.runner.ctl.paused:
            text = "一時停止中です。「再開」で続きを調べます。"
        elif running and info.get("phase") == "scan":
            text = (f"調査中: {info.get('location', '')} — {info.get('current', '')}\n"
                    f"検出 {info.get('found', 0):,} 件 / {format_size(info.get('bytes', 0))}"
                    "（全体の件数は調べ終わるまで分かりません）")
        elif running and info.get("text"):
            text = info["text"]
            if info.get("total"):
                text += f"（{min(info['done'] / info['total'], 1):.0%}）"
        elif running:
            text = "調査を開始しています…"
        else:
            last = self.db.get_setting("scan_finished")
            if last is None and not self.db.media():
                text = "まだ調査していません。「再スキャン」で開始します。"
            else:
                when = candidates.format_date(float(last)) if last else "未完了"
                text = f"調査結果を表示しています（最終完了: {when}）。起動するたびにバックグラウンドで更新します。"
            if result and result.get("cancelled"):
                text = "調査を中断しました。途中までの結果を表示しています。"
            notes = []
            if not_done:
                notes.append(f"未調査・中断した場所 {not_done} か所")
            if skips:
                notes.append(f"アクセスできずスキップした場所 {skips} か所")
            notes.append("除外範囲・外付けドライブなどは調べていません（「調査範囲…」で確認）")
            text += "\n" + " / ".join(notes)
        self.status_label.setText(text)

    # --- 一覧 ---------------------------------------------------------------
    def set_category(self, category: str) -> None:
        self.category = category
        self.refresh()

    def refresh(self) -> None:
        selected = set(self.selected_ids())
        self.ctx = ctx = candidates.Context(self.db)
        s = candidates.summary(self.db)
        self.totals_label.setText(
            f"画像 {s['image'][0]:,} 件・{format_size(s['image'][1])}　動画 {s['video'][0]:,} 件・"
            f"{format_size(s['video'][1])}"
            + (f"　（うちオンライン専用 {s['cloud_only'][0]:,} 件: 内容は解析していません）" if s["cloud_only"][0] else ""))
        ids = candidates.category_ids(ctx, self.category)
        rows = [row_dict(ctx.rows[i], ctx) for i in ids]
        text = self.search.text().strip().casefold()
        if text:
            rows = [r for r in rows if text in r["name"].casefold() or text in r["folder"].casefold()]
        rows = sort_rows(rows, self.sort_combo.currentText())
        if self.category in ("重複", "類似画像"):
            rows = self._group_items(rows)
        self.model.set_items(rows)
        self.title_label.setText(self.category)
        cnt = len({i for r in rows for i in (r.get("members") or [r["id"]])})
        self.count_label.setText(f"{cnt:,} 件（{format_size(sum(r['size'] for r in rows))}）")
        trash_rows = [r for r in ctx.rows.values() if r["decision"] == "削除候補"]
        self.trash_label.setText(f"削除候補 {len(trash_rows):,} 件・{format_size(sum(r['size'] for r in trash_rows))}"
                                 "（初期状態では何も選んでいません）")
        self.trash_button.setEnabled(bool(trash_rows))
        for i, it in enumerate(self.model.items):
            if it["id"] in selected:
                self.view.selectionModel().select(self.model.index(i),
                                                  self.view.selectionModel().SelectionFlag.Select)
        self.on_select()

    def _group_items(self, rows: list[dict]) -> list[dict]:
        ctx = self.ctx
        groups = ctx.dup_groups if self.category == "重複" else ctx.sim_groups
        keeps = ctx.dup_keep if self.category == "重複" else ctx.sim_keep
        of = ctx.dup_of if self.category == "重複" else ctx.sim_of
        present = {r["id"] for r in rows}
        items, done = [], set()
        for r in rows:
            key = of.get(r["id"])
            if key is None or key in done:
                continue
            done.add(key)
            members = [m for m in groups[key] if m in present]
            keep_id = keeps[key][0]
            rep = dict(row_dict(ctx.rows[keep_id], ctx))
            extra = sum(ctx.rows[m]["size"] for m in members if m != keep_id)
            label = "重複分" if self.category == "重複" else "おすすめ以外"
            rep.update(group=(self.category, key), members=members, size=extra,
                       group_label=f"{len(members)} 件のグループ\n{label} {format_size(extra)}")
            items.append(rep)
        return items

    def selected_ids(self) -> list[int]:
        out = []
        for idx in self.view.selectionModel().selectedIndexes():
            it = self.model.items[idx.row()]
            out.extend(it.get("members") or [it["id"]])
        return out

    def on_select(self, *_) -> None:
        idxs = self.view.selectionModel().selectedIndexes()
        if not idxs:
            self.right.setCurrentIndex(0)
            self.detail.player.stop()
            return
        it = self.model.items[idxs[0].row()]
        if it.get("group"):
            self.detail.player.stop()
            self.compare.show_group(*it["group"])
            self.right.setCurrentWidget(self.compare)
        else:
            self.detail.show_media(it["id"])
            self.right.setCurrentWidget(self.detail)

    # --- 判断 ---------------------------------------------------------------
    def apply_decision(self, name: str, ids: list[int] | None = None) -> None:
        ids = ids if ids is not None else self.selected_ids()
        skipped = 0
        for mid in ids:
            if name == "絶対に残す":
                self.db.set_protected(mid, True)
            elif name == "保護を解除":
                self.db.set_protected(mid, False)
            else:
                try:
                    self.db.set_decision(mid, None if name == "判断を解除" else name)
                except PermissionError:
                    skipped += 1
        if skipped:
            QMessageBox.information(self, "判断", f"「絶対に残す」が付いた {skipped} 件は削除候補にしませんでした。")
        self.refresh()

    def open_trash_dialog(self) -> None:
        ids = [r["id"] for r in self.ctx.rows.values() if r["decision"] == "削除候補"]
        if ids:
            dlg = TrashDialog(self.db, ids, self)
            if dlg.exec() == QDialog.DialogCode.Accepted:
                self.refresh()

    def shutdown(self) -> None:
        self.runner.shutdown()
        self.detail.player.stop()
        self.loader.pool.waitForDone(5000)


# =============================================================================
# 右側: 1件の詳細
# =============================================================================
class DetailPanel(QWidget):
    def __init__(self, page: OrganizerPage) -> None:
        super().__init__()
        from .pages import VideoPlayer
        self.page = page
        self.media_id: int | None = None
        v = QVBoxLayout(self)
        self.stack = QStackedWidget()
        self.image = QLabel()
        self.image.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.image.setMinimumHeight(240)
        self.image.setStyleSheet("background: #222; color: #ccc;")
        self.player = VideoPlayer()
        self.stack.addWidget(self.image)
        self.stack.addWidget(self.player)
        v.addWidget(self.stack, 1)
        self.info = QLabel()
        self.info.setWordWrap(True)
        self.info.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.info.setAlignment(Qt.AlignmentFlag.AlignTop)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(self.info)
        v.addWidget(scroll, 1)
        row = QHBoxLayout()
        self.buttons = {}
        for name in ("残す", "削除候補", "保留", "絶対に残す"):
            b = QPushButton(name)
            b.clicked.connect(partial(self._decide, name))
            row.addWidget(b)
            self.buttons[name] = b
        self.open_button = QPushButton("保存場所を開く")
        self.open_button.clicked.connect(self._open_folder)
        row.addWidget(self.open_button)
        v.addLayout(row)

    def show_media(self, media_id: int) -> None:
        db = self.page.db
        r = db.video(media_id)
        if r is None:
            return
        if media_id != self.media_id:
            db.mark_viewed(media_id)  # 「見返していない順」のため、このアプリで確認した日時を記録
            self.media_id = media_id
            self._load_preview(r)
            r = db.video(media_id)
        ctx = self.page.ctx or candidates.Context(db)
        meta = loads(r["meta"], {}) or {}
        lines = [f"<b>{r['name']}</b>", f"保存場所: {r['folder']}",
                 f"容量: {format_size(r['size'])}（{format_bytes_exact(r['size'])}）"]
        if meta.get("width"):
            lines.append(f"解像度: {meta['width']}×{meta['height']}")
        label = "撮影日時" if r["kind"] == "image" else "録画日時（動画内の記録）"
        lines.append(f"{label}: {candidates.format_date(r['taken_at'])}")
        lines.append(f"更新日時: {candidates.format_date(r['mtime'])}")
        d = candidates.duration(r)
        if r["kind"] == "video":
            lines.append(f"再生時間: {f'{d:.0f} 秒' if d else '不明'}")
            pm = candidates.per_minute(r)
            lines.append(f"1分あたりの容量: {format_size(int(pm)) if pm else '不明'}")
        started = db.get_setting("view_history_started")
        lines.append("このアプリでの最終確認: " + candidates.format_date(r["last_viewed"])
                     + (f"（記録開始 {candidates.format_date(float(started))} より前は不明）" if started else ""))
        state = []
        if r["protected"]:
            state.append("絶対に残す")
        if r["decision"]:
            state.append(r["decision"])
        lines.append("判断: " + ("・".join(state) if state else "未判断"))
        if r["cloud_only"]:
            lines.append("<span style='color:#b06000'>オンライン専用ファイルです。自動ダウンロードしないため、"
                         "内容の解析・プレビュー・ごみ箱への移動は行いません。</span>")
        if r["light_note"]:
            lines.append(f"<span style='color:#b06000'>{r['light_note']}</span>")
        reasons = ctx.reasons(r)
        lines.append("<b>候補になった理由</b>" if reasons else "<b>候補になった理由</b>: 特にありません")
        lines += [f"・{x}" for x in reasons]
        lines.append("<span style='color:gray'>内容の面白さや大切さは判断していません。プレビューで確認してください。</span>")
        self.info.setText("<br>".join(lines))
        self.buttons["絶対に残す"].setText("保護を解除" if r["protected"] else "絶対に残す")

    def _load_preview(self, r) -> None:
        self.player.stop()
        if r["cloud_only"]:
            self.stack.setCurrentWidget(self.image)
            self.image.setText("オンライン専用ファイルのためプレビューしません")
            return
        if r["kind"] == "video":
            self.stack.setCurrentWidget(self.player)
            self.player.set_source(r["path"])
            return
        self.stack.setCurrentWidget(self.image)
        reader = QImageReader(r["path"])
        reader.setAutoTransform(True)
        size = reader.size()
        if size.isValid() and (size.width() > 1600 or size.height() > 1600):
            reader.setScaledSize(size.scaled(1600, 1600, Qt.AspectRatioMode.KeepAspectRatio))
        img = reader.read()
        if img.isNull():
            self.image.setText(f"プレビューできません（{reader.errorString()}）")
        else:
            self.image.setPixmap(QPixmap.fromImage(img).scaled(
                460, 340, Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation))

    def _decide(self, name: str) -> None:
        if self.media_id is None:
            return
        if name == "絶対に残す" and self.page.db.video(self.media_id)["protected"]:
            name = "保護を解除"
        self.page.apply_decision(name, [self.media_id])

    def _open_folder(self) -> None:
        if self.media_id is not None and os.name == "nt":
            path = self.page.db.video(self.media_id)["path"]
            subprocess.Popen(["explorer", "/select,", path])


# =============================================================================
# 右側: 比較（重複・類似）
# =============================================================================
class ComparePanel(QWidget):
    def __init__(self, page: OrganizerPage) -> None:
        super().__init__()
        self.page = page
        self.group = None
        v = QVBoxLayout(self)
        self.header = QLabel()
        self.header.setWordWrap(True)
        v.addWidget(self.header)
        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        v.addWidget(self.scroll, 1)
        self.keep_others_button = QPushButton("おすすめ以外を削除候補にする")
        self.keep_others_button.clicked.connect(self._mark_others)
        v.addWidget(self.keep_others_button)
        self.cards: list[QWidget] = []

    def show_group(self, category: str, key) -> None:
        ctx = self.page.ctx
        self.group = (category, key)
        dup = category == "重複"
        ids = (ctx.dup_groups if dup else ctx.sim_groups).get(key, [])
        keep_id, why = (ctx.dup_keep if dup else ctx.sim_keep).get(key, (None, ""))
        self.header.setText(
            ("<b>完全重複</b>: 内容が完全に一致するファイルです（SHA-256 で確認）。最低1つは残す前提です。"
             if dup else "<b>類似画像</b>: 似ている画像です（完全一致ではありません。縮小・連写・編集違いなど）。")
            + f"<br>残すおすすめ: {ctx.rows[keep_id]['name'] if keep_id else '-'}（{why}）"
            "<br><span style='color:gray'>おすすめは解像度などの客観的な情報だけで選んでいます。大切さは判断していません。</span>")
        box = QWidget()
        h = QHBoxLayout(box)
        ids = sorted(ids, key=lambda m: (m != keep_id, -candidates.pixels(ctx.rows[m]), -ctx.rows[m]["size"]))
        for mid in ids:  # 残すおすすめを先頭に、解像度の高い順
            h.addWidget(self._card(ctx.rows[mid], mid == keep_id, ids))
        h.addStretch()
        self.scroll.setWidget(box)

    def _card(self, r, recommended: bool, ids) -> QWidget:
        ctx = self.page.ctx
        card = QFrame()
        card.setFrameShape(QFrame.Shape.StyledPanel)
        card.setFixedWidth(230)
        v = QVBoxLayout(card)
        thumb = QLabel()
        thumb.setFixedSize(210, 160)
        thumb.setAlignment(Qt.AlignmentFlag.AlignCenter)
        thumb.setStyleSheet("background:#222;color:#ccc;")
        pm = self.page.loader.pixmap(row_dict(r, ctx))
        if pm is not None:
            thumb.setPixmap(pm.scaled(210, 160, Qt.AspectRatioMode.KeepAspectRatio))
        else:
            thumb.setText("読み込み中…" if not r["cloud_only"] else "オンライン専用")
        v.addWidget(thumb)
        meta = loads(r["meta"], {}) or {}
        exact = [i for i in ids if i != r["id"] and r["sha256"] and ctx.rows[i]["sha256"] == r["sha256"]]
        badges = []
        if recommended:
            badges.append("<b style='color:#0a7'>残すおすすめ</b>")
        if r["protected"]:
            badges.append("<b>絶対に残す</b>")
        if r["decision"]:
            badges.append(r["decision"])
        if exact and self.group and self.group[0] == "類似画像":
            badges.append(f"完全一致 {len(exact)} 件あり")
        info = QLabel("<br>".join([
            r["name"], " ".join(badges),
            f"解像度: {meta.get('width', '?')}×{meta.get('height', '?')}" if meta.get("width") else "解像度: 不明",
            f"容量: {format_size(r['size'])}",
            f"撮影日時: {candidates.format_date(r['taken_at'])}",
            f"更新日時: {candidates.format_date(r['mtime'])}",
            f"<span style='color:gray'>{r['folder']}</span>"]))
        info.setWordWrap(True)
        v.addWidget(info)
        row = QHBoxLayout()
        for name in ("残す", "削除候補"):
            b = QPushButton(name)
            b.clicked.connect(partial(self._decide, name, r["id"]))
            row.addWidget(b)
        v.addLayout(row)
        prot = QPushButton("保護を解除" if r["protected"] else "絶対に残す")
        prot.clicked.connect(partial(self._decide, "保護を解除" if r["protected"] else "絶対に残す", r["id"]))
        v.addWidget(prot)
        return card

    def _decide(self, name, mid) -> None:
        self.page.apply_decision(name, [mid])

    def _mark_others(self) -> None:
        if not self.group:
            return
        category, key = self.group
        ctx = self.page.ctx
        dup = category == "重複"
        ids = (ctx.dup_groups if dup else ctx.sim_groups).get(key, [])
        keep_id = (ctx.dup_keep if dup else ctx.sim_keep)[key][0]
        self.page.apply_decision("残す", [keep_id])
        self.page.apply_decision("削除候補", [i for i in ids if i != keep_id and not ctx.rows[i]["protected"]])


# =============================================================================
# ダイアログ
# =============================================================================
def _table(headers: list[str]) -> QTableWidget:
    t = QTableWidget(0, len(headers))
    t.setHorizontalHeaderLabels(headers)
    t.verticalHeader().setVisible(False)
    t.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
    t.horizontalHeader().setStretchLastSection(True)
    return t


def _fill(table: QTableWidget, rows: list[list[str]]) -> None:
    table.setRowCount(len(rows))
    for i, row in enumerate(rows):
        for j, value in enumerate(row):
            table.setItem(i, j, QTableWidgetItem(str(value)))
    table.resizeColumnsToContents()


class ScopeDialog(QDialog):
    """調査した場所・できなかった場所・除外範囲。"""

    def __init__(self, db: Database, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("調査範囲")
        self.resize(900, 600)
        v = QVBoxLayout(self)
        v.addWidget(QLabel("除外範囲や外付けドライブなど、調べていない場所があります。"
                           "このアプリは「PC内のすべてを確認済み」とは表示しません。"))
        tabs = QTabWidget()
        self.locations = _table(["場所", "保存先", "状態", "件数", "容量"])
        _fill(self.locations, [[r["label"], r["path"], r["status"], f"{r['files']:,}", format_size(r["bytes"])]
                               for r in db.conn.execute("SELECT * FROM scan_locations ORDER BY priority")])
        tabs.addTab(self.locations, "調査した場所")
        self.errors = _table(["スキップした場所", "理由"])
        _fill(self.errors, [[r["path"], r["reason"]] for r in db.conn.execute(
            "SELECT * FROM scan_skips WHERE kind='error' ORDER BY path LIMIT 2000")])
        tabs.addTab(self.errors, "アクセスできなかった場所")
        self.excluded = _table(["除外した場所", "理由"])
        _fill(self.excluded, [[r["path"], r["reason"]] for r in db.conn.execute(
            "SELECT * FROM scan_skips WHERE kind='excluded' ORDER BY path LIMIT 2000")])
        tabs.addTab(self.excluded, "除外した場所")
        rules = QLabel("<br>".join(["<b>自動で除外する範囲</b>"] + [f"・{x}" for x in autoscan.EXCLUSION_RULES]
                                   + ["", "<b>自動では調べていないドライブ</b>"]
                                   + [f"・{x}" for x in db.json_list("not_scanned_drives")]
                                   + (["・なし（または検出できません）"] if not db.json_list("not_scanned_drives") else [])
                                   + ["", "設定画面で、外付けドライブやフォルダを追加したり、スキャン対象外のフォルダを指定したりできます。"]))
        rules.setWordWrap(True)
        rules.setAlignment(Qt.AlignmentFlag.AlignTop)
        tabs.addTab(rules, "除外のルール")
        v.addWidget(tabs, 1)
        bb = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        bb.rejected.connect(self.reject)
        v.addWidget(bb)


class SummaryDialog(QDialog):
    def __init__(self, db: Database, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("容量の内訳")
        self.resize(820, 560)
        s = candidates.summary(db)
        v = QVBoxLayout(self)
        v.addWidget(QLabel(f"画像 {s['image'][0]:,} 件・{format_size(s['image'][1])}　"
                           f"動画 {s['video'][0]:,} 件・{format_size(s['video'][1])}　（ファイルサイズの合計）"))
        tabs = QTabWidget()
        self.folders = _table(["フォルダ", "件数", "容量"])
        _fill(self.folders, [[f, f"{n:,}", format_size(b)] for f, n, b in s["folders"][:500]])
        self.exts = _table(["拡張子", "件数", "容量"])
        _fill(self.exts, [[e, f"{n:,}", format_size(b)] for e, n, b in s["exts"]])
        tabs.addTab(self.folders, "フォルダ別")
        tabs.addTab(self.exts, "拡張子別")
        v.addWidget(tabs, 1)
        bb = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        bb.rejected.connect(self.reject)
        v.addWidget(bb)


class TargetDialog(QDialog):
    """「20GB空けたい」などの目標から、整理候補の組み合わせを提案する。"""

    def __init__(self, page: OrganizerPage) -> None:
        super().__init__(page)
        self.page = page
        self.setWindowTitle("目標容量から提案")
        self.resize(900, 560)
        v = QVBoxLayout(self)
        row = QHBoxLayout()
        self.target = QDoubleSpinBox()
        self.target.setRange(0.1, 100000)
        self.target.setValue(20)
        self.target.setSuffix(" GB")
        self.propose_button = QPushButton("提案する")
        row.addWidget(QLabel("空けたい容量"))
        row.addWidget(self.target)
        row.addWidget(self.propose_button)
        row.addStretch()
        v.addLayout(row)
        v.addWidget(QLabel("提案の対象は、完全重複の重複分・類似画像のおすすめ以外・失敗した可能性のあるファイルです。"
                           "「絶対に残す」「残す」と判断したもの、大容量という理由だけのものは含めません。"))
        self.table = _table(["選択", "ファイル", "容量", "理由"])
        v.addWidget(self.table, 1)
        self.result = QLabel()
        self.result.setWordWrap(True)
        v.addWidget(self.result)
        bb = QDialogButtonBox()
        self.apply_button = bb.addButton("選んだファイルを削除候補にする", QDialogButtonBox.ButtonRole.AcceptRole)
        bb.addButton("閉じる", QDialogButtonBox.ButtonRole.RejectRole)
        bb.accepted.connect(self.apply)
        bb.rejected.connect(self.reject)
        v.addWidget(bb)
        self.propose_button.clicked.connect(self.propose)
        self.table.itemChanged.connect(self.update_total)
        self.ids: list[int] = []

    def propose(self) -> None:
        ctx = self.page.ctx
        target = int(self.target.value() * 1024 ** 3)
        prop = candidates.propose_for_target(ctx, target)
        self.ids = prop["ids"]
        self.table.blockSignals(True)
        self.table.setRowCount(len(self.ids))
        for i, mid in enumerate(self.ids):
            r = ctx.rows[mid]
            check = QTableWidgetItem()
            check.setFlags(Qt.ItemFlag.ItemIsUserCheckable | Qt.ItemFlag.ItemIsEnabled)
            check.setCheckState(Qt.CheckState.Checked)
            self.table.setItem(i, 0, check)
            self.table.setItem(i, 1, QTableWidgetItem(r["path"]))
            self.table.setItem(i, 2, QTableWidgetItem(format_size(r["size"])))
            self.table.setItem(i, 3, QTableWidgetItem(" / ".join(ctx.reasons(r))))
        self.table.blockSignals(False)
        self.table.resizeColumnToContents(0)
        self.table.setColumnWidth(1, 360)
        self.update_total()

    def chosen(self) -> list[int]:
        return [mid for i, mid in enumerate(self.ids)
                if self.table.item(i, 0).checkState() == Qt.CheckState.Checked]

    def update_total(self, *_) -> None:
        ctx = self.page.ctx
        target = int(self.target.value() * 1024 ** 3)
        total = sum(ctx.rows[m]["size"] for m in self.chosen())
        text = f"見込みの空き容量: {format_size(total)} / 目標 {format_size(target)}"
        if total < target:
            text += f"　不足 {format_size(target - total)}（候補が足りないため、ほかのファイルは追加していません）"
        text += "\n※ ごみ箱へ移動しただけでは容量は空きません。ごみ箱を空にすると空きます。"
        self.result.setText(text)

    def apply(self) -> None:
        chosen = self.chosen()
        self.page.apply_decision("削除候補", chosen)
        self.accept()


class TrashDialog(QDialog):
    """削除前の確認。ファイル名・保存場所・件数・合計容量を表示し、ごみ箱へ移動する。"""

    def __init__(self, db: Database, ids: list[int], parent=None, backend=None) -> None:
        super().__init__(parent)
        self.db, self.ids, self.backend = db, ids, backend
        self.results: list[trash.TrashResult] = []
        self.setWindowTitle("ごみ箱へ移動する前の確認")
        self.resize(940, 580)
        v = QVBoxLayout(self)
        v.addWidget(QLabel("次のファイルを Windows のごみ箱へ移動します（完全削除はしません）。"
                           "チェックを外したファイルは移動しません。"))
        self.table = _table(["移動", "ファイル名", "保存場所", "容量"])
        ctx = candidates.Context(db)
        self.table.setRowCount(len(ids))
        for i, mid in enumerate(ids):
            r = db.video(mid)
            check = QTableWidgetItem()
            check.setFlags(Qt.ItemFlag.ItemIsUserCheckable | Qt.ItemFlag.ItemIsEnabled)
            check.setCheckState(Qt.CheckState.Checked)
            self.table.setItem(i, 0, check)
            for j, value in enumerate((r["name"], r["folder"], format_size(r["size"])), 1):
                self.table.setItem(i, j, QTableWidgetItem(value))
        self.table.resizeColumnsToContents()
        v.addWidget(self.table, 1)
        self.summary = QLabel()
        self.summary.setWordWrap(True)
        v.addWidget(self.summary)
        self.warning = QLabel()
        self.warning.setWordWrap(True)
        self.warning.setStyleSheet("color:#b00020;")
        v.addWidget(self.warning)
        self.ctx = ctx
        bb = QDialogButtonBox()
        self.move_button = bb.addButton("ごみ箱へ移動", QDialogButtonBox.ButtonRole.AcceptRole)
        bb.addButton("キャンセル", QDialogButtonBox.ButtonRole.RejectRole)
        bb.accepted.connect(self.execute)
        bb.rejected.connect(self.reject)
        v.addWidget(bb)
        self.table.itemChanged.connect(self.update_summary)
        self.update_summary()

    def chosen(self) -> list[int]:
        return [mid for i, mid in enumerate(self.ids) if self.table.item(i, 0).checkState() == Qt.CheckState.Checked]

    def update_summary(self, *_) -> None:
        ids = self.chosen()
        total = sum(self.ctx.rows[m]["size"] for m in ids if m in self.ctx.rows)
        self.summary.setText(f"{len(ids):,} 件・合計 {format_size(total)}（{format_bytes_exact(total)}）\n"
                             "ごみ箱を空にするまで、ディスクの空き容量は増えません。"
                             "移動の直前に、スキャンした後にファイルが変更されていないかを確認します。")
        problems = candidates.protect_group_rule(self.ctx, set(ids))
        self.warning.setText(("⚠ " + "\n⚠ ".join(problems) + "\n→ それぞれ残すおすすめの1つは移動しません。")
                             if problems else "")
        self.move_button.setEnabled(bool(ids))

    def execute(self) -> None:
        self.results = trash.move_to_trash(self.db, self.chosen(), self.backend)
        ok = [r for r in self.results if r.ok]
        ng = [r for r in self.results if not r.ok]
        text = f"ごみ箱へ移動: {len(ok)} 件（{format_size(sum(r.size for r in ok))}）"
        if ng:
            text += f"\n移動しなかった: {len(ng)} 件\n" + "\n".join(f"・{os.path.basename(r.path)}: {r.message}"
                                                          for r in ng[:20])
        (QMessageBox.warning if ng else QMessageBox.information)(self, "ごみ箱への移動", text)
        self.accept()


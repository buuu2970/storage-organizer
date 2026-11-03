"""整理画面（担当: UI）。

ホーム・ファイル一覧・整理のおすすめ・詳細パネル・ステータス欄。
調査は起動時に自動で始まり、見つかったものから順次表示する。ごみ箱への移動は確認画面から行う。
見た目は theme.py にまとめ、ここでは配置と操作の流れを扱う。
"""

from __future__ import annotations

import os
import shutil
import subprocess
from functools import partial

from PySide6.QtCore import (QAbstractTableModel, QItemSelectionModel, QModelIndex, QObject, QRectF, QRunnable,
                            QSize, Qt, QThread, QThreadPool, QTimer, Signal)
from PySide6.QtGui import QColor, QFont, QImageReader, QPainter, QPen, QPixmap
from PySide6.QtWidgets import (
    QAbstractItemView, QComboBox, QDialog, QDialogButtonBox, QDoubleSpinBox, QFileDialog, QFormLayout, QFrame,
    QGridLayout, QHBoxLayout, QLabel, QLineEdit, QListView, QListWidget, QListWidgetItem, QMenu, QMessageBox,
    QProgressBar, QPushButton, QScrollArea, QSplitter, QStackedWidget, QStyle, QStyledItemDelegate, QTabBar,
    QTableView, QTableWidget, QTableWidgetItem, QTabWidget, QToolButton, QTreeWidget, QTreeWidgetItem,
    QVBoxLayout, QWidget,
)

from . import autoscan, candidates, media, theme, trash
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

    def _image(self) -> None:
        try:
            from PIL import Image
            with Image.open(self.path) as img:
                img.draft("RGB", (THUMB * 2, THUMB * 2))
                small = img.convert("RGB")
                small.thumbnail((THUMB * 2, THUMB * 2))
                small.save(self.out, "JPEG", quality=80)
            return
        except Exception as first:
            # Pillow で読めない場合は Qt の読み込みでも試す
            reader = QImageReader(self.path)
            reader.setAutoTransform(True)
            size = reader.size()
            if size.isValid():
                reader.setScaledSize(size.scaled(THUMB * 2, THUMB * 2, Qt.AspectRatioMode.KeepAspectRatio))
            img = reader.read()
            if img.isNull() or not img.save(self.out, "JPG"):
                raise RuntimeError(f"画像を読み込めません（{type(first).__name__}: {first}）") from first

    def _video(self) -> None:
        if not self.tools.available:
            raise RuntimeError("FFmpeg が見つかりません（設定で ffmpeg のフォルダを指定してください）")
        info = media.probe(self.tools, self.path)
        paths = media.make_thumbnails(self.tools, self.path, info.get("duration", 0), self.out[:-4], None)
        if not paths:
            raise RuntimeError("動画から場面を取り出せませんでした")
        os.replace(paths[min(1, len(paths) - 1)], self.out)
        for p in paths:
            if os.path.exists(p):
                os.remove(p)  # アプリのキャッシュ内の一時画像だけを消す

    def run(self):
        try:
            self._image() if self.kind == "image" else self._video()
            if not os.path.exists(self.out):
                raise RuntimeError("サムネイルの保存に失敗しました")
            self.loader.done.emit(self.media_id, self.out, "")
        except Exception as e:
            self.loader.done.emit(self.media_id, "", str(e) or type(e).__name__)


class ThumbnailLoader(QObject):
    done = Signal(int, str, str)  # 動画ID, サムネイルのパス, エラー内容
    updated = Signal(int)

    def __init__(self, db: Database, tools_fn) -> None:
        super().__init__()
        self.db = db
        self.tools_fn = tools_fn
        self.pool = QThreadPool()
        self.pool.setMaxThreadCount(2)
        self.cache: dict[int, QPixmap | None] = {}
        self.errors: dict[int, str] = {}
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

    def state(self, row: dict) -> str:
        """ready（表示できる） / pending（作成中） / failed（作れなかった） / cloud（オンライン専用）"""
        if row["cloud_only"]:
            return "cloud"
        if self.cache.get(row["id"]) is not None:
            return "ready"
        if row["id"] in self.cache:
            return "failed"
        return "pending"

    def _on_done(self, mid: int, path: str, error: str = "") -> None:
        self.pending.discard(mid)
        pm = QPixmap(path) if path else QPixmap()
        if pm.isNull():
            self.errors[mid] = error or "作成した画像を表示できませんでした"
        else:
            self.errors.pop(mid, None)
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


# =============================================================================
# 一覧のモデルとカード描画
# =============================================================================
COLUMNS = ["名前", "種類", "容量", "撮影・録画日時", "更新日時", "判断", "保存場所"]
KIND_LABEL = {"image": "画像", "video": "動画"}


def decision_text(it: dict) -> str:
    if it.get("protected"):
        return "保護中"
    return it.get("decision") or "未判断"


def format_duration_short(sec: float | None) -> str:
    if not sec:
        return ""
    sec = int(round(sec))
    h, rem = divmod(sec, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


class MediaModel(QAbstractTableModel):
    """カード表示（1列目だけ使う）と詳細リスト表示（全列）で共有するモデル。"""

    def __init__(self, loader: ThumbnailLoader, parent=None) -> None:
        super().__init__(parent)
        self.items: list[dict] = []
        self._index: dict[int, int] = {}
        self.loader = loader
        loader.updated.connect(self._thumb_updated)

    def rowCount(self, parent=QModelIndex()):
        return 0 if parent.isValid() else len(self.items)

    def columnCount(self, parent=QModelIndex()):
        return 0 if parent.isValid() else len(COLUMNS)

    def headerData(self, section, orientation, role=Qt.ItemDataRole.DisplayRole):
        if role == Qt.ItemDataRole.DisplayRole and orientation == Qt.Orientation.Horizontal:
            return COLUMNS[section]
        return None

    def data(self, index, role=Qt.ItemDataRole.DisplayRole):
        if not index.isValid():
            return None
        it = self.items[index.row()]
        col = index.column()
        if role == Qt.ItemDataRole.UserRole:
            return it
        if role == Qt.ItemDataRole.DisplayRole:
            if col == 0:
                return it.get("group_title") or it["name"]
            if col == 1:
                return KIND_LABEL.get(it["kind"], "") + (f"（{len(it['members'])}件）" if it.get("members") else "")
            if col == 2:
                return format_size(it["size"])
            if col == 3:
                return candidates.format_date(it["taken_at"])
            if col == 4:
                return candidates.format_date(it["mtime"])
            if col == 5:
                return decision_text(it)
            if col == 6:
                return it["folder"]
        if role == Qt.ItemDataRole.DecorationRole and col == 0:
            return theme.icon("film" if it["kind"] == "video" else "image", theme.TEXT_SUB, 16)
        if role == Qt.ItemDataRole.TextAlignmentRole and col == 2:
            return int(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        if role == Qt.ItemDataRole.ToolTipRole:
            tip = it["path"]
            if it.get("members"):
                tip = f"{it['group_title']}\n代表: {it['path']}"
            if it["cloud_only"]:
                tip += "\nオンライン専用ファイルです（内容は読み込みません）"
            if it["id"] in self.loader.errors:
                tip += "\nプレビューを作れませんでした: " + self.loader.errors[it["id"]]
            return tip
        return None

    def set_items(self, items: list[dict]) -> None:
        self.beginResetModel()
        self.items = items
        self._index = {it["id"]: i for i, it in enumerate(items)}
        self.endResetModel()

    def _thumb_updated(self, mid: int) -> None:
        i = self._index.get(mid)
        if i is not None:
            idx = self.index(i, 0)
            self.dataChanged.emit(idx, idx, [Qt.ItemDataRole.DecorationRole])


CARD_W, CARD_H, CARD_PAD, THUMB_H = 184, 214, 6, 116


class CardDelegate(QStyledItemDelegate):
    """カード（サムネイル・ファイル名・容量・種類・判断状態）を描画する。Widget は作らない。"""

    def __init__(self, loader: ThumbnailLoader, parent=None) -> None:
        super().__init__(parent)
        self.loader = loader

    def sizeHint(self, option, index):
        return QSize(CARD_W, CARD_H)

    def paint(self, painter: QPainter, option, index) -> None:
        it = index.data(Qt.ItemDataRole.UserRole)
        if it is None:
            return
        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        rect = QRectF(option.rect).adjusted(CARD_PAD, CARD_PAD, -CARD_PAD, -CARD_PAD)
        selected = bool(option.state & QStyle.StateFlag.State_Selected)
        hover = bool(option.state & QStyle.StateFlag.State_MouseOver)
        focus = bool(option.state & QStyle.StateFlag.State_HasFocus)
        # カード（選択・ホバー・フォーカスを区別する）
        painter.setPen(QPen(QColor(theme.ACCENT if selected else ("#B0B0B0" if hover else theme.BORDER)),
                            2 if selected else 1))
        painter.setBrush(QColor(theme.ACCENT_SOFT if selected else theme.SURFACE))
        painter.drawRoundedRect(rect, theme.RADIUS, theme.RADIUS)
        if focus:
            pen = QPen(QColor(theme.FOCUS), 1, Qt.PenStyle.DashLine)
            painter.setPen(pen)
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawRoundedRect(rect.adjusted(3, 3, -3, -3), theme.RADIUS - 2, theme.RADIUS - 2)
        # サムネイル（縦横比を保ち、引き伸ばさない）
        thumb = QRectF(rect.left() + 8, rect.top() + 8, rect.width() - 16, THUMB_H)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor("#EFEFEF"))
        painter.drawRoundedRect(thumb, 6, 6)
        pm = self.loader.pixmap(it)
        if pm is not None and not pm.isNull():
            scaled = pm.size().scaled(thumb.size().toSize(), Qt.AspectRatioMode.KeepAspectRatio)
            if scaled.width() > pm.width() or scaled.height() > pm.height():
                scaled = pm.size()  # 小さい画像は拡大しない
            target = QRectF(thumb.center().x() - scaled.width() / 2, thumb.center().y() - scaled.height() / 2,
                            scaled.width(), scaled.height())
            painter.drawPixmap(target.toRect(), pm)
        else:
            state = self.loader.state(it)
            label = {"pending": "読み込み中…", "cloud": "オンライン専用"}.get(state, "プレビューなし")
            ic = theme.pixmap("film" if it["kind"] == "video" else "image", theme.TEXT_SUB, 28)
            painter.drawPixmap(int(thumb.center().x() - 14), int(thumb.center().y() - 24), ic)
            painter.setPen(QColor(theme.TEXT_SUB))
            painter.setFont(theme.font(theme.SIZE_CAPTION))
            painter.drawText(QRectF(thumb.left(), thumb.center().y() + 8, thumb.width(), 20),
                             Qt.AlignmentFlag.AlignCenter, label)
        # 動画であることの表示（再生マーク＋長さ）
        if it["kind"] == "video":
            text = format_duration_short(it.get("duration")) or "動画"
            painter.setFont(theme.font(theme.SIZE_CAPTION, QFont.Weight.DemiBold))
            w = painter.fontMetrics().horizontalAdvance(text) + 26
            badge = QRectF(thumb.left() + 6, thumb.bottom() - 24, w, 18)
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QColor(0, 0, 0, 170))
            painter.drawRoundedRect(badge, 4, 4)
            painter.drawPixmap(int(badge.left() + 4), int(badge.top() + 2), theme.pixmap("play", "#FFFFFF", 14))
            painter.setPen(QColor("#FFFFFF"))
            painter.drawText(badge.adjusted(20, 0, -4, 0), Qt.AlignmentFlag.AlignVCenter, text)
        if it.get("members"):  # 重複・類似のグループ
            text = f"{len(it['members'])}件"
            painter.setFont(theme.font(theme.SIZE_CAPTION, QFont.Weight.DemiBold))
            w = painter.fontMetrics().horizontalAdvance(text) + 12
            badge = QRectF(thumb.right() - w - 6, thumb.top() + 6, w, 18)
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QColor(theme.ACCENT))
            painter.drawRoundedRect(badge, 4, 4)
            painter.setPen(QColor("#FFFFFF"))
            painter.drawText(badge, Qt.AlignmentFlag.AlignCenter, text)
        # 文字（長い名前は省略。全文はツールチップと詳細で確認できる）
        x, y, w = rect.left() + 10, thumb.bottom() + 8, rect.width() - 20
        painter.setFont(theme.font(theme.SIZE_BODY, QFont.Weight.DemiBold))
        painter.setPen(QColor(theme.TEXT))
        name = it.get("group_title") or it["name"]
        painter.drawText(QRectF(x, y, w, 20), Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                         painter.fontMetrics().elidedText(name, Qt.TextElideMode.ElideMiddle, int(w)))
        painter.setFont(theme.font(theme.SIZE_CAPTION))
        painter.setPen(QColor(theme.TEXT_SUB))
        sub = f"{format_size(it['size'])} · {KIND_LABEL.get(it['kind'], '')}"
        if it.get("members"):
            sub = it.get("group_sub", sub)
        painter.drawText(QRectF(x, y + 20, w, 18), Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                         painter.fontMetrics().elidedText(sub, Qt.TextElideMode.ElideRight, int(w)))
        # 判断状態（色だけに頼らず、アイコンと文字で示す）
        state = decision_text(it)
        icon_name = {"保護中": "shield", "残す": "check", "保留": "pause", "削除候補": "flag"}.get(state)
        color = {"削除候補": theme.WARN, "保護中": theme.ACCENT, "残す": theme.OK}.get(state, theme.TEXT_SUB)
        cx = x
        if icon_name:
            painter.drawPixmap(int(cx), int(y + 41), theme.pixmap(icon_name, color, 14))
            cx += 18
        painter.setPen(QColor(color))
        painter.drawText(QRectF(cx, y + 38, w - (cx - x), 18), Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                         state if not it.get("members") else "比較して選ぶ")
        painter.restore()


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
# 共有状態（調査結果・判断・サムネイル・自動スキャン）
# =============================================================================
DECISION_ALIASES = {"削除候補にする": "削除候補", "絶対に残す": "保護する"}


class MediaState(QObject):
    """ホーム・ファイル一覧・整理のおすすめで共有する状態。"""

    changed = Signal()
    scan_progress = Signal(object)

    def __init__(self, db: Database, tools_fn) -> None:
        super().__init__()
        self.db = db
        self.tools_fn = tools_fn
        self.loader = ThumbnailLoader(db, tools_fn)
        self.runner = AutoScanRunner(db.data_dir, tools_fn)
        self.ctx = candidates.Context(db)
        self.last_progress: dict = {}
        self.last_result: dict | None = None
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.timeout.connect(self.refresh)
        self.runner.progress.connect(self._on_progress)
        self.runner.finished.connect(self._on_finished)
        self.runner.state_changed.connect(lambda _: self.scan_progress.emit(self.last_progress))

    def refresh(self) -> None:
        self.ctx = candidates.Context(self.db)
        self.changed.emit()

    def close(self) -> None:
        """終了処理。データベースを閉じた後に表示更新が届かないよう、通知を止めてから調査を止める。"""
        self.runner.blockSignals(True)
        self.blockSignals(True)
        self._timer.stop()
        self.runner.shutdown()
        self.loader.pool.waitForDone(5000)

    def start_scan(self) -> bool:
        self.last_result = None
        return self.runner.start()

    def _on_progress(self, info: dict) -> None:
        self.last_progress = info
        self.scan_progress.emit(info)
        if info.get("refresh") and not self._timer.isActive():
            self._timer.start(1500)  # 見つかったものを順次表示（まとめて更新）

    def _on_finished(self, result: dict) -> None:
        self.last_progress = {}
        self.last_result = result
        if result.get("error"):
            QMessageBox.warning(None, "調査", f"調査中にエラーが発生しました: {result['error']}")
        self.refresh()
        self.scan_progress.emit({})

    def decide(self, name: str, ids: list[int]) -> int:
        """判断を記録する。「保護する」が付いたものは削除候補にしない。スキップ件数を返す。"""
        name = DECISION_ALIASES.get(name, name)
        skipped = 0
        for mid in ids:
            if name == "保護する":
                self.db.set_protected(mid, True)
            elif name == "保護を解除":
                self.db.set_protected(mid, False)
            else:
                try:
                    self.db.set_decision(mid, None if name == "判断を解除" else name)
                except PermissionError:
                    skipped += 1
        self.refresh()
        return skipped

    def has_data(self) -> bool:
        return bool(self.ctx.rows)

    def scan_state(self) -> dict:
        """ステータス欄に出す状態。kind: 未調査 / 調査中 / 一時停止中 / 調査完了 / 一部を調査できなかった / 中断"""
        db, info = self.db, self.last_progress
        running = self.runner.is_running()
        skips = db.conn.execute("SELECT COUNT(*) FROM scan_skips WHERE kind='error'").fetchone()[0]
        unfinished = db.conn.execute(
            "SELECT COUNT(*) FROM scan_locations WHERE status IN ('中断','調査中')").fetchone()[0]
        if running and self.runner.ctl and self.runner.ctl.paused:
            return {"kind": "一時停止中", "text": "一時停止中", "icon": "pause"}
        if running:
            if info.get("phase") == "scan":
                return {"kind": "調査中", "icon": "search",
                        "text": f"調査中 · {info.get('location', '')} · 検出 {info.get('found', 0):,} 件・"
                                f"{format_size(info.get('bytes', 0))}",
                        "tooltip": f"調べている場所: {info.get('current', '')}\n全体の件数は調べ終わるまで分かりません"}
            if info.get("text"):
                progress = (info["done"], info["total"]) if info.get("total") else None
                return {"kind": "調査中", "icon": "search", "text": info["text"], "progress": progress}
            return {"kind": "調査中", "icon": "search", "text": "調査を開始しています…"}
        last = db.get_setting("scan_finished")
        if last is None and not self.ctx.rows:
            return {"kind": "未調査", "icon": "info", "text": "まだ調査していません"}
        if (self.last_result or {}).get("cancelled") or unfinished:
            return {"kind": "中断", "icon": "warning",
                    "text": "調査を中断しました。途中までの結果を表示しています"}
        when = candidates.format_date(float(last)) if last else "-"
        if skips:
            return {"kind": "一部を調査できなかった", "icon": "warning",
                    "text": f"調査完了（{when}）· 一部の場所（{skips} か所）を調査できませんでした"}
        return {"kind": "調査完了", "icon": "check", "text": f"調査完了（{when}）"}


# =============================================================================
# 共通部品
# =============================================================================
def card_frame(name: str = "Card") -> QFrame:
    f = QFrame()
    f.setObjectName(name)
    return f


def label(text: str = "", role: str | None = None, wrap: bool = False) -> QLabel:
    lb = QLabel(text)
    if role:
        theme.set_role(lb, role)
    lb.setWordWrap(wrap)
    return lb


def button(text: str, kind: str | None = None, icon_name: str | None = None, tip: str | None = None) -> QPushButton:
    b = QPushButton(text)
    if kind:
        theme.set_kind(b, kind)
    if icon_name:
        color = "#FFFFFF" if kind in ("primary", "danger") else theme.TEXT
        b.setIcon(theme.icon(icon_name, color, 16))
    if tip:
        b.setToolTip(tip)
    return b


class EmptyState(QWidget):
    """データがない・候補がない・検索結果がないときの表示（理由と次の操作）。"""

    def __init__(self) -> None:
        super().__init__()
        v = QVBoxLayout(self)
        v.addStretch()
        self.icon = QLabel()
        self.icon.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.title = label("", "subtitle")
        self.title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.message = label("", "sub", wrap=True)
        self.message.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.action = button("", "primary")
        row = QHBoxLayout()
        row.addStretch()
        row.addWidget(self.action)
        row.addStretch()
        for w in (self.icon, self.title, self.message):
            v.addWidget(w)
        v.addSpacing(theme.S1)
        v.addLayout(row)
        v.addStretch()
        self._callback = None
        self.action.clicked.connect(lambda: self._callback and self._callback())

    def show_state(self, icon_name: str, title: str, message: str, action: str | None = None, callback=None):
        self.icon.setPixmap(theme.pixmap(icon_name, theme.TEXT_SUB, 40))
        self.title.setText(title)
        self.message.setText(message)
        self.action.setVisible(bool(action))
        self.action.setText(action or "")
        self._callback = callback


INFO_HELP = ("「残す」はこのファイルを整理しないという判断の記録です。\n"
             "「保護する」を付けると、整理のおすすめ・削除候補・ごみ箱への移動から常に除外します。\n"
             "「削除候補にする」はファイルを削除しません。ごみ箱へ移すのは「整理のおすすめ」の「削除候補」から行います。")


class SelectionBar(QFrame):
    """ファイルを選んだときだけ表示する操作バー。"""

    def __init__(self, browser) -> None:
        super().__init__()
        self.setObjectName("SelectionBar")
        self.browser = browser
        h = QHBoxLayout(self)
        h.setContentsMargins(theme.S1, 4, theme.S1, 4)
        h.setSpacing(theme.S1)
        self.count = label("", "subtitle")
        h.addWidget(self.count)
        h.addStretch()
        self.keep = button("残す", None, "check", "整理しないという判断を記録します")
        self.hold = button("保留", None, "pause", "あとで判断します。「保留」はファイル一覧で絞り込めます")
        self.delete = button("削除候補にする", None, "flag",
                             "まだ削除しません。ごみ箱へ移すのは「整理のおすすめ」の「削除候補」から行います")
        self.protect = button("保護する", None, "shield", "整理のおすすめ・ごみ箱への移動から常に除外します")
        self.more = QToolButton()
        self.more.setText("その他")
        self.more.setIcon(theme.icon("more", theme.TEXT, 16))
        self.more.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        self.more.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        menu = QMenu(self.more)
        self.act_clear = menu.addAction("判断を解除")
        self.act_unprotect = menu.addAction("保護を解除")
        menu.addSeparator()
        self.act_open = menu.addAction("保存場所を開く")
        self.act_help = menu.addAction("「残す」と「保護する」の違い")
        self.more.setMenu(menu)
        self.close = QToolButton()
        self.close.setIcon(theme.icon("close", theme.TEXT, 16))
        self.close.setToolTip("選択を解除")
        theme.set_kind(self.close, "subtle")
        for w in (self.keep, self.hold, self.delete, self.protect, self.more, self.close):
            h.addWidget(w)
        self.keep.clicked.connect(lambda: browser.apply_decision("残す"))
        self.hold.clicked.connect(lambda: browser.apply_decision("保留"))
        self.delete.clicked.connect(lambda: browser.apply_decision("削除候補"))
        self.protect.clicked.connect(lambda: browser.apply_decision("保護する"))
        self.act_clear.triggered.connect(lambda: browser.apply_decision("判断を解除"))
        self.act_unprotect.triggered.connect(lambda: browser.apply_decision("保護を解除"))
        self.act_open.triggered.connect(browser.open_location)
        self.act_help.triggered.connect(lambda: QMessageBox.information(self, "判断の種類", INFO_HELP))
        self.close.clicked.connect(browser.clear_selection)

    def update_for(self, ids: list[int], ctx: candidates.Context, group: bool = False) -> None:
        total = sum(ctx.rows[i]["size"] for i in ids if i in ctx.rows)
        if group:
            # グループ全体を一度に削除候補にしないよう、比較画面で1件ずつ判断してもらう
            self.count.setText("グループを選択中 · 右の比較で1件ずつ判断してください")
        else:
            self.count.setText(f"{len(ids):,} 件を選択 · {format_size(total)}")
        for b in (self.keep, self.hold, self.delete, self.protect, self.more):
            b.setVisible(not group)
        self.act_open.setEnabled(len(ids) == 1)


# =============================================================================
# 詳細パネル（プレビュー・ファイル情報・理由・判断）
# =============================================================================
class Section(QFrame):
    def __init__(self, title: str) -> None:
        super().__init__()
        self.setObjectName("Card")
        self.v = QVBoxLayout(self)
        self.v.setContentsMargins(theme.S2, theme.S1 + 4, theme.S2, theme.S2)
        self.v.setSpacing(theme.S1)
        self.v.addWidget(label(title, "subtitle"))


class DetailPanel(QFrame):
    closed = Signal()

    def __init__(self, browser) -> None:
        super().__init__()
        from .pages import VideoPlayer
        self.setObjectName("DetailPanel")
        self.browser = browser
        self.media_id: int | None = None
        v = QVBoxLayout(self)
        v.setContentsMargins(0, 0, 0, 0)
        head = QHBoxLayout()
        self.title = label("詳細", "subtitle")
        self.title.setMinimumWidth(1)
        close = QToolButton()
        close.setIcon(theme.icon("close", theme.TEXT, 16))
        close.setToolTip("詳細を閉じる")
        theme.set_kind(close, "subtle")
        close.clicked.connect(self.closed.emit)
        head.addWidget(self.title, 1)
        head.addWidget(close)
        v.addLayout(head)
        self.stack = QStackedWidget()
        v.addWidget(self.stack, 1)
        # 1件
        single = QScrollArea()
        single.setWidgetResizable(True)
        single.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        body = QWidget()
        bv = QVBoxLayout(body)
        bv.setContentsMargins(0, 0, 4, 0)
        bv.setSpacing(theme.S1)
        self.preview_section = Section("プレビュー")
        self.preview_stack = QStackedWidget()
        self.image = QLabel()
        self.image.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.image.setMinimumHeight(160)
        self.preview_msg = EmptyState()
        self.player = VideoPlayer()
        self.player.setMinimumHeight(220)
        for w in (self.image, self.player, self.preview_msg):
            self.preview_stack.addWidget(w)
        self.preview_section.v.addWidget(self.preview_stack)
        self.info_section = Section("ファイル情報")
        self.info_form = QFormLayout()
        self.info_form.setHorizontalSpacing(theme.S2)
        self.info_section.v.addLayout(self.info_form)
        self.reason_section = Section("整理候補にした理由")
        self.reasons = label("", None, wrap=True)
        self.reasons.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.reason_section.v.addWidget(self.reasons)
        self.reason_section.v.addWidget(label("内容の面白さや大切さは判断していません。プレビューで確認してください。",
                                              "caption", wrap=True))
        self.decision_section = Section("あなたの判断")
        self.decision = label("", None, wrap=True)
        self.decision_section.v.addWidget(self.decision)
        self.decision_section.v.addWidget(label("判断は上の操作バーで変更できます。"
                                                "「削除候補にする」だけではファイルは削除されません。", "caption", wrap=True))
        for w in (self.preview_section, self.info_section, self.reason_section, self.decision_section):
            bv.addWidget(w)
        bv.addStretch()
        single.setWidget(body)
        self.stack.addWidget(single)
        # 複数
        multi = QWidget()
        mv = QVBoxLayout(multi)
        self.multi_section = Section("選択したファイル")
        self.multi_text = label("", None, wrap=True)
        self.multi_section.v.addWidget(self.multi_text)
        row = QHBoxLayout()
        for name, text, ic in (("残す", "残す", "check"), ("保留", "保留", "pause"), ("削除候補", "削除候補にする", "flag")):
            b = button(text, None, ic)
            b.clicked.connect(partial(browser.apply_decision, name))
            row.addWidget(b)
        self.multi_section.v.addLayout(row)
        self.multi_section.v.addWidget(label("まとめて判断を記録します。ファイルは削除されません。", "caption", wrap=True))
        mv.addWidget(self.multi_section)
        mv.addStretch()
        self.stack.addWidget(multi)
        # 重複・類似の比較（画面が低いときは縦にスクロール）
        self.compare = ComparePanel(browser)
        compare_scroll = QScrollArea()
        compare_scroll.setWidgetResizable(True)
        compare_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        compare_scroll.setWidget(self.compare)
        self.compare_scroll = compare_scroll
        self.stack.addWidget(compare_scroll)

    def show_single(self, media_id: int, plan_reasons: list[str] | None = None) -> None:
        db = self.browser.state.db
        r = db.video(media_id)
        if r is None:
            return
        self.stack.setCurrentIndex(0)
        if media_id != self.media_id:
            db.mark_viewed(media_id)  # 「見返していない順」のため、このアプリで確認した日時を記録
            self.media_id = media_id
            self._load_preview(r)
            r = db.video(media_id)
        self.title.setText(r["name"])
        self.title.setToolTip(r["path"])
        ctx = self.browser.state.ctx
        meta = loads(r["meta"], {}) or {}
        while self.info_form.rowCount():
            self.info_form.removeRow(0)

        def add(k, val):
            value = label(val, None, wrap=True)
            value.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
            self.info_form.addRow(label(k, "sub"), value)
        add("名前", r["name"])
        add("保存場所", r["folder"])
        add("容量", f"{format_size(r['size'])}（{format_bytes_exact(r['size'])}）")
        add("種類", KIND_LABEL.get(r["kind"], "") + (f" · {meta['width']}×{meta['height']}" if meta.get("width") else ""))
        add("撮影日時" if r["kind"] == "image" else "録画日時", candidates.format_date(r["taken_at"]))
        add("更新日時", candidates.format_date(r["mtime"]))
        if r["kind"] == "video":
            d, pm = candidates.duration(r), candidates.per_minute(r)
            add("再生時間", format_duration_short(d) or "不明")
            add("1分あたり", format_size(int(pm)) if pm else "不明")
        started = db.get_setting("view_history_started")
        add("最終確認", candidates.format_date(r["last_viewed"])
            + (f"（{candidates.format_date(float(started))} より前の記録はありません）" if started else ""))
        reasons = list(plan_reasons) if plan_reasons else ctx.reasons(r)
        notes = []
        if r["cloud_only"]:
            notes.append("オンライン専用ファイルです。自動ダウンロードしないため、内容の解析・プレビュー・ごみ箱への移動は行いません。")
        if r["light_note"]:
            notes.append(r["light_note"])
        self.reasons.setText("\n".join(f"・{x}" for x in reasons + notes) if (reasons or notes)
                             else "整理候補になる理由は見つかりませんでした。")
        state = "保護中" if r["protected"] else (r["decision"] or "未判断")
        explain = {
            "保護中": "整理のおすすめ・削除候補・ごみ箱への移動から常に除外します。",
            "残す": "整理しないと判断しました（保護とは違い、目標容量の提案などからは除外のみ）。",
            "保留": "あとで判断します。ファイル一覧の「保留」で確認できます。",
            "削除候補": "ごみ箱へ移す候補です。まだ削除していません。「整理のおすすめ」→「削除候補」から確認して移動できます。",
            "未判断": "まだ判断していません。",
        }[state]
        extra = f"（判断: {r['decision']}）" if r["protected"] and r["decision"] else ""
        self.decision.setText(f"<b>{state}</b>{extra}<br>{explain}")

    def _load_preview(self, r) -> None:
        self.player.stop()
        if r["cloud_only"]:
            self.preview_stack.setCurrentWidget(self.preview_msg)
            self.preview_msg.show_state("info", "プレビューしません", "オンライン専用ファイルのため、自動でダウンロードしません。")
            return
        if r["kind"] == "video":
            self.preview_stack.setCurrentWidget(self.player)
            self.player.set_source(r["path"])
            return
        reader = QImageReader(r["path"])
        reader.setAutoTransform(True)
        size = reader.size()
        if size.isValid() and (size.width() > 1600 or size.height() > 1600):
            reader.setScaledSize(size.scaled(1600, 1600, Qt.AspectRatioMode.KeepAspectRatio))
        img = reader.read()
        if img.isNull():
            self.preview_stack.setCurrentWidget(self.preview_msg)
            self.preview_msg.show_state("warning", "プレビューできません",
                                        f"{reader.errorString()}\n形式が未対応か、ファイルが読み込めない可能性があります。",
                                        "保存場所を開く", self.browser.open_location)
            return
        self.preview_stack.setCurrentWidget(self.image)
        pm = QPixmap.fromImage(img)
        w = max(self.image.width() - 8, 240)
        h = 300 if self.window().height() > 700 else 200
        if pm.width() > w or pm.height() > h:  # 縮小のみ（拡大しない）
            pm = pm.scaled(w, h, Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation)
        self.image.setPixmap(pm)

    def show_multi(self, ids: list[int]) -> None:
        self.player.stop()
        self.stack.setCurrentIndex(1)
        ctx = self.browser.state.ctx
        rows = [ctx.rows[i] for i in ids if i in ctx.rows]
        images = sum(1 for r in rows if r["kind"] == "image")
        self.title.setText(f"{len(rows):,} 件を選択")
        self.multi_text.setText(f"合計 {format_size(sum(r['size'] for r in rows))}<br>"
                                f"画像 {images:,} 件・動画 {len(rows) - images:,} 件<br>"
                                f"保護中 {sum(1 for r in rows if r['protected']):,} 件"
                                "（保護中のファイルは削除候補にしません）")
        self.media_id = None

    def show_group(self, category: str, key) -> None:
        self.player.stop()
        self.stack.setCurrentWidget(self.compare_scroll)
        self.title.setText("完全重複の比較" if category == "重複" else "類似画像の比較")
        self.compare.show_group(category, key)
        self.media_id = None


# =============================================================================
# ファイル一覧 / 整理のおすすめ（共通の一覧画面）
# =============================================================================
RECOMMEND_TABS = [("重複", "完全重複"), ("類似画像", "類似画像"), ("録画確認候補", "録画確認候補"),
                  ("大容量", "大容量"), ("削除候補", "削除候補")]
RECOMMEND_HELP = {
    "重複": "内容が完全に一致するファイルです（SHA-256 で確認）。1つは残す前提で比較してください。",
    "類似画像": "縮小版・連写・軽い編集違いなど、似ている画像です（完全一致ではありません）。",
    "録画確認候補": "極端に短い・ほぼ真っ暗・読み込めない・破損の可能性があるファイルです。プレビューで確認してください。",
    "大容量": "100MB 以上のファイルです。容量が大きいだけで整理候補とは判断しません。",
    "削除候補": "「削除候補にする」を付けたファイルです。確認してからごみ箱へ移動できます（完全削除はしません）。",
}
EMPTY_HELP = {
    "重複": "内容が完全に一致するファイルは見つかりませんでした。",
    "類似画像": "似ている画像のグループは見つかりませんでした。",
    "録画確認候補": "失敗した可能性のある録画・画像は見つかりませんでした。",
    "大容量": "100MB 以上のファイルは見つかりませんでした。",
    "削除候補": "まだ削除候補はありません。一覧でファイルを選び「削除候補にする」を押すと、ここに集まります。",
}
STATE_FILTERS = ["すべての判断", "未判断", "残す", "保留", "削除候補", "保護中"]


class MediaBrowser(QWidget):
    """mode="files"（ファイル一覧）または "recommend"（整理のおすすめ）。"""

    request_locations = Signal()
    request_files = Signal()

    def __init__(self, state: MediaState, mode: str, parent=None) -> None:
        super().__init__(parent)
        self.setObjectName("Page")
        self.state = state
        self.mode = mode
        self.category = "重複" if mode == "recommend" else "すべて"
        v = QVBoxLayout(self)
        v.setContentsMargins(theme.S3, theme.S2, theme.S3, theme.S1)
        v.setSpacing(theme.S1)
        head = QHBoxLayout()
        title_box = QVBoxLayout()
        title_box.setSpacing(2)
        self.title = label("ファイル一覧" if mode == "files" else "その他の整理（画像など）", "title")
        self.help = label("", "sub", wrap=True)
        title_box.addWidget(self.title)
        title_box.addWidget(self.help)
        head.addLayout(title_box, 1)
        self.target_button = button("目標容量から提案", None, "target",
                                    "「20GB空けたい」などの目標から、整理候補の組み合わせを提案します")
        self.trash_button = button("ごみ箱へ移動…", "danger", "trash",
                                   "削除候補を確認してから Windows のごみ箱へ移動します（完全削除はしません）")
        head.addWidget(self.target_button, 0, Qt.AlignmentFlag.AlignTop)
        head.addWidget(self.trash_button, 0, Qt.AlignmentFlag.AlignTop)
        v.addLayout(head)
        self.tabs = QTabBar()
        self.tabs.setExpanding(False)
        self.tabs.setDrawBase(False)
        if mode == "recommend":
            for key, title in RECOMMEND_TABS:
                self.tabs.addTab(title)
            v.addWidget(self.tabs)
        else:
            self.tabs.hide()
        # 検索・絞り込み・並べ替え・表示切り替え
        tools = QHBoxLayout()
        tools.setSpacing(theme.S1)
        self.search = QLineEdit()
        self.search.setPlaceholderText("ファイル名・保存場所で検索")
        self.search.setClearButtonEnabled(True)
        self.kind_filter = QComboBox()
        self.kind_filter.addItems(["画像と動画", "画像のみ", "動画のみ"])
        self.state_filter = QComboBox()
        self.state_filter.addItems(STATE_FILTERS)
        self.state_filter.setVisible(mode == "files")
        self.sort_combo = QComboBox()
        self.sort_combo.addItems(SORTS)
        self.grid_button = QToolButton()
        self.grid_button.setIcon(theme.icon("grid", theme.TEXT, 16))
        self.grid_button.setToolTip("カード表示")
        self.list_button = QToolButton()
        self.list_button.setIcon(theme.icon("list", theme.TEXT, 16))
        self.list_button.setToolTip("詳細なリスト表示")
        for b in (self.grid_button, self.list_button):
            b.setCheckable(True)
            b.setAutoExclusive(True)
        self.grid_button.setChecked(True)
        tools.addWidget(self.search, 3)
        tools.addWidget(self.kind_filter, 1)
        tools.addWidget(self.state_filter, 1)
        tools.addWidget(self.sort_combo, 1)
        tools.addWidget(self.grid_button)
        tools.addWidget(self.list_button)
        v.addLayout(tools)
        self.selection_bar = SelectionBar(self)
        self.selection_bar.hide()
        v.addWidget(self.selection_bar)
        # 一覧（カード / リスト）と詳細パネル
        self.model = MediaModel(state.loader, self)
        self.view = QListView()
        self.view.setObjectName("Grid")
        self.view.setViewMode(QListView.ViewMode.IconMode)
        self.view.setResizeMode(QListView.ResizeMode.Adjust)
        self.view.setMovement(QListView.Movement.Static)
        self.view.setUniformItemSizes(True)
        self.view.setGridSize(QSize(CARD_W, CARD_H))
        self.view.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.view.setMouseTracking(True)
        self.view.setModel(self.model)
        self.view.setItemDelegate(CardDelegate(state.loader, self.view))
        self.table = QTableView()
        self.table.setModel(self.model)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setAlternatingRowColors(True)
        self.table.setShowGrid(False)
        self.table.setWordWrap(False)
        self.table.verticalHeader().setVisible(False)
        self.table.verticalHeader().setDefaultSectionSize(30)
        self.table.horizontalHeader().setStretchLastSection(True)
        self.table.setTextElideMode(Qt.TextElideMode.ElideMiddle)
        for i, w in enumerate((260, 70, 90, 130, 130, 80)):
            self.table.setColumnWidth(i, w)
        self.empty = EmptyState()
        self.content = QStackedWidget()
        for w in (self.view, self.table, self.empty):
            self.content.addWidget(w)
        self.detail = DetailPanel(self)
        self.detail.setMinimumWidth(300)
        self.detail.hide()
        self.splitter = QSplitter()
        self.splitter.setChildrenCollapsible(False)
        self.splitter.addWidget(self.content)
        self.splitter.addWidget(self.detail)
        self.splitter.setStretchFactor(0, 3)
        self.splitter.setStretchFactor(1, 2)
        v.addWidget(self.splitter, 1)
        self.footer = label("", "caption")
        v.addWidget(self.footer)

        self.tabs.currentChanged.connect(self._on_tab)
        self.search.textChanged.connect(self.refresh)
        self.kind_filter.currentIndexChanged.connect(self.refresh)
        self.state_filter.currentIndexChanged.connect(self.refresh)
        self.sort_combo.currentIndexChanged.connect(self.refresh)
        self.grid_button.toggled.connect(self._switch_view)
        self.view.selectionModel().selectionChanged.connect(self.on_select)
        self.table.selectionModel().selectionChanged.connect(self.on_select)
        self.detail.closed.connect(self.clear_selection)
        self.target_button.clicked.connect(lambda: TargetDialog(self).exec())
        self.trash_button.clicked.connect(self.open_trash_dialog)
        state.changed.connect(self.refresh)
        state.scan_progress.connect(lambda _: self._update_empty_if_needed())
        self.refresh()

    # ComparePanel・TargetDialog から使う
    @property
    def ctx(self) -> candidates.Context:
        return self.state.ctx

    @property
    def loader(self) -> ThumbnailLoader:
        return self.state.loader

    @property
    def db(self) -> Database:
        return self.state.db

    def active_view(self):
        return self.view if self.grid_button.isChecked() else self.table

    # --- 表示 ---------------------------------------------------------------
    def set_category(self, category: str) -> None:
        if self.mode == "recommend":
            keys = [k for k, _ in RECOMMEND_TABS]
            if category in keys:
                self.tabs.setCurrentIndex(keys.index(category))
                self.category = category
        else:
            self.category = "すべて"
            mapping = {"保護": "保護中", "保留": "保留", "削除候補": "削除候補", "すべて": "すべての判断"}
            self.state_filter.setCurrentText(mapping.get(category, "すべての判断"))
        self.refresh()

    def _on_tab(self, index: int) -> None:
        if 0 <= index < len(RECOMMEND_TABS):
            self.category = RECOMMEND_TABS[index][0]
            self.clear_selection()
            self.refresh()

    def _switch_view(self, grid: bool) -> None:
        source = self.table if grid else self.view  # 切り替え前の表示から選択を引き継ぐ
        rows = sorted({i.row() for i in source.selectionModel().selectedIndexes()})
        target = self.view if grid else self.table
        self._show_content()
        sm = target.selectionModel()
        sm.clearSelection()
        for r in rows:
            sm.select(self.model.index(r, 0), QItemSelectionModel.SelectionFlag.Select
                      | QItemSelectionModel.SelectionFlag.Rows)

    def _filters_active(self) -> bool:
        return bool(self.search.text().strip()) or self.kind_filter.currentIndex() > 0 or \
            (self.mode == "files" and self.state_filter.currentIndex() > 0)

    def refresh(self) -> None:
        ctx = self.state.ctx
        selected = set(self.selected_ids())
        if self.mode == "recommend":
            for i, (key, title) in enumerate(RECOMMEND_TABS):
                n = len(ctx.dup_groups if key == "重複" else ctx.sim_groups) if key in ("重複", "類似画像") \
                    else len(candidates.category_ids(ctx, key))
                self.tabs.setTabText(i, f"{title}（{n:,}）")
            self.help.setText(RECOMMEND_HELP.get(self.category, ""))
            ids = candidates.category_ids(ctx, self.category)
        else:
            self.help.setText("調査で見つかった画像と動画です。選ぶと詳細と操作が表示されます。")
            ids = candidates.category_ids(ctx, "すべて")
        rows = [row_dict(ctx.rows[i], ctx) for i in ids]
        text = self.search.text().strip().casefold()
        if text:
            rows = [r for r in rows if text in r["name"].casefold() or text in r["folder"].casefold()]
        kind = {1: "image", 2: "video"}.get(self.kind_filter.currentIndex())
        if kind:
            rows = [r for r in rows if r["kind"] == kind]
        if self.mode == "files" and self.state_filter.currentIndex() > 0:
            want = self.state_filter.currentText()
            rows = [r for r in rows if decision_text(r) == want or (want == "未判断" and decision_text(r) == "未判断")]
        rows = sort_rows(rows, self.sort_combo.currentText())
        if self.mode == "recommend" and self.category in ("重複", "類似画像"):
            rows = self._group_items(rows)
        self.model.set_items(rows)
        trash_rows = [r for r in ctx.rows.values() if r["decision"] == "削除候補"]
        is_trash_tab = self.mode == "recommend" and self.category == "削除候補"
        self.trash_button.setVisible(is_trash_tab)
        self.trash_button.setEnabled(bool(trash_rows))
        self.target_button.setVisible(self.mode == "recommend")
        count = len({m for r in rows for m in (r.get("members") or [r["id"]])})
        total = sum(r["size"] for r in rows)
        self.footer.setText(f"{count:,} 件 · {format_size(total)}"
                            + (f" · 削除候補 {len(trash_rows):,} 件（{format_size(sum(r['size'] for r in trash_rows))}）"
                               if trash_rows else ""))
        self._show_content()
        view = self.active_view()
        for i, it in enumerate(self.model.items):
            if it["id"] in selected or (it.get("members") and set(it["members"]) & selected):
                view.selectionModel().select(self.model.index(i, 0), QItemSelectionModel.SelectionFlag.Select
                                             | QItemSelectionModel.SelectionFlag.Rows)
        self.on_select()

    def _show_content(self) -> None:
        if self.model.rowCount():
            self.content.setCurrentWidget(self.view if self.grid_button.isChecked() else self.table)
            return
        self.content.setCurrentWidget(self.empty)
        self._update_empty()

    def _update_empty_if_needed(self) -> None:
        if self.content.currentWidget() is self.empty:
            self._update_empty()

    def _update_empty(self) -> None:
        st = self.state.scan_state()
        if not self.state.has_data():
            if st["kind"] in ("調査中", "一時停止中"):
                self.empty.show_state("search", "調査中です", "見つかったファイルから順に表示します。しばらくお待ちください。")
            else:
                self.empty.show_state("folder", "まだ調査していません",
                                      "調べる場所を選ぶと、PC内の画像と動画を探して整理候補を表示します。",
                                      "調べる場所を選ぶ", self.request_locations.emit)
        elif self._filters_active():
            self.empty.show_state("search", "条件に一致するファイルはありません",
                                  "検索語や絞り込みを変えてみてください。", "条件をクリア", self.clear_filters)
        elif self.mode == "recommend":
            note = EMPTY_HELP.get(self.category, "")
            if st["kind"] in ("調査中", "一時停止中"):
                note += "\n調査中のため、あとで見つかる可能性があります。"
            self.empty.show_state("check", "候補はありません", note, "ファイル一覧を見る", self.request_files.emit)
        else:
            self.empty.show_state("info", "表示するファイルはありません", "")

    def clear_filters(self) -> None:
        self.search.clear()
        self.kind_filter.setCurrentIndex(0)
        self.state_filter.setCurrentIndex(0)

    def _group_items(self, rows: list[dict]) -> list[dict]:
        ctx = self.state.ctx
        dup = self.category == "重複"
        groups, keeps, of = (ctx.dup_groups, ctx.dup_keep, ctx.dup_of) if dup else \
            (ctx.sim_groups, ctx.sim_keep, ctx.sim_of)
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
            rep.update(group=(self.category, key), members=members, size=extra,
                       group_title=f"{rep['name']} ほか{len(members) - 1}件",
                       group_sub=("重複分 " if dup else "おすすめ以外 ") + format_size(extra))
            items.append(rep)
        return items

    # --- 選択 ---------------------------------------------------------------
    def selected_rows(self) -> list[int]:
        return sorted({i.row() for i in self.active_view().selectionModel().selectedIndexes()})

    def selected_ids(self) -> list[int]:
        out = []
        for r in self.selected_rows():
            if r < len(self.model.items):
                it = self.model.items[r]
                out.extend(it.get("members") or [it["id"]])
        return out

    def clear_selection(self) -> None:
        self.view.selectionModel().clearSelection()
        self.table.selectionModel().clearSelection()

    def on_select(self, *_) -> None:
        rows = self.selected_rows()
        ids = self.selected_ids()
        if not rows:
            self.selection_bar.hide()
            self.detail.hide()  # 未選択のときは一覧に表示領域を使う
            self.detail.player.stop()
            return
        self.selection_bar.update_for(ids, self.state.ctx, group=any(
            self.model.items[r].get("group") for r in rows if r < len(self.model.items)))
        self.selection_bar.show()
        if not self.detail.isVisible():
            self.detail.show()
            total = self.splitter.width()
            panel = max(320, min(480, int(total * (0.5 if total < 1000 else 0.36))))
            self.splitter.setSizes([total - panel, panel])
        it = self.model.items[rows[0]]
        if len(rows) == 1 and it.get("group"):
            self.detail.show_group(*it["group"])
        elif len(rows) == 1:
            self.detail.show_single(it["id"])
        else:
            self.detail.show_multi(ids)

    # --- 操作 ---------------------------------------------------------------
    def apply_decision(self, name: str, ids: list[int] | None = None) -> None:
        ids = ids if ids is not None else self.selected_ids()
        skipped = self.state.decide(name, ids)
        if skipped:
            QMessageBox.information(self, "判断", f"保護中の {skipped} 件は削除候補にしませんでした。")

    def open_location(self) -> None:
        ids = self.selected_ids()
        if len(ids) == 1 and os.name == "nt":
            subprocess.Popen(["explorer", "/select,", self.state.db.video(ids[0])["path"]])

    def open_trash_dialog(self) -> None:
        ids = [r["id"] for r in self.state.ctx.rows.values() if r["decision"] == "削除候補"]
        if ids:
            dlg = TrashDialog(self.state.db, ids, self)
            if dlg.exec() == QDialog.DialogCode.Accepted:
                self.state.refresh()

    def stop_media(self) -> None:
        self.detail.player.stop()

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self.help.setVisible(self.height() > 600)
        self.title.setToolTip(self.help.text())


# =============================================================================
# ホーム
# =============================================================================
def system_drive_usage() -> tuple[str, int, int] | None:
    root = (os.environ.get("SystemDrive", "C:") + "\\") if os.name == "nt" else "/"
    try:
        u = shutil.disk_usage(root)
        return root, u.used, u.total
    except OSError:
        return None


class StatCard(QFrame):
    def __init__(self, title: str, icon_name: str) -> None:
        super().__init__()
        self.setObjectName("Card")
        v = QVBoxLayout(self)
        v.setContentsMargins(theme.S2, theme.S2, theme.S2, theme.S2)
        v.setSpacing(4)
        head = QHBoxLayout()
        ic = QLabel()
        ic.setPixmap(theme.pixmap(icon_name, theme.ACCENT, 18))
        head.addWidget(ic)
        head.addWidget(label(title, "sub"), 1)
        v.addLayout(head)
        self.value = label("", "display")
        self.detail = label("", "caption", wrap=True)
        v.addWidget(self.value)
        v.addWidget(self.detail)
        v.addStretch()
        self.body = v

    def set(self, value: str, detail: str = "") -> None:
        self.value.setText(value)
        self.detail.setText(detail)


class HomePage(QWidget):
    """空き容量・調査済み録画の合計・目標容量の入力を中心にしたホーム。"""

    start_plan = Signal(float)
    request_locations = Signal()
    request_files = Signal()
    request_settings = Signal()

    def __init__(self, state: MediaState, parent=None) -> None:
        super().__init__(parent)
        self.setObjectName("Page")
        self.state = state
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        outer.addWidget(scroll)
        body = QWidget()
        scroll.setWidget(body)
        v = QVBoxLayout(body)
        v.setContentsMargins(theme.S3, theme.S2, theme.S3, theme.S3)
        v.setSpacing(theme.S2)
        v.addWidget(label("ホーム", "title"))
        self.lead = label("", "sub", wrap=True)
        v.addWidget(self.lead)
        self.banner = FFmpegBanner(state.tools_fn)
        self.banner.open_settings.connect(self.request_settings.emit)
        v.addWidget(self.banner)
        grid = QGridLayout()
        grid.setSpacing(theme.S2)
        self.free = StatCard("空き容量（実際）", "drive")
        self.total = StatCard("調査済みの録画", "film")
        # 目標容量の入力
        self.goal = card_frame()
        gv = QVBoxLayout(self.goal)
        gv.setContentsMargins(theme.S2, theme.S2, theme.S2, theme.S2)
        gh = QHBoxLayout()
        ic = QLabel()
        ic.setPixmap(theme.pixmap("target", theme.ACCENT, 18))
        gh.addWidget(ic)
        gh.addWidget(label("どれくらい空けたいですか", "sub"), 1)
        gv.addLayout(gh)
        row = QHBoxLayout()
        self.target = QDoubleSpinBox()
        self.target.setRange(0.1, 100000)
        self.target.setDecimals(1)
        self.target.setValue(10)
        self.target.setSuffix(" GB")
        self.target.setMinimumWidth(110)
        self.primary = button("", "primary", "sparkle")
        self.primary.setMinimumHeight(34)
        row.addWidget(self.target)
        row.addWidget(self.primary, 1)
        gv.addLayout(row)
        self.goal_note = label("", "caption", wrap=True)
        gv.addWidget(self.goal_note)
        gv.addStretch()
        self.saving = StatCard("完全一致の重複（同じ録画が残ります）", "copy")
        self.images = StatCard("画像（補助機能）", "image")
        grid.addWidget(self.free, 0, 0)
        grid.addWidget(self.total, 0, 1)
        grid.addWidget(self.goal, 0, 2)
        grid.addWidget(self.saving, 1, 0)
        grid.addWidget(self.images, 1, 1)
        for c in range(3):
            grid.setColumnStretch(c, 1)
        v.addLayout(grid)
        links = QHBoxLayout()
        self.locations_button = button("調べる場所を選ぶ…", None, "folder")
        self.secondary = button("ファイル一覧を見る", None, "files")
        links.addWidget(self.locations_button)
        links.addWidget(self.secondary)
        links.addStretch()
        v.addLayout(links)
        v.addStretch()
        self.primary.clicked.connect(self._primary)
        self.locations_button.clicked.connect(self.request_locations.emit)
        self.secondary.clicked.connect(self.request_files.emit)
        state.changed.connect(self.refresh)
        state.scan_progress.connect(lambda _: self.refresh_status())
        self.refresh()

    def _primary(self) -> None:
        if self.state.has_data():
            self.start_plan.emit(self.target.value())
        else:
            self.request_locations.emit()

    def refresh_status(self) -> None:
        self.banner.update_state()
        st = self.state.scan_state()
        has = self.state.has_data()
        scanning = st["kind"] in ("調査中", "一時停止中")
        self.primary.setText("整理候補を見る" if has else ("調査中…" if scanning else "調べる場所を選ぶ"))
        self.primary.setIcon(theme.icon("sparkle" if has else "folder", "#FFFFFF", 16))
        self.primary.setEnabled(has or not scanning)
        self.goal_note.setText("重複を最優先に、目標に合わせた整理候補を理由付きで提案します。"
                               if has else "まず録画が保存されているフォルダを調査してください。")
        if has:
            self.lead.setText("空き容量と録画の合計を確認し、空けたい容量を入力して整理候補を見てみましょう。")
        elif scanning:
            self.lead.setText("調査中です。見つかった録画から順に集計します。")
            self.total.set("調査中")
            self.saving.set("算出中")
        else:
            self.lead.setText("録画で容量がいっぱいのときに、どれを整理すればよいかを理由付きで提案します。"
                              "まず調べる場所を選んでください。")

    def refresh(self) -> None:
        ctx = self.state.ctx
        videos = [r for r in ctx.rows.values() if r["kind"] == "video"]
        images = [r for r in ctx.rows.values() if r["kind"] == "image"]
        scanning = self.state.runner.is_running()
        paths = [r[0] for r in self.state.db.conn.execute("SELECT path FROM scan_locations")]
        spaces = candidates.drive_space(paths or [system_drive_usage()[0] if system_drive_usage() else "/"])
        if spaces:
            d = spaces[0]
            more = "".join(f"／{x['root']} 残り {format_size(x['free'])}" for x in spaces[1:])
            self.free.set(format_size(d["free"]), f"{d['root']} の空き（全体 {format_size(d['total'])}）{more}。"
                                                   "ドライブの実際の値です。")
        else:
            self.free.set("取得できません")
        if not ctx.rows:
            self.total.set("未調査", "調査すると、録画の本数と合計サイズを表示します。")
            self.saving.set("算出できません", "調査結果がないため算出できません。")
            self.images.set("未調査")
        else:
            self.total.set(format_size(sum(r["size"] for r in videos)),
                           f"{len(videos):,} 本（ファイルサイズの合計。ドライブ全体の使用量とは別です）"
                           + ("。調査中のため増える可能性があります" if scanning else ""))
            groups = [ids for ids in ctx.dup_groups.values() if ctx.rows[ids[0]]["kind"] == "video"]
            extra = sum(ctx.rows[i]["size"] for h, ids in ctx.dup_groups.items()
                        if ctx.rows[ids[0]]["kind"] == "video" for i in ids if i != ctx.dup_keep[h][0])
            if scanning:
                self.saving.set("算出中", "調査が終わると、重複分を表示します。")
            else:
                self.saving.set(format_size(extra), f"{len(groups):,} グループ。各グループで1本を残した場合の重複分です"
                                                    "（ごみ箱を空にした後に空きます）。")
            self.images.set(f"{len(images):,} 件", f"{format_size(sum(r['size'] for r in images))}。"
                                                   "画像の整理は「その他の整理」で行えます。")
        self.refresh_status()


# =============================================================================
# ステータス欄（調査の状態）
# =============================================================================
class ScanStatus(QWidget):
    def __init__(self, state: MediaState, parent=None) -> None:
        super().__init__(parent)
        self.state = state
        h = QHBoxLayout(self)
        h.setContentsMargins(theme.S1, 2, theme.S1, 2)
        h.setSpacing(theme.S1)
        self.icon = QLabel()
        self.text = QLabel()
        self.text.setMinimumWidth(80)
        self.bar = QProgressBar()
        self.bar.setFixedWidth(140)
        self.bar.setTextVisible(False)
        self.pause = button("一時停止", "subtle", "pause")
        self.cancel = button("キャンセル", "subtle", "close")
        self.details = button("詳細", "link", None, "調べた場所・調査できなかった場所・除外範囲を表示します")
        self.summary = button("容量の内訳", "link", None, "フォルダ別・拡張子別の容量を表示します")
        self.rescan = button("再スキャン", None, "refresh", "調査をやり直します（変更のないファイルは解析をやり直しません）")
        for w in (self.icon, self.text):
            h.addWidget(w)
        h.addWidget(self.bar)
        h.addStretch()
        for w in (self.pause, self.cancel, self.details, self.summary, self.rescan):
            h.addWidget(w)
        self.pause.clicked.connect(self.toggle_pause)
        self.cancel.clicked.connect(state.runner.cancel)
        self.details.clicked.connect(lambda: ScopeDialog(state.db, self.window()).exec())
        self.summary.clicked.connect(lambda: SummaryDialog(state.db, self.window()).exec())
        state.scan_progress.connect(lambda _: self.update_state())
        state.changed.connect(self.update_state)
        self.update_state()

    def toggle_pause(self) -> None:
        r = self.state.runner
        if not r.is_running():
            return
        if r.ctl.paused:
            r.resume()
        else:
            r.pause()
        self.update_state()

    def update_state(self) -> None:
        st = self.state.scan_state()
        running = self.state.runner.is_running()
        color = {"一部を調査できなかった": theme.WARN, "中断": theme.WARN, "調査完了": theme.OK}.get(st["kind"], theme.TEXT_SUB)
        self.icon.setPixmap(theme.pixmap(st["icon"], color, 16))
        fm = self.text.fontMetrics()
        self.text.setText(fm.elidedText(st["text"], Qt.TextElideMode.ElideMiddle, 520))
        self.text.setToolTip(st.get("tooltip", st["text"]))
        # 一時停止・キャンセルは調査中だけ表示する
        self.pause.setVisible(running)
        self.cancel.setVisible(running)
        paused = running and self.state.runner.ctl and self.state.runner.ctl.paused
        self.pause.setText("再開" if paused else "一時停止")
        self.pause.setIcon(theme.icon("play" if paused else "pause", theme.TEXT, 16))
        self.rescan.setEnabled(not running)
        self.bar.setVisible(running and not paused)
        progress = st.get("progress")
        if progress:  # 総量が分かっているときだけ割合を出す
            self.bar.setRange(0, 1000)
            self.bar.setValue(int(1000 * min(progress[0] / max(progress[1], 1), 1)))
        else:
            self.bar.setRange(0, 0)


# =============================================================================
# 調べる場所を選ぶ
# =============================================================================
class LocationsDialog(QDialog):
    def __init__(self, db: Database, parent=None) -> None:
        super().__init__(parent)
        self.db = db
        self.setWindowTitle("調べる場所を選ぶ")
        self.resize(640, 460)
        v = QVBoxLayout(self)
        v.setSpacing(theme.S1)
        v.addWidget(label("調べる場所", "title"))
        v.addWidget(label("PC内の画像と動画を自動で探します。チェックを外した場所は調べません。"
                          "ファイルの変更・削除は行いません。", "sub", wrap=True))
        self.list = QListWidget()
        v.addWidget(self.list, 1)
        row = QHBoxLayout()
        add = button("フォルダやドライブを追加…", None, "folder")
        row.addWidget(add)
        row.addStretch()
        v.addLayout(row)
        self.note = label("", "caption", wrap=True)
        v.addWidget(self.note)
        bb = QDialogButtonBox()
        self.ok = bb.addButton("この場所を調べる", QDialogButtonBox.ButtonRole.AcceptRole)
        theme.set_kind(self.ok, "primary")
        bb.addButton("キャンセル", QDialogButtonBox.ButtonRole.RejectRole)
        bb.accepted.connect(self.save)
        bb.rejected.connect(self.reject)
        v.addWidget(bb)
        add.clicked.connect(self.add_location)
        self.reload()

    def reload(self) -> None:
        self.list.clear()
        disabled = set(self.db.json_list("disabled_locations"))
        locations, not_scanned = autoscan.build_locations(self.db, include_disabled=True)
        for loc in locations:
            item = QListWidgetItem(f"{loc.label}　{loc.path}")
            item.setData(Qt.ItemDataRole.UserRole, loc.path)
            item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            item.setCheckState(Qt.CheckState.Unchecked if loc.path in disabled else Qt.CheckState.Checked)
            item.setIcon(theme.icon("drive" if loc.kind == "drive" else "folder", theme.TEXT_SUB, 16))
            self.list.addItem(item)
        others = [f"{d['root']}（{autoscan.DRIVE_KIND_LABEL[d['kind']]}）" for d in not_scanned]
        self.note.setText(("自動では調べないドライブ: " + "、".join(others) + "。必要なら「追加」で指定できます。")
                          if others else "外付け・ネットワークドライブは自動では調べません。必要なら「追加」で指定できます。")

    def add_location(self) -> None:
        path = QFileDialog.getExistingDirectory(self, "調べるフォルダ・ドライブを選択")
        if path:
            values = self.db.json_list("extra_locations")
            if path not in values:
                self.db.set_json_list("extra_locations", values + [path])
            self.reload()

    def save(self) -> None:
        disabled = [self.list.item(i).data(Qt.ItemDataRole.UserRole) for i in range(self.list.count())
                    if self.list.item(i).checkState() != Qt.CheckState.Checked]
        self.db.set_json_list("disabled_locations", disabled)
        self.accept()


# =============================================================================
# 整理候補（目標容量に合わせたルールによる提案）
# =============================================================================
class FFmpegBanner(QFrame):
    """FFmpeg が見つからないときの案内。"""

    open_settings = Signal()

    def __init__(self, tools_fn) -> None:
        super().__init__()
        self.setObjectName("SelectionBar")
        self.tools_fn = tools_fn
        h = QHBoxLayout(self)
        h.setContentsMargins(theme.S1, 4, theme.S1, 4)
        ic = QLabel()
        ic.setPixmap(theme.pixmap("warning", theme.WARN, 16))
        h.addWidget(ic)
        h.addWidget(label("FFmpeg が見つかりません。録画の長さ・プレビュー画像・失敗した録画の判定ができません"
                          "（重複の検出と容量の集計はできます）。", None, wrap=True), 1)
        b = button("設定を開く", None, "settings")
        b.clicked.connect(self.open_settings.emit)
        h.addWidget(b)
        self.update_state()

    def update_state(self) -> None:
        self.setVisible(not self.tools_fn().available)


class PlanPage(QWidget):
    """ホーム → 整理候補 → 内容確認 → 最終確認 の中心画面。"""

    request_locations = Signal()
    request_settings = Signal()

    def __init__(self, state: MediaState, parent=None) -> None:
        super().__init__(parent)
        self.setObjectName("Page")
        self.state = state
        self.plan: dict | None = None
        v = QVBoxLayout(self)
        v.setContentsMargins(theme.S3, theme.S2, theme.S3, theme.S1)
        v.setSpacing(theme.S1)
        v.addWidget(label("整理候補", "title"))
        self.help = label("空けたい容量を入力すると、決まったルールで録画の整理候補を提案します（AI による判定ではありません）。"
                          "完全一致の重複を最優先にし、足りない場合だけ「要確認」の候補を追加します。", "sub", wrap=True)
        v.addWidget(self.help)
        self.banner = FFmpegBanner(state.tools_fn)
        self.banner.open_settings.connect(self.request_settings.emit)
        v.addWidget(self.banner)
        row = QHBoxLayout()
        row.addWidget(label("空けたい容量"))
        self.target = QDoubleSpinBox()
        self.target.setRange(0.1, 100000)
        self.target.setDecimals(1)
        self.target.setValue(10)
        self.target.setSuffix(" GB")
        self.target.setMinimumWidth(120)
        self.propose_button = button("提案する", "primary", "target")
        row.addWidget(self.target)
        row.addWidget(self.propose_button)
        row.addStretch()
        v.addLayout(row)
        cards = QHBoxLayout()
        cards.setSpacing(theme.S1)
        self.status_card = StatCard("目標への達成状況", "target")
        self.total_card = StatCard("提案の合計（ファイルサイズ）", "flag")
        self.free_card = StatCard("空き容量（実際）", "drive")
        for c in (self.status_card, self.total_card, self.free_card):
            cards.addWidget(c, 1)
        v.addLayout(cards)
        self.note = label("解放見込みはファイルサイズの合計です。ごみ箱へ移しただけでは空き容量は増えず、"
                          "ごみ箱を空にすると増えます。", "caption", wrap=True)
        v.addWidget(self.note)
        self.tree = QTreeWidget()
        self.tree.setHeaderLabels(["録画", "容量", "理由", "保存場所"])
        self.tree.setColumnWidth(0, 260)
        self.tree.setColumnWidth(1, 90)
        self.tree.setColumnWidth(2, 380)
        self.tree.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.tree.setUniformRowHeights(True)
        self.tree.setTextElideMode(Qt.TextElideMode.ElideRight)  # 理由の全文は詳細とツールチップで確認できる
        self.empty = EmptyState()
        self.content = QStackedWidget()
        self.content.addWidget(self.tree)
        self.content.addWidget(self.empty)
        self.detail = DetailPanel(self)
        self.detail.setMinimumWidth(300)
        self.detail.hide()
        self.splitter = QSplitter()
        self.splitter.setChildrenCollapsible(False)
        self.splitter.addWidget(self.content)
        self.splitter.addWidget(self.detail)
        v.addWidget(self.splitter, 1)
        bar = QHBoxLayout()
        self.selected_label = label("", "subtitle")
        self.protect_button = button("保護する", None, "shield", "残す録画を保護します。以後の提案・ごみ箱への移動から除外します")
        self.keep_button = button("残す（候補から外す）", None, "check", "この録画を整理しないと記録し、提案から外します")
        self.compare_button = button("重複を比較", None, "copy", "同じ内容のファイルを並べて確認します")
        self.final_button = button("最終確認へ…", "danger", "trash",
                                   "チェックした録画を確認してから Windows のごみ箱へ移します（完全削除はしません）")
        bar.addWidget(self.selected_label, 1)
        for b in (self.compare_button, self.keep_button, self.protect_button, self.final_button):
            bar.addWidget(b)
        v.addLayout(bar)

        self.propose_button.clicked.connect(self.propose)
        self.target.editingFinished.connect(self.propose)
        self.tree.itemChanged.connect(self._on_check)
        self.tree.currentItemChanged.connect(self._on_current)
        self.detail.closed.connect(self._close_detail)
        self.protect_button.clicked.connect(lambda: self._decide_current("保護する"))
        self.keep_button.clicked.connect(lambda: self._decide_current("残す"))
        self.compare_button.clicked.connect(self._compare_current)
        self.final_button.clicked.connect(self.final_confirm)
        state.changed.connect(self.replan)
        self.replan()

    # DetailPanel・ComparePanel から使う
    @property
    def ctx(self):
        return self.state.ctx

    @property
    def loader(self):
        return self.state.loader

    @property
    def db(self):
        return self.state.db

    def apply_decision(self, name: str, ids: list[int] | None = None) -> None:
        ids = ids if ids is not None else ([self._current_id()] if self._current_id() else [])
        self.state.decide(name, ids)

    def open_location(self) -> None:
        mid = self._current_id()
        if mid and os.name == "nt":
            subprocess.Popen(["explorer", "/select,", self.state.db.video(mid)["path"]])

    def selected_ids(self) -> list[int]:
        mid = self._current_id()
        return [mid] if mid else []

    # --- 提案 ---------------------------------------------------------------
    def set_target_gb(self, gb: float) -> None:
        self.target.setValue(gb)
        self.propose()

    def propose(self) -> None:
        self._proposed_target = self.target.value()
        self.replan()

    def replan(self) -> None:
        self.banner.update_state()
        ctx = self.state.ctx
        target = int(self.target.value() * 1024 ** 3)
        checked_before = {it["id"]: it for it in (self.plan or {}).get("items", [])}
        prev_checks = getattr(self, "_checks", {})
        self.plan = candidates.plan_recordings(ctx, target)
        self._checks = {}
        for it in self.plan["items"]:
            row = ctx.rows[it["id"]]
            if it["id"] in prev_checks and it["id"] in checked_before:
                self._checks[it["id"]] = prev_checks[it["id"]]
            else:  # 重複は提案どおり、要確認は内容を見てから選ぶ。保存済みの「削除候補」は選択済み
                self._checks[it["id"]] = it["kind"] == "duplicate" or row["decision"] == "削除候補"
        self._fill_tree()
        self._update_summary()

    def _fill_tree(self) -> None:
        ctx = self.state.ctx
        current = self._current_id()
        self.tree.blockSignals(True)
        self.tree.clear()
        groups = {}
        for kind, title in (("duplicate", "重複（同じ録画が残ります）"), ("review", "要確認（内容を確認してから選んでください）")):
            items = [it for it in self.plan["items"] if it["kind"] == kind]
            if not items:
                continue
            top = QTreeWidgetItem([f"{title}　{len(items)} 本・{format_size(sum(i['size'] for i in items))}"])
            f = top.font(0)
            f.setBold(True)
            top.setFont(0, f)
            top.setFlags(Qt.ItemFlag.ItemIsEnabled)
            self.tree.addTopLevelItem(top)
            top.setFirstColumnSpanned(True)  # ツリーに追加してから列を結合する
            groups[kind] = top
            for it in items:
                r = ctx.rows[it["id"]]
                child = QTreeWidgetItem([r["name"], format_size(it["size"]), " ／ ".join(it["reasons"]), r["folder"]])
                child.setData(0, Qt.ItemDataRole.UserRole, it["id"])
                child.setFlags(Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable | Qt.ItemFlag.ItemIsUserCheckable)
                child.setCheckState(0, Qt.CheckState.Checked if self._checks.get(it["id"]) else Qt.CheckState.Unchecked)
                child.setToolTip(0, r["path"])
                child.setToolTip(2, "\n".join(it["reasons"]))
                child.setIcon(0, theme.icon("film" if r["kind"] == "video" else "image", theme.TEXT_SUB, 16))
                top.addChild(child)
                if it["id"] == current:
                    self.tree.setCurrentItem(child)
            top.setExpanded(True)
        self.tree.blockSignals(False)
        self._show_content()

    def _show_content(self) -> None:
        if self.plan["items"]:
            self.content.setCurrentWidget(self.tree)
            return
        self.content.setCurrentWidget(self.empty)
        self.detail.hide()
        st = self.state.scan_state()
        if not self.state.has_data():
            if st["kind"] in ("調査中", "一時停止中"):
                self.empty.show_state("search", "調査中です", "録画が見つかると、整理候補を提案できるようになります。")
            else:
                self.empty.show_state("folder", "まだ調査していません", "録画が保存されているフォルダを選んで調査してください。",
                                      "調べる場所を選ぶ", self.request_locations.emit)
            return
        self.empty.show_state("check", "整理候補はありません",
                              "完全一致の重複も、「要確認」に当てはまる録画（失敗した可能性・500MB以上・1年以上更新なし）も"
                              "見つかりませんでした。保護・「残す」にした録画は対象外です。")

    def _update_summary(self) -> None:
        p = self.plan
        has = self.state.has_data()
        if not has:
            self.status_card.set("未調査", "調査すると提案できます。")
            self.total_card.set("-", "")
        else:
            if p["achieved"]:
                self.status_card.set("達成できます", f"提案どおりに整理すると、目標 {format_size(p['target'])} に届きます。")
            else:
                note = "条件に合う候補が足りないため、ほかの録画は追加していません。"
                self.status_card.set(f"不足 {format_size(p['shortage'])}", f"目標 {format_size(p['target'])} に対して不足します。{note}")
            ex = "・".join(f"{k} {v} 件" for k, v in p["excluded"].items() if v)
            self.total_card.set(format_size(p["total"]),
                                f"重複 {format_size(p['duplicate_total'])}・要確認 {format_size(p['review_total'])}"
                                + (f"。対象外: {ex}" if ex else ""))
        paths = [r[0] for r in self.state.db.conn.execute("SELECT path FROM scan_locations")] or [
            os.environ.get("SystemDrive", "C:") + "\\" if os.name == "nt" else "/"]
        spaces = candidates.drive_space(paths)
        if spaces:
            first = spaces[0]
            more = "".join(f"\n{d['root']}: 残り {format_size(d['free'])} / {format_size(d['total'])}" for d in spaces[1:])
            self.free_card.set(format_size(first["free"]),
                               f"{first['root']} の空き（全体 {format_size(first['total'])}）。"
                               f"提案の合計とは別の、今の実際の値です。{more}")
        else:
            self.free_card.set("取得できません")
        self._update_selected()

    def checked_ids(self) -> list[int]:
        return [mid for mid, on in self._checks.items() if on]

    def _update_selected(self) -> None:
        ids = self.checked_ids()
        ctx = self.state.ctx
        total = sum(ctx.rows[i]["size"] for i in ids if i in ctx.rows)
        target = self.plan["target"] if self.plan else 0
        rest = max(target - total, 0)
        self.selected_label.setText(f"選択中 {len(ids):,} 本・{format_size(total)}"
                                    + (f"（目標まであと {format_size(rest)}）" if rest else "（目標に届きます）"))
        self.final_button.setEnabled(bool(ids))
        mid = self._current_id()
        self.protect_button.setEnabled(bool(mid))
        self.keep_button.setEnabled(bool(mid))
        self.compare_button.setVisible(bool(mid) and mid in ctx.dup_of)

    def _on_check(self, item, column) -> None:
        mid = item.data(0, Qt.ItemDataRole.UserRole)
        if mid is not None:
            self._checks[mid] = item.checkState(0) == Qt.CheckState.Checked
            self._update_selected()

    def _current_id(self) -> int | None:
        item = self.tree.currentItem()
        return item.data(0, Qt.ItemDataRole.UserRole) if item else None

    def _plan_item(self, mid: int) -> dict | None:
        return next((it for it in (self.plan or {}).get("items", []) if it["id"] == mid), None)

    def _on_current(self, *_) -> None:
        mid = self._current_id()
        self._update_selected()
        if mid is None:
            return
        if not self.detail.isVisible():
            self.detail.show()
            total = self.splitter.width()
            panel = max(320, min(460, int(total * (0.5 if total < 1000 else 0.38))))
            self.splitter.setSizes([total - panel, panel])
        it = self._plan_item(mid)
        self.detail.show_single(mid, it["reasons"] if it else None)

    def _close_detail(self) -> None:
        self.detail.player.stop()
        self.detail.hide()
        self.tree.setCurrentItem(None)

    def _decide_current(self, name: str) -> None:
        mid = self._current_id()
        if mid:
            self._checks.pop(mid, None)
            self.state.decide(name, [mid])

    def _compare_current(self) -> None:
        mid = self._current_id()
        h = self.state.ctx.dup_of.get(mid)
        if h:
            self.detail.show()
            self.detail.show_group("重複", h)

    def resizeEvent(self, event) -> None:
        """画面が低いときは補足を隠して一覧に場所を譲る（補足はツールチップで確認できる）。"""
        super().resizeEvent(event)
        compact = self.height() < 620
        self.help.setVisible(not compact)
        self.note.setVisible(not compact)
        for card in (self.status_card, self.total_card, self.free_card):
            card.detail.setVisible(not compact)
            card.setToolTip(card.detail.text() if compact else "")
        self.selected_label.setWordWrap(compact)

    def final_confirm(self) -> None:
        ids = self.checked_ids()
        if not ids:
            return
        for mid in ids:  # 選択を「削除候補」として保存（起動し直しても残る）
            try:
                self.state.db.set_decision(mid, "削除候補")
            except PermissionError:
                pass
        self.detail.player.stop()
        dlg = TrashDialog(self.state.db, ids, self)
        dlg.exec()
        self.state.refresh()

    def stop_media(self) -> None:
        self.detail.player.stop()


# =============================================================================
# 右側: 比較（重複・類似）
# =============================================================================
class ComparePanel(QWidget):
    def __init__(self, page: "MediaBrowser") -> None:
        super().__init__()
        self.page = page
        self.group = None
        v = QVBoxLayout(self)
        self.header = QLabel()
        self.header.setWordWrap(True)
        v.addWidget(self.header)
        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.scroll.setFixedHeight(336)  # カードの高さ（小さい画面でもボタンまで見えるように固定）
        v.addWidget(self.scroll)
        self.keep_others_button = button("おすすめ以外を削除候補にする", None, "flag",
                                         "おすすめの1つを「残す」にし、ほかを削除候補にします（まだ削除しません）")
        self.keep_others_button.clicked.connect(self._mark_others)
        v.addWidget(self.keep_others_button)
        v.addStretch()
        self.thumbs: dict[int, QLabel] = {}
        page.loader.updated.connect(self._thumb_ready)

    def _thumb_ready(self, mid: int) -> None:
        label = self.thumbs.get(mid)
        if label is not None and self.page.ctx and mid in self.page.ctx.rows:
            self._set_thumb(label, self.page.ctx.rows[mid])

    def _set_thumb(self, label: QLabel, r) -> None:
        row = row_dict(r, self.page.ctx)
        pm = self.page.loader.pixmap(row)
        if pm is not None:
            label.setPixmap(pm if pm.width() <= 196 and pm.height() <= 112 else
                           pm.scaled(196, 112, Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation))
        else:
            state = self.page.loader.state(row)
            text = {"pending": "読み込み中…", "cloud": "オンライン専用"}.get(state, "")
            if state == "failed":
                text = "サムネイルを作れませんでした\n" + self.page.loader.errors.get(r["id"], "")
            label.setText(text)
            label.setToolTip(text)

    def show_group(self, category: str, key) -> None:
        ctx = self.page.ctx
        self.group = (category, key)
        dup = category == "重複"
        ids = (ctx.dup_groups if dup else ctx.sim_groups).get(key, [])
        keep_id, why = (ctx.dup_keep if dup else ctx.sim_keep).get(key, (None, ""))
        self.header.setText(
            ("内容が完全に一致（SHA-256）。1つは残す前提です。" if dup else "似ている画像です（完全一致ではありません）。")
            + f"<br>残すおすすめ: <b>{ctx.rows[keep_id]['name'] if keep_id else '-'}</b>（{why}）")
        self.header.setToolTip("おすすめは解像度などの客観的な情報だけで選んでいます。大切さは判断していません。")
        box = QWidget()
        h = QHBoxLayout(box)
        self.thumbs = {}
        ids = sorted(ids, key=lambda m: (m != keep_id, -candidates.pixels(ctx.rows[m]), -ctx.rows[m]["size"]))
        for mid in ids:  # 残すおすすめを先頭に、解像度の高い順
            h.addWidget(self._card(ctx.rows[mid], mid == keep_id, ids))
        h.addStretch()
        self.scroll.setWidget(box)

    def _card(self, r, recommended: bool, ids) -> QWidget:
        ctx = self.page.ctx
        card = QFrame()
        card.setObjectName("Card")
        card.setFixedWidth(220)
        v = QVBoxLayout(card)
        v.setContentsMargins(theme.S1 + 4, theme.S1 + 4, theme.S1 + 4, theme.S1 + 4)
        v.setSpacing(6)
        thumb = QLabel()
        thumb.setFixedSize(196, 112)
        thumb.setAlignment(Qt.AlignmentFlag.AlignCenter)
        thumb.setWordWrap(True)
        self.thumbs[r["id"]] = thumb
        self._set_thumb(thumb, r)
        v.addWidget(thumb)
        meta = loads(r["meta"], {}) or {}
        exact = [i for i in ids if i != r["id"] and r["sha256"] and ctx.rows[i]["sha256"] == r["sha256"]]
        badges = []
        if recommended:
            badges.append("<b>【残すおすすめ】</b>")
        if r["protected"]:
            badges.append("<b>保護中</b>")
        if r["decision"]:
            badges.append(r["decision"])
        if exact and self.group and self.group[0] == "類似画像":
            badges.append(f"完全一致 {len(exact)} 件あり")
        res = f"{meta.get('width')}×{meta.get('height')}" if meta.get("width") else "解像度不明"
        fm = card.fontMetrics()
        info = QLabel("<br>".join([
            f"<b>{fm.elidedText(r['name'], Qt.TextElideMode.ElideMiddle, 190)}</b>",
            " ".join(badges) or "&nbsp;",
            f"{res} · {format_size(r['size'])}",
            f"撮影: {candidates.format_date(r['taken_at'])}",
            f"更新: {candidates.format_date(r['mtime'])}",
            fm.elidedText(r["folder"], Qt.TextElideMode.ElideLeft, 190)]))
        info.setToolTip(r["path"])
        v.addWidget(info)
        row = QHBoxLayout()
        for name, text in (("残す", "残す"), ("削除候補", "削除候補にする")):
            b = QPushButton(text)
            b.clicked.connect(partial(self._decide, name, r["id"]))
            row.addWidget(b)
        v.addLayout(row)
        prot = QPushButton("保護を解除" if r["protected"] else "保護する")
        prot.clicked.connect(partial(self._decide, "保護を解除" if r["protected"] else "保護する", r["id"]))
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

    def __init__(self, page: "MediaBrowser") -> None:
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


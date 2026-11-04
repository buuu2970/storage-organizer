"""整理画面（担当: UI）。

ホーム・ファイル一覧・整理のおすすめ・詳細パネル・ステータス欄。
調査は起動時に自動で始まり、見つかったものから順次表示する。ごみ箱への移動は確認画面から行う。
見た目は theme.py にまとめ、ここでは配置と操作の流れを扱う。
"""

from __future__ import annotations

import os
import shutil
import subprocess
import threading
from functools import partial

from PySide6.QtCore import (QAbstractTableModel, QItemSelectionModel, QModelIndex, QObject, QRectF, QRunnable,
                            QSize, Qt, QThread, QThreadPool, QTimer, Signal)
from PySide6.QtGui import QColor, QFont, QFontMetrics, QIcon, QImageReader, QPainter, QPen, QPixmap
from PySide6.QtWidgets import (
    QAbstractItemView, QCheckBox, QComboBox, QDialog, QDialogButtonBox, QDoubleSpinBox, QFileDialog, QFormLayout, QFrame,
    QGridLayout, QGroupBox, QHBoxLayout, QLabel, QLineEdit, QListView, QListWidget, QListWidgetItem, QMenu, QMessageBox,
    QProgressBar, QPushButton, QScrollArea, QSplitter, QStackedWidget, QStyle, QStyledItemDelegate, QTabBar,
    QTableView, QTableWidget, QTableWidgetItem, QTabWidget, QToolButton, QTreeWidget, QTreeWidgetItem,
    QVBoxLayout, QWidget,
)

from . import autoscan, candidates, media, theme, trash
from .store import Store as Database, StoreError, loads
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


THUMB_CACHE_MAX_BYTES = 300 * 1024 * 1024


def prune_thumb_cache(folder, max_bytes: int = THUMB_CACHE_MAX_BYTES) -> int:
    """サムネイルのキャッシュが上限を超えたら、古いものから消して上限の 8 割まで減らす（アプリのキャッシュだけ）。"""
    try:
        files = [(e.stat().st_mtime, e.stat().st_size, e.path) for e in os.scandir(folder) if e.is_file()]
    except OSError:
        return 0
    total = sum(f[1] for f in files)
    removed = 0
    if total <= max_bytes:
        return 0
    for _, size, path in sorted(files):
        if total <= max_bytes * 0.8:
            break
        try:
            os.remove(path)
            total -= size
            removed += 1
        except OSError:
            pass
    return removed


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
SORTS = ["容量が大きい順", "容量が小さい順", "更新日時が新しい順", "更新日時が古い順", "名前順",
         "録画日時が新しい順", "録画日時が古い順", "動画時間が長い順", "1分あたりの容量が大きい順", "見返していない順"]


def sort_rows(rows: list[dict], how: str) -> list[dict]:
    """日時が不明なものは、ほかの日時で代用せず末尾に並べる。"""
    def unknown_last(key, reverse):
        known = [r for r in rows if r[key] is not None]
        unknown = [r for r in rows if r[key] is None]
        return sorted(known, key=lambda r: r[key], reverse=reverse) + unknown
    if how in ("録画日時が新しい順", "撮影・録画日時が新しい順"):
        return unknown_last("taken_at", True)
    if how in ("録画日時が古い順", "撮影・録画日時が古い順"):
        return unknown_last("taken_at", False)
    if how == "容量が小さい順":
        return sorted(rows, key=lambda r: r["size"])
    if how == "名前順":
        return sorted(rows, key=lambda r: (r["name"].casefold(), r["path"]))
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
            "favorite": bool(r.get("favorite")), "ext": r["ext"] or "",
            "reasons": ctx.short_reasons(r) if hasattr(ctx, "short_reasons") else [], "group": None}


# =============================================================================
# 一覧のモデルとカード描画
# =============================================================================
COLUMNS = ["名前", "形式", "容量", "更新日時", "判断", "お気に入り", "保護", "整理候補の理由", "保存場所"]
KIND_LABEL = {"image": "画像", "video": "動画"}

# 判断の用語と説明（画面内で統一して使う）
DECISION_HELP = {
    "未判断": "まだ判断していません。",
    "残す": "この録画は残しておきます。",
    "保留": "あとで確認して決めます。",
    "削除候補": "ごみ箱に移動する候補に追加します。まだ削除されません。",
    "保護中": "保護を解除するまでごみ箱へ移動できません。",
    "お気に入り": "整理のおすすめから外します（「残す」に近い扱い）。お気に入りを外すと対象に戻ります。",
}


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
                return (it.get("ext") or "").lstrip(".").upper() + (f"（{len(it['members'])}件）" if it.get("members") else "")
            if col == 2:
                return format_size(it["size"])
            if col == 3:
                return candidates.format_date(it["mtime"])
            if col == 4:
                return it.get("decision") or "未判断"
            if col == 5:
                return "★" if it.get("favorite") else ""
            if col == 6:
                return "保護中" if it.get("protected") else ""
            if col == 7:
                return "・".join(it.get("reasons") or [])
            if col == 8:
                return it["folder"]
        if role == Qt.ItemDataRole.DecorationRole and col == 0:
            return theme.icon("film" if it["kind"] == "video" else "image", theme.TEXT_SUB, 16)
        if role == Qt.ItemDataRole.TextAlignmentRole and col == 2:
            return int(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        if role == Qt.ItemDataRole.ToolTipRole and col == 4:
            state = it.get("decision") or "未判断"
            return f"{state}: {DECISION_HELP.get(state, '')}"
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
        sub = f"{format_size(it['size'])} · {(it.get('ext') or '').lstrip('.').upper() or KIND_LABEL.get(it['kind'], '')}"
        if it.get("reasons"):
            sub += " · " + "・".join(it["reasons"])
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
        text = state if not it.get("members") else "比較して選ぶ"
        if it.get("favorite") and not it.get("members"):
            text += "　★お気に入り"
        painter.drawText(QRectF(cx, y + 38, w - (cx - x), 18), Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                         text)
        painter.restore()


# =============================================================================
# 自動スキャンの実行
# =============================================================================
class AutoScanRunner(QObject):
    progress = Signal(object)
    finished = Signal(object)
    state_changed = Signal(bool)

    def __init__(self, store, tools_fn) -> None:
        super().__init__()
        self.store, self.tools_fn = store, tools_fn
        self.thread: QThread | None = None
        self.worker: TaskWorker | None = None
        self.ctl: autoscan.Controller | None = None

    def is_running(self) -> bool:
        return self.thread is not None

    def start(self) -> bool:
        if self.is_running():
            return False
        self.ctl = ctl = autoscan.Controller()
        store, tools = self.store, self.tools_fn()
        self.thread = QThread(self)
        self.worker = TaskWorker(lambda c, r: autoscan.task_autoscan(store, tools, ctl, r))
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
        prune_thumb_cache(db.thumb_dir)
        self.loader = ThumbnailLoader(db, tools_fn)
        self.runner = AutoScanRunner(db, tools_fn)  # 画面と同じ保存オブジェクトを共有する
        self.ctx = candidates.Context(db)
        self.last_progress: dict = {}
        self.last_result: dict | None = None
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.timeout.connect(self.refresh)
        self.runner.progress.connect(self._on_progress)
        self.runner.finished.connect(self._on_finished)
        self.runner.state_changed.connect(self._on_runner_state)

    def refresh(self) -> None:
        self.ctx = candidates.Context(self.db)
        self.changed.emit()

    def _on_runner_state(self, running: bool) -> None:
        # 調査スレッドが終わった時点で、もう一度すべての画面を更新する
        # （完了の通知はスレッドの終了より先に届くため、そのときは「調査中」として集計されてしまう）
        if not running:
            self.refresh()
        self.scan_progress.emit(self.last_progress)

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
        """判断を記録する。「保護する」が付いたものは削除候補にしない。スキップ件数を返す。

        判断（残す・保留・削除候補・解除）は、選んだ件数にかかわらず1回の保存でまとめて記録する。
        """
        name = DECISION_ALIASES.get(name, name)
        skipped = 0
        try:
            if name in ("保護する", "保護を解除", "お気に入り", "お気に入りを外す"):
                for mid in ids:
                    if name in ("保護する", "保護を解除"):
                        self.db.set_protected(mid, name == "保護する")
                    else:
                        self.db.set_favorite(mid, name == "お気に入り")
            else:
                skipped = len(self.db.set_decisions(ids, None if name in ("判断を解除", "未判断") else name))
        except StoreError as e:  # 保存できなかった判断は、成功したように見せずにその場で伝える
            QMessageBox.warning(None, "保存できませんでした", str(e))
        self.refresh()
        return skipped

    def has_data(self) -> bool:
        return bool(self.ctx.rows)

    def scan_state(self) -> dict:
        """ステータス欄に出す状態。kind: 未調査 / 調査中 / 一時停止中 / 調査完了 / 一部を調査できなかった / 中断"""
        db, info = self.db, self.last_progress
        running = self.runner.is_running()
        skips = db.skip_count("error")
        unfinished = sum(1 for x in db.locations() if x["status"] in ("中断", "調査中"))
        if running and self.runner.ctl and self.runner.ctl.paused:
            return {"kind": "一時停止中", "text": "一時停止中", "icon": "pause"}
        if running:
            if info.get("phase") == "scan":
                return {"kind": "調査中", "icon": "search",
                        "text": f"ファイルを探しています · {info.get('location', '')} · 見つけた録画 {info.get('found', 0):,} 件・"
                                f"{format_size(info.get('bytes', 0))}",
                        "tooltip": f"調べている場所: {info.get('current', '')}\n全体の件数は調べ終わるまで分かりません"}
            if info.get("text"):
                progress = (info["done"], info["total"]) if info.get("total") else None
                return {"kind": "調査中", "icon": "search", "text": info["text"], "progress": progress}
            return {"kind": "調査中", "icon": "search", "text": "調査を開始しています…"}
        last = db.get_setting("scan_finished")
        if last is None and not self.ctx.rows:
            return {"kind": "未調査", "icon": "info", "text": "まだ調査していません"}
        analysis = db.get_setting("analysis_state")
        if (self.last_result or {}).get("cancelled") or unfinished or analysis in ("running", "cancelled"):
            extra = "（重複・類似の確認が途中です）" if analysis in ("running", "cancelled") and not unfinished else ""
            return {"kind": "中断", "icon": "warning",
                    "text": f"調査を中断しました{extra}。途中までの結果を表示しています。「再スキャン」で調べ直せます"}
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


INFO_HELP = "\n".join(f"「{k}」… {v}" for k, v in DECISION_HELP.items()) + (
    "\n\nファイルがごみ箱へ移るのは、「削除候補」画面の最終確認で確定したときだけです。完全削除はしません。")


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
        self.keep = button("残す", None, "check", DECISION_HELP["残す"])
        self.hold = button("保留", None, "pause", DECISION_HELP["保留"])
        self.delete = button("削除候補にする", None, "flag", DECISION_HELP["削除候補"])
        self.protect = button("保護する", None, "shield", DECISION_HELP["保護中"])
        self.favorite = button("お気に入り", None, "star", DECISION_HELP["お気に入り"])
        self.more = QToolButton()
        self.more.setText("その他")
        self.more.setIcon(theme.icon("more", theme.TEXT, 16))
        self.more.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        self.more.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        menu = QMenu(self.more)
        self.act_clear = menu.addAction("未判断に戻す")
        self.act_unprotect = menu.addAction("保護を解除")
        self.act_unfavorite = menu.addAction("お気に入りを外す")
        menu.addSeparator()
        self.act_open = menu.addAction("保存場所を開く")
        self.act_help = menu.addAction("判断の種類の説明")
        self.more.setMenu(menu)
        self.close = QToolButton()
        self.close.setIcon(theme.icon("close", theme.TEXT, 16))
        self.close.setToolTip("選択を解除")
        theme.set_kind(self.close, "subtle")
        for w in (self.keep, self.hold, self.delete, self.protect, self.favorite, self.more, self.close):
            h.addWidget(w)
        self.keep.clicked.connect(lambda: browser.apply_decision("残す"))
        self.hold.clicked.connect(lambda: browser.apply_decision("保留"))
        self.delete.clicked.connect(lambda: browser.apply_decision("削除候補"))
        self.protect.clicked.connect(lambda: browser.apply_decision("保護する"))
        self.favorite.clicked.connect(lambda: browser.apply_decision("お気に入り"))
        self.act_clear.triggered.connect(lambda: browser.apply_decision("判断を解除"))
        self.act_unprotect.triggered.connect(lambda: browser.apply_decision("保護を解除"))
        self.act_unfavorite.triggered.connect(lambda: browser.apply_decision("お気に入りを外す"))
        self.act_open.triggered.connect(browser.open_location)
        self.act_help.triggered.connect(lambda: QMessageBox.information(self, "判断の種類", INFO_HELP))
        self.close.clicked.connect(browser.clear_selection)

    def resizeEvent(self, event) -> None:
        """幅が狭いときは「保護する」「お気に入り」をアイコンだけにして、主な操作が切れないようにする。"""
        super().resizeEvent(event)
        compact = self.width() < 900
        for b, text in ((self.protect, "保護する"), (self.favorite, "お気に入り")):
            b.setText("" if compact else text)
            b.setAccessibleName(text)

    def update_for(self, ids: list[int], ctx: candidates.Context, group: bool = False) -> None:
        total = sum(ctx.rows[i]["size"] for i in ids if i in ctx.rows)
        if group:
            # グループ全体を一度に削除候補にしないよう、比較画面で1件ずつ判断してもらう
            self.count.setText("グループを選択中 · 右の比較で1件ずつ判断してください")
        else:
            self.count.setText(f"{len(ids):,} 件を選択 · {format_size(total)}")
        for b in (self.keep, self.hold, self.delete, self.protect, self.favorite, self.more):
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
        from .player import VideoPlayer
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
        self.open_default = button("既定のアプリで開く", "link", None, "Windows で関連付けられたアプリでファイルを開きます")
        self.open_default.clicked.connect(self._open_default)
        self.preview_section.v.addWidget(self.open_default, 0, Qt.AlignmentFlag.AlignLeft)
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
                                                "「削除候補にする」だけではファイルは移動しません。", "caption", wrap=True))
        self.fav_row = QHBoxLayout()
        self.fav_button = button("★ お気に入りにする", None, "star", DECISION_HELP["お気に入り"])
        self.protect_toggle = button("保護する", None, "shield", DECISION_HELP["保護中"])
        self.fav_row.addWidget(self.fav_button)
        self.fav_row.addWidget(self.protect_toggle)
        self.fav_row.addStretch()
        self.decision_section.v.addLayout(self.fav_row)
        self.fav_button.clicked.connect(self._toggle_favorite)
        self.protect_toggle.clicked.connect(self._toggle_protect)
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
            b = button(text, None, ic, DECISION_HELP[name])
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
        add("形式", (r["ext"] or "").lstrip(".").upper() + (f" · {meta['width']}×{meta['height']}" if meta.get("width") else "")
            + (f" · {meta['fps']:g} fps" if meta.get("fps") else ""))
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
        explain = DECISION_HELP[state]
        if state == "削除候補":
            explain += "「削除候補」画面の最終確認で、ごみ箱へ移すかを確定します。"
        extra = f"（判断: {r['decision']}）" if r["protected"] and r["decision"] else ""
        fav = "<br><b>★ お気に入り</b>: " + DECISION_HELP["お気に入り"] if r["favorite"] else ""
        self.decision.setText(f"<b>{state}</b>{extra}<br>{explain}{fav}")
        editable = db.protection_ok
        self.fav_button.setText("★ お気に入りを外す" if r["favorite"] else "★ お気に入りにする")
        self.protect_toggle.setText("保護を解除" if r["protected"] else "保護する")
        self.fav_button.setEnabled(editable)
        self.protect_toggle.setEnabled(editable)

    def _toggle_favorite(self) -> None:
        r = self.browser.state.db.video(self.media_id) if self.media_id is not None else None
        if r is not None:
            self.browser.apply_decision("お気に入りを外す" if r["favorite"] else "お気に入り", [r["id"]])

    def _toggle_protect(self) -> None:
        r = self.browser.state.db.video(self.media_id) if self.media_id is not None else None
        if r is not None:
            self.browser.apply_decision("保護を解除" if r["protected"] else "保護する", [r["id"]])

    def _load_preview(self, r) -> None:
        self.player.stop()
        self.open_default.setVisible(not r["cloud_only"])
        if r["cloud_only"]:
            self.preview_stack.setCurrentWidget(self.preview_msg)
            self.preview_msg.show_state("info", "プレビューしません", "オンライン専用ファイルのため、自動でダウンロードしません。")
            return
        if r["kind"] == "video":
            self.preview_stack.setCurrentWidget(self.player)
            poster = str(self.browser.state.db.thumb_dir / f"m{r['id']}_{int(r['mtime'])}_{r['size']}.jpg")
            self.player.set_source(r["path"], poster)
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
                                        "形式が未対応か、ファイルを読み込めない可能性があります。ファイル情報と理由は下で確認できます。"
                                        f"\n（詳細: {reader.errorString()}）",
                                        "既定のアプリで開く", self._open_default)
            return
        self.preview_stack.setCurrentWidget(self.image)
        pm = QPixmap.fromImage(img)
        w = max(self.image.width() - 8, 240)
        h = 300 if self.window().height() > 700 else 200
        if pm.width() > w or pm.height() > h:  # 縮小のみ（拡大しない）
            pm = pm.scaled(w, h, Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation)
        self.image.setPixmap(pm)

    def _open_default(self) -> None:
        from PySide6.QtCore import QUrl
        from PySide6.QtGui import QDesktopServices
        if self.media_id is not None:
            row = self.browser.state.db.video(self.media_id)
            if row is not None and not row["cloud_only"]:
                QDesktopServices.openUrl(QUrl.fromLocalFile(row["path"]))

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
    "削除候補": "削除候補に入れたファイルです。最終確認でごみ箱へ移すかを確定します（完全削除はしません）。",
}
EMPTY_HELP = {
    "重複": "内容が完全に一致するファイルは見つかりませんでした。",
    "類似画像": "似ている画像のグループは見つかりませんでした。",
    "録画確認候補": "失敗した可能性のある録画・画像は見つかりませんでした。",
    "大容量": "100MB 以上のファイルは見つかりませんでした。",
    "削除候補": "削除候補のファイルはまだありません。比較して「これを残す」を選ぶと、ほかのコピーがここに入ります。",
}
# (表示名, 絞り込みの値)
STATE_FILTERS = [("すべての判断", "すべての判断"), ("未判断", "未判断"), ("残す", "残す"), ("保留", "保留"),
                 ("削除候補", "削除候補"), ("保護中", "保護中"), ("★ お気に入り", "お気に入り"),
                 ("整理候補の理由がある", "理由あり")]
CANDIDATES_HELP = ("「削除候補」にした録画です。ここで最終確認をしてから、Windows のごみ箱へ移します（完全削除はしません）。"
                   "候補から外すときは、選んで「残す」「保留」などを押してください。")


class MediaBrowser(QWidget):
    """mode="files"（ファイル一覧）または "candidates"（削除候補）。"recommend" は以前の画像整理用（今の版では使わない）。"""

    final_requested = Signal(list)

    def add_header_link(self, text: str, fn) -> None:
        b = button(text, "link")
        b.clicked.connect(fn)
        self._head.insertWidget(1, b, 0, Qt.AlignmentFlag.AlignTop)

    request_locations = Signal()
    request_files = Signal()

    def __init__(self, state: MediaState, mode: str, parent=None) -> None:
        super().__init__(parent)
        self.setObjectName("Page")
        self.state = state
        self.mode = mode
        self.category = {"recommend": "重複", "candidates": "削除候補"}.get(mode, "すべて")
        v = QVBoxLayout(self)
        v.setContentsMargins(theme.S3, theme.S2, theme.S3, theme.S1)
        v.setSpacing(theme.S1)
        head = QHBoxLayout()
        title_box = QVBoxLayout()
        title_box.setSpacing(2)
        self.title = label({"files": "ファイル一覧", "candidates": "削除候補"}.get(mode, "画像の整理（補助）"), "title")
        self.help = label("", "sub", wrap=True)
        title_box.addWidget(self.title)
        title_box.addWidget(self.help)
        head.addLayout(title_box, 1)
        self.target_button = button("目標容量から提案", None, "target",
                                    "「20GB空けたい」などの目標から、整理候補の組み合わせを提案します")
        self.trash_button = button("最終確認へ（ごみ箱へ移動）…", "danger", "trash",
                                   "削除候補の一覧（名前・保存場所・件数・容量）を確認してから、Windows のごみ箱へ移します"
                                   "（完全削除はしません）")
        head.addWidget(self.target_button, 0, Qt.AlignmentFlag.AlignTop)
        head.addWidget(self.trash_button, 0, Qt.AlignmentFlag.AlignTop)
        self._head = head
        if mode != "recommend":  # 目標容量の提案は「整理のおすすめ」で行う
            self.target_button.hide()
        if mode == "files":  # ごみ箱へ移す操作は「削除候補」画面の最終確認だけで行う
            self.trash_button.hide()
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
        self.kind_filter.setVisible(mode == "recommend")  # 今の版は録画（動画）だけを一覧にする
        self.state_filter = QComboBox()
        for text, value in STATE_FILTERS:
            self.state_filter.addItem(text, value)
        self.state_filter.setVisible(mode == "files")
        self.state_filter.setToolTip("\n".join(f"{k}: {v}" for k, v in DECISION_HELP.items()))
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
        self.grid_button.setChecked(mode == "recommend")
        self.list_button.setChecked(mode != "recommend")  # 録画の一覧は、項目が並ぶリスト表示を基本にする
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
        for i, w in enumerate((260, 60, 90, 130, 80, 90, 70, 160)):
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
            idx = self.state_filter.findData(mapping.get(category, "すべての判断"))
            self.state_filter.setCurrentIndex(max(idx, 0))
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
        elif self.mode == "candidates":
            self.help.setText(CANDIDATES_HELP)
            ids = candidates.category_ids(ctx, "削除候補")
        else:
            self.help.setText("調査で見つかった録画です。選ぶとプレビュー・ファイル情報・整理候補になった理由と、判断の操作が表示されます。")
            ids = candidates.category_ids(ctx, "すべて")
        rows = [row_dict(ctx.rows[i], ctx) for i in ids]  # 一覧に登録されるのは録画だけ（autoscan.KIND_OF_CATEGORY）
        text = self.search.text().strip().casefold()
        if text:
            rows = [r for r in rows if text in r["name"].casefold() or text in r["folder"].casefold()]
        kind = {1: "image", 2: "video"}.get(self.kind_filter.currentIndex()) if self.mode == "recommend" else None
        if kind:
            rows = [r for r in rows if r["kind"] == kind]
        if self.mode == "files" and self.state_filter.currentIndex() > 0:
            want = self.state_filter.currentData()
            if want == "お気に入り":
                rows = [r for r in rows if r["favorite"]]
            elif want == "理由あり":
                rows = [r for r in rows if r["reasons"]]
            else:
                rows = [r for r in rows if decision_text(r) == want]
        rows = sort_rows(rows, self.sort_combo.currentText())
        if self.mode == "recommend" and self.category in ("重複", "類似画像"):
            rows = self._group_items(rows)
        self.model.set_items(rows)
        trash_rows = [r for r in ctx.rows.values() if r["decision"] == "削除候補"]
        self.trash_button.setVisible(self.mode == "candidates")
        self.trash_button.setEnabled(bool(trash_rows) and self.state.db.protection_ok)
        count = len({m for r in rows for m in (r.get("members") or [r["id"]])})
        total = sum(r["size"] for r in rows)
        self.footer.setText(f"{count:,} 件 · {format_size(total)}"
                            + (f" · 削除候補 {len(trash_rows):,} 件（{format_size(sum(r['size'] for r in trash_rows))}）"
                               if trash_rows else "")
                            + (" · ごみ箱へ移しただけでは空き容量は増えません（ごみ箱を空にすると増えます）"
                               if self.mode == "candidates" else ""))
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
                self.empty.show_state("search", "調査中です", "見つかった録画から順に表示します。しばらくお待ちください。")
            else:
                self.empty.show_state("folder", "まだ調査していません",
                                      "PCを調べると、録画を探して整理候補を表示します。",
                                      "PCを調べる", self.request_locations.emit)
        elif self.mode == "candidates" and not self._filters_active():
            self.empty.show_state("check", "削除候補はまだありません",
                                  "「整理のおすすめ」や「ファイル一覧」で録画を選び、「削除候補にする」を押すとここに入ります。"
                                  "\n削除候補にしただけでは、ファイルは移動・削除されません。",
                                  "ファイル一覧を見る", self.request_files.emit)
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
        """削除候補の最終確認へ進む（ごみ箱への移動は最終確認の画面でだけ行う）。"""
        ids = [r["id"] for r in self.state.ctx.rows.values() if r["decision"] == "削除候補"]
        if ids:
            self.final_requested.emit(ids)

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
    """おすすめを見せるホーム（容量・調査状況・重複/類似録画のおすすめ・目標容量）。"""

    start_plan = Signal(float, object)
    open_group = Signal(str, object)
    request_locations = Signal()
    request_files = Signal()
    request_settings = Signal()
    request_capacity = Signal()
    request_candidates = Signal()

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
        scope_row = QHBoxLayout()
        self.scope = label("", "caption", wrap=True)
        self.scope_button = button("調査範囲を見る", "link")
        self.scope_button.clicked.connect(lambda: ScopeDialog(state.db, self.window()).exec())
        scope_row.addWidget(self.scope, 1)
        scope_row.addWidget(self.scope_button)
        v.addLayout(scope_row)
        self.banner = FFmpegBanner(state.tools_fn)
        self.banner.open_settings.connect(self.request_settings.emit)
        v.addWidget(self.banner)
        self.data_banner = DataProblemBanner(state.db)
        self.data_banner.open_settings.connect(self.request_settings.emit)
        v.addWidget(self.data_banner)
        actions = QHBoxLayout()
        self.scan_button = button("PCをもう一度調べる", None, "search", "調べる範囲（ドライブ・フォルダ）を確認して調査を始めます")
        self.primary = button("おすすめを確認する", "primary", "sparkle")
        self.primary.setMinimumHeight(36)
        actions.addWidget(self.primary)
        actions.addWidget(self.scan_button)
        actions.addStretch()
        v.addLayout(actions)
        # おすすめ（代表画像と次の操作を目立たせる）
        self.rec = card_frame()
        rv = QVBoxLayout(self.rec)
        rv.setContentsMargins(theme.S2, theme.S2, theme.S2, theme.S2)
        rh = QHBoxLayout()
        ic = QLabel()
        ic.setPixmap(theme.pixmap("sparkle", theme.ACCENT, 18))
        rh.addWidget(ic)
        rh.addWidget(label("どこから確認するとよいか（理由付きのおすすめ）", "subtitle"), 1)
        rv.addLayout(rh)
        self.rec_rows = QVBoxLayout()
        self.rec_rows.setSpacing(theme.S1)
        rv.addLayout(self.rec_rows)
        v.addWidget(self.rec)
        grid = QGridLayout()
        grid.setSpacing(theme.S2)
        self.free = StatCard("ドライブの空き容量（実際）", "drive")
        self.total = StatCard("調査済みの録画", "film")
        self.goal = card_frame()  # 任意の目標容量
        gv = QVBoxLayout(self.goal)
        gv.setContentsMargins(theme.S2, theme.S2, theme.S2, theme.S2)
        gh = QHBoxLayout()
        ic = QLabel()
        ic.setPixmap(theme.pixmap("target", theme.ACCENT, 18))
        gh.addWidget(ic)
        gh.addWidget(label("追加で空けたい容量（任意）", "sub"), 1)
        gv.addLayout(gh)
        row = QHBoxLayout()
        self.target = QDoubleSpinBox()
        self.target.setRange(0.1, 100000)
        self.target.setDecimals(1)
        self.target.setValue(10)
        self.target.setSuffix(" GB")
        self.target.setMinimumWidth(100)
        self.target.setToolTip("今より○GB 空けたい、という目標です（空き容量を○GBにする、ではありません）")
        self.drive = QComboBox()
        self.goal_button = button("この目標で提案", None, "target")
        row.addWidget(self.target)
        row.addWidget(self.drive)
        gv.addLayout(row)
        gv.addWidget(self.goal_button)
        self.goal_note = label("追加で空けたい容量です。完全重複を優先し、足りなければ類似録画などの要確認の候補を提案します。",
                               "caption", wrap=True)
        gv.addWidget(self.goal_note)
        self.usage = StatCard("何が容量を使っているか", "drive")
        self.usage_link = button("容量の内訳を見る", "link")
        self.usage_link.clicked.connect(self.request_capacity.emit)
        self.usage.body.addWidget(self.usage_link, 0, Qt.AlignmentFlag.AlignLeft)
        self.saving = StatCard("重複を整理した場合の削減見込み", "copy")
        self.cand = StatCard("整理候補（理由のある録画）", "sparkle")
        self.cand_link = button("整理のおすすめを見る", "link")
        self.cand_link.clicked.connect(lambda: self.start_plan.emit(0.0, None))
        self.cand.body.addWidget(self.cand_link, 0, Qt.AlignmentFlag.AlignLeft)
        self.selected = StatCard("削除候補（あなたが選んだ録画）", "flag")
        self.selected_link = button("削除候補を確認する", "link")
        self.selected_link.clicked.connect(self.request_candidates.emit)
        self.selected.body.addWidget(self.selected_link, 0, Qt.AlignmentFlag.AlignLeft)
        self.images = StatCard("画像（補助機能）", "image")  # 今の版では表示しない
        self.images.hide()
        self.grid = grid
        # 最初に見る情報: 録画の件数と容量・整理候補・削減見込み・選んだ削除候補・ドライブの空き・容量の内訳
        self.cards = [self.total, self.cand, self.saving, self.selected, self.free, self.usage, self.goal]
        self._cols = 0
        self._relayout(3)
        v.addLayout(grid)
        v.addStretch()
        self.primary.clicked.connect(self._primary)
        self.scan_button.clicked.connect(self.request_locations.emit)
        self.goal_button.clicked.connect(lambda: self.start_plan.emit(self.target.value(), self.drive.currentData()))
        state.changed.connect(self.refresh)
        state.scan_progress.connect(lambda _: self.refresh_status())
        self.refresh()

    # 互換: 以前の「調べる場所を選ぶ…」ボタン
    @property
    def locations_button(self):
        return self.scan_button

    def _relayout(self, cols: int) -> None:
        """画面の幅に合わせて、カードを 3・2・1 列に並べ替える（横にはみ出さないように）。"""
        if cols == self._cols:
            return
        self._cols = cols
        for card in self.cards:
            self.grid.removeWidget(card)
        for i, card in enumerate(self.cards):
            self.grid.addWidget(card, i // cols, i % cols)
        for c in range(3):
            self.grid.setColumnStretch(c, 1 if c < cols else 0)

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        w = self.width()
        self._relayout(3 if w >= 1000 else 2 if w >= 640 else 1)

    def _primary(self) -> None:
        if self.state.has_data():
            self.start_plan.emit(0.0, None)  # 目標なしで、おすすめを確認する
        else:
            self.request_locations.emit()

    def refresh_status(self) -> None:
        self.banner.update_state()
        self.data_banner.update_state()
        st = self.state.scan_state()
        has = self.state.has_data()
        scanning = st["kind"] in ("調査中", "一時停止中")
        self.primary.setText("おすすめを確認する" if has else ("調査中…" if scanning else "PCの容量を調べる"))
        self.primary.setIcon(theme.icon("sparkle" if has else "search", "#FFFFFF", 16))
        self.primary.setEnabled(has or not scanning)
        self.scan_button.setVisible(has)
        self.scan_button.setEnabled(not scanning)
        # 初回は「PCの容量を調べる」と空き容量だけを見せ、まだ使えない欄は出さない
        self.rec.setVisible(has or scanning)
        self.goal.setVisible(has)
        self.usage.setVisible(has or scanning)
        for card in (self.total, self.cand, self.saving, self.selected):
            card.setVisible(has or scanning)
        self.primary.setMinimumHeight(44 if not has else 36)
        self.goal_button.setEnabled(has)
        db = self.state.db
        labels = [x["label"] for x in db.locations()]
        last = db.get_setting("scan_finished")
        skips = db.skip_count("error")
        if labels:
            shown = "、".join(labels[:3]) + (f" ほか {len(labels) - 3} か所" if len(labels) > 3 else "")
            self.scope.setText(f"調査範囲: {shown} · 最終調査: {candidates.format_date(float(last)) if last else '未完了'}"
                               + (f" · 調査できなかった場所: {skips} か所" if skips else ""))
        else:
            self.scope.setText("調査範囲: まだ選んでいません")
        self.scope_button.setVisible(bool(labels))
        if has and scanning:
            self.lead.setText("前回の調査結果を表示しています。バックグラウンドで最新の状態に更新中です。")
        elif has:
            self.lead.setText("録画の容量と整理候補をまとめました。まず「おすすめを確認する」で、理由の付いた候補から確認しましょう。"
                              "ファイルがごみ箱へ移るのは、「削除候補」画面で最終確認したときだけです。")
        elif scanning:
            self.lead.setText("調査中です。見つかった録画から順に集計します。")
            self.total.set("調査中")
            self.saving.set("算出中")
        else:
            self.lead.setText("PC内のドライブを調べて、録画がどれくらい容量を使っているかと、整理を考えるとよい録画"
                              "（重複・極端に短い・黒画面が多い・読み込めないなど）を理由付きで示します。"
                              "まず「PCの容量を調べる」を押してください（ファイルは変更しません）。")

    def refresh(self) -> None:
        ctx = self.state.ctx
        db = self.state.db
        videos = [r for r in ctx.rows.values() if r["kind"] == "video"]
        images = [r for r in ctx.rows.values() if r["kind"] == "image"]
        scanning = self.state.runner.is_running()
        paths = [x["path"] for x in db.locations()]
        drives = candidates.media_drives(ctx, paths) or [d["root"] for d in candidates.drive_space(
            [system_drive_usage()[0] if system_drive_usage() else "/"])]
        current = self.drive.currentData()
        self.drive.clear()
        for d in drives:
            self.drive.addItem(d, d)
        if current in drives:
            self.drive.setCurrentIndex(drives.index(current))
        spaces = candidates.drive_space(drives or paths)
        if spaces:
            d = spaces[0]
            more = "".join(f"／{x['root']} 空き {format_size(x['free'])}" for x in spaces[1:])
            self.free.set(format_size(d["free"]), f"{d['root']} の空き（使用 {format_size(d['used'])} / "
                                                   f"全体 {format_size(d['total'])}）{more}。ドライブの実際の値です。")
        else:
            self.free.set("取得できません")
        stats = db.location_stats()
        cats: dict[str, int] = {}
        for st in stats.values():
            for cat, (_, b) in st.get("by_category", {}).items():
                cats[cat] = cats.get(cat, 0) + b
        if cats:
            top = sorted(cats.items(), key=lambda x: -x[1])[:3]
            self.usage.set(top[0][0], "・".join(f"{c} {format_size(b)}" for c, b in top)
                           + "（調査した範囲のファイルサイズ）")
        else:
            self.usage.set("未調査", "PCを調べると、種類別・フォルダ別の容量を表示します。")
        chosen = [r for r in videos if r["decision"] == "削除候補"]
        self.selected.set(f"{len(chosen):,} 本", f"合計 {format_size(sum(r['size'] for r in chosen))}。"
                                               "まだごみ箱へは移していません（最終確認で移します）")
        if not ctx.rows:
            self.total.set("未調査", "調査すると、録画の本数と合計サイズを表示します。")
            self.saving.set("算出できません", "調査結果がないため算出できません。")
            self.cand.set("未調査", "調査すると、整理を考えるとよい録画を理由付きで示します。")
            self.images.set("未調査")
        else:
            self.total.set(f"{len(videos):,} 本",
                           f"合計 {format_size(sum(r['size'] for r in videos))}（調査した録画のファイルサイズの合計。"
                           "ドライブ全体の使用量とは別です）" + ("。調査中のため増える可能性があります" if scanning else ""))
            plan = candidates.plan_recordings(ctx, None)
            kinds = {"duplicate": "重複", "similar": "類似", "review": "要確認"}
            counts = "・".join(f"{kinds[k]} {n}" for k in kinds
                               if (n := sum(1 for it in plan["items"] if it["kind"] == k)))
            self.cand.set(f"{len(plan['items']):,} 本",
                          f"合計 {format_size(plan['total'])}" + (f"（{counts}）" if counts else "")
                          + "。保護中・お気に入り・「残す」の録画は含めません")
            extra = sum(ctx.rows[i]["size"] for h, ids in ctx.dup_groups.items()
                        if ctx.rows[ids[0]]["kind"] == "video" for i in ids if i != ctx.dup_keep[h][0])
            groups = [ids for ids in ctx.dup_groups.values() if ctx.rows[ids[0]]["kind"] == "video"]
            if scanning:
                phase = self.state.last_progress.get("text") or "録画を探しています"
                self.saving.set("算出中", f"いまの処理: {phase}。終わると重複分を表示します。")
            else:
                self.saving.set(format_size(extra), f"{len(groups):,} グループ。各グループで1本を残した場合の重複分です"
                                                    "（ごみ箱を空にした後に空きます）。")
            self.images.set(f"{len(images):,} 件", f"{format_size(sum(r['size'] for r in images))}。"
                                                   "画像の整理は「画像の整理（補助）」で行えます。")
        self._fill_recommendations()
        self.refresh_status()

    def _fill_recommendations(self) -> None:
        ctx = self.state.ctx
        while self.rec_rows.count():
            item = self.rec_rows.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
        recs = []
        for h, ids in ctx.dup_groups.items():
            if ctx.rows[ids[0]]["kind"] != "video":
                continue
            keep = ctx.dup_keep[h][0]
            recs.append(("duplicate", h, ids, keep, sum(ctx.rows[i]["size"] for i in ids if i != keep),
                         f"完全重複 {len(ids)} 本（同じ録画が残ります）"))
        for n, ids in enumerate(ctx.video_groups):
            keep = ctx.vgroup_keep[n][0] or max(ids, key=lambda i: ctx.rows[i]["size"])
            recs.append(("similar", n, ids, keep, sum(ctx.rows[i]["size"] for i in ids if i != keep),
                         f"類似録画 {len(ids)} 本（解像度・圧縮違いの可能性。比較が必要）"))
        recs.sort(key=lambda r: -r[4])
        # 重複・類似以外で、客観的な問題（短い・黒画面・読み込み失敗・無音）がある録画
        flagged = [r for r in ctx.rows.values() if r["kind"] == "video" and candidates.failures(r)
                   and not (r["protected"] or r["favorite"] or r["decision"] in ("残す", "保留", "削除候補"))]
        if flagged:
            biggest = max(flagged, key=lambda r: r["size"])
            recs.append(("review", None, [r["id"] for r in flagged], biggest["id"], sum(r["size"] for r in flagged),
                         f"確認が必要な録画 {len(flagged)} 本（極端に短い・黒画面が多い・読み込み失敗など）"))
        if not recs:
            text = ("PCを調べると、重複・類似する録画をここに表示します。" if not ctx.rows else
                    "重複・類似する録画は見つかりませんでした。目標容量を指定すると、大きい録画などの要確認の候補を探せます。")
            self.rec_rows.addWidget(label(text, "sub", wrap=True))
            return
        for kind, key, ids, keep, extra, title in recs[:3]:
            row = QFrame()
            h = QHBoxLayout(row)
            h.setContentsMargins(0, 0, 0, 0)
            thumb = QLabel()
            thumb.setFixedSize(112, 63)
            thumb.setAlignment(Qt.AlignmentFlag.AlignCenter)
            thumb.setStyleSheet("background: #1f1f1f; color: #f0f0f0; border-radius: 6px;")
            pm = self.state.loader.pixmap(row_dict(ctx.rows[keep], ctx))
            if pm is not None and not pm.isNull():
                thumb.setPixmap(pm.scaled(112, 63, Qt.AspectRatioMode.KeepAspectRatio,
                                          Qt.TransformationMode.SmoothTransformation))
            else:
                thumb.setText("代表画像\n準備中")
            h.addWidget(thumb)
            text = QVBoxLayout()
            name = ctx.rows[keep]["name"]
            text.addWidget(label(row.fontMetrics().elidedText(name, Qt.TextElideMode.ElideMiddle, 420), "subtitle"))
            if kind == "review":
                text.addWidget(label(f"{title}・合計 {format_size(extra)}。プレビューで内容を確認してから判断してください",
                                     "sub", wrap=True))
            else:
                text.addWidget(label(f"{title}・整理できる見込み {format_size(extra)}", "sub", wrap=True))
            h.addLayout(text, 1)
            if kind == "review":
                go = button("内容を確認する", "primary", "search")
                go.clicked.connect(lambda: self.start_plan.emit(0.0, None))
            else:
                go = button("比較して選ぶ", "primary", "copy")
                go.clicked.connect(partial(self.open_group.emit, kind, key))
            h.addWidget(go)
            self.rec_rows.addWidget(row)
        if len(recs) > 3:
            more = button(f"ほか {len(recs) - 3} グループを候補で見る", "link")
            more.clicked.connect(lambda: self.start_plan.emit(0.0, None))
            self.rec_rows.addWidget(more, 0, Qt.AlignmentFlag.AlignLeft)


# =============================================================================
# ステータス欄（調査の状態）
# =============================================================================
class ScanStatus(QWidget):
    capacity_requested = Signal()

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
        self.summary.clicked.connect(self.capacity_requested.emit)
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
        v.addWidget(label("PC内の録画を自動で探します。チェックを外した場所は調べません。"
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


PLAN_THUMB_W, PLAN_THUMB_H = 96, 54


def _framed_thumb(pm: QPixmap | None, row, state: str = "pending") -> QPixmap:
    """サムネイルを同じ大きさの暗い枠に収める（縦長などは余白を付ける）。なければ状態を文字で示す。"""
    canvas = QPixmap(PLAN_THUMB_W, PLAN_THUMB_H)
    canvas.fill(QColor("#1f1f1f"))
    painter = QPainter(canvas)
    if pm is not None and not pm.isNull():
        fit = pm.scaled(PLAN_THUMB_W, PLAN_THUMB_H, Qt.AspectRatioMode.KeepAspectRatio,
                        Qt.TransformationMode.SmoothTransformation)
        painter.drawPixmap((PLAN_THUMB_W - fit.width()) // 2, (PLAN_THUMB_H - fit.height()) // 2, fit)
    else:
        painter.setPen(QColor("#d0d0d0"))
        f = painter.font()
        f.setPointSizeF(max(f.pointSizeF() * 0.8, 7))
        painter.setFont(f)
        text = {"cloud": "オンライン専用", "failed": "サムネイルなし"}.get(state, "準備中")
        painter.drawText(canvas.rect(), Qt.AlignmentFlag.AlignCenter, text)
    painter.end()
    return canvas


class DataProblemBanner(QFrame):
    """保存データの一部を読み込めなかったことを知らせる（保護情報が読めないときは整理操作を止めている）。"""

    open_settings = Signal()

    def __init__(self, db) -> None:
        super().__init__()
        self.setObjectName("SelectionBar")
        self.db = db
        h = QHBoxLayout(self)
        h.setContentsMargins(theme.S1, 4, theme.S1, 4)
        ic = QLabel()
        ic.setPixmap(theme.pixmap("warning", theme.WARN, 16))
        h.addWidget(ic)
        self.text = label("", None, wrap=True)
        h.addWidget(self.text, 1)
        b = button("設定を開く", None, "settings")
        b.clicked.connect(self.open_settings.emit)
        h.addWidget(b)
        self.update_state()

    def update_state(self) -> None:
        problems = self.db.load_problems
        if not problems:
            self.hide()
            return
        head = ("保護・判断の記録を読み込めなかったため、ごみ箱への移動などの整理操作を止めています。調査と確認はできます。"
                if not self.db.protection_ok else "保存データの一部を読み込めませんでした（復旧済み、または作り直します）。")
        self.text.setText(head + " " + " / ".join(problems[:2]) + (f" ほか {len(problems) - 2} 件" if len(problems) > 2 else ""))
        self.setToolTip("\n".join(problems))
        self.show()


class PlanPage(QWidget):
    """候補を確認: 重複・類似録画をグループ単位で示す。目標容量（追加で空けたい容量）は任意。"""

    request_locations = Signal()
    request_settings = Signal()
    compare_requested = Signal(str, object)
    final_requested = Signal(list)

    def __init__(self, state: MediaState, parent=None) -> None:
        super().__init__(parent)
        self.setObjectName("Page")
        self.state = state
        self.plan: dict | None = None
        self._checks: dict[int, bool] = {}
        v = QVBoxLayout(self)
        v.setContentsMargins(theme.S3, theme.S2, theme.S3, theme.S1)
        v.setSpacing(theme.S1)
        v.addWidget(StepBar(0))
        v.addWidget(label("整理のおすすめ", "title"))
        self.help = label("完全一致の重複・類似録画・確認が必要な録画（極端に短い・黒画面が多い・読み込み失敗・無音）を、"
                          "理由付きで示します（決まったルールによる提案で、AI の判定ではありません）。"
                          "チェックした録画を「削除候補に追加」すると、「削除候補」画面で最終確認できます。"
                          "保護中・お気に入り・「残す」の録画は含めません。", "sub", wrap=True)
        v.addWidget(self.help)
        self.banner = FFmpegBanner(state.tools_fn)
        self.banner.open_settings.connect(self.request_settings.emit)
        v.addWidget(self.banner)
        self.data_banner = DataProblemBanner(state.db)
        self.data_banner.open_settings.connect(self.request_settings.emit)
        v.addWidget(self.data_banner)
        row = QHBoxLayout()
        row.addWidget(label("追加で空けたい容量（任意）"))
        self.target = QDoubleSpinBox()
        self.target.setRange(0, 100000)
        self.target.setDecimals(1)
        self.target.setSpecialValueText("指定なし")
        self.target.setValue(0)
        self.target.setSuffix(" GB")
        self.target.setMinimumWidth(120)
        self.target.setToolTip("「今より○GB 空けたい」という目標です（「空き容量を○GBにする」ではありません）")
        self.drive_combo = QComboBox()
        self.drive_combo.setToolTip("容量を空けたいドライブ。別のドライブの録画は、このドライブの空き容量を増やさないため候補にしません")
        self.propose_button = button("提案する", "primary", "target")
        row.addWidget(self.target)
        row.addWidget(label("ドライブ"))
        row.addWidget(self.drive_combo)
        row.addWidget(self.propose_button)
        row.addStretch()
        v.addLayout(row)
        self.cards_grid = QGridLayout()
        self.cards_grid.setSpacing(theme.S1)
        self.status_card = StatCard("目標と選択中", "target")
        self.total_card = StatCard("提案全体（ファイルサイズ）", "flag")
        self.free_card = StatCard("空き容量（実際）", "drive")
        self._card_cols = 0
        self._layout_cards(3)
        v.addLayout(self.cards_grid)
        self.note = label("容量はファイルサイズに基づく見込みです。ごみ箱へ移しただけでは空き容量は増えず、"
                          "ごみ箱を空にすると増えます。", "caption", wrap=True)
        v.addWidget(self.note)
        self.tree = QTreeWidget()
        self.tree.setHeaderLabels(["録画", "容量", "理由", "保存場所"])
        self.tree.setColumnWidth(0, 300)
        self.tree.setColumnWidth(1, 90)
        self.tree.setColumnWidth(2, 380)
        self.tree.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        # 一覧を出した時点でサムネイルが見えるようにする（行ごとに高さが違うので、高さの統一はしない）
        self.tree.setIconSize(QSize(PLAN_THUMB_W, PLAN_THUMB_H))
        self.tree.setUniformRowHeights(False)
        self.tree.setTextElideMode(Qt.TextElideMode.ElideRight)  # 理由の全文は詳細とツールチップで確認できる
        self._thumb_items: dict[int, list[QTreeWidgetItem]] = {}
        state.loader.updated.connect(self._on_thumb)
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
        self.compare_button = button("比較して選ぶ", "primary", "copy", "グループの録画を並べて比較し、残す1本を選びます")
        self.keep_button = button("残す（候補から外す）", None, "check", "この録画を整理しないと記録し、候補から外します")
        self.protect_button = button("保護する", None, "shield", "残す録画を保護します。以後の提案・ごみ箱への移動から除外します")
        self.final_button = button("削除候補に追加", "primary", "flag",
                                   "チェックした録画を「削除候補」にします。まだ削除・移動はしません。"
                                   "「削除候補」画面の最終確認で、ごみ箱へ移すかを決めます")
        bar.addWidget(self.selected_label, 1)
        for b in (self.compare_button, self.keep_button, self.protect_button, self.final_button):
            bar.addWidget(b)
        v.addLayout(bar)

        self.propose_button.clicked.connect(self.propose)
        self.target.editingFinished.connect(self.propose)
        self.drive_combo.activated.connect(lambda _: self.propose())
        self.tree.itemChanged.connect(self._on_check)
        self.tree.currentItemChanged.connect(self._on_current)
        self.tree.itemDoubleClicked.connect(lambda *_: self._compare_current())
        self.detail.closed.connect(self._close_detail)
        self.protect_button.clicked.connect(lambda: self._decide_current("保護する"))
        self.keep_button.clicked.connect(lambda: self._decide_current("残す"))
        self.compare_button.clicked.connect(self._compare_current)
        self.final_button.clicked.connect(self.final_confirm)
        state.changed.connect(self.replan)
        state.scan_progress.connect(lambda _: self.plan is not None and self._update_summary())
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
    def set_target_gb(self, gb: float, drive: str | None = None) -> None:
        if drive is not None:
            self._wanted_drive = drive
        self.target.setValue(gb)
        self.propose()

    def propose(self) -> None:
        self.replan()

    def target_bytes(self) -> int | None:
        return int(self.target.value() * 1024 ** 3) if self.target.value() > 0 else None

    def replan(self) -> None:
        self.banner.update_state()
        ctx = self.state.ctx
        before = {it["id"]: it for it in (self.plan or {}).get("items", [])}
        prev = self._checks
        locations = [x["path"] for x in self.state.db.locations()]
        drives = candidates.media_drives(ctx, locations)
        current = getattr(self, "_wanted_drive", None) or self.drive_combo.currentData()
        self._wanted_drive = None
        self.drive_combo.blockSignals(True)
        self.drive_combo.clear()
        for d in drives:
            self.drive_combo.addItem(d, d)
        if current in drives:
            self.drive_combo.setCurrentIndex(drives.index(current))
        self.drive_combo.blockSignals(False)
        self.drive_combo.setEnabled(len(drives) > 1)
        self.plan = candidates.plan_recordings(ctx, self.target_bytes(), drive=self.drive_combo.currentData())
        self._checks = {}
        for it in self.plan["items"]:
            row = ctx.rows[it["id"]]
            old = before.get(it["id"])
            if it["kind"] == "similar" and not it.get("confirmed"):
                self._checks[it["id"]] = False  # 残す1本を確認するまで選べない
            elif row["decision"] == "削除候補":
                self._checks[it["id"]] = True   # 比較画面などで削除候補にしたもの
            elif it["id"] in prev and old is not None and old["kind"] == it["kind"]:
                # 同じ種類の候補のときだけ、利用者のチェックを引き継ぐ（要確認 → 重複 に変わったら選び直す）
                self._checks[it["id"]] = prev[it["id"]]
            else:  # 重複は提案どおり、要確認・類似は内容を見てから選ぶ
                self._checks[it["id"]] = it["kind"] == "duplicate" or (it["kind"] == "similar" and it.get("confirmed"))
        self._fill_tree()
        self._update_summary()

    def _fill_tree(self) -> None:
        ctx = self.state.ctx
        current = self._current_id()
        found = False
        self.tree.blockSignals(True)
        self.tree.clear()
        self._thumb_items = {}
        groups: dict[tuple, list[dict]] = {}
        for it in self.plan["items"]:
            if it["kind"] == "duplicate":
                key = ("duplicate", ctx.dup_of.get(it["id"]))
            elif it["kind"] == "similar":
                key = ("similar", it["group"])
            else:
                key = ("review", None)
            groups.setdefault(key, []).append(it)
        bold = None
        for (kind, gkey), items in sorted(groups.items(), key=lambda kv: ("duplicate", "similar", "review").index(kv[0][0])):
            size = sum(i["size"] for i in items)
            if kind == "review":
                title = f"確認が必要な録画（プレビューで内容を確認してから選んでください）　{len(items)} 本・{format_size(size)}"
                reason = ("重複・類似ではありませんが、客観的な条件（極端に短い・黒画面が多い・読み込み失敗・無音"
                          + ("・500MB以上・1年以上更新なし" if self.target_bytes() else "") + "）に当てはまります。"
                          "不要と判断したものではありません")
            else:
                keep = ctx.rows[items[0]["keep_id"]]
                head = "完全重複（同じ録画が残ります）" if kind == "duplicate" else "類似録画（比較が必要）"
                title = f"{head}・{keep['name']} ほか {len(items)} 本・整理できる見込み {format_size(size)}"
                reason = items[0]["reasons"][0]
            top = QTreeWidgetItem([title, format_size(size), reason, ""])
            top.setData(0, Qt.ItemDataRole.UserRole + 1, (kind, gkey))
            bold = bold or top.font(0)
            f = top.font(0)
            f.setBold(True)
            top.setFont(0, f)
            top.setToolTip(2, reason)
            top.setFlags(Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable)
            self.tree.addTopLevelItem(top)
            top.setFirstColumnSpanned(True)  # 見出しは全列を使って切れないようにする（件数・容量も見出しに含む）
            if kind != "review":
                keep = ctx.rows[items[0]["keep_id"]]
                confirmed = kind == "duplicate" or items[0].get("confirmed")
                mark = "【残す】" if confirmed else "【残すおすすめ・未確定】"
                krow = QTreeWidgetItem([mark + keep["name"], format_size(keep["size"]),
                                        "この1本を残します" if confirmed else "比較して残す1本を確定してください",
                                        keep["folder"]])
                krow.setData(0, Qt.ItemDataRole.UserRole + 2, keep["id"])
                krow.setFlags(Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable)
                self._set_thumb(krow, keep)
                top.addChild(krow)
            for it in items:
                r = ctx.rows[it["id"]]
                child = QTreeWidgetItem([r["name"], format_size(it["size"]), " ／ ".join(it["reasons"]), r["folder"]])
                child.setData(0, Qt.ItemDataRole.UserRole, it["id"])
                flags = Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable
                if not (it["kind"] == "similar" and not it.get("confirmed")):
                    flags |= Qt.ItemFlag.ItemIsUserCheckable
                child.setFlags(flags)
                child.setCheckState(0, Qt.CheckState.Checked if self._checks.get(it["id"]) else Qt.CheckState.Unchecked)
                child.setToolTip(0, r["path"])
                child.setToolTip(2, "\n".join(it["reasons"]))
                self._set_thumb(child, r)
                top.addChild(child)
                if it["id"] == current:
                    self.tree.setCurrentItem(child)
                    found = True
            top.setExpanded(True)
        self.tree.blockSignals(False)
        if current is not None and not found:  # 選んでいた録画が候補から外れたら（保護・残す・整理済み）詳細を閉じる
            self.detail.player.stop()
            self.detail.hide()
        self._show_content()

    def _set_thumb(self, item: QTreeWidgetItem, row) -> None:
        """行にサムネイルを付ける。まだなければ「準備中」を置き、できしだい差し替える。"""
        self._thumb_items.setdefault(row["id"], []).append(item)
        rd = row_dict(row, self.state.ctx)
        pm = self.state.loader.pixmap(rd)
        item.setIcon(0, QIcon(_framed_thumb(pm, row, self.state.loader.state(rd))))
        item.setData(0, Qt.ItemDataRole.UserRole + 3, self.state.loader.state(rd))

    def _on_thumb(self, media_id: int) -> None:
        items = self._thumb_items.get(media_id)
        row = self.state.ctx.rows.get(media_id)
        if not items or row is None:
            return
        rd = row_dict(row, self.state.ctx)
        pm = self.state.loader.pixmap(rd)
        st = self.state.loader.state(rd)
        icon = QIcon(_framed_thumb(pm, row, st))
        for item in items:
            try:
                item.setIcon(0, icon)
                item.setData(0, Qt.ItemDataRole.UserRole + 3, st)
            except RuntimeError:
                pass  # 一覧を作り直した後に届いた通知は無視する

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
                self.empty.show_state("folder", "まだ調査していません", "PCを調べると、重複・類似録画などの整理候補を提案します。",
                                      "PCを調べる", self.request_locations.emit)
            return
        extra = "（目標を指定すると、容量が大きい録画なども「要確認」として探します）" if self.target_bytes() is None else ""
        self.empty.show_state("check", "整理候補はありません",
                              "完全一致の重複・類似録画・確認が必要な録画は見つかりませんでした。"
                              "保護中・お気に入り・「残す」・保留にした録画は対象外です。" + extra)

    def _update_summary(self) -> None:
        p = self.plan
        has = self.state.has_data()
        if not has:
            self.status_card.set("未調査", "調査すると提案できます。")
            self.total_card.set("-", "")
        else:
            self._update_status()
            ex = "・".join(f"{k} {v} 件" for k, v in p["excluded"].items() if v)
            self.total_card.set(format_size(p["total"]),
                                f"重複 {format_size(p['duplicate_total'])}・類似 {format_size(p.get('similar_total', 0))}・"
                                f"要確認 {format_size(p['review_total'])}" + (f"。対象外: {ex}" if ex else "")
                                + "。ファイルサイズ（論理サイズ）の合計で、実際のディスク占有量は取得していません")
        drive = p.get("drive") if p else None
        paths = ([drive] if drive else []) + [x["path"] for x in self.state.db.locations()] or [
            os.environ.get("SystemDrive", "C:") + "\\" if os.name == "nt" else "/"]
        spaces = candidates.drive_space(paths)
        if spaces:
            first = spaces[0]
            self.free_card.set(format_size(first["free"]), f"{first['root']} の空き（全体 {format_size(first['total'])}）。"
                                                         "提案の合計とは別の、今の実際の値です。")
        else:
            self.free_card.set("取得できません")
        self._update_selected()

    def checked_ids(self) -> list[int]:
        return [mid for mid, on in self._checks.items() if on]

    def _update_status(self) -> None:
        """目標・提案全体・選択中・目標までの不足を区別して示す。達成状況は「いまチェックしている録画」で判断する。"""
        p = self.plan
        ctx = self.state.ctx
        selected = sum(ctx.rows[i]["size"] for i in set(self.checked_ids()) if i in ctx.rows)
        target = p["target"]
        notes = []
        if self.state.runner.is_running():
            notes.append("調査中のため、候補が増える可能性があります。")
        if p["unchecked"]:
            notes.append(f"重複の確認が終わっていない録画が {p['unchecked']:,} 本あります（「再スキャン」で確認できます）。")
        pending = sum(1 for it in p["items"] if it["kind"] == "similar" and not it.get("confirmed"))
        if pending:
            notes.append(f"類似録画 {pending} 本は、比較して残す1本を確定すると選べます。")
        if target is None:
            self.status_card.set(f"選択中 {format_size(selected)}",
                                 f"目標（追加で空ける容量）: 指定なし。提案全体は {format_size(p['total'])}。" + "".join(notes))
            return
        if p["achieved"]:
            notes.insert(0, f"提案をすべて選ぶと {format_size(p['total'])} で目標に届きます。")
        else:
            notes.insert(0, f"提案をすべて選んでも {format_size(p['shortage'])} 不足します"
                            "（条件に合う候補が足りないため、ほかの録画は追加していません）。")
        head = (f"目標（追加で空ける）{format_size(target)}・選択中 {format_size(selected)}・"
                + ("不足なし。" if selected >= target else f"目標までの不足 {format_size(target - selected)}。"))
        self.status_card.set("目標に届きます" if selected >= target else f"不足 {format_size(target - selected)}",
                             head + "".join(notes))

    def _update_selected(self) -> None:
        ids = self.checked_ids()
        ctx = self.state.ctx
        total = sum(ctx.rows[i]["size"] for i in ids if i in ctx.rows)
        target = self.plan["target"] if self.plan else None
        rest = max(target - total, 0) if target else 0
        self.data_banner.update_state()
        self.selected_label.setText(f"選択中 {len(ids):,} 本・{format_size(total)}"
                                    + ("" if target is None else (f"（目標まであと {format_size(rest)}）" if rest
                                                                  else "（目標に届きます）")))
        self.final_button.setEnabled(bool(ids) and self.state.db.protection_ok)  # 保護情報が読めないときは止める
        mid = self._current_id()
        editable = self.state.db.protection_ok  # 保護・判断の記録が復旧待ちのときは変更できない
        self.protect_button.setEnabled(bool(mid) and editable)
        self.keep_button.setEnabled(bool(mid) and editable)
        self.compare_button.setEnabled(self._current_group() is not None)

    def _on_check(self, item, column) -> None:
        mid = item.data(0, Qt.ItemDataRole.UserRole)
        if mid is not None:
            self._checks[mid] = item.checkState(0) == Qt.CheckState.Checked
            self._update_selected()
            self._update_status()  # チェックの変更に追従する

    def _current_id(self) -> int | None:
        item = self.tree.currentItem()
        if not item:
            return None
        return item.data(0, Qt.ItemDataRole.UserRole) or item.data(0, Qt.ItemDataRole.UserRole + 2)

    def _current_group(self) -> tuple | None:
        item = self.tree.currentItem()
        while item is not None:
            g = item.data(0, Qt.ItemDataRole.UserRole + 1)
            if g is not None:
                return g if g[0] in ("duplicate", "similar") else None
            item = item.parent()
        return None

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
        g = self._current_group()
        if g is not None:
            self.compare_requested.emit(g[0], g[1])

    def persist_checks(self) -> bool:
        """チェックを「削除候補」として保存する（起動し直しても残る）。外したものは削除候補から戻す。

        まとめて保存する（件数にかかわらず書き込みは最大2回）。保存できなかったときは False。
        """
        on_ids, off_ids = [], []
        for mid, on in self._checks.items():
            row = self.state.db.video(mid)
            if row is None:
                continue
            if on and row["decision"] != "削除候補":
                on_ids.append(mid)
            elif not on and row["decision"] == "削除候補":
                off_ids.append(mid)
        try:
            if on_ids:
                self.state.db.set_decisions(on_ids, "削除候補")  # 保護中は記録しない（戻り値の ID）
            if off_ids:
                self.state.db.set_decisions(off_ids, None)
        except StoreError as e:
            QMessageBox.warning(self, "保存できませんでした", str(e))
            return False
        return True

    def final_confirm(self) -> None:
        ids = self.checked_ids()
        if ids:
            self.final_requested.emit(ids)

    def resizeEvent(self, event) -> None:
        """画面が低いときは補足を隠して一覧に場所を譲る（補足はツールチップで確認できる）。"""
        super().resizeEvent(event)
        self._layout_cards(3 if self.width() >= 700 else 1)
        compact = self.height() < 620
        self.help.setVisible(not compact)
        self.note.setVisible(not compact)
        self.detail.player.setMinimumHeight(160 if compact else 220)  # 狭い画面でも再生の操作まで届くように
        # 低い画面ではサムネイルを小さくして、一覧に入る行数を増やす
        self.tree.setIconSize(QSize(64, 36) if compact else QSize(PLAN_THUMB_W, PLAN_THUMB_H))
        for card in (self.status_card, self.total_card, self.free_card):
            card.detail.setVisible(not compact)
            card.setToolTip(card.detail.text() if compact else "")
        self.selected_label.setWordWrap(compact)

    def _layout_cards(self, cols: int) -> None:
        if cols == self._card_cols:
            return
        self._card_cols = cols
        cards = (self.status_card, self.total_card, self.free_card)
        for c in cards:
            self.cards_grid.removeWidget(c)
        for i, c in enumerate(cards):
            self.cards_grid.addWidget(c, i // cols, i % cols)
        for c in range(3):
            self.cards_grid.setColumnStretch(c, 1 if c < cols else 0)

    def stop_media(self) -> None:
        self.detail.player.stop()


def similar_n() -> int:
    from . import similar
    return similar.N_POINTS


class StepBar(QWidget):
    """「候補を確認 → 比較して選ぶ → 最終確認」の進行状態（文字とアイコンで示し、色だけに頼らない）。"""

    STEPS = ("おすすめを見る", "残すものを選ぶ", "削除候補で最終確認")

    def __init__(self, current: int) -> None:
        super().__init__()
        h = QHBoxLayout(self)
        h.setContentsMargins(0, 0, 0, 0)
        h.setSpacing(theme.S1)
        for i, name in enumerate(self.STEPS):
            if i:
                arrow = QLabel("→")
                theme.set_role(arrow, "sub")
                h.addWidget(arrow)
            ic = QLabel()
            done, now = i < current, i == current
            ic.setPixmap(theme.pixmap("check" if done else ("target" if now else "info"),
                                      theme.ACCENT if now else theme.TEXT_SUB, 14))
            text = QLabel(f"{i + 1}. {name}" + ("（いまここ）" if now else ""))
            theme.set_role(text, "subtitle" if now else "sub")
            h.addWidget(ic)
            h.addWidget(text)
        h.addStretch()


class PlayerDialog(QDialog):
    """1本ずつ再生して確認する（比較中もファイルは変更・移動しない）。"""

    def __init__(self, path: str, title: str, poster: str | None = None, parent=None) -> None:
        super().__init__(parent)
        from .player import VideoPlayer
        self.setWindowTitle(title)
        self.resize(720, 480)
        v = QVBoxLayout(self)
        self.player = VideoPlayer()
        v.addWidget(self.player, 1)
        bb = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        bb.rejected.connect(self.reject)
        v.addWidget(bb)
        self.player.set_source(path, poster)

    def done(self, r) -> None:
        self.player.stop()
        super().done(r)


class _FramesJob(QRunnable):
    def __init__(self, page, media_id, path, duration, prefix, tools, fractions):
        super().__init__()
        self.page, self.media_id, self.path, self.duration, self.prefix, self.tools = \
            page, media_id, path, duration, prefix, tools
        self.fractions = fractions

    def run(self):
        try:
            paths = media.compare_frames(self.tools, self.path, self.duration, self.prefix, fractions=self.fractions)
        except Exception:
            paths = []
        self.page.frames_ready.emit(self.media_id, paths)


class ComparePage(QWidget):
    """比較して選ぶ: 録画を横に並べ、同じ相対時点の場面・長さ・解像度・容量を比べて残す1本を選ぶ。"""

    frames_ready = Signal(int, list)
    back = Signal()
    to_final = Signal()

    def __init__(self, state: MediaState, parent=None) -> None:
        super().__init__(parent)
        self.setObjectName("Page")
        self.state = state
        self.group: tuple | None = None
        self.pool = QThreadPool()
        self.pool.setMaxThreadCount(2)
        self.frame_labels: dict[int, list[QLabel]] = {}
        v = QVBoxLayout(self)
        v.setContentsMargins(theme.S3, theme.S2, theme.S3, theme.S1)
        v.setSpacing(theme.S1)
        v.addWidget(StepBar(1))
        self.title = label("比較して選ぶ", "title")
        self.reason = label("", "sub", wrap=True)
        v.addWidget(self.title)
        v.addWidget(self.reason)
        pick = QHBoxLayout()
        self.pick_label = label("比較する2本:", "sub")
        self.left_combo, self.right_combo = QComboBox(), QComboBox()
        for c in (self.left_combo, self.right_combo):
            c.setMinimumWidth(220)
            c.activated.connect(self._pick_changed)
        pick.addWidget(self.pick_label)
        pick.addWidget(self.left_combo)
        pick.addWidget(label("と", "sub"))
        pick.addWidget(self.right_combo)
        pick.addStretch()
        self.pick_row = QWidget()
        self.pick_row.setLayout(pick)
        v.addWidget(self.pick_row)
        self.result_bar = QFrame()
        self.result_bar.setObjectName("SelectionBar")
        rb = QHBoxLayout(self.result_bar)
        rb.setContentsMargins(theme.S1, 4, theme.S1, 4)
        self.result_text = label("", None, wrap=True)
        self.undo_button = button("取り消す", None, "undo", "直前の判断を取り消して、選び直せるようにします")
        self.undo_button.clicked.connect(self.undo)
        rb.addWidget(self.result_text, 1)
        rb.addWidget(self.undo_button)
        self.result_bar.hide()
        v.addWidget(self.result_bar)
        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        v.addWidget(self.scroll, 1)
        bar = QHBoxLayout()
        self.back_button = button("おすすめへ戻る", None, "undo")
        self.keep_all = button("両方残す", None, "check", "このグループの録画をすべて残し、候補から外します")
        self.keep_pair = button("比較中の2本を残す", None, "check", "いま並べている2本を残します（ほかの録画はそのまま）")
        self.hold = button("保留", None, "pause", "このグループの判断をあとにします。候補からいったん外れます")
        self.next_button = button("最終確認へ", "primary", "flag")
        bar.addWidget(self.back_button)
        bar.addStretch()
        for b in (self.keep_pair, self.keep_all, self.hold, self.next_button):
            bar.addWidget(b)
        v.addLayout(bar)
        v.addWidget(label("比較中はファイルを変更・移動しません。ごみ箱へ移すのは、最終確認で確定したときだけです。",
                          "caption", wrap=True))
        self.back_button.clicked.connect(self.back.emit)
        self.next_button.clicked.connect(self.to_final.emit)
        self.keep_all.clicked.connect(lambda: self._decide_all("残す"))
        self.keep_pair.clicked.connect(self._keep_pair)
        self.hold.clicked.connect(lambda: self._decide_all("保留"))
        self.pair: list[int] = []
        self._undo: dict[int, str | None] | None = None
        self.frames_ready.connect(self._on_frames)

    def members(self) -> list[int]:
        ctx = self.state.ctx
        if not self.group:
            return []
        kind, key = self.group
        ids = ctx.dup_groups.get(key, []) if kind == "duplicate" else \
            (ctx.video_groups[key] if key < len(ctx.video_groups) else [])
        return [i for i in ids if i in ctx.rows]

    def show_group(self, kind: str, key) -> None:
        self.group = (kind, key)
        self.pair = []
        self.result_bar.hide()
        self._undo = None
        self.refresh()

    def refresh(self) -> None:
        ctx = self.state.ctx
        ids = self.members()
        if not ids:
            box = EmptyState()
            box.show_state("check", "このグループはもうありません", "整理済みか、ファイルが見つからなくなりました。",
                           "候補へ戻る", self.back.emit)
            self.scroll.setWidget(box)
            return
        kind, key = self.group
        if kind == "duplicate":
            keep_id, why = ctx.dup_keep[key]
            self.title.setText(f"完全重複を比較（{len(ids)} 本）")
            self.reason.setText(f"内容が完全に一致します（SHA-256 で確認）。1本は必ず残します。"
                                f"残すおすすめ: {ctx.rows[keep_id]['name']}（{why}）")
        else:
            keep_id, why = ctx.vgroup_keep[key]
            confirmed = candidates.similar_keep_confirmed(ctx, key)
            self.title.setText(f"類似録画を比較（{len(ids)} 本）")
            text = ("長さがほぼ同じで、代表場面の大半が一致する録画です。同じ内容とは限りません。"
                    "場面を見比べ、必要なら再生して確認してください。")
            if confirmed:
                text += f" 残す1本: {ctx.rows[confirmed]['name']}（あなたが選びました）"
            elif keep_id:
                text += f" 残すおすすめ: {ctx.rows[keep_id]['name']}（{why}）"
            else:
                text += f" {why}"
            self.reason.setText(text)
        # 表示する場面: 類似は「一致しない → 比較できない → 一致」の順に4つ。完全一致は 20/40/60/80%
        if kind == "similar":
            det = ctx.vgroup_detail[key]
            order = ([(i, "一致しない場面") for i in det["unmatched"]] + [(i, "比較できない場面") for i in det["uncomparable"]]
                     + [(i, "一致した場面") for i in det["matched"]])
            self.points = [((i + 0.5) / similar_n(), tag) for i, tag in order[:4]] or \
                [(f, "場面") for f in media.COMPARE_POINTS]
            self.points.sort(key=lambda p: ({"一致しない場面": 0, "比較できない場面": 1}.get(p[1], 2), p[0]))
            warn = (f"⚠ 一致しない場面が {len(det['unmatched'])} あります。内容が違う可能性があります。"
                    "再生して確認してから選んでください。" if det["unmatched"] else "")
            self.reason.setText(("" if not warn else warn + "\n") + self.reason.text()
                                + f"\n{det['text']}。")
        else:
            self.points = [(f, "完全一致") for f in media.COMPARE_POINTS]
        box = QWidget()
        h = QHBoxLayout(box)
        h.setSpacing(theme.S1)
        self.frame_labels = {}
        ids = sorted(ids, key=lambda m: (m != keep_id, -candidates.pixels(ctx.rows[m])))
        self.all_ids = ids
        if len(self.pair) != 2 or any(m not in ids for m in self.pair) or self.pair[0] == self.pair[1]:
            self.pair = ids[:2]
        many = len(ids) > 2
        self.pick_row.setVisible(many)
        if many:  # 3本以上: どの2本を比べているかを取り違えないよう、番号付きで選ぶ
            for c, chosen in ((self.left_combo, self.pair[0]), (self.right_combo, self.pair[1])):
                c.blockSignals(True)
                c.clear()
                for n, m in enumerate(ids, 1):
                    c.addItem(f"{n}. {ctx.rows[m]['name']}", m)
                c.setCurrentIndex(ids.index(chosen))
                c.blockSignals(False)
            self.pick_label.setText(f"このグループの {len(ids)} 本から、比較する2本:")
        self.keep_all.setText("すべて残す" if many else "両方残す")
        self.keep_all.setToolTip(f"このグループの {len(ids)} 本をすべて残します")
        self.keep_pair.setVisible(many)
        durs = {m: candidates.duration(ctx.rows[m]) for m in self.pair}
        if all(durs.values()) and abs(durs[self.pair[0]] - durs[self.pair[1]]) > 1.0:
            self.reason.setText(f"⚠ 長さが違います（{format_duration_short(durs[self.pair[0]])} と "
                                f"{format_duration_short(durs[self.pair[1]])}）。\n" + self.reason.text())
        for mid in self.pair:
            h.addWidget(self._column(ctx.rows[mid], mid == keep_id))
        h.addStretch()
        self.scroll.setWidget(box)
        editable = self.state.db.protection_ok
        self.keep_all.setEnabled(editable)
        self.keep_pair.setEnabled(editable)
        self.hold.setEnabled(editable)
        for b in box.findChildren(QPushButton):
            if b.text() in ("これを残す", "保護する（次回以降も守る）", "保護を解除"):
                b.setEnabled(editable)

    def _column(self, r, recommended: bool) -> QWidget:
        col = QFrame()
        col.setObjectName("Card")
        col.setFixedWidth(300)
        v = QVBoxLayout(col)
        v.setContentsMargins(theme.S1 + 4, theme.S1 + 4, theme.S1 + 4, theme.S1 + 4)
        v.setSpacing(6)
        fm = col.fontMetrics()
        name = label(r["name"], "subtitle")
        big = QFont(name.font())
        big.setPointSizeF(max(big.pointSizeF(), 10) * 1.25)  # 見出しの文字の大きさで省略位置を決める
        name.setText(QFontMetrics(big).elidedText(r["name"], Qt.TextElideMode.ElideMiddle, 260))
        name.setToolTip(r["path"])
        v.addWidget(name)
        badges = []
        if recommended:
            badges.append("【残すおすすめ】")
        if r["protected"]:
            badges.append("【保護中】")
        if r["decision"]:
            badges.append(f"【{r['decision']}】")
        v.addWidget(label(" ".join(badges) or "　", None))
        # 操作は上に置く（場面を見るためにスクロールしても、まず操作が見える）
        row = QHBoxLayout()
        keep = button("これを残す", "primary", "check", "この1本を残し、ほかを削除候補にします（まだ移動しません）")
        scope = self._keep_scope_text(r["id"])
        play = button("再生", None, "play")
        keep.clicked.connect(partial(self._keep_this, r["id"]))
        play.clicked.connect(partial(self._play, r["id"]))
        play.setEnabled(r["kind"] == "video" and not r["cloud_only"])
        v.addWidget(keep)  # 主な操作は1行に単独で置き、文字が切れないようにする
        v.addWidget(label(scope, "caption", wrap=True))  # 「これを残す」で何が削除候補に入るか
        row.addWidget(play)
        details = button("詳しい情報", None, "info", "解像度・ビットレート・更新日時・保存場所・保護")
        details.setCheckable(True)
        row.addWidget(details)
        v.addLayout(row)
        meta = loads(r["meta"], {}) or {}
        d = candidates.duration(r)
        br = meta.get("bitrate")
        v.addWidget(label(f"長さ: {format_duration_short(d) or '不明'}　容量: {format_size(r['size'])}", None))
        extra = [f"解像度: {meta['width']}×{meta['height']}" if meta.get("width") else "解像度: 不明",
                 ("ビットレート: " + (f"{br / 1e6:.1f} Mbps" if br >= 1e6 else f"{br / 1e3:.0f} kbps")) if br
                 else "ビットレート: 不明",
                 f"更新日時: {candidates.format_date(r['mtime'])}",
                 "保存場所: " + fm.elidedText(r["folder"], Qt.TextElideMode.ElideLeft, 230),
                 "（解像度だけで画質の良し悪しは決められません）"]
        more = QWidget()
        mv = QVBoxLayout(more)
        mv.setContentsMargins(0, 0, 0, 0)
        mv.addWidget(label("\n".join(extra), "caption", wrap=True))
        prot = button("保護を解除" if r["protected"] else "保護する（次回以降も守る）", None, "shield",
                      "残す判断とは別に、次回以降も整理の対象から守ります")
        prot.clicked.connect(partial(self._protect, r["id"], not r["protected"]))
        mv.addWidget(prot)
        more.hide()
        details.toggled.connect(more.setVisible)
        v.addWidget(more)
        labels = []
        for frac, tag in self.points:
            at = f"（{format_duration_short(d * frac)}）" if d else ""
            v.addWidget(label(f"{tag}: {int(frac * 100)}% の位置{at}", "caption"))
            lb = QLabel("読み込み中…")
            lb.setFixedSize(280, 158)
            lb.setAlignment(Qt.AlignmentFlag.AlignCenter)
            lb.setStyleSheet("background: #1f1f1f; color: #f0f0f0; border-radius: 6px;")
            labels.append(lb)
            v.addWidget(lb)
        self.frame_labels[r["id"]] = labels
        self._request_frames(r, d, labels)
        v.addStretch()
        return col

    def _request_frames(self, r, duration, labels) -> None:
        tools = self.state.tools_fn()
        if r["cloud_only"]:
            for lb in labels:
                lb.setText("オンライン専用ファイルのため\n表示しません")
            return
        if r["kind"] != "video":
            pm = QPixmap(r["path"])
            if not pm.isNull():
                labels[0].setPixmap(pm.scaled(280, 158, Qt.AspectRatioMode.KeepAspectRatio,
                                              Qt.TransformationMode.SmoothTransformation))
            for lb in labels[1:]:
                lb.hide()
            return
        if not tools.available or not duration:
            for lb in labels:
                lb.setText("場面を表示できません\n（FFmpeg が必要です）" if not tools.available else "長さが不明のため\n場面を表示できません")
            return
        prefix = str(self.state.db.thumb_dir / f"cmp_{r['id']}_{int(r['mtime'])}_{r['size']}")
        self.pool.start(_FramesJob(self, r["id"], r["path"], duration, prefix, tools, [f for f, _ in self.points]))

    def _on_frames(self, mid: int, paths: list) -> None:
        for lb, path in zip(self.frame_labels.get(mid, []), paths):
            try:
                pm = QPixmap(path) if path else QPixmap()
                if pm.isNull():
                    lb.setText("この場面を取り出せませんでした")
                else:
                    lb.setPixmap(pm.scaled(280, 158, Qt.AspectRatioMode.KeepAspectRatio,
                                           Qt.TransformationMode.SmoothTransformation))
            except RuntimeError:
                pass  # 画面を作り直した後に届いた結果は捨てる

    def _save(self, fn) -> bool:
        try:
            fn()
            return True
        except StoreError as e:  # 保存できなかった判断は、成功したように見せずにその場で伝える
            QMessageBox.warning(self, "保存できませんでした", str(e))
            return False
        finally:
            self.state.refresh()

    def _targets_for_keep(self, mid: int) -> list[int]:
        """「これを残す」で削除候補にする録画。完全に同じファイルはほかのコピー全部、
        似た録画は比較中のもう1本だけ（確認していない録画は入れない）。保護中は入れない。"""
        kind = self.group[0] if self.group else "duplicate"
        others = [m for m in self.members() if m != mid] if kind == "duplicate" else \
            [m for m in self.pair if m != mid]
        return [m for m in others if not self.state.db.video(m)["protected"]]

    def _keep_scope_text(self, mid: int) -> str:
        n = len(self._targets_for_keep(mid))
        if self.group and self.group[0] == "duplicate":
            return f"ほかのコピー {n} 本を削除候補にします（まだ移動しません）"
        if len(self.members()) > 2:
            return "比較中のもう1本だけを削除候補にします（ほかの録画は未確認のまま）"
        return "もう1本を削除候補にします（まだ移動しません）"

    def _remember(self, ids) -> None:
        self._undo = {m: self.state.db.video(m)["decision"] for m in ids}

    def _show_result(self, text: str) -> None:
        self.result_text.setText(text)
        self.undo_button.setVisible(self._undo is not None)
        self.result_bar.show()

    def _keep_this(self, mid: int) -> None:
        """この1本を残し、対象を削除候補にする（まだ移動しない）。結果を表示し、取り消せるようにする。"""
        targets = self._targets_for_keep(mid)
        self._remember([mid] + targets)

        def run():
            self.state.db.set_decision(mid, "残す")
            for other in targets:
                self.state.db.set_decision(other, "削除候補")
        if self._save(run):
            name = self.state.db.video(mid)["name"]
            self._show_result(f"「{name}」を残します。ほかの {len(targets)} 本を削除候補に追加しました"
                              "（まだ移動していません。最終確認でごみ箱へ移すかを決めます）")
        self.refresh()

    def _decide_all(self, decision: str) -> None:
        ids = self.members()
        self._remember(ids)
        if self._save(lambda: [self.state.db.set_decision(m, decision) for m in ids]):
            self._show_result(f"このグループの {len(ids)} 本をすべて残します" if decision == "残す"
                              else "このグループは「保留」にしました（候補からいったん外れます）")
        self.refresh()

    def _keep_pair(self) -> None:
        ids = list(self.pair)
        self._remember(ids)
        if self._save(lambda: [self.state.db.set_decision(m, "残す") for m in ids]):
            self._show_result("比較中の2本を残します（このグループのほかの録画はそのまま）")
        self.refresh()

    def undo(self) -> None:
        """直前の判断を取り消す（判断する前の状態に戻す）。"""
        if not self._undo:
            return
        prev = self._undo
        if self._save(lambda: [self.state.db.set_decision(m, d) for m, d in prev.items()
                               if self.state.db.video(m) is not None]):
            self._undo = None
            self._show_result("直前の判断を取り消しました。もう一度選べます")
            self.undo_button.hide()
        self.refresh()

    def _pick_changed(self) -> None:
        a, b = self.left_combo.currentData(), self.right_combo.currentData()
        if a == b:  # 同じ録画は並べない
            others = [m for m in self.all_ids if m != a]
            b = others[0]
        self.pair = [a, b]
        self.refresh()

    def _protect(self, mid: int, value: bool) -> None:
        self._save(lambda: self.state.db.set_protected(mid, value))
        self.refresh()

    def _play(self, mid: int) -> None:
        r = self.state.db.video(mid)
        poster = str(self.state.db.thumb_dir / f"m{r['id']}_{int(r['mtime'])}_{r['size']}.jpg")
        PlayerDialog(r["path"], r["name"], poster, self).exec()

    def stop(self) -> None:
        self.pool.waitForDone(3000)


class FinalPage(QWidget):
    """最終確認: 残すファイルとごみ箱へ移すファイルを分けて示し、確定したら移動する。"""

    back = Signal()
    finished = Signal(list)

    def __init__(self, state: MediaState, parent=None) -> None:
        super().__init__(parent)
        self.setObjectName("Page")
        self.state = state
        self.ids: list[int] = []
        self.backend = None
        v = QVBoxLayout(self)
        v.setContentsMargins(theme.S3, theme.S2, theme.S3, theme.S1)
        v.setSpacing(theme.S1)
        v.addWidget(StepBar(2))
        v.addWidget(label("最終確認", "title"))
        self.summary = label("", "subtitle", wrap=True)
        v.addWidget(self.summary)
        lists = QHBoxLayout()
        left, right = QVBoxLayout(), QVBoxLayout()
        left.addWidget(label("ごみ箱へ移すファイル", "subtitle"))
        self.trash_table = _table(["ファイル", "容量", "保存場所"])
        left.addWidget(self.trash_table, 1)
        right.addWidget(label("残すファイル（同じグループで残すもの）", "subtitle"))
        self.keep_table = _table(["ファイル", "保存場所", "理由"])
        right.addWidget(self.keep_table, 1)
        lists.addLayout(left, 3)
        lists.addLayout(right, 2)
        v.addLayout(lists, 1)
        self.warning = label("", "warn", wrap=True)
        v.addWidget(self.warning)
        v.addWidget(label("移動の直前に、ファイルの存在・変更・保護と、残す側が実在することを確認します。"
                          "ごみ箱へ移しただけでは空き容量は増えません（ごみ箱を空にすると増えます。アプリはごみ箱を空にしません）。",
                          "caption", wrap=True))
        self.progress = label("", "subtitle", wrap=True)
        self.progress.hide()
        v.addWidget(self.progress)
        bar = QHBoxLayout()
        self.back_button = button("戻って選び直す", None, "undo")
        self.cancel_button = button("中断（ファイル単位）", None, "pause", "いま処理中のファイルが終わったところで止めます")
        self.cancel_button.hide()
        self.move_button = button("ごみ箱へ移す", "danger", "trash")
        bar.addWidget(self.back_button)
        bar.addStretch()
        bar.addWidget(self.cancel_button)
        bar.addWidget(self.move_button)
        v.addLayout(bar)
        self._worker: TrashWorker | None = None
        self.back_button.clicked.connect(self.back.emit)
        self.move_button.clicked.connect(self.execute)
        self.cancel_button.clicked.connect(self.cancel)

    def prepare(self, ids: list[int]) -> None:
        if self.is_running():  # 移動中は対象を変えない（画面の表示と実際の処理を食い違わせない）
            return
        self.generation = self.state.db.generation  # この確認の根拠になった調査結果の世代
        ctx = self.state.ctx
        self.ids = [i for i in dict.fromkeys(ids) if i in ctx.rows]
        rows = [ctx.rows[i] for i in self.ids]
        total = sum(r["size"] for r in rows)
        self.summary.setText(f"ごみ箱へ移す: {len(rows):,} 件・合計 {format_size(total)}（ファイルサイズの合計）")
        self.trash_table.setRowCount(len(rows))
        for i, r in enumerate(rows):
            for j, val in enumerate((r["name"], format_size(r["size"]), r["folder"])):
                self.trash_table.setItem(i, j, QTableWidgetItem(val))
        self.trash_table.resizeColumnsToContents()
        keeps = []
        chosen = set(self.ids)
        for h, members in ctx.dup_groups.items():
            if chosen & set(members):
                keeps += [(ctx.rows[m], "完全重複で残す1本" + ("（保護中）" if ctx.rows[m]["protected"] else ""))
                          for m in members if m not in chosen]
        for n, members in enumerate(ctx.video_groups):
            if chosen & set(members):
                keeps += [(ctx.rows[m], "類似録画で残す" + ("（保護中）" if ctx.rows[m]["protected"] else ""))
                          for m in members if m not in chosen]
        seen = set()
        keeps = [(r, why) for r, why in keeps if not (r["id"] in seen or seen.add(r["id"]))]  # 同じファイルは1回だけ
        self.keep_table.setRowCount(len(keeps))
        for i, (r, why) in enumerate(keeps):
            for j, val in enumerate((r["name"], r["folder"], why)):
                self.keep_table.setItem(i, j, QTableWidgetItem(val))
        self.keep_table.resizeColumnsToContents()
        problems = candidates.protect_group_rule(ctx, chosen)
        if not self.state.db.protection_ok:
            problems.insert(0, "保護・判断の記録を読み込めなかったため、整理操作を止めています（設定画面で対処できます）")
        protected = [r for r in rows if r["protected"]]
        if protected:
            problems.append(f"保護中の {len(protected)} 件は移動しません（保護を解除するまでごみ箱へ移動できません）")
        favorites = [r for r in rows if r["favorite"] and not r["protected"]]
        if favorites:
            problems.append(f"お気に入りの録画が {len(favorites)} 件含まれています: "
                            + "、".join(r["name"] for r in favorites[:3]) + ("ほか" if len(favorites) > 3 else ""))
        self.warning.setText("⚠ " + " / ".join(problems) if problems else "")
        self.move_button.setEnabled(bool(self.ids) and self.state.db.protection_ok)
        self._running = False

    def confirm(self, count: int, total: int) -> bool:
        """移動する直前の確認（テストでは置き換えられる）。"""
        ans = QMessageBox.question(
            self, "ごみ箱へ移す",
            f"{count:,} 件（合計 {format_size(total)}）を Windows のごみ箱へ移します。\n\n"
            "・完全削除はしません。ごみ箱へ移せないファイルは、移動せずにそのまま残します\n"
            "・移動の直前に、保護・ファイルの変更・残す側の存在を確認します\n"
            "・ごみ箱へ移しただけでは、空き容量は増えません（ごみ箱を空にすると増えます）\n\n"
            "移動してよろしいですか？",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No, QMessageBox.StandardButton.No)
        return ans == QMessageBox.StandardButton.Yes

    def is_running(self) -> bool:
        return self._worker is not None

    def execute(self) -> None:
        """別スレッドでごみ箱へ移す（画面は応答し続ける）。二重実行を防ぐ。"""
        if self._worker is not None or not self.ids:
            return
        rows = [self.state.ctx.rows[i] for i in self.ids if i in self.state.ctx.rows]
        if not self.confirm(len(rows), sum(r["size"] for r in rows)):
            return  # 利用者が承認しなければ何もしない
        self._running = True
        self.move_button.setEnabled(False)
        self.back_button.setEnabled(False)
        self.cancel_button.show()
        self.cancel_button.setEnabled(True)
        self.progress.setText(f"ごみ箱へ移動しています… 0 / {len(self.ids)}")
        self.progress.show()
        if self.state.runner.is_running():
            QMessageBox.information(self, "ごみ箱へ移す", "調査中は移動できません。調査が終わってから、もう一度確認してください。")
            self._running = False
            self.move_button.setEnabled(True)
            self.back_button.setEnabled(True)
            self.cancel_button.hide()
            self.progress.hide()
            return
        self._worker = TrashWorker(self.state.db, list(self.ids), self.backend, getattr(self, "generation", None))
        self._worker.progress.connect(self._on_progress)
        self._worker.done.connect(self._on_done)
        self._worker.failed.connect(self._on_failed)
        self._worker.finished.connect(self._worker.deleteLater)
        self._worker.start()

    def cancel(self) -> None:
        """ファイル単位の区切りで止める（移動中のファイルは最後まで処理する）。"""
        if self._worker is not None:
            self._worker.cancel_event.set()
            self.cancel_button.setEnabled(False)
            self.progress.setText(self.progress.text() + "（中断しています。いま処理中のファイルが終わると止まります）")

    def wait(self, msecs: int = 60000) -> bool:
        return self._worker.wait(msecs) if self._worker is not None else True

    def _on_progress(self, done: int, total: int, name: str) -> None:
        self.progress.setText(f"ごみ箱へ移動しています… {done + 1} / {total}: {name}")

    def _finish_ui(self) -> None:
        self._worker = None
        self._running = False
        self.back_button.setEnabled(True)
        self.cancel_button.hide()
        self.progress.hide()

    def _on_done(self, results: list) -> None:
        self._finish_ui()
        self.state.refresh()
        self.finished.emit(results)

    def _on_failed(self, message: str) -> None:
        # 予期しない例外でも「実行中」のまま固まらない。移動済みの分は 1 件ずつ保存済み
        self._finish_ui()
        self.state.refresh()
        self.move_button.setEnabled(True)
        QMessageBox.warning(self, "ごみ箱への移動", f"移動を続けられませんでした: {message}\n"
                                                   "移動できたファイルの結果は保存されています。一覧を確認してください。")


class TrashWorker(QThread):
    """ごみ箱への移動を GUI から分離して実行する（保護・再検証は move_to_trash の中で行う）。"""

    progress = Signal(int, int, str)
    done = Signal(list)
    failed = Signal(str)

    def __init__(self, db, ids, backend, generation=None) -> None:
        super().__init__()
        self.db, self.ids, self.backend, self.generation = db, ids, backend, generation
        self.cancel_event = threading.Event()

    def run(self) -> None:
        try:
            with trash.windows_com_thread():  # Windows のごみ箱 API は、呼び出すスレッドで COM の初期化が必要
                results = trash.move_to_trash(self.db, self.ids, self.backend,
                                              progress=lambda n, t, name: self.progress.emit(n, t, name),
                                              cancel=self.cancel_event, expected_generation=self.generation)
        except Exception as e:  # noqa: BLE001  画面を「実行中」のままにしない
            self.failed.emit(str(e) or type(e).__name__)
            return
        self.done.emit(results)


class ResultPage(QWidget):
    """結果: 移動・スキップ・失敗を分けて示し、次の候補へ進める。"""

    next_candidates = Signal()
    go_home = Signal()

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setObjectName("Page")
        v = QVBoxLayout(self)
        v.setContentsMargins(theme.S3, theme.S2, theme.S3, theme.S1)
        v.setSpacing(theme.S1)
        v.addWidget(label("整理の結果", "title"))
        self.summary = label("", "subtitle", wrap=True)
        v.addWidget(self.summary)
        self.table = _table(["結果", "ファイル", "説明"])
        v.addWidget(self.table, 1)
        v.addWidget(label("ごみ箱へ移したファイルは、ごみ箱を空にするまで空き容量が増えません。"
                          "元に戻すときは Windows のごみ箱から復元してください（このアプリに復元機能はありません）。",
                          "sub", wrap=True))
        bar = QHBoxLayout()
        home = button("ホームへ", None, "home")
        nxt = button("整理のおすすめへ", "primary", "target")
        bar.addWidget(home)
        bar.addStretch()
        bar.addWidget(nxt)
        v.addLayout(bar)
        home.clicked.connect(self.go_home.emit)
        nxt.clicked.connect(self.next_candidates.emit)
        self.results: list = []

    def show_results(self, results: list) -> None:
        self.results = results
        groups = {k: [r for r in results if r.status == k] for k in ("moved", "skipped", "failed")}
        moved = sum(r.size for r in groups["moved"])
        self.summary.setText(f"ごみ箱へ移動: {len(groups['moved'])} 件（{format_size(moved)}） / "
                             f"安全確認のため移動しなかった: {len(groups['skipped'])} 件 / 移動に失敗した: {len(groups['failed'])} 件")
        label_ = {"moved": "移動した", "skipped": "スキップ", "failed": "失敗"}
        rows = [r for k in ("failed", "skipped", "moved") for r in groups[k]]
        self.table.setRowCount(len(rows))
        for i, r in enumerate(rows):
            for j, val in enumerate((label_[r.status], os.path.basename(r.path), r.message)):
                self.table.setItem(i, j, QTableWidgetItem(val))
        self.table.resizeColumnsToContents()


class FlowPage(QWidget):
    """整理のおすすめ（候補を確認 → 比較して選ぶ）。選んだ録画は「削除候補」へ送り、そこで最終確認する。"""

    to_candidates = Signal()

    def __init__(self, state: MediaState, plan: "PlanPage", parent=None) -> None:
        super().__init__(parent)
        self.setObjectName("Page")
        self.state = state
        self.plan = plan
        self.compare = ComparePage(state)
        self.stack = QStackedWidget()
        for w in (plan, self.compare):
            self.stack.addWidget(w)
        v = QVBoxLayout(self)
        v.setContentsMargins(0, 0, 0, 0)
        v.addWidget(self.stack)
        plan.compare_requested.connect(self.open_compare)
        plan.final_requested.connect(self.open_final)
        self.compare.back.connect(self.show_candidates)
        self.compare.to_final.connect(lambda: self.open_final(self.plan.checked_ids()))

    def show_candidates(self) -> None:
        self.plan.replan()
        self.stack.setCurrentWidget(self.plan)

    def open_compare(self, kind: str, key) -> None:
        self.plan.stop_media()
        self.compare.show_group(kind, key)
        self.stack.setCurrentWidget(self.compare)

    def open_final(self, ids: list) -> None:
        """チェックした録画を削除候補として保存し、「削除候補」画面へ進む（ここでは移動しない）。"""
        self.plan.stop_media()
        if self.plan.persist_checks():
            self.state.refresh()
            self.stack.setCurrentWidget(self.plan)
            self.to_candidates.emit()

    def stop_media(self) -> None:
        self.plan.stop_media()


class CandidatesPage(QWidget):
    """削除候補（一覧 → 最終確認 → 結果）。ごみ箱への移動は、ここの最終確認からだけ行う。"""

    go_home = Signal()
    go_recommend = Signal()

    def __init__(self, state: MediaState, parent=None) -> None:
        super().__init__(parent)
        self.setObjectName("Page")
        self.state = state
        self.browser = MediaBrowser(state, "candidates")
        self.final = FinalPage(state)
        self.result = ResultPage()
        self.stack = QStackedWidget()
        for w in (self.browser, self.final, self.result):
            self.stack.addWidget(w)
        v = QVBoxLayout(self)
        v.setContentsMargins(0, 0, 0, 0)
        v.addWidget(self.stack)
        self.browser.final_requested.connect(self.open_final)
        self.final.back.connect(self.show_list)
        self.final.finished.connect(self.show_result)
        self.result.next_candidates.connect(self.go_recommend.emit)
        self.result.go_home.connect(self.go_home.emit)

    @property
    def request_locations(self):
        return self.browser.request_locations

    @property
    def request_files(self):
        return self.browser.request_files

    def show_list(self) -> None:
        if not self.final.is_running():
            self.browser.refresh()
            self.stack.setCurrentWidget(self.browser)

    def open_final(self, ids: list) -> None:
        self.browser.stop_media()
        self.final.prepare(ids)
        self.stack.setCurrentWidget(self.final)

    def show_result(self, results: list) -> None:
        self.result.show_results(results)
        self.stack.setCurrentWidget(self.result)

    def stop_media(self) -> None:
        self.browser.stop_media()


# =============================================================================
# 容量の内訳（PC全体の調査結果）
# =============================================================================
WINDOWS_SETTINGS = [("ストレージの設定を開く", "ms-settings:storagesense", "一時ファイル・ごみ箱・ダウンロードなどを Windows で整理します"),
                    ("アプリの一覧を開く", "ms-settings:appsfeatures", "使っていないアプリは Windows の設定からアンインストールします"),
                    ("ディスク クリーンアップ", "cleanmgr.exe", "Windows の更新ファイルなどを Windows の機能で削除します")]


def open_windows_setting(target: str) -> bool:
    if os.name != "nt":
        return False
    try:
        os.startfile(target)  # type: ignore[attr-defined]
        return True
    except OSError:
        return False


class CapacityPage(QWidget):
    """何が容量を使っているか（種類別・フォルダ別・大きいファイル・システムやアプリの領域）。ホームから開く。"""

    def add_back_link(self, text: str, fn) -> None:
        b = button(text, "link", "undo")
        b.clicked.connect(fn)
        self.layout().insertWidget(0, b, 0, Qt.AlignmentFlag.AlignLeft)

    request_locations = Signal()

    def __init__(self, state: MediaState, parent=None) -> None:
        super().__init__(parent)
        self.setObjectName("Page")
        self.state = state
        v = QVBoxLayout(self)
        v.setContentsMargins(theme.S3, theme.S2, theme.S3, theme.S1)
        v.setSpacing(theme.S1)
        v.addWidget(label("容量の内訳", "title"))
        v.addWidget(label("調査した範囲のファイルサイズ（論理サイズ）の合計です。実際のディスク占有量や、"
                          "調べていない場所・アクセスできない場所の分は含みません。", "sub", wrap=True))
        self.drives = label("", None, wrap=True)
        v.addWidget(self.drives)
        self.tabs = QTabWidget()
        self.by_category = _table(["種類", "件数", "合計サイズ", "割合"])
        self.by_folder = _table(["調査場所", "フォルダ", "件数", "合計サイズ"])
        self.top_files = _table(["ファイル", "種類", "サイズ"])
        areas = QWidget()
        av = QVBoxLayout(areas)
        av.addWidget(label("システムやアプリの領域は、直接消さずに Windows の機能で整理してください。"
                           "このアプリでは整理候補にしません。", "sub", wrap=True))
        self.areas = _table(["場所", "サイズ", "理由"])
        av.addWidget(self.areas, 1)
        row = QHBoxLayout()
        for text, target, tip in WINDOWS_SETTINGS:
            b = button(text, None, "settings", tip)
            b.setEnabled(os.name == "nt")
            if os.name != "nt":
                b.setToolTip(tip + "（Windows でのみ使えます）")
            b.clicked.connect(partial(open_windows_setting, target))
            row.addWidget(b)
        row.addStretch()
        av.addLayout(row)
        self.tabs.addTab(self.by_category, "種類別")
        self.tabs.addTab(self.by_folder, "フォルダ別")
        self.tabs.addTab(self.top_files, "大きいファイル")
        self.tabs.addTab(areas, "システム・アプリなど")
        self.empty = EmptyState()
        self.content = QStackedWidget()
        self.content.addWidget(self.tabs)
        self.content.addWidget(self.empty)
        v.addWidget(self.content, 1)
        state.changed.connect(self.refresh)
        self.refresh()

    def refresh(self) -> None:
        db = self.state.db
        locs = {x["path"]: x for x in db.locations()}
        stats = db.location_stats()
        paths = list(locs) or [system_drive_usage()[0] if system_drive_usage() else "/"]
        spaces = candidates.drive_space(paths)
        self.drives.setText("ドライブ: " + "　".join(
            f"{d['root']} 使用 {format_size(d['used'])} / 全体 {format_size(d['total'])}（空き {format_size(d['free'])}）"
            for d in spaces) if spaces else "ドライブの容量を取得できません")
        if not stats:
            self.content.setCurrentWidget(self.empty)
            self.empty.show_state("drive", "まだ調査していません", "PCを調べると、何が容量を使っているかを表示します。",
                                  "PCを調べる", self.request_locations.emit)
            return
        self.content.setCurrentWidget(self.tabs)
        cats: dict[str, list[int]] = {}
        folders, files, areas = [], [], []
        for path, st in stats.items():
            label_ = locs.get(path, {}).get("label", path)
            for cat, (n, b) in st.get("by_category", {}).items():
                c = cats.setdefault(cat, [0, 0])
                c[0] += n
                c[1] += b
            for folder, (n, b) in st.get("by_folder", {}).items():
                folders.append((label_, folder, n, b))
            files += st.get("top_files", [])
            areas += st.get("areas", [])
        total = sum(b for _, b in cats.values()) or 1
        _fill(self.by_category, [[c, f"{n:,}", format_size(b), f"{b / total:.1%}"]
                                 for c, (n, b) in sorted(cats.items(), key=lambda x: -x[1][1])])
        _fill(self.by_folder, [[lb, f, f"{n:,}", format_size(b)] for lb, f, n, b in sorted(folders, key=lambda x: -x[3])[:500]])
        seen = set()
        uniq = []
        for size, path, cat in sorted(files, reverse=True):
            if path not in seen:
                seen.add(path)
                uniq.append([path, cat, format_size(size)])
        _fill(self.top_files, uniq[:200])
        _fill(self.areas, [[p, format_size(b), r] for p, b, r in sorted(areas, key=lambda a: -a[1])[:200]])


# =============================================================================
# 設定
# =============================================================================
class SettingsPage(QWidget):
    changed = Signal()

    def __init__(self, state: MediaState, parent=None) -> None:
        super().__init__(parent)
        self.state = state
        db = state.db
        v = QVBoxLayout(self)
        v.setSpacing(theme.S2)
        ff = QGroupBox("FFmpeg（録画の長さ・代表場面・プレビュー画像に使用）")
        fv = QVBoxLayout(ff)
        row = QHBoxLayout()
        self.ffmpeg_dir = QLineEdit(db.get_setting("ffmpeg_dir") or "")
        self.ffmpeg_dir.setPlaceholderText("空欄なら PATH から探します（ffmpeg.exe と ffprobe.exe がある bin フォルダ）")
        browse = button("参照…", None, "folder")
        check = button("保存して確認", "primary")
        row.addWidget(self.ffmpeg_dir, 1)
        row.addWidget(browse)
        row.addWidget(check)
        fv.addLayout(row)
        self.ffmpeg_state = label("", None, wrap=True)
        fv.addWidget(self.ffmpeg_state)
        guide = label("入手方法（このアプリは自動でインストールしません）:\n"
                      "1. スタートメニューで「ターミナル」または「PowerShell」を開く\n"
                      "2. winget install Gyan.FFmpeg と入力して Enter（WinGet で入れた FFmpeg は自動で見つけます）\n"
                      "3. このアプリを起動し直す（見つからないときは、上の欄に ffmpeg.exe がある bin フォルダを指定）\n"
                      "FFmpeg がなくても、録画の一覧・容量の集計・完全重複の検出・ごみ箱への移動は使えます。",
                      "caption", wrap=True)
        guide.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        fv.addWidget(guide)
        v.addWidget(ff)
        scan = QGroupBox("調査")
        sv = QVBoxLayout(scan)
        self.auto_start = QCheckBox("起動時に自動で調査する（内蔵ドライブ全体と、ビデオ・デスクトップなどのフォルダ）")
        self.auto_start.setChecked(db.get_setting("auto_scan_on_start", "1") != "0")
        sv.addWidget(self.auto_start)
        scope_row = QHBoxLayout()
        scope_button = button("調査する場所・除外する場所を確認…", None, "search",
                              "調べる場所のチェック・外付けドライブの追加・除外の規則を確認できます")
        scope_button.clicked.connect(lambda: ScopeDialog(db, self.window()).exec())
        scope_row.addWidget(scope_button)
        scope_row.addStretch()
        sv.addLayout(scope_row)
        sv.addWidget(label("外付け・ネットワーク・リムーバブルドライブは自動では調べません（下の「追加で調べる場所」で追加できます）。"
                           "Windows・Program Files・ProgramData・AppData などのシステムやアプリの領域は調べません。",
                           "caption", wrap=True))
        self.lists = {}
        for key, title in (("excluded_folders", "調査の対象外にするフォルダ"),
                           ("extra_locations", "追加で調べる場所（外付け・ネットワークドライブやフォルダ）")):
            sv.addWidget(label(title, "sub"))
            lst = QListWidget()
            lst.setMaximumHeight(90)
            sv.addWidget(lst)
            r = QHBoxLayout()
            add, remove = button("追加…", None, "folder"), button("削除")
            add.clicked.connect(partial(self._add, key))
            remove.clicked.connect(partial(self._remove, key))
            r.addWidget(add)
            r.addWidget(remove)
            r.addStretch()
            sv.addLayout(r)
            self.lists[key] = lst
        v.addWidget(scan)
        data = QGroupBox("データ")
        dv = QVBoxLayout(data)
        self.cache_label = label("", None, wrap=True)
        clear = button("サムネイルのキャッシュを削除")
        dv.addWidget(self.cache_label)
        dv.addWidget(clear, 0, Qt.AlignmentFlag.AlignLeft)
        self.reset_user = button("保護・判断の記録を初期化して整理を再開")
        self.reset_user.setVisible(not db.protection_ok)
        self.reset_user.clicked.connect(self._reset_user_state)
        dv.addWidget(self.reset_user, 0, Qt.AlignmentFlag.AlignLeft)
        dv.addWidget(label(f"保存先: {db.data_dir}（設定・保護・判断・お気に入りは JSON、調査結果は場所ごとのファイルに保存。"
                           "データベースは使いません。以前の版（RecSortAI・RecSort）のデータは読み書きしません）",
                           "caption", wrap=True))
        v.addWidget(data)
        v.addStretch()
        browse.clicked.connect(self._browse)
        check.clicked.connect(self._save_ffmpeg)
        self.auto_start.toggled.connect(lambda on: db.set_setting("auto_scan_on_start", "1" if on else "0"))
        clear.clicked.connect(self._clear_cache)
        self.refresh()

    def refresh(self) -> None:
        db = self.state.db
        for key, lst in self.lists.items():
            lst.clear()
            lst.addItems(db.json_list(key))
        tools = self.state.tools_fn()
        self.ffmpeg_state.setText(f"FFmpeg を利用できます（{tools.source or '見つかった場所'}: {tools.ffmpeg}）"
                                  if tools.available else
                                  "FFmpeg が見つかりません（録画の長さ・黒画面・無音・代表場面・サムネイルの判定が使えません）")
        size = sum(f.stat().st_size for f in db.thumb_dir.glob("*") if f.is_file())
        self.cache_label.setText(f"サムネイルのキャッシュ: {format_size(size)}（上限 {format_size(THUMB_CACHE_MAX_BYTES)}。"
                                 "超えると古いものから自動で削除します）")

    def _browse(self) -> None:
        d = QFileDialog.getExistingDirectory(self, "ffmpeg.exe があるフォルダ")
        if d:
            self.ffmpeg_dir.setText(d)

    def _save_ffmpeg(self) -> None:
        self.state.db.set_setting("ffmpeg_dir", self.ffmpeg_dir.text().strip() or None)
        self.refresh()
        self.changed.emit()

    def _add(self, key: str, path: str | None = None) -> None:
        path = path or QFileDialog.getExistingDirectory(self, "フォルダ・ドライブを選択")
        if path:
            self.state.db.set_json_list(key, self.state.db.json_list(key) + [path])
            self.refresh()

    def _remove(self, key: str) -> None:
        item = self.lists[key].currentItem()
        if item:
            self.state.db.set_json_list(key, [p for p in self.state.db.json_list(key) if p != item.text()])
            self.refresh()

    def _reset_user_state(self) -> None:
        ans = QMessageBox.question(
            self, "保護・判断の記録", "読み込めなかった保護・判断の記録を初期化します。\n"
            "これまでの保護・判断は使えなくなります（壊れたファイルは保存先に退避してあり、削除しません）。"
            "必要なファイルは整理の前にもう一度保護してください。続けますか？")
        if ans != QMessageBox.StandardButton.Yes:
            return
        self.state.db.reset_user_state()
        self.state.db.load_problems[:] = [p for p in self.state.db.load_problems if not p.startswith("user_state.json")]
        self.reset_user.hide()
        self.state.refresh()
        self.changed.emit()

    def _clear_cache(self) -> None:
        prune_thumb_cache(self.state.db.thumb_dir, max_bytes=0)
        self.state.loader.cache.clear()
        self.refresh()


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
                                         "おすすめの1つを「残す」にし、ほかを削除候補にします（まだ移動しません）")
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
                               for r in db.locations()])
        tabs.addTab(self.locations, "調査した場所")
        self.errors = _table(["スキップした場所", "理由"])
        _fill(self.errors, [[r["path"], r["reason"]] for r in sorted(db.skips("error"), key=lambda r: r["path"])[:2000]])
        tabs.addTab(self.errors, "アクセスできなかった場所")
        self.excluded = _table(["除外した場所", "理由"])
        _fill(self.excluded, [[r["path"], r["reason"]] for r in sorted(db.skips("excluded"),
                                                                         key=lambda r: r["path"])[:2000]])
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
                           "「保護」「残す」と判断したもの、大容量という理由だけのものは含めません。"))
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



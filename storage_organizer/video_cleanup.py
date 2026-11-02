"""動画の削除候補スキャン (推奨度づけ・サムネイル・削除予定フォルダへの移動・移動履歴)

※ このアプリは動画を削除しない。削除予定フォルダへ移動するところまでで、
  ごみ箱へ移す・削除するのは利用者自身の操作に限る。
"""

from __future__ import annotations

import csv
import hashlib
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path

from . import ffmpeg
from .files import find_duplicates, find_files
from .pending_folder import move_to_pending, pending_dir
from .video_analyzer import (CAT_AUDIO, CAT_DUP, CAT_ERROR, CAT_SHORT, CAT_VIDEO, VIDEO_EXTS, Thresholds,
                             analyse, fmt_time)

APP_DIR = Path(os.environ.get("LOCALAPPDATA", Path.home())) / "storage-organizer"
THUMB_DIR = APP_DIR / "thumbs"
HISTORY_FILE = APP_DIR / "移動履歴.csv"

HIGH, MID, LOW, NONE = 3, 2, 1, 0


def priority(categories: list[str]) -> int:
    """削除推奨度。
    高: 短すぎる / 重複 / 壊れている / 画質・音声の問題が両方
    中: 画質が粗い or 音声が不明瞭
    低: 長すぎるだけ (大事な長時間録画の可能性があるので参考扱い)
    """
    cats = set(categories)
    if cats & {CAT_SHORT, CAT_DUP, CAT_ERROR}:
        return HIGH
    mid = cats & {CAT_VIDEO, CAT_AUDIO}
    if mid:
        return HIGH if len(mid) >= 2 else MID
    return LOW if cats else NONE


def make_thumbnail(path: Path, duration: float, key: str) -> bool:
    out = THUMB_DIR / f"{key}.jpg"
    if not out.exists():
        ss = duration * 0.1 if duration > 1 else 0
        ffmpeg.run([ffmpeg.ffmpeg(), "-v", "error", "-y", "-ss", f"{ss:.2f}", "-i", str(path), "-frames:v", "1",
                    "-vf", "scale=320:-2", "-q:v", "5", str(out)], timeout=60)
    return out.exists()


def log_move(item: dict, dest: Path) -> None:
    """移動履歴。元の場所に戻したいときの手がかりになる。"""
    APP_DIR.mkdir(parents=True, exist_ok=True)
    new = not HISTORY_FILE.exists()
    with HISTORY_FILE.open("a", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        if new:
            w.writerow(["移動日時", "元の場所", "移動先", "サイズMB", "長さ", "理由"])
        w.writerow([datetime.now().strftime("%Y-%m-%d %H:%M:%S"), item["path"], str(dest),
                    f"{item['size'] / 2**20:.1f}", fmt_time(item["duration"]), " / ".join(item["reasons"])])


class CleanupScanner:
    """バックグラウンドで動画をスキャンし、結果を保持する。移動はスキャン結果にあるものだけ許可。"""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.state, self.done, self.total, self.message = "idle", 0, 0, ""
        self._items: dict[str, dict] = {}

    # --- 状態
    def status(self) -> dict:
        with self._lock:
            s = {"state": self.state, "done": self.done, "total": self.total, "message": self.message}
            if self.state == "done":
                s["items"] = list(self._items.values())
            return s

    def get(self, item_id: str) -> dict | None:
        with self._lock:
            return self._items.get(item_id)

    # --- スキャン
    def start(self, folders: list[str], thresholds: Thresholds | None = None) -> bool:
        with self._lock:
            if self.state == "scanning":
                return False
            self.state, self.done, self.total, self.message = "scanning", 0, 0, "動画を探しています…"
            self._items = {}
        threading.Thread(target=self._run, args=(folders, thresholds or Thresholds()), daemon=True).start()
        return True

    def wait(self, timeout: float | None = None) -> None:
        """テスト用: スキャン完了まで待つ。"""
        end = None if timeout is None else time.monotonic() + timeout
        while self.state == "scanning" and (end is None or time.monotonic() < end):
            time.sleep(0.2)

    def _run(self, folders: list[str], th: Thresholds) -> None:
        try:
            THUMB_DIR.mkdir(parents=True, exist_ok=True)
            files = find_files(folders, VIDEO_EXTS)
            with self._lock:
                self.total, self.message = len(files), "重複をチェックしています…"
            dups = find_duplicates(files)
            with self._lock:
                self.message = "動画を解析しています…"
            workers = max(2, min(6, (os.cpu_count() or 4) // 2))
            with ThreadPoolExecutor(workers) as ex:
                for item in ex.map(lambda p: self._analyse_one(p, th, dups.get(p)), files):
                    with self._lock:
                        self._items[item["id"]] = item
                        self.done += 1
            with self._lock:
                self.state, self.message = "done", ""
        except Exception as e:  # noqa: BLE001
            with self._lock:
                self.state, self.message = "error", f"スキャン中にエラー: {e}"

    def _analyse_one(self, p: Path, th: Thresholds, dup_of: Path | None) -> dict:
        r = analyse(p, th)
        if dup_of is not None:
            r.flag(CAT_DUP, f"重複 ({dup_of} と同じ内容)")
        st = p.stat()
        key = hashlib.sha1(f"{p}|{st.st_size}|{st.st_mtime}".encode()).hexdigest()[:16]
        try:
            has_thumb = make_thumbnail(p, r.duration, key)
        except Exception:  # noqa: BLE001
            has_thumb = False
        return {
            "id": key, "path": str(p), "name": p.name, "folder": str(p.parent),
            "size": st.st_size, "mtime": datetime.fromtimestamp(st.st_mtime).strftime("%Y-%m-%d %H:%M"),
            "duration": r.duration, "res": f"{r.width}x{r.height}" if r.width else "",
            "categories": r.categories, "reasons": r.reasons, "priority": priority(r.categories),
            "thumb": has_thumb,
        }

    # --- 削除予定フォルダへ移動
    def move_to_pending(self, ids: list[str], dest_dir: Path | None = None) -> dict:
        dest_dir = dest_dir or pending_dir()
        results = []
        for i in ids:
            item = self.get(i)
            if item is None:  # スキャン結果に無いものは動かさない
                results.append({"id": i, "ok": False, "error": "不明なファイル"})
                continue
            try:
                dest = move_to_pending(Path(item["path"]), dest_dir)
                with self._lock:
                    self._items.pop(i, None)
                log_move(item, dest)
                results.append({"id": i, "ok": True, "dest": str(dest)})
            except Exception as e:  # noqa: BLE001
                results.append({"id": i, "ok": False, "error": str(e), "name": item["name"]})
        return {"folder": str(dest_dir), "results": results}

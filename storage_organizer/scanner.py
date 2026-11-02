"""フォルダを走査してファイルのメタデータを集めるスキャナ。

Qt に依存しないので、単体でテストできる。

安全上の方針:
- ファイルの内容は一切読まない（os.scandir と stat 情報だけを使う）
- ファイルの作成・変更・削除は一切しない
- シンボリックリンクとジャンクションはたどらない（集計にも含めない）
- アクセスできない場所はスキップして記録する
- サイズは stat の st_size（ファイルサイズ）であり、実際のディスク使用量ではない
"""

from __future__ import annotations

import os
import stat
import sys
import threading
import time
from dataclasses import dataclass, field
from typing import Callable

from .file_types import categorize

# Windows の再解析ポイント（リンク類）のタグ
_IO_REPARSE_TAG_MOUNT_POINT = 0xA0000003  # ジャンクション / マウントポイント
_IO_REPARSE_TAG_SYMLINK = 0xA000000C  # シンボリックリンク
_LINK_REPARSE_TAGS = frozenset({_IO_REPARSE_TAG_MOUNT_POINT, _IO_REPARSE_TAG_SYMLINK})
_FILE_ATTRIBUTE_REPARSE_POINT = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
# OneDrive などで「クラウド上のみ」にあるファイルの属性。中身を読むとダウンロードが始まる
_CLOUD_ONLY_ATTRIBUTES = 0x00400000 | 0x00040000 | 0x00001000  # RECALL_ON_DATA_ACCESS / RECALL_ON_OPEN / OFFLINE

# 詳細表示用に保持するスキップ場所の最大数（件数自体は上限なしで数える）
MAX_SKIPPED_DETAILS = 200


@dataclass(frozen=True, slots=True)
class FileRecord:
    """1ファイル分のメタデータ。"""

    name: str
    size: int  # ファイルサイズ（バイト）。ディスク使用量ではない
    category: str
    mtime: float  # 最終更新日時（UNIX時刻）
    folder: str  # 保存場所（親フォルダのパス）
    cloud_only: bool = False  # クラウド上のみ（中身を読む処理の対象外にする）

    @property
    def path(self) -> str:
        return os.path.join(self.folder, self.name)


@dataclass(frozen=True, slots=True)
class ScanProgress:
    """進捗表示用の途中経過。総件数は事前に分からないので割合は持たない。"""

    files: int
    total_size: int
    dirs: int
    skipped: int
    links: int
    current_dir: str


@dataclass
class ScanResult:
    """スキャン終了時の集計。"""

    root: str
    files: int = 0
    total_size: int = 0
    dirs: int = 0
    skipped: int = 0  # アクセスできずスキップした場所の数
    links: int = 0  # たどらなかったリンク・ジャンクションの数
    skipped_details: list[tuple[str, str]] = field(default_factory=list)
    cancelled: bool = False
    elapsed: float = 0.0

    def record_skip(self, path: str, error: OSError) -> None:
        self.skipped += 1
        if len(self.skipped_details) < MAX_SKIPPED_DETAILS:
            reason = error.strerror or type(error).__name__
            self.skipped_details.append((path, reason))


def is_link(entry: os.DirEntry) -> bool:
    """シンボリックリンクまたはジャンクションなら True。"""
    if entry.is_symlink():
        return True
    is_junction = getattr(entry, "is_junction", None)  # Python 3.12 以降
    if is_junction is not None and is_junction():
        return True
    if sys.platform == "win32":
        # 古い Python 向けの予備判定。リンク系のタグだけを対象にし、
        # OneDrive などの再解析ポイント（リンクではない）は通常通り扱う。
        st = entry.stat(follow_symlinks=False)
        if st.st_file_attributes & _FILE_ATTRIBUTE_REPARSE_POINT:
            if getattr(st, "st_reparse_tag", 0) in _LINK_REPARSE_TAGS:
                return True
    return False


def is_cloud_only(st: os.stat_result) -> bool:
    return bool(getattr(st, "st_file_attributes", 0) & _CLOUD_ONLY_ATTRIBUTES)


def scan(
    root: str,
    cancel_event: threading.Event,
    on_batch: Callable[[list[FileRecord]], None],
    on_progress: Callable[[ScanProgress], None] | None = None,
    *,
    batch_interval: float = 0.2,
    batch_size: int = 2000,
) -> ScanResult:
    """root 以下をサブフォルダまで走査する。

    見つかったファイルは batch_interval 秒ごと、または batch_size 件ごとに
    まとめて on_batch に渡す（画面への通知回数を抑えるため）。
    cancel_event がセットされたら、その時点までの結果で終了する。
    """
    result = ScanResult(root=root)
    started = time.monotonic()
    batch: list[FileRecord] = []
    last_flush = started
    current_dir = root

    def flush() -> None:
        nonlocal batch, last_flush
        if batch:
            on_batch(batch)
            batch = []
        if on_progress is not None:
            on_progress(
                ScanProgress(
                    files=result.files,
                    total_size=result.total_size,
                    dirs=result.dirs,
                    skipped=result.skipped,
                    links=result.links,
                    current_dir=current_dir,
                )
            )
        last_flush = time.monotonic()

    # 再帰ではなくスタックで走査する（深い階層でも再帰上限に当たらない）
    stack: list[str] = [root]
    interrupted = False  # フォルダの途中でキャンセルされたか
    while stack and not cancel_event.is_set():
        current_dir = stack.pop()
        try:
            iterator = os.scandir(current_dir)
        except OSError as error:  # PermissionError などを含む
            result.record_skip(current_dir, error)
            continue

        result.dirs += 1
        with iterator:
            while True:
                if cancel_event.is_set():
                    interrupted = True
                    break
                try:
                    entry = next(iterator)
                except StopIteration:
                    break
                except OSError as error:
                    # 一覧の途中で読めなくなった場合
                    result.record_skip(current_dir, error)
                    break

                try:
                    if is_link(entry):
                        result.links += 1
                        continue
                    if entry.is_dir(follow_symlinks=False):
                        stack.append(entry.path)
                        continue
                    if not entry.is_file(follow_symlinks=False):
                        continue  # デバイスファイルなど通常ファイル以外
                    # stat はメタデータだけを取得し、ファイルを開かない
                    st = entry.stat(follow_symlinks=False)
                except OSError as error:
                    result.record_skip(entry.path, error)
                    continue

                batch.append(
                    FileRecord(
                        name=entry.name,
                        size=st.st_size,
                        category=categorize(entry.name),
                        mtime=st.st_mtime,
                        folder=current_dir,
                        cloud_only=is_cloud_only(st),
                    )
                )
                result.files += 1
                result.total_size += st.st_size

                if len(batch) >= batch_size or time.monotonic() - last_flush >= batch_interval:
                    flush()

        # ファイルの少ないフォルダが続いても進捗表示が止まらないようにする
        if time.monotonic() - last_flush >= batch_interval:
            flush()

    # 走査し終えた後にキャンセルが押された場合は「完了」として扱う
    result.cancelled = interrupted or bool(stack)
    flush()
    result.elapsed = time.monotonic() - started
    return result

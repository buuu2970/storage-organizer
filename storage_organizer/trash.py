"""ごみ箱への移動（担当: 削除処理）。

- ユーザーが確認して実行したときだけ動く（自動削除はしない）
- 完全削除はしない。ごみ箱へ移せない場合はエラーにし、完全削除に切り替えない
- 移動する直前に、スキャン時からファイルが変わっていないかを確認する
- 「絶対に残す」・オンライン専用・重複グループの最後の1つは移動しない
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from typing import Callable

from . import candidates, media
from .db import Database


class TrashError(Exception):
    pass


@dataclass
class TrashResult:
    media_id: int
    path: str
    size: int
    ok: bool
    message: str


def _windows_recycle(path: str) -> None:
    """Windows のごみ箱へ移動する。完全削除になりそうな場合は Windows の警告で止められる。"""
    import ctypes
    from ctypes import wintypes

    root = os.path.splitdrive(os.path.abspath(path))[0] + "\\"

    class SHQUERYRBINFO(ctypes.Structure):
        _fields_ = [("cbSize", wintypes.DWORD), ("i64Size", ctypes.c_longlong),
                    ("i64NumItems", ctypes.c_longlong)]

    info = SHQUERYRBINFO(ctypes.sizeof(SHQUERYRBINFO), 0, 0)
    if ctypes.windll.shell32.SHQueryRecycleBinW(root, ctypes.byref(info)) != 0:
        raise TrashError(f"{root} にはごみ箱がないため移動できません（完全削除は行いません）")

    class SHFILEOPSTRUCTW(ctypes.Structure):
        _fields_ = [("hwnd", wintypes.HWND), ("wFunc", wintypes.UINT), ("pFrom", wintypes.LPCWSTR),
                    ("pTo", wintypes.LPCWSTR), ("fFlags", ctypes.c_ushort), ("fAnyOperationsAborted", wintypes.BOOL),
                    ("hNameMappings", ctypes.c_void_p), ("lpszProgressTitle", wintypes.LPCWSTR)]

    FO_DELETE, FOF_SILENT, FOF_NOCONFIRMATION, FOF_ALLOWUNDO, FOF_WANTNUKEWARNING = 3, 0x4, 0x10, 0x40, 0x4000
    op = SHFILEOPSTRUCTW(None, FO_DELETE, os.path.abspath(path) + "\0", None,
                         FOF_ALLOWUNDO | FOF_NOCONFIRMATION | FOF_SILENT | FOF_WANTNUKEWARNING, False, None, None)
    rc = ctypes.windll.shell32.SHFileOperationW(ctypes.byref(op))
    if rc != 0:
        raise TrashError(f"ごみ箱へ移動できませんでした（エラーコード {rc}）")
    if op.fAnyOperationsAborted:
        raise TrashError("ごみ箱へ移動できないため中止しました（完全削除は行っていません）")


def default_backend() -> Callable[[str], None]:
    if sys.platform == "win32":
        return _windows_recycle

    def unsupported(path: str) -> None:
        raise TrashError("ごみ箱への移動は Windows でのみ利用できます")
    return unsupported


def check(db: Database, media_id: int, ctx: candidates.Context | None = None) -> str | None:
    """移動してよいか確認する。問題があれば理由を返す。"""
    row = db.video(media_id)
    if row is None or row["missing"]:
        return "ファイルが見つかりません"
    if row["protected"]:
        return "「絶対に残す」が付いています"
    if row["cloud_only"]:
        return "オンライン専用ファイルはこのアプリでは移動しません"
    try:
        size, mtime_ns = media.file_state(row["path"])
    except OSError as e:
        return f"ファイルを確認できません（{e.strerror or e}）"
    if size != row["size"] or abs(mtime_ns / 1e9 - row["mtime"]) > 1e-6:
        return "スキャンした後にファイルが変更されています。もう一度確認してください"
    return None


def _intact(db: Database, media_id: int) -> bool:
    """スキャン時と同じ状態でディスクに残っているか。"""
    row = db.video(media_id)
    if row is None or row["missing"] or row["cloud_only"]:
        return False
    try:
        size, mtime_ns = media.file_state(row["path"])
    except OSError:
        return False
    return size == row["size"] and abs(mtime_ns / 1e9 - row["mtime"]) <= 1e-6


def survivors_check(db: Database, ctx: candidates.Context, ids: list[int]) -> set[int]:
    """重複グループの最後の1本を失わないための確認。移動を止める ID を返す。

    移動しないコピーのうち、実際にディスクに残っていて変更されていないものが1本もなければ、
    そのグループの「残す1本」（選ばれていなければ選ばれたうちの1本）を移動しない。
    """
    chosen = set(ids)
    # 移動するファイルの実体（別の表記・ハードリンクでも同じ値になる）
    moving = {candidates.file_identity(db.video(m)["path"]) for m in chosen if db.video(m) is not None}
    blocked = set()
    for h, members in ctx.dup_groups.items():
        selected = [m for m in members if m in chosen]
        if not selected:
            continue
        if any(_intact(db, m) and candidates.file_identity(db.video(m)["path"]) not in moving
               for m in members if m not in chosen):
            continue
        keep = ctx.dup_keep[h][0]
        blocked.add(keep if keep in chosen else selected[0])
    return blocked


def move_to_trash(db: Database, ids: list[int], backend: Callable[[str], None] | None = None) -> list[TrashResult]:
    """確認済みのファイルをごみ箱へ移動する。1件ずつ確認・移動し、結果を返す。"""
    backend = backend or default_backend()
    ctx = candidates.Context(db)
    blocked = survivors_check(db, ctx, ids)
    results = []
    moved_identities: set = set()
    for media_id in ids:
        row = db.video(media_id)
        if media_id in ctx.aliases:  # 同じ実体が別の表記で登録されたもの
            results.append(TrashResult(media_id, row["path"], row["size"], False,
                                       "同じファイルが別の表記で二重に登録されているため、移動しませんでした"))
            continue
        ident = candidates.file_identity(row["path"]) if row else None
        if ident in moved_identities:
            results.append(TrashResult(media_id, row["path"], row["size"], False,
                                       "同じファイルをすでに移動しています"))
            continue
        if media_id in blocked:
            results.append(TrashResult(media_id, row["path"], row["size"], False,
                                       "同じ内容のファイルがほかに残っていないため、このファイルは移動しませんでした"
                                       "（重複の最後の1本を残します）"))
            continue
        problem = check(db, media_id, ctx)
        if problem:
            results.append(TrashResult(media_id, row["path"] if row else "", row["size"] if row else 0, False, problem))
            continue
        try:
            backend(row["path"])
        except Exception as e:  # 失敗しても完全削除には切り替えない
            results.append(TrashResult(media_id, row["path"], row["size"], False, str(e)))
            db.log_trash(row["path"], row["size"], "error", str(e))
            continue
        if os.path.exists(row["path"]):
            results.append(TrashResult(media_id, row["path"], row["size"], False,
                                       "移動を確認できませんでした（ファイルが残っています）"))
            db.log_trash(row["path"], row["size"], "error", "移動後もファイルが残っている")
            continue
        moved_identities.add(ident)
        db.mark_trashed(media_id)
        db.log_trash(row["path"], row["size"], "moved", None)
        results.append(TrashResult(media_id, row["path"], row["size"], True, "ごみ箱へ移動しました"))
    return results

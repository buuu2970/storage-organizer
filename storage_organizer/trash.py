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

from . import candidates, media, recycle_bin
from .recycle_bin import TrashError  # noqa: F401  （呼び出し側は trash.TrashError を使う）
from .store import Store as Database


@dataclass
class TrashResult:
    media_id: int
    path: str
    size: int
    ok: bool
    message: str
    status: str = ""  # moved（移動した） / skipped（安全確認で止めた） / failed（ごみ箱への移動に失敗）
    record_error: str = ""  # 移動はできたが、その後の記録の保存に失敗した内容

    def __post_init__(self) -> None:
        if not self.status:
            self.status = "moved" if self.ok else "skipped"


def _windows_recycle(path: str) -> None:
    """Windows のごみ箱へ移動する（実体は recycle_bin.recycle）。

    完全削除になりそうな場合は移動せずに TrashError にする。確実にごみ箱へ移せたと確認できない場合もエラー。
    COM はこの中で初期化・終了するが、別スレッドから呼ぶ場合は windows_com_thread で囲んでおくとよい。
    """
    recycle_bin.recycle(path)


class windows_com_thread:
    """別スレッドで Windows のごみ箱を使う前後に、COM を初期化・終了する（Windows 以外では何もしない）。"""

    def __enter__(self):
        self._init = False
        if sys.platform == "win32":
            import ctypes
            hr = ctypes.windll.ole32.CoInitializeEx(None, 0x2)  # COINIT_APARTMENTTHREADED
            self._init = hr in (0, 1)  # S_OK / S_FALSE（すでに初期化済み）
        return self

    def __exit__(self, *exc):
        if self._init:
            import ctypes
            ctypes.windll.ole32.CoUninitialize()
        return False


def default_backend() -> Callable[[str], None]:
    """ごみ箱への移動処理。Windows + pywin32 でなければ、常にエラーにする関数を返す（完全削除へは切り替えない）。"""
    if recycle_bin.available() is None:
        return _windows_recycle
    return recycle_bin.backend()


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
    from .autoscan import AMBIGUOUS_VIDEO_EXTS, is_mpeg_ts
    if row["ext"] in AMBIGUOUS_VIDEO_EXTS and not is_mpeg_ts(row["path"]):
        return "録画ではないファイル（ソースコードなど）の可能性があるため移動しません"
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


def similar_check(db: Database, ctx: candidates.Context, ids: list[int]) -> set[int]:
    """類似録画は、移動しない別の1本が「残す」か保護として確定していて、実在・未変更のときだけ移動できる。"""
    chosen = set(ids)
    moving = {candidates.file_identity(db.video(m)["path"]) for m in chosen if db.video(m) is not None}
    blocked = set()
    for members in ctx.video_groups:
        selected = [m for m in members if m in chosen]
        if not selected:
            continue
        keepers = [m for m in members if m not in chosen and _intact(db, m)
                   and (db.video(m)["protected"] or db.video(m)["decision"] == "残す")
                   and candidates.file_identity(db.video(m)["path"]) not in moving]
        if not keepers:
            blocked.update(selected)
    return blocked


def _stat(path: str):
    try:
        st = os.stat(path)
        return st.st_size, st.st_mtime_ns
    except OSError:
        return None


def move_to_trash(db: Database, ids: list[int], backend: Callable[[str], None] | None = None,
                  progress: Callable[[int, int, str], None] | None = None, cancel=None,
                  expected_generation: int | None = None) -> list[TrashResult]:
    """確認済みのファイルをごみ箱へ移動する。1件ずつ確認・移動し、結果を返す。"""
    backend = backend or default_backend()
    def stop_all(message):
        return [TrashResult(m, (db.video(m) or {}).get("path", ""), (db.video(m) or {}).get("size", 0), False, message)
                for m in ids]
    if getattr(db, "scan_active", False):  # 調査と整理は同時に行わない（処理側でも防ぐ）
        return stop_all("調査中のため移動しませんでした。調査が終わってから、もう一度確認してください")
    if expected_generation is not None and getattr(db, "generation", 0) != expected_generation:
        return stop_all("確認したあとに調査結果が更新されました。内容が変わった可能性があるため、もう一度確認してください")
    if not getattr(db, "protection_ok", True):  # 保護情報が読めない状態で「保護なし」として整理しない
        return [TrashResult(m, (db.video(m) or {}).get("path", ""), (db.video(m) or {}).get("size", 0), False,
                            "保護・判断の記録を読み込めなかったため、整理操作を止めています") for m in ids]
    ctx = candidates.Context(db)
    blocked = survivors_check(db, ctx, ids)
    similar_blocked = similar_check(db, ctx, ids)
    results = []
    moved_identities: set = set()
    start_generation = getattr(db, "generation", 0)
    # 確認時点の記録: グループに関わる全ファイルの、いまのディスク上のサイズと更新日時（ナノ秒）。
    # 保存されている情報が整理中に再スキャンで書き換わっても、この記録と比べて変化を見つける
    involved = set(ids)
    for m in ids:
        h = ctx.dup_of.get(m)
        if h is not None:
            involved |= set(ctx.dup_groups[h])
        n_ = ctx.vgroup_of.get(m)
        if n_ is not None:
            involved |= set(ctx.video_groups[n_])
    snapshot = {}
    for m in involved:
        row = db.video(m)
        if row is None:
            continue
        st = _stat(row["path"])
        # 保存された情報（確認の根拠）とディスクがすでに違うものは、最初から「変更あり」として扱う
        if st is None or st[0] != row["size"] or abs(st[1] / 1e9 - row["mtime"]) > 1e-6:
            snapshot[m] = None
        else:
            snapshot[m] = (row["path"], st)
    if hasattr(db, "organize_active"):
        db.organize_active = True
    try:
        return _move_all(db, ctx, ids, backend, progress, cancel, blocked, similar_blocked, snapshot,
                         start_generation, results, moved_identities)
    finally:
        if hasattr(db, "organize_active"):
            db.organize_active = False


def _move_all(db, ctx, ids, backend, progress, cancel, blocked, similar_blocked, snapshot, start_generation,
              results, moved_identities):
    # 実際に移動しようとするファイル（最後の1本として残すと決めたものは除く）
    will_move = set(ids) - blocked - similar_blocked
    moving_idents = {candidates.file_identity(db.video(m)["path"]) for m in will_move if db.video(m) is not None}
    for n, media_id in enumerate(ids):
        row = db.video(media_id)
        if cancel is not None and cancel.is_set():  # ファイル単位の区切りで止める。残りは「中断」として結果に残す
            results.append(TrashResult(media_id, row["path"] if row else "", row["size"] if row else 0, False,
                                       "中断したため移動しませんでした"))
            continue
        if getattr(db, "generation", 0) != start_generation or getattr(db, "scan_active", False):
            results.append(TrashResult(media_id, row["path"] if row else "", row["size"] if row else 0, False,
                                       "整理中に調査結果が更新されたため、残りは移動しませんでした（もう一度確認してください）"))
            continue
        try:
            results.append(_move_one(db, ctx, media_id, row, n, len(ids), backend, progress, blocked,
                                     similar_blocked, will_move, moving_idents, moved_identities, snapshot))
        except Exception as e:  # noqa: BLE001  1件の想定外の失敗で、それまでの結果を失わない
            results.append(TrashResult(media_id, row["path"] if row else "", row["size"] if row else 0, False,
                                       f"想定外のエラーのため移動しませんでした（{e}）", "failed"))
    return results


def _snap_intact(snapshot: dict, m: int) -> bool:
    """確認時点の記録と、いまのディスクが一致するか（保存されている情報には頼らない）。"""
    rec = snapshot.get(m)
    return rec is not None and _stat(rec[0]) == rec[1]


def _keeper_ok(db: Database, ctx: candidates.Context, media_id: int, will_move: set, moving_idents: set,
               snapshot: dict | None = None) -> str | None:
    """移動の直前に、同じグループの「残す側」が実在・未変更かを確かめる。問題があれば理由を返す。"""
    intact = (lambda m: _snap_intact(snapshot, m)) if snapshot is not None else (lambda m: _intact(db, m))
    h = ctx.dup_of.get(media_id)
    if h is not None:
        others = [m for m in ctx.dup_groups[h] if m != media_id and m not in will_move]
        if not any(intact(m) and candidates.file_identity(db.video(m)["path"]) not in moving_idents for m in others):
            return ("同じ内容の残すファイルが見つからない・変更されたため、移動しませんでした"
                    "（重複の最後の1本を残します）")
    n = ctx.vgroup_of.get(media_id)
    if n is not None:
        keepers = [m for m in ctx.video_groups[n] if m != media_id and m not in will_move
                   and (db.video(m)["protected"] or db.video(m)["decision"] == "残す")]
        if not any(intact(m) and candidates.file_identity(db.video(m)["path"]) not in moving_idents for m in keepers):
            return "類似録画の残す録画が見つからない・変更されたため、移動しませんでした"
    return None


def _move_one(db, ctx, media_id, row, n, total, backend, progress, blocked, similar_blocked, will_move,
              moving_idents, moved_identities, snapshot=None) -> TrashResult:
    if progress is not None:
        progress(n, total, row["name"] if row else "")
    if row is None:
        return TrashResult(media_id, "", 0, False, "一覧にないファイルです")
    if media_id in ctx.aliases:  # 同じ実体が別の表記で登録されたもの
        return TrashResult(media_id, row["path"], row["size"], False,
                           "同じファイルが別の表記で二重に登録されているため、移動しませんでした")
    ident = candidates.file_identity(row["path"])
    if ident in moved_identities:
        return TrashResult(media_id, row["path"], row["size"], False, "同じファイルをすでに移動しています")
    if media_id in similar_blocked:
        return TrashResult(media_id, row["path"], row["size"], False,
                           "類似録画は、比較して残す1本を選んでから移動できます（残す録画が見つからない・変更された場合も移動しません）")
    if media_id in blocked:
        return TrashResult(media_id, row["path"], row["size"], False,
                           "同じ内容のファイルがほかに残っていないため、このファイルは移動しませんでした（重複の最後の1本を残します）")
    problem = check(db, media_id, ctx) or _keeper_ok(db, ctx, media_id, will_move, moving_idents, snapshot)
    if not problem and snapshot is not None and media_id in snapshot and not _snap_intact(snapshot, media_id):
        problem = "確認したあとにファイルが変更されています。もう一度確認してください"
    if problem:  # 存在・変更・保護と、残す側を移動の直前に確かめる
        return TrashResult(media_id, row["path"], row["size"], False, problem)
    try:
        backend(row["path"])
    except Exception as e:  # 失敗しても完全削除には切り替えない
        _safe_log(db, row, "error", str(e))
        return TrashResult(media_id, row["path"], row["size"], False, str(e), "failed")
    if os.path.exists(row["path"]):
        _safe_log(db, row, "error", "移動後もファイルが残っている")
        return TrashResult(media_id, row["path"], row["size"], False,
                           "移動を確認できませんでした（ファイルが残っています）", "failed")
    moved_identities.add(ident)
    # ここから先は「移動した」ことが確定。記録の失敗は、移動の結果とは分けて伝える
    errors = []
    try:
        db.mark_trashed(media_id)
    except Exception as e:  # noqa: BLE001
        errors.append(f"状態の保存: {e}")
    if not _safe_log(db, row, "moved", None):
        errors.append("移動の記録")
    message = "ごみ箱へ移動しました"
    if errors:
        message += f"（ただし記録の保存に失敗しました: {' / '.join(errors)}。次の調査で反映されます）"
    return TrashResult(media_id, row["path"], row["size"], True, message, record_error=" / ".join(errors))


def _safe_log(db, row, result: str, message: str | None) -> bool:
    try:
        db.log_trash(row["path"], row["size"], result, message)
        return True
    except Exception:  # noqa: BLE001  記録の失敗で、移動の結果を失わない
        return False

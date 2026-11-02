"""デスクトップの「削除予定」フォルダへの移動

このアプリに許可している操作は「削除予定フォルダの作成」と「そこへの動画の移動」だけ。
削除予定フォルダの中身をごみ箱へ移す・削除するのは利用者自身が行う (アプリには削除機能を持たせない)。
"""

from __future__ import annotations

import ctypes
import shutil
import sys
import uuid
from pathlib import Path

PENDING_DIR_NAME = "削除予定"

# FOLDERID_Desktop {B4BFCC3A-DB2C-424C-B029-7FE99A87C641}
_FOLDERID_DESKTOP = uuid.UUID("B4BFCC3A-DB2C-424C-B029-7FE99A87C641")


def desktop_dir() -> Path:
    """実際のデスクトップの場所 (OneDrive にリダイレクトされている場合も正しく取得)。"""
    if sys.platform == "win32":
        guid = (ctypes.c_byte * 16).from_buffer_copy(_FOLDERID_DESKTOP.bytes_le)
        out = ctypes.c_wchar_p()
        if ctypes.windll.shell32.SHGetKnownFolderPath(ctypes.byref(guid), 0, None, ctypes.byref(out)) == 0:
            try:
                return Path(out.value)
            finally:
                ctypes.windll.ole32.CoTaskMemFree(out)
    return Path.home() / "Desktop"


def pending_dir() -> Path:
    return desktop_dir() / PENDING_DIR_NAME


def _free_name(dest_dir: Path, name: str) -> Path:
    """同名ファイルがあれば「名前_1.mp4」のように番号を付ける (上書きは絶対にしない)。"""
    dest = dest_dir / name
    stem, suffix = Path(name).stem, Path(name).suffix
    n = 1
    while dest.exists():
        dest = dest_dir / f"{stem}_{n}{suffix}"
        n += 1
    return dest


def move_to_pending(src: Path, dest_dir: Path | None = None) -> Path:
    """動画を削除予定フォルダへ移動し、移動先のパスを返す。フォルダが無ければ作る。"""
    src = Path(src)
    if not src.is_file():
        raise FileNotFoundError(f"ファイルが見つかりません: {src}")
    dest_dir = Path(dest_dir) if dest_dir else pending_dir()
    dest_dir.mkdir(parents=True, exist_ok=True)
    if src.resolve().parent == dest_dir.resolve():
        raise ValueError("すでに削除予定フォルダにあります")
    dest = _free_name(dest_dir, src.name)
    try:
        shutil.move(str(src), str(dest))
    except PermissionError as e:
        raise PermissionError("移動できませんでした。動画が再生中などで使用されている可能性があります") from e
    return dest

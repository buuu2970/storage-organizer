"""ffmpeg / ffprobe の検出と実行"""

from __future__ import annotations

import os
import shutil
import subprocess
from functools import lru_cache
from pathlib import Path


class FFmpegNotFound(RuntimeError):
    pass


@lru_cache(maxsize=None)
def find_tool(name: str) -> str:
    """PATH → winget のインストール先 の順で探す。"""
    found = shutil.which(name)
    if found:
        return found
    pkg_root = Path(os.environ.get("LOCALAPPDATA", "")) / "Microsoft" / "WinGet" / "Packages"
    if pkg_root.is_dir():
        for exe in pkg_root.glob(f"*FFmpeg*/**/bin/{name}.exe"):
            return str(exe)
    raise FFmpegNotFound(f"{name} が見つかりません。ffmpeg をインストールしてください (例: winget install Gyan.FFmpeg)")


def ffmpeg() -> str:
    return find_tool("ffmpeg")


def ffprobe() -> str:
    return find_tool("ffprobe")


def run(cmd: list[str], timeout: int = 600) -> subprocess.CompletedProcess:
    # CREATE_NO_WINDOW: GUI から呼んだときに黒い窓が出ないようにする
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    return subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace",
                          timeout=timeout, creationflags=flags)

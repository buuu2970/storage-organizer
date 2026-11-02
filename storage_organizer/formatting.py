"""画面表示用の書式変換。"""

from __future__ import annotations

from datetime import datetime

_UNITS = ("KB", "MB", "GB", "TB", "PB")


def format_size(num_bytes: int) -> str:
    """バイト数を読みやすい単位に変換する（エクスプローラーと同じく 1KB = 1024B）。"""
    if num_bytes < 1024:
        return f"{num_bytes} B"
    value = float(num_bytes)
    for unit in _UNITS:
        value /= 1024
        if value < 1024 or unit == _UNITS[-1]:
            return f"{value:,.1f} {unit}"
    raise AssertionError("unreachable")


def format_bytes_exact(num_bytes: int) -> str:
    return f"{num_bytes:,} バイト"


def format_datetime(timestamp: float) -> str:
    try:
        return datetime.fromtimestamp(timestamp).strftime("%Y-%m-%d %H:%M")
    except (OverflowError, OSError, ValueError):
        return "不明"


def format_duration(seconds: float) -> str:
    seconds = int(seconds)
    minutes, sec = divmod(seconds, 60)
    if minutes:
        return f"{minutes}分{sec}秒"
    return f"{sec}秒"

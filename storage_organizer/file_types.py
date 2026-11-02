"""拡張子からファイルの種類を判定する。

ファイルの中身は読まず、拡張子だけで分類する。
"""

from __future__ import annotations

import os

_CATEGORY_EXTENSIONS: dict[str, tuple[str, ...]] = {
    "動画": (
        ".mp4", ".mkv", ".avi", ".mov", ".wmv", ".flv", ".webm", ".m4v",
        ".mpg", ".mpeg", ".ts", ".m2ts", ".3gp",
    ),
    "画像": (
        ".jpg", ".jpeg", ".png", ".gif", ".bmp", ".webp", ".heic", ".heif",
        ".tif", ".tiff", ".svg", ".ico", ".raw", ".cr2", ".nef", ".arw", ".psd",
    ),
    "音声": (
        ".mp3", ".wav", ".flac", ".aac", ".m4a", ".ogg", ".wma", ".opus",
    ),
    "文書": (
        ".pdf", ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx", ".txt",
        ".md", ".csv", ".rtf", ".odt", ".ods", ".odp", ".epub",
    ),
    "圧縮": (
        ".zip", ".rar", ".7z", ".tar", ".gz", ".bz2", ".xz", ".lzh", ".cab",
    ),
    "インストーラ・ディスクイメージ": (
        ".exe", ".msi", ".msix", ".appx", ".iso", ".img", ".dmg", ".vhd", ".vhdx",
    ),
}

OTHER_CATEGORY = "その他"

_EXTENSION_TO_CATEGORY: dict[str, str] = {
    ext: category
    for category, extensions in _CATEGORY_EXTENSIONS.items()
    for ext in extensions
}


def categorize(filename: str) -> str:
    """ファイル名の拡張子から種類名を返す。該当しなければ「その他」。"""
    ext = os.path.splitext(filename)[1].lower()
    return _EXTENSION_TO_CATEGORY.get(ext, OTHER_CATEGORY)

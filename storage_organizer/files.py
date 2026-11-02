"""ファイル探索と重複検出"""

from __future__ import annotations

import hashlib
import os
from collections.abc import Iterable
from pathlib import Path

# 探索しないフォルダ (システム・アプリ領域など。小文字で比較)
SKIP_DIRS = {"windows", "program files", "program files (x86)", "programdata", "appdata", "$recycle.bin",
             "system volume information", "recovery", "perflogs", "node_modules", ".git", "選別結果",
             "削除予定"}  # 移動済みの動画を再スキャンで拾わない


def default_folders() -> list[str]:
    home = Path.home()
    cands = [home / "Videos", home / "Desktop", home / "Downloads", home / "Pictures", home / "OneDrive"]
    return [str(p) for p in cands if p.is_dir()]


def find_files(folders: Iterable[str | Path], exts: set[str], recursive: bool = True,
               exclude: Path | None = None) -> list[Path]:
    """フォルダ内から拡張子が exts のファイルを集める (重複パスは除く)。"""
    seen: set[str] = set()
    found: list[Path] = []
    exclude_key = os.path.normcase(str(exclude.resolve())) if exclude else None
    for folder in folders:
        for root, dirs, files in os.walk(folder):
            if not recursive:
                dirs[:] = []
            dirs[:] = [d for d in dirs if d.lower() not in SKIP_DIRS and not d.startswith(".")
                       and os.path.normcase(str((Path(root) / d).resolve())) != exclude_key]
            for name in files:
                p = Path(root) / name
                if p.suffix.lower() not in exts:
                    continue
                key = os.path.normcase(str(p.resolve()))
                if key not in seen:
                    seen.add(key)
                    found.append(p)
    return sorted(found)


def quick_hash(p: Path, chunk: int = 1 << 20) -> str:
    """サイズ + 先頭1MB + 末尾1MB のハッシュ。同サイズのファイル同士の同一判定に使う。"""
    size = p.stat().st_size
    h = hashlib.sha1(str(size).encode())
    with p.open("rb") as f:
        h.update(f.read(chunk))
        f.seek(max(size - chunk, 0))
        h.update(f.read(chunk))
    return h.hexdigest()


def find_duplicates(files: Iterable[Path]) -> dict[Path, Path]:
    """{重複コピー: 残すべき元ファイル} を返す。元ファイル = 更新日時が最も古いもの。"""
    by_size: dict[int, list[Path]] = {}
    for p in files:
        try:
            by_size.setdefault(p.stat().st_size, []).append(p)
        except OSError:
            pass
    dups: dict[Path, Path] = {}
    for group in (g for g in by_size.values() if len(g) > 1):
        by_hash: dict[str, list[Path]] = {}
        for p in group:
            try:
                by_hash.setdefault(quick_hash(p), []).append(p)
            except OSError:
                pass
        for same in (g for g in by_hash.values() if len(g) > 1):
            same.sort(key=lambda x: (x.stat().st_mtime, len(str(x))))
            for copy in same[1:]:
                dups[copy] = same[0]
    return dups

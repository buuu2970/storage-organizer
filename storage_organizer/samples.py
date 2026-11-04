"""初期学習の見本動画（全プロファイル共通の12本）。

- 見本の動画はリポジトリに含めない（動画本体を Git に入れない。公開・配布前に出典と利用許諾の確認が必要）。
  既定ではアプリのフォルダの sample_videos、または設定で指定したフォルダから読む
- 見本は通常の動画一覧に入れない（調査の対象外にする）。見本の回答は厳選 AI に混ぜない
- 見本の特徴（長さ・明るさなど）は FFmpeg で調べ、共通のキャッシュ（sample_features.json）に保存する
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from . import candidates, media
from .file_types import categorize
from .store import Store, StoreError, _write_json

SAMPLE_COUNT = 12
DEFAULT_DIR = Path(__file__).resolve().parent.parent / "sample_videos"


def sample_dir(store: Store) -> Path:
    custom = store.get_setting("sample_dir")
    return Path(custom) if custom else DEFAULT_DIR


def list_samples(folder: Path) -> list[Path]:
    """見本の動画（名前順に最大12本）。フォルダがなければ空。"""
    try:
        files = sorted((p for p in Path(folder).iterdir() if p.is_file() and categorize(p.name) == "動画"),
                       key=lambda p: p.name)
    except OSError:
        return []
    return files[:SAMPLE_COUNT]


def _cache_file(store: Store) -> Path:
    return store.data_dir / "sample_features.json"


def _key(p: Path) -> str:
    st = p.stat()
    return f"{p.name}|{st.st_size}|{st.st_mtime_ns}"


def load_cache(store: Store) -> dict:
    try:
        with open(_cache_file(store), encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def sample_rows(store: Store) -> dict[str, dict]:
    """見本ごとの「動画の行」（ai_curation.features に渡せる形）。特徴を調べていない見本は容量だけ。"""
    cache = load_cache(store)
    out = {}
    for p in list_samples(sample_dir(store)):
        try:
            st = p.stat()
            key = _key(p)
        except OSError:
            continue
        c = cache.get(key, {})
        out[p.name] = {"name": p.name, "path": str(p), "size": st.st_size, "mtime": None,
                       "meta": c.get("meta"), "features": c.get("features")}
    return out


def analyze_samples(store: Store, tools: media.FFmpegTools, cancel=None, report=None) -> dict:
    """見本の特徴を調べる（まだ調べていない・変わった見本だけ）。結果: {analyzed, failed, skipped}"""
    result = {"analyzed": 0, "failed": 0, "skipped": 0}
    if not tools.available:
        result["skipped"] = len(list_samples(sample_dir(store)))
        return result
    cache = load_cache(store)
    files = list_samples(sample_dir(store))
    for i, p in enumerate(files):
        if cancel is not None and cancel.is_set():
            break
        try:
            key = _key(p)
        except OSError:
            result["failed"] += 1
            continue
        if key in cache:
            continue
        if report:
            report({"text": f"見本の動画を調べています（{i + 1} / {len(files)}）: {p.name}", "done": i, "total": len(files)})
        try:
            r = candidates.analyze_video_light(tools, str(p), cancel)
        except media.Cancelled:
            break
        except (OSError, RuntimeError, TimeoutError):
            result["failed"] += 1
            continue
        if r.get("meta"):
            cache[key] = {"meta": r["meta"], "features": r.get("features")}
            result["analyzed"] += 1
        else:
            result["failed"] += 1
    try:
        _write_json(_cache_file(store), cache)
    except StoreError:
        result["failed"] += 1
    return result


def excluded_paths(store: Store) -> list[str]:
    """調査の対象外にする見本のフォルダ。"""
    d = sample_dir(store)
    return [os.path.normpath(str(d))] if d.is_dir() else []

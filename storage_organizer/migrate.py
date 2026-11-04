"""以前の版（SQLite の recsort.db）からの移行。設定画面のボタンを押したときだけ使う。

通常の動作では SQLite を読み込まない（このモジュールも import しない）。
移行するのは設定・保護・判断・確認日時だけ。調査結果は新しく調べ直す。旧 DB は削除しない。
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

from .store import Store

SETTING_KEYS = ("ffmpeg_dir", "auto_scan_on_start", "extra_locations", "excluded_folders", "disabled_locations",
                "view_history_started")


def legacy_db_path() -> Path:
    if sys.platform == "win32":
        base = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local"))
    else:
        base = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share"))
    return base / "RecSortAI" / "recsort.db"


def migrate_from_legacy(db_path: Path | str, store: Store) -> dict:
    """旧 DB を読み取り専用で開いて移行する。戻り値: 移行した件数。"""
    import sqlite3  # 移行するときだけ読み込む
    uri = Path(db_path).resolve().as_uri() + "?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    conn.row_factory = sqlite3.Row
    try:
        settings = 0
        for row in conn.execute("SELECT key, value FROM settings"):
            if row["key"] in SETTING_KEYS and row["value"] is not None:
                value = row["value"]
                if row["key"] in ("extra_locations", "excluded_folders", "disabled_locations"):
                    store.set_json_list(row["key"], json.loads(value) if value else [])
                else:
                    store.set_setting(row["key"], value)
                settings += 1
        cols = {r[1] for r in conn.execute("PRAGMA table_info(videos)")}
        picks = [c for c in ("protected", "decision", "favorite", "last_viewed") if c in cols]
        entries = {}
        for row in conn.execute(f"SELECT path, {', '.join(picks)} FROM videos"):
            entry = {k: row[k] for k in picks if row[k] not in (None, 0)}
            if entry.get("decision") not in (None, "残す", "削除候補", "保留"):
                entry.pop("decision", None)
            if entry:
                entries[os.path.normpath(row["path"])] = entry
        store.import_user_state(entries)
        store.set_setting("legacy_migrated", str(db_path))
        return {"settings": settings, "files": len(entries)}
    finally:
        conn.close()

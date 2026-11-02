"""SQLite によるローカルデータ管理。

動画ファイル自体は保存せず、メタデータ・分類・解析結果だけを保存する。
スレッドごとに Database を作って使う（sqlite3 の接続はスレッドをまたげない）。
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
import sys
import time
import unicodedata
from pathlib import Path

DEFAULT_GENRES = [
    "ゲーム", "授業・学習", "会議・仕事", "アニメ・テレビ", "配信・実況",
    "音楽・歌唱", "音声・ラジオ", "個人撮影", "その他・不明",
]
# 「その他・不明」は中身がばらばらなので学習対象のクラスにしない（名前変更に備え、フラグで管理）
EXCLUDED_GENRE = "その他・不明"
DEFAULT_TAGS = [
    "歌唱あり", "会話あり", "BGMあり", "字幕あり", "黒画面", "静止画が多い",
    "画面録画", "カメラ撮影", "長時間", "無音区間が多い",
]

ANALYSIS_STATES = ("未解析", "解析中", "完了", "一部失敗", "失敗")
CURATION_CHOICES = ("残す", "整理候補", "保留")
TRAINABLE_CHOICES = ("残す", "整理候補")  # 「保留」は教師データに使わない
CLASSIFY_SCOPE = "classify"  # ジャンル分類AI
CURATE_ALL_SCOPE = "curate_all"  # 全動画厳選AI
# 初期12本の回答状態（「未回答」は行を持たない）
UNANSWERED, ANSWERED, SKIPPED = "未回答", "回答済み", "スキップ"
# 初期学習の進捗（settings の initial_state。None = 未実施）
INITIAL_STATES = {None: "未実施", "in_progress": "途中", "done": "完了", "skipped": "スキップ"}
RESERVED_GENRE_NAMES = ("未分類",)  # 未分類は分類状態であり、ジャンルとして作らない


def normalize_genre_name(name: str) -> str:
    """重複確認用の正規化（全角半角・大文字小文字・空白や区切り記号の違いを無視）。"""
    s = unicodedata.normalize("NFKC", name).lower()
    return re.sub(r"[\s・･/,、。.\-‐−_&＆]+", "", s)


def curate_genre_scope(genre_id: int) -> str:
    """ジャンル別厳選AIの学習範囲名。"""
    return f"curate_genre:{genre_id}"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS videos (
    id INTEGER PRIMARY KEY,
    path TEXT NOT NULL UNIQUE,
    name TEXT NOT NULL,
    folder TEXT NOT NULL,
    size INTEGER NOT NULL,
    mtime REAL NOT NULL,
    ext TEXT NOT NULL,
    cloud_only INTEGER NOT NULL DEFAULT 0,
    missing INTEGER NOT NULL DEFAULT 0,
    favorite INTEGER NOT NULL DEFAULT 0,
    protected INTEGER NOT NULL DEFAULT 0,
    analysis_state TEXT NOT NULL DEFAULT '未解析',
    analysis_error TEXT,
    meta TEXT,          -- JSON: duration, width, height, fps, bitrate, vcodec, has_audio
    features TEXT,      -- JSON: 軽量特徴量
    thumbs TEXT,        -- JSON: サムネイル画像パス（アプリのキャッシュ内）
    sha256 TEXT,
    ai_state TEXT NOT NULL DEFAULT '未判定',   -- 未判定 / 分類済み / 未分類
    ai_genres TEXT,     -- JSON: [{"genre_id":..,"score":..}]
    ai_candidates TEXT, -- JSON: しきい値未満も含む候補
    ai_reason TEXT,
    ai_model_version INTEGER,
    curation TEXT,
    added_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS genres (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL UNIQUE,
    exclude_from_training INTEGER NOT NULL DEFAULT 0,
    sort INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS video_genres (
    video_id INTEGER NOT NULL REFERENCES videos(id) ON DELETE CASCADE,
    genre_id INTEGER NOT NULL REFERENCES genres(id) ON DELETE CASCADE,
    PRIMARY KEY (video_id, genre_id)
);
CREATE TABLE IF NOT EXISTS tags (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL UNIQUE
);
CREATE TABLE IF NOT EXISTS video_tags (
    video_id INTEGER NOT NULL REFERENCES videos(id) ON DELETE CASCADE,
    tag_id INTEGER NOT NULL REFERENCES tags(id) ON DELETE CASCADE,
    source TEXT NOT NULL,          -- manual / ai
    status TEXT NOT NULL,          -- 採用 / 保留 / 却下
    PRIMARY KEY (video_id, tag_id)
);
CREATE TABLE IF NOT EXISTS training (
    video_id INTEGER PRIMARY KEY REFERENCES videos(id) ON DELETE CASCADE,
    source TEXT NOT NULL,          -- 初期学習 / 修正 / 未分類一覧
    labeled_at REAL NOT NULL,
    model_version INTEGER          -- NULL = まだ学習に反映していない
);
CREATE TABLE IF NOT EXISTS models (
    version INTEGER PRIMARY KEY,
    created_at REAL NOT NULL,
    n_samples INTEGER NOT NULL,
    file TEXT NOT NULL,
    evaluation TEXT,
    active INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS curation_labels (
    id INTEGER PRIMARY KEY,
    video_id INTEGER NOT NULL REFERENCES videos(id) ON DELETE CASCADE,
    scope TEXT NOT NULL,           -- all（全動画厳選） / genre（ジャンル別厳選）
    genre_id INTEGER,              -- scope=genre のとき。ジャンル削除後も履歴は残す
    label TEXT NOT NULL,           -- 残す / 整理候補 / 保留
    source TEXT NOT NULL,          -- all / common（共通回答） / individual（ジャンル別の個別回答） / migrated
    active INTEGER NOT NULL DEFAULT 1,       -- 1 = 現在有効な回答（同じ動画・学習範囲で1つ）
    needs_review INTEGER NOT NULL DEFAULT 0, -- 1 = ジャンル変更による再確認待ち（学習に使わない）
    created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_curation_active ON curation_labels(scope, genre_id, active);
CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT
);
-- 初期12本（共通サンプル）への回答。動画本体やパスは持たず、サンプル ID で参照する
CREATE TABLE IF NOT EXISTS sample_answers (
    sample_id TEXT PRIMARY KEY,
    status TEXT NOT NULL,          -- 回答済み / スキップ
    content_hash TEXT,             -- 回答したときの動画の簡易ハッシュ（入れ替わり検出用）
    updated_at REAL NOT NULL,
    model_version INTEGER          -- NULL = まだ学習に反映していない
);
CREATE TABLE IF NOT EXISTS sample_answer_genres (
    sample_id TEXT NOT NULL REFERENCES sample_answers(sample_id) ON DELETE CASCADE,
    genre_id INTEGER NOT NULL REFERENCES genres(id) ON DELETE CASCADE,
    PRIMARY KEY (sample_id, genre_id)
);
CREATE TABLE IF NOT EXISTS sample_answer_history (
    id INTEGER PRIMARY KEY,
    sample_id TEXT NOT NULL,
    status TEXT NOT NULL,
    genre_ids TEXT,                -- JSON
    content_hash TEXT,
    created_at REAL NOT NULL
);
"""


def default_data_dir() -> Path:
    override = os.environ.get("RECSORT_DATA_DIR")
    if override:
        return Path(override)
    if sys.platform == "win32":
        base = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local"))
    else:
        base = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share"))
    return base / "RecSortAI"


def _norm(path: str) -> str:
    return os.path.normcase(os.path.abspath(path))


class Database:
    def __init__(self, data_dir: Path | str | None = None) -> None:
        self.data_dir = Path(data_dir) if data_dir else default_data_dir()
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.thumb_dir = self.data_dir / "thumbs"
        self.model_dir = self.data_dir / "models"
        self.thumb_dir.mkdir(exist_ok=True)
        self.model_dir.mkdir(exist_ok=True)
        self.path = self.data_dir / "recsort.db"
        self.conn = sqlite3.connect(self.path, timeout=30)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        self.conn.execute("PRAGMA journal_mode = WAL")
        self.conn.executescript(_SCHEMA)
        self._migrate()
        self._seed()

    def close(self) -> None:
        self.conn.close()

    def _migrate(self) -> None:
        """以前の版のデータベースとの互換性を保つ。"""
        cols = {r[1] for r in self.conn.execute("PRAGMA table_info(models)")}
        if "scope" not in cols:
            self.conn.execute(f"ALTER TABLE models ADD COLUMN scope TEXT NOT NULL DEFAULT '{CLASSIFY_SCOPE}'")
        if "ai_rejected" not in {r[1] for r in self.conn.execute("PRAGMA table_info(videos)")}:
            # ユーザーが却下した AI のジャンル提案（JSON: ジャンルID の配列）
            self.conn.execute("ALTER TABLE videos ADD COLUMN ai_rejected TEXT")
        # 以前の厳選結果（videos.curation）は全動画厳選の回答として1回だけ引き継ぐ
        if self.conn.execute("SELECT value FROM settings WHERE key='curation_migrated'").fetchone() is None:
            now = time.time()
            for vid, label in self.conn.execute(
                    "SELECT id, curation FROM videos WHERE curation IS NOT NULL").fetchall():
                if label in CURATION_CHOICES:
                    self.conn.execute(
                        "INSERT INTO curation_labels(video_id,scope,genre_id,label,source,created_at) "
                        "VALUES (?,'all',NULL,?,'migrated',?)", (vid, label, now))
            self.conn.execute("INSERT INTO settings VALUES ('curation_migrated','1')")
        self.conn.commit()

    def _seed(self) -> None:
        if self.conn.execute("SELECT COUNT(*) FROM genres").fetchone()[0] == 0:
            for i, name in enumerate(DEFAULT_GENRES):
                self.conn.execute(
                    "INSERT INTO genres(name, exclude_from_training, sort) VALUES (?,?,?)",
                    (name, int(name == EXCLUDED_GENRE), i))
            for name in DEFAULT_TAGS:
                self.conn.execute("INSERT OR IGNORE INTO tags(name) VALUES (?)", (name,))
            self.conn.commit()

    # --- 設定 -------------------------------------------------------------
    def get_setting(self, key: str, default: str | None = None) -> str | None:
        row = self.conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
        return row[0] if row else default

    def set_setting(self, key: str, value: str | None) -> None:
        self.conn.execute("INSERT INTO settings(key,value) VALUES(?,?) "
                          "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value))
        self.conn.commit()

    # --- 動画 -------------------------------------------------------------
    def register_videos(self, records, root: str | None = None,
                        complete_scan: bool = False) -> tuple[int, int]:
        """スキャン結果の動画を登録・更新する。(新規件数, 内容変更で解析を無効化した件数)"""
        added = invalidated = 0
        seen: set[str] = set()
        now = time.time()
        for r in records:
            path = os.path.join(r.folder, r.name)
            seen.add(_norm(path))
            row = self.conn.execute("SELECT id,size,mtime FROM videos WHERE path=?",
                                    (path,)).fetchone()
            if row is None:
                self.conn.execute(
                    "INSERT INTO videos(path,name,folder,size,mtime,ext,cloud_only,added_at) "
                    "VALUES (?,?,?,?,?,?,?,?)",
                    (path, r.name, r.folder, r.size, r.mtime, os.path.splitext(r.name)[1].lower(),
                     int(r.cloud_only), now))
                added += 1
            else:
                changed = row["size"] != r.size or abs(row["mtime"] - r.mtime) > 1e-6
                self.conn.execute("UPDATE videos SET missing=0, cloud_only=? WHERE id=?",
                                  (int(r.cloud_only), row["id"]))
                if changed:
                    self._invalidate(row["id"], r.size, r.mtime)
                    invalidated += 1
        if complete_scan and root:
            # 中断なく調べ終えたフォルダにだけ「見つからない」印を付ける（削除はしない）
            prefix = _norm(os.path.join(root, ""))
            for row in self.conn.execute("SELECT id,path FROM videos WHERE missing=0").fetchall():
                p = _norm(row["path"])
                if p.startswith(prefix) and p not in seen:
                    self.conn.execute("UPDATE videos SET missing=1 WHERE id=?", (row["id"],))
        self.conn.commit()
        return added, invalidated

    def _invalidate(self, video_id: int, size: int, mtime: float) -> None:
        """ファイルが変わったので、古い解析結果を無効にする（ユーザーの分類は残す）。"""
        self.conn.execute(
            "UPDATE videos SET size=?, mtime=?, analysis_state='未解析', analysis_error=NULL, "
            "meta=NULL, features=NULL, thumbs=NULL, sha256=NULL, ai_state='未判定', "
            "ai_genres=NULL, ai_candidates=NULL, ai_reason=NULL, ai_model_version=NULL, ai_rejected=NULL "
            "WHERE id=?", (size, mtime, video_id))
        self.conn.execute("DELETE FROM video_tags WHERE video_id=? AND source='ai'", (video_id,))

    def update_file_state(self, video_id: int, size: int, mtime: float) -> bool:
        """実際のファイル状態と DB を比べ、違えば無効化して True を返す。"""
        row = self.video(video_id)
        if row["size"] != size or abs(row["mtime"] - mtime) > 1e-6:
            self._invalidate(video_id, size, mtime)
            self.conn.commit()
            return True
        return False

    def video(self, video_id: int) -> sqlite3.Row:
        return self.conn.execute("SELECT * FROM videos WHERE id=?", (video_id,)).fetchone()

    def videos(self, include_missing: bool = False) -> list[sqlite3.Row]:
        sql = "SELECT * FROM videos" + ("" if include_missing else " WHERE missing=0")
        return self.conn.execute(sql + " ORDER BY id").fetchall()

    def video_ids(self) -> list[int]:
        return [r[0] for r in self.conn.execute("SELECT id FROM videos WHERE missing=0")]

    def set_flag(self, video_id: int, column: str, value: bool) -> None:
        if column not in ("favorite", "protected"):
            raise ValueError(column)
        self.conn.execute(f"UPDATE videos SET {column}=? WHERE id=?", (int(value), video_id))
        self.conn.commit()

    def update_video(self, video_id: int, **fields) -> None:
        allowed = {"analysis_state", "analysis_error", "meta", "features", "thumbs", "sha256",
                   "ai_state", "ai_genres", "ai_candidates", "ai_reason", "ai_model_version"}
        if not fields or set(fields) - allowed:
            raise ValueError(fields)
        for key in ("meta", "features", "thumbs", "ai_genres", "ai_candidates"):
            if key in fields and fields[key] is not None and not isinstance(fields[key], str):
                fields[key] = json.dumps(fields[key], ensure_ascii=False)
        cols = ", ".join(f"{k}=?" for k in fields)
        self.conn.execute(f"UPDATE videos SET {cols} WHERE id=?", (*fields.values(), video_id))
        self.conn.commit()

    def rejected_ai_genres(self, video_id: int) -> set[int]:
        return set(loads(self.video(video_id)["ai_rejected"], []))

    def reject_ai_genres(self, video_id: int) -> None:
        """AI のジャンル提案を却下する。却下したジャンルは同じ動画に再提案しない（教師データにはしない）。"""
        row = self.video(video_id)
        rejected = set(loads(row["ai_rejected"], [])) | {e["genre_id"] for e in loads(row["ai_genres"], [])}
        self.conn.execute(
            "UPDATE videos SET ai_rejected=?, ai_state='未分類', ai_genres='[]', "
            "ai_reason='AIの提案をユーザーが却下しました' WHERE id=?",
            (json.dumps(sorted(rejected)), video_id))
        self.conn.commit()

    # --- 厳選の回答（教師データ） ---------------------------------------------
    def _active_label(self, video_id: int, scope: str, genre_id: int | None):
        return self.conn.execute(
            "SELECT * FROM curation_labels WHERE video_id=? AND scope=? AND genre_id IS ? AND active=1",
            (video_id, scope, genre_id)).fetchone()

    def answer(self, video_id: int, scope: str, genre_id: int | None, label: str | None,
               source: str) -> bool:
        """厳選の回答を記録する。label=None は回答の解除。変更があれば True。

        - 同じ動画・同じ学習範囲で有効な回答は1つ。以前の回答は active=0 で履歴に残す
        - 同じ回答は重複登録しない
        - 共通回答（common）は個別回答（individual）を上書きしない
        - 保護中の動画には回答できない
        """
        if scope not in ("all", "genre") or (scope == "genre") != (genre_id is not None):
            raise ValueError("学習範囲の指定が正しくありません")
        if label is not None and label not in CURATION_CHOICES:
            raise ValueError(label)
        if self.video(video_id)["protected"]:
            raise PermissionError("保護中の動画は厳選の対象にできません")
        cur = self._active_label(video_id, scope, genre_id)
        if cur is not None and source == "common" and cur["source"] == "individual":
            return False  # 個別回答を優先する
        if label is None:
            if cur is None:
                return False
            self.conn.execute("UPDATE curation_labels SET active=0 WHERE id=?", (cur["id"],))
            self.conn.commit()
            return True
        if cur is not None and cur["label"] == label and cur["source"] == source:
            if cur["needs_review"]:  # 同じ回答で確認したら学習対象に戻す
                self.conn.execute("UPDATE curation_labels SET needs_review=0 WHERE id=?", (cur["id"],))
                self.conn.commit()
                return True
            return False
        if cur is not None:
            self.conn.execute("UPDATE curation_labels SET active=0 WHERE id=?", (cur["id"],))
        self.conn.execute(
            "INSERT INTO curation_labels(video_id,scope,genre_id,label,source,created_at) VALUES (?,?,?,?,?,?)",
            (video_id, scope, genre_id, label, source, time.time()))
        self.conn.commit()
        return True

    def effective_labels(self, scope: str, genre_id: int | None = None,
                         include_review: bool = True) -> dict[int, sqlite3.Row]:
        """{動画ID: 有効な回答}。scope=genre で genre_id=None なら全ジャンル分を (動画, ジャンル) で返す。"""
        sql = "SELECT * FROM curation_labels WHERE active=1 AND scope=?"
        args: list = [scope]
        if genre_id is not None:
            sql += " AND genre_id=?"
            args.append(genre_id)
        if not include_review:
            sql += " AND needs_review=0"
        rows = self.conn.execute(sql, args).fetchall()
        if scope == "genre" and genre_id is None:
            return {(r["video_id"], r["genre_id"]): r for r in rows}
        return {r["video_id"]: r for r in rows}

    def training_labels(self, scope: str, genre_id: int | None = None) -> list[sqlite3.Row]:
        """学習に使う回答: 有効・再確認待ちでない・保留以外。"""
        sql = ("SELECT * FROM curation_labels WHERE active=1 AND needs_review=0 AND scope=? "
               "AND label IN ('残す','整理候補')")
        args: list = [scope]
        if scope == "genre":
            sql += " AND genre_id=?"
            args.append(genre_id)
        return self.conn.execute(sql + " ORDER BY video_id", args).fetchall()

    def label_history(self, video_id: int) -> list[sqlite3.Row]:
        return self.conn.execute("SELECT * FROM curation_labels WHERE video_id=? ORDER BY id",
                                 (video_id,)).fetchall()

    def review_queue(self) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT c.*, v.name AS video_name FROM curation_labels c JOIN videos v ON v.id=c.video_id "
            "WHERE c.active=1 AND c.needs_review=1 ORDER BY c.created_at").fetchall()

    def confirm_label(self, label_id: int) -> None:
        """再確認待ちの回答を、内容を変えずに学習対象へ戻す。"""
        self.conn.execute("UPDATE curation_labels SET needs_review=0 WHERE id=?", (label_id,))
        self.conn.commit()

    def labeled_genres(self) -> set[int]:
        return {r[0] for r in self.conn.execute(
            "SELECT DISTINCT genre_id FROM curation_labels WHERE scope='genre'")}

    # --- ジャンル ---------------------------------------------------------
    def genres(self) -> list[sqlite3.Row]:
        return self.conn.execute("SELECT * FROM genres ORDER BY sort, id").fetchall()

    def genre_names(self) -> dict[int, str]:
        return {g["id"]: g["name"] for g in self.genres()}

    def _check_genre_name(self, name: str, genre_id: int | None = None) -> str:
        name = name.strip()
        if not name:
            raise ValueError("ジャンル名が空です")
        key = normalize_genre_name(name)
        if not key:
            raise ValueError("ジャンル名に文字を入れてください")
        if key in {normalize_genre_name(r) for r in RESERVED_GENRE_NAMES}:
            raise ValueError("「未分類」は分類状態を表す言葉のため、ジャンル名には使えません")
        for g in self.genres():
            if g["id"] != genre_id and normalize_genre_name(g["name"]) == key:
                raise ValueError(f"「{g['name']}」と同じジャンルがあります")
        return name

    def add_genre(self, name: str) -> int:
        name = self._check_genre_name(name)
        sort = self.conn.execute("SELECT COALESCE(MAX(sort),0)+1 FROM genres").fetchone()[0]
        cur = self.conn.execute("INSERT INTO genres(name, sort) VALUES (?,?)", (name, sort))
        self.conn.commit()
        return cur.lastrowid

    def rename_genre(self, genre_id: int, name: str) -> None:
        name = self._check_genre_name(name, genre_id)
        self.conn.execute("UPDATE genres SET name=? WHERE id=?", (name, genre_id))
        self.conn.commit()

    def delete_genre(self, genre_id: int) -> None:
        self.conn.execute("DELETE FROM genres WHERE id=?", (genre_id,))
        self.conn.commit()

    def genre_counts(self) -> dict[int, int]:
        return {r[0]: r[1] for r in self.conn.execute(
            "SELECT genre_id, COUNT(*) FROM video_genres GROUP BY genre_id")}

    def user_genres(self, video_id: int) -> set[int]:
        return {r[0] for r in self.conn.execute(
            "SELECT genre_id FROM video_genres WHERE video_id=?", (video_id,))}

    def all_user_genres(self) -> dict[int, set[int]]:
        result: dict[int, set[int]] = {}
        for vid, gid in self.conn.execute("SELECT video_id, genre_id FROM video_genres"):
            result.setdefault(vid, set()).add(gid)
        return result

    def set_user_genres(self, video_id: int, genre_ids, source: str | None = None) -> bool:
        """ユーザーが確定したジャンルを保存する。変更があり source があれば教師データに登録。"""
        new = set(genre_ids)
        old = self.user_genres(video_id)
        if new == old:
            return False
        self.conn.execute("DELETE FROM video_genres WHERE video_id=?", (video_id,))
        self.conn.executemany("INSERT INTO video_genres VALUES (?,?)",
                              [(video_id, g) for g in new])
        # 追加・削除されたジャンルの厳選回答は、確認されるまで学習から外す（移動はしない）
        changed = old ^ new
        if changed:
            marks = ",".join("?" * len(changed))
            self.conn.execute(
                f"UPDATE curation_labels SET needs_review=1 WHERE video_id=? AND scope='genre' "
                f"AND active=1 AND genre_id IN ({marks})", (video_id, *changed))
        if source and new:
            self.conn.execute(
                "INSERT INTO training(video_id, source, labeled_at, model_version) VALUES (?,?,?,NULL) "
                "ON CONFLICT(video_id) DO UPDATE SET source=excluded.source, "
                "labeled_at=excluded.labeled_at, model_version=NULL",
                (video_id, source, time.time()))
        elif not new:
            self.conn.execute("DELETE FROM training WHERE video_id=?", (video_id,))
        self.conn.commit()
        return True

    # --- タグ ---------------------------------------------------------------
    def tags(self) -> list[sqlite3.Row]:
        return self.conn.execute("SELECT * FROM tags ORDER BY id").fetchall()

    def tag_id(self, name: str, create: bool = True) -> int | None:
        row = self.conn.execute("SELECT id FROM tags WHERE name=?", (name,)).fetchone()
        if row:
            return row[0]
        if not create:
            return None
        cur = self.conn.execute("INSERT INTO tags(name) VALUES (?)", (name.strip(),))
        self.conn.commit()
        return cur.lastrowid

    def rename_tag(self, tag_id: int, name: str) -> None:
        self.conn.execute("UPDATE tags SET name=? WHERE id=?", (name.strip(), tag_id))
        self.conn.commit()

    def delete_tag(self, tag_id: int) -> None:
        self.conn.execute("DELETE FROM tags WHERE id=?", (tag_id,))
        self.conn.commit()

    def video_tags(self, video_id: int) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT t.id, t.name, vt.source, vt.status FROM video_tags vt "
            "JOIN tags t ON t.id = vt.tag_id WHERE vt.video_id=? ORDER BY t.id",
            (video_id,)).fetchall()

    def all_adopted_tags(self) -> dict[int, list[str]]:
        result: dict[int, list[str]] = {}
        for vid, name in self.conn.execute(
                "SELECT vt.video_id, t.name FROM video_tags vt JOIN tags t ON t.id=vt.tag_id "
                "WHERE vt.status='採用' ORDER BY t.id"):
            result.setdefault(vid, []).append(name)
        return result

    def set_manual_tag(self, video_id: int, tag_name: str, on: bool) -> None:
        tag_id = self.tag_id(tag_name)
        if on:
            self.conn.execute(
                "INSERT INTO video_tags VALUES (?,?,'manual','採用') ON CONFLICT(video_id, tag_id) "
                "DO UPDATE SET source='manual', status='採用'", (video_id, tag_id))
        else:
            self.conn.execute("DELETE FROM video_tags WHERE video_id=? AND tag_id=?",
                              (video_id, tag_id))
        self.conn.commit()

    def suggest_ai_tag(self, video_id: int, tag_name: str) -> None:
        """AI のタグ候補を「保留」で追加する。手動タグや却下済みには触れない。"""
        tag_id = self.tag_id(tag_name)
        self.conn.execute("INSERT OR IGNORE INTO video_tags VALUES (?,?,'ai','保留')",
                          (video_id, tag_id))
        self.conn.commit()

    def set_ai_tag_status(self, video_id: int, tag_id: int, status: str) -> None:
        if status not in ("採用", "保留", "却下"):
            raise ValueError(status)
        self.conn.execute("UPDATE video_tags SET status=? WHERE video_id=? AND tag_id=? AND source='ai'",
                          (status, video_id, tag_id))
        self.conn.commit()

    # --- 教師データ・モデル -------------------------------------------------
    def training_rows(self) -> list[sqlite3.Row]:
        return self.conn.execute("SELECT * FROM training ORDER BY labeled_at").fetchall()

    def pending_training_count(self) -> int:
        """まだ学習に反映していない教師データ（通常の動画＋初期12本の回答）。"""
        return self.conn.execute(
            "SELECT (SELECT COUNT(*) FROM training WHERE model_version IS NULL) + "
            f"(SELECT COUNT(*) FROM sample_answers WHERE status='{ANSWERED}' AND model_version IS NULL)"
        ).fetchone()[0]

    def mark_training_applied(self, version: int, video_ids) -> None:
        self.conn.executemany("UPDATE training SET model_version=? WHERE video_id=?",
                              [(version, v) for v in video_ids])
        self.conn.commit()

    def active_model(self, scope: str = CLASSIFY_SCOPE) -> sqlite3.Row | None:
        return self.conn.execute("SELECT * FROM models WHERE active=1 AND scope=?", (scope,)).fetchone()

    def next_model_version(self) -> int:
        return self.conn.execute("SELECT COALESCE(MAX(version),0)+1 FROM models").fetchone()[0]

    def add_model(self, version: int, n_samples: int, file: str, evaluation: dict,
                  activate: bool, scope: str = CLASSIFY_SCOPE) -> None:
        if activate:  # 同じ学習範囲のモデルだけを切り替える
            self.conn.execute("UPDATE models SET active=0 WHERE scope=?", (scope,))
        self.conn.execute(
            "INSERT INTO models(version,created_at,n_samples,file,evaluation,active,scope) "
            "VALUES (?,?,?,?,?,?,?)",
            (version, time.time(), n_samples, file, json.dumps(evaluation, ensure_ascii=False),
             int(activate), scope))
        self.conn.commit()

    def models(self, scope: str | None = None) -> list[sqlite3.Row]:
        if scope is None:
            return self.conn.execute("SELECT * FROM models ORDER BY version DESC").fetchall()
        return self.conn.execute("SELECT * FROM models WHERE scope=? ORDER BY version DESC",
                                 (scope,)).fetchall()

    def active_models(self) -> dict[str, sqlite3.Row]:
        return {r["scope"]: r for r in self.conn.execute("SELECT * FROM models WHERE active=1")}

    def deactivate_models(self, scope: str) -> list[int]:
        """学習範囲のモデルを未学習状態に戻す（ファイルと記録は残す）。"""
        versions = [r[0] for r in self.conn.execute(
            "SELECT version FROM models WHERE scope=? AND active=1", (scope,))]
        self.conn.execute("UPDATE models SET active=0 WHERE scope=?", (scope,))
        if scope == CLASSIFY_SCOPE:  # ジャンル分類の予測結果も消す（ユーザーの分類は残す）
            self.conn.execute("UPDATE videos SET ai_state='未判定', ai_genres=NULL, ai_candidates=NULL, "
                              "ai_reason=NULL, ai_model_version=NULL")
            self.conn.execute("UPDATE training SET model_version=NULL")
            self.conn.execute("UPDATE sample_answers SET model_version=NULL")
        self.conn.commit()
        return versions

    # --- 初期12本の回答（このプロファイル専用） ------------------------------------
    def sample_answers(self) -> dict[str, dict]:
        """{サンプルID: {"status", "genres", "content_hash", "updated_at", "model_version"}}"""
        genres: dict[str, set[int]] = {}
        for sid, gid in self.conn.execute("SELECT sample_id, genre_id FROM sample_answer_genres"):
            genres.setdefault(sid, set()).add(gid)
        return {r["sample_id"]: {"status": r["status"], "genres": genres.get(r["sample_id"], set()),
                                 "content_hash": r["content_hash"], "updated_at": r["updated_at"],
                                 "model_version": r["model_version"]}
                for r in self.conn.execute("SELECT * FROM sample_answers")}

    def sample_answer(self, sample_id: str) -> dict | None:
        return self.sample_answers().get(sample_id)

    def save_sample_answer(self, sample_id: str, status: str, genre_ids=(),
                           content_hash: str | None = None) -> bool:
        """初期12本の回答を保存する。回答済みはジャンル1つ以上が必要。変更があれば True。

        以前の回答は sample_answer_history に残す。スキップ・未回答は教師データに使わない。
        """
        new = set(genre_ids) if status == ANSWERED else set()
        if status not in (UNANSWERED, ANSWERED, SKIPPED):
            raise ValueError(status)
        if status == ANSWERED and not new:
            raise ValueError("回答済みにするにはジャンルを1つ以上選んでください")
        cur = self.sample_answer(sample_id)
        if cur is None and status == UNANSWERED:
            return False
        if (cur is not None and cur["status"] == status and cur["genres"] == new
                and cur["content_hash"] == content_hash):
            return False
        now = time.time()
        if status == UNANSWERED:
            self.conn.execute("DELETE FROM sample_answers WHERE sample_id=?", (sample_id,))
        else:
            self.conn.execute(
                "INSERT INTO sample_answers(sample_id,status,content_hash,updated_at,model_version) "
                "VALUES (?,?,?,?,NULL) ON CONFLICT(sample_id) DO UPDATE SET status=excluded.status, "
                "content_hash=excluded.content_hash, updated_at=excluded.updated_at, model_version=NULL",
                (sample_id, status, content_hash, now))
            self.conn.execute("DELETE FROM sample_answer_genres WHERE sample_id=?", (sample_id,))
            self.conn.executemany("INSERT INTO sample_answer_genres VALUES (?,?)",
                                  [(sample_id, g) for g in sorted(new)])
        self.conn.execute(
            "INSERT INTO sample_answer_history(sample_id,status,genre_ids,content_hash,created_at) "
            "VALUES (?,?,?,?,?)", (sample_id, status, json.dumps(sorted(new)), content_hash, now))
        self.conn.commit()
        return True

    def sample_answer_history(self, sample_id: str) -> list[sqlite3.Row]:
        return self.conn.execute("SELECT * FROM sample_answer_history WHERE sample_id=? ORDER BY id",
                                 (sample_id,)).fetchall()

    def mark_sample_answers_applied(self, version: int, sample_ids) -> None:
        self.conn.executemany("UPDATE sample_answers SET model_version=? WHERE sample_id=?",
                              [(version, s) for s in sample_ids])
        self.conn.commit()

    def initial_state(self) -> str | None:
        return self.get_setting("initial_state")

    # --- 集計 ---------------------------------------------------------------
    def stats(self) -> dict:
        q = lambda sql: self.conn.execute(sql).fetchone()[0]  # noqa: E731
        return {
            "videos": q("SELECT COUNT(*) FROM videos WHERE missing=0"),
            "total_size": q("SELECT COALESCE(SUM(size),0) FROM videos WHERE missing=0"),
            "analyzed": q("SELECT COUNT(*) FROM videos WHERE missing=0 AND analysis_state IN ('完了','一部失敗')"),
            "failed": q("SELECT COUNT(*) FROM videos WHERE missing=0 AND analysis_state='失敗'"),
            "manual": q("SELECT COUNT(DISTINCT video_id) FROM video_genres"),
            "ai_classified": q("SELECT COUNT(*) FROM videos WHERE missing=0 AND ai_state='分類済み'"),
            "unclassified": q("SELECT COUNT(*) FROM videos v WHERE missing=0 AND ai_state='未分類' "
                              "AND NOT EXISTS (SELECT 1 FROM video_genres g WHERE g.video_id=v.id)"),
            "favorite": q("SELECT COUNT(*) FROM videos WHERE missing=0 AND favorite=1"),
            "protected": q("SELECT COUNT(*) FROM videos WHERE missing=0 AND protected=1"),
            "training": q("SELECT COUNT(*) FROM training"),
            "pending": self.pending_training_count(),
            "review": q("SELECT COUNT(*) FROM curation_labels WHERE active=1 AND needs_review=1"),
            "missing": q("SELECT COUNT(*) FROM videos WHERE missing=1"),
        }


def loads(text: str | None, default=None):
    if not text:
        return default
    try:
        return json.loads(text)
    except ValueError:
        return default

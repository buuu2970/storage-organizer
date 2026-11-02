"""初期学習用サンプル動画（全プロファイル共通）の管理。

- 動画本体はファイルのまま扱い、SQLite には保存しない。プロファイルごとに複製もしない
- 共通の samples.db には「どのファイルか」と解析結果（特徴量キャッシュ）だけを置く。
  回答・進捗・モデルは各プロファイルの recsort.db（db.Database）に保存する
- サンプル ID はファイル名（拡張子なし）またはフォルダ内の samples.json で決める。
  内容が入れ替わったことは、サイズ・更新日時と簡易ハッシュ（先頭・末尾 1MB）で検出する
- 元ファイルは読み取るだけで、変更・削除しない
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import threading
from dataclasses import dataclass, field
from pathlib import Path

from . import media
from .file_types import categorize
from .scanner import is_cloud_only, is_link

INITIAL_SAMPLE_COUNT = 12
MANIFEST = "samples.json"
SAMPLE_DIR_ENV = "RECSORT_SAMPLE_DIR"
_HASH_CHUNK = 1024 * 1024
SAMPLE_PREFIX = "sample:"  # モデル内で個人の動画 ID と区別するための接頭辞

STATUS_TEXT = {
    "ok": "",
    "missing": "ファイルが見つかりません",
    "cloud_only": "クラウド上のみのファイルのため読み込みません",
    "unreadable": "ファイルを読み込めません",
}

_SCHEMA = """
CREATE TABLE IF NOT EXISTS samples (
    sample_id TEXT PRIMARY KEY,
    sort INTEGER NOT NULL,
    path TEXT NOT NULL,
    name TEXT NOT NULL,
    size INTEGER NOT NULL DEFAULT 0,
    mtime_ns INTEGER NOT NULL DEFAULT 0,
    content_hash TEXT,                       -- 簡易ハッシュ。変わったら別の動画として扱う
    status TEXT NOT NULL DEFAULT 'ok',       -- ok / missing / cloud_only / unreadable
    status_detail TEXT,
    analysis_state TEXT NOT NULL DEFAULT '未解析',
    analysis_error TEXT,
    meta TEXT,
    features TEXT,
    thumbs TEXT,
    analyzed_hash TEXT                       -- 解析したときの content_hash
);
CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT
);
"""


def project_sample_dir() -> Path:
    """リポジトリ直下の sample_videos（同梱の12本。起動時に自動で登録する）。"""
    return Path(__file__).resolve().parent.parent / "sample_videos"


def content_hash(path: str, size: int) -> str:
    """サイズ＋先頭・末尾 1MB の SHA-256。大きな動画でもすぐ終わる内容確認用。"""
    h = hashlib.sha256(str(size).encode())
    with open(path, "rb") as f:
        h.update(f.read(_HASH_CHUNK))
        if size > 2 * _HASH_CHUNK:
            f.seek(-_HASH_CHUNK, os.SEEK_END)
            h.update(f.read(_HASH_CHUNK))
    return h.hexdigest()


def _natural_key(name: str):
    return [int(t) if t.isdigit() else t.lower() for t in re.split(r"(\d+)", name)]


@dataclass
class RegisterResult:
    count: int
    warnings: list[str] = field(default_factory=list)
    changed: list[str] = field(default_factory=list)  # 内容が入れ替わったサンプル ID


def list_sample_files(folder: str) -> tuple[list[tuple[str, str]], list[str]]:
    """フォルダ直下の動画を [(サンプルID, パス)] で返す。リンク・ジャンクションはたどらない。"""
    warnings: list[str] = []
    root = Path(folder)
    if not root.is_dir():
        raise FileNotFoundError(f"サンプル動画のフォルダが見つかりません: {folder}")
    files: dict[str, str] = {}
    with os.scandir(root) as it:
        for entry in it:
            try:
                if is_link(entry):
                    warnings.append(f"{entry.name}: リンク・ジャンクションのため使いません")
                    continue
                if entry.is_file(follow_symlinks=False) and categorize(entry.name) == "動画":
                    files[entry.name] = entry.path
            except OSError as e:
                warnings.append(f"{entry.name}: 読み取れません（{e.strerror or e}）")
    manifest = root / MANIFEST
    ordered: list[tuple[str, str]] = []
    if manifest.is_file():
        try:
            data = json.loads(manifest.read_text(encoding="utf-8"))
            for item in data.get("samples", []):
                sid, name = str(item["id"]).strip(), str(item["file"])
                if not sid or name not in files:
                    warnings.append(f"{MANIFEST}: {name} が見つからないため飛ばしました")
                    continue
                ordered.append((sid, files[name]))
        except (OSError, ValueError, KeyError, TypeError, AttributeError) as e:
            warnings.append(f"{MANIFEST} を読み込めないため、ファイル名順で登録します（{e}）")
            ordered = []
    if not ordered:
        stems: dict[str, int] = {}
        for name in files:
            stem = Path(name).stem.lower()
            stems[stem] = stems.get(stem, 0) + 1
        for name in sorted(files, key=_natural_key):
            stem = Path(name).stem
            sid = stem if stems[stem.lower()] == 1 else name  # 拡張子違いの同名は拡張子込みで区別
            ordered.append((sid, files[name]))
    seen: set[str] = set()
    unique = []
    for sid, path in ordered:
        if sid.lower() in seen:
            warnings.append(f"サンプルID「{sid}」が重複しているため2本目以降を飛ばしました")
            continue
        seen.add(sid.lower())
        unique.append((sid, path))
    if len(unique) > INITIAL_SAMPLE_COUNT:
        warnings.append(f"動画が {len(unique)} 本あるため、先頭の {INITIAL_SAMPLE_COUNT} 本を使います")
        unique = unique[:INITIAL_SAMPLE_COUNT]
    elif len(unique) < INITIAL_SAMPLE_COUNT:
        warnings.append(f"初期学習用の動画が {len(unique)} 本です（想定は {INITIAL_SAMPLE_COUNT} 本）")
    return unique, warnings


class SampleStore:
    """共通サンプルの登録情報と解析キャッシュ。スレッドごとに作って使う。"""

    def __init__(self, root: Path | str) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.thumb_dir = self.root / "thumbs"
        self.thumb_dir.mkdir(exist_ok=True)
        self.conn = sqlite3.connect(self.root / "samples.db", timeout=30)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode = WAL")
        self.conn.executescript(_SCHEMA)
        self.conn.commit()

    def close(self) -> None:
        self.conn.close()

    # --- 設定 -------------------------------------------------------------
    def sample_dir(self) -> str | None:
        row = self.conn.execute("SELECT value FROM settings WHERE key='sample_dir'").fetchone()
        return row[0] if row else None

    def _set_sample_dir(self, folder: str) -> None:
        self.conn.execute("INSERT INTO settings VALUES ('sample_dir', ?) "
                          "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (folder,))

    def candidate_dirs(self) -> list[str]:
        """指定済みのフォルダ、環境変数、リポジトリ直下の sample_videos の順に探す。"""
        dirs = [self.sample_dir(), os.environ.get(SAMPLE_DIR_ENV), str(project_sample_dir())]
        return [d for d in dirs if d]

    def ensure_registered(self) -> RegisterResult | None:
        """未登録なら既定の場所から登録を試みる。登録済みなら状態だけ確認する。

        登録したフォルダがなくなっていた場合（別のパソコンへ移したときなど）も既定の場所を探す。
        """
        current = self.sample_dir()
        if self.samples() and current and Path(current).is_dir():
            return self.refresh()
        for d in self.candidate_dirs():
            if Path(d).is_dir():
                try:
                    result = self.register_folder(d)
                except OSError:
                    continue
                if result.count:
                    return result
        return self.refresh() if self.samples() else None

    # --- 登録・状態確認 -----------------------------------------------------
    def register_folder(self, folder: str) -> RegisterResult:
        files, warnings = list_sample_files(folder)
        if not files:
            raise FileNotFoundError(f"フォルダ直下に動画がありません: {folder}")
        old = {r["sample_id"]: r for r in self.samples()}
        result = RegisterResult(len(files), warnings)
        keep = set()
        for sort, (sid, path) in enumerate(files):
            keep.add(sid)
            if sid not in old:
                self.conn.execute("INSERT INTO samples(sample_id, sort, path, name) VALUES (?,?,?,?)",
                                  (sid, sort, path, os.path.basename(path)))
            else:
                self.conn.execute("UPDATE samples SET sort=?, path=?, name=? WHERE sample_id=?",
                                  (sort, path, os.path.basename(path), sid))
            if self._check(sid, force_hash=sid not in old or old[sid]["path"] != path):
                result.changed.append(sid)
        for sid in set(old) - keep:  # 使わなくなったサンプルの解析キャッシュは消す（回答は各プロファイルに残る）
            self.conn.execute("DELETE FROM samples WHERE sample_id=?", (sid,))
        self._set_sample_dir(folder)
        self.conn.commit()
        return result

    def refresh(self) -> RegisterResult:
        """登録済みサンプルの存在・変更を確認する。ファイルが壊れていても例外にしない。"""
        rows = self.samples()
        result = RegisterResult(len(rows))
        for r in rows:
            if self._check(r["sample_id"]):
                result.changed.append(r["sample_id"])
        self.conn.commit()
        return result

    def _check(self, sid: str, force_hash: bool = False) -> bool:
        """ファイルの状態を確かめる。内容が変わっていたら解析を無効にして True。"""
        row = self.sample(sid)
        path = row["path"]
        try:
            st = os.stat(path)
        except OSError:
            self.conn.execute("UPDATE samples SET status='missing', status_detail=NULL WHERE sample_id=?", (sid,))
            return False
        if is_cloud_only(st):
            self.conn.execute("UPDATE samples SET status='cloud_only', status_detail=NULL, size=?, "
                              "mtime_ns=? WHERE sample_id=?", (st.st_size, st.st_mtime_ns, sid))
            return False
        unchanged = (not force_hash and row["content_hash"] and row["size"] == st.st_size
                     and row["mtime_ns"] == st.st_mtime_ns)
        if unchanged:
            self.conn.execute("UPDATE samples SET status='ok', status_detail=NULL WHERE sample_id=?", (sid,))
            return False
        try:
            new_hash = content_hash(path, st.st_size)
        except OSError as e:
            self.conn.execute("UPDATE samples SET status='unreadable', status_detail=? WHERE sample_id=?",
                              (str(e), sid))
            return False
        changed = row["content_hash"] is not None and row["content_hash"] != new_hash
        self.conn.execute("UPDATE samples SET status='ok', status_detail=NULL, size=?, mtime_ns=?, "
                          "content_hash=? WHERE sample_id=?", (st.st_size, st.st_mtime_ns, new_hash, sid))
        if row["analyzed_hash"] and row["analyzed_hash"] != new_hash:
            self.conn.execute("UPDATE samples SET analysis_state='未解析', analysis_error=NULL, meta=NULL, "
                              "features=NULL, thumbs=NULL, analyzed_hash=NULL WHERE sample_id=?", (sid,))
        return changed

    # --- 参照 -------------------------------------------------------------
    def samples(self) -> list[sqlite3.Row]:
        return self.conn.execute("SELECT * FROM samples ORDER BY sort").fetchall()

    def sample(self, sid: str) -> sqlite3.Row | None:
        return self.conn.execute("SELECT * FROM samples WHERE sample_id=?", (sid,)).fetchone()

    def usable_features(self) -> dict[str, sqlite3.Row]:
        """解析済みで、解析後に内容が変わっていないサンプル。"""
        return {r["sample_id"]: r for r in self.samples()
                if r["status"] == "ok" and r["features"] and r["analyzed_hash"] == r["content_hash"]}

    def needs_analysis(self) -> list[str]:
        return [r["sample_id"] for r in self.samples()
                if r["status"] == "ok" and r["analysis_state"] == "未解析"]

    def is_sample_path(self, path: str) -> bool:
        """通常の動画一覧に登録しないためのパス判定。"""
        norm = os.path.normcase(os.path.abspath(path))
        paths = {os.path.normcase(os.path.abspath(r["path"])) for r in self.samples()}
        return norm in paths

    def update_analysis(self, sid: str, **fields) -> None:
        allowed = {"analysis_state", "analysis_error", "meta", "features", "thumbs", "analyzed_hash"}
        if not fields or set(fields) - allowed:
            raise ValueError(fields)
        for key in ("meta", "features", "thumbs"):
            if fields.get(key) is not None and not isinstance(fields[key], str):
                fields[key] = json.dumps(fields[key], ensure_ascii=False)
        cols = ", ".join(f"{k}=?" for k in fields)
        self.conn.execute(f"UPDATE samples SET {cols} WHERE sample_id=?", (*fields.values(), sid))
        self.conn.commit()


def display_name(row) -> str:
    return f"初期サンプル {row['sort'] + 1}（{row['name']}）"


def task_analyze_samples(root, tools: media.FFmpegTools, max_frames: int,
                         cancel: threading.Event, report) -> dict:
    """共通サンプルを解析して特徴量をキャッシュする（回答は扱わない）。"""
    store = SampleStore(root)
    done = {"完了": 0, "一部失敗": 0, "失敗": 0, "対象外": 0}
    try:
        store.refresh()
        ids = store.needs_analysis()
        for i, sid in enumerate(ids):
            if cancel.is_set():
                done["cancelled"] = True
                break
            row = store.sample(sid)
            report({"text": f"初期サンプルを解析中 {i + 1}/{len(ids)} 本: {row['name']}",
                    "done": i, "total": len(ids)})
            if row["status"] != "ok":
                done["対象外"] += 1
                continue
            expected = row["content_hash"]
            try:
                state, err, meta, feats, thumbs = media.analyze_video(
                    tools, row["path"], store.thumb_dir / sid, max_frames, cancel)
            except media.Cancelled:
                done["cancelled"] = True
                break
            except (OSError, RuntimeError, ValueError, TimeoutError, media.FileChanged) as e:
                store.update_analysis(sid, analysis_state="失敗", analysis_error=str(e))
                done["失敗"] += 1
                continue
            store.refresh()  # 解析中に入れ替わっていたら結果を使わない
            if store.sample(sid)["content_hash"] != expected:
                done["失敗"] += 1
                continue
            store.update_analysis(sid, analysis_state=state, analysis_error=err, meta=meta,
                                  features=feats, thumbs=thumbs, analyzed_hash=expected)
            done[state] += 1
    finally:
        store.close()
    return done

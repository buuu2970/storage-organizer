"""データの保存（データベースは使わない）。

| 保存するもの | 形式 | 書き方 |
|---|---|---|
| 設定 | settings.json | 一時ファイルに書いてから置き換える（書き込み中に落ちても壊れない） |
| 保護・判断・確認日時（パスごと） | user_state.json | 同上。変更があったときだけ書く |
| 調査場所・スキップ・容量の集計・類似グループ | scan_state.json | 同上。調査の区切りごとに書く |
| 見つけた画像・動画の一覧 | inventory/<場所>.json.gz | 場所の調査が終わるたびに、その場所の分だけ書き直す |
| ハッシュ・動画解析などの結果 | analysis.jsonl | 追記のみ（巨大なファイルを毎回書き直さない）。壊れた行は読み飛ばし、大きくなったら起動時に整理する |
| ごみ箱への移動の記録 | trash_log.jsonl | 追記のみ |

1つの Store を、画面と調査スレッドでロックを使って共有する。
"""

from __future__ import annotations

import gzip
import hashlib
import json
import os
import sys
import threading
import time
from pathlib import Path

ANALYSIS_STATES = ("未解析", "解析中", "完了", "一部失敗", "失敗")
DECISIONS = ("残す", "削除候補", "保留", "両方残す")
PATH_LIST_KEYS = ("extra_locations", "excluded_folders", "disabled_locations")
# 解析結果（JSON 文字列で持つ項目は loads() で読む）
ANALYSIS_FIELDS = ("analysis_state", "analysis_error", "meta", "features", "thumbs", "sha256", "taken_at", "dhash",
                   "light_state", "light_note", "failure", "vsig")
JSON_FIELDS = ("meta", "features", "thumbs", "failure", "vsig")
_DEFAULTS = {"analysis_state": "未解析", "analysis_error": None, "meta": None, "features": None, "thumbs": None,
             "sha256": None, "taken_at": None, "dhash": None, "similar_group": None, "light_state": "未解析",
             "light_note": None, "failure": None, "vsig": None}


def default_data_dir() -> Path:
    override = os.environ.get("RECSORT_DATA_DIR")
    if override:
        return Path(override)
    if sys.platform == "win32":
        base = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local"))
    else:
        base = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share"))
    return base / "RecSort"


def loads(text, default=None):
    if text is None or text == "":
        return default
    if not isinstance(text, str):
        return text
    try:
        return json.loads(text)
    except ValueError:
        return default


def _read_json(path: Path, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return default
    except (OSError, ValueError):
        # 壊れていたら退避して既定値で続ける（元のファイルは消さない）
        try:
            os.replace(path, path.with_suffix(path.suffix + f".broken-{int(time.time())}"))
        except OSError:
            pass
        return default


def _write_json(path: Path, data) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def _key(row) -> str:
    return f"{row['path']}|{row['size']}|{round(row['mtime'], 6)}"


class Store:
    def __init__(self, data_dir: Path | str | None = None) -> None:
        self.data_dir = Path(data_dir) if data_dir else default_data_dir()
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.thumb_dir = self.data_dir / "thumbs"
        self.inventory_dir = self.data_dir / "inventory"
        self.thumb_dir.mkdir(exist_ok=True)
        self.inventory_dir.mkdir(exist_ok=True)
        self.lock = threading.RLock()
        self._settings = _read_json(self.data_dir / "settings.json", {})
        self._user = _read_json(self.data_dir / "user_state.json", {})
        self._scan = _read_json(self.data_dir / "scan_state.json", {})
        self._scan.setdefault("locations", [])
        self._scan.setdefault("skips", [])
        self._scan.setdefault("stats", {})
        self._scan.setdefault("similar_groups", {})
        self._rows: dict[int, dict] = {}
        self._by_path: dict[str, int] = {}
        self._next_id = 1
        self._analysis_path = self.data_dir / "analysis.jsonl"
        self._load()

    # --- 読み込み -------------------------------------------------------------
    def _load(self) -> None:
        for f in sorted(self.inventory_dir.glob("*.json.gz")):
            try:
                with gzip.open(f, "rt", encoding="utf-8") as fh:
                    data = json.load(fh)
            except (OSError, ValueError, EOFError):
                continue  # 壊れた一覧は使わない（次の調査で作り直される）
            for rec in data.get("files", []):
                path, name, folder, size, mtime, ext, kind, cloud = rec
                if path in self._by_path:
                    continue
                self._new_row(path, name, folder, size, mtime, ext, kind, cloud, data.get("location"),
                              data.get("gen", 0))
        cache = self._read_analysis()
        live = 0
        for row in self._rows.values():
            fields = cache.get(_key(row))
            if fields:
                row.update(fields)
                live += 1
        if len(cache) > max(1000, 3 * max(live, 1)) or self._analysis_lines > 2 * max(len(cache), 1):
            self._compact_analysis()
        groups = self._scan.get("similar_groups", {})
        for gid, paths in groups.items():
            for p in paths:
                if p in self._by_path:
                    self._rows[self._by_path[p]]["similar_group"] = int(gid)

    def _read_analysis(self) -> dict[str, dict]:
        cache: dict[str, dict] = {}
        self._analysis_lines = 0
        try:
            with open(self._analysis_path, encoding="utf-8") as f:
                for line in f:
                    self._analysis_lines += 1
                    try:
                        rec = json.loads(line)
                    except ValueError:
                        continue  # 途中で落ちたときの壊れた行は読み飛ばす
                    cache.setdefault(rec["k"], {}).update(rec["f"])
        except FileNotFoundError:
            pass
        return cache

    def _compact_analysis(self) -> None:
        """今の一覧にあるファイルの解析結果だけを残して書き直す（起動時のみ）。"""
        tmp = self._analysis_path.with_suffix(".jsonl.tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            for row in self._rows.values():
                fields = {k: row[k] for k in ANALYSIS_FIELDS if row.get(k) not in (None, _DEFAULTS.get(k))}
                if fields:
                    f.write(json.dumps({"k": _key(row), "f": fields}, ensure_ascii=False) + "\n")
        os.replace(tmp, self._analysis_path)

    def _new_row(self, path, name, folder, size, mtime, ext, kind, cloud, location, gen) -> dict:
        rid = self._next_id
        self._next_id += 1
        user = self._user.get(path, {})
        row = {"id": rid, "path": path, "name": name, "folder": folder, "size": size, "mtime": mtime, "ext": ext,
               "kind": kind, "cloud_only": int(bool(cloud)), "missing": 0, "location": location, "seen_gen": gen,
               "protected": int(bool(user.get("protected"))), "decision": user.get("decision"),
               "favorite": int(bool(user.get("favorite"))), "last_viewed": user.get("last_viewed"),
               "trashed_at": None, "curation": None, **_DEFAULTS}
        self._rows[rid] = row
        self._by_path[path] = rid
        return row

    # --- 設定 -------------------------------------------------------------------
    def get_setting(self, key: str, default=None):
        with self.lock:
            return self._settings.get(key, default)

    def set_setting(self, key: str, value) -> None:
        with self.lock:
            if value is None:
                self._settings.pop(key, None)
            else:
                self._settings[key] = value
            _write_json(self.data_dir / "settings.json", self._settings)

    def json_list(self, key: str) -> list[str]:
        value = self.get_setting(key, [])
        return list(value) if isinstance(value, list) else (loads(value, []) or [])

    def set_json_list(self, key: str, values: list[str]) -> None:
        if key in PATH_LIST_KEYS:  # 場所の一覧は表記を統一して重複を除く
            values = list(dict.fromkeys(os.path.normpath(v) for v in values))
        self.set_setting(key, list(values))

    # --- 画像・動画の一覧 ---------------------------------------------------------
    def media(self) -> list[dict]:
        with self.lock:
            return [r for r in self._rows.values() if not r["missing"]]

    def videos(self) -> list[dict]:
        return [r for r in self.media() if r["kind"] == "video"]

    def all_rows(self) -> list[dict]:
        with self.lock:
            return list(self._rows.values())

    def video(self, media_id: int) -> dict | None:
        with self.lock:
            return self._rows.get(media_id)

    def id_of(self, path: str) -> int | None:
        with self.lock:
            return self._by_path.get(os.path.normpath(path))

    def upsert_media(self, records: list[tuple], gen: int, location: str) -> tuple[int, int]:
        """records: (path, name, folder, size, mtime, ext, kind, cloud_only)。変更のないファイルは解析結果を使い続ける。"""
        added = changed = 0
        with self.lock:
            for path, name, folder, size, mtime, ext, kind, cloud in records:
                path, folder = os.path.normpath(path), os.path.normpath(folder)
                rid = self._by_path.get(path)
                if rid is None:
                    self._new_row(path, name, folder, size, mtime, ext, kind, cloud, location, gen)
                    added += 1
                    continue
                row = self._rows[rid]
                row.update(missing=0, trashed_at=None, cloud_only=int(bool(cloud)), kind=kind, location=location,
                           seen_gen=gen)
                if row["size"] != size or abs(row["mtime"] - mtime) > 1e-6:
                    self._invalidate(row, size, mtime)
                    changed += 1
        return added, changed

    def _invalidate(self, row: dict, size: int, mtime: float) -> None:
        """ファイルが変わったので、古い解析結果を使わない（保護・判断は残す）。"""
        row.update(size=size, mtime=mtime, **_DEFAULTS)

    def update_file_state(self, media_id: int, size: int, mtime: float) -> bool:
        with self.lock:
            row = self._rows[media_id]
            if row["size"] != size or abs(row["mtime"] - mtime) > 1e-6:
                self._invalidate(row, size, mtime)
                return True
            return False

    def update_video(self, media_id: int, **fields) -> None:
        """解析結果などを更新する。解析結果は analysis.jsonl に追記する。"""
        if "last_viewed" in fields:
            self._set_user(media_id, last_viewed=fields.pop("last_viewed"))
        if not fields:
            return
        for key in JSON_FIELDS:
            if key in fields and fields[key] is not None and not isinstance(fields[key], str):
                fields[key] = json.dumps(fields[key], ensure_ascii=False)
        with self.lock:
            row = self._rows[media_id]
            row.update(fields)
            persist = {k: v for k, v in fields.items() if k in ANALYSIS_FIELDS}
            if persist:
                with open(self._analysis_path, "a", encoding="utf-8") as f:
                    f.write(json.dumps({"k": _key(row), "f": persist}, ensure_ascii=False) + "\n")

    def set_field(self, media_id: int, **fields) -> None:
        """一覧上の項目を直接変える（テスト・内部用。保存はしない）。"""
        with self.lock:
            self._rows[media_id].update(fields)

    def insert_raw(self, path: str, name: str, folder: str, size: int, mtime: float, ext: str, kind: str = "video",
                   location: str | None = None, **analysis) -> int:
        """表記を統一せずに1件登録する（以前の版のデータの再現・テスト用）。"""
        with self.lock:
            row = self._new_row(path, name, folder, size, mtime, ext, kind, False, location, 0)
        if analysis:
            self.update_video(row["id"], **analysis)
        return row["id"]

    def mark_unseen_missing(self, location: str, gen: int) -> int:
        with self.lock:
            n = 0
            for r in self._rows.values():
                if r["location"] == location and r["seen_gen"] < gen and not r["missing"]:
                    r["missing"] = 1
                    n += 1
            return n

    def hide(self, media_id: int, clear_decision: bool = True) -> None:
        """一覧から外す（ソースコードの .ts・除外した場所など）。保護・判断の記録は残す。"""
        with self.lock:
            self._rows[media_id]["missing"] = 1
        if clear_decision:
            self._set_user(media_id, decision=None)

    def save_inventory(self, location: str, gen: int) -> None:
        """場所ごとの一覧を書き出す（場所の調査が終わったとき）。"""
        with self.lock:
            files = [[r["path"], r["name"], r["folder"], r["size"], r["mtime"], r["ext"], r["kind"], r["cloud_only"]]
                     for r in self._rows.values() if r["location"] == location and not r["missing"]]
        name = hashlib.sha1(location.encode("utf-8")).hexdigest()[:16] + ".json.gz"
        tmp = self.inventory_dir / (name + ".tmp")
        with gzip.open(tmp, "wt", encoding="utf-8") as f:
            json.dump({"location": location, "gen": gen, "files": files}, f, ensure_ascii=False)
        os.replace(tmp, self.inventory_dir / name)

    def forget_location(self, location: str) -> None:
        name = hashlib.sha1(location.encode("utf-8")).hexdigest()[:16] + ".json.gz"
        try:
            os.remove(self.inventory_dir / name)  # アプリが作った一覧ファイルだけを消す
        except OSError:
            pass

    # --- 保護・判断 ---------------------------------------------------------------
    def _set_user(self, media_id: int, **fields) -> None:
        with self.lock:
            row = self._rows[media_id]
            entry = self._user.setdefault(row["path"], {})
            for k, v in fields.items():
                row[k] = int(bool(v)) if k in ("protected", "favorite") else v
                if v in (None, 0, False):
                    entry.pop(k, None)
                else:
                    entry[k] = v
            if not entry:
                self._user.pop(row["path"], None)
            _write_json(self.data_dir / "user_state.json", self._user)

    def import_user_state(self, entries: dict[str, dict]) -> None:
        """以前の版から保護・判断を取り込む（すでにある判断は上書きしない）。"""
        with self.lock:
            for path, entry in entries.items():
                cur = self._user.setdefault(path, {})
                for k, v in entry.items():
                    cur.setdefault(k, v)
                rid = self._by_path.get(path)
                if rid is not None:
                    row = self._rows[rid]
                    for k, v in cur.items():
                        row[k] = int(bool(v)) if k in ("protected", "favorite") else v
            _write_json(self.data_dir / "user_state.json", self._user)

    def set_decision(self, media_id: int, decision: str | None) -> None:
        if decision is not None and decision not in DECISIONS:
            raise ValueError(decision)
        if decision == "削除候補" and self.video(media_id)["protected"]:
            raise PermissionError("保護したファイルは削除候補にできません")
        self._set_user(media_id, decision=decision)

    def set_protected(self, media_id: int, value: bool) -> None:
        """保護する。付けると削除候補から外す。"""
        row = self.video(media_id)
        if value and row["decision"] == "削除候補":
            self._set_user(media_id, protected=True, decision=None)
        else:
            self._set_user(media_id, protected=bool(value))

    def mark_viewed(self, media_id: int) -> None:
        if self.get_setting("view_history_started") is None:
            self.set_setting("view_history_started", str(time.time()))
        self._set_user(media_id, last_viewed=time.time())

    def mark_trashed(self, media_id: int) -> None:
        with self.lock:
            self._rows[media_id].update(trashed_at=time.time(), missing=1)
        self._set_user(media_id, decision=None)

    def log_trash(self, path: str, size: int, result: str, message: str | None) -> None:
        with self.lock, open(self.data_dir / "trash_log.jsonl", "a", encoding="utf-8") as f:
            f.write(json.dumps({"path": path, "size": size, "result": result, "message": message,
                                "at": time.time()}, ensure_ascii=False) + "\n")

    def trash_log(self) -> list[dict]:
        out = []
        try:
            with open(self.data_dir / "trash_log.jsonl", encoding="utf-8") as f:
                for line in f:
                    try:
                        out.append(json.loads(line))
                    except ValueError:
                        continue
        except FileNotFoundError:
            pass
        return out

    # --- 調査の状態 ---------------------------------------------------------------
    def locations(self) -> list[dict]:
        with self.lock:
            return sorted((dict(x) for x in self._scan["locations"]), key=lambda x: x["priority"])

    def set_locations(self, locations) -> None:
        """調査場所の一覧を更新する（前回の状態・件数は引き継ぐ）。"""
        with self.lock:
            old = {x["path"]: x for x in self._scan["locations"]}
            new = []
            for loc in locations:
                prev = old.get(loc.path, {})
                status = prev.get("status", "未調査")
                new.append({"path": loc.path, "kind": loc.kind, "label": loc.label, "priority": loc.priority,
                            "status": "中断" if status == "調査中" else status, "files": prev.get("files", 0),
                            "bytes": prev.get("bytes", 0), "last_completed": prev.get("last_completed")})
            for path in set(old) - {x["path"] for x in new}:
                self._scan["stats"].pop(path, None)
                self.forget_location(path)
            self._scan["locations"] = new
            self.save_scan_state()

    def update_location(self, path: str, **fields) -> None:
        with self.lock:
            for x in self._scan["locations"]:
                if x["path"] == path:
                    x.update(fields)
            self.save_scan_state()

    def add_skip(self, path: str, location: str, reason: str, kind: str) -> None:
        with self.lock:
            skips = self._scan["skips"]
            if sum(1 for s in skips if s["kind"] == kind) < 5000:  # 記録の上限（件数は別に数える）
                skips.append({"path": path, "location": location, "reason": reason, "kind": kind})
            counts = self._scan.setdefault("skip_counts", {})
            counts.setdefault(location, {}).setdefault(kind, 0)
            counts[location][kind] += 1

    def clear_skips(self, location: str) -> None:
        with self.lock:
            self._scan["skips"] = [s for s in self._scan["skips"] if s["location"] != location]
            self._scan.setdefault("skip_counts", {}).pop(location, None)

    def skips(self, kind: str | None = None, location: str | None = None) -> list[dict]:
        with self.lock:
            return [dict(s) for s in self._scan["skips"] if (kind is None or s["kind"] == kind)
                    and (location is None or s["location"] == location)]

    def skip_count(self, kind: str) -> int:
        with self.lock:
            return sum(c.get(kind, 0) for c in self._scan.get("skip_counts", {}).values())

    def set_location_stats(self, path: str, stats: dict) -> None:
        with self.lock:
            self._scan["stats"][path] = stats

    def location_stats(self) -> dict[str, dict]:
        with self.lock:
            return dict(self._scan["stats"])

    def set_similar_groups(self, groups: dict[int, list[int]]) -> None:
        with self.lock:
            for r in self._rows.values():
                r["similar_group"] = None
            stored = {}
            for gid, ids in groups.items():
                for i in ids:
                    self._rows[i]["similar_group"] = gid
                stored[str(gid)] = [self._rows[i]["path"] for i in ids]
            self._scan["similar_groups"] = stored
            self.save_scan_state()

    def set_video_groups(self, groups: list[list[int]]) -> None:
        """類似録画のグループ（パスで保存）。"""
        with self.lock:
            self._scan["video_groups"] = [[self._rows[i]["path"] for i in g] for g in groups]
            self.save_scan_state()

    def video_groups(self) -> list[list[int]]:
        with self.lock:
            out = []
            for g in self._scan.get("video_groups", []):
                ids = [self._by_path[p] for p in g if p in self._by_path and not self._rows[self._by_path[p]]["missing"]]
                if len(ids) > 1:
                    out.append(ids)
            return out

    def save_scan_state(self) -> None:
        with self.lock:
            _write_json(self.data_dir / "scan_state.json", self._scan)

    def close(self) -> None:
        with self.lock:
            self.save_scan_state()

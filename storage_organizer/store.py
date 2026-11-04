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
import re
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


class StoreError(Exception):
    """保存に失敗した、または安全のため変更できない（画面には成功と表示しない）。"""


def _is_int_text(v) -> bool:
    """半角の 0〜9 だけからなる文字列か（"²" などは isdigit() でも int() できないため使わない）。"""
    return isinstance(v, str) and re.fullmatch(r"[0-9]+", v) is not None


def _is_num(v) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def _check_settings(d) -> str | None:
    if not isinstance(d, dict):
        return "形式が正しくありません（設定の一覧ではありません）"
    if not all(isinstance(k, str) for k in d):
        return "形式が正しくありません（項目名）"
    return None


def _check_user_state(d) -> str | None:
    if not isinstance(d, dict):
        return "形式が正しくありません（保護・判断の一覧ではありません）"
    for path, e in d.items():
        if not isinstance(path, str) or not isinstance(e, dict):
            return "形式が正しくありません（ファイルごとの記録）"
        for k, v in e.items():
            if k in ("protected", "favorite") and not isinstance(v, (bool, int)):
                return f"形式が正しくありません（{k} の値）"
            if k == "decision" and v not in DECISIONS:
                return "形式が正しくありません（判断の値）"
            if k == "last_viewed" and v is not None and not _is_num(v):
                return "形式が正しくありません（確認日時）"
            if k not in ("protected", "favorite", "decision", "last_viewed"):
                return f"形式が正しくありません（不明な項目 {k}）"
    return None


def _check_scan_state(d) -> str | None:
    """調査状態の中心（調査場所・スキップ）を検証する。派生キャッシュは _clean_derived で個別に扱う。"""
    if not isinstance(d, dict):
        return "形式が正しくありません（調査状態ではありません）"
    for key, typ in (("locations", list), ("skips", list)):
        if key in d and not isinstance(d[key], typ):
            return f"形式が正しくありません（{key}）"
    for x in d.get("locations", []):
        if not isinstance(x, dict) or not isinstance(x.get("path"), str) or not _is_num(x.get("priority", 0)):
            return "形式が正しくありません（調査場所）"
    for x in d.get("skips", []):
        if not isinstance(x, dict) or not all(isinstance(x.get(k), str) for k in ("path", "location", "reason", "kind")):
            return "形式が正しくありません（スキップした場所）"
    return None


def _clean_derived(d: dict, problems: list[str]) -> None:
    """派生キャッシュ（容量の集計・類似グループ）は、不正なら捨てる（次の調査・解析で作り直す）。"""
    def bad(key, what):
        d.pop(key, None)
        problems.append(f"scan_state.json: {what}の形式が正しくないため捨てました（次の調査で作り直します）")
    vg = d.get("video_groups", [])
    if not (isinstance(vg, list) and all(isinstance(g, list) and all(isinstance(p, str) for p in g) for g in vg)):
        bad("video_groups", "類似録画のグループ")
    sg = d.get("similar_groups", {})
    if not (isinstance(sg, dict) and all(_is_int_text(k) and isinstance(v, list)
                                         and all(isinstance(p, str) for p in v) for k, v in sg.items())):
        bad("similar_groups", "類似画像のグループ")
    pair = lambda v: isinstance(v, list) and len(v) == 2 and all(_is_num(x) for x in v)  # noqa: E731
    triple = lambda v: isinstance(v, list) and len(v) == 3 and _is_num(v[0]) and isinstance(v[1], str)  # noqa: E731
    stats = d.get("stats", {})
    ok = isinstance(stats, dict)
    for st in (stats.values() if ok else []):
        if not isinstance(st, dict):
            ok = False
            break
        for key in ("by_category", "by_folder"):
            if not (isinstance(st.get(key, {}), dict) and all(pair(v) for v in st.get(key, {}).values())):
                ok = False
        if not (isinstance(st.get("top_files", []), list) and all(triple(v) for v in st.get("top_files", []))):
            ok = False
        if not (isinstance(st.get("areas", []), list) and all(
                isinstance(v, list) and len(v) == 3 and isinstance(v[0], str) and _is_num(v[1])
                for v in st.get("areas", []))):
            ok = False
    if not ok:
        bad("stats", "容量の集計")
    if not isinstance(d.get("skip_counts", {}), dict):
        bad("skip_counts", "スキップの件数")


CHECKS = {"settings.json": _check_settings, "user_state.json": _check_user_state, "scan_state.json": _check_scan_state}


def _parse(path: Path, name: str | None = None):
    """(データ, 問題)。問題: None / 'missing' / 構文エラー / 形式違いの説明。name は検証に使う元のファイル名。"""
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except FileNotFoundError:
        return None, "missing"
    except UnicodeDecodeError:
        return None, "文字コードが正しくありません（途中までの書き込みの可能性）"
    except ValueError:
        return None, "JSON として読めません（壊れている・途中までの書き込みの可能性）"
    except OSError as e:
        return None, f"読み込めません（{e.strerror or e}）"
    problem = CHECKS[name or path.name](data)
    return (None, problem) if problem else (data, None)


PENDING_MARK = "protection_recovery_pending.json"


def _load_json(path: Path, default, problems: list[str], pending_mark: Path | None = None) -> tuple[object, bool]:
    """検証して読み込む。読めなければ元のファイルを退避し、直前のバックアップ（.bak）から復旧する。

    戻り値: (データ, 正しく読めたか（復旧できた場合を含む）)
    """
    data, problem = _parse(path)
    if problem is None:
        return data, True
    bak = path.with_suffix(path.suffix + ".bak")
    bak_data, bak_problem = _parse(bak, path.name) if bak.exists() else (None, "missing")
    if problem != "missing":
        moved = True
        if pending_mark is not None and bak_problem is not None:
            # 保護情報を復旧できない: 退避する前に「復旧待ち」の印を残す（再起動しても整理禁止を続けるため）
            try:
                _write_raw(pending_mark, {"file": path.name, "problem": problem, "at": time.time()})
            except OSError:
                moved = False  # 印を書けないときは、壊れたファイルを動かさない（次の起動でも検出できるように）
        if moved:
            try:  # 元のファイルは消さずに退避する
                os.replace(path, path.with_suffix(path.suffix + f".broken-{int(time.time())}"))
            except OSError:
                pass
    if pending_mark is not None and pending_mark.exists():
        problems.append(f"{path.name}: 保護・判断の記録の復旧待ちです（設定画面で初期化するまで整理操作を止めます）")
        return default, False
    if bak.exists():
        data, bproblem = bak_data, bak_problem
        if bproblem is None:
            if problem != "missing":
                problems.append(f"{path.name}: {problem}。直前のバックアップから復旧しました")
            return data, True
        problems.append(f"{path.name}: {problem}。バックアップも使えません（{bproblem}）")
        return default, False
    if problem == "missing":
        return default, True  # 初めての起動
    problems.append(f"{path.name}: {problem}。バックアップがないため復旧できません")
    return default, False


def _write_raw(path: Path, data) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def _write_json(path: Path, data) -> None:
    """一時ファイルに書いて置き換える。直前の正常なファイルは .bak として残す。失敗したら StoreError。"""
    tmp = path.with_suffix(path.suffix + ".tmp")
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)
            f.flush()
            os.fsync(f.fileno())
        if path.exists():
            os.replace(path, path.with_suffix(path.suffix + ".bak"))
        os.replace(tmp, path)
    except OSError as e:
        raise StoreError(f"{path.name} を保存できませんでした（{e.strerror or e}）。変更は反映されていません") from e


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
        self.load_problems: list[str] = []
        self._settings, _ = _load_json(self.data_dir / "settings.json", {}, self.load_problems)
        self._user, self.protection_ok = _load_json(self.data_dir / "user_state.json", {}, self.load_problems,
                                                    pending_mark=self.data_dir / PENDING_MARK)
        self._scan, _ = _load_json(self.data_dir / "scan_state.json", {}, self.load_problems)
        _clean_derived(self._scan, self.load_problems)
        self._sanitize_settings()
        self._scan.setdefault("locations", [])
        self._scan.setdefault("skips", [])
        self._scan.setdefault("stats", {})
        self._scan.setdefault("similar_groups", {})
        self._rows: dict[int, dict] = {}
        self._by_path: dict[str, int] = {}
        # 調査結果の世代番号（ファイルの情報・重複・類似の関係が変わるたびに進む）と、調査・整理の排他
        self.generation = 0
        self.scan_active = False
        self.organize_active = False
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
                self.load_problems.append(f"{f.name}: 調査結果を読めません（次の調査で作り直します）")
                continue  # 壊れた一覧は使わない（次の調査で作り直される）
            files = data.get("files") if isinstance(data, dict) else None
            if not isinstance(files, list) or not all(isinstance(r, list) and len(r) == 8 for r in files):
                self.load_problems.append(f"{f.name}: 調査結果の形式が正しくありません（次の調査で作り直します）")
                continue
            for rec in files:
                path, name, folder, size, mtime, ext, kind, cloud = rec
                if not (isinstance(path, str) and _is_num(size) and _is_num(mtime)):
                    continue
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
        bad = 0
        try:
            with open(self._analysis_path, encoding="utf-8") as f:
                for line in f:
                    self._analysis_lines += 1
                    try:
                        rec = json.loads(line)
                    except ValueError:
                        bad += 1
                        continue  # 途中で落ちたときの壊れた行は読み飛ばす
                    if not (isinstance(rec, dict) and isinstance(rec.get("k"), str) and isinstance(rec.get("f"), dict)):
                        bad += 1
                        continue
                    fields = {k: v for k, v in rec["f"].items() if k in ANALYSIS_FIELDS}
                    cache.setdefault(rec["k"], {}).update(fields)
        except FileNotFoundError:
            pass
        except (OSError, UnicodeDecodeError):
            self.load_problems.append("analysis.jsonl: 解析結果を読めません（必要に応じて解析し直します）")
        if bad:
            self.load_problems.append(f"analysis.jsonl: 読めない行が {bad} 行ありました（その分は解析し直します）")
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

    SETTING_TYPES = {"ffmpeg_dir": (str,), "auto_scan_on_start": (str,), "scan_gen": (str, int),
                     "scan_started": (str,), "scan_finished": (str,), "legacy_migrated": (str,),
                     "files_scanned": (str,), "analysis_state": (str,),
                     "view_history_started": (str,), "extra_locations": (list,), "excluded_folders": (list,),
                     "disabled_locations": (list,), "not_scanned_drives": (list,)}

    def _sanitize_settings(self) -> None:
        """型が違う設定だけを使わない（ほかの設定は残す）。ファイルは次に保存するときに直る。"""
        bad = []
        for key, types in self.SETTING_TYPES.items():
            v = self._settings.get(key)
            if v is None:
                continue
            ok = isinstance(v, types) and not isinstance(v, bool)
            if ok and key == "scan_gen":
                ok = _is_int_text(str(v))
            if ok and isinstance(v, list):
                ok = all(isinstance(x, str) for x in v)
            if not ok:
                bad.append(key)
                self._settings.pop(key)
        if bad:
            self.load_problems.append(f"settings.json: 値の形式が正しくない設定（{', '.join(bad)}）を使わずに起動しました")

    # --- 設定 -------------------------------------------------------------------
    def get_setting(self, key: str, default=None):
        with self.lock:
            return self._settings.get(key, default)

    def set_setting(self, key: str, value) -> None:
        with self.lock:
            new = dict(self._settings)
            if value is None:
                new.pop(key, None)
            else:
                new[key] = value
            _write_json(self.data_dir / "settings.json", new)  # 書けてから反映する
            self._settings = new

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
            self.generation += 1
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
            self.generation += 1
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
            if any(k in fields for k in ("sha256", "vsig")):
                self.generation += 1  # 重複・類似の関係が変わりうる
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
            self.generation += 1
            n = 0
            for r in self._rows.values():
                if r["location"] == location and r["seen_gen"] < gen and not r["missing"]:
                    r["missing"] = 1
                    n += 1
            return n

    def hide(self, media_id: int, clear_decision: bool = True) -> None:
        """一覧から外す（ソースコードの .ts・除外した場所など）。保護・判断の記録は残す。"""
        with self.lock:
            self.generation += 1
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
            if not self.protection_ok:
                raise StoreError("保護・判断の記録を読み込めなかったため、変更できません（設定画面で対処できます）")
            row = self._rows[media_id]
            new = dict(self._user)
            entry = dict(new.get(row["path"], {}))
            for k, v in fields.items():
                if v in (None, 0, False):
                    entry.pop(k, None)
                else:
                    entry[k] = v
            if entry:
                new[row["path"]] = entry
            else:
                new.pop(row["path"], None)
            _write_json(self.data_dir / "user_state.json", new)  # 書けてから画面上の状態を変える
            self._user = new
            for k, v in fields.items():
                row[k] = int(bool(v)) if k in ("protected", "favorite") else v

    def reset_user_state(self) -> None:
        """読み込めなかった保護・判断の記録を、利用者の明示の操作で初期化する（壊れたファイルは退避済み）。"""
        with self.lock:
            _write_json(self.data_dir / "user_state.json", {})
            try:
                os.remove(self.data_dir / PENDING_MARK)  # 利用者が明示して初期化したので、復旧待ちを解除する
            except FileNotFoundError:
                pass
            except OSError as e:
                raise StoreError(f"復旧待ちの印を消せませんでした（{e.strerror or e}）") from e
            self._user = {}
            self.protection_ok = True
            for r in self._rows.values():
                r.update(protected=0, decision=None, favorite=0, last_viewed=None)

    def import_user_state(self, entries: dict[str, dict]) -> None:
        """以前の版から保護・判断を取り込む（すでにある判断は上書きしない）。"""
        with self.lock:
            if not self.protection_ok:
                raise StoreError("保護・判断の記録を読み込めなかったため、取り込めません")
            new = {p: dict(e) for p, e in self._user.items()}
            for path, entry in entries.items():
                cur = new.setdefault(path, {})
                for k, v in entry.items():
                    cur.setdefault(k, v)
            _write_json(self.data_dir / "user_state.json", new)
            self._user = new
            for path, cur in new.items():
                rid = self._by_path.get(path)
                if rid is not None:
                    row = self._rows[rid]
                    for k, v in cur.items():
                        row[k] = int(bool(v)) if k in ("protected", "favorite") else v

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
        """このアプリで確認した日時を記録する（利用者の判断ではないので、記録できなくても表示は続ける）。"""
        if not self.protection_ok:
            return  # 保護・判断の記録が復旧待ち: 記録しない（情報の確認はできる）
        try:
            if self.get_setting("view_history_started") is None:
                self.set_setting("view_history_started", str(time.time()))
            self._set_user(media_id, last_viewed=time.time())
        except StoreError:
            pass

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
            self.generation += 1
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
            self.generation += 1
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

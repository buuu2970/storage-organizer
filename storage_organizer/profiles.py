"""プロファイル（ログイン不要の個人設定）と、プロファイルごとの AI の記録（JSON・データベースは使わない）。

- プロファイルは「ユーザー1」「ユーザー2」… の自動番号と、固有の内部 ID だけ（パスワード・メールアドレスは不要）
- 動画の一覧・判断（残す／整理候補／保留）・保護・お気に入りは、全プロファイルで共通（store.py）
- プロファイルごとに分けるのは、AI に教えた判断・ジャンル・AI の判断基準・初期学習の回答・整理目標・案内の表示状態
- 保存は store._write_json（一時ファイル → 読み直して確認 → .bak を残して置き換え）。壊れていたら .bak から読む
"""

from __future__ import annotations

import json
import os
import threading
import time
import uuid
from pathlib import Path

from .store import StoreError, _write_json

PRESET_GENRES = ["ゲーム", "授業・講義", "会議・打ち合わせ", "配信・実況", "趣味・その他"]
LABELS = ("keep", "review")  # AI に教える判断: 残す / 整理候補（保留は教えない）
ALL_SCOPE = "all"


def genre_scope(name: str) -> str:
    return "genre:" + name


def _read(path: Path):
    """JSON を読む。読めなければ .bak を試す。どちらも読めなければ None（壊れたファイルは消さない）。"""
    for p in (path, path.with_suffix(path.suffix + ".bak")):
        try:
            with open(p, encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict):
                return data
        except (OSError, ValueError):
            continue
    return None


class ProfileStore:
    """プロファイルの一覧と、いま使っているプロファイル。"""

    def __init__(self, data_dir: Path | str) -> None:
        self.root = Path(data_dir)
        self.file = self.root / "profiles.json"
        self.lock = threading.RLock()
        data = _read(self.file) if self.file.exists() or self.file.with_suffix(".json.bak").exists() else None
        ok = (isinstance(data, dict) and isinstance(data.get("profiles"), list)
              and all(isinstance(p, dict) and isinstance(p.get("id"), str) and isinstance(p.get("name"), str)
                      for p in data["profiles"]) and data["profiles"])
        if not ok:
            if self.file.exists():  # 壊れた一覧は消さずに退避する（中の AI の記録はフォルダごと残る）
                try:
                    os.replace(self.file, self.file.with_suffix(f".json.broken-{int(time.time())}"))
                except OSError:
                    pass
            first = {"id": uuid.uuid4().hex, "name": "ユーザー1"}
            data = {"profiles": [first], "current": first["id"]}
            self._save(data)
        if data.get("current") not in {p["id"] for p in data["profiles"]}:
            data["current"] = data["profiles"][0]["id"]
        self._data = data
        self._cache: dict[str, ProfileData] = {}

    def _save(self, data) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        _write_json(self.file, data)

    def profiles(self) -> list[dict]:
        with self.lock:
            return [dict(p) for p in self._data["profiles"]]

    def current_id(self) -> str:
        with self.lock:
            return self._data["current"]

    def current_name(self) -> str:
        with self.lock:
            return next(p["name"] for p in self._data["profiles"] if p["id"] == self._data["current"])

    def create(self) -> dict:
        """新しいプロファイルを作る（「ユーザーN」。N は使われていない一番小さい番号）。切り替えはしない。"""
        with self.lock:
            used = set()
            for p in self._data["profiles"]:
                if p["name"].startswith("ユーザー") and p["name"][4:].isascii() and p["name"][4:].isdigit():
                    used.add(int(p["name"][4:]))
            n = 1
            while n in used:
                n += 1
            prof = {"id": uuid.uuid4().hex, "name": f"ユーザー{n}"}
            new = {**self._data, "profiles": self._data["profiles"] + [prof]}
            self._save(new)
            self._data = new
            return dict(prof)

    def switch(self, profile_id: str) -> None:
        with self.lock:
            if profile_id not in {p["id"] for p in self._data["profiles"]}:
                raise ValueError(profile_id)
            new = {**self._data, "current": profile_id}
            self._save(new)
            self._data = new

    def data(self, profile_id: str | None = None) -> "ProfileData":
        """プロファイルの AI の記録。切り替えても、前のプロファイルの記録は消さない。"""
        with self.lock:
            pid = profile_id or self._data["current"]
            if pid not in self._cache:
                self._cache[pid] = ProfileData(self.root / "profiles" / pid)
            return self._cache[pid]

    def current(self) -> "ProfileData":
        return self.data()


_EMPTY = {"genres": list(PRESET_GENRES), "video_genres": {}, "labels": {}, "suspended": {}, "models": {},
          "sample_answers": {}, "genre_model": None, "initial_state": None, "goal": None, "flags": {},
          "session": {}}


class ProfileData:
    """1つのプロファイルの AI の記録（ai.json）。変更のたびに安全に保存する。"""

    def __init__(self, folder: Path) -> None:
        self.folder = Path(folder)
        self.file = self.folder / "ai.json"
        self.lock = threading.RLock()
        self.problem: str | None = None
        data = _read(self.file)
        if data is None and (self.file.exists() or self.file.with_suffix(".json.bak").exists()):
            self.problem = "AI の記録を読み込めませんでした。新しい記録で始めます（壊れたファイルは消していません）"
            try:
                os.replace(self.file, self.file.with_suffix(f".json.broken-{int(time.time())}"))
            except OSError:
                pass
        self._d = {k: (json.loads(json.dumps(v)) if isinstance(v, (dict, list)) else v) for k, v in _EMPTY.items()}
        if isinstance(data, dict):
            for k in _EMPTY:
                if k in data and (data[k] is None or isinstance(data[k], type(_EMPTY[k]) if _EMPTY[k] is not None
                                                                else (dict, str))):
                    self._d[k] = data[k]

    # --- 保存 ---------------------------------------------------------------
    def _commit(self, new: dict) -> None:
        """書けてから記録を変える（失敗したら StoreError で、変更は反映しない）。"""
        self.folder.mkdir(parents=True, exist_ok=True)
        _write_json(self.file, new)
        self._d = new

    def _update(self, **changes) -> None:
        with self.lock:
            self._commit({**self._d, **changes})

    def get(self, key: str):
        with self.lock:
            return json.loads(json.dumps(self._d[key])) if isinstance(self._d[key], (dict, list)) else self._d[key]

    # --- ジャンル -------------------------------------------------------------
    def genres(self) -> list[str]:
        return self.get("genres")

    def add_genre(self, name: str) -> None:
        name = " ".join(name.split())
        if not name or len(name) > 30:
            raise ValueError("ジャンル名は1〜30文字で入力してください")
        with self.lock:
            if name not in self._d["genres"]:
                self._update(genres=self._d["genres"] + [name])

    def video_genres(self, path: str) -> list[str] | None:
        """利用者が決めたジャンル（決めていなければ None）。"""
        with self.lock:
            v = self._d["video_genres"].get(path)
            return list(v) if isinstance(v, list) else None

    def set_video_genres(self, path: str, genres: list[str]) -> None:
        """動画のジャンルを利用者が決める。外したジャンルで教えた判断は、再確認まで学習に使わない。"""
        genres = list(dict.fromkeys(g for g in genres if isinstance(g, str) and g))
        with self.lock:
            old = self._d["video_genres"].get(path) or []
            vg = dict(self._d["video_genres"])
            vg[path] = genres
            labels = {s: dict(v) for s, v in self._d["labels"].items()}
            suspended = {s: dict(v) for s, v in self._d["suspended"].items()}
            for g in set(old) - set(genres):
                scope = genre_scope(g)
                if path in labels.get(scope, {}):
                    suspended.setdefault(scope, {})[path] = labels[scope].pop(path)
            self._commit({**self._d, "video_genres": vg, "labels": labels, "suspended": suspended})

    # --- AI に教えた判断 ----------------------------------------------------------
    def set_label(self, scopes: list[str], path: str, label: str | None) -> None:
        """厳選の回答を記録する（label: keep / review / None=教えない）。複数のジャンルには共通の回答を入れる。"""
        if label is not None and label not in LABELS:
            raise ValueError(label)
        with self.lock:
            labels = {s: dict(v) for s, v in self._d["labels"].items()}
            suspended = {s: dict(v) for s, v in self._d["suspended"].items()}
            for scope in scopes:
                bucket = labels.setdefault(scope, {})
                suspended.get(scope, {}).pop(path, None)
                if label is None:
                    bucket.pop(path, None)
                else:
                    bucket[path] = label
            self._commit({**self._d, "labels": labels, "suspended": suspended})

    def labels(self, scope: str) -> dict[str, str]:
        with self.lock:
            return dict(self._d["labels"].get(scope, {}))

    def suspended_count(self, scope: str) -> int:
        with self.lock:
            return len(self._d["suspended"].get(scope, {}))

    # --- AI の判断基準（覚えた時点の回答の写し） ------------------------------------------
    def learn(self, scope: str) -> dict:
        """いまの回答を AI の判断基準として覚え直させる。"""
        with self.lock:
            model = {"labels": dict(self._d["labels"].get(scope, {})), "at": time.time()}
            models = {**self._d["models"], scope: model}
            self._commit({**self._d, "models": models})
            return dict(model)

    def model(self, scope: str) -> dict | None:
        with self.lock:
            m = self._d["models"].get(scope)
            return {"labels": dict(m.get("labels", {})), "at": m.get("at")} if isinstance(m, dict) else None

    def reset_model(self, scope: str) -> None:
        """AI の判断基準を最初の状態に戻す（その範囲だけ。教えた判断・動画・保護・お気に入りは変えない）。"""
        with self.lock:
            models = {k: v for k, v in self._d["models"].items() if k != scope}
            self._commit({**self._d, "models": models})

    def new_labels_since_learn(self, scope: str) -> int:
        with self.lock:
            cur = self._d["labels"].get(scope, {})
            learned = (self._d["models"].get(scope) or {}).get("labels", {})
            return sum(1 for p, v in cur.items() if learned.get(p) != v) + sum(1 for p in learned if p not in cur)

    # --- 初期学習 ---------------------------------------------------------------
    def sample_answers(self) -> dict[str, dict]:
        return self.get("sample_answers")

    def set_sample_answer(self, sample: str, genres: list[str] | None, skipped: bool = False) -> None:
        with self.lock:
            answers = dict(self._d["sample_answers"])
            if genres is None and not skipped:
                answers.pop(sample, None)
            else:
                answers[sample] = {"genres": list(dict.fromkeys(genres or [])), "skipped": bool(skipped)}
            state = self._d["initial_state"] if self._d["initial_state"] == "done" else "in_progress"
            self._commit({**self._d, "sample_answers": answers, "initial_state": state})

    def set_initial_state(self, state: str | None) -> None:
        if state not in (None, "in_progress", "skipped", "done"):
            raise ValueError(state)
        self._update(initial_state=state)

    def initial_state(self) -> str | None:
        return self.get("initial_state")

    def learn_genres(self) -> dict:
        with self.lock:
            model = {"answers": json.loads(json.dumps(self._d["sample_answers"])), "at": time.time()}
            self._commit({**self._d, "genre_model": model, "initial_state": "done"})
            return model

    def genre_model(self) -> dict | None:
        return self.get("genre_model")

    def reset_genre_model(self) -> None:
        self._update(genre_model=None)

    # --- 整理目標・案内・続き ---------------------------------------------------------
    def goal(self) -> dict | None:
        return self.get("goal")

    def set_goal(self, target_bytes: int | None, drive: str | None = None, free_at_set: int | None = None) -> None:
        if target_bytes is None:
            self._update(goal=None)
            return
        if target_bytes <= 0:
            raise ValueError("目標は 0 より大きい容量にしてください")
        self._update(goal={"bytes": int(target_bytes), "drive": drive, "free_at_set": free_at_set, "at": time.time()})

    def flag(self, name: str) -> bool:
        with self.lock:
            return bool(self._d["flags"].get(name))

    def set_flag(self, name: str, value: bool = True) -> None:
        with self.lock:
            self._commit({**self._d, "flags": {**self._d["flags"], name: bool(value)}})

    def session(self) -> dict:
        return self.get("session")

    def set_session(self, **fields) -> None:
        try:
            with self.lock:
                self._commit({**self._d, "session": {**self._d["session"], **fields}})
        except StoreError:
            pass  # 続きの位置は便利機能。保存できなくても判断の記録には影響しない

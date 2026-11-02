"""プロファイル（ログイン不要の利用者の区別）。

- 名前は「ユーザー1」「ユーザー2」…の自動番号。パスワードやメールアドレスは扱わない
- 各プロファイルは固定の内部 ID と専用のデータフォルダ（recsort.db・モデル・サムネイル）を持つ。
  ジャンル・初期学習の回答・モデル・動画一覧はフォルダごと分かれるので混ざらない
- 初期学習用サンプルの登録情報と解析キャッシュは共通フォルダ（samples）に置く
"""

from __future__ import annotations

import json
import os
import time
import uuid
from pathlib import Path

from .db import default_data_dir

PROFILES_FILE = "profiles.json"


class ProfileManager:
    def __init__(self, root: Path | str | None = None) -> None:
        self.root = Path(root) if root else default_data_dir()
        self.root.mkdir(parents=True, exist_ok=True)
        self.file = self.root / PROFILES_FILE
        self._data = self._load()

    @property
    def samples_root(self) -> Path:
        return self.root / "samples"

    def _load(self) -> dict:
        try:
            data = json.loads(self.file.read_text(encoding="utf-8"))
            if data.get("profiles"):
                return data
        except (OSError, ValueError, AttributeError):
            pass
        # 初回: 以前の版のデータ（データフォルダ直下の recsort.db）があれば「ユーザー1」として引き継ぐ
        legacy = (self.root / "recsort.db").exists()
        first = self._new_profile(1, "." if legacy else None)
        data = {"profiles": [first], "current": first["id"]}
        self._write(data)
        return data

    @staticmethod
    def _new_profile(number: int, folder: str | None = None) -> dict:
        pid = uuid.uuid4().hex
        return {"id": pid, "number": number, "name": f"ユーザー{number}",
                "dir": folder or f"profiles/{pid}", "created_at": time.time()}

    def _write(self, data: dict) -> None:
        tmp = self.file.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
        os.replace(tmp, self.file)

    def profiles(self) -> list[dict]:
        return list(self._data["profiles"])

    def current(self) -> dict:
        cur = self._data.get("current")
        return next((p for p in self._data["profiles"] if p["id"] == cur), self._data["profiles"][0])

    def get(self, profile_id: str) -> dict:
        for p in self._data["profiles"]:
            if p["id"] == profile_id:
                return p
        raise KeyError(profile_id)

    def add(self) -> dict:
        number = max(p["number"] for p in self._data["profiles"]) + 1
        profile = self._new_profile(number)
        self._data["profiles"].append(profile)
        self._write(self._data)
        return profile

    def set_current(self, profile_id: str) -> None:
        self.get(profile_id)
        self._data["current"] = profile_id
        self._write(self._data)

    def data_dir(self, profile_id: str | None = None) -> Path:
        profile = self.get(profile_id) if profile_id else self.current()
        path = (self.root / profile["dir"]).resolve()
        path.mkdir(parents=True, exist_ok=True)
        return path

"""反復試験の共通部品。期待値はアプリの判定処理を使わず、生成時に決めた「正解」から求める。"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import random
import string
import sys
import traceback
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

MP4 = b"\x00\x00\x00\x18ftypmp42"
JP = "録画配信ゲーム実況ボス戦第回テスト高画質低画質編集前後　"


class Fail(AssertionError):
    """アプリの不具合と思われる失敗（期待した性質が成り立たない）。"""


class HarnessError(Exception):
    """試験ハーネス自体の不具合（アプリの不具合とは区別して集計する）。"""


def check(cond, msg, record):
    record["checks"] += 1
    if not cond:
        raise Fail(msg)


def name(rng: random.Random, ext: str, long: bool = False) -> str:
    kind = rng.random()
    if kind < 0.3:
        base = "".join(rng.choice(JP) for _ in range(rng.randint(2, 10)))
    elif kind < 0.5:
        base = " ".join("".join(rng.choice(string.ascii_letters) for _ in range(rng.randint(2, 6)))
                        for _ in range(rng.randint(2, 4)))
    else:
        base = "rec_" + "".join(rng.choice(string.ascii_lowercase + string.digits) for _ in range(rng.randint(3, 8)))
    if long:
        base = (base + "_長い名前") * 3
    base = base.strip() or "x"
    # Linux のファイル名の上限（255 バイト）を超えないように
    while len((base + ext).encode("utf-8")) > 200:
        base = base[:-1]
    return base + ext


def unique(rng, folder: Path, ext: str, long: bool = False) -> Path:
    for _ in range(50):
        p = folder / name(rng, ext, long)
        if not p.exists():
            return p
    return folder / f"fallback_{rng.randrange(10**9)}{ext}"


def fake_mp4(path: Path, content: bytes) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(MP4 + content)
    return path


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


class MockTrash:
    """ごみ箱の代わり: ファイルを専用フォルダへ移す（消さない）。失敗・遅延を注入できる。"""

    def __init__(self, folder: Path, fail_names=(), hook=None, pre=None):
        self.folder = folder
        self.folder.mkdir(parents=True, exist_ok=True)
        self.fail_names = set(fail_names)
        self.hook = hook
        self.pre = pre  # 移動の直前に呼ぶ（その瞬間の状態を確かめるため）
        self.moved: list[str] = []
        self.calls = 0

    def __call__(self, path: str) -> None:
        from storage_organizer import trash
        self.calls += 1
        if self.pre:
            self.pre(path)
        if os.path.basename(path) in self.fail_names:
            raise trash.TrashError("ごみ箱へ移動できませんでした（試験で注入した失敗）")
        dest = self.folder / f"{len(self.moved):04d}_{os.path.basename(path)}"
        os.replace(path, dest)
        self.moved.append(path)
        if self.hook:
            self.hook(len(self.moved), path)


# --- 類似判定用の特徴データ（ファイルなし） -------------------------------------------
def frames_family(rng: np.random.Generator, kind: str, H: int, W: int, n: int = 12) -> list[np.ndarray]:
    if kind == "scene":  # 録画に近い場面: なめらかな明暗の広がり＋図形。場面ごとに動き、素材ごとに違う
        field = rng.normal(128, 50, (3, 4))
        out = []
        for i in range(n):
            f = field + rng.normal(0, 25, (3, 4))
            img = np.kron(f, np.ones((H // 3 + 1, W // 4 + 1)))[:H, :W]
            img = (img + np.roll(img, 2, 0) + np.roll(img, 2, 1) + np.roll(img, -2, 0) + np.roll(img, -2, 1)) / 5
            y, x = rng.integers(0, H - 6), rng.integers(0, W - 8)
            img[y:y + 6, x:x + 8] = rng.uniform(0, 255)
            out.append(img + rng.normal(0, 3, (H, W)))
        return out
    if kind == "noise":
        return [rng.normal(128, 40, (H, W)) for _ in range(n)]
    if kind == "gradient":
        return [np.tile(np.linspace(rng.uniform(0, 100), rng.uniform(150, 255), W), (H, 1)) + rng.normal(0, 12, (H, W))
                for _ in range(n)]
    if kind == "text":
        out = []
        for _ in range(n):
            img = np.full((H, W), rng.uniform(10, 60))
            img[rng.integers(0, H, 60), rng.integers(0, W, 60)] = 230
            out.append(img)
        return out
    if kind == "fine":
        out = []
        for _ in range(n):
            cb = (np.indices((H, W)).sum(axis=0) % 2) * rng.uniform(40, 80) + 90
            out.append(cb + rng.normal(0, 6, (H, W)) * (rng.random((H, W)) > 0.5))
        return out
    if kind == "black":
        return [np.zeros((H, W)) for _ in range(n)]
    if kind == "lowinfo":  # 大半が黒画面
        return [rng.normal(128, 40, (H, W)) if i in (3, 8) else np.zeros((H, W)) for i in range(n)]
    raise HarnessError(kind)


def mild_variant(rng: np.random.Generator, frames: list[np.ndarray], allow_downscale: bool = True) -> list[np.ndarray]:
    """同じ録画の再圧縮・縮小に相当する、軽い変形（同じ内容として扱われるべきもの）。

    縮小は、なめらかな構造をもつ場面にだけ使う（画素ごとのノイズや細かい模様は縮小で消えるため、
    実際の録画の縮小とは違う。これを「見逃し」と数えるのは試験の誤り）。
    """
    k = rng.integers(0, 3 if allow_downscale else 2)
    if k == 0:
        return [f + rng.normal(0, rng.uniform(1, 5), f.shape) for f in frames]
    if k == 1:
        a, b = rng.uniform(0.9, 1.1), rng.uniform(-10, 10)
        return [f * a + b for f in frames]
    H, W = frames[0].shape
    return [np.kron(f.reshape(H // 2, 2, W // 2, 2).mean(axis=(1, 3)), np.ones((2, 2))) for f in frames]


def to_sig(frames, duration: float, version: int) -> dict:
    return {"v": version, "duration": duration,
            "frames": [base64.b64encode(np.clip(f, 0, 255).astype(np.uint8).tobytes()).decode() for f in frames]}


def run_trial(fn, trial: int, category: str, seed: int, workdir: Path) -> dict:
    record = {"trial": trial, "category": category, "seed": seed, "setup": {}, "ops": [], "checks": 0,
              "result": "ok", "kind": None, "message": ""}
    try:
        fn(seed, workdir, record)
    except Fail as e:
        record.update(result="fail", kind="app", message=str(e), trace=traceback.format_exc()[-2000:])
    except HarnessError as e:
        record.update(result="fail", kind="harness", message=str(e), trace=traceback.format_exc()[-2000:])
    except Exception as e:  # noqa: BLE001  予期しない例外はアプリの不具合として記録（原因の確認で分類し直す）
        record.update(result="fail", kind="exception", message=f"{type(e).__name__}: {e}",
                      trace=traceback.format_exc()[-3000:])
    return record


def write_jsonl(path: Path, rec: dict) -> None:
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False, default=str) + "\n")

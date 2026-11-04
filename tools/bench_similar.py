"""類似比較の性能計測（ファイルは作らず、合成した特徴データだけで測る）。

使い方: python tools/bench_similar.py
同じ長さに集中した 100 本・1,000 本で、比較とグループ化の時間・組数・メモリを測る。
「同じ録画の変形（明るさの小さな揺らぎ・ノイズ）」を混ぜ、検出が保たれるかも確かめる。
動画からのシグネチャ抽出（実ファイルの解析）の時間は、tools/eval_similar.py で別に測る。
"""

from __future__ import annotations

import base64
import json
import sys
import threading
import time
import tracemalloc
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from storage_organizer import similar  # noqa: E402


def make_items(n: int, pairs: int, seed: int = 0) -> tuple[dict, set]:
    rng = np.random.default_rng(seed)
    items, truth = {}, set()
    for i in range(n):
        base = rng.normal(128, 40, (similar.N_POINTS, similar.H, similar.W))
        frames = [np.clip(f, 0, 255).astype(np.uint8) for f in base]
        items[i] = {"v": similar.VERSION, "duration": 600.0,
                    "frames": [base64.b64encode(f.tobytes()).decode() for f in frames]}
    for k in range(pairs):  # 同じ録画の変形版（再圧縮・縮小相当の揺らぎ）
        src = items[k]
        frames = []
        for f in src["frames"]:
            a = np.frombuffer(base64.b64decode(f), np.uint8).astype(np.float32)
            a = np.clip(a * 0.97 + 3 + rng.normal(0, 6, a.shape), 0, 255).astype(np.uint8)
            frames.append(base64.b64encode(a.tobytes()).decode())
        items[n + k] = {"v": similar.VERSION, "duration": 600.4, "frames": frames}
        truth.add((k, n + k))
    return items, truth


def measure(n: int, pairs: int) -> dict:
    items, truth = make_items(n, pairs)
    tracemalloc.start()
    t0 = time.perf_counter()
    groups, stats = similar.group(items)
    elapsed = time.perf_counter() - t0
    _, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    found = {tuple(sorted(g)) for g in groups}
    detected = sum(1 for p in truth if tuple(sorted(p)) in found)
    total = len(items)
    return {"videos": total, "all_pairs_same_length": total * (total - 1) // 2, **stats,
            "seconds": round(elapsed, 3), "peak_memory_mb": round(peak / 1e6, 1),
            "planted_pairs": len(truth), "detected": detected,
            "false_groups": [g for g in found if g not in {tuple(sorted(p)) for p in truth}]}


def measure_cancel(n: int) -> dict:
    items, _ = make_items(n, 10)
    cancel = threading.Event()
    seen = []

    def progress(stage, done, total):
        seen.append(stage)
        if stage == "compare" and done >= 40:
            cancel.set()
    t0 = time.perf_counter()
    try:
        similar.group(items, cancel=cancel, progress=progress)
        result = "完了してしまった"
    except similar.Cancelled:
        result = "キャンセルされた（途中結果なし）"
    return {"videos": len(items), "result": result, "seconds_until_stop": round(time.perf_counter() - t0, 3),
            "stages_seen": sorted(set(seen))}


if __name__ == "__main__":
    out = {"100": measure(100, 10), "1000": measure(1000, 20), "cancel_1000": measure_cancel(1000)}
    print(json.dumps(out, ensure_ascii=False, indent=1))

"""類似比較の性能試験（合成した特徴データ・ファイルなし）。

条件: ばらばらな内容／似た画面が集中する内容（共通画面が多い・同じゲームの別の回に相当）× 100 本・1,000 本
段階ごと（候補抽出の準備・比較・グループ化）の時間、詳しく比べた組数、メモリ、キャンセルの応答を記録する。
上限（事前に決めた値）を超えたら、失敗として記録する。
"""

from __future__ import annotations

import json
import resource
import sys
import threading
import time
import tracemalloc
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import frames_family, mild_variant, to_sig  # noqa: E402
from storage_organizer import similar  # noqa: E402

LIMITS = {"seconds_total_1000": 60.0, "python_peak_mb": 200.0, "process_maxrss_mb": 1024.0, "cancel_seconds": 2.0}


def dataset(n: int, kind: str, seed: int = 7):
    rng = np.random.default_rng(seed)
    H, W = similar.H, similar.W
    items, truth = {}, []
    if kind == "distinct":
        for i in range(n):
            items[i] = to_sig(frames_family(rng, "scene", H, W), 600.0, similar.VERSION)
    else:  # 似た画面が集中: 共通のタイトル・メニュー画面が 12 場面中 7 場面、残りが回ごとに違う
        common = frames_family(rng, "scene", H, W)
        for i in range(n):
            own = frames_family(rng, "scene", H, W)
            frames = [common[k] if k in (0, 1, 2, 3, 9, 10, 11) else own[k] for k in range(12)]
            items[i] = to_sig(frames, 600.0, similar.VERSION)
    for k in range(min(20, n // 10)):  # 同じ録画の軽い変形を仕込む
        frames = [np.frombuffer(__import__("base64").b64decode(f), np.uint8).astype(float).reshape(H, W)
                  for f in items[k]["frames"]]
        items[n + k] = to_sig(mild_variant(rng, frames), 600.3, similar.VERSION)
        truth.append((k, n + k))
    return items, truth


def measure(n: int, kind: str) -> dict:
    items, truth = dataset(n, kind)
    stages: dict[str, float] = {}
    last = {"stage": None, "t": time.perf_counter()}

    def progress(stage, done, total):
        now = time.perf_counter()
        if stage != last["stage"]:
            if last["stage"]:
                stages[last["stage"]] = stages.get(last["stage"], 0) + now - last["t"]
            last.update(stage=stage, t=now)
    tracemalloc.start()
    t0 = time.perf_counter()
    groups, stats = similar.group(items, progress=progress)
    total = time.perf_counter() - t0
    stages[last["stage"]] = stages.get(last["stage"], 0) + time.perf_counter() - last["t"]
    _, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    found = {tuple(sorted(g)) for g in groups}
    detected = sum(any(a in g and b in g for g in found) for a, b in truth)
    return {"videos": len(items), "kind": kind, **stats, "seconds_total": round(total, 2),
            "seconds_by_stage": {k: round(v, 2) for k, v in stages.items()},
            "python_peak_mb": round(peak / 1e6, 1), "planted": len(truth), "detected": detected,
            "groups": len(groups), "largest_group": max((len(g) for g in groups), default=0)}


def cancel_response(n: int, kind: str, stage: str) -> dict:
    items, _ = dataset(n, kind)
    ev = threading.Event()
    marks = {}

    def progress(st, done, total):
        if st == stage and "set" not in marks and (st != "compare" or done >= n // 4):
            marks["set"] = time.perf_counter()
            ev.set()
    try:
        similar.group(items, cancel=ev, progress=progress)
        result = "完了してしまった（失敗）"
    except similar.Cancelled:
        result = "キャンセルされた"
    delay = time.perf_counter() - marks["set"] if "set" in marks else None
    return {"videos": len(items), "kind": kind, "stage": stage, "result": result,
            "seconds_after_cancel": round(delay, 3) if delay is not None else None}


def main():
    out = {"limits": LIMITS, "runs": [], "cancel": [], "violations": []}
    for kind in ("distinct", "similar_heavy"):
        for n in (100, 1000):
            r = measure(n, kind)
            out["runs"].append(r)
            print(json.dumps(r, ensure_ascii=False), flush=True)
            if n == 1000 and r["seconds_total"] > LIMITS["seconds_total_1000"]:
                out["violations"].append(f"{kind} 1000本: {r['seconds_total']} 秒（上限 {LIMITS['seconds_total_1000']}）")
            if r["python_peak_mb"] > LIMITS["python_peak_mb"]:
                out["violations"].append(f"{kind} {n}本: Python の割当 {r['python_peak_mb']}MB")
            if r["detected"] < r["planted"]:
                out["violations"].append(f"{kind} {n}本: 仕込んだ {r['planted']} 組のうち {r['detected']} 組しか検出しない")
        for stage in ("prepare", "compare", "group"):
            c = cancel_response(1000, kind, stage)
            out["cancel"].append(c)
            print(json.dumps(c, ensure_ascii=False), flush=True)
            if c["result"] != "キャンセルされた" or (c["seconds_after_cancel"] or 0) > LIMITS["cancel_seconds"]:
                out["violations"].append(f"キャンセル {kind} {stage}: {c}")
    rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024  # Linux は KB
    out["process_maxrss_mb"] = round(rss, 1)
    if rss > LIMITS["process_maxrss_mb"]:
        out["violations"].append(f"プロセス全体のメモリ {rss:.0f}MB")
    print(json.dumps({"process_maxrss_mb": out["process_maxrss_mb"], "violations": out["violations"]},
                     ensure_ascii=False))
    Path(sys.argv[1] if len(sys.argv) > 1 else "perf_similar.json").write_text(
        json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")


if __name__ == "__main__":
    main()

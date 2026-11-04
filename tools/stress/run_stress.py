"""反復試験の実行。区分ごとに試行を連続で行い、結果を JSONL に記録する。

使い方: python tools/stress/run_stress.py --category storage --count 60 --base-seed 1 --out stress_logs/run.jsonl
seed は base_seed・区分・試行番号から決まるので、記録された seed で1回分を再現できる（--only-seed）。
"""

from __future__ import annotations

import argparse
import os
import shutil
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
if sys.platform.startswith("linux"):
    os.environ.setdefault("QT_AUDIO_BACKEND", "alsa")
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from common import run_trial, write_jsonl  # noqa: E402

CATEGORIES = ["storage", "scan", "groups", "trash", "gui"]


def get_case(name):
    if name == "gui":
        import gui_cases
        return gui_cases.case_gui
    import cases
    return {"storage": cases.case_storage, "scan": cases.case_scan, "groups": cases.case_groups,
            "trash": cases.case_trash}[name]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--category", required=True, choices=CATEGORIES)
    ap.add_argument("--count", type=int, default=60)
    ap.add_argument("--start", type=int, default=0)
    ap.add_argument("--base-seed", type=int, default=1)
    ap.add_argument("--out", required=True)
    ap.add_argument("--only-seed", type=int)
    a = ap.parse_args()
    fn = get_case(a.category)
    out = Path(a.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    ci = CATEGORIES.index(a.category)
    seeds = [a.only_seed] if a.only_seed is not None else \
        [a.base_seed * 100000 + ci * 1000 + i for i in range(a.start, a.start + a.count)]
    fails = 0
    for n, seed in enumerate(seeds):
        work = Path(tempfile.mkdtemp(prefix=f"recsort_stress_{a.category}_"))
        t0 = time.perf_counter()
        rec = run_trial(fn, a.start + n, a.category, seed, work)
        rec["seconds"] = round(time.perf_counter() - t0, 2)
        write_jsonl(out, rec)
        shutil.rmtree(work, ignore_errors=True)
        fails += rec["result"] != "ok"
        print(f"{a.category} #{a.start + n} seed={seed} {rec['result']} {rec.get('kind') or ''} {rec['message'][:120]}",
              flush=True)
    print(f"done {a.category}: {len(seeds) - fails} ok / {fails} fail")
    return 1 if fails else 0  # 失敗があれば非 0（最終実行スクリプトが完了扱いにしないように）


if __name__ == "__main__":
    sys.exit(main())

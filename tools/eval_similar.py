"""類似録画の判定の検証（サンプルを自動生成して、正しく見つけた割合と誤ってまとめた例を記録する）。

使い方: python tools/eval_similar.py [作業フォルダ]
学習用（判定基準の調整に使った素材）と検証用（調整に使っていない素材）で別々に評価する。
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
import time
from itertools import combinations
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from storage_organizer import media, similar  # noqa: E402

SETS = {
    "学習用": {"A": "testsrc2=s=320x180:r=15", "B": "mandelbrot=s=320x180:r=15", "C": "life=s=320x180:r=15:mold=10"},
    "検証用": {"D": "cellauto=s=320x180:r=15:rule=110", "E": "gradients=s=320x180:r=15:speed=0.05",
             "F": "sierpinski=s=320x180:r=15"},
    # 判定基準の調整（縮小サイズの変更）後に追加した、調整に一度も使っていない素材
    "再検証用": {"G": "testsrc=s=320x180:r=15,rotate=a=0.2*t",
               "H": "mandelbrot=s=320x180:r=15:start_x=-0.75:start_y=0.1:end_scale=0.1",
               "I": "sierpinski=s=320x180:r=15:type=triangle"},
}
SECONDS = 20


def ff(tools, *args):
    subprocess.run([tools.ffmpeg, "-v", "error", "-y", *args], check=True)


def make_samples(tools, root: Path) -> dict[str, dict]:
    """各素材について: 元・低解像度版・再圧縮版・編集（先頭を切った）版、と共通タイトル付きの別動画。"""
    info = {}
    for set_name, sources in SETS.items():
        for key, src in sources.items():
            orig = root / f"{key}_orig.mp4"
            ff(tools, "-f", "lavfi", "-i", src, "-t", str(SECONDS), "-pix_fmt", "yuv420p", "-c:v", "libx264",
               "-crf", "20", str(orig))
            ff(tools, "-i", str(orig), "-vf", "scale=160:90", "-c:v", "libx264", "-crf", "28", str(root / f"{key}_lowres.mp4"))
            ff(tools, "-i", str(orig), "-c:v", "libx264", "-crf", "40", str(root / f"{key}_recompress.mp4"))
            ff(tools, "-ss", "5", "-i", str(orig), "-c:v", "libx264", "-crf", "22", str(root / f"{key}_edited.mp4"))
            # 共通のタイトル画面（3秒）＋本編＋黒画面（3秒）。本編は素材ごとに違う
            title = root / "_title.mp4"
            if not title.exists():
                ff(tools, "-f", "lavfi", "-i", "smptebars=s=320x180:r=15", "-t", "3", "-pix_fmt", "yuv420p", str(title))
                ff(tools, "-f", "lavfi", "-i", "color=c=black:s=320x180:r=15", "-t", "3", "-pix_fmt", "yuv420p",
                   str(root / "_black.mp4"))
            body = root / f"_{key}_body.mp4"
            ff(tools, "-f", "lavfi", "-i", src, "-t", str(SECONDS - 6), "-pix_fmt", "yuv420p", str(body))
            lst = root / f"_{key}_list.txt"
            lst.write_text(f"file '{title}'\nfile '{body}'\nfile '{root / '_black.mp4'}'\n")
            ff(tools, "-f", "concat", "-safe", "0", "-i", str(lst), "-c:v", "libx264", "-crf", "22",
               str(root / f"{key}_titled.mp4"))
            for variant in ("orig", "lowres", "recompress", "edited", "titled"):
                info[f"{key}_{variant}"] = {"set": set_name, "source": key, "variant": variant,
                                            "path": str(root / f"{key}_{variant}.mp4")}
    return info


def expected_same(a: dict, b: dict) -> bool | None:
    """同じ録画の解像度違い・再圧縮なら True、別の内容なら False、対象外（編集）なら None。"""
    if "edited" in (a["variant"], b["variant"]):
        return None if a["source"] == b["source"] else False
    if "titled" in (a["variant"], b["variant"]):
        # 同じ素材にタイトルと黒画面を付けたものは「編集版」（対象外）。素材が違えば、同じタイトル画面を持つ別の動画
        return None if a["source"] == b["source"] else False
    return a["source"] == b["source"]


def main() -> int:
    tools = media.find_ffmpeg()
    if not tools.available:
        print("FFmpeg が必要です")
        return 1
    root = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(tempfile.mkdtemp(prefix="recsort_eval_"))
    root.mkdir(parents=True, exist_ok=True)
    info = make_samples(tools, root)
    sigs, times = {}, []
    for name, it in info.items():
        dur = media.probe(tools, it["path"])["duration"]
        t0 = time.perf_counter()
        sigs[name] = similar.signature(tools, it["path"], dur)
        times.append(time.perf_counter() - t0)
    report = {"signature_seconds_avg": round(sum(times) / len(times), 3), "per_set": {}, "errors": []}
    for set_name in SETS:
        names = [n for n in info if info[n]["set"] == set_name]
        tp = fn = fp = tn = skipped = unknown = 0
        for a, b in combinations(names, 2):
            exp = expected_same(info[a], info[b])
            if exp is None:
                skipped += 1
                continue
            r = similar.compare(sigs[a], sigs[b])
            if r["verdict"] == "unknown":
                unknown += 1
            got = r["verdict"] == "similar"
            if exp and got:
                tp += 1
            elif exp and not got:
                fn += 1
                report["errors"].append({"set": set_name, "pair": [a, b], "kind": "見逃し", "result": r["verdict"],
                                         "score": r["score"]})
            elif not exp and got:
                fp += 1
                report["errors"].append({"set": set_name, "pair": [a, b], "kind": "誤ってまとめた", "score": r["score"]})
            else:
                tn += 1
        report["per_set"][set_name] = {"同じ録画の組": tp + fn, "見つけた": tp, "見逃し": fn,
                                       "別の動画の組": fp + tn, "誤ってまとめた": fp, "判定できない": unknown,
                                       "対象外（編集・同じタイトル同士）": skipped,
                                       "検出率": round(tp / (tp + fn), 3) if tp + fn else None}
    # グループ化で連鎖的に混ざらないか（全サンプルをまとめて分類）
    groups, _ = similar.group({i: sigs[n] for i, n in enumerate(info)})
    names = list(info)
    mixed = [[names[i] for i in g] for g in groups if len({info[names[i]]["source"] for i in g}) > 1]
    report["groups"] = [[names[i] for i in g] for g in groups]
    report["mixed_groups"] = mixed
    print(json.dumps(report, ensure_ascii=False, indent=1))
    if len(sys.argv) <= 1:
        shutil.rmtree(root, ignore_errors=True)
    return 0


if __name__ == "__main__" and "--extra" not in sys.argv:
    sys.exit(main())


def extra_cases(root: Path) -> dict:
    """追加の評価: 一部の場面だけ違う・音声だけ違う・共通画面が長い別動画。"""
    tools = media.find_ffmpeg()
    root.mkdir(parents=True, exist_ok=True)
    src = "testsrc2=s=320x180:r=15"

    def make(name, *args):
        ff(tools, *args, str(root / name))
        return str(root / name)

    a = make("a.mp4", "-f", "lavfi", "-i", src, "-t", "24", "-pix_fmt", "yuv420p", "-c:v", "libx264")
    # 一部の場面だけ違う: 8〜16秒を別の映像に差し替え（長さは同じ）
    make("_mid.mp4", "-f", "lavfi", "-i", "mandelbrot=s=320x180:r=15", "-t", "8", "-pix_fmt", "yuv420p", "-c:v", "libx264")
    make("_p1.mp4", "-i", a, "-t", "8", "-c:v", "libx264")
    make("_p3.mp4", "-ss", "16", "-i", a, "-c:v", "libx264")
    (root / "_l.txt").write_text(f"file '{root/'_p1.mp4'}'\nfile '{root/'_mid.mp4'}'\nfile '{root/'_p3.mp4'}'\n")
    part = make("partial.mp4", "-f", "concat", "-safe", "0", "-i", str(root / "_l.txt"), "-c:v", "libx264")
    # 音声だけ違う
    au1 = make("audio1.mp4", "-f", "lavfi", "-i", src, "-f", "lavfi", "-i", "sine=frequency=440", "-t", "12",
               "-pix_fmt", "yuv420p", "-c:v", "libx264", "-g", "15", "-c:a", "aac")
    au2 = make("audio2.mp4", "-f", "lavfi", "-i", src, "-f", "lavfi", "-i", "sine=frequency=880", "-t", "12",
               "-pix_fmt", "yuv420p", "-c:v", "libx264", "-g", "15", "-c:a", "aac")
    # 共通画面が長い別動画: 共通タイトル 8 秒 + 本編 4 秒（別） + 共通エンディング 8 秒
    make("_t.mp4", "-f", "lavfi", "-i", "smptehdbars=s=320x180:r=15", "-t", "8", "-pix_fmt", "yuv420p", "-c:v", "libx264")
    make("_e.mp4", "-f", "lavfi", "-i", "testsrc=s=320x180:r=15", "-t", "8", "-pix_fmt", "yuv420p", "-c:v", "libx264")
    shared = []
    for key, body in (("x", "life=s=320x180:r=15:mold=10"), ("y", "cellauto=s=320x180:r=15:rule=110")):
        make(f"_b{key}.mp4", "-f", "lavfi", "-i", body, "-t", "4", "-pix_fmt", "yuv420p", "-c:v", "libx264")
        (root / f"_s{key}.txt").write_text(f"file '{root/'_t.mp4'}'\nfile '{root/f'_b{key}.mp4'}'\nfile '{root/'_e.mp4'}'\n")
        shared.append(make(f"shared_{key}.mp4", "-f", "concat", "-safe", "0", "-i", str(root / f"_s{key}.txt"),
                           "-c:v", "libx264"))
    out = {}
    for label, (p, q) in {"一部の場面だけ違う（同じ長さ）": (a, part), "音声だけ違う": (au1, au2),
                          "共通画面が長い別動画（共通 16 秒／20 秒）": tuple(shared)}.items():
        sp = similar.signature(tools, p, media.probe(tools, p)["duration"])
        sq = similar.signature(tools, q, media.probe(tools, q)["duration"])
        r = similar.compare(sp, sq)
        per = r["per_frame"]
        out[label] = {"判定": r["verdict"], "一致": r["matched"], "比較できた場面": r["informative"],
                      "一致しない場面": [i for i, c in enumerate(per) if c is not None and c < similar.MATCH_CORR]}
    return out


if __name__ == "__main__" and "--extra" in sys.argv:
    work = Path(tempfile.mkdtemp(prefix="recsort_eval_extra_"))
    print(json.dumps(extra_cases(work), ensure_ascii=False, indent=1))
    shutil.rmtree(work, ignore_errors=True)

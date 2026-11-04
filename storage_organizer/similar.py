"""類似録画の判定（同じ録画の解像度違い・再圧縮などを、比較が必要な候補としてまとめる）。

方式:
- 長さがほぼ同じ動画どうしだけを比べる（長さが違う・編集された動画は対象外）
- 同じ相対時点（長さの (i+0.5)/N）の N 場面を 32×18 の白黒画像にして比べる
- 黒画面・単色など情報のない場面は判定に使わない（共通のタイトル画面や黒画面での誤検出を防ぐ）
- 情報のある場面の大半が一致したときだけ「類似」とする
- グループは完全連結（全員どうしが類似のときだけ同じグループ）で、似ていない動画の連鎖的な混入を防ぐ
類似は同じ内容であることを保証しない。必ず利用者の比較が必要な候補として扱う。
"""

from __future__ import annotations

import base64

import numpy as np

from . import media

N_POINTS = 12
W, H = 32, 18  # 16×9 では細かい模様が平坦になり判定できなかった（検証で確認）
FLAT_STD = 6.0          # 0〜255 の輝度の標準偏差。これ未満の場面は情報がないとみなす
MATCH_CORR = 0.85       # 場面ごとの相関係数がこれ以上なら一致
MIN_INFORMATIVE = 6     # 判定に使える場面がこれ未満なら「判定できない」
MIN_MATCH_RATIO = 0.75  # 情報のある場面のうち、一致した割合
VERSION = 1


def duration_close(a: float, b: float) -> bool:
    """長さがほぼ同じか（差が 1.5 秒以内、または長い方の 2% 以内）。"""
    if not a or not b:
        return False
    return abs(a - b) <= max(1.5, 0.02 * max(a, b))


def signature(tools: media.FFmpegTools, path: str, duration: float, cancel=None) -> dict:
    """代表場面の白黒縮小画像（N 枚）を取り出す。全編は読み込まない。"""
    frames = []
    for t in media._sample_times(duration, N_POINTS):
        if cancel is not None and cancel.is_set():
            raise media.Cancelled()
        cp = media._run([tools.ffmpeg, "-v", "error", "-ss", f"{t:.3f}", "-i", path, "-frames:v", "1",
                         "-vf", f"scale={W}:{H}", "-pix_fmt", "gray", "-f", "rawvideo", "-"], cancel, timeout=60)
        frames.append(cp.stdout if cp.returncode == 0 and len(cp.stdout) == W * H else None)
    if sum(f is not None for f in frames) < N_POINTS // 2:
        raise RuntimeError("代表場面を取り出せませんでした")
    return {"v": VERSION, "duration": duration,
            "frames": [base64.b64encode(f).decode() if f is not None else None for f in frames]}


def _frames(sig: dict) -> list[np.ndarray | None]:
    return [np.frombuffer(base64.b64decode(f), np.uint8).astype(np.float32) if f else None
            for f in sig.get("frames", [])]


def compare(a: dict, b: dict) -> dict:
    """2本の動画の代表場面を比べる。verdict: similar / different / unknown（判定できない）。"""
    if not duration_close(a.get("duration", 0), b.get("duration", 0)):
        return {"verdict": "different", "score": 0.0, "informative": 0, "matched": 0, "per_frame": []}
    fa, fb = _frames(a), _frames(b)
    informative = matched = 0
    per_frame = []
    for x, y in zip(fa, fb):
        if x is None or y is None or x.std() < FLAT_STD or y.std() < FLAT_STD:
            per_frame.append(None)  # 情報のない場面（黒画面・単色・取り出し失敗）は使わない
            continue
        corr = float(np.corrcoef(x, y)[0, 1])
        informative += 1
        ok = corr >= MATCH_CORR
        matched += ok
        per_frame.append(round(corr, 3))
    if informative < MIN_INFORMATIVE:
        return {"verdict": "unknown", "score": 0.0, "informative": informative, "matched": matched,
                "per_frame": per_frame}
    score = matched / informative
    return {"verdict": "similar" if score >= MIN_MATCH_RATIO else "different", "score": round(score, 3),
            "informative": informative, "matched": matched, "per_frame": per_frame}


def group(items: dict[int, dict]) -> tuple[list[list[int]], dict[tuple[int, int], dict]]:
    """{ID: シグネチャ} を類似グループに分ける（完全連結）。戻り値: (グループ, 比べた組の結果)。"""
    ids = sorted(items, key=lambda i: items[i].get("duration", 0))
    results: dict[tuple[int, int], dict] = {}
    for x in range(len(ids)):
        for y in range(x + 1, len(ids)):
            a, b = ids[x], ids[y]
            if items[b].get("duration", 0) - items[a].get("duration", 0) > max(1.5, 0.02 * items[b].get("duration", 0)):
                break  # 長さ順に並べているので、これより先は長さが合わない
            results[(a, b)] = compare(items[a], items[b])
    similar = {k for k, r in results.items() if r["verdict"] == "similar"}

    def sim(a, b):
        return (a, b) in similar or (b, a) in similar

    clusters: list[list[int]] = []
    for (a, b) in sorted(similar, key=lambda k: -results[k]["score"]):
        ca = next((c for c in clusters if a in c), None)
        cb = next((c for c in clusters if b in c), None)
        if ca is not None and ca is cb:
            continue
        merged = (ca or [a]) + (cb or [b])
        if all(sim(p, q) for i, p in enumerate(merged) for q in merged[i + 1:]):  # 全員どうしが類似のときだけ
            for c in (ca, cb):
                if c is not None:
                    clusters.remove(c)
            clusters.append(merged)
    return [sorted(c) for c in clusters if len(c) > 1], results


def frame_previews(sig: dict) -> list[np.ndarray | None]:
    """比較画面用（縮小画像の配列）。"""
    return [f.reshape(H, W) if f is not None else None for f in _frames(sig)]

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


COARSE_COLS, COARSE_ROWS = 8, 6  # 軽い特徴: 1場面を 8×6 の区画にして、明暗の並びで比べる
PRE_CORR = 0.6                   # 軽い特徴での場面の一致（本判定 MATCH_CORR より緩い）
PRE_RATIO = 0.6                  # 軽い特徴で一致した場面の割合（本判定 MIN_MATCH_RATIO より緩い）
COARSE_MIN_STD = 3.0             # 区画の平均のばらつきがこれ未満の場面は、軽い特徴に情報がないとみなす
COMMON_MIN_COUNT = 5             # 同じ位置でこの本数以上（かつ全体の 2% 以上）に現れる場面は「共通画面」とみなす
MIN_OWN_FRAMES = 3               # 共通画面以外の場面がこれ未満の組は、全場面で絞り込む（見逃しを防ぐ）


class Cancelled(Exception):
    """類似比較を途中で止めた（途中の結果は使わない）。"""


def coarse(sig: dict) -> tuple[np.ndarray, np.ndarray]:
    """(N×48 の正規化した区画の明るさ, N 個の「情報がある場面か」)。取り出せなかった場面は情報なし。"""
    d = COARSE_COLS * COARSE_ROWS
    feats = np.zeros((N_POINTS, d), np.float32)
    ok = np.zeros(N_POINTS, bool)
    for i, f in enumerate(_frames(sig)[:N_POINTS]):
        if f is None or f.size != W * H:
            continue
        img = f.reshape(H, W)
        if img.std() < FLAT_STD:
            continue  # 黒画面・単色は使わない
        v = img.reshape(COARSE_ROWS, H // COARSE_ROWS, COARSE_COLS, W // COARSE_COLS).mean(axis=(1, 3)).ravel()
        sd = v.std()
        if sd < COARSE_MIN_STD:
            continue  # 細かい模様だけの場面は区画の平均に情報が残らない → 絞り込みには使わない（詳しい比較に任せる）
        feats[i] = (v - v.mean()) / sd / np.sqrt(d)  # 内積がそのまま相関係数になるように
        ok[i] = True
    return feats, ok


def group(items: dict[int, dict], cancel=None, progress=None) -> tuple[list[list[int]], dict]:
    """{ID: シグネチャ} を類似グループに分ける（完全連結）。

    1. 長さがほぼ同じ組だけを対象にする 2. 軽い特徴（区画の平均の明るさ）で明らかに違う組を除く
    3. 残った組だけ代表場面を詳しく比べる 4. 全員どうしが類似のときだけ同じグループにする
    類似と判定した組だけを保持する（全組の結果は持たない）。各段階でキャンセルできる。
    戻り値: (グループ, 統計 {"window_pairs", "detailed_pairs", "similar_pairs"})
    """
    def check():
        if cancel is not None and cancel.is_set():
            raise Cancelled()

    ids = sorted(items, key=lambda i: items[i].get("duration", 0))
    n = len(ids)
    durs = np.array([items[i].get("duration", 0) or 0 for i in ids], np.float64)
    feats = np.zeros((n, N_POINTS, COARSE_COLS * COARSE_ROWS), np.float32)
    oks = np.zeros((n, N_POINTS), bool)
    for k, i in enumerate(ids):  # 候補抽出の準備（軽い特徴）
        if k % 200 == 0:
            check()
            if progress:
                progress("prepare", k, n)
        feats[k], oks[k] = coarse(items[i])
    # 共通画面（タイトル・メニューなど、多くの動画で同じ場面）を見つける。絞り込みでは、共通画面以外の
    # 場面の一致を見る（共通画面だけが一致する別の回の動画を、詳しく比べずに除外するため）
    qkeys = np.round(feats * np.sqrt(feats.shape[2]) * 2).astype(np.int8)
    common = np.zeros((n, N_POINTS), bool)
    limit = max(COMMON_MIN_COUNT, int(0.02 * n))
    for k in range(N_POINTS):
        check()
        keys = [qkeys[x, k].tobytes() if oks[x, k] else None for x in range(n)]
        counts: dict[bytes, int] = {}
        for key in keys:
            if key is not None:
                counts[key] = counts.get(key, 0) + 1
        for x, key in enumerate(keys):
            if key is not None and counts[key] >= limit:
                common[x, k] = True
    stats = {"window_pairs": 0, "detailed_pairs": 0, "similar_pairs": 0, "common_frames": int(common.sum())}
    similar: set[tuple[int, int]] = set()
    scores: dict[tuple[int, int], float] = {}
    for x in range(n):
        if x % 20 == 0:
            check()
            if progress:
                progress("compare", x, n)
        tol = np.maximum(1.5, 0.02 * np.maximum(durs[x], durs[x + 1:]))
        end = x + 1 + int(np.argmax(durs[x + 1:] - durs[x] > tol)) if n > x + 1 and (durs[x + 1:] - durs[x] > tol).any() \
            else n
        if end <= x + 1:
            continue
        stats["window_pairs"] += end - x - 1
        both = oks[x][None, :] & oks[x + 1:end]                          # (k, N) 両方に情報がある場面
        corr = np.einsum("kd,jkd->jk", feats[x], feats[x + 1:end])       # (j, N) 場面ごとの相関（軽い特徴）
        informative = both.sum(axis=1)
        hits = ((corr >= PRE_CORR) & both).sum(axis=1)
        # 軽い特徴で判断できる場面が少ない組は、除外せず詳しい比較に回す（高速化による見逃しを防ぐ）
        passed = (informative < MIN_INFORMATIVE) | (hits >= PRE_RATIO * np.maximum(informative, 1))
        # 共通画面以外の場面が十分ある組は、その場面の一致で判断する（同じ録画ならそこも一致する）
        own = both & ~(common[x][None, :] & common[x + 1:end])
        own_n = own.sum(axis=1)
        own_hits = ((corr >= PRE_CORR) & own).sum(axis=1)
        use_own = own_n >= MIN_OWN_FRAMES
        passed = np.where(use_own, passed & (own_hits >= PRE_RATIO * np.maximum(own_n, 1)), passed)
        for off in np.nonzero(passed)[0]:
            y = x + 1 + int(off)
            stats["detailed_pairs"] += 1
            r = compare(items[ids[x]], items[ids[y]])
            if r["verdict"] == "similar":
                similar.add((ids[x], ids[y]))
                scores[(ids[x], ids[y])] = r["score"]
    stats["similar_pairs"] = len(similar)
    check()
    if progress:
        progress("group", 0, 1)

    def sim(a, b):
        return (a, b) in similar or (b, a) in similar

    clusters: list[list[int]] = []
    where: dict[int, list[int]] = {}
    for step, (a, b) in enumerate(sorted(similar, key=lambda k: -scores[k])):
        if step % 50 == 0:
            check()  # グループ化の途中でもキャンセルを受け付ける
        ca, cb = where.get(a), where.get(b)
        if ca is not None and ca is cb:
            continue
        merged = (ca or [a]) + (cb or [b])
        if all(sim(p, q) for i, p in enumerate(merged) for q in merged[i + 1:]):  # 全員どうしが類似のときだけ
            for c in (ca, cb):
                if c is not None:
                    clusters.remove(c)
            clusters.append(merged)
            for m in merged:
                where[m] = merged
    check()  # 終了直前にも確認する（キャンセル後の結果を完了扱いにしない）
    return [sorted(c) for c in clusters if len(c) > 1], stats


def frame_previews(sig: dict) -> list[np.ndarray | None]:
    """比較画面用（縮小画像の配列）。"""
    return [f.reshape(H, W) if f is not None else None for f in _frames(sig)]

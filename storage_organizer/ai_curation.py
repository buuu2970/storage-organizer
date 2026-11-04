"""AI の予測（厳選とジャンル）。あなたが教えた判断に「動画の特徴」が近いかどうかで予測する。

- 方式: 近いものを数える方法（k 近傍法）。外部へは何も送らない。学習済みファイルは作らず、
  「覚えさせた時点の回答の写し」（profiles.ProfileData.learn）を判断基準にする
- 予測の根拠（近い見本と、どの特徴が近いか）を必ず返す。根拠が足りないときは予測しない（None）
- AI の予測と、利用者が決めた判断は別に扱う。予測で利用者の判断を変えることはない
- 動画の特徴は、ファイルの情報（容量・長さ・画面の大きさ等）と、FFmpeg で確認した3場面の明るさ・動きだけ。
  内容の意味（面白さ・大切さ）は判断しない
"""

from __future__ import annotations

import math
import time
from dataclasses import dataclass

from . import candidates
from .profiles import ALL_SCOPE, ProfileData, genre_scope
from .store import loads

MIN_PER_CLASS = 2      # 判断ごとに最低この数だけ教えてもらってから予測する
AGREEMENT = 0.7        # 近い見本の重みのうち、この割合以上が同じ判断なら予測を示す
MIN_SHARED = 3         # 比べられる特徴がこの数より少ないときは予測しない
K = 5

FEATURE_LABEL = {
    "size": "容量", "duration": "長さ", "per_minute": "1分あたりの容量", "height": "画面の大きさ", "fps": "なめらかさ",
    "age": "古さ", "audio": "音声の有無", "brightness": "明るさ", "contrast": "明暗の差", "saturation": "色の濃さ",
    "motion": "動きの多さ", "black_ratio": "黒い画面の割合", "static_ratio": "止まった場面の割合",
}
LABEL_TEXT = {"keep": "残す", "review": "整理候補"}


def features(row, now: float | None = None) -> dict[str, float]:
    """動画の特徴（数値）。分からない項目は入れない（推測で埋めない）。"""
    now = now or time.time()
    f: dict[str, float] = {"size": math.log1p(row["size"] / 1024 ** 2)}
    if row.get("mtime"):
        f["age"] = math.log1p(max(now - row["mtime"], 0) / 86400)
    meta = loads(row.get("meta"), {}) or {}
    d = meta.get("duration")
    if d and d > 0:
        f["duration"] = math.log1p(d / 60)
        f["per_minute"] = math.log1p(row["size"] / 1024 ** 2 / (d / 60))
    if meta.get("height"):
        f["height"] = float(meta["height"]) / 1080
    if meta.get("fps"):
        f["fps"] = float(meta["fps"]) / 60
    if "has_audio" in meta:
        f["audio"] = 1.0 if meta["has_audio"] else 0.0
    feat = loads(row.get("features"), {}) or {}
    for k in ("brightness", "contrast", "saturation", "motion", "black_ratio", "static_ratio"):
        if isinstance(feat.get(k), (int, float)):
            f[k] = float(feat[k])
    return f


@dataclass
class Prediction:
    label: str | None          # 予測（keep / review / ジャンル名）。予測しないときは None
    text: str                  # 画面に出す説明（予測と根拠、または予測しない理由）
    confidence: float = 0.0    # 近い見本の中で同じ判断だった割合（0〜1）


def _stats(vectors: list[dict]) -> dict[str, tuple[float, float]]:
    keys = {k for v in vectors for k in v}
    out = {}
    for k in keys:
        vals = [v[k] for v in vectors if k in v]
        if len(vals) < 2:
            continue
        mean = sum(vals) / len(vals)
        sd = math.sqrt(sum((x - mean) ** 2 for x in vals) / len(vals))
        out[k] = (mean, sd if sd > 1e-9 else 1.0)
    return out


def _distance(a: dict, b: dict, stats) -> tuple[float, list[tuple[float, str]]] | None:
    shared = [k for k in a if k in b and k in stats]
    if len(shared) < MIN_SHARED:
        return None
    diffs = [(abs(a[k] - b[k]) / stats[k][1], k) for k in shared]
    return math.sqrt(sum(d * d for d, _ in diffs) / len(diffs)), sorted(diffs)


def nearest(target: dict, examples: list[tuple[str, str, dict]]) -> list[tuple[float, str, str, list]]:
    """(距離, 見本の名前, 判断, 近い特徴) の近い順。examples: (名前, 判断, 特徴)。"""
    stats = _stats([e[2] for e in examples] + [target])
    out = []
    for name, label, vec in examples:
        r = _distance(target, vec, stats)
        if r is not None:
            out.append((r[0], name, label, r[1]))
    out.sort(key=lambda x: x[0])
    return out


def vote(target: dict, examples: list[tuple[str, str, dict]], what: str) -> Prediction:
    near = nearest(target, examples)[:K]
    if len(near) < 2:
        return Prediction(None, f"まだ判断できません（比べられる{what}の見本が足りません）")
    weights: dict[str, float] = {}
    for d, _, label, _ in near:
        weights[label] = weights.get(label, 0.0) + 1.0 / (d + 0.1)
    best, w = max(weights.items(), key=lambda kv: kv[1])
    share = w / sum(weights.values())
    if share < AGREEMENT:
        return Prediction(None, "まだ判断できません（似ている見本の判断が分かれています）", share)
    d, name, _, diffs = next(n for n in near if n[2] == best)
    close = "・".join(FEATURE_LABEL.get(k, k) for _, k in diffs[:3])
    return Prediction(best, f"「{name}」に{close}が近いためです", share)


# --- 厳選 ---------------------------------------------------------------------
def curation_examples(pdata: ProfileData, scope: str, rows_by_path: dict,
                      exclude: str | None = None) -> list[tuple[str, str, dict]] | None:
    """覚えさせた時点の回答から見本を作る（exclude の動画自身は見本にしない）。判断基準がなければ None。"""
    model = pdata.model(scope)
    if model is None:
        return None
    now = time.time()
    return [(rows_by_path[p]["name"], label, features(rows_by_path[p], now))
            for p, label in model["labels"].items() if p in rows_by_path and p != exclude]


def predict_curation(pdata: ProfileData, scope: str, row, rows_by_path: dict) -> Prediction:
    """1つの範囲（全動画、または1つのジャンル）の判断基準で予測する。"""
    examples = curation_examples(pdata, scope, rows_by_path, exclude=row["path"])
    if examples is None:
        return Prediction(None, "まだ判断できません（AI にまだ判断を覚えさせていません）")
    counts = {lab: sum(1 for e in examples if e[1] == lab) for lab in ("keep", "review")}
    if min(counts.values()) < MIN_PER_CLASS:
        return Prediction(None, f"まだ判断できません（「残す」「整理候補」をそれぞれ {MIN_PER_CLASS} 本以上教えてから"
                                "覚えさせてください）")
    p = vote(features(row), examples, "動画")
    if p.label:
        word = "以前に整理候補にした動画" if p.label == "review" else "以前に残すと決めた動画"
        return Prediction(p.label, f"あなたが{word}{p.text}", p.confidence)
    return p


def predict_for_mode(pdata: ProfileData, row, rows_by_path: dict, mode: str, genres: list[str]) -> Prediction:
    """厳選モードに合わせて予測する。ジャンル別で判断が分かれたら予測しない（保留をすすめる）。"""
    if mode == "all":
        return predict_curation(pdata, ALL_SCOPE, row, rows_by_path)
    own = effective_genres(pdata, row, rows_by_path)[0]
    targets = [g for g in genres if g in own] or genres[:1]
    preds = {g: predict_curation(pdata, genre_scope(g), row, rows_by_path) for g in targets}
    labels = {p.label for p in preds.values()}
    if len(preds) > 1 and len(labels - {None}) > 1:
        return Prediction(None, "まだ判断できません（ジャンルごとの判断が分かれています。保留にしてあとで確認できます）")
    decided = [(g, p) for g, p in preds.items() if p.label]
    if decided and len(labels) == 1:
        g, p = decided[0]
        return Prediction(p.label, f"【{g}】{p.text}", min(x.confidence for _, x in decided))
    return next(iter(preds.values())) if preds else Prediction(None, "まだ判断できません")


# --- ジャンル -------------------------------------------------------------------
def genre_examples(pdata: ProfileData, sample_rows: dict) -> list[tuple[str, str, dict]] | None:
    """初期学習の回答（覚えさせた時点）から、ジャンルの見本を作る。複数のジャンルは見本を分けて持つ。"""
    model = pdata.genre_model()
    if not model:
        return None
    out = []
    for name, ans in model.get("answers", {}).items():
        if ans.get("skipped") or name not in sample_rows:
            continue
        for g in ans.get("genres", []):
            out.append((name, g, features(sample_rows[name])))
    return out


def predict_genre(pdata: ProfileData, row, sample_rows: dict) -> Prediction:
    examples = genre_examples(pdata, sample_rows)
    if examples is None:
        return Prediction(None, "まだ判断できません（初期学習でジャンルの分け方を覚えさせると予測できます）")
    if len({e[1] for e in examples}) < 2:
        return Prediction(None, "まだ判断できません（2つ以上のジャンルを教えると予測できます）")
    p = vote(features(row), examples, "ジャンル")
    if p.label:
        return Prediction(p.label, f"見本の動画{p.text}", p.confidence)
    return p


def effective_genres(pdata: ProfileData, row, rows_by_path: dict, sample_rows: dict | None = None) -> tuple[list[str], str]:
    """(ジャンル, 出どころ)。利用者が決めたジャンルを優先する。出どころ: user / ai / none。"""
    mine = pdata.video_genres(row["path"])
    if mine is not None:
        return mine, "user"
    if sample_rows is not None:
        p = predict_genre(pdata, row, sample_rows)
        if p.label:
            return [p.label], "ai"
    return [], "none"


# --- 圧縮候補 -------------------------------------------------------------------
COMPRESS_MIN_BYTES = 200 * 1024 * 1024
COMPRESS_MB_PER_MIN = 40  # 1分あたりこれより大きい録画は、圧縮で小さくできる可能性がある


def compress_reason(row) -> str | None:
    """圧縮で小さくできる可能性がある理由（客観的な数値だけ）。分からなければ None。"""
    if row["size"] < COMPRESS_MIN_BYTES or row.get("cloud_only") or row.get("protected"):
        return None
    pm = candidates.per_minute(row)
    if pm is None or pm / 1024 ** 2 < COMPRESS_MB_PER_MIN:
        return None
    return f"1分あたり {pm / 1024 ** 2:,.0f} MB と大きいため、圧縮で小さくできる可能性があります（画質は下がります）"

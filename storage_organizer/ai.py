"""軽量 AI（特徴量の類似度による分類）・整理推薦・バックグラウンド処理。

方式（試作）:
- 映像・音声・メタデータの簡易な数値特徴をベクトルにし、全動画の平均・標準偏差で標準化
- ユーザーが分類した動画（教師データ）との距離を比べ、近いジャンルを候補にする
- 近い教師データがない／根拠が弱い場合は「未分類」にする
- 「その他・不明」は学習クラスにしない
音楽・歌唱・会話などの意味の判定はしていない（数値の傾向が似ているかだけを見る）。
"""

from __future__ import annotations

import hashlib
import json
import os
import threading
import time
import warnings
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from . import media
from .db import (ANSWERED, CLASSIFY_SCOPE, CURATE_ALL_SCOPE, TRAINABLE_CHOICES, Database,
                 curate_genre_scope, loads)
from .samples import SAMPLE_PREFIX, SampleStore, display_name

FEATURE_NAMES = [
    "長さ(log)", "縦横比", "解像度(log)", "ビットレート(log)", "音声あり",
    "明るさ", "明るさの変化", "コントラスト", "彩度", "黒画面率", "静止率", "動き", "輪郭",
    "白色率", "色相1", "色相2", "色相3", "色相4", "色相5", "色相6",
    "音量", "音量の変化", "無音率", "ゼロ交差率", "スペクトル重心", "スペクトル平坦度", "音量の抑揚",
]
# 誤った提案を減らすため厳しめにする（サンプル動画で、同じ種類どうしは類似度 0.9 以上、
# 別の種類は 0.7 未満だった）
MIN_SIMILARITY = 0.75  # これ未満は「根拠不足」で未分類
MARGIN = 0.8  # 正例までの距離 < 負例までの距離 × MARGIN のときだけ採用
ANALYSIS_VERSION = 1


def feature_vector(meta: dict | None, features: dict | None) -> np.ndarray | None:
    """解析結果を固定長ベクトルにする。欠けた部分は NaN（後で平均で埋める）。"""
    if not meta or not features or (not features.get("video") and not features.get("audio")):
        return None
    v = np.full(len(FEATURE_NAMES), np.nan, dtype=np.float64)
    w, h = meta.get("width", 0), meta.get("height", 0)
    v[0] = np.log1p(meta.get("duration", 0))
    v[1] = (w / h) if h else np.nan
    v[2] = np.log1p(w * h) if w and h else np.nan
    v[3] = np.log1p(meta.get("bitrate", 0))
    v[4] = float(meta.get("has_audio", False))
    vf = features.get("video")
    if vf:
        v[5:14] = [vf["brightness"], vf["brightness_var"], vf["contrast"], vf["saturation"],
                   vf["black_ratio"], vf["static_ratio"], vf["motion"], vf["edges"], vf["white_ratio"]]
        v[14:20] = vf["hue_hist"]
    af = features.get("audio")
    if af:
        v[20:27] = [af["loudness_db"], af["loudness_var"], af["silence_ratio"], af["zcr"],
                    af["spectral_centroid"] / 4000, af["spectral_flatness"], af["modulation"]]
    return v


@dataclass
class Normalizer:
    mean: np.ndarray
    std: np.ndarray

    @classmethod
    def fit(cls, vectors: list[np.ndarray]) -> "Normalizer":
        m = np.vstack(vectors)
        with warnings.catch_warnings():  # 全部欠けている列は平均0・標準偏差1として扱う
            warnings.simplefilter("ignore", RuntimeWarning)
            mean = np.nanmean(m, axis=0)
            std = np.nanstd(m, axis=0)
        mean = np.where(np.isnan(mean), 0.0, mean)
        std = np.where(np.isnan(std) | (std < 1e-6), 1.0, std)
        return cls(mean, std)

    def transform(self, v: np.ndarray) -> np.ndarray:
        z = (v - self.mean) / self.std
        return np.where(np.isnan(z), 0.0, z)  # 欠けた特徴は平均扱い


def _distance(a: np.ndarray, b: np.ndarray) -> float:
    return float(np.sqrt(((a - b) ** 2).mean()))


def similarity_from_distance(d: float, tau: float) -> float:
    return float(np.exp(-(d / tau) ** 2))


# --- モデル -----------------------------------------------------------------
@dataclass
class Model:
    version: int
    normalizer: Normalizer
    tau: float
    samples: list[dict]  # {"video_id", "name", "z": list, "genres": [ids]}
    genre_ids: list[int]  # 学習対象のジャンル

    def to_json(self) -> dict:
        return {"version": self.version, "mean": self.normalizer.mean.tolist(),
                "std": self.normalizer.std.tolist(), "tau": self.tau,
                "samples": self.samples, "genre_ids": self.genre_ids,
                "method": "類似度比較（最近傍）", "analysis_version": ANALYSIS_VERSION}

    @classmethod
    def from_json(cls, data: dict) -> "Model":
        return cls(data["version"], Normalizer(np.array(data["mean"]), np.array(data["std"])),
                   data["tau"], data["samples"], data["genre_ids"])

    def classify(self, z: np.ndarray, exclude_video: int | None = None,
                 names: dict[int, str] | None = None) -> dict:
        """1本を分類する。genres: 採用したジャンル、candidates: 参考の候補。"""
        samples = [s for s in self.samples if s["video_id"] != exclude_video]
        names = names or {}
        if not samples:
            return {"state": "未分類", "genres": [], "candidates": [],
                    "reason": "教師データがありません"}
        dists = [(_distance(z, np.array(s["z"])), s) for s in samples]
        nearest_d, nearest_s = min(dists, key=lambda x: x[0])
        genres, candidates = [], []
        for g in self.genre_ids:
            pos = [(d, s) for d, s in dists if g in s["genres"]]
            if not pos:
                continue
            neg = [d for d, s in dists if g not in s["genres"]]
            d_pos, s_pos = min(pos, key=lambda x: x[0])
            d_neg = min(neg) if neg else float("inf")
            sim = similarity_from_distance(d_pos, self.tau)
            entry = {"genre_id": g, "score": round(sim, 3), "nearest": s_pos["name"]}
            candidates.append(entry)
            if sim >= MIN_SIMILARITY and d_pos < d_neg * MARGIN:
                genres.append(entry)
        candidates.sort(key=lambda e: -e["score"])
        genres.sort(key=lambda e: -e["score"])
        if genres:
            parts = [f"「{names.get(e['genre_id'], e['genre_id'])}」に分類した {e['nearest']} に近い"
                     f"（類似度 {e['score']:.2f}）" for e in genres]
            return {"state": "分類済み", "genres": genres, "candidates": candidates,
                    "reason": "、".join(parts) + "。数値特徴の類似度による推定です。"}
        best = similarity_from_distance(nearest_d, self.tau)
        if best < MIN_SIMILARITY:
            reason = f"学習済みの動画との類似度が低い（最大 {best:.2f}）"
        elif not candidates:
            reason = "近い動画のジャンルが学習対象外（その他・不明など）です"
        else:
            reason = "複数のジャンルの教師データと同じくらい近く、決め手がありません"
        return {"state": "未分類", "genres": [], "candidates": candidates, "reason": reason}


def _load_vectors(db: Database) -> dict[int, np.ndarray]:
    result = {}
    for row in db.videos():
        v = feature_vector(loads(row["meta"]), loads(row["features"]))
        if v is not None:
            result[row["id"]] = v
    return result


def _sample_training(db: Database, store) -> tuple[dict[str, np.ndarray], list[tuple], dict[str, int]]:
    """初期12本のうち学習に使える回答。

    戻り値: (解析済みサンプルの全特徴, [(サンプルID, 表示名, ジャンル)], 除外理由ごとの件数)
    - 使うのはこのプロファイルの「回答済み」だけ（スキップ・未回答は使わない）
    - 回答後に動画が入れ替わった回答は使わない（再確認が必要）
    """
    if store is None:
        return {}, [], {}
    rows = {r["sample_id"]: r for r in store.samples()}
    usable = store.usable_features()
    vectors = {}
    for sid, r in usable.items():
        v = feature_vector(loads(r["meta"]), loads(r["features"]))
        if v is not None:
            vectors[sid] = v
    labeled, skipped = [], {"stale": 0, "no_features": 0}
    for sid, ans in db.sample_answers().items():
        if ans["status"] != ANSWERED or sid not in rows:
            continue
        if ans["content_hash"] != rows[sid]["content_hash"]:
            skipped["stale"] += 1
            continue
        if sid not in vectors:
            skipped["no_features"] += 1
            continue
        labeled.append((sid, display_name(rows[sid]), ans["genres"]))
    return vectors, labeled, skipped


def build_model(db: Database, version: int, store=None) -> tuple[Model | None, str, list]:
    """教師データからモデルを作る。(モデル, 説明, 使った動画ID と "sample:サンプルID")

    store（samples.SampleStore）を渡すと、初期12本への回答も教師データにする。
    """
    vectors = _load_vectors(db)
    sample_vectors, sample_labeled, sample_skipped = _sample_training(db, store)
    if not vectors and not sample_vectors:
        return None, "解析済みの動画がありません。先に動画を解析してください。", []
    trainable = {g["id"] for g in db.genres() if not g["exclude_from_training"]}
    user_genres = db.all_user_genres()
    names = {r["id"]: r["name"] for r in db.videos()}
    samples, used, no_features = [], [], 0
    all_vectors = list(vectors.values()) + list(sample_vectors.values())
    norm = Normalizer.fit(all_vectors)
    for sid, name, genres in sample_labeled:
        key = SAMPLE_PREFIX + sid
        used.append(key)  # 「その他・不明」だけの回答も負例として使う
        samples.append({"video_id": key, "name": name,
                        "z": norm.transform(sample_vectors[sid]).round(5).tolist(),
                        "genres": sorted(set(genres) & trainable)})
    for t in db.training_rows():
        vid = t["video_id"]
        genres = sorted(user_genres.get(vid, set()) & trainable)
        if vid not in vectors:
            no_features += 1
            continue
        used.append(vid)  # 「その他・不明」だけの動画も負例として使う
        samples.append({"video_id": vid, "name": names.get(vid, str(vid)),
                        "z": norm.transform(vectors[vid]).round(5).tolist(), "genres": genres})
    genre_ids = sorted({g for s in samples for g in s["genres"]})
    if not genre_ids:
        msg = "学習できるジャンルが付いた教師データがありません（「その他・不明」は学習対象外です）。"
        if no_features or sample_skipped.get("no_features"):
            msg += (f" 解析されていない教師データが "
                    f"{no_features + sample_skipped.get('no_features', 0)} 件あります。")
        return None, msg, []
    # 距離の目安: 全動画（初期サンプルを含む）の標準化ベクトルどうしの距離の中央値
    zs = [norm.transform(v) for v in all_vectors]
    if len(zs) >= 2:
        idx = np.random.default_rng(0).choice(len(zs), size=min(len(zs), 200), replace=False)
        pts = [zs[i] for i in idx]
        ds = [_distance(pts[i], pts[j]) for i in range(len(pts)) for j in range(i + 1, len(pts))]
        tau = max(float(np.median(ds)), 0.3)
    else:
        tau = 1.0
    n_initial = len(sample_labeled)
    note = f"教師データ {len(samples)} 件" + (f"（うち初期12本の回答 {n_initial} 件）" if n_initial else "") \
        + "で作成しました。"
    if no_features or sample_skipped.get("no_features"):
        note += f"（解析されていない教師データ {no_features + sample_skipped.get('no_features', 0)} 件は除外）"
    if sample_skipped.get("stale"):
        note += f"（動画が入れ替わった初期サンプルの回答 {sample_skipped['stale']} 件は再確認まで除外）"
    if len(samples) < 20:
        note += " 教師データが少ないため、分類の精度は保証できません。"
    return Model(version, norm, tau, samples, genre_ids), note, used


def evaluate(model: Model) -> dict:
    """1件ずつ外して残りで予測する（leave-one-out）。"""
    n = len(model.samples)
    if n < 3:
        return {"n": n, "status": "評価データ不足（3件未満）"}
    tp = fp = fn = abstain = 0
    for s in model.samples:
        r = model.classify(np.array(s["z"]), exclude_video=s["video_id"])
        pred = {e["genre_id"] for e in r["genres"]}
        true = set(s["genres"]) & set(model.genre_ids)
        if not pred:
            abstain += 1
        tp += len(pred & true)
        fp += len(pred - true)
        fn += len(true - pred)
    precision = tp / (tp + fp) if tp + fp else None
    recall = tp / (tp + fn) if tp + fn else None
    status = "参考値（データが少ないため精度は未確定）" if n < 20 else "評価済み"
    return {"n": n, "precision": precision, "recall": recall,
            "wrong_suggestions": fp, "abstain_rate": abstain / n, "status": status}


def load_active_model(db: Database) -> Model | None:
    row = db.active_model()
    if row is None:
        return None
    try:
        with open(row["file"], encoding="utf-8") as f:
            return Model.from_json(json.load(f))
    except (OSError, ValueError, KeyError):
        return None


def retrain(db: Database, store=None) -> dict:
    """手動の「再学習」。新しいモデルを作り、条件を満たせば採用する。失敗時は以前のモデルを維持。"""
    version = db.next_model_version()
    model, note, used = build_model(db, version, store)
    if model is None:
        return {"adopted": False, "message": "再学習できませんでした: " + note}
    ev = evaluate(model)
    old = load_active_model(db)
    adopt, why = True, ""
    if old is not None and ev.get("precision") is not None and ev["n"] >= 6:
        # 以前のモデルの設定（距離の目安）で同じ教師データを評価して比べる
        old_like = Model(old.version, model.normalizer, old.tau, model.samples, model.genre_ids)
        old_ev = evaluate(old_like)
        if old_ev.get("precision") is not None and ev["precision"] + 0.05 < old_ev["precision"]:
            adopt = False
            why = (f"新しいモデルの適合率 {ev['precision']:.2f} が以前の設定 "
                   f"{old_ev['precision']:.2f} より低いため、以前のモデルを維持しました。")
    path = db.model_dir / f"model_v{version}.json"
    with open(path, "w", encoding="utf-8") as f:
        json.dump(model.to_json(), f, ensure_ascii=False)
    db.add_model(version, len(model.samples), str(path), ev, activate=adopt)
    if not adopt:
        return {"adopted": False, "message": why, "evaluation": ev}
    db.mark_training_applied(version, [u for u in used if isinstance(u, int)])
    db.mark_sample_answers_applied(version, [u[len(SAMPLE_PREFIX):] for u in used if isinstance(u, str)])
    counts = classify_all(db, model)
    return {"adopted": True, "version": version, "evaluation": ev, "message": note,
            "classified": counts}


def classify_all(db: Database, model: Model | None = None) -> dict:
    """手動分類されていない動画を、採用中のモデルで再評価する。手動分類は上書きしない。"""
    model = model or load_active_model(db)
    counts = {"分類済み": 0, "未分類": 0}
    if model is None:
        return counts
    names = db.genre_names()
    user = db.all_user_genres()
    for row in db.videos():
        if row["id"] in user:
            continue
        v = feature_vector(loads(row["meta"]), loads(row["features"]))
        if v is None:
            if row["analysis_state"] == "失敗":
                db.update_video(row["id"], ai_state="未分類", ai_genres=[], ai_candidates=[],
                                ai_reason="映像・音声を解析できなかったため判断できません",
                                ai_model_version=model.version)
                counts["未分類"] += 1
            continue
        r = model.classify(model.normalizer.transform(v), names=names)
        rejected = set(loads(row["ai_rejected"], []))
        if rejected and r["genres"]:  # ユーザーが却下したジャンルは再提案しない
            r["genres"] = [e for e in r["genres"] if e["genre_id"] not in rejected]
            if not r["genres"]:
                r["state"], r["reason"] = "未分類", "提案できるジャンルは以前ユーザーが却下したものだけです"
        db.update_video(row["id"], ai_state=r["state"], ai_genres=r["genres"],
                        ai_candidates=r["candidates"][:3], ai_reason=r["reason"],
                        ai_model_version=model.version)
        counts[r["state"]] += 1
    return counts


# --- 初期学習の動画選択 -------------------------------------------------------
def select_diverse(db: Database, k: int = 5) -> list[int]:
    """特徴の異なる動画を最大 k 本選ぶ。

    k-means で k 個のグループに分け、各グループの中心に最も近い動画を選ぶ
    （距離が最も遠いものから選ぶと、黒画面などの例外ばかりになりやすいため）。
    完全一致の重複（同じハッシュ、または同じサイズと長さ）は1本にまとめる。
    """
    rows = {r["id"]: r for r in db.videos()}
    vectors = _load_vectors(db)
    seen_keys, ids = set(), []
    for vid in sorted(vectors):
        r = rows[vid]
        meta = loads(r["meta"], {})
        key = r["sha256"] or (r["size"], round(meta.get("duration", 0), 1))
        if key in seen_keys:
            continue
        seen_keys.add(key)
        ids.append(vid)
    if len(ids) <= k:
        return ids
    norm = Normalizer.fit([vectors[i] for i in ids])
    X = np.vstack([norm.transform(vectors[i]) for i in ids])
    rng = np.random.default_rng(0)
    centers = [X[rng.integers(len(X))]]
    for _ in range(1, k):  # k-means++ の初期化
        d2 = np.min([((X - c) ** 2).sum(axis=1) for c in centers], axis=0)
        centers.append(X[rng.choice(len(X), p=d2 / d2.sum())] if d2.sum() > 0 else X[rng.integers(len(X))])
    C = np.vstack(centers)
    for _ in range(50):
        labels = np.argmin(((X[:, None, :] - C[None]) ** 2).sum(axis=2), axis=1)
        newC = np.vstack([X[labels == j].mean(axis=0) if (labels == j).any() else C[j]
                          for j in range(k)])
        if np.allclose(newC, C):
            break
        C = newC
    chosen = []
    for j in range(k):
        members = np.where(labels == j)[0]
        if len(members) == 0:
            continue
        best = members[np.argmin(((X[members] - C[j]) ** 2).sum(axis=1))]
        chosen.append(ids[best])
    return chosen


# --- 整理推薦（4段階） ----------------------------------------------------------
HEAVY_CODECS = {"mpeg2video", "mpeg1video", "mjpeg", "rawvideo", "dvvideo", "prores", "huffyuv",
                "ffv1", "utvideo", "msmpeg4v3", "wmv2", "wmv3"}


def compression_reason(meta: dict | None, size: int) -> str | None:
    if not meta or not meta.get("has_video") or not meta.get("width"):
        return None
    w, h, fps, br = meta["width"], meta["height"], meta.get("fps") or 30, meta.get("bitrate", 0)
    if meta.get("vcodec") in HEAVY_CODECS:
        return f"圧縮効率の低い形式（{meta['vcodec']}）です"
    if br and w and h:
        bpp = br / (w * h * fps)
        if bpp > 0.15 and size > 50 * 1024 * 1024:
            return (f"解像度 {w}×{h} に対してビットレートが高めです（{br / 1e6:.1f} Mbps）")
    return None


def recommend(row, duplicate_groups: dict[str, list[int]] | None = None) -> dict:
    """保護対象 ＞ 重複確認 ＞ 圧縮候補 ＞ 内容確認 の順でメイン判定を決める。"""
    matches: list[tuple[str, str]] = []
    if row["protected"]:
        matches.append(("保護対象", "ユーザーが保護した動画です。整理・圧縮の操作はできません"))
    dup = duplicate_groups.get(row["sha256"]) if duplicate_groups and row["sha256"] else None
    if dup and len(dup) > 1:
        matches.append(("重複確認", f"内容が完全に一致する動画が他に {len(dup) - 1} 本あります"))
    reason = compression_reason(loads(row["meta"]), row["size"])
    if reason:
        matches.append(("圧縮候補", reason + "。圧縮で容量が減るとは限りません"))
    if not matches or matches[0][0] != "保護対象":
        if row["analysis_state"] in ("未解析", "失敗"):
            matches.append(("内容確認", "解析情報が不足しているため、内容を確認してください"))
        elif not matches:
            matches.append(("内容確認", "特別な整理理由は見つかりませんでした"))
    main, main_reason = matches[0]
    return {"main": main, "reason": main_reason, "extra": matches[1:]}


def duplicate_groups(db: Database) -> dict[str, list[int]]:
    groups: dict[str, list[int]] = {}
    for row in db.videos():
        if row["sha256"]:
            groups.setdefault(row["sha256"], []).append(row["id"])
    return {h: ids for h, ids in groups.items() if len(ids) > 1}


# --- バックグラウンド処理（別スレッドで実行。DB 接続はスレッド内で作る） ------------------
def task_analyze(data_dir, tools: media.FFmpegTools, video_ids: list[int], max_frames: int,
                 cancel: threading.Event, report) -> dict:
    db = Database(data_dir)
    done = {"完了": 0, "一部失敗": 0, "失敗": 0, "対象外": 0}
    try:
        for i, vid in enumerate(video_ids):
            if cancel.is_set():
                done["cancelled"] = True
                break
            row = db.video(vid)
            report({"text": f"解析中 {i + 1}/{len(video_ids)} 本: {row['name']}",
                    "done": i, "total": len(video_ids)})
            if row["cloud_only"]:
                db.update_video(vid, analysis_error="クラウド上のみのファイルのため解析しません")
                done["対象外"] += 1
                continue
            try:
                size, mtime_ns = media.file_state(row["path"])
                db.update_file_state(vid, size, mtime_ns / 1e9)
                db.update_video(vid, analysis_state="解析中")
                state, err, meta, feats, thumbs = media.analyze_video(
                    tools, row["path"], db.thumb_dir / f"v{vid}", max_frames, cancel)
            except media.Cancelled:
                db.update_video(vid, analysis_state="未解析")
                done["cancelled"] = True
                break
            except (OSError, RuntimeError, ValueError, TimeoutError, media.FileChanged) as e:
                db.update_video(vid, analysis_state="失敗", analysis_error=str(e))
                done["失敗"] += 1
                continue
            db.update_video(vid, analysis_state=state, analysis_error=err, meta=meta,
                            features=feats, thumbs=thumbs)
            for tag in media.suggested_tags(meta, feats):
                db.suggest_ai_tag(vid, tag)
            done[state] += 1
        model = load_active_model(db)
        if model is not None:
            classify_all(db, model)
    finally:
        db.close()
    return done


def task_find_duplicates(data_dir, cancel: threading.Event, report) -> dict:
    """サイズで候補を絞ってから SHA-256 を計算する。"""
    db = Database(data_dir)
    try:
        by_size: dict[int, list] = {}
        for row in db.videos():
            if not row["cloud_only"]:
                by_size.setdefault(row["size"], []).append(row)
        candidates = [r for rows in by_size.values() if len(rows) > 1 and rows[0]["size"] > 0
                      for r in rows]
        total = sum(r["size"] for r in candidates if not r["sha256"])
        processed = [0]
        errors, cancelled = [], False

        def on_bytes(n):
            processed[0] += n
            report({"text": f"ハッシュ計算中 ({processed[0] / 1024 ** 2:,.0f} / "
                            f"{total / 1024 ** 2:,.0f} MB)", "done": processed[0], "total": total})

        for row in candidates:
            if row["sha256"]:
                continue  # 変更がなければ前回の結果を使う
            try:
                size, mtime_ns = media.file_state(row["path"])
                if db.update_file_state(row["id"], size, mtime_ns / 1e9):
                    continue
                db.update_video(row["id"], sha256=media.sha256_file(row["path"], cancel, on_bytes))
            except media.Cancelled:
                cancelled = True
                break
            except (OSError, media.FileChanged) as e:
                errors.append(f"{row['name']}: {e}")
        groups = duplicate_groups(db)
        return {"groups": len(groups), "candidates": len(candidates), "errors": errors,
                "cancelled": cancelled}
    finally:
        db.close()


def task_retrain(data_dir, cancel, report, samples_root=None) -> dict:
    db = Database(data_dir)
    store = SampleStore(samples_root) if samples_root else None
    try:
        report({"text": "再学習中…"})
        if store is not None:
            store.refresh()  # 入れ替わったサンプルの回答を使わないよう、最新の状態で確認する
        return retrain(db, store)
    finally:
        db.close()
        if store is not None:
            store.close()


def task_compress(data_dir, tools, video_id: int, out_dir: str, preset: str,
                  cancel: threading.Event, report) -> dict:
    db = Database(data_dir)
    try:
        row = db.video(video_id)
        if row["protected"]:
            raise PermissionError("保護中の動画は圧縮できません。保護を解除してから実行してください。")
        if row["cloud_only"]:
            raise PermissionError("クラウド上のみのファイルは圧縮できません。")
        meta = loads(row["meta"]) or media.probe(tools, row["path"], cancel)
        output = media.compressed_output_path(row["path"], out_dir, preset)
        start = time.monotonic()

        def on_progress(fraction):
            report({"text": f"圧縮中: {Path(output).name}", "done": fraction, "total": 1.0})
        result = media.compress(tools, row["path"], output, preset, meta.get("duration", 0),
                                cancel, on_progress)
        result["elapsed"] = time.monotonic() - start
        return result
    finally:
        db.close()


def describe_features(features: dict | None, meta: dict | None) -> str:
    if not features:
        return "未解析"
    lines = []
    if meta:
        lines.append(f"長さ {meta.get('duration', 0):.0f}秒 / {meta.get('width')}×{meta.get('height')} / "
                     f"{meta.get('vcodec') or '-'} / 音声 {'あり' if meta.get('has_audio') else 'なし'}")
    v, a = features.get("video"), features.get("audio")
    if v:
        lines.append(f"映像（{v['frames']}枚抽出）: 明るさ {v['brightness']:.2f} / 黒画面率 "
                     f"{v['black_ratio']:.0%} / 静止率 {v['static_ratio']:.0%} / 動き {v['motion']:.3f}")
    if a:
        lines.append(f"音声（抽出区間）: 平均音量 {a['loudness_db']:.0f} dB / 無音率 {a['silence_ratio']:.0%}")
    lines.append("※ 音楽・歌唱・会話などの意味の判定は未実装です")
    return "\n".join(lines)


def file_exists(path: str) -> bool:
    return os.path.exists(path)


# =============================================================================
# 厳選AI（ジャンル別・全動画）
#   ジャンル分類AIとも、互いとも、教師データとモデルを分けて持つ。
#   特徴量にはジャンルを含めない（ジャンルによる優遇・減点をしない）。
# =============================================================================
MIN_PER_CLASS = 2  # 「残す」「整理候補」がそれぞれ何件以上あれば学習するか
KEEP, DROP, HOLD = "残す候補", "整理候補", "保留"
_LABEL_TO_SUGGEST = {"残す": KEEP, "整理候補": DROP}


def curation_vector(row) -> np.ndarray:
    """解析特徴（未解析なら欠損扱い）＋ファイルサイズ＋経過日数。ジャンルは使わない。"""
    base = feature_vector(loads(row["meta"]), loads(row["features"]))
    if base is None:
        base = np.full(len(FEATURE_NAMES), np.nan)
    age_days = max((time.time() - row["mtime"]) / 86400, 0)
    return np.concatenate([base, [np.log1p(row["size"]), np.log1p(age_days)]])


def scope_label(db: Database, scope: str) -> str:
    if scope == CURATE_ALL_SCOPE:
        return "全動画厳選AI"
    if scope == CLASSIFY_SCOPE:
        return "ジャンル分類AI"
    gid = int(scope.split(":")[1])
    return f"ジャンル別厳選AI（{db.genre_names().get(gid, f'削除済みジャンル{gid}')}）"


def _scope_args(scope: str) -> tuple[str, int | None]:
    if scope == CURATE_ALL_SCOPE:
        return "all", None
    return "genre", int(scope.split(":")[1])


def _fingerprint(samples: list[tuple[int, str]]) -> str:
    return hashlib.sha1(json.dumps(sorted(samples)).encode()).hexdigest()


@dataclass
class CurationModel:
    version: int
    scope: str
    normalizer: Normalizer
    tau: float
    samples: list[dict]  # {"video_id", "name", "z", "label"}
    fingerprint: str

    def to_json(self) -> dict:
        return {"version": self.version, "scope": self.scope, "mean": self.normalizer.mean.tolist(),
                "std": self.normalizer.std.tolist(), "tau": self.tau, "samples": self.samples,
                "fingerprint": self.fingerprint, "method": "類似度比較（最近傍）"}

    @classmethod
    def from_json(cls, d: dict) -> "CurationModel":
        return cls(d["version"], d["scope"], Normalizer(np.array(d["mean"]), np.array(d["std"])),
                   d["tau"], d["samples"], d["fingerprint"])

    def predict(self, row, exclude_video: int | None = None) -> tuple[str, str]:
        z = self.normalizer.transform(curation_vector(row))
        samples = [s for s in self.samples if s["video_id"] != exclude_video]
        best = {}
        for s in samples:
            d = _distance(z, np.array(s["z"]))
            if s["label"] not in best or d < best[s["label"]][0]:
                best[s["label"]] = (d, s["name"])
        if len(best) < 2:
            return HOLD, "教師データが不足しています"
        (d_keep, n_keep), (d_drop, n_drop) = best["残す"], best["整理候補"]
        label, (d, name), other = ("残す", (d_keep, n_keep), d_drop) if d_keep <= d_drop \
            else ("整理候補", (d_drop, n_drop), d_keep)
        sim = similarity_from_distance(d, self.tau)
        if sim < MIN_SIMILARITY:
            return HOLD, f"似た回答の動画がありません（最大類似度 {sim:.2f}）"
        if d >= other * MARGIN:
            return HOLD, "「残す」と「整理候補」の回答に同じくらい近く、決め手がありません"
        return _LABEL_TO_SUGGEST[label], f"「{label}」と回答した {name} に近い（類似度 {sim:.2f}）"


def build_curation_model(db: Database, scope: str, version: int) -> tuple[CurationModel | None, str]:
    kind, gid = _scope_args(scope)
    labels = db.training_labels(kind, gid)
    rows = {r["id"]: r for r in db.videos()}
    labels = [lb for lb in labels if lb["video_id"] in rows]
    counts = {c: sum(1 for lb in labels if lb["label"] == c) for c in TRAINABLE_CHOICES}
    if min(counts.values()) < MIN_PER_CLASS:
        return None, (f"教師データ不足（残す {counts['残す']} 件・整理候補 {counts['整理候補']} 件。"
                      f"それぞれ {MIN_PER_CLASS} 件以上必要です）")
    vectors = [curation_vector(r) for r in rows.values()]
    norm = Normalizer.fit(vectors)
    zs = [norm.transform(v) for v in vectors]
    if len(zs) >= 2:
        idx = np.random.default_rng(0).choice(len(zs), size=min(len(zs), 200), replace=False)
        pts = [zs[i] for i in idx]
        ds = [_distance(pts[i], pts[j]) for i in range(len(pts)) for j in range(i + 1, len(pts))]
        tau = max(float(np.median(ds)), 0.3)
    else:
        tau = 1.0
    samples = [{"video_id": lb["video_id"], "name": rows[lb["video_id"]]["name"], "label": lb["label"],
                "z": norm.transform(curation_vector(rows[lb["video_id"]])).round(5).tolist()}
               for lb in labels]
    fp = _fingerprint([(s["video_id"], s["label"]) for s in samples])
    return CurationModel(version, scope, norm, tau, samples, fp), f"教師データ {len(samples)} 件で作成しました"


def evaluate_curation(model: CurationModel, rows: dict) -> dict:
    n = len(model.samples)
    correct = wrong = hold = 0
    for s in model.samples:
        pred, _ = model.predict(rows[s["video_id"]], exclude_video=s["video_id"])
        if pred == HOLD:
            hold += 1
        elif pred == _LABEL_TO_SUGGEST[s["label"]]:
            correct += 1
        else:
            wrong += 1
    decided = correct + wrong
    return {"n": n, "accuracy": correct / decided if decided else None, "wrong": wrong,
            "hold_rate": hold / n if n else 0,
            "status": "参考値（データが少ないため精度は未確定）" if n < 20 else "評価済み"}


def load_curation_models(db: Database) -> dict[str, CurationModel]:
    result = {}
    for scope, row in db.active_models().items():
        if scope == CLASSIFY_SCOPE:
            continue
        try:
            with open(row["file"], encoding="utf-8") as f:
                result[scope] = CurationModel.from_json(json.load(f))
        except (OSError, ValueError, KeyError):
            pass  # 読めないモデルは使わない（未学習扱い）
    return result


def curation_scopes_with_data(db: Database) -> list[str]:
    scopes = [CURATE_ALL_SCOPE] if db.effective_labels("all") else []
    return scopes + [curate_genre_scope(g) for g in sorted(db.labeled_genres())]


def retrain_curation(db: Database, scopes: list[str] | None = None) -> dict[str, dict]:
    """厳選AIを再学習する。教師データに変更があった学習範囲だけを作り直す。

    作れなかった場合（データ不足・失敗）は以前のモデルを維持する。
    """
    models = load_curation_models(db)
    rows = {r["id"]: r for r in db.videos()}
    results = {}
    for scope in scopes or curation_scopes_with_data(db):
        kind, gid = _scope_args(scope)
        current = [(lb["video_id"], lb["label"]) for lb in db.training_labels(kind, gid)
                   if lb["video_id"] in rows]
        old = models.get(scope)
        if old is not None and old.fingerprint == _fingerprint(current):
            results[scope] = {"status": "変更なし", "message": "教師データに変更がないため再学習しません"}
            continue
        try:
            version = db.next_model_version()
            model, msg = build_curation_model(db, scope, version)
            if model is None:
                kept = "（以前のモデルを維持）" if old else ""
                results[scope] = {"status": "データ不足", "message": msg + kept}
                continue
            ev = evaluate_curation(model, rows)
            path = db.model_dir / f"{scope.replace(':', '_')}_v{version}.json"
            with open(path, "w", encoding="utf-8") as f:
                json.dump(model.to_json(), f, ensure_ascii=False)
            db.add_model(version, len(model.samples), str(path), ev, activate=True, scope=scope)
            results[scope] = {"status": "更新", "message": msg, "evaluation": ev, "version": version}
        except Exception as e:  # 失敗しても以前のモデルは有効のまま
            results[scope] = {"status": "失敗", "message": f"再学習に失敗しました（以前のモデルを維持）: {e}"}
    return results


def combine_genre_predictions(preds: list[tuple[str, str]]) -> tuple[str, str]:
    """ジャンルごとの判定 [(判定, 理由)] を統合する。"""
    if not preds:
        return HOLD, "判定に使えるジャンル別厳選AIがありません"
    labels = [p for p, _ in preds]
    if HOLD in labels:
        return HOLD, ("すべてのジャンルで判定できませんでした" if set(labels) == {HOLD}
                      else "判定できないジャンルがあるため保留にしました")
    if set(labels) == {KEEP}:
        return KEEP, "対象ジャンルすべてで「残す候補」です"
    if set(labels) == {DROP}:
        return DROP, "対象ジャンルすべてで「整理候補」です"
    return HOLD, "ジャンルによって判定が分かれたため保留にしました"


def target_genres(row: dict, selected: set[int], include_ai: bool) -> set[int]:
    """厳選の対象ジャンル = 選択したジャンル ∩ 動画が属するジャンル。"""
    member = set(row["user_genres"]) | (set(row["ai_genres"]) if include_ai else set())
    return member & selected


def select_curation_rows(rows: list[dict], selected: set[int], include_ai: bool) -> list[dict]:
    """ジャンル別厳選の対象（OR 条件）。複数ジャンルに属する動画も1本として返す。"""
    return [r for r in rows if target_genres(r, selected, include_ai)]


def predict_for_mode(db: Database, rows: list[dict], mode: str, selected: set[int] | None = None,
                     include_ai: bool = False, models: dict | None = None) -> dict[int, dict]:
    """厳選画面用の AI 提案。全動画モードでジャンル別AIは使わない（逆も同じ）。"""
    models = load_curation_models(db) if models is None else models
    names = db.genre_names()
    raw = {r["id"]: r for r in db.videos()}
    out = {}
    for row in rows:
        vid = row["id"]
        if row["protected"]:
            out[vid] = {"suggest": "—", "reason": "保護中のため判定対象外です", "per_genre": []}
            continue
        if mode == "all":
            model = models.get(CURATE_ALL_SCOPE)
            if model is None:
                out[vid] = {"suggest": HOLD, "reason": "全動画厳選AIが未学習です（教師データ不足）",
                            "per_genre": []}
            else:
                sug, why = model.predict(raw[vid])
                out[vid] = {"suggest": sug, "reason": why, "per_genre": []}
            continue
        per = []
        for gid in sorted(target_genres(row, selected or set(), include_ai)):
            model = models.get(curate_genre_scope(gid))
            sug, why = (model.predict(raw[vid]) if model else
                        (HOLD, "このジャンルの厳選AIは未学習です（教師データ不足）"))
            per.append((names.get(gid, "?"), sug, why))
        sug, why = combine_genre_predictions([(p[1], p[2]) for p in per])
        detail = " ／ ".join(f"{g}: {s}（{w}）" for g, s, w in per)
        out[vid] = {"suggest": sug, "reason": why + ("。" + detail if detail else ""), "per_genre": per}
    return out


def reset_plan(db: Database, scopes: list[str]) -> dict:
    """リセット前の確認用に、初期化するもの・残すものを区別して返す。"""
    active = db.active_models()
    plan = {"reset_models": [], "kept_labels": [], "cleared_predictions": [], "untouched": []}
    for scope in scopes:
        name = scope_label(db, scope)
        if scope in active:
            plan["reset_models"].append(f"{name}: モデル v{active[scope]['version']} を未学習状態に戻す")
        else:
            plan["reset_models"].append(f"{name}: 学習済みモデルはありません")
        if scope == CLASSIFY_SCOPE:
            plan["kept_labels"].append(f"{name}: 教師データ {len(db.training_rows())} 件は保持")
            plan["cleared_predictions"].append(f"{name}: AIの分類結果（予測）を消去。手動の分類は保持")
        else:
            kind, gid = _scope_args(scope)
            n_active = len(db.effective_labels(kind, gid)) if kind == "all" else \
                len(db.effective_labels("genre", gid))
            n_hist = db.conn.execute(
                "SELECT COUNT(*) FROM curation_labels WHERE scope=? AND genre_id IS ?", (kind, gid)).fetchone()[0]
            plan["kept_labels"].append(f"{name}: 有効な回答 {n_active} 件・回答履歴 {n_hist} 件は保持")
    others = [scope_label(db, s) for s in active if s not in scopes]
    plan["untouched"] = others
    return plan


def reset_models(db: Database, scopes: list[str]) -> dict[str, list[int]]:
    """指定した学習範囲のモデルだけを初期化する。教師データ・回答履歴・動画・ジャンル・保護は変えない。"""
    return {scope: db.deactivate_models(scope) for scope in scopes}


def task_retrain_curation(data_dir, scopes, cancel, report) -> dict:
    db = Database(data_dir)
    try:
        report({"text": "厳選AIを再学習中…"})
        results = retrain_curation(db, scopes)
        return {scope_label(db, s): r for s, r in results.items()}
    finally:
        db.close()

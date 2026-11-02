"""動画1本を解析して問題点を判定する

判定カテゴリ:
  - 短すぎる / 長すぎる
  - 画質が粗い   (低解像度 / 低フレームレート / 映像のぼやけ)
  - 音声が不明瞭 (音声なし / 無音 / ほぼ無音 / 小音量 / 音割れ / 低音質)
"""

from __future__ import annotations

import json
import math
import re
import statistics
from dataclasses import dataclass, field
from pathlib import Path

from . import ffmpeg

VIDEO_EXTS = {".mp4", ".mkv", ".mov", ".avi", ".wmv", ".flv", ".webm", ".m4v", ".ts", ".mts", ".m2ts", ".3gp"}

CAT_SHORT = "短すぎる"
CAT_LONG = "長すぎる"
CAT_VIDEO = "画質が粗い"
CAT_AUDIO = "音声が不明瞭"
CAT_ERROR = "読み込みエラー"
CAT_DUP = "重複"


@dataclass
class Thresholds:
    """判定基準。GUI と CLI で共通。"""
    min_sec: float = 10                # これ未満は短すぎる (秒)
    max_sec: float = 90 * 60           # これ以上は長すぎる (秒)
    min_height: int = 720              # 短辺がこれ未満は低解像度 (px)
    min_fps: float = 15                # これ未満は低フレームレート
    blur_threshold: float = 8.0        # ぼやけ値がこれを超えるとぼやけ (くっきり≈5, 引き伸ばし≈10, 強いぼけ≈20)
    check_blur: bool = True            # ぼやけ検出をするか (しないと高速)
    min_mean_db: float = -40           # 平均音量がこれ未満は小音量 (dB)
    silence_db: float = -50            # これ以下の音量を無音とみなす (dB)
    max_silence: float = 0.8           # 無音率がこれを超えるとほぼ無音 (0〜1)
    max_clip: float = 0.001            # クリップ率がこれを超えると音割れ (0〜1)
    min_sample_rate: int = 16000       # これ未満のサンプリングレートは低音質 (Hz)
    min_audio_kbps: float = 48         # これ未満の音声ビットレートは低音質 (kbps)
    samples: int = 5                   # 1本あたりの解析サンプル箇所数
    seg_len: float = 20                # 1サンプルあたりの音声解析秒数


@dataclass
class VideoReport:
    path: Path
    duration: float = 0.0
    width: int = 0
    height: int = 0
    fps: float = 0.0
    blur: float | None = None
    has_audio: bool = False
    sample_rate: int = 0
    audio_kbps: float | None = None
    mean_db: float | None = None
    max_db: float | None = None
    silence_ratio: float | None = None
    clip_ratio: float | None = None
    categories: list[str] = field(default_factory=list)
    reasons: list[str] = field(default_factory=list)

    def flag(self, category: str, reason: str) -> None:
        if category not in self.categories:
            self.categories.append(category)
        self.reasons.append(reason)


# ---------------------------------------------------------------- 補助

def fmt_time(sec: float) -> str:
    sec = int(round(sec))
    h, rem = divmod(sec, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def parse_fps(rate: str) -> float:
    try:
        num, den = rate.split("/")
        return float(num) / float(den) if float(den) else 0.0
    except (ValueError, ZeroDivisionError):
        return 0.0


def sample_points(duration: float, count: int, seg_len: float) -> list[float]:
    """動画全体から均等に count 箇所、解析の開始位置を選ぶ。短い動画は先頭1箇所 (全体) のみ。"""
    if duration <= seg_len * count:
        return [0.0]
    usable = duration - seg_len
    return [usable * (i + 0.5) / count for i in range(count)]


def _last(pattern: str, log: str) -> str | None:
    # ffmpeg は初期化時に "n_samples: 0" を余分に出すことがあるので最後の値を使う
    found = re.findall(pattern, log)
    return found[-1] if found else None


# ---------------------------------------------------------------- 測定

def probe(path: Path) -> dict:
    p = ffmpeg.run([ffmpeg.ffprobe(), "-v", "error", "-print_format", "json",
                    "-show_format", "-show_streams", str(path)], timeout=60)
    if p.returncode != 0:
        lines = p.stderr.strip().splitlines()
        raise RuntimeError(lines[-1] if lines else "ffprobe失敗")
    return json.loads(p.stdout)


def measure_blur(path: Path, starts: list[float], frames_per_point: int = 3) -> float | None:
    """blurdetect の中央値 (大きいほどぼやけている)。"""
    values: list[float] = []
    for ss in starts:
        p = ffmpeg.run([ffmpeg.ffmpeg(), "-v", "error", "-ss", f"{ss:.2f}", "-i", str(path), "-an",
                        "-vf", "fps=1,blurdetect,metadata=print:file=-",
                        "-frames:v", str(frames_per_point), "-f", "null", "-"], timeout=120)
        values += [float(v) for v in re.findall(r"lavfi\.blur=([0-9.]+)", p.stdout + p.stderr)]
    values = [v for v in values if math.isfinite(v) and v > 0]  # 真っ黒な画面などは除外
    return statistics.median(values) if values else None


def measure_audio(path: Path, starts: list[float], seg_len: float, duration: float, silence_db: float) -> dict:
    """平均音量・最大音量・無音率・クリップ率をサンプル区間から集計する。"""
    powers, weights = [], []
    max_db = -math.inf
    total_samples = clipped = 0
    silent_sec = analysed_sec = 0.0
    for ss in starts:
        length = min(seg_len, max(duration - ss, 0.0)) if len(starts) > 1 else duration
        if length <= 0:
            continue
        p = ffmpeg.run([ffmpeg.ffmpeg(), "-hide_banner", "-nostats", "-ss", f"{ss:.2f}", "-t", f"{length:.2f}",
                        "-i", str(path), "-vn", "-af", f"silencedetect=noise={silence_db}dB:d=1,volumedetect",
                        "-f", "null", "-"], timeout=300)
        log = p.stderr
        s_mean = _last(r"mean_volume:\s*(-?[0-9.]+|-inf) dB", log)
        if s_mean is None:
            continue
        s_max = _last(r"max_volume:\s*(-?[0-9.]+|-inf) dB", log)
        s_n = _last(r"n_samples:\s*(\d+)", log)
        s_0db = _last(r"histogram_0db:\s*(\d+)", log)

        mean = float(s_mean) if s_mean != "-inf" else -120.0
        powers.append(10 ** (mean / 10))
        weights.append(length)
        if s_max and s_max != "-inf":
            max_db = max(max_db, float(s_max))
        if s_n:
            total_samples += int(s_n)
            clipped += int(s_0db) if s_0db else 0
        # 無音区間の合計 (区間末尾まで無音で silence_end が出ないケースも考慮)
        starts_s = [float(x) for x in re.findall(r"silence_start:\s*(-?[0-9.]+)", log)]
        ends_s = [float(x) for x in re.findall(r"silence_end:\s*(-?[0-9.]+)", log)]
        for i, st in enumerate(starts_s):
            en = ends_s[i] if i < len(ends_s) else length
            silent_sec += max(0.0, min(en, length) - max(st, 0.0))
        analysed_sec += length

    if not powers:
        return {}
    mean_power = sum(pw * w for pw, w in zip(powers, weights)) / sum(weights)
    return {
        "mean_db": 10 * math.log10(mean_power) if mean_power > 0 else -120.0,
        "max_db": max_db if math.isfinite(max_db) else None,
        "silence_ratio": silent_sec / analysed_sec if analysed_sec else None,
        "clip_ratio": clipped / total_samples if total_samples else None,
    }


# ---------------------------------------------------------------- 判定

def analyse(path: Path, th: Thresholds | None = None) -> VideoReport:
    th = th or Thresholds()
    r = VideoReport(path)
    try:
        info = probe(path)
    except Exception as e:  # noqa: BLE001
        r.flag(CAT_ERROR, f"解析不可: {e}")
        return r

    streams = info.get("streams", [])
    v = next((s for s in streams
              if s.get("codec_type") == "video" and not s.get("disposition", {}).get("attached_pic")), None)
    au = next((s for s in streams if s.get("codec_type") == "audio"), None)
    r.duration = float(info.get("format", {}).get("duration") or (v or {}).get("duration") or 0)

    _judge_length(r, th)
    starts = sample_points(r.duration, th.samples, th.seg_len)
    _judge_video(r, v, starts, th)
    _judge_audio(r, au, starts, th)
    return r


def _judge_length(r: VideoReport, th: Thresholds) -> None:
    if r.duration < th.min_sec:
        r.flag(CAT_SHORT, f"長さ {fmt_time(r.duration)} (< {th.min_sec:g}秒)")
    if r.duration >= th.max_sec:
        r.flag(CAT_LONG, f"長さ {fmt_time(r.duration)} (≥ {fmt_time(th.max_sec)})")


def _judge_video(r: VideoReport, v: dict | None, starts: list[float], th: Thresholds) -> None:
    if v is None:
        r.flag(CAT_VIDEO, "映像トラックなし")
        return
    r.width, r.height = int(v.get("width") or 0), int(v.get("height") or 0)
    r.fps = parse_fps(v.get("avg_frame_rate") or v.get("r_frame_rate") or "0/1")
    short_side = min(r.width, r.height) if r.width and r.height else 0
    if short_side and short_side < th.min_height:
        r.flag(CAT_VIDEO, f"低解像度 {r.width}x{r.height} (< {th.min_height}p)")
    if r.fps and r.fps < th.min_fps:
        r.flag(CAT_VIDEO, f"低フレームレート {r.fps:.1f}fps (< {th.min_fps:g})")
    if th.check_blur and r.duration > 0:
        r.blur = measure_blur(r.path, starts)
        if r.blur is not None and r.blur > th.blur_threshold:
            r.flag(CAT_VIDEO, f"映像がぼやけている (ぼやけ値 {r.blur:.1f} > {th.blur_threshold:g})")


def _judge_audio(r: VideoReport, au: dict | None, starts: list[float], th: Thresholds) -> None:
    if au is None:
        r.flag(CAT_AUDIO, "音声トラックなし")
        return
    r.has_audio = True
    r.sample_rate = int(au.get("sample_rate") or 0)
    if au.get("bit_rate"):
        r.audio_kbps = int(au["bit_rate"]) / 1000
    if r.sample_rate and r.sample_rate < th.min_sample_rate:
        r.flag(CAT_AUDIO, f"低サンプリングレート {r.sample_rate}Hz (< {th.min_sample_rate})")
    if r.duration <= 0:
        return

    st = measure_audio(r.path, starts, th.seg_len, r.duration, th.silence_db)
    r.mean_db, r.max_db = st.get("mean_db"), st.get("max_db")
    r.silence_ratio, r.clip_ratio = st.get("silence_ratio"), st.get("clip_ratio")
    if r.max_db is not None and r.max_db < th.silence_db:
        r.flag(CAT_AUDIO, f"無音 (最大音量 {r.max_db:.1f}dB)")
        return
    if r.silence_ratio is not None and r.silence_ratio > th.max_silence:
        r.flag(CAT_AUDIO, f"ほぼ無音 (無音率 {r.silence_ratio:.0%} > {th.max_silence:.0%})")
    quiet = r.mean_db is not None and r.mean_db < th.min_mean_db
    if quiet:
        r.flag(CAT_AUDIO, f"音量が小さい (平均 {r.mean_db:.1f}dB < {th.min_mean_db:g}dB)")
    if r.clip_ratio is not None and r.clip_ratio > th.max_clip:
        r.flag(CAT_AUDIO, f"音割れ (クリップ率 {r.clip_ratio:.2%} > {th.max_clip:.2%})")
    # 無音・小音量は圧縮でビットレートが自然に下がるので、それ以外のときだけ判定
    if not quiet and r.audio_kbps and r.audio_kbps < th.min_audio_kbps:
        r.flag(CAT_AUDIO, f"低音声ビットレート {r.audio_kbps:.0f}kbps (< {th.min_audio_kbps:g})")

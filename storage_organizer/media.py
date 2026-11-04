"""FFmpeg を使った動画の読み取り処理（解析・サムネイル・ハッシュ・圧縮）。

安全上の方針:
- 元の動画は読み取るだけで、変更・上書き・削除しない
- 書き込むのはアプリのキャッシュ（サムネイル）と、圧縮時にユーザーが選んだ出力先の新規ファイルだけ
- クラウド上のみのファイルは中身を読まない（勝手にダウンロードさせない）
- 読み取りの前後でファイル状態を比べ、途中で変わったら結果を使わない
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

import numpy as np

_NO_WINDOW = 0x08000000 if sys.platform == "win32" else 0  # CREATE_NO_WINDOW

FRAME_W, FRAME_H = 32, 18
AUDIO_RATE = 8000
AUDIO_SEGMENT_SEC = 3.0
MAX_FRAMES = 80


class Cancelled(Exception):
    pass


class FileChanged(Exception):
    pass


# --- FFmpeg の場所 ------------------------------------------------------------
@dataclass
class FFmpegTools:
    ffmpeg: str | None
    ffprobe: str | None

    @property
    def available(self) -> bool:
        return bool(self.ffmpeg and self.ffprobe)


def find_ffmpeg(custom_dir: str | None = None) -> FFmpegTools:
    def find(name: str) -> str | None:
        if custom_dir:
            for candidate in (name, name + ".exe"):
                p = Path(custom_dir) / candidate
                if p.is_file():
                    return str(p)
        return shutil.which(name)
    return FFmpegTools(find("ffmpeg"), find("ffprobe"))


def _run(args: list[str], cancel: threading.Event | None = None, timeout: float = 120,
         stdin_data: bytes | None = None) -> subprocess.CompletedProcess:
    proc = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            stdin=subprocess.DEVNULL, creationflags=_NO_WINDOW)
    result: dict = {}

    def communicate():
        result["out"], result["err"] = proc.communicate()

    t = threading.Thread(target=communicate, daemon=True)
    t.start()
    waited = 0.0
    while t.is_alive():
        t.join(0.1)
        waited += 0.1
        if (cancel is not None and cancel.is_set()) or waited > timeout:
            proc.kill()
            t.join()
            if cancel is not None and cancel.is_set():
                raise Cancelled()
            raise TimeoutError("FFmpeg の処理が時間内に終わりませんでした")
    return subprocess.CompletedProcess(args, proc.returncode, result.get("out", b""),
                                       result.get("err", b""))


# --- ファイル状態 ---------------------------------------------------------------
def file_state(path: str) -> tuple[int, int]:
    st = os.stat(path)
    return st.st_size, st.st_mtime_ns


def ensure_unchanged(path: str, before: tuple[int, int]) -> None:
    if file_state(path) != before:
        raise FileChanged("処理中にファイルが変更されたため、結果を使用しません")


# --- 情報取得 ---------------------------------------------------------------
def probe(tools: FFmpegTools, path: str, cancel=None) -> dict:
    cp = _run([tools.ffprobe, "-v", "error", "-print_format", "json", "-show_format",
               "-show_streams", path], cancel, timeout=60)
    if cp.returncode != 0:
        raise RuntimeError("動画情報を読み取れませんでした: " +
                           cp.stderr.decode("utf-8", "replace").strip()[:200])
    data = json.loads(cp.stdout or b"{}")
    fmt = data.get("format", {})
    video = next((s for s in data.get("streams", []) if s.get("codec_type") == "video"
                  and not s.get("disposition", {}).get("attached_pic")), None)
    audio = next((s for s in data.get("streams", []) if s.get("codec_type") == "audio"), None)

    def num(value, cast=float, default=0):
        try:
            return cast(value)
        except (TypeError, ValueError):
            return default

    fps = 0.0
    if video and video.get("avg_frame_rate", "0/0") not in ("0/0", ""):
        n, _, d = video["avg_frame_rate"].partition("/")
        fps = num(n) / num(d, default=1) if num(d, default=0) else 0.0
    return {
        "duration": num(fmt.get("duration")),
        "bitrate": num(fmt.get("bit_rate"), int),
        "format": fmt.get("format_name", ""),
        "has_video": video is not None,
        "width": num(video.get("width"), int) if video else 0,
        "height": num(video.get("height"), int) if video else 0,
        "fps": round(fps, 3),
        "vcodec": video.get("codec_name", "") if video else "",
        "has_audio": audio is not None,
        "acodec": audio.get("codec_name", "") if audio else "",
    }


def _sample_times(duration: float, count: int) -> list[float]:
    if duration <= 0:
        return [0.0]
    count = max(1, count)
    return [duration * (i + 0.5) / count for i in range(count)]


def frame_count_for(duration: float, max_frames: int) -> int:
    """長さに応じた抽出枚数（10秒に1枚、6〜max_frames 枚）。"""
    return int(min(max_frames, max(6, duration // 10)))


def extract_frames(tools: FFmpegTools, path: str, duration: float, count: int,
                   cancel=None) -> np.ndarray:
    frames = []
    for t in _sample_times(duration, count):
        if cancel is not None and cancel.is_set():
            raise Cancelled()
        cp = _run([tools.ffmpeg, "-v", "error", "-ss", f"{t:.3f}", "-i", path, "-frames:v", "1",
                   "-vf", f"scale={FRAME_W}:{FRAME_H}", "-pix_fmt", "rgb24", "-f", "rawvideo", "-"],
                  cancel, timeout=60)
        if cp.returncode == 0 and len(cp.stdout) == FRAME_W * FRAME_H * 3:
            frames.append(np.frombuffer(cp.stdout, np.uint8).reshape(FRAME_H, FRAME_W, 3))
    if not frames:
        raise RuntimeError("映像フレームを取り出せませんでした")
    return np.stack(frames).astype(np.float32)


def extract_audio_segments(tools: FFmpegTools, path: str, duration: float, count: int = 12,
                           cancel=None) -> list[np.ndarray]:
    segments = []
    seg = min(AUDIO_SEGMENT_SEC, duration) if duration > 0 else AUDIO_SEGMENT_SEC
    for t in _sample_times(max(duration - seg, 0.0), count if duration > seg * 2 else 1):
        if cancel is not None and cancel.is_set():
            raise Cancelled()
        cp = _run([tools.ffmpeg, "-v", "error", "-ss", f"{t:.3f}", "-t", f"{seg:.3f}", "-i", path,
                   "-vn", "-ac", "1", "-ar", str(AUDIO_RATE), "-f", "s16le", "-"], cancel, timeout=60)
        if cp.returncode == 0 and len(cp.stdout) >= 2 * AUDIO_RATE // 10:
            segments.append(np.frombuffer(cp.stdout[: len(cp.stdout) // 2 * 2], np.int16)
                            .astype(np.float32) / 32768.0)
    if not segments:
        raise RuntimeError("音声を取り出せませんでした")
    return segments


def make_thumbnails(tools: FFmpegTools, path: str, duration: float, out_prefix: Path,
                    cancel=None) -> list[str]:
    """異なる場面（20%・50%・80%）のサムネイルをアプリのキャッシュに作る。"""
    paths = []
    times = [duration * r for r in (0.2, 0.5, 0.8)] if duration > 0 else [0.0]
    for i, t in enumerate(times):
        out = f"{out_prefix}_{i}.jpg"
        cp = _run([tools.ffmpeg, "-v", "error", "-y", "-ss", f"{t:.3f}", "-i", path,
                   "-frames:v", "1", "-vf", "scale=320:-2", "-q:v", "5", out], cancel, timeout=60)
        if cp.returncode == 0 and os.path.exists(out):
            paths.append(out)
    return paths


# --- 特徴量 -----------------------------------------------------------------
def frame_features(frames: np.ndarray) -> dict:
    rgb = frames / 255.0
    luma = rgb @ np.array([0.299, 0.587, 0.114], np.float32)  # (n, h, w)
    mx, mn = rgb.max(axis=3), rgb.min(axis=3)
    sat = np.where(mx > 0, (mx - mn) / np.maximum(mx, 1e-6), 0)
    frame_mean = luma.mean(axis=(1, 2))
    frame_std = luma.std(axis=(1, 2))
    black = (frame_mean < 0.08) & (frame_std < 0.04)
    diffs = np.abs(np.diff(luma, axis=0)).mean(axis=(1, 2)) if len(luma) > 1 else np.zeros(1)
    static = diffs < 0.01
    grad = (np.abs(np.diff(luma, axis=1)).mean() + np.abs(np.diff(luma, axis=2)).mean()) / 2
    # 色相のヒストグラム（彩度で重み付け）
    r, g, b = rgb[..., 0], rgb[..., 1], rgb[..., 2]
    hue = (np.degrees(np.arctan2(np.sqrt(3) * (g - b), 2 * r - g - b)) + 360) % 360
    hist, _ = np.histogram(hue, bins=6, range=(0, 360), weights=sat)
    hist = hist / max(hist.sum(), 1e-6)
    white = ((luma > 0.85) & (sat < 0.15)).mean()
    return {
        "frames": int(len(frames)),
        "brightness": float(frame_mean.mean()),
        "brightness_var": float(frame_mean.std()),
        "contrast": float(frame_std.mean()),
        "saturation": float(sat.mean()),
        "black_ratio": float(black.mean()),
        "static_ratio": float(static.mean()) if len(luma) > 1 else 1.0,
        "motion": float(diffs.mean()),
        "edges": float(grad),
        "white_ratio": float(white),
        "hue_hist": [float(x) for x in hist],
    }


def audio_features(segments: list[np.ndarray]) -> dict:
    win = AUDIO_RATE // 20  # 50ms
    rms_db, zcr, centroid, flatness, modulation = [], [], [], [], []
    for x in segments:
        n = len(x) // win
        if n == 0:
            continue
        frames = x[: n * win].reshape(n, win)
        rms = np.sqrt((frames ** 2).mean(axis=1) + 1e-12)
        db = 20 * np.log10(rms + 1e-9)
        rms_db.extend(db.tolist())
        zcr.append(float((np.abs(np.diff(np.sign(frames), axis=1)) > 0).mean()))
        spec = np.abs(np.fft.rfft(frames * np.hanning(win), axis=1)) + 1e-9
        freqs = np.fft.rfftfreq(win, 1 / AUDIO_RATE)
        centroid.append(float((spec * freqs).sum(axis=1).mean() / spec.sum(axis=1).mean()))
        flatness.append(float((np.exp(np.log(spec).mean(axis=1)) / spec.mean(axis=1)).mean()))
        modulation.append(float(np.std(np.diff(db))) if n > 2 else 0.0)
    if not rms_db:
        raise RuntimeError("音声が短すぎて解析できませんでした")
    db = np.array(rms_db)
    return {
        "loudness_db": float(np.clip(db.mean(), -90, 0)),
        "loudness_var": float(db.std()),
        "silence_ratio": float((db < -45).mean()),
        "zcr": float(np.mean(zcr)),
        "spectral_centroid": float(np.mean(centroid)),
        "spectral_flatness": float(np.mean(flatness)),
        "modulation": float(np.mean(modulation)),
    }


def suggested_tags(meta: dict, features: dict) -> list[str]:
    """測定値から判断できるタグだけを候補にする（意味の推定はしない）。"""
    tags = []
    video, audio = features.get("video"), features.get("audio")
    if video and video["black_ratio"] >= 0.8:
        tags.append("黒画面")
    if video and video["static_ratio"] >= 0.7 and video["black_ratio"] < 0.8:
        tags.append("静止画が多い")
    if audio and audio["silence_ratio"] >= 0.5:
        tags.append("無音区間が多い")
    if meta.get("duration", 0) >= 2 * 3600:
        tags.append("長時間")
    return tags


def analyze_video(tools: FFmpegTools, path: str, thumb_prefix: Path, max_frames: int = 40,
                  cancel=None) -> tuple[str, str | None, dict, dict, list[str]]:
    """1本を解析する。戻り値: (解析状態, エラー内容, meta, features, サムネイル)"""
    before = file_state(path)
    meta = probe(tools, path, cancel)
    features: dict = {"video": None, "audio": None}
    errors = []
    thumbs: list[str] = []
    duration = meta["duration"]
    if meta["has_video"]:
        try:
            frames = extract_frames(tools, path, duration,
                                    frame_count_for(duration, min(max_frames, MAX_FRAMES)), cancel)
            features["video"] = frame_features(frames)
            thumbs = make_thumbnails(tools, path, duration, thumb_prefix, cancel)
        except (RuntimeError, TimeoutError) as e:
            errors.append(f"映像: {e}")
    else:
        errors.append("映像: 映像トラックがありません")
    if meta["has_audio"]:
        try:
            features["audio"] = audio_features(
                extract_audio_segments(tools, path, duration, cancel=cancel))
        except (RuntimeError, TimeoutError) as e:
            errors.append(f"音声: {e}")
    else:
        errors.append("音声: 音声トラックがありません（解析失敗ではありません）")
    ensure_unchanged(path, before)
    has_any = features["video"] is not None or features["audio"] is not None
    real_errors = [e for e in errors if "解析失敗ではありません" not in e]
    if not has_any:
        state = "失敗"
    elif real_errors:
        state = "一部失敗"
    else:
        state = "完了"
    return state, " / ".join(errors) or None, meta, features, thumbs


# --- ハッシュ ---------------------------------------------------------------
def sha256_file(path: str, cancel=None, on_bytes: Callable[[int], None] | None = None,
                chunk: int = 1024 * 1024) -> str:
    before = file_state(path)
    h = hashlib.sha256()
    with open(path, "rb") as f:  # 読み取り専用
        while True:
            if cancel is not None and cancel.is_set():
                raise Cancelled()
            data = f.read(chunk)
            if not data:
                break
            h.update(data)
            if on_bytes:
                on_bytes(len(data))
    ensure_unchanged(path, before)
    return h.hexdigest()


# --- 圧縮 -------------------------------------------------------------------
PRESETS = {
    "画質優先": {"crf": 20, "preset": "medium", "audio": "160k", "max_height": None},
    "バランス": {"crf": 24, "preset": "medium", "audio": "128k", "max_height": None},
    "容量優先": {"crf": 28, "preset": "medium", "audio": "96k", "max_height": 1080},
}


def compressed_output_path(source: str, out_dir: str, preset: str) -> str:
    stem = Path(source).stem
    base = Path(out_dir) / f"{stem}_compressed_{preset}.mp4"
    candidate, i = base, 2
    while candidate.exists() or Path(str(candidate) + ".part").exists():
        candidate = base.with_name(f"{base.stem}_{i}.mp4")
        i += 1
    return str(candidate)


def compress(tools: FFmpegTools, source: str, output: str, preset: str, duration: float,
             cancel: threading.Event, on_progress: Callable[[float | None], None]) -> dict:
    """source を output に圧縮する。元ファイルは読むだけ。

    一時ファイル（output + ".part"）に書き、検証に通ったら output に名前を変える。
    失敗・キャンセル時は一時ファイルだけを消す。
    """
    if os.path.normcase(os.path.abspath(source)) == os.path.normcase(os.path.abspath(output)):
        raise ValueError("出力先が元の動画と同じです")
    if os.path.exists(output):
        raise FileExistsError(f"出力ファイルが既に存在します: {output}")
    p = PRESETS[preset]
    temp = output + ".part"
    vf = ["-vf", f"scale=-2:'min({p['max_height']},ih)'"] if p["max_height"] else []
    args = [tools.ffmpeg, "-v", "error", "-n", "-i", source, *vf,
            "-c:v", "libx264", "-crf", str(p["crf"]), "-preset", p["preset"],
            "-c:a", "aac", "-b:a", p["audio"], "-movflags", "+faststart",
            "-progress", "pipe:1", "-nostats", "-f", "mp4", temp]
    before = file_state(source)
    proc = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            stdin=subprocess.DEVNULL, creationflags=_NO_WINDOW, text=True,
                            encoding="utf-8", errors="replace")
    err_lines: list[str] = []
    err_thread = threading.Thread(target=lambda: err_lines.extend(proc.stderr), daemon=True)
    err_thread.start()
    try:
        for line in proc.stdout:
            if cancel.is_set():
                proc.kill()
                break
            key, _, value = line.strip().partition("=")
            if key == "out_time_us" and duration > 0:
                try:
                    on_progress(min(int(value) / 1e6 / duration, 1.0))
                except ValueError:
                    pass
        proc.wait()
        err_thread.join(5)
        if cancel.is_set():
            raise Cancelled()
        if proc.returncode != 0:
            raise RuntimeError("圧縮に失敗しました: " + "".join(err_lines).strip()[-300:])
        ensure_unchanged(source, before)
        # 出力の検証: 動画として読めて、長さがほぼ同じであること
        out_meta = probe(tools, temp)
        if not out_meta["has_video"] or (duration > 0 and abs(out_meta["duration"] - duration) >
                                         max(2.0, duration * 0.02)):
            raise RuntimeError("圧縮結果の検証に失敗しました（長さが一致しません）")
        if os.path.exists(output):
            raise FileExistsError(f"出力ファイルが既に存在します: {output}")
        os.rename(temp, output)  # 既存ファイルがあれば Windows では失敗する（上書きしない）
    except BaseException:
        if proc.poll() is None:
            proc.kill()
            proc.wait()
        if os.path.exists(temp):
            os.remove(temp)  # アプリが作った一時ファイルだけを消す
        raise
    finally:
        err_thread.join(5)
        proc.stdout.close()
        proc.stderr.close()
    src_size, out_size = os.path.getsize(source), os.path.getsize(output)
    return {"source_size": src_size, "output_size": out_size, "output": output,
            "larger": out_size >= src_size}


COMPARE_POINTS = (0.2, 0.4, 0.6, 0.8)


def compare_frames(tools: FFmpegTools, path: str, duration: float, out_prefix: str, cancel=None) -> list[str]:
    """比較画面用に、同じ相対時点（長さの 20・40・60・80%）の場面を書き出す（アプリのキャッシュへ）。"""
    paths = []
    for i, r in enumerate(COMPARE_POINTS):
        out = f"{out_prefix}_{i}.jpg"
        if not os.path.exists(out):
            cp = _run([tools.ffmpeg, "-v", "error", "-y", "-ss", f"{duration * r:.3f}", "-i", path, "-frames:v", "1",
                       "-vf", "scale=320:-2", "-q:v", "4", out], cancel, timeout=60)
            if cp.returncode != 0 or not os.path.exists(out):
                paths.append("")
                continue
        paths.append(out)
    return paths

"""FFmpeg を使った動画の読み取り処理（解析・サムネイル・ハッシュ・音量の確認）。

安全上の方針:
- 元の動画は読み取るだけで、変更・上書き・削除しない
- 書き込むのはアプリのキャッシュ（サムネイル・比較用の場面）だけ
- FFmpeg は探すだけで、自動でインストールしない
- クラウド上のみのファイルは中身を読まない（勝手にダウンロードさせない）
- 読み取りの前後でファイル状態を比べ、途中で変わったら結果を使わない
"""

from __future__ import annotations

import hashlib
import json
import os
import re
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
    source: str = ""  # どこで見つけたか（設定画面の表示用）

    @property
    def available(self) -> bool:
        return bool(self.ffmpeg and self.ffprobe)


def _exe(name: str) -> str:
    return name + ".exe" if sys.platform == "win32" else name


def _pair_in(folder) -> tuple[str, str] | None:
    """同じフォルダに ffmpeg と ffprobe の両方があるときだけ返す（別々の版を組み合わせない）。"""
    if not folder:
        return None
    a, b = Path(folder) / _exe("ffmpeg"), Path(folder) / _exe("ffprobe")
    return (str(a), str(b)) if a.is_file() and b.is_file() else None


def winget_ffmpeg_dirs(local_appdata: str | None = None) -> list[str]:
    """WinGet で入れた FFmpeg の bin フォルダ候補（インストールはしない。探すだけ）。

    WinGet はコマンドの入口を Links に、本体を Packages\\<パッケージ名>\\...\\bin に置く。
    """
    base = local_appdata if local_appdata is not None else os.environ.get("LOCALAPPDATA", "")
    if not base:
        return []
    root = Path(base) / "Microsoft" / "WinGet"
    found = [str(root / "Links")]
    packages = root / "Packages"
    try:
        dirs = sorted((p for p in packages.iterdir() if p.is_dir() and "ffmpeg" in p.name.lower()),
                      key=lambda p: p.name)
    except OSError:
        dirs = []
    for pkg in dirs:
        try:  # 例: Gyan.FFmpeg_...\ffmpeg-7.1-full_build\bin\ffmpeg.exe
            found += sorted({str(p.parent) for p in pkg.glob("**/bin/" + _exe("ffmpeg"))}, reverse=True)
        except OSError:
            continue
    return found


def manual_ffmpeg_dirs(local_appdata: str | None = None) -> list[str]:
    """配布元の zip を %LOCALAPPDATA%\\Programs\\FFmpeg に展開した場合の bin フォルダ候補（探すだけ）。"""
    base = local_appdata if local_appdata is not None else os.environ.get("LOCALAPPDATA", "")
    root = Path(base) / "Programs" / "FFmpeg" if base else None
    if root is None or not root.is_dir():
        return []
    try:
        return sorted({str(p.parent) for p in root.glob("*/bin/" + _exe("ffmpeg"))}, reverse=True)
    except OSError:
        return []


def find_ffmpeg(custom_dir: str | None = None) -> FFmpegTools:
    """FFmpeg を探す: 設定のフォルダ → PATH → WinGet（Links・Packages）→ Programs\\FFmpeg。見つからなければ利用不可。"""
    if custom_dir:
        pair = _pair_in(custom_dir)
        if pair:
            return FFmpegTools(*pair, "設定で指定したフォルダ")
    ff, fp = shutil.which("ffmpeg"), shutil.which("ffprobe")
    if ff and fp:
        return FFmpegTools(ff, fp, "PATH")
    if sys.platform == "win32":
        for folder in winget_ffmpeg_dirs():
            pair = _pair_in(folder)
            if pair:
                return FFmpegTools(*pair, "WinGet でインストールされた FFmpeg")
        for folder in manual_ffmpeg_dirs():
            pair = _pair_in(folder)
            if pair:
                return FFmpegTools(*pair, "Programs\\FFmpeg に展開した FFmpeg")
    return FFmpegTools(None, None)


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


def run_at(tools: FFmpegTools, pre: list[str], t: float, path: str, post: list[str], ok, cancel=None,
           timeout: float = 60) -> subprocess.CompletedProcess:
    """t 秒の位置から ffmpeg を実行する。

    まず位置へ飛んでから読む（速い）。テレビ録画などの .ts では、これで1コマも取り出せないことがあるため、
    結果が ok(cp) を満たさなければ、先頭から読んで t 秒まで進める方法でやり直す（遅いが確実）。
    """
    cp = _run([tools.ffmpeg, *pre, "-ss", f"{t:.3f}", "-i", path, *post], cancel, timeout=timeout)
    if ok(cp):
        return cp
    return _run([tools.ffmpeg, *pre, "-i", path, "-ss", f"{t:.3f}", *post], cancel, timeout=timeout)


def _wrote(out: str):
    return lambda cp: cp.returncode == 0 and os.path.exists(out) and os.path.getsize(out) > 0


def extract_frames(tools: FFmpegTools, path: str, duration: float, count: int,
                   cancel=None) -> np.ndarray:
    frames = []
    size = FRAME_W * FRAME_H * 3
    for t in _sample_times(duration, count):
        if cancel is not None and cancel.is_set():
            raise Cancelled()
        cp = run_at(tools, ["-v", "error"], t, path,
                    ["-frames:v", "1", "-vf", f"scale={FRAME_W}:{FRAME_H}", "-pix_fmt", "rgb24", "-f", "rawvideo", "-"],
                    lambda c: c.returncode == 0 and len(c.stdout) == size, cancel)
        if cp.returncode == 0 and len(cp.stdout) == size:
            frames.append(np.frombuffer(cp.stdout, np.uint8).reshape(FRAME_H, FRAME_W, 3))
    if not frames:
        raise RuntimeError("映像フレームを取り出せませんでした")
    return np.stack(frames).astype(np.float32)


VOLUME_SEGMENT_SEC = 10.0
_MAX_VOLUME = re.compile(r"max_volume:\s*(-?(?:inf|[0-9.]+))\s*dB")


def parse_max_volume(stderr: str) -> float | None:
    """volumedetect の出力から最大音量（dB）を読む。読めなければ None。"""
    m = _MAX_VOLUME.search(stderr)
    if not m:
        return None
    return float("-inf") if m.group(1) == "-inf" else float(m.group(1))


def measure_max_volume(tools: FFmpegTools, path: str, duration: float, count: int = 3,
                       cancel=None) -> list[float]:
    """録画の中の数か所（各10秒）の最大音量を測る。全編は読み込まない。測れなかった区間は含めない。"""
    seg = min(VOLUME_SEGMENT_SEC, duration) if duration > 0 else VOLUME_SEGMENT_SEC
    values = []
    for t in _sample_times(max(duration - seg, 0.0), count if duration > seg * 2 else 1):
        if cancel is not None and cancel.is_set():
            raise Cancelled()
        cp = run_at(tools, ["-hide_banner", "-nostats"], t, path,
                    ["-t", f"{seg:.3f}", "-vn", "-af", "volumedetect", "-f", "null", "-"],
                    lambda c: c.returncode == 0 and parse_max_volume(c.stderr.decode("utf-8", "replace")) is not None,
                    cancel)
        v = parse_max_volume(cp.stderr.decode("utf-8", "replace")) if cp.returncode == 0 else None
        if v is not None:
            values.append(v)
    return values


def make_thumbnails(tools: FFmpegTools, path: str, duration: float, out_prefix: Path,
                    cancel=None) -> list[str]:
    """異なる場面（20%・50%・80%）のサムネイルをアプリのキャッシュに作る。"""
    paths = []
    times = [duration * r for r in (0.2, 0.5, 0.8)] if duration > 0 else [0.0]
    for i, t in enumerate(times):
        out = f"{out_prefix}_{i}.jpg"
        cp = run_at(tools, ["-v", "error", "-y"], t, path,
                    ["-frames:v", "1", "-vf", "scale=320:-2", "-q:v", "5", out], _wrote(out), cancel)
        if _wrote(out)(cp):
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


COMPARE_POINTS = (0.2, 0.4, 0.6, 0.8)


def compare_frames(tools: FFmpegTools, path: str, duration: float, out_prefix: str, cancel=None,
                   fractions=COMPARE_POINTS) -> list[str]:
    """比較画面用に、同じ相対時点の場面を書き出す（アプリのキャッシュへ）。"""
    paths = []
    for r in fractions:
        out = f"{out_prefix}_{int(round(r * 1000))}.jpg"
        if not os.path.exists(out):
            cp = run_at(tools, ["-v", "error", "-y"], duration * r, path,
                        ["-frames:v", "1", "-vf", "scale=320:-2", "-q:v", "4", out], _wrote(out), cancel)
            if not _wrote(out)(cp):
                paths.append("")
                continue
        paths.append(out)
    return paths

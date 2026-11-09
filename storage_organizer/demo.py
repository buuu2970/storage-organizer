"""授業の発表・動作確認用のデモ（python main.py --demo）。

- デモ用の録画を、ビデオ・デスクトップ・ダウンロード・ドキュメントの「RecSortデモ」フォルダに分けて作る
  （いろいろな場所から一度に探せることを見せるため）。映像と音声は FFmpeg の合成だけで作り、外部の素材は使わない
- デモのときは保存先を分け（RecSortDemo）、デモのフォルダだけを調べる。自分の録画やファイル名を画面に映さないため
- 片付け（--remove-demo）では、デモで作ったファイルだけを消す（作ったファイルの一覧を目印のファイルに記録しておく）
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
from pathlib import Path

from . import autoscan, media

DEMO_FOLDER = "RecSortデモ"
MARKER = ".recsort-demo.json"  # このフォルダはデモで作った、という目印（作ったファイルの一覧）
DEMO_DATA_DIR_NAME = "RecSortDemo"
DAY = 86400

# (置く場所, ファイル名, 映像, 音声 or None, 秒数, 何日前に更新されたことにするか, 何を見せるためのファイルか)
VIDEOS = [
    ("ビデオ", "ゲーム実況_ボス戦_第1回.mp4", "testsrc2=s=640x360:r=30", "anoisesrc=c=white:a=0.3", 12, 60, "元の録画（重複の残す側）"),
    ("ビデオ", "配信_真っ暗.mp4", "color=c=black:s=640x360:r=30", "sine=f=330,volume=0.4", 8, 20, "黒画面"),
    ("ビデオ", "録画ミス_2秒.mp4", "testsrc=s=640x360:r=30", "sine=f=500", 2, 5, "極端に短い"),
    ("デスクトップ", "授業の録画.mp4",
     "color=c=white:s=640x360:r=30,drawbox=x=40:y=40:w=560:h=20:c=black:t=fill,drawbox=x=40:y=100:w=400:h=12:c=gray:t=fill",
     "anoisesrc=c=pink:a=0.05", 10, 40, "似た録画の元"),
    ("デスクトップ", "音が入っていない録画.mp4", "mandelbrot=s=640x360:r=30", "anullsrc=r=44100:cl=stereo", 8, 10, "無音"),
    ("ドキュメント", "カメラ撮影.mp4", "life=s=640x360:mold=10:r=30:ratio=0.1:death_color=#C83232:life_color=#00ff00",
     "anoisesrc=c=brown:a=0.2", 10, 90, "普通の録画（おすすめに出ない）"),
    ("ドキュメント", "テレビ録画.ts", "testsrc2=s=640x360:r=30,hue=h=120", "sine=f=440", 8, 30,
     "ブラウザで再生できない形式（.ts）"),
]
# (置く場所, ファイル名, 元にする録画, 作り方, 何日前, 説明)
DERIVED = [
    ("デスクトップ", "ゲーム実況_ボス戦_第1回 (コピー).mp4", "ゲーム実況_ボス戦_第1回.mp4", "copy", 3, "完全に同じ録画（重複）"),
    ("ダウンロード", "ゲーム実況_ボス戦_第1回 (1).mp4", "ゲーム実況_ボス戦_第1回.mp4", "copy", 1, "完全に同じ録画（重複）"),
    ("ダウンロード", "ゲーム実況_ボス戦_第1回_低画質.mp4", "ゲーム実況_ボス戦_第1回.mp4", "lowres", 2, "似た録画（解像度違い）"),
    ("ドキュメント", "授業の録画_再圧縮.mp4", "授業の録画.mp4", "recompress", 4, "似た録画（再圧縮）"),
    ("ダウンロード", "壊れた録画.mp4", None, "broken", 7, "読み込めない録画"),
]


def demo_roots() -> dict[str, Path]:
    """表示名 → デモのフォルダ（ビデオ・デスクトップ・ダウンロード・ドキュメントの中）。"""
    places = dict(autoscan.known_folders())
    return {label: Path(places[label]) / DEMO_FOLDER for label in ("ビデオ", "デスクトップ", "ダウンロード", "ドキュメント")
            if label in places}


def demo_data_dir() -> Path:
    base = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local"))
    return base / DEMO_DATA_DIR_NAME


def exists() -> bool:
    return any((root / MARKER).is_file() for root in demo_roots().values())


def _ffmpeg(tools: media.FFmpegTools, args: list[str]) -> None:
    subprocess.run([tools.ffmpeg, "-v", "error", "-y", *args], check=True,
                   stdin=subprocess.DEVNULL, timeout=300)


def make(tools: media.FFmpegTools, report=print) -> list[Path]:
    """デモの録画を作る。すでにデモのフォルダがある場合は、作り直さずにそのまま使う。"""
    if not tools.available:
        raise RuntimeError("FFmpeg が見つかりません。デモの録画（再生できる動画）を作るには FFmpeg が必要です")
    roots = demo_roots()
    created: dict[str, list[str]] = {label: [] for label in roots}
    for root in roots.values():
        if root.exists() and not (root / MARKER).is_file():
            raise RuntimeError(f"{root} はデモで作ったフォルダではないため、使いません（名前を変えてください）")
        root.mkdir(parents=True, exist_ok=True)
    made: dict[str, Path] = {}
    now = time.time()

    def record(label: str, path: Path, days: int) -> None:
        os.utime(path, (now - days * DAY, now - days * DAY))
        created[label].append(path.name)
        made[path.name] = path

    for label, name, vsrc, asrc, sec, days, what in VIDEOS:
        if label not in roots:
            continue
        out = roots[label] / name
        report(f"作成中: {label}\\{DEMO_FOLDER}\\{name}（{what}）")
        args = ["-f", "lavfi", "-i", vsrc]
        if asrc:
            args += ["-f", "lavfi", "-i", asrc]
        args += ["-t", str(sec), "-c:v", "libx264", "-pix_fmt", "yuv420p", "-preset", "veryfast"]
        if asrc:
            args += ["-c:a", "aac", "-shortest"]
        if out.suffix == ".ts":
            args += ["-f", "mpegts"]
        _ffmpeg(tools, args + [str(out)])
        record(label, out, days)
    for label, name, source, how, days, what in DERIVED:
        if label not in roots or (source and source not in made):
            continue
        out = roots[label] / name
        report(f"作成中: {label}\\{DEMO_FOLDER}\\{name}（{what}）")
        if how == "copy":
            shutil.copyfile(made[source], out)
        elif how == "lowres":
            _ffmpeg(tools, ["-i", str(made[source]), "-vf", "scale=256:144", "-c:v", "libx264", "-crf", "32",
                            "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "64k", str(out)])
        elif how == "recompress":
            _ffmpeg(tools, ["-i", str(made[source]), "-c:v", "libx264", "-crf", "40",
                            "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "48k", str(out)])
        elif how == "broken":
            out.write_bytes(b"\x00\x00\x00\x18ftypmp42" + b"this is not a real video " * 2000)
        record(label, out, days)
    for label, root in roots.items():
        (root / MARKER).write_text(json.dumps({"files": created[label]}, ensure_ascii=False), encoding="utf-8")
    return list(made.values())


def remove(report=print) -> int:
    """デモで作ったファイルだけを消す（目印の一覧にないファイルは残す）。消した数を返す。"""
    count = 0
    for root in demo_roots().values():
        marker = root / MARKER
        if not marker.is_file():
            continue
        try:
            names = json.loads(marker.read_text(encoding="utf-8")).get("files", [])
        except (OSError, ValueError):
            names = []
        for name in names:
            path = root / name
            if path.is_file() and path.parent == root:
                path.unlink()
                count += 1
        marker.unlink()
        try:
            root.rmdir()  # ほかのファイルが残っていれば消さない
        except OSError:
            report(f"{root} にはデモ以外のファイルがあるため、フォルダは残しました")
    return count

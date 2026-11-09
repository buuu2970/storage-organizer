"""授業の発表・動作確認用のデモ（python main.py --demo）。

- デモ用の録画を、ビデオ・デスクトップ・ダウンロード・ドキュメントの「RecSortデモ」フォルダに分けて作る
  （いろいろな場所から一度に探せることを見せるため）
- 元にする動画: ダウンロードの「RecSort素材」に、学校生活の動画（ChatGPT などで作ったもの。ファイル名は SOURCES）が
  あればそれを使う。なければ FFmpeg の合成映像だけで作る。重複のコピー・低画質版・再圧縮版・短い録画・無音・暗い録画・
  壊れた録画は、どちらの場合もこのプログラムが作る（意図どおりの「整理候補」を確実に見せるため）
- デモのときは保存先を分ける（RecSortDemo。ふだんの判断・保護の記録と混ぜない）。調べる範囲はふだんと同じ PC 全体
  （素材のフォルダだけは外す）。--demo-only ではデモのフォルダだけを調べる（自分の録画やファイル名を映したくないとき）
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
    ("Cドライブ", "ゲーム実況_ボス戦_第1回 (2).mp4", "ゲーム実況_ボス戦_第1回.mp4", "copy", 6, "完全に同じ録画（ドライブの直下）"),
    ("別のドライブ", "カメラ撮影 (バックアップ).mp4", "カメラ撮影.mp4", "copy", 8, "完全に同じ録画（別のドライブ）"),
]


SOURCE_FOLDER = "RecSort素材"
# 素材（ダウンロードの RecSort素材 の中のファイル名）→ (置く場所, デモでの名前, 何日前, 作り方, 何を見せるか)
# 作り方: keep（そのまま）/ short（2秒に切る）/ silent（音を無音にする）/ dark（暗くする）/ ts（.ts に変える）
SOURCES = [
    ("math_class.mp4", "ビデオ", "数学の授業_第3回.mp4", 60, "keep", "元の録画（重複の残す側）"),
    ("online_lecture.mp4", "デスクトップ", "オンライン授業.mp4", 40, "keep", "似た録画の元"),
    ("soccer_practice.mp4", "ビデオ", "サッカー部_パス練習.mp4", 25, "keep", "普通の録画"),
    ("night_school.mp4", "ビデオ", "夜の校舎.mp4", 20, "dark", "黒画面"),
    ("presentation_mistake.mp4", "ビデオ", "発表練習_録画ミス.mp4", 5, "short", "極端に短い"),
    ("science_lab.mp4", "ドキュメント", "理科の実験.mp4", 35, "keep", "普通の録画"),
    ("programming_class.mp4", "ドキュメント", "プログラミングの授業.mp4", 15, "keep", "普通の録画"),
    ("english_listening.mp4", "ビデオ", "英語リスニング教材.mp4", 50, "keep", "普通の録画"),
    ("brass_band.mp4", "ダウンロード", "吹奏楽部_練習.mp4", 12, "keep", "普通の録画"),
    ("school_festival.mp4", "ドキュメント", "文化祭の準備.ts", 30, "ts", "ブラウザで再生できない形式（.ts）"),
    ("sports_day.mp4", "デスクトップ", "体育祭_リレー.mp4", 45, "keep", "普通の録画"),
    ("library_timelapse.mp4", "デスクトップ", "図書室_タイムラプス.mp4", 10, "silent", "無音"),
]
# (置く場所, デモでの名前, 元にする素材, 作り方, 何日前, 何を見せるか)。元の素材がなければ作らない
SOURCE_DERIVED = [
    ("デスクトップ", "数学の授業_第3回 (コピー).mp4", "math_class.mp4", "copy", 3, "完全に同じ録画（重複）"),
    ("ダウンロード", "数学の授業_第3回 (1).mp4", "math_class.mp4", "copy", 1, "完全に同じ録画（重複）"),
    ("ダウンロード", "数学の授業_第3回_低画質.mp4", "math_class.mp4", "lowres", 2, "似た録画（解像度違い）"),
    ("ドキュメント", "オンライン授業_再圧縮.mp4", "online_lecture.mp4", "recompress", 4, "似た録画（再圧縮）"),
    ("ダウンロード", "壊れた録画.mp4", None, "broken", 7, "読み込めない録画"),
    ("Cドライブ", "数学の授業_第3回 (2).mp4", "math_class.mp4", "copy", 6, "完全に同じ録画（ドライブの直下）"),
    ("別のドライブ", "サッカー部_パス練習 (バックアップ).mp4", "soccer_practice.mp4", "copy", 8,
     "完全に同じ録画（別のドライブ）"),
]
REQUIRED_SOURCE = "math_class.mp4"  # これがあれば素材を使う（重複・似た録画の元になる）
QUIET_AMBIENCE = "anoisesrc=c=pink:a=0.03"  # 素材に音がないときに足す、小さな環境音（無音と判定されない大きさ）


def source_dir() -> Path | None:
    places = dict(autoscan.known_folders())
    return Path(places["ダウンロード"]) / SOURCE_FOLDER if "ダウンロード" in places else None


def has_sources() -> bool:
    d = source_dir()
    return d is not None and (d / REQUIRED_SOURCE).is_file()


def _drive_roots() -> dict[str, Path]:
    """Windows のドライブの直下（Cドライブ）と、別の内蔵ドライブ（あれば）のデモのフォルダ。

    「ドライブのどこにあっても見つかる」ことを見せるため。書き込めない場合（学校の PC など）は make で飛ばす。
    """
    system = os.path.normcase(os.environ.get("SystemDrive", "C:") + os.sep)
    internal = [d["root"] for d in autoscan.local_drives() if d["kind"] == "internal"]
    roots = {}
    for root in internal:
        if os.path.normcase(root) == system:
            roots["Cドライブ"] = Path(root) / DEMO_FOLDER
    others = [r for r in internal if os.path.normcase(r) != system]
    if others:
        roots["別のドライブ"] = Path(others[0]) / DEMO_FOLDER
    return roots


def demo_roots() -> dict[str, Path]:
    """表示名 → デモのフォルダ（ビデオ・デスクトップ・ダウンロード・ドキュメントの中と、ドライブの直下）。"""
    places = dict(autoscan.known_folders())
    roots = {label: Path(places[label]) / DEMO_FOLDER for label in ("ビデオ", "デスクトップ", "ダウンロード", "ドキュメント")
             if label in places}
    roots.update(_drive_roots())
    return roots


def demo_data_dir() -> Path:
    base = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local"))
    return base / DEMO_DATA_DIR_NAME


def exists() -> bool:
    return any((root / MARKER).is_file() for root in demo_roots().values())


def _ffmpeg(tools: media.FFmpegTools, args: list[str]) -> None:
    subprocess.run([tools.ffmpeg, "-v", "error", "-y", *args], check=True,
                   stdin=subprocess.DEVNULL, timeout=300)


def make(tools: media.FFmpegTools, report=print) -> list[Path]:
    """デモの録画を作る。素材（ダウンロードの RecSort素材）があれば使い、なければ合成映像で作る。"""
    if not tools.available:
        raise RuntimeError("FFmpeg が見つかりません。デモの録画（再生できる動画）を作るには FFmpeg が必要です")
    roots = demo_roots()
    created: dict[str, list[str]] = {label: [] for label in roots}
    for root in roots.values():
        if root.exists() and not (root / MARKER).is_file():
            raise RuntimeError(f"{root} はデモで作ったフォルダではないため、使いません（名前を変えてください）")
    for label, root in list(roots.items()):
        try:
            root.mkdir(parents=True, exist_ok=True)
        except OSError:  # 学校の PC などで、ドライブの直下に書き込めない場合は、その場所を使わない
            report(f"{root} に書き込めないため、ここには置きません")
            del roots[label]
            created.pop(label, None)
    made: dict[str, Path] = {}
    now = time.time()

    def record(label: str, path: Path, days: int, key: str | None = None) -> None:
        os.utime(path, (now - days * DAY, now - days * DAY))
        created[label].append(path.name)
        made[key or path.name] = path

    use_sources = has_sources()
    try:
        if use_sources:
            _make_from_sources(tools, roots, record, report)
        else:
            _make_synthetic(tools, roots, record, report)
        _derive(tools, roots, record, made, report, SOURCE_DERIVED if use_sources else DERIVED)
    finally:  # 途中で失敗しても、作った分は --remove-demo で消せるように目印を残す
        for label, root in roots.items():
            (root / MARKER).write_text(json.dumps({"files": created[label]}, ensure_ascii=False), encoding="utf-8")
    return list(dict.fromkeys(made.values()))


def _report_name(label: str, name: str) -> str:
    return label + "\\" + DEMO_FOLDER + "\\" + name


def _make_synthetic(tools, roots, record, report) -> None:
    for label, name, vsrc, asrc, sec, days, what in VIDEOS:
        if label not in roots:
            continue
        out = roots[label] / name
        report(f"作成中: {_report_name(label, name)}（{what}）")
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


def _is_dark(tools, path: str) -> bool:
    info = media.probe(tools, path)
    feat = media.frame_features(media.extract_frames(tools, path, info.get("duration", 0), 3))
    return feat["black_ratio"] >= 0.99 or (feat["frames"] >= 3 and feat["brightness"] < 0.04)


def _make_from_sources(tools, roots, record, report) -> None:
    src_dir = source_dir()
    for src_name, label, name, days, how, what in SOURCES:
        src = src_dir / src_name
        if label not in roots or not src.is_file():
            continue  # 作れなかった素材は飛ばす（数学の授業だけは必須）
        out = roots[label] / name
        report(f"作成中: {_report_name(label, name)}（{what}・素材 {src_name}）")
        has_audio = media.probe(tools, str(src)).get("has_audio")
        # 音のない素材には小さな環境音を足す（無音を見せる録画以外が「無音」にならないように）
        audio_in = [] if has_audio else ["-f", "lavfi", "-i", QUIET_AMBIENCE]
        audio_map = ["-map", "0:v:0", "-map", "0:a:0" if has_audio else "1:a:0"]
        video = ["-c:v", "libx264", "-pix_fmt", "yuv420p", "-preset", "veryfast"]
        copy_with_audio = ["-i", str(src), *audio_in, *audio_map, "-c:v", "copy", "-c:a", "aac", "-shortest", str(out)]
        if how in ("keep", "dark"):
            if has_audio:
                shutil.copyfile(src, out)
            else:
                _ffmpeg(tools, copy_with_audio)
        elif how == "short":
            _ffmpeg(tools, ["-i", str(src), *audio_in, *audio_map, "-t", "2", *video, "-c:a", "aac", str(out)])
        elif how == "silent":  # 音声トラックはあるが、音が入っていない録画
            _ffmpeg(tools, ["-i", str(src), "-f", "lavfi", "-i", "anullsrc=r=44100:cl=stereo",
                            "-map", "0:v:0", "-map", "1:a:0", "-c:v", "copy", "-c:a", "aac", "-shortest", str(out)])
        elif how == "ts":
            _ffmpeg(tools, ["-i", str(src), *audio_in, *audio_map, *video, "-c:a", "aac", "-shortest",
                            "-f", "mpegts", str(out)])
        if how == "dark":  # 暗い夜の映像でも「ほぼ真っ暗」と判定されるとは限らないため、必要なら暗くする
            # 弱い順に試す（もとが夜の映像なら、少し暗くするだけで足りる）。最後は明るさを 6% に落とす
            for vf in ("eq=brightness=-0.5", "eq=brightness=-0.8", "lutyuv=y=val*0.06:u=128:v=128"):
                if _is_dark(tools, str(out)):
                    break
                tmp = out.with_name("tmp_" + out.name)
                _ffmpeg(tools, ["-i", str(src), *audio_in, *audio_map, "-vf", vf, *video,
                                "-c:a", "aac", "-shortest", str(tmp)])
                os.replace(tmp, out)
        record(label, out, days, key=src_name)


def _derive(tools, roots, record, made, report, plan) -> None:
    for label, name, source, how, days, what in plan:
        if label not in roots or (source and source not in made):
            continue
        out = roots[label] / name
        report(f"作成中: {_report_name(label, name)}（{what}）")
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

"""コマンドライン版: 動画を解析して CSV レポート出力 / 理由別フォルダへ仕分け

例:
  python -m storage_organizer.cli "D:\\録画" -r
  python -m storage_organizer.cli "D:\\録画" --action copy
"""

from __future__ import annotations

import argparse
import csv
import shutil
import sys
from datetime import datetime
from pathlib import Path

from . import ffmpeg
from .files import find_duplicates, find_files
from .video_analyzer import (CAT_AUDIO, CAT_DUP, CAT_ERROR, CAT_LONG, CAT_SHORT, CAT_VIDEO, VIDEO_EXTS, Thresholds,
                             VideoReport, analyse, fmt_time)
from .video_cleanup import priority

PRIORITY_LABEL = {3: "高", 2: "中", 1: "低", 0: ""}


def build_parser() -> argparse.ArgumentParser:
    d = Thresholds()
    ap = argparse.ArgumentParser(prog="storage-organizer-cli", description="録画ファイル選別 (レポート/仕分け)",
                                 formatter_class=argparse.ArgumentDefaultsHelpFormatter)
    ap.add_argument("folder", type=Path, help="調べるフォルダ")
    ap.add_argument("-r", "--recursive", action="store_true", help="サブフォルダも調べる")
    ap.add_argument("--action", choices=["report", "copy", "move"], default="report",
                    help="report=レポートのみ / copy=理由別フォルダにコピー / move=移動")
    ap.add_argument("--out", type=Path, help="出力先フォルダ (既定: <folder>/選別結果)")
    g = ap.add_argument_group("長さ")
    g.add_argument("--min-sec", type=float, default=d.min_sec, help="これ未満は「短すぎる」(秒)")
    g.add_argument("--max-sec", type=float, default=d.max_sec, help="これ以上は「長すぎる」(秒)")
    g = ap.add_argument_group("画質")
    g.add_argument("--min-height", type=int, default=d.min_height, help="短辺がこれ未満は低解像度 (px)")
    g.add_argument("--min-fps", type=float, default=d.min_fps, help="これ未満は低フレームレート")
    g.add_argument("--blur-threshold", type=float, default=d.blur_threshold, help="ぼやけ値がこれを超えるとぼやけ判定")
    g.add_argument("--no-blur", action="store_true", help="ぼやけ検出をしない (高速化)")
    g = ap.add_argument_group("音声")
    g.add_argument("--min-mean-db", type=float, default=d.min_mean_db, help="平均音量がこれ未満は小音量 (dB)")
    g.add_argument("--silence-db", type=float, default=d.silence_db, help="これ以下の音量を無音とみなす (dB)")
    g.add_argument("--max-silence", type=float, default=d.max_silence, help="無音率がこれを超えると「ほぼ無音」")
    g.add_argument("--max-clip", type=float, default=d.max_clip, help="クリップ率がこれを超えると「音割れ」")
    g.add_argument("--min-sample-rate", type=int, default=d.min_sample_rate, help="これ未満は低音質 (Hz)")
    g.add_argument("--min-audio-kbps", type=float, default=d.min_audio_kbps, help="これ未満は低音質 (kbps)")
    g = ap.add_argument_group("解析")
    g.add_argument("--samples", type=int, default=d.samples, help="1本あたりの解析サンプル箇所数")
    g.add_argument("--seg-len", type=float, default=d.seg_len, help="1サンプルあたりの音声解析秒数")
    return ap


def thresholds_from_args(a: argparse.Namespace) -> Thresholds:
    return Thresholds(
        min_sec=a.min_sec, max_sec=a.max_sec, min_height=a.min_height, min_fps=a.min_fps,
        blur_threshold=a.blur_threshold, check_blur=not a.no_blur, min_mean_db=a.min_mean_db,
        silence_db=a.silence_db, max_silence=a.max_silence, max_clip=a.max_clip,
        min_sample_rate=a.min_sample_rate, min_audio_kbps=a.min_audio_kbps, samples=a.samples, seg_len=a.seg_len,
    )


def _opt(v, f: str) -> str:
    return "" if v is None else format(v, f)


def write_csv(reports: list[VideoReport], out: Path) -> None:
    with out.open("w", newline="", encoding="utf-8-sig") as fp:  # Excelで文字化けしないようBOM付き
        w = csv.writer(fp)
        w.writerow(["判定", "削除推奨度", "ファイル", "カテゴリ", "理由", "長さ", "解像度", "fps", "ぼやけ値",
                    "音声", "平均音量dB", "最大音量dB", "無音率", "クリップ率", "サンプリングHz", "音声kbps"])
        for r in sorted(reports, key=lambda x: (-priority(x.categories), str(x.path))):
            w.writerow([
                "要確認" if r.categories else "OK", PRIORITY_LABEL[priority(r.categories)], str(r.path),
                " / ".join(r.categories), " / ".join(r.reasons), fmt_time(r.duration),
                f"{r.width}x{r.height}" if r.width else "", _opt(r.fps, ".1f"), _opt(r.blur, ".1f"),
                "あり" if r.has_audio else "なし", _opt(r.mean_db, ".1f"), _opt(r.max_db, ".1f"),
                _opt(r.silence_ratio, ".0%"), _opt(r.clip_ratio, ".3%"), r.sample_rate or "", _opt(r.audio_kbps, ".0f"),
            ])


def sort_into_folders(reports: list[VideoReport], dest_root: Path, action: str) -> None:
    """引っかかったファイルを理由別フォルダへ。複数理由は「A+B」フォルダにまとめる。"""
    for r in reports:
        if not r.categories:
            continue
        dest_dir = dest_root / "+".join(r.categories)
        dest_dir.mkdir(parents=True, exist_ok=True)
        dest = dest_dir / r.path.name
        n = 1
        while dest.exists():
            dest = dest_dir / f"{r.path.stem}_{n}{r.path.suffix}"
            n += 1
        (shutil.move if action == "move" else shutil.copy2)(str(r.path), str(dest))
        print(f"  {'移動' if action == 'move' else 'コピー'}: {r.path.name} → {dest_dir.name}/")


def main(argv: list[str] | None = None) -> None:
    a = build_parser().parse_args(argv)
    if not a.folder.is_dir():
        sys.exit(f"[エラー] フォルダが見つかりません: {a.folder}")
    try:
        ffmpeg.ffmpeg(), ffmpeg.ffprobe()
    except ffmpeg.FFmpegNotFound as e:
        sys.exit(f"[エラー] {e}")

    th = thresholds_from_args(a)
    out_dir = a.out or a.folder / "選別結果"
    files = find_files([a.folder], VIDEO_EXTS, recursive=a.recursive, exclude=out_dir)
    if not files:
        sys.exit("動画ファイルが見つかりませんでした。")

    print(f"{len(files)} 本の動画を解析します…\n")
    dups = find_duplicates(files)
    reports = []
    for i, f in enumerate(files, 1):
        print(f"[{i}/{len(files)}] {f.name}", end=" … ", flush=True)
        r = analyse(f, th)
        if f in dups:
            r.flag(CAT_DUP, f"重複 ({dups[f]} と同じ内容)")
        reports.append(r)
        print("要確認: " + " / ".join(r.reasons) if r.categories else "OK")

    out_dir.mkdir(parents=True, exist_ok=True)
    report = out_dir / f"選別レポート_{datetime.now():%Y%m%d_%H%M%S}.csv"
    write_csv(reports, report)

    flagged = [r for r in reports if r.categories]
    print(f"\n===== 結果: {len(files)} 本中 {len(flagged)} 本が要確認 =====")
    for cat in (CAT_SHORT, CAT_LONG, CAT_DUP, CAT_VIDEO, CAT_AUDIO, CAT_ERROR):
        n = sum(cat in r.categories for r in reports)
        if n:
            print(f"  {cat}: {n} 本")
    print(f"レポート: {report}")

    if a.action != "report" and flagged:
        print()
        sort_into_folders(flagged, out_dir, a.action)


if __name__ == "__main__":
    main()

"""動作確認用の短いサンプル動画を FFmpeg の合成映像・合成音声だけで作る。

使い方: python tools/make_sample_videos.py [作成先フォルダ]
（省略時はカレントフォルダに sample_videos を作る。既にある場合は中止）
外部素材を使わないので、学校での共有・テストに使える。
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

# (相対パス, 映像ソース, 音声ソース or None, 秒数, 追加オプション)
SAMPLES = [
    ("ゲーム/game_01.mp4", "testsrc2=s=320x180:r=30", "anoisesrc=c=white:a=0.3", 8, []),
    ("ゲーム/game_02.mp4", "testsrc2=s=320x180:r=30,hue=h=90", "anoisesrc=c=white:a=0.25", 10, []),
    ("歌/song_01.mp4", "color=c=black:s=320x180:r=30",
     "sine=f=440,tremolo=f=5:d=0.7,volume=0.8", 8, []),
    ("歌/song_02.mp4", "color=c=black:s=320x180:r=30",
     "sine=f=523,tremolo=f=4:d=0.6,volume=0.7", 12, []),
    ("授業/lecture_01.mp4", "color=c=white:s=320x180:r=30,drawbox=x=20:y=20:w=280:h=10:c=black:t=fill",
     "anoisesrc=c=pink:a=0.05,tremolo=f=3:d=0.9", 10, []),
    ("授業/lecture_02.mp4", "color=c=0xF0F0F0:s=320x180:r=30,drawbox=x=30:y=40:w=200:h=8:c=gray:t=fill",
     "anoisesrc=c=pink:a=0.06,tremolo=f=3.5:d=0.9", 9, []),
    ("撮影/camera_01.mp4", "mandelbrot=s=320x180:r=30", "anoisesrc=c=brown:a=0.2", 8, []),
    ("その他/silent_gray.mp4", "color=c=gray:s=320x180:r=30", None, 6, []),
    ("その他/old_format.avi", "testsrc=s=320x180:r=30", "sine=f=300", 5,
     ["-c:v", "mjpeg", "-q:v", "2"]),
]


def create(target: Path, ffmpeg: str) -> int:
    for rel, vsrc, asrc, sec, extra in SAMPLES:
        out = target / rel
        out.parent.mkdir(parents=True, exist_ok=True)
        args = [ffmpeg, "-v", "error", "-f", "lavfi", "-i", vsrc]
        if asrc:
            args += ["-f", "lavfi", "-i", asrc]
        args += ["-t", str(sec)]
        args += extra or ["-c:v", "libx264", "-pix_fmt", "yuv420p", "-preset", "veryfast"]
        if asrc:
            args += ["-c:a", "aac" if out.suffix == ".mp4" else "pcm_s16le"]
        subprocess.run(args + [str(out)], check=True)
    # 完全一致の重複（コピー）と、解析できない壊れたファイル
    shutil.copyfile(target / "ゲーム/game_01.mp4", target / "その他/game_01_copy.mp4")
    (target / "その他/broken.mp4").write_bytes(b"this is not a video" * 100)
    return len(SAMPLES) + 2


def main() -> int:
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        print("FFmpeg が見つかりません。README の手順で導入してください。")
        return 1
    target = Path(sys.argv[1]) if len(sys.argv) > 1 else Path.cwd() / "sample_videos"
    if target.exists():
        print(f"中止しました: {target} はすでに存在します。")
        return 1
    count = create(target, ffmpeg)
    print(f"作成しました: {target}（{count} 本）")
    return 0


if __name__ == "__main__":
    sys.exit(main())

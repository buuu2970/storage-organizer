"""FFmpeg を配布元（gyan.dev）から取ってきて、%LOCALAPPDATA%\\Programs\\FFmpeg に展開する（管理者の権限は不要）。

デモを開く.bat から、利用者が「入れる」と答えたときだけ呼ばれる。RecSort 本体は FFmpeg を自動でインストールしない。
- 配布元が公開している SHA-256 と一致しなければ、展開せずに中止する
- すでに FFmpeg が見つかる場合は何もしない
"""

from __future__ import annotations

import hashlib
import os
import sys
import tempfile
import urllib.request
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from storage_organizer import media  # noqa: E402

URL = "https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-essentials.zip"


def target_dir() -> Path:
    base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
    return Path(base) / "Programs" / "FFmpeg"


def main() -> int:
    if media.find_ffmpeg().available:
        print("FFmpeg はすでに入っています。")
        return 0
    print("FFmpeg をダウンロードしています（約 110MB。数分かかります）...")
    with tempfile.TemporaryDirectory() as tmp:
        zip_path = Path(tmp) / "ffmpeg.zip"
        try:
            urllib.request.urlretrieve(URL, zip_path)
            expected = urllib.request.urlopen(URL + ".sha256", timeout=60).read().decode().split()[0].lower()
        except OSError as e:
            print(f"ダウンロードできませんでした: {e}")
            return 1
        h = hashlib.sha256()
        with open(zip_path, "rb") as f:
            for chunk in iter(lambda: f.read(1024 * 1024), b""):
                h.update(chunk)
        if h.hexdigest() != expected:
            print("ダウンロードしたファイルが配布元の内容と一致しないため、中止しました。")
            return 1
        print("展開しています...")
        with zipfile.ZipFile(zip_path) as z:
            z.extractall(target_dir())
    if not media.find_ffmpeg().available:
        print("FFmpeg を展開しましたが、見つけられませんでした。")
        return 1
    print(f"FFmpeg を入れました: {target_dir()}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

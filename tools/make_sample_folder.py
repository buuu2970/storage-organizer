"""動作確認用の小さなサンプルフォルダを作る。

使い方: python tools/make_sample_folder.py [作成先フォルダ]
（省略時はカレントフォルダに sample_scan_folder を作る）

指定したフォルダの中に新しいファイルを作るだけで、既存のファイルには触れない。
作成先がすでに存在する場合は中止する。合計は約 13.5 MB。
"""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path

DAY = 86400

# (相対パス, サイズ(バイト), 何日前に更新されたことにするか)
SAMPLE_FILES = [
    ("録画/ゲーム録画_2024-03-01.mp4", 8 * 1024 * 1024, 600),
    ("録画/授業メモ動画.mkv", 2 * 1024 * 1024, 30),
    ("写真/旅行/IMG_0001.jpg", 600 * 1024, 400),
    ("写真/旅行/IMG_0002.jpg", 650 * 1024, 400),
    ("写真/screenshot.png", 300 * 1024, 2),
    ("ダウンロード/setup_tool.exe", 1500 * 1024, 200),
    ("ダウンロード/資料.zip", 400 * 1024, 90),
    ("ダウンロード/課題.pdf", 120 * 1024, 10),
    ("文書/レポート.docx", 45 * 1024, 5),
    ("文書/メモ.txt", 2 * 1024, 1),
    ("その他/データ.dat", 10 * 1024, 700),
    ("その他/空のファイル", 0, 0),
]


def main() -> int:
    target = Path(sys.argv[1]) if len(sys.argv) > 1 else Path.cwd() / "sample_scan_folder"
    if target.exists():
        print(f"中止しました: {target} はすでに存在します。別の場所を指定してください。")
        return 1

    now = time.time()
    for rel, size, days_ago in SAMPLE_FILES:
        path = target / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "wb") as f:
            f.write(b"\0" * size)
        ts = now - days_ago * DAY
        os.utime(path, (ts, ts))
    (target / "空のフォルダ").mkdir()

    total = sum(size for _, size, _ in SAMPLE_FILES)
    print(f"作成しました: {target}")
    print(f"ファイル {len(SAMPLE_FILES)} 件 / 合計 {total:,} バイト")
    return 0


if __name__ == "__main__":
    sys.exit(main())

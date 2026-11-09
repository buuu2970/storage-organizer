"""デスクトップに RecSort のショートカット（RecSort・RecSort デモ）を作る。

tools/launch.bat が、はじめての起動（部品を入れたとき）に呼ぶ。フォルダを移した場合も、次の起動で作り直す。
使い方: python tools/make_shortcuts.py [作る場所（省略時はデスクトップ）]
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

SHORTCUTS = [  # (ショートカットの名前, 起動するファイル, 説明)
    ("RecSort", "RecSortを開く.bat", "RecSort を開く（パソコンの録画を整理する）"),
    ("RecSort デモ", "デモを開く.bat", "RecSort のデモを開く（授業の発表用）"),
]


def desktop() -> Path:
    from storage_organizer import autoscan
    places = dict(autoscan.known_folders())  # OneDrive に移したデスクトップにも対応する
    return Path(places.get("デスクトップ") or Path.home() / "Desktop")


def make(dest: Path) -> list[Path]:
    import win32com.client
    shell = win32com.client.Dispatch("WScript.Shell")
    made = []
    for name, target, desc in SHORTCUTS:
        bat = ROOT / target
        if not bat.is_file():
            continue
        link = dest / f"{name}.lnk"
        sc = shell.CreateShortCut(str(link))
        sc.TargetPath = str(bat)
        sc.WorkingDirectory = str(ROOT)
        sc.Description = desc
        sc.IconLocation = os.path.join(os.environ.get("SystemRoot", r"C:\Windows"), "System32", "imageres.dll") + ",18"
        sc.save()
        made.append(link)
    return made


def main() -> int:
    dest = Path(sys.argv[1]) if len(sys.argv) > 1 else desktop()
    try:
        made = make(dest)
    except Exception as e:  # noqa: BLE001  ショートカットが作れなくても、アプリの起動は続ける
        print(f"デスクトップにショートカットを作れませんでした: {e}")
        return 0
    if made:
        print("デスクトップにショートカットを作りました: " + "、".join(p.stem for p in made))
        print("次からは、デスクトップの「RecSort」をダブルクリックするだけで開けます。")
    return 0


if __name__ == "__main__":
    sys.exit(main())

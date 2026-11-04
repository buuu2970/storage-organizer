"""アプリの起動処理。"""

from __future__ import annotations

import sys

from PySide6.QtCore import QLibraryInfo, QLocale, QTranslator
from PySide6.QtWidgets import QApplication

from . import APP_NAME, __version__, theme
from .main_window import MainWindow


def install_japanese(app: QApplication) -> None:
    """Qt の標準ボタン（はい・いいえ・閉じる など）を日本語にする。"""
    translator = QTranslator(app)
    if translator.load(QLocale(QLocale.Language.Japanese), "qtbase", "_",
                       QLibraryInfo.path(QLibraryInfo.LibraryPath.TranslationsPath)):
        app.installTranslator(translator)


def _scrub(text: str) -> str:
    """ログに個人のフォルダ名を残さないよう、ホームフォルダのパスを伏せる。"""
    import os
    for home in {os.path.expanduser("~"), os.environ.get("USERPROFILE", "")}:
        if home and len(home) > 3:
            text = text.replace(home, "%USERPROFILE%").replace(home.replace("\\", "/"), "%USERPROFILE%")
    return text


def install_error_log(data_dir) -> None:
    """想定外のエラーを、アプリのデータ保存先の logs に記録して知らせる。

    録画の内容は記録しない。パスのうちホームフォルダは伏せる。ログは 1MB を超えたら作り直す。
    """
    import traceback
    from pathlib import Path
    log_dir = Path(data_dir) / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    log_file = log_dir / "recsort-error.log"
    shown = {"done": False}

    def hook(exc_type, exc, tb):
        text = _scrub("".join(traceback.format_exception(exc_type, exc, tb)))
        try:
            if log_file.exists() and log_file.stat().st_size > 1024 * 1024:
                log_file.unlink()
            with open(log_file, "a", encoding="utf-8") as f:
                import time
                f.write(f"--- {time.strftime('%Y-%m-%d %H:%M:%S')} RecSort {__version__}\n{text}\n")
        except OSError:
            pass
        if not shown["done"] and QApplication.instance() is not None:
            shown["done"] = True
            from PySide6.QtWidgets import QMessageBox
            QMessageBox.warning(None, "RecSort", "予期しないエラーが発生しました。操作を続けられない場合はアプリを再起動してください。\n"
                                f"問題を報告するときは、次のファイルを添付してください:\n{log_file}")
        sys.__excepthook__(exc_type, exc, tb)
    sys.excepthook = hook


def main() -> int:
    app = QApplication(sys.argv)
    app.setApplicationName(APP_NAME)
    app.setApplicationVersion(__version__)
    from .store import default_data_dir
    install_error_log(default_data_dir())
    install_japanese(app)
    theme.apply(app)
    window = MainWindow()
    window.show()
    return app.exec()

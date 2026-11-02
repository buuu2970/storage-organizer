"""アプリの起動処理。"""

from __future__ import annotations

import sys

from PySide6.QtCore import QLibraryInfo, QLocale, QTranslator
from PySide6.QtWidgets import QApplication

from . import theme
from .main_window import MainWindow


def install_japanese(app: QApplication) -> None:
    """Qt の標準ボタン（はい・いいえ・閉じる など）を日本語にする。"""
    translator = QTranslator(app)
    if translator.load(QLocale(QLocale.Language.Japanese), "qtbase", "_",
                       QLibraryInfo.path(QLibraryInfo.LibraryPath.TranslationsPath)):
        app.installTranslator(translator)


def main() -> int:
    app = QApplication(sys.argv)
    app.setApplicationName("録画・画像の整理支援")
    install_japanese(app)
    theme.apply(app)
    window = MainWindow()
    window.show()
    return app.exec()

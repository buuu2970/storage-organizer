# サードパーティのソフトウェア

RecSort は次のソフトウェアを利用しています。ライセンス全文は `licenses/` にあり、配布版では `_internal/licenses/` に同梱されます。

| ソフトウェア | 用途 | ライセンス | 配布版への同梱 | 全文 |
|---|---|---|---|---|
| Qt / PySide6（Qt for Python） | 画面・動画再生 | LGPL v3（GPL v3 を参照） | 同梱（変更していないライブラリを動的に利用） | `licenses/Qt-PySide6_LGPL-3.0.txt`、`licenses/Qt-PySide6_GPL-3.0.txt` |
| Qt Multimedia の FFmpeg ライブラリ | 動画再生（PySide6 に含まれる） | LGPL v2.1 以上 | PySide6 の一部として同梱 | `licenses/FFmpeg_LGPL-2.1.txt` |
| numpy | 特徴量の計算 | BSD 3-Clause（同梱ライブラリの表記を含む） | 同梱 | ビルド時に `licenses/numpy-<版>/` へ自動でコピー（参考: `licenses/numpy_LICENSE.txt`） |
| Pillow | 画像の読み込み・類似判定・撮影日時 | MIT-CMU（HPND、同梱ライブラリの表記を含む） | 同梱 | ビルド時に `licenses/Pillow-<版>/` へ自動でコピー（参考: `licenses/Pillow_LICENSE.txt`） |
| Python | 実行環境（配布版に含まれる） | PSF License | 同梱 | `licenses/Python_PSF-LICENSE.txt` |
| PyInstaller | 配布版の作成（ブートローダーが exe に含まれる） | GPL v2 ＋ 配布物への例外条項 | ブートローダーのみ | `licenses/PyInstaller_COPYING.txt` |
| FFmpeg（ffmpeg / ffprobe） | 録画の長さ・サムネイル・失敗録画の判定 | LGPL / GPL（入手したビルドによる） | **同梱しない**（利用者が別途導入し、設定で場所を指定） | — |

## LGPL のライブラリについて
- Qt / PySide6 と、その FFmpeg ライブラリは変更せずに使い、onedir 形式（フォルダ配布）で動的に読み込みます。利用者は配布フォルダ内のライブラリを差し替えられます
- 対応するソースコードの入手先: Qt for Python は https://download.qt.io/official_releases/QtForPython/ 、Qt は https://download.qt.io/ 、FFmpeg は https://ffmpeg.org/download.html

## 取得元
- Qt の LGPL/GPL 全文: https://github.com/qt/qtbase の `LICENSES/`
- FFmpeg の LGPL v2.1 全文: https://github.com/FFmpeg/FFmpeg の `COPYING.LGPLv2.1`
- Python の PSF License: https://github.com/python/cpython （3.12 ブランチ）の `LICENSE`
- PyInstaller: https://github.com/pyinstaller/pyinstaller の `COPYING.txt`
- numpy・Pillow: インストールしたパッケージの `dist-info/licenses/`

## 未確認・要対応
- PySide6 のパッケージにはライセンス全文が含まれていなかったため、Qt の公式リポジトリから取得しました。配布する PySide6 の版（requirements.txt の範囲）と条件が変わっていないことを、正式配布の前にチームで確認してください
- FFmpeg を将来同梱する場合は、利用するビルド（GPL ビルドかどうか）のライセンス文とソースの入手方法を追加してください

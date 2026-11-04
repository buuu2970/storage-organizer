@echo off
chcp 65001 >nul
rem RecSort の配布版を作る（Windows 10/11、Python 3.12 以上が必要）。
rem 実行すると dist\RecSort\RecSort.exe ができる。インターネットから依存ライブラリを取得する。
setlocal
cd /d "%~dp0\.."
if not exist .venv-build (
    python -m venv .venv-build || goto :error
)
call .venv-build\Scripts\activate.bat || goto :error
python -m pip install --upgrade pip || goto :error
python -m pip install -r requirements.txt -r requirements-dev.txt || goto :error
python -m unittest discover -s tests || echo テストに失敗しました。内容を確認してください。
pyinstaller --noconfirm --clean packaging\recsort.spec || goto :error
echo.
echo 完成: dist\RecSort\RecSort.exe
echo 配布するときは dist\RecSort フォルダを ZIP にまとめてください。
exit /b 0
:error
echo 配布版の作成に失敗しました。
exit /b 1

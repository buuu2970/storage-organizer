@echo off
rem RecSort を起動する（RecSortを開く.bat・デモを開く.bat から呼ばれる）。
rem 初回だけ、このフォルダの .venv に必要な部品（requirements-web.txt）を入れる。2回目からはすぐ起動する。
chcp 65001 >nul
setlocal
cd /d "%~dp0.."
title RecSort

set "PY="
py -3 -c "import sys; sys.exit(0 if sys.version_info >= (3, 12) else 1)" >nul 2>nul && set "PY=py -3"
if not defined PY python -c "import sys; sys.exit(0 if sys.version_info >= (3, 12) else 1)" >nul 2>nul && set "PY=python"
if not defined PY goto nopython

if not exist ".venv\Scripts\python.exe" (
    echo はじめての起動です。必要な部品を準備しています（数分かかります）...
    %PY% -m venv .venv || goto fail
)
".venv\Scripts\python.exe" -m pip install --disable-pip-version-check -q -r requirements-web.txt || goto fail

echo.
echo RecSort を起動します。ブラウザに画面が開きます。
echo 終わるときは、この黒いウィンドウを閉じてください。
echo.
".venv\Scripts\python.exe" main.py %*
if errorlevel 1 pause
exit /b 0

:nopython
echo Python（3.12 以上）が見つかりません。
echo これから開くページから Python をインストールしてください。
echo インストール画面の最初で「Add python.exe to PATH」にチェックを入れてから、もう一度ダブルクリックしてください。
start "" https://www.python.org/downloads/
pause
exit /b 1

:fail
echo 準備に失敗しました。インターネットにつながっているか確認して、もう一度ダブルクリックしてください。
pause
exit /b 1

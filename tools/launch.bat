@echo off
rem RecSort を起動する（RecSortを開く.bat・デモを開く.bat から呼ばれる）。
rem 1. Python を探す（なければ、聞いてから自動で入れる）
rem 2. 初回だけ、このフォルダの .venv に必要な部品（requirements-web.txt）を入れる
rem 3. デモのときは FFmpeg を確かめる（なければ、聞いてから配布元から入れる）
rem 4. main.py を起動する（ブラウザに画面が開く）
chcp 65001 >nul
setlocal
cd /d "%~dp0.."
title RecSort

call :findpython
if not defined PY (
    echo.
    echo Python（3.12 以上）が見つかりません。RecSort を動かすのに必要です。
    where winget >nul 2>nul && (
        choice /C YN /M "いま自動で Python を入れますか（数分かかります）"
        if not errorlevel 2 (
            winget install -e --id Python.Python.3.12 --scope user --accept-package-agreements --accept-source-agreements
            call :findpython
        )
    )
)
if not defined PY goto nopython

if not exist ".venv\Scripts\python.exe" (
    echo はじめての起動です。必要な部品を準備しています（数分かかります）...
    %PY% -m venv .venv || goto fail
)
".venv\Scripts\python.exe" -m pip install --disable-pip-version-check -q -r requirements-web.txt || goto fail

echo %* | find "--demo" >nul && (
    ".venv\Scripts\python.exe" -c "import sys; from storage_organizer import media; sys.exit(0 if media.find_ffmpeg().available else 1)" || (
        echo.
        echo デモの録画を作るには FFmpeg（動画を扱う無料のソフト）が必要です。
        choice /C YN /M "いま配布元から FFmpeg を入れますか（約 110MB）"
        if not errorlevel 2 ".venv\Scripts\python.exe" tools\get_ffmpeg.py
    )
)

echo.
echo RecSort を起動します。ブラウザに画面が開きます。
echo 終わるときは、この黒いウィンドウを閉じてください。
echo.
".venv\Scripts\python.exe" main.py %*
if errorlevel 1 pause
exit /b 0

:findpython
set "PY="
set "CHECK=import sys; sys.exit(0 if sys.version_info >= (3, 12) else 1)"
py -3 -c "%CHECK%" >nul 2>nul && (set "PY=py -3" & exit /b 0)
python -c "%CHECK%" >nul 2>nul && (set "PY=python" & exit /b 0)
rem 自動で入れた直後は PATH が更新されていないため、入れた場所を直接見る
if exist "%LOCALAPPDATA%\Programs\Python\Python312\python.exe" set PY="%LOCALAPPDATA%\Programs\Python\Python312\python.exe"
exit /b 0

:nopython
echo.
echo Python を入れられませんでした。これから開くページから Python をインストールしてください。
echo インストール画面の最初で「Add python.exe to PATH」にチェックを入れてから、もう一度ダブルクリックしてください。
start "" https://www.python.org/downloads/
pause
exit /b 1

:fail
echo 準備に失敗しました。インターネットにつながっているか確認して、もう一度ダブルクリックしてください。
pause
exit /b 1

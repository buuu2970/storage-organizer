@echo off
chcp 65001 > nul
rem ダブルクリックで起動 → ブラウザに画面が開きます
cd /d "%~dp0"
where py > nul 2>&1 && (set "PY=py -3") || (set "PY=python")
%PY% -X utf8 -m storage_organizer
if errorlevel 1 pause

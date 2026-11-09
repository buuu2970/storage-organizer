@echo off
rem ダブルクリックで、授業の発表用のデモを開く（自分の録画は表示しない。FFmpeg が必要）
call "%~dp0tools\launch.bat" --demo %*

@echo off
rem ダブルクリックで、授業の発表用のデモを開く（デモの録画を作って PC 全体を調べる。FFmpeg が必要）
call "%~dp0tools\launch.bat" --demo %*

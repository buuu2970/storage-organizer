@echo off
rem デモで作った録画だけを消す（素材や自分のファイルは消さない）
call "%~dp0tools\launch.bat" --remove-demo
pause

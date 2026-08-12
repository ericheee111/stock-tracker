@echo off
REM Stock Tracker 一键启动（Windows）
cd /d "%~dp0.."
python scripts\start.py start %*
pause

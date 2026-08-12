@echo off
REM Stock Tracker 停止
cd /d "%~dp0.."
python scripts\start.py stop
pause

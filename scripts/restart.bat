@echo off
REM Stock Tracker 重启
cd /d "%~dp0.."
python scripts\start.py restart %*
pause

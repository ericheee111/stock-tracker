@echo off
REM Stock Tracker 状态检查
cd /d "%~dp0.."
python scripts\start.py status
pause

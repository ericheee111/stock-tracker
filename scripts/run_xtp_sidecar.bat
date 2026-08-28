@echo off
setlocal
cd /d "%~dp0.."
py -3.9 scripts\run_xtp_sidecar.py %*
exit /b %ERRORLEVEL%

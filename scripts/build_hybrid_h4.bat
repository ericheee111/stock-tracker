@echo off
setlocal
python "%~dp0build_hybrid_h4.py" %*
exit /b %errorlevel%

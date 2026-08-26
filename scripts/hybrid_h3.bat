@echo off
setlocal
python "%~dp0hybrid_h3.py" %*
exit /b %errorlevel%

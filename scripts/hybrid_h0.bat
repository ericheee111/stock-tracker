@echo off
REM Hybrid H0 Tailscale Serve operator entrypoint.
REM Set STOCK_TRACKER_PRIVATE_ACCESS in the process environment before preflight/enable.
cd /d "%~dp0.."
python scripts\hybrid_h0.py %*

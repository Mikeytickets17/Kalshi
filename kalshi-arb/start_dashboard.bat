@echo off
REM ------------------------------------------------------------------
REM  kalshi-arb dashboard -- one-click launcher (Windows).
REM
REM  What this does:
REM    1. Moves into the kalshi-arb folder (wherever this file lives).
REM    2. Runs the Python launcher, which:
REM       - downloads cloudflared.exe on first run (~25 MB, one-time)
REM       - generates a dashboard password on first run
REM         (saved to .dashboard_creds, gitignored)
REM       - starts uvicorn on 127.0.0.1:8000
REM       - starts cloudflared tunnel -> prints a public HTTPS URL
REM       - writes the URL to dashboard_url.txt in this folder
REM
REM  When you're done: close this window (or press Ctrl+C). Both the
REM  dashboard and tunnel stop. No cleanup needed.
REM ------------------------------------------------------------------

setlocal
cd /d "%~dp0"

echo.
echo ==================================================================
echo   Starting kalshi-arb dashboard launcher
echo   Wait for the DASHBOARD URL banner (about 10 seconds).
echo ==================================================================
echo.

python -m kalshi_arb.dashboard.launcher
set EXITCODE=%ERRORLEVEL%

echo.
echo Launcher exited with code %EXITCODE%. Press any key to close.
pause >nul
exit /b %EXITCODE%

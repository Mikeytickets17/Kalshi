@echo off
title Kalshi Trading Bot
color 0A
cd /d "%~dp0"

echo.
echo  ============================================================
echo   KALSHI MULTI-STRATEGY TRADING BOT
echo   Double-click this file to start. That's it.
echo  ============================================================
echo.

:: Auto-update to latest version
echo  [1/4] Updating to latest version...
git pull origin main >nul 2>&1

:: Install/update dependencies silently
echo  [2/4] Checking dependencies...
py -m pip install -r requirements.txt --quiet >nul 2>&1

:: Create .env if it doesn't exist
if not exist .env (
    copy .env.example .env >nul 2>&1
    echo  [!] Created .env — add your API keys by editing .env
)

:: Start bot in background
echo  [3/4] Starting trading bot...
start "Kalshi Bot" /min cmd /c "cd /d %~dp0 && py bot.py"
timeout /t 2 /nobreak >nul

:: Start dashboard in background
echo  [4/4] Starting dashboard...
start "Kalshi Dashboard" /min cmd /c "cd /d %~dp0 && py dashboard.py"
timeout /t 2 /nobreak >nul

:: Open dashboard in browser automatically
start "" "file:///%~dp0dashboard.html"

echo.
echo  ============================================================
echo   RUNNING - Do not close this window
echo  ============================================================
echo.
echo   Dashboard: file:///%~dp0dashboard.html
echo   Bot + Dashboard + Auto-updates all running.
echo   Leave this open. Check dashboard in your browser.
echo.
echo   Press any key to STOP everything.
echo.
pause >nul

:: Kill everything on exit
taskkill /fi "windowtitle eq Kalshi Bot" /f >nul 2>&1
taskkill /fi "windowtitle eq Kalshi Dashboard" /f >nul 2>&1
echo  Stopped. Close this window.
pause >nul

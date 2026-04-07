@echo off
echo ============================================================
echo   Kalshi Multi-Strategy Trading Bot v5.1
echo ============================================================
echo.

cd /d "%~dp0"

:: Check Python
python --version >nul 2>&1
if errorlevel 1 (
    echo [ERROR] Python not found. Install from https://www.python.org/downloads/
    echo         Make sure to check "Add to PATH" during install.
    pause
    exit /b 1
)

:: Check .env
if not exist .env (
    echo [SETUP] Creating .env from .env.example...
    copy .env.example .env
    echo [SETUP] Edit .env to add your API keys, then run this again.
    notepad .env
    pause
    exit /b 0
)

:: Install dependencies
echo [SETUP] Installing dependencies...
python -m pip install -r requirements.txt --quiet

echo.
echo [START] Launching bot + dashboard + research scanner...
echo.
echo   Dashboard: http://localhost:5050
echo   Press Ctrl+C in any window to stop that process.
echo.

:: Start bot in new window
start "Kalshi Bot" cmd /k "cd /d %~dp0 && python bot.py"
timeout /t 2 /nobreak >nul

:: Start dashboard in new window
start "Kalshi Dashboard" cmd /k "cd /d %~dp0 && python dashboard.py"
timeout /t 1 /nobreak >nul

:: Start research scanner in new window
start "Kalshi Research" cmd /k "cd /d %~dp0 && python research_scanner.py"

echo.
echo [RUNNING] All 3 processes started in separate windows.
echo   - Bot: monitoring Trump + news + whales
echo   - Dashboard: http://localhost:5050
echo   - Research: scanning with Brave Search every 30min
echo.
echo Close this window anytime. The other 3 windows keep running.
pause

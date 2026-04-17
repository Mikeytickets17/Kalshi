@echo off
REM ------------------------------------------------------------------
REM  kalshi-arb: one-click Windows setup.
REM  Double-click this file in File Explorer. It will:
REM    1. cd into the kalshi-arb folder (wherever this script lives)
REM    2. install Python dependencies
REM    3. make sure the .env and .pem files are in place
REM  You do NOT need to open a terminal manually.
REM ------------------------------------------------------------------

setlocal enabledelayedexpansion
cd /d "%~dp0"

echo.
echo ======================================================
echo  Kalshi Arb Setup
echo ======================================================
echo.

REM -- Check Python is available --
where python >nul 2>nul
if errorlevel 1 (
    echo [ERROR] Python is not installed or not on PATH.
    echo Install Python 3.11+ from https://www.python.org/downloads/ and rerun.
    pause
    exit /b 1
)

REM -- Check .env exists --
if not exist ".env" (
    echo [WARN] .env does not exist yet.
    echo Copying .env.example to .env...
    copy /y ".env.example" ".env" >nul
    echo.
    echo  ACTION REQUIRED:
    echo  1. Open .env in VS Code or Notepad
    echo  2. Fill in KALSHI_API_KEY_ID = your Kalshi demo key
    echo  3. Put kalshi-demo.pem in THIS folder (same folder as this .bat)
    echo  4. Save the .env file
    echo  5. Rerun this setup.bat after editing.
    echo.
    pause
    exit /b 0
)

REM -- Check PEM exists --
if not exist "kalshi-demo.pem" (
    echo [ERROR] kalshi-demo.pem is not in this folder.
    echo.
    echo Move kalshi-demo.pem from Desktop into:
    echo   %CD%
    echo Then rerun this setup.bat.
    pause
    exit /b 1
)

echo Installing Python dependencies...
python -m pip install --quiet --upgrade pip
python -m pip install --quiet -e ".[dev]"
if errorlevel 1 (
    echo.
    echo [ERROR] pip install failed. Check your internet connection and try again.
    pause
    exit /b 1
)

echo.
echo Running the test suite...
python -m pytest tests/ -q
if errorlevel 1 (
    echo.
    echo [ERROR] Tests failed. Stopping before the probe.
    pause
    exit /b 1
)

echo.
echo ======================================================
echo  Setup complete.
echo  Next: double-click run_probe.bat to run the probe.
echo ======================================================
pause

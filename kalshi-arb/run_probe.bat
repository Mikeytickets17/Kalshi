@echo off
REM ------------------------------------------------------------------
REM  kalshi-arb: one-click probe runner.
REM  Runs the 3 demo probes (WS cap, REST latency, rate-limit ceiling).
REM  Probe #4 (end-to-end loop) is deferred to prod paper-trading phase.
REM  Output lands at config\detected_limits.yaml
REM ------------------------------------------------------------------

setlocal
cd /d "%~dp0"

echo.
echo ======================================================
echo  Running Kalshi Arb Probe (demo mode)
echo  This will take about 3-4 minutes.
echo ======================================================
echo.

REM -- Sanity check prerequisites --
if not exist ".env" (
    echo [ERROR] .env not found. Run setup.bat first.
    pause
    exit /b 1
)
if not exist "kalshi-demo.pem" (
    echo [ERROR] kalshi-demo.pem not found in this folder.
    pause
    exit /b 1
)

python -m kalshi_arb.probe.probe
if errorlevel 1 (
    echo.
    echo [ERROR] Probe failed. Check logs\kalshi-arb.log for the full error.
    pause
    exit /b 1
)

echo.
echo ======================================================
echo  Probe complete.
echo  Results file:
echo    %CD%\config\detected_limits.yaml
echo.
echo  Open that file in VS Code and paste the contents back
echo  to Claude for verification.
echo ======================================================
pause

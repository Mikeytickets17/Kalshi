@echo off
REM ------------------------------------------------------------------
REM  kalshi-arb paper mode -- one-click launcher.
REM
REM  Starts the paper-trading pipeline:
REM    Kalshi WS (read-only) -> scanner -> sizer -> PaperKalshiAPI
REM    -> event store -> dashboard
REM
REM  PaperKalshiAPI is in-process. NO real orders fire. Your Kalshi
REM  account is not touched by orders (only by read-only portfolio
REM  queries, if enabled).
REM
REM  This launcher uses --skip-probe-gate because the prod probe has
REM  been flaky across iteration and is defense-in-depth, not a safety
REM  requirement for paper mode. Live trading (future CLI) will NOT
REM  expose this flag; the probe gate will be re-enforced there.
REM
REM  To stop: press Ctrl+C in this window. The runner shuts down
REM  cleanly, cancels any pending orders, and flushes the event store.
REM
REM  Usage:
REM    1. Double-click this file.
REM    2. Leave the window open while paper trading runs.
REM    3. Watch the dashboard (run start_dashboard.bat in another
REM       window) for live activity.
REM    4. Close the window / Ctrl+C to stop.
REM ------------------------------------------------------------------

setlocal
cd /d "%~dp0"

REM Sanity check: .env should exist (we need KALSHI_API_KEY_ID etc).
if not exist ".env" (
    echo [ERROR] .env not found. Run setup.bat first.
    pause
    exit /b 1
)

echo.
echo ======================================================
echo  kalshi-arb paper mode starting...
echo  Bypass: --skip-probe-gate (paper uses in-process API)
echo  Ctrl+C to stop cleanly.
echo ======================================================
echo.

python -m kalshi_arb.cli paper --skip-probe-gate
set EXITCODE=%ERRORLEVEL%

echo.
if %EXITCODE% == 0 (
    echo Paper run stopped cleanly.
) else (
    echo Paper run exited with code %EXITCODE%.
    echo Check logs\kalshi-arb.log for details.
)
pause
exit /b %EXITCODE%

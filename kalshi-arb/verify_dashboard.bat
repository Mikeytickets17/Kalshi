@echo off
REM ------------------------------------------------------------------
REM  Verifies the running dashboard end-to-end and shows a Windows
REM  popup with PASS or FAIL. ZERO interpretation needed.
REM
REM  Usage:
REM    1. Make sure start_dashboard.bat is running in another window.
REM    2. Double-click this file.
REM    3. Wait ~20 seconds.
REM    4. A popup tells you if step 3 works.
REM
REM  If FAIL: copy the dialog text and paste it to Claude. Don't
REM  guess what went wrong.
REM ------------------------------------------------------------------

setlocal
cd /d "%~dp0"

python -m kalshi_arb.tools.verify_dashboard > verify_output.txt 2>&1
set EXITCODE=%ERRORLEVEL%

REM Show a Windows MessageBox with the result. No terminal reading required.
powershell -NoProfile -ExecutionPolicy Bypass -Command ^
  "$txt = Get-Content -Raw verify_output.txt;" ^
  "if (%EXITCODE% -eq 0) {" ^
  "  $title = 'kalshi-arb: STEP 3 PASS';" ^
  "  $icon  = 'Information';" ^
  "  $msg   = 'All checks passed. The dashboard is live, login works, all six tabs load, and synthetic events flowed end-to-end through the tunnel within 10 seconds.';" ^
  "} else {" ^
  "  $title = 'kalshi-arb: STEP 3 FAIL';" ^
  "  $icon  = 'Error';" ^
  "  $msg   = 'A check failed. Copy the text below and paste it to Claude:' + [Environment]::NewLine + [Environment]::NewLine + $txt;" ^
  "}" ^
  "Add-Type -AssemblyName System.Windows.Forms;" ^
  "[System.Windows.Forms.MessageBox]::Show($msg, $title, 'OK', $icon)"

exit /b %EXITCODE%

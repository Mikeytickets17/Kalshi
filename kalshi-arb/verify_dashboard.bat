@echo off
REM ------------------------------------------------------------------
REM  Verifies the running dashboard end-to-end and shows a Windows
REM  popup with a one-line PASS/FAIL headline + actionable detail.
REM  ZERO interpretation needed -- the popup tells you what to do.
REM
REM  Usage:
REM    1. Make sure start_dashboard.bat is running in another window
REM       (it should show a "DASHBOARD URL" banner).
REM    2. Double-click this file.
REM    3. Wait ~20 seconds.
REM    4. Read the popup headline -- if it says FAIL, the popup also
REM       tells you the next step. Do that.
REM ------------------------------------------------------------------

setlocal
cd /d "%~dp0"

python -m kalshi_arb.tools.verify_dashboard > verify_output.txt 2>&1
set EXITCODE=%ERRORLEVEL%

REM Show a Windows MessageBox with the headline + full output.
REM HEADLINE: <message> in the script output becomes the dialog title;
REM the body shows the full transcript so any failure detail is visible
REM without opening a terminal.
powershell -NoProfile -ExecutionPolicy Bypass -Command ^
  "$txt = Get-Content -Raw verify_output.txt;" ^
  "$headline = (Select-String -Path verify_output.txt -Pattern '^HEADLINE: (.*)$' | ForEach-Object { $_.Matches[0].Groups[1].Value } | Select-Object -Last 1);" ^
  "if (-not $headline) { $headline = 'verifier produced no HEADLINE line' }" ^
  "if (%EXITCODE% -eq 0) {" ^
  "  $title = 'kalshi-arb: PASS';" ^
  "  $icon  = 'Information';" ^
  "  $url = '';" ^
  "  if (Test-Path dashboard_url.txt) { $url = (Get-Content -Raw dashboard_url.txt).Trim() }" ^
  "  $msg = 'ALL GREEN.' + [Environment]::NewLine + [Environment]::NewLine + $headline + [Environment]::NewLine + [Environment]::NewLine + 'Dashboard URL: ' + $url + [Environment]::NewLine + [Environment]::NewLine + 'Login: admin / (password in .dashboard_creds)';" ^
  "} else {" ^
  "  $title = 'kalshi-arb: FAIL';" ^
  "  $icon  = 'Error';" ^
  "  $msg = $headline + [Environment]::NewLine + [Environment]::NewLine + '--- full transcript below ---' + [Environment]::NewLine + $txt;" ^
  "}" ^
  "Add-Type -AssemblyName System.Windows.Forms;" ^
  "[System.Windows.Forms.MessageBox]::Show($msg, $title, 'OK', $icon)"

exit /b %EXITCODE%

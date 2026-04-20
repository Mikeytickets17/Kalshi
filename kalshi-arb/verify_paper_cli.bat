@echo off
REM ------------------------------------------------------------------
REM  Verifies the `kalshi-arb paper` CLI subcommand end-to-end.
REM  Pops a Windows MessageBox with PASS/FAIL + actionable detail.
REM
REM  What this checks (all on throwaway tmp data, touches nothing real):
REM    1. CLI refuses without a prod probe file
REM    2. CLI refuses when probe environment != prod
REM    3. CLI refuses when probe is stale (> 24h)
REM    4. CLI refuses when LIVE_TRADING=true is in env
REM    5. --smoke-test runs pipeline clean and exits 0
REM    6. Event store gets opportunities + orders_placed rows
REM
REM  Usage:
REM    1. Double-click this file.
REM    2. Wait about 30 seconds.
REM    3. Read the popup headline.
REM ------------------------------------------------------------------

setlocal
cd /d "%~dp0"

python -m kalshi_arb.tools.verify_paper_cli > verify_paper_output.txt 2>&1
set EXITCODE=%ERRORLEVEL%

REM Show a Windows MessageBox with the headline + full output.
powershell -NoProfile -ExecutionPolicy Bypass -Command ^
  "$txt = Get-Content -Raw verify_paper_output.txt;" ^
  "$headline = (Select-String -Path verify_paper_output.txt -Pattern '^HEADLINE: (.*)$' | ForEach-Object { $_.Matches[0].Groups[1].Value } | Select-Object -Last 1);" ^
  "if (-not $headline) { $headline = 'verifier produced no HEADLINE line' }" ^
  "if (%EXITCODE% -eq 0) {" ^
  "  $title = 'kalshi-arb paper CLI: PASS';" ^
  "  $icon  = 'Information';" ^
  "  $msg = 'ALL GREEN.' + [Environment]::NewLine + [Environment]::NewLine + $headline + [Environment]::NewLine + [Environment]::NewLine + 'CLI is safe to run for the 48h paper session.';" ^
  "} else {" ^
  "  $title = 'kalshi-arb paper CLI: FAIL';" ^
  "  $icon  = 'Error';" ^
  "  $msg = $headline + [Environment]::NewLine + [Environment]::NewLine + '--- full transcript below ---' + [Environment]::NewLine + $txt;" ^
  "}" ^
  "Add-Type -AssemblyName System.Windows.Forms;" ^
  "[System.Windows.Forms.MessageBox]::Show($msg, $title, 'OK', $icon)"

exit /b %EXITCODE%

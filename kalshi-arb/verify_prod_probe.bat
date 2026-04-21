@echo off
REM ------------------------------------------------------------------
REM  Runs the production probe end-to-end against real Kalshi.
REM  Pops a Windows MessageBox with PASS/FAIL + the measured numbers
REM  (or the specific failure reason).
REM
REM  Before double-clicking:
REM    1. Your .env must have:
REM         KALSHI_USE_DEMO=false
REM         KALSHI_API_KEY_ID=<your production key id>
REM         KALSHI_PRIVATE_KEY_PATH=<path to your production PEM>
REM    2. You must be on the IP that is allowlisted for your
REM       production key.
REM
REM  What it does:
REM    - Prints a 5-second countdown banner (press Ctrl+C to abort).
REM    - Runs all 4 probes against production Kalshi:
REM        1. WS subscription cap
REM        2. REST write latency (p50/p95/p99)
REM        3. REST rate-limit ceiling
REM        4. End-to-end arb loop latency
REM    - Every order placed is a 1c BUY YES limit tagged `probe-`,
REM      cancelled immediately. Nothing can fill.
REM    - If any probe fails or takes longer than 3 minutes, NO
REM      detected_limits.yaml is written.
REM    - On PASS, detected_limits.yaml is ready; paper CLI will
REM      now accept it.
REM
REM  Usage:
REM    1. Double-click this file.
REM    2. Wait up to ~3 minutes.
REM    3. Read the popup headline.
REM ------------------------------------------------------------------

setlocal
cd /d "%~dp0"

python -m kalshi_arb.cli probe --env prod > verify_prod_probe_output.txt 2>&1
set EXITCODE=%ERRORLEVEL%

REM Show a Windows MessageBox with PASS/FAIL + transcript.
powershell -NoProfile -ExecutionPolicy Bypass -Command ^
  "$txt = Get-Content -Raw verify_prod_probe_output.txt;" ^
  "$pass_line = (Select-String -Path verify_prod_probe_output.txt -Pattern '^PROBE PASSED' | ForEach-Object { $_.Line } | Select-Object -Last 1);" ^
  "$fail_line = (Select-String -Path verify_prod_probe_output.txt -Pattern '^PROBE FAILED' | ForEach-Object { $_.Line } | Select-Object -Last 1);" ^
  "if (%EXITCODE% -eq 0) {" ^
  "  $title = 'kalshi-arb prod probe: PASS';" ^
  "  $icon  = 'Information';" ^
  "  $headline = if ($pass_line) { $pass_line } else { 'Probe completed successfully.' };" ^
  "  $msg = 'ALL GREEN.' + [Environment]::NewLine + [Environment]::NewLine + $headline + [Environment]::NewLine + [Environment]::NewLine + 'config/detected_limits.yaml has been written with production numbers.' + [Environment]::NewLine + 'The paper CLI gate will now accept it. Next step: run verify_paper_cli.bat again for the 48h session.' + [Environment]::NewLine + [Environment]::NewLine + '--- full transcript below ---' + [Environment]::NewLine + $txt;" ^
  "} else {" ^
  "  $title = 'kalshi-arb prod probe: FAIL';" ^
  "  $icon  = 'Error';" ^
  "  $headline = if ($fail_line) { $fail_line } else { 'Probe did not complete cleanly.' };" ^
  "  $msg = $headline + [Environment]::NewLine + [Environment]::NewLine + 'detected_limits.yaml was NOT written. Fix the issue above and re-run this .bat.' + [Environment]::NewLine + [Environment]::NewLine + '--- full transcript below ---' + [Environment]::NewLine + $txt;" ^
  "}" ^
  "Add-Type -AssemblyName System.Windows.Forms;" ^
  "[System.Windows.Forms.MessageBox]::Show($msg, $title, 'OK', $icon)"

exit /b %EXITCODE%

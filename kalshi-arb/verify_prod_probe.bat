@echo off
REM ------------------------------------------------------------------
REM  Runs the production probe end-to-end against real Kalshi and
REM  pops a Windows MessageBox with PASS/FAIL + the measured numbers.
REM
REM  The popup is diagnostic on failure: every specific threshold
REM  that missed is listed with the measured value AND the required
REM  value. Never "thresholds not met" without details.
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
REM    - Runs all 4 probes against production Kalshi.
REM    - Every order placed is a 1c BUY YES limit tagged `probe-`,
REM      cancelled immediately. Nothing can fill.
REM    - Emits one `PROBE SUMMARY:` line with every measured number
REM      regardless of pass/fail.
REM    - On FAIL, emits one `PROBE FAILED DETAIL:` line per
REM      threshold that missed.
REM    - If any probe fails or the suite exceeds 3 minutes, NO
REM      detected_limits.yaml is written.
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

REM Pull the structured diagnostic lines out of the transcript.
REM   PROBE SUMMARY:          -> always present, shows every measured number
REM   PROBE FAILED DETAIL:    -> one per missed threshold (only on FAIL)
REM   PROBE ERROR DETAIL:     -> grouped Kalshi rejections (emitted in
REM                              either env whenever writes fail)
REM   PROBE PASSED / FAILED:  -> headline banner
powershell -NoProfile -ExecutionPolicy Bypass -Command ^
  "$txt = Get-Content -Raw verify_prod_probe_output.txt;" ^
  "$summary = (Select-String -Path verify_prod_probe_output.txt -Pattern '^PROBE SUMMARY:' | ForEach-Object { $_.Line } | Select-Object -Last 1);" ^
  "if (-not $summary) { $summary = '(no summary line emitted -- probe exited before measurement)' }" ^
  "$details = (Select-String -Path verify_prod_probe_output.txt -Pattern '^PROBE FAILED DETAIL:' | ForEach-Object { $_.Line });" ^
  "$details_text = if ($details) { ($details | Out-String).Trim() } else { '' };" ^
  "$errors = (Select-String -Path verify_prod_probe_output.txt -Pattern '^PROBE ERROR DETAIL:' | ForEach-Object { $_.Line });" ^
  "$errors_text = if ($errors) { ($errors | Out-String).Trim() } else { '' };" ^
  "$pass_line = (Select-String -Path verify_prod_probe_output.txt -Pattern '^PROBE PASSED' | ForEach-Object { $_.Line } | Select-Object -Last 1);" ^
  "$fail_line = (Select-String -Path verify_prod_probe_output.txt -Pattern '^PROBE FAILED \\(' | ForEach-Object { $_.Line } | Select-Object -Last 1);" ^
  "if (%EXITCODE% -eq 0) {" ^
  "  $title = 'kalshi-arb prod probe: PASS';" ^
  "  $icon  = 'Information';" ^
  "  $headline = if ($pass_line) { $pass_line } else { 'Probe completed successfully.' };" ^
  "  $msg = 'ALL GREEN.' + [Environment]::NewLine + [Environment]::NewLine + $summary + [Environment]::NewLine + [Environment]::NewLine + $headline + [Environment]::NewLine + [Environment]::NewLine + 'config/detected_limits.yaml has been written with production numbers.' + [Environment]::NewLine + 'The paper CLI gate will now accept it. Next step: run verify_paper_cli.bat for the 48h session.' + [Environment]::NewLine + [Environment]::NewLine + '--- full transcript below ---' + [Environment]::NewLine + $txt;" ^
  "} else {" ^
  "  $title = 'kalshi-arb prod probe: FAIL';" ^
  "  $icon  = 'Error';" ^
  "  $headline = if ($fail_line) { $fail_line } else { 'Probe did not complete cleanly.' };" ^
  "  $body = $headline + [Environment]::NewLine + [Environment]::NewLine + $summary;" ^
  "  if ($details_text) { $body = $body + [Environment]::NewLine + [Environment]::NewLine + 'Thresholds that failed:' + [Environment]::NewLine + $details_text }" ^
  "  if ($errors_text) { $body = $body + [Environment]::NewLine + [Environment]::NewLine + 'Kalshi error responses:' + [Environment]::NewLine + $errors_text }" ^
  "  $body = $body + [Environment]::NewLine + [Environment]::NewLine + 'detected_limits.yaml was NOT written. Fix the specific threshold(s) above and re-run this .bat.' + [Environment]::NewLine + [Environment]::NewLine + '--- full transcript below ---' + [Environment]::NewLine + $txt;" ^
  "  $msg = $body;" ^
  "}" ^
  "Add-Type -AssemblyName System.Windows.Forms;" ^
  "[System.Windows.Forms.MessageBox]::Show($msg, $title, 'OK', $icon)"

exit /b %EXITCODE%

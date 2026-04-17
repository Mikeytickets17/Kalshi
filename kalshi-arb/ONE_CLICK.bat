@echo off
REM ==================================================================
REM  ONE-CLICK. Do every setup step automatically.
REM
REM  What this does for you:
REM    1. Pulls latest code
REM    2. Finds your kalshi-demo.pem on Desktop and moves it here
REM    3. Asks you for your Key ID in a popup
REM    4. Creates the .env file with the right contents
REM    5. Installs Python dependencies
REM    6. Runs the probe
REM    7. Opens the result file so you can copy-paste it to Claude
REM
REM  You do NOTHING except double-click this file and paste your Key ID
REM  when asked.
REM ==================================================================

setlocal enabledelayedexpansion
cd /d "%~dp0"

powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0\one_click.ps1"
pause

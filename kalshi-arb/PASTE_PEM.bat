@echo off
REM Double-click this. A text box opens. Paste the PEM contents (starts with
REM -----BEGIN RSA PRIVATE KEY-----), click OK. It saves as kalshi-demo.pem
REM in this folder so ONE_CLICK.bat can find it.

setlocal
cd /d "%~dp0"
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0\paste_pem.ps1"
pause

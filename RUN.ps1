# Kalshi Trading Bot Launcher — PowerShell version
# Right-click this file → Run with PowerShell
# Or paste in VS Code terminal: .\RUN.ps1

Set-Location $PSScriptRoot

Write-Host ""
Write-Host "============================================================" -ForegroundColor Cyan
Write-Host "  KALSHI MULTI-STRATEGY TRADING BOT" -ForegroundColor Cyan
Write-Host "============================================================" -ForegroundColor Cyan
Write-Host ""

# Auto-update
Write-Host "[1/4] Updating..." -ForegroundColor Yellow
git pull origin main 2>$null

# Install deps
Write-Host "[2/4] Dependencies..." -ForegroundColor Yellow
py -m pip install -r requirements.txt --quiet 2>$null

# Create .env if needed
if (-not (Test-Path .env)) {
    Copy-Item .env.example .env
    Write-Host "[!] Created .env — edit it to add API keys" -ForegroundColor Red
}

# Kill old processes
Stop-Process -Name "py" -Force -ErrorAction SilentlyContinue
Start-Sleep 1

# Start bot
Write-Host "[3/4] Starting bot..." -ForegroundColor Green
Start-Process py -ArgumentList "bot.py"
Start-Sleep 2

# Start dashboard
Write-Host "[4/4] Starting dashboard..." -ForegroundColor Green
Start-Process py -ArgumentList "dashboard.py"
Start-Sleep 1

# Open dashboard
Start-Process "http://localhost:5050"

Write-Host ""
Write-Host "============================================================" -ForegroundColor Cyan
Write-Host "  RUNNING — Bot + Dashboard active" -ForegroundColor Green
Write-Host "  Dashboard: http://localhost:5050" -ForegroundColor Cyan
Write-Host "  Press any key to STOP everything" -ForegroundColor Yellow
Write-Host "============================================================" -ForegroundColor Cyan
Write-Host ""

$null = $Host.UI.RawUI.ReadKey("NoEcho,IncludeKeyDown")

Stop-Process -Name "py" -Force -ErrorAction SilentlyContinue
Write-Host "Stopped." -ForegroundColor Red

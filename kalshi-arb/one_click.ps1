# Kalshi Arb -- one-click setup + probe. Zero manual steps except pasting the Key ID.
#
# This is run from ONE_CLICK.bat so the user never has to open PowerShell directly.

# Do NOT use "Stop" globally. External commands (git, pip, pytest, python)
# routinely write progress and warnings to stderr. With Stop, any stderr
# line becomes a terminating error -- which crashed both the git-pull step
# and pip install in earlier versions of this file. Use Continue and rely
# on explicit $LASTEXITCODE checks after every external invocation.
$ErrorActionPreference = "Continue"

function Say($text, $color = "White") {
    Write-Host $text -ForegroundColor $color
}

function Fail($text) {
    Say ""
    Say "============================================================" Red
    Say "  ERROR: $text" Red
    Say "============================================================" Red
    Say ""
    Say "  Copy the text above (the red lines) and paste it to Claude." Yellow
    Say ""
    exit 1
}

Say ""
Say "============================================================" Cyan
Say "  KALSHI ARB -- ONE-CLICK SETUP + PROBE" Cyan
Say "============================================================" Cyan
Say ""

# ------------- STEP 1: Make sure we are in the right folder -------------
$here = $PSScriptRoot
if (-not $here) { $here = Split-Path -Parent $MyInvocation.MyCommand.Path }
Set-Location $here

if (-not (Test-Path "pyproject.toml")) {
    Fail "This script must sit inside the kalshi-arb folder. Current folder: $here"
}

# ------------- STEP 2: Pull latest code (best-effort) -------------
Say "[1/7] Pulling latest code..." Cyan
$parent = Split-Path -Parent $here
# Isolate the git call from $ErrorActionPreference=Stop -- git writes normal
# progress to stderr and PowerShell would otherwise treat that as a
# terminating error. Use SilentlyContinue + $LASTEXITCODE as the truth.
$oldEAP = $ErrorActionPreference
$ErrorActionPreference = "Continue"
try {
    $pullOutput = & git -C $parent pull origin claude/fix-crypto-discovery-EvvsD 2>&1 | Out-String
    Write-Host $pullOutput
    if ($LASTEXITCODE -ne 0) {
        Say "  (Pull exited $LASTEXITCODE -- continuing with local copy.)" Yellow
    }
} catch {
    Say "  (Pull threw: $_ -- continuing with local copy.)" Yellow
}
$ErrorActionPreference = $oldEAP
# Defensive: always reset CWD before every subsequent step.
Set-Location $here

# ------------- STEP 3: Find the PEM file -------------
Say ""
Say "[2/7] Looking for kalshi-demo.pem..." Cyan
$pemHere = Join-Path $here "kalshi-demo.pem"
if (-not (Test-Path $pemHere)) {
    $desktop = [Environment]::GetFolderPath("Desktop")
    $pemDesktop = Join-Path $desktop "kalshi-demo.pem"
    if (Test-Path $pemDesktop) {
        Say "  Found on Desktop, moving into this folder..." Green
        Move-Item $pemDesktop $pemHere
    } else {
        # Search common locations
        $candidates = @(
            (Join-Path $env:USERPROFILE "Downloads\kalshi-demo.pem"),
            (Join-Path $env:USERPROFILE "Documents\kalshi-demo.pem"),
            (Join-Path $desktop "kalshi.pem"),
            (Join-Path $desktop "kalshi_demo.pem")
        )
        $found = $null
        foreach ($c in $candidates) {
            if (Test-Path $c) { $found = $c; break }
        }
        if ($found) {
            Say "  Found at $found, copying..." Green
            Copy-Item $found $pemHere
        } else {
            # Last resort: popup file picker
            Add-Type -AssemblyName System.Windows.Forms
            $dlg = New-Object System.Windows.Forms.OpenFileDialog
            $dlg.Title = "Select your kalshi-demo.pem file"
            $dlg.Filter = "PEM files (*.pem)|*.pem|All files (*.*)|*.*"
            $dlg.InitialDirectory = $desktop
            if ($dlg.ShowDialog() -eq "OK") {
                Copy-Item $dlg.FileName $pemHere
                Say "  Copied from $($dlg.FileName)" Green
            } else {
                Fail "No kalshi-demo.pem found. Put it on your Desktop and rerun."
            }
        }
    }
} else {
    Say "  Already in place." Green
}

# ------------- STEP 4: Get the Key ID -------------
Say ""
Say "[3/7] Creating the .env file..." Cyan

$envPath = Join-Path $here ".env"
$existingKeyId = ""
if (Test-Path $envPath) {
    $existing = Get-Content $envPath -Raw
    $m = [regex]::Match($existing, "KALSHI_API_KEY_ID=(\S+)")
    if ($m.Success) { $existingKeyId = $m.Groups[1].Value }
}

Add-Type -AssemblyName Microsoft.VisualBasic
$prompt = "Paste your Kalshi demo Key ID (from Notes).`n`nIt looks like a long string with dashes, e.g. abcd1234-ef56-...."
$keyId = [Microsoft.VisualBasic.Interaction]::InputBox($prompt, "Kalshi Key ID", $existingKeyId)
if (-not $keyId) {
    Fail "No Key ID entered. Rerun and paste your Key ID when asked."
}
$keyId = $keyId.Trim()

# Validate it at least looks like an API key
if ($keyId.Length -lt 10) {
    Fail "That Key ID looks too short. Copy it from Notes again and rerun."
}

# Build the .env file from the template, with user values filled in
$template = Get-Content (Join-Path $here ".env.example") -Raw
$envContent = $template `
    -replace '(?m)^KALSHI_API_KEY_ID=.*$', "KALSHI_API_KEY_ID=$keyId" `
    -replace '(?m)^KALSHI_PRIVATE_KEY_PATH=.*$', "KALSHI_PRIVATE_KEY_PATH=./kalshi-demo.pem" `
    -replace '(?m)^KALSHI_USE_DEMO=.*$', "KALSHI_USE_DEMO=true" `
    -replace '(?m)^AUTO_PUBLISH=.*$', "AUTO_PUBLISH=false"
Set-Content -Path $envPath -Value $envContent -NoNewline
Say "  .env written with your Key ID + demo mode + dry-run (no publish)." Green

# ------------- STEP 5: Install dependencies -------------
Say ""
Say "[4/7] Installing Python dependencies (this takes ~2 minutes)..." Cyan
Set-Location $here   # defensive: ensure we're in the folder with pyproject.toml
if (-not (Test-Path "pyproject.toml")) {
    Fail "pyproject.toml not found in $(Get-Location). Something drifted the working directory."
}
python -m pip install --quiet --upgrade pip 2>&1 | Out-Host
if ($LASTEXITCODE -ne 0) { Fail "pip upgrade failed. Check internet." }

python -m pip install --quiet -e ".[dev]" 2>&1 | Out-Host
if ($LASTEXITCODE -ne 0) { Fail "Dependency install failed. Check internet and Python version (need 3.11+)." }
Say "  Done." Green

# ------------- STEP 6: Run the test suite (sanity check) -------------
Say ""
Say "[5/7] Running the test suite..." Cyan
python -m pytest tests/ -q 2>&1 | Out-Host
if ($LASTEXITCODE -ne 0) { Fail "Tests failed. Stopping before the probe -- something is broken." }

# ------------- STEP 7: Run the probe -------------
Say ""
Say "[6/7] Running the probe (takes ~3-4 minutes)..." Cyan
Say "  Probes 1-3 run now. Probe 4 (end-to-end loop) is deferred to" Gray
Say "  the production paper-trading phase per plan." Gray
Say ""
python -m kalshi_arb.probe.probe 2>&1 | Out-Host
if ($LASTEXITCODE -ne 0) {
    Fail "Probe failed. Open logs\kalshi-arb.log and paste the last 40 lines to Claude."
}

# ------------- STEP 8: Open the result so user can copy it -------------
$resultPath = Join-Path $here "config\detected_limits.yaml"
if (-not (Test-Path $resultPath)) {
    Fail "Probe finished but didn't write the result file. Check logs\kalshi-arb.log."
}

Say ""
Say "[7/7] Opening the result file for you..." Cyan
Start-Process notepad.exe $resultPath

Say ""
Say "============================================================" Green
Say "  DONE." Green
Say "============================================================" Green
Say ""
Say "  A Notepad window just opened with your probe results." White
Say ""
Say "  Next:" White
Say "    1. Select all the text in Notepad (Ctrl+A)" White
Say "    2. Copy it (Ctrl+C)" White
Say "    3. Paste it back to Claude in chat" White
Say ""
Say "  Claude will verify no account data leaked and approve" White
Say "  flipping AUTO_PUBLISH=true for the real run." White
Say ""

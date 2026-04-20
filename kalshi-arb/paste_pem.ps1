# Prompts the user to paste the PEM contents via a multi-line text box and
# saves the result as kalshi-demo.pem in the script's folder.

$ErrorActionPreference = "Stop"
$here = $PSScriptRoot
if (-not $here) { $here = Split-Path -Parent $MyInvocation.MyCommand.Path }
Set-Location $here

Add-Type -AssemblyName System.Windows.Forms
Add-Type -AssemblyName System.Drawing

$form = New-Object System.Windows.Forms.Form
$form.Text = "Paste kalshi-demo.pem contents"
$form.Size = New-Object System.Drawing.Size(700, 560)
$form.StartPosition = "CenterScreen"

$label = New-Object System.Windows.Forms.Label
$label.Location = New-Object System.Drawing.Point(12, 10)
$label.Size = New-Object System.Drawing.Size(660, 60)
$label.Text = ("Paste the ENTIRE contents of your kalshi-demo.pem file below.`r`n" +
               "It starts with '-----BEGIN RSA PRIVATE KEY-----' and ends with " +
               "'-----END RSA PRIVATE KEY-----'. Click SAVE when done.")
$form.Controls.Add($label)

$textbox = New-Object System.Windows.Forms.TextBox
$textbox.Multiline = $true
$textbox.ScrollBars = "Vertical"
$textbox.Font = New-Object System.Drawing.Font("Consolas", 9)
$textbox.Location = New-Object System.Drawing.Point(12, 80)
$textbox.Size = New-Object System.Drawing.Size(660, 380)
$textbox.AcceptsReturn = $true
$form.Controls.Add($textbox)

$saveBtn = New-Object System.Windows.Forms.Button
$saveBtn.Text = "SAVE"
$saveBtn.Location = New-Object System.Drawing.Point(500, 470)
$saveBtn.Size = New-Object System.Drawing.Size(80, 30)
$saveBtn.DialogResult = [System.Windows.Forms.DialogResult]::OK
$form.Controls.Add($saveBtn)
$form.AcceptButton = $saveBtn

$cancelBtn = New-Object System.Windows.Forms.Button
$cancelBtn.Text = "Cancel"
$cancelBtn.Location = New-Object System.Drawing.Point(590, 470)
$cancelBtn.Size = New-Object System.Drawing.Size(80, 30)
$cancelBtn.DialogResult = [System.Windows.Forms.DialogResult]::Cancel
$form.Controls.Add($cancelBtn)
$form.CancelButton = $cancelBtn

$result = $form.ShowDialog()
if ($result -ne "OK") {
    Write-Host "Cancelled. No file written."
    exit 1
}

$pem = $textbox.Text.Trim()
if (-not $pem.StartsWith("-----BEGIN")) {
    Write-Host "That does not look like a PEM file (must start with -----BEGIN ...-----). Nothing saved." -ForegroundColor Red
    exit 1
}
if (-not $pem.Contains("-----END")) {
    Write-Host "That does not look like a complete PEM file (missing -----END ...-----). Nothing saved." -ForegroundColor Red
    exit 1
}

# Normalize line endings to LF (PEM files typically use LF, and pykalshi
# tolerates either, but stripping CRLF avoids subtle cryptography errors).
$pem = $pem -replace "`r`n", "`n"

$outPath = Join-Path $here "kalshi-demo.pem"
[System.IO.File]::WriteAllText($outPath, $pem + "`n")
Write-Host ""
Write-Host "Saved to $outPath" -ForegroundColor Green
Write-Host ""
Write-Host "Now double-click ONE_CLICK.bat to continue." -ForegroundColor Cyan

# Fringe Business System - Owner Creation Script (PowerShell)
# Usage:
#   .\create_owner.ps1
#   .\create_owner.ps1 --non-interactive
#   .\create_owner.ps1 --username owner --email owner@example.com --password "MySecret@123" --pin 1234

$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $scriptDir

# Prefer the local virtualenv 'fringe'
if (Test-Path "$scriptDir\fringe\Scripts\python.exe") {
    $python = "$scriptDir\fringe\Scripts\python.exe"
} else {
    $python = "python"
}

Write-Host "Running Owner Initialization via Django Management Command..." -ForegroundColor Cyan
& $python "$scriptDir\manage.py" create_owner @args

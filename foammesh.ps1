# FoamMesh launcher (Windows PowerShell)
# Renamed to foammesh.ps1 in phase 01; entry module renamed baramMesh -> foammesh in phase 01.

$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Definition
Set-Location $ScriptDir

# Activate a local venv if present
if (Test-Path "venv\Scripts\Activate.ps1") { . "venv\Scripts\Activate.ps1" }

# src/ (first-party) + vendor/ (PyFoam) source roots on the import path
$env:PYTHONPATH = "src;vendor" + $(if ($env:PYTHONPATH) { ";$env:PYTHONPATH" } else { "" })

python -m foammesh.main

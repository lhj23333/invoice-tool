param([Parameter(Mandatory = $true)][string]$Python)
$ErrorActionPreference = 'Stop'
& $Python -c 'import sys, tkinter; assert sys.version_info[:2] == (3,12), "Python 3.12 is required"'
if ($LASTEXITCODE -ne 0) { throw 'Please use Windows Python 3.12 with Tcl/Tk installed.' }
& $Python -m venv (Join-Path $PSScriptRoot '.venv')
if ($LASTEXITCODE -ne 0) { throw 'Virtual environment creation failed.' }
& (Join-Path $PSScriptRoot '.venv\Scripts\python.exe') -m pip install -r (Join-Path $PSScriptRoot 'requirements.txt')
if ($LASTEXITCODE -ne 0) { throw 'Dependency installation failed. Check your network and package index.' }
Write-Host 'Setup complete. Run start.ps1.'

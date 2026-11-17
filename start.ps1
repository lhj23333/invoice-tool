param([string]$Python, [string]$DataDirectory, [switch]$RecoverLock)
$ErrorActionPreference = 'Stop'
if (-not $Python) {
    if (Test-Path -LiteralPath (Join-Path $PSScriptRoot 'runtime\pythonw.exe')) {
        $Python = Join-Path $PSScriptRoot 'runtime\pythonw.exe'
    } elseif (Test-Path -LiteralPath (Join-Path $PSScriptRoot '.venv\Scripts\pythonw.exe')) {
        $Python = Join-Path $PSScriptRoot '.venv\Scripts\pythonw.exe'
    } else {
        throw 'Run setup.ps1 with Python 3.12, or use the portable InvoiceWorkbench.exe build.'
    }
}
$launchArgs = @((Join-Path $PSScriptRoot 'app.py'))
if ($DataDirectory) { $launchArgs += @('--data-dir', $DataDirectory) }
if ($RecoverLock) { $launchArgs += '--recover-lock' }
& $Python @launchArgs

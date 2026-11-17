param([string]$Python, [string]$DataDirectory, [switch]$RecoverLock, [switch]$CheckEnvironment)
$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'tools\python_runtime.ps1')
$runtime = Get-WorkbenchPython -Python $Python -ProjectRoot $PSScriptRoot
Write-Host "Using Python: $($runtime.executable) ($($runtime.platform))"
if ($CheckEnvironment) { return }
$launchArgs = @((Join-Path $PSScriptRoot 'app.py'))
if ($DataDirectory) { $launchArgs += @('--data-dir', $DataDirectory) }
if ($RecoverLock) { $launchArgs += '--recover-lock' }
& $runtime.executable -B @launchArgs
if ($LASTEXITCODE -ne 0) { throw "The program failed to run. Exit code: $LASTEXITCODE. Please check the log errors and startup-error.log." }

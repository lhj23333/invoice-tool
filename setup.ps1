param([string]$Python)
$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'tools\python_runtime.ps1')
$runtime = Get-WorkbenchPython -Python $Python -ProjectRoot $PSScriptRoot -Setup
$environment = Join-Path $PSScriptRoot '.venv'
Write-Host "Using Python: $($runtime.executable) ($($runtime.platform))"
if (Test-Path -LiteralPath (Join-Path $environment 'pyvenv.cfg')) {
    $existingRuntime = Get-WorkbenchPython -ProjectRoot $PSScriptRoot -Setup
    if ($existingRuntime.prefix -ine $environment -or $existingRuntime.base_prefix -ine $runtime.base_prefix) {
        throw 'There is a .venv using a different Python installation. Please remove it first, then run setup.ps1 with the specified Python.'
    }
}
if ([System.IO.Path]::GetFullPath($runtime.prefix) -ne [System.IO.Path]::GetFullPath($environment)) {
    & $runtime.executable -B -m venv $environment
    if ($LASTEXITCODE -ne 0) { throw 'Failed to create Python virtual environment. Please check the log errors.' }
}
$environmentRuntime = Get-WorkbenchPython -Python $runtime.environment_python -ProjectRoot $PSScriptRoot -Setup
Initialize-WorkbenchPip -Python $environmentRuntime.executable
& $environmentRuntime.executable -B -m pip install -r (Join-Path $PSScriptRoot 'requirements.txt')
if ($LASTEXITCODE -ne 0) { throw "Failed to install dependencies ($($environmentRuntime.platform)). Please check the log errors." }
& $environmentRuntime.executable -B (Join-Path $PSScriptRoot 'tools\build_ofd.py')
if ($LASTEXITCODE -ne 0) { throw 'Failed to build the local OFD renderer. Please check the log errors.' }
$null = Get-WorkbenchPython -Python $environmentRuntime.executable -ProjectRoot $PSScriptRoot
Write-Host 'Environment installation completed. Run .\start.ps1 to start the program.'

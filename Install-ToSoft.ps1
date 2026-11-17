param([string]$Destination = 'C:\Users\lhj32\soft\InvoiceWorkbench')
$ErrorActionPreference = 'Stop'
$sourcePath = [System.IO.Path]::GetFullPath($PSScriptRoot)
$destinationPath = [System.IO.Path]::GetFullPath($Destination)
if ($destinationPath -eq $sourcePath -or $destinationPath.StartsWith($sourcePath + [System.IO.Path]::DirectorySeparatorChar, [System.StringComparison]::OrdinalIgnoreCase)) {
    throw 'Destination must be outside the source directory.'
}
if (Test-Path -LiteralPath $destinationPath) {
    throw "Destination already exists. Nothing was overwritten: $destinationPath"
}
New-Item -ItemType Directory -Path $destinationPath | Out-Null
$skip = @('.git', '.venv', '.deps', '.test-tmp', '__pycache__', 'dist', 'data', 'demo-data', 'local-settings.json', 'startup-error.log', 'test-results.txt')
foreach ($entry in Get-ChildItem -LiteralPath $sourcePath -Force) {
    if ($skip -notcontains $entry.Name -and $entry.Name -notlike '.test-*') {
        Copy-Item -LiteralPath $entry.FullName -Destination $destinationPath -Recurse
    }
}
Write-Host "Project copied to: $destinationPath"
Write-Host 'Open InvoiceWorkbench.exe in the destination directory.'

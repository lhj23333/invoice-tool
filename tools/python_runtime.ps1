function Invoke-WorkbenchPythonProbe {
    param([string]$Path, [string[]]$Arguments)
    $info = [System.Diagnostics.ProcessStartInfo]::new()
    $info.FileName = $Path
    $info.Arguments = ($Arguments | ForEach-Object { '"' + $_ + '"' }) -join ' '
    $info.UseShellExecute = $false
    $info.CreateNoWindow = $true
    $info.RedirectStandardOutput = $true
    $info.RedirectStandardError = $true
    $info.StandardOutputEncoding = [System.Text.Encoding]::UTF8
    $info.StandardErrorEncoding = [System.Text.Encoding]::UTF8
    $info.EnvironmentVariables['PYTHONIOENCODING'] = 'utf-8'
    $process = [System.Diagnostics.Process]::new()
    $process.StartInfo = $info
    try {
        $null = $process.Start()
        $stdout = $process.StandardOutput.ReadToEndAsync()
        $stderr = $process.StandardError.ReadToEndAsync()
        $process.WaitForExit()
        return [PSCustomObject]@{ ExitCode = $process.ExitCode; Output = $stdout.Result.Trim(); Error = $stderr.Result.Trim() }
    } finally {
        $process.Dispose()
    }
}

function Get-WorkbenchPython {
    param(
        [string]$Python,
        [Parameter(Mandatory = $true)][string]$ProjectRoot,
        [switch]$Setup
    )
    $checkScript = Join-Path $PSScriptRoot 'check_python.py'
    $candidates = [System.Collections.Generic.List[string]]::new()
    $failures = [System.Collections.Generic.List[string]]::new()
    if ($Python) {
        $candidates.Add($Python)
    } else {
        $roots = @()
        if (-not $Setup) { $roots += (Join-Path $ProjectRoot 'runtime') }
        $roots += (Join-Path $ProjectRoot '.venv')
        $roots += @($env:VIRTUAL_ENV, $env:CONDA_PREFIX) | Where-Object { $_ }
        foreach ($root in $roots) {
            foreach ($relative in @('python.exe', 'Scripts\python.exe', 'bin\python.exe', 'bin\python3.exe')) {
                $path = Join-Path $root $relative
                if (Test-Path -LiteralPath $path -PathType Leaf) { $candidates.Add($path) }
            }
        }
        foreach ($name in @('python', 'python3', 'python3.*.exe')) {
            foreach ($command in (Get-Command $name -All -CommandType Application -ErrorAction SilentlyContinue)) {
                if ($command.Source -notlike '*\Microsoft\WindowsApps\*') { $candidates.Add($command.Source) }
            }
        }
        foreach ($registryRoot in @('HKCU:\Software\Python', 'HKLM:\Software\Python', 'HKLM:\Software\WOW6432Node\Python')) {
            foreach ($company in (Get-ChildItem -LiteralPath $registryRoot -ErrorAction SilentlyContinue)) {
                foreach ($tag in (Get-ChildItem -LiteralPath $company.PSPath -ErrorAction SilentlyContinue)) {
                    $installPath = Get-Item -LiteralPath (Join-Path $tag.PSPath 'InstallPath') -ErrorAction SilentlyContinue
                    if ($installPath) {
                        $executable = $installPath.GetValue('ExecutablePath')
                        if (-not $executable -and $installPath.GetValue('')) {
                            $executable = Join-Path $installPath.GetValue('') 'python.exe'
                        }
                        if ($executable) { $candidates.Add($executable) }
                    }
                }
            }
        }
        foreach ($launcher in (Get-Command py -All -CommandType Application -ErrorAction SilentlyContinue)) {
            $probe = Invoke-WorkbenchPythonProbe -Path $launcher.Source -Arguments @('-3', '-c', 'import sys; print(sys.executable)')
            if ($probe.ExitCode -eq 0) {
                $candidates.Add($probe.Output)
            } else {
                $failures.Add("$($launcher.Source): $($probe.Error)")
            }
        }
    }

    $seen = [System.Collections.Generic.HashSet[string]]::new([System.StringComparer]::OrdinalIgnoreCase)
    foreach ($candidate in $candidates) {
        $command = Get-Command $candidate -CommandType Application -ErrorAction SilentlyContinue | Select-Object -First 1
        if (-not $command) {
            $failures.Add("${candidate}: 找不到 Python 可执行文件。")
            continue
        }
        $path = $command.Source
        if ([System.IO.Path]::GetFileName($path) -ieq 'pythonw.exe') {
            $path = Join-Path (Split-Path -Parent $path) 'python.exe'
            if (-not (Test-Path -LiteralPath $path -PathType Leaf)) {
                $failures.Add("${candidate}: 需要同目录下的 python.exe 以检查环境并显示错误。")
                continue
            }
        }
        if (-not $seen.Add($path)) { continue }
        $arguments = @('-B', $checkScript)
        if ($Setup) { $arguments += @('--setup', '--environment', (Join-Path $ProjectRoot '.venv')) }
        $probe = Invoke-WorkbenchPythonProbe -Path $path -Arguments $arguments
        if ($probe.ExitCode -eq 0) { return ($probe.Output | ConvertFrom-Json) }
        $failures.Add("${path}: $($probe.Error)")
    }
    $details = $failures -join [Environment]::NewLine
    throw "没有找到满足项目版本及模块要求的 Windows Python 环境。请运行 .\setup.ps1 安装项目依赖，或使用 -Python 指定解释器。$([Environment]::NewLine)$details"
}

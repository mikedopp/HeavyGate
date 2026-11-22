# Puts a `heavy` command on your PATH that runs this copy of HeavyGate. No admin rights needed.
#   .\install.ps1 [-BinDir <folder>] [-NoPath]     default folder: %LOCALAPPDATA%\HeavyGate\bin
param(
    [string]$BinDir = (Join-Path $env:LOCALAPPDATA 'HeavyGate\bin'),
    [switch]$NoPath
)
$ErrorActionPreference = 'Stop'
if (-not (Get-Command python -ErrorAction SilentlyContinue)) { throw 'HeavyGate needs Python 3.9 or newer on the PATH' }
& python -c "import sys; sys.exit(sys.version_info < (3, 9))"
if ($LASTEXITCODE -ne 0) { throw 'HeavyGate needs Python 3.9 or newer' }

New-Item -ItemType Directory -Force -Path $BinDir | Out-Null
$gate = Join-Path $PSScriptRoot 'heavygate.py'
$cmd = Join-Path $BinDir 'heavy.cmd'
Set-Content -Path $cmd -Encoding ascii -Value "@python `"$gate`" %*`r`n@exit /b %errorlevel%"
Write-Output "Installed $cmd"

if (-not $NoPath) {
    $path = [Environment]::GetEnvironmentVariable('Path', 'User')
    if (($path -split ';') -notcontains $BinDir) {
        [Environment]::SetEnvironmentVariable('Path', ($path.TrimEnd(';') + ';' + $BinDir), 'User')
        Write-Output "Added $BinDir to your user PATH. Open a new terminal to use heavy."
    }
}

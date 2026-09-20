$ErrorActionPreference = 'Stop'
$ProjectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$Python = Join-Path $ProjectRoot '.venv\Scripts\python.exe'
$DistDir = Join-Path $ProjectRoot 'dist'
$BuildDir = Join-Path $ProjectRoot 'build'

if (-not (Test-Path -LiteralPath $Python)) {
    throw 'Virtual environment not found. Run setup.ps1 first.'
}

& $Python -m PyInstaller `
    --noconfirm `
    --clean `
    --onefile `
    --windowed `
    --name 'BenchmarkCompanion' `
    --distpath $DistDir `
    --workpath $BuildDir `
    --specpath $BuildDir `
    (Join-Path $ProjectRoot 'app.py')

Copy-Item -LiteralPath (Join-Path $ProjectRoot 'config.json') -Destination (Join-Path $DistDir 'config.json') -Force
Write-Host "Build complete: $DistDir\BenchmarkCompanion.exe"


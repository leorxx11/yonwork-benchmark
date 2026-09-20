$ErrorActionPreference = 'Stop'
$ProjectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$Python = Join-Path $ProjectRoot '.venv\Scripts\pythonw.exe'

if (-not (Test-Path -LiteralPath $Python)) {
    throw "Virtual environment not found. Run setup.ps1 first."
}

$AppScript = Join-Path $ProjectRoot 'app.py'
$QuotedAppScript = '"' + $AppScript + '"'
Start-Process -FilePath $Python -ArgumentList $QuotedAppScript -WorkingDirectory $ProjectRoot -WindowStyle Hidden

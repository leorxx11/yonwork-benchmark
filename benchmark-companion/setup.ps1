$ErrorActionPreference = 'Stop'
$ProjectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$BundledPython = 'C:\Users\Administrator\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe'
$VenvPython = Join-Path $ProjectRoot '.venv\Scripts\python.exe'

if (-not (Test-Path -LiteralPath $VenvPython)) {
    if (-not (Test-Path -LiteralPath $BundledPython)) {
        throw 'Bundled Python was not found. Install Python 3.12 and update this script.'
    }
    & $BundledPython -m venv --system-site-packages (Join-Path $ProjectRoot '.venv')
}

$ProxyCommand = Get-Command proxy_on -ErrorAction SilentlyContinue
if ($null -ne $ProxyCommand) {
    & proxy_on
}

& $VenvPython -m pip install -r (Join-Path $ProjectRoot 'requirements.txt')
Write-Host 'Setup complete. Run .\run.ps1 or build the EXE with .\build.ps1.'

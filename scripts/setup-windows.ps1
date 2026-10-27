param([switch]$SkipNode)
$ErrorActionPreference = "Stop"
Set-Location (Split-Path $PSScriptRoot -Parent)

if (!(Get-Command py -ErrorAction SilentlyContinue)) { throw "Install Python 3.12 from python.org, including the Python launcher." }
if (!(Get-Command cargo -ErrorAction SilentlyContinue)) { throw "Install Rust through rustup.rs and restart PowerShell." }
if (!(Test-Path .venv/Scripts/python.exe)) {
    & py -3.12 -m venv .venv
    if ($LASTEXITCODE) { throw "Python virtual environment setup failed." }
}
& .venv/Scripts/python.exe -m pip install --upgrade pip
if ($LASTEXITCODE) { throw "pip upgrade failed." }
& .venv/Scripts/python.exe -m pip install -r requirements.txt pyinstaller pytest
if ($LASTEXITCODE) { throw "Python dependency installation failed." }
if (!$SkipNode) {
    if (!(Get-Command npm -ErrorAction SilentlyContinue)) { throw "Install Node.js 22 LTS or newer." }
    if (Test-Path package-lock.json) { & npm ci } else { & npm install }
    if ($LASTEXITCODE) { throw "Node dependency installation failed." }
}
Write-Host "Dependencies ready. Run npm run desktop:dev or ./scripts/build-windows.ps1."

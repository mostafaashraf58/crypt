param()
$ErrorActionPreference = "Stop"
Set-Location (Split-Path $PSScriptRoot -Parent)

if (!(Test-Path .venv-model/Scripts/python.exe)) {
    if (Get-Command py -ErrorAction SilentlyContinue) {
        & py -3.13 -m venv .venv-model
        if ($LASTEXITCODE) { & py -3.12 -m venv .venv-model }
    } elseif (Get-Command python -ErrorAction SilentlyContinue) {
        & python -m venv .venv-model
    } else {
        throw "Install 64-bit Python 3.12 or 3.13 from python.org, then run this script again."
    }
    if ($LASTEXITCODE) { throw "Could not create the model environment. Install Python 3.12 or 3.13 (64-bit)." }
}
$modelPython = Join-Path (Get-Location) '.venv-model/Scripts/python.exe'
& $modelPython -c "import sys, struct; assert (3,12) <= sys.version_info[:2] <= (3,13) and struct.calcsize('P') == 8, 'Use Python 3.12 or 3.13, 64-bit'"
if ($LASTEXITCODE) { throw "The model requires 64-bit Python 3.12 or 3.13." }
& $modelPython -m pip install --disable-pip-version-check --no-cache-dir torch==2.9.1 --index-url https://download.pytorch.org/whl/cpu
if ($LASTEXITCODE) { throw "CPU PyTorch download failed. Check internet access and rerun this script." }
& $modelPython -m pip install --disable-pip-version-check --no-cache-dir -r requirements-model.txt
if ($LASTEXITCODE) { throw "Model package installation failed. Rerun this script to retry." }
& $modelPython -m model_service.download
if ($LASTEXITCODE) { throw "Model download or test failed. Check the message above and rerun this script." }
Write-Host "Setup complete. Run ./scripts/start-model.ps1, then click Connect local model in Axiom > AI Models."

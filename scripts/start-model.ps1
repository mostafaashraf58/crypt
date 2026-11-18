param()
$ErrorActionPreference = "Stop"
Set-Location (Split-Path $PSScriptRoot -Parent)
if (!(Test-Path .venv-model/Scripts/python.exe) -or !(Test-Path work/local-model/ready.json)) {
    throw "Set up the model first with ./scripts/setup-model.ps1."
}
Write-Host "Keep this window open while using the model. Press Ctrl+C to stop."
& .venv-model/Scripts/python.exe -m model_service.server
if ($LASTEXITCODE) { throw "The model stopped with an error. Check the message above. If files are missing, rerun setup-model.ps1." }

param([switch]$SkipSetup)
$ErrorActionPreference = "Stop"
Set-Location (Split-Path $PSScriptRoot -Parent)
if (!$IsWindows -and $PSVersionTable.PSVersion.Major -ge 6) { throw "The Windows installer must be built on Windows." }
if (!$SkipSetup) { & $PSScriptRoot/setup-windows.ps1 }
& .venv/Scripts/python.exe -m pytest tests -q
if ($LASTEXITCODE) { throw "Python tests failed." }
& npm run test:frontend
if ($LASTEXITCODE) { throw "Frontend logic tests failed." }
& npm run build
if ($LASTEXITCODE) { throw "Frontend build failed." }
& .venv/Scripts/python.exe -m PyInstaller --noconfirm --clean --onefile --console --name axiom-engine --paths . --collect-all keyring --collect-all duckdb --collect-all uvicorn --collect-all websockets --collect-all httpx --distpath work/engine-bundle --workpath work/pyinstaller --specpath work engine/desktop_entry.py
if ($LASTEXITCODE) { throw "Engine bundling failed." }
$target = (& rustc -vV | Select-String '^host: ').ToString().Substring(6).Trim()
New-Item -ItemType Directory -Force src-tauri/binaries | Out-Null
Copy-Item work/engine-bundle/axiom-engine.exe "src-tauri/binaries/axiom-engine-$target.exe" -Force
& npm run tauri -- build
if ($LASTEXITCODE) { throw "Tauri installer build failed." }
Write-Host "Installer: src-tauri/target/release/bundle/nsis/"

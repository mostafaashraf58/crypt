[CmdletBinding()]
param([string]$InstallerPath)
$ErrorActionPreference = 'Stop'
if (!$InstallerPath) { $InstallerPath = (Get-ChildItem 'src-tauri/target/release/bundle/nsis/*-setup.exe' | Select-Object -First 1).FullName }
if (!(Test-Path $InstallerPath)) { throw 'Installer artifact is missing.' }
$installDirectory = Join-Path $env:LOCALAPPDATA ('Axiom-Smoke-' + [guid]::NewGuid().ToString('N'))
$app = $null
function Get-TestEngineIds([int]$DesktopProcessId) {
    # Packaged hosts may redirect LOCALAPPDATA paths. Identify ownership through
    # the desktop's child tree, including PyInstaller's bootloader and worker.
    $engines = @(Get-CimInstance Win32_Process -Filter "Name = 'axiom-engine.exe'")
    $owned = New-Object 'System.Collections.Generic.HashSet[int]'
    $null = $owned.Add($DesktopProcessId)
    do {
        $changed = $false
        foreach ($engine in $engines) {
            if ($owned.Contains([int]$engine.ParentProcessId) -and $owned.Add([int]$engine.ProcessId)) { $changed = $true }
        }
    } while ($changed)
    $engines | Where-Object { $owned.Contains([int]$_.ProcessId) } | Select-Object -ExpandProperty ProcessId
}
try {
    $setup = Start-Process -FilePath $InstallerPath -ArgumentList @('/S', "/D=$installDirectory") -WindowStyle Hidden -Wait -PassThru
    if ($setup.ExitCode -ne 0) { throw "Installer exited with $($setup.ExitCode)." }
    $executable = Join-Path $installDirectory 'axiom-workstation.exe'
    if (!(Test-Path $executable)) { throw 'Installed desktop executable is missing.' }
    if (!(Test-Path (Join-Path $installDirectory 'axiom-engine.exe'))) { throw 'Installed Python sidecar is missing.' }
    $app = Start-Process -FilePath $executable -WindowStyle Hidden -PassThru
    $ready = $false
    $deadline = (Get-Date).AddSeconds(65)
    while ((Get-Date) -lt $deadline) {
        $app.Refresh()
        if ($app.HasExited) { throw 'Installed desktop exited during startup.' }
        $engineIds = @(Get-TestEngineIds $app.Id)
        Write-Verbose "Installed engine process IDs: $($engineIds -join ', ')"
        foreach ($engineProcessId in $engineIds) {
            $listeners = @(Get-NetTCPConnection -State Listen -OwningProcess $engineProcessId -ErrorAction SilentlyContinue)
            Write-Verbose "Engine $engineProcessId listener count: $($listeners.Count)"
            foreach ($listener in $listeners) {
                if ($listener.LocalAddress -ne '127.0.0.1') { throw 'Engine listener is exposed beyond loopback.' }
                try { $health = Invoke-RestMethod -Uri "http://127.0.0.1:$($listener.LocalPort)/health" -TimeoutSec 2 } catch { Write-Verbose "Health probe failed: $($_.Exception.Message)"; continue }
                if ($health.status -eq 'ok') {
                    $statusCode = $null
                    try {
                        $response = Invoke-WebRequest -Uri "http://127.0.0.1:$($listener.LocalPort)/snapshot" -UseBasicParsing -TimeoutSec 2
                        $statusCode = [int]$response.StatusCode
                    } catch {
                        if ($_.Exception.Response) { $statusCode = [int]$_.Exception.Response.StatusCode } else { throw }
                    }
                    if ($statusCode -ne 401) { throw 'Installed engine failed its unauthorized request check.' }
                    $ready = $true
                }
            }
        }
        if ($ready) { break }
        Start-Sleep -Milliseconds 500
    }
    if (!$ready) { throw 'Installed local engine did not become healthy within 65 seconds.' }
    Write-Host 'PASS silent installation, desktop launch, bundled engine health, loopback isolation, and authentication.'
    $engineProcesses = @(Get-Process -Id $engineIds -ErrorAction SilentlyContinue)
    $null = $app.CloseMainWindow()
    if (!$app.WaitForExit(15000)) { throw 'Desktop did not close normally.' }
    Start-Sleep -Seconds 2
    foreach ($engineProcess in $engineProcesses) {
        if (!$engineProcess.WaitForExit(10000)) { throw 'Python sidecar survived desktop shutdown.' }
    }
    Write-Host 'PASS desktop shutdown cleans up Python sidecar processes.'
} finally {
    if ($app -and !$app.HasExited) { Stop-Process -Id $app.Id -Force -ErrorAction SilentlyContinue }
    $uninstaller = Join-Path $installDirectory 'uninstall.exe'
    if (Test-Path $uninstaller) {
        $removed = Start-Process -FilePath $uninstaller -ArgumentList '/S' -WindowStyle Hidden -Wait -PassThru
        if ($removed.ExitCode -ne 0) { throw 'Silent uninstall failed.' }
    }
}
if (Test-Path (Join-Path $installDirectory 'axiom-workstation.exe')) { throw 'Desktop executable remains after uninstall.' }
Write-Host 'PASS silent uninstall.'

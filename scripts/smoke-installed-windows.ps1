param([string]$InstallerPath)
$ErrorActionPreference = 'Stop'
if (!$InstallerPath) { $InstallerPath = (Get-ChildItem 'src-tauri/target/release/bundle/nsis/*-setup.exe' | Select-Object -First 1).FullName }
if (!(Test-Path $InstallerPath)) { throw 'Installer artifact is missing.' }
$installDirectory = Join-Path $env:LOCALAPPDATA ('Axiom-Smoke-' + [guid]::NewGuid().ToString('N'))
$app = $null
try {
    $setup = Start-Process -FilePath $InstallerPath -ArgumentList @('/S', "/D=$installDirectory") -Wait -PassThru
    if ($setup.ExitCode -ne 0) { throw "Installer exited with $($setup.ExitCode)." }
    $executable = Join-Path $installDirectory 'axiom-workstation.exe'
    if (!(Test-Path $executable)) { throw 'Installed desktop executable is missing.' }
    if (!(Test-Path (Join-Path $installDirectory 'axiom-engine.exe'))) { throw 'Installed Python sidecar is missing.' }
    $app = Start-Process -FilePath $executable -PassThru
    $ready = $false
    $deadline = (Get-Date).AddSeconds(65)
    while ((Get-Date) -lt $deadline) {
        $app.Refresh()
        if ($app.HasExited) { throw 'Installed desktop exited during startup.' }
        $engineIds = @(Get-Process -Name 'axiom-engine' -ErrorAction SilentlyContinue | Select-Object -ExpandProperty Id)
        foreach ($engineProcessId in $engineIds) {
            $listeners = @(Get-NetTCPConnection -State Listen -OwningProcess $engineProcessId -ErrorAction SilentlyContinue)
            foreach ($listener in $listeners) {
                if ($listener.LocalAddress -ne '127.0.0.1') { throw 'Engine listener is exposed beyond loopback.' }
                try { $health = Invoke-RestMethod -Uri "http://127.0.0.1:$($listener.LocalPort)/health" -TimeoutSec 2 } catch { continue }
                if ($health.status -eq 'ok') {
                    $response = Invoke-WebRequest -Uri "http://127.0.0.1:$($listener.LocalPort)/snapshot" -SkipHttpErrorCheck -TimeoutSec 2
                    if ([int]$response.StatusCode -ne 401) { throw 'Installed engine failed its unauthorized request check.' }
                    $ready = $true
                }
            }
        }
        if ($ready) { break }
        Start-Sleep -Milliseconds 500
    }
    if (!$ready) { throw 'Installed local engine did not become healthy within 65 seconds.' }
    Write-Host 'PASS silent installation, desktop launch, bundled engine health, loopback isolation, and authentication.'
    $null = $app.CloseMainWindow()
    if (!$app.WaitForExit(15000)) { throw 'Desktop did not close normally.' }
    Start-Sleep -Seconds 2
    if (Get-Process -Name 'axiom-engine' -ErrorAction SilentlyContinue) { throw 'Python sidecar survived desktop shutdown.' }
    Write-Host 'PASS desktop shutdown cleans up Python sidecar processes.'
} finally {
    if ($app -and !$app.HasExited) { Stop-Process -Id $app.Id -Force -ErrorAction SilentlyContinue }
    $uninstaller = Join-Path $installDirectory 'uninstall.exe'
    if (Test-Path $uninstaller) {
        $removed = Start-Process -FilePath $uninstaller -ArgumentList '/S' -Wait -PassThru
        if ($removed.ExitCode -ne 0) { throw 'Silent uninstall failed.' }
    }
}
if (Test-Path (Join-Path $installDirectory 'axiom-workstation.exe')) { throw 'Desktop executable remains after uninstall.' }
Write-Host 'PASS silent uninstall.'

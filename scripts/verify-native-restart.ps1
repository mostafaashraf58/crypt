[CmdletBinding()]
param()
$ErrorActionPreference='Stop'
$workspace=[IO.Path]::GetFullPath((Split-Path $PSScriptRoot -Parent))
Set-Location -LiteralPath $workspace
$qaRoot=Join-Path $workspace ('work/native-qa-'+[guid]::NewGuid().ToString('N'))
$qaRoot=[IO.Path]::GetFullPath($qaRoot)
if (!$qaRoot.StartsWith((Join-Path $workspace 'work')+[IO.Path]::DirectorySeparatorChar,[StringComparison]::OrdinalIgnoreCase)) { throw 'QA path escaped workspace.' }
New-Item -ItemType Directory -Force -Path $qaRoot | Out-Null
$exe=Join-Path $workspace 'src-tauri/target/release/axiom-workstation.exe'
if (!(Test-Path -LiteralPath $exe)) { throw 'Build the desktop first.' }
$oldData=$env:AXIOM_DATA_DIR
$oldWeb=$env:WEBVIEW2_USER_DATA_FOLDER
$oldTemp=$env:TEMP
$oldTmp=$env:TMP
$app=$null
try {
    $env:AXIOM_DATA_DIR=Join-Path $qaRoot 'data'
    $env:WEBVIEW2_USER_DATA_FOLDER=Join-Path $qaRoot 'webview'
    $env:TEMP=Join-Path $qaRoot 'temp'
    $env:TMP=$env:TEMP
    New-Item -ItemType Directory -Force -Path $env:TEMP | Out-Null
    & .venv/Scripts/python.exe scripts/verify-persisted-data.py seed $env:AXIOM_DATA_DIR
    if ($LASTEXITCODE) { throw 'QA data seeding failed.' }
    foreach ($cycle in 1..2) {
        $app=Start-Process -FilePath $exe -WindowStyle Hidden -PassThru
        $ready=$false
        $engineIds=@()
        $deadline=(Get-Date).AddSeconds(60)
        while ((Get-Date)-lt $deadline) {
            $app.Refresh()
            if ($app.HasExited) { throw 'Native desktop exited before engine readiness.' }
            $processes=@(Get-CimInstance Win32_Process -Filter "Name = 'axiom-engine.exe'")
            $owned=New-Object 'System.Collections.Generic.HashSet[int]'
            $null=$owned.Add($app.Id)
            do {
                $changed=$false
                foreach($process in $processes) {
                    if($owned.Contains([int]$process.ParentProcessId)-and $owned.Add([int]$process.ProcessId)) {$changed=$true}
                }
            } while($changed)
            $engineIds=@($processes | Where-Object {$owned.Contains([int]$_.ProcessId)} | Select-Object -ExpandProperty ProcessId)
            foreach($engineId in $engineIds) {
                foreach($listener in @(Get-NetTCPConnection -State Listen -OwningProcess $engineId -ErrorAction SilentlyContinue)) {
                    if($listener.LocalAddress-ne '127.0.0.1') {throw 'Engine exposed a non-loopback listener.'}
                    try {$health=Invoke-RestMethod -Uri "http://127.0.0.1:$($listener.LocalPort)/health" -TimeoutSec 2} catch {continue}
                    if($health.version-ne '0.2.1') {throw 'Wrong engine version.'}
                    $code=$null
                    try {$response=Invoke-WebRequest -Uri "http://127.0.0.1:$($listener.LocalPort)/snapshot" -UseBasicParsing -TimeoutSec 2; $code=[int]$response.StatusCode} catch {if($_.Exception.Response){$code=[int]$_.Exception.Response.StatusCode}else{throw}}
                    if($code-ne 401){throw 'Unauthenticated financial state was exposed.'}
                    $ready=$true
                }
            }
            if($ready){break}
            Start-Sleep -Milliseconds 400
        }
        if(!$ready){throw 'Native engine did not become ready.'}
        Write-Host "PASS native cycle ${cycle}: engine version, health, loopback binding and session authentication"
        $children=@(Get-Process -Id $engineIds -ErrorAction SilentlyContinue)
        $null=$app.CloseMainWindow()
        if(!$app.WaitForExit(15000)){throw 'Native close did not complete.'}
        foreach($child in $children){if(!$child.WaitForExit(10000)){throw 'Sidecar survived native shutdown.'}}
        & .venv/Scripts/python.exe scripts/verify-persisted-data.py check $env:AXIOM_DATA_DIR
        if($LASTEXITCODE){throw 'Native restart lost saved data.'}
        Write-Host "PASS native cycle ${cycle}: shutdown cleanup and complete saved-data preservation"
    }
    Write-Host "QA evidence preserved at $qaRoot"
} finally {
    if($app-and !$app.HasExited){Stop-Process -Id $app.Id -Force -ErrorAction SilentlyContinue}
    $env:AXIOM_DATA_DIR=$oldData
    $env:WEBVIEW2_USER_DATA_FOLDER=$oldWeb
    $env:TEMP=$oldTemp
    $env:TMP=$oldTmp
}

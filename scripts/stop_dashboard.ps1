$ErrorActionPreference = "Stop"
$projectRoot = Split-Path -Parent $PSScriptRoot
$pidFile = Join-Path $projectRoot "runtime\dashboard-processes.json"

if (-not (Test-Path $pidFile)) {
    Write-Host "No managed dashboard processes found."
    exit 0
}

$saved = Get-Content $pidFile -Raw | ConvertFrom-Json
foreach ($id in @($saved.servicePid, $saved.receiverPid)) {
    if ($id -and (Get-Process -Id $id -ErrorAction SilentlyContinue)) {
        Stop-Process -Id $id
        Write-Host "Stopped PID $id."
    }
}
Remove-Item $pidFile -ErrorAction SilentlyContinue

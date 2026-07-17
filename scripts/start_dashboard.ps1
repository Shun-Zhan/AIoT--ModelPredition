<#!
.SYNOPSIS
Starts the local forecast service, ESP32 receiver, and full-screen dashboard.
#>

param(
    [string]$EspSerialPort = ""
)

$ErrorActionPreference = "Stop"
$projectRoot = Split-Path -Parent $PSScriptRoot
$venvPython = Join-Path $projectRoot ".venv\Scripts\python.exe"
$forecastCli = Join-Path $projectRoot ".venv\Scripts\dual-forecast.exe"
$runtimeDir = Join-Path $projectRoot "runtime"
$logDir = Join-Path $runtimeDir "logs"
$pidFile = Join-Path $runtimeDir "dashboard-processes.json"
$dashboardUrl = "http://127.0.0.1:8000/dashboard"

function Test-ManagedProcessAlive($id) {
    if (-not $id) { return $false }
    return $null -ne (Get-Process -Id $id -ErrorAction SilentlyContinue)
}

function Start-ManagedProcess($name, [string[]]$arguments, $existingId) {
    if (Test-ManagedProcessAlive $existingId) {
        Write-Host "$name is already running (PID $existingId)."
        return $existingId
    }

    $stdout = Join-Path $logDir "$name.out.log"
    $stderr = Join-Path $logDir "$name.err.log"
    # Run Python in unbuffered mode so runtime\logs is useful immediately.
    $pythonArguments = @("-u", "-m", "dual_forecast.cli") + $arguments
    $process = Start-Process -FilePath $venvPython -ArgumentList $pythonArguments `
        -WorkingDirectory $projectRoot -WindowStyle Hidden -RedirectStandardOutput $stdout `
        -RedirectStandardError $stderr -PassThru
    Write-Host "Started $name (PID $($process.Id))."
    return $process.Id
}

if (-not (Test-Path $venvPython)) {
    Write-Host "Creating Python virtual environment..."
    & py -m venv (Join-Path $projectRoot ".venv")
    if ($LASTEXITCODE -ne 0) { throw "Failed to create .venv. Install Python 3.10 or later and ensure 'py' works." }
}

# A missing pyserial is expected on the first run.  Check it without raising a
# Python traceback (PowerShell 7 may otherwise treat a non-zero native exit as
# a terminating NativeCommandError before the installer below can run).
$nativeErrorPreference = Get-Variable PSNativeCommandUseErrorActionPreference -ErrorAction SilentlyContinue
$previousNativeErrorPreference = if ($nativeErrorPreference) { $nativeErrorPreference.Value } else { $null }
if ($nativeErrorPreference) { $PSNativeCommandUseErrorActionPreference = $false }
try {
    & $venvPython -c "import importlib.util, sys; sys.exit(0 if importlib.util.find_spec('serial') else 1)" 2>$null
    $serialDependencyReady = $LASTEXITCODE -eq 0
} finally {
    if ($nativeErrorPreference) { $PSNativeCommandUseErrorActionPreference = $previousNativeErrorPreference }
}
if (-not (Test-Path $forecastCli) -or -not $serialDependencyReady) {
    Write-Host "Installing project dependencies (first run or updated dependencies)..."
    & $venvPython -m pip install --upgrade pip
    & $venvPython -m pip install -r (Join-Path $projectRoot "requirements.txt")
    if ($LASTEXITCODE -ne 0) { throw "Dependency installation failed. See the error above." }
}

New-Item -ItemType Directory -Force -Path $logDir | Out-Null
$serialPorts = @(Get-CimInstance Win32_SerialPort -ErrorAction SilentlyContinue)
if (-not $EspSerialPort) {
    $usbPorts = @($serialPorts | Where-Object { $_.PNPDeviceID -match "USB|VID_" })
    if ($usbPorts.Count -eq 1) {
        $EspSerialPort = $usbPorts[0].DeviceID
        Write-Host "Detected ESP32 USB serial port: $EspSerialPort"
    } else {
        $available = ($serialPorts | ForEach-Object { "$($_.DeviceID) ($($_.Name))" }) -join "; "
        throw "Cannot determine the ESP32 serial port. Run: .\start_dashboard.cmd -EspSerialPort COM3 . Available ports: $available"
    }
}
$saved = @{}
if (Test-Path $pidFile) {
    try { $saved = Get-Content $pidFile -Raw | ConvertFrom-Json } catch { $saved = @{} }
}

$serviceId = Start-ManagedProcess "forecast-service" @("serve", "--host", "127.0.0.1", "--port", "8000") $saved.servicePid
Start-Sleep -Seconds 2
$receiverId = Start-ManagedProcess "esp32-receiver" @("receive-esp32-serial", "--serial-port", $EspSerialPort) $saved.receiverPid

@{ servicePid = $serviceId; receiverPid = $receiverId } | ConvertTo-Json | Set-Content -Encoding utf8 $pidFile

$browserCandidates = @(
    (Join-Path ${env:ProgramFiles(x86)} "Microsoft\Edge\Application\msedge.exe"),
    (Join-Path $env:ProgramFiles "Microsoft\Edge\Application\msedge.exe"),
    (Join-Path $env:LOCALAPPDATA "Microsoft\Edge\Application\msedge.exe"),
    (Join-Path ${env:ProgramFiles(x86)} "Google\Chrome\Application\chrome.exe"),
    (Join-Path $env:ProgramFiles "Google\Chrome\Application\chrome.exe"),
    (Join-Path $env:LOCALAPPDATA "Google\Chrome\Application\chrome.exe")
) | Where-Object { $_ -and (Test-Path $_) }

 $dashboardOpened = $false
foreach ($browser in $browserCandidates) {
    try {
        Start-Process -FilePath $browser -ArgumentList "--app=$dashboardUrl", "--start-fullscreen"
        $dashboardOpened = $true
        break
    } catch {
        Write-Host "Could not start $browser; trying the next browser."
    }
}

if (-not $dashboardOpened) {
    Start-Process $dashboardUrl
}

Write-Host "Dashboard opened. ESP32 USB serial port: $EspSerialPort"
Write-Host "Logs: $logDir"

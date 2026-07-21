<#!
.SYNOPSIS
Starts the local forecast service, ESP32 receiver, and full-screen dashboard.
#>

param(
    [string]$EspSerialPort = "",
    # Receive telemetry over the ESP32's provisioned Wi-Fi TCP endpoint.
    [switch]$Wifi,
    # "auto" (default) receives the ESP32 local UDP announcement. This works
    # even when DHCP changes the address and a phone hotspot has no mDNS.
    # An IP or esp32-sensors.local remains available as a troubleshooting fallback.
    [string]$EspWifiHost = "auto",
    # Deliberately opt-in: expose the dashboard to phones on the same LAN.
    # Default remains loopback so an ordinary desktop start is not network-open.
    [switch]$Lan
)

$ErrorActionPreference = "Stop"
$projectRoot = Split-Path -Parent $PSScriptRoot
$venvPython = Join-Path $projectRoot ".venv\Scripts\python.exe"
$forecastCli = Join-Path $projectRoot ".venv\Scripts\dual-forecast.exe"
$runtimeDir = Join-Path $projectRoot "runtime"
$logDir = Join-Path $runtimeDir "logs"
$pidFile = Join-Path $runtimeDir "dashboard-processes.json"
$serverHost = if ($Lan) { "0.0.0.0" } else { "127.0.0.1" }
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

function Test-FreshLiveTelemetry {
    try {
        $latest = Invoke-RestMethod -Uri "http://127.0.0.1:8000/v1/dashboard/latest" -TimeoutSec 2
        if (-not $latest.snapshot) { return $false }
        $receivedAt = [DateTimeOffset]::Parse([string]$latest.snapshot.receivedAt)
        return (([DateTimeOffset]::UtcNow - $receivedAt).TotalSeconds -le 15)
    } catch {
        return $false
    }
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
    & $venvPython -c "import importlib.util, sys; sys.exit(0 if all(importlib.util.find_spec(name) for name in ('serial', 'qrcode')) else 1)" 2>$null
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
if (-not $Wifi -and -not $EspSerialPort) {
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

$requestedTransport = if ($Wifi) { "wifi" } else { "usb" }
$previousReceiverId = $saved.receiverPid
$previousTransport = [string]$saved.transport
$previousWifiHost = [string]$saved.espWifiHost
$receiverNeedsRestart = $previousTransport -and (
    $previousTransport -ne $requestedTransport -or
    ($Wifi -and $previousTransport -eq "wifi" -and $previousWifiHost -ne $EspWifiHost)
)
if ((Test-ManagedProcessAlive $previousReceiverId) -and $receiverNeedsRestart) {
    Stop-Process -Id $previousReceiverId -ErrorAction SilentlyContinue
    Write-Host "Stopped previous $previousTransport ESP32 receiver (PID $previousReceiverId) to apply updated connection settings."
    $previousReceiverId = $null
}

$serviceId = Start-ManagedProcess "forecast-service" @("serve", "--host", $serverHost, "--port", "8000") $saved.servicePid
Start-Sleep -Seconds 2
$receiverArgs = if ($Wifi) {
    @("receive-esp32", "--esp-host", $EspWifiHost)
} else {
    @("receive-esp32-serial", "--serial-port", $EspSerialPort)
}
$receiverId = Start-ManagedProcess "esp32-receiver" $receiverArgs $previousReceiverId

@{ servicePid = $serviceId; receiverPid = $receiverId; transport = $requestedTransport; espWifiHost = $EspWifiHost; serialPort = $EspSerialPort } | ConvertTo-Json | Set-Content -Encoding utf8 $pidFile

$liveReady = $false
for ($i = 0; $i -lt 15; $i++) {
    if (Test-FreshLiveTelemetry) {
        $liveReady = $true
        break
    }
    Start-Sleep -Seconds 1
}
if ($liveReady) {
    Write-Host "Live ESP32 telemetry confirmed."
} else {
    if ($Wifi) {
        Write-Warning "Dashboard has not received fresh ESP32 Wi-Fi telemetry yet. Check $logDir\esp32-receiver.out.log and confirm ESP32 joined Wi-Fi so it can announce itself."
    } else {
        Write-Warning "Dashboard has not received fresh ESP32 telemetry yet. Check $logDir\esp32-receiver.out.log and close Arduino Serial Monitor if the port is busy."
    }
}

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

if ($Wifi) {
    if ($EspWifiHost -eq "auto") {
        Write-Host "Dashboard opened. ESP32 Wi-Fi telemetry: automatic local discovery (UDP 3334 -> TCP 3333)"
    } else {
        Write-Host "Dashboard opened. ESP32 Wi-Fi telemetry: $EspWifiHost`:3333"
    }
} else {
    Write-Host "Dashboard opened. ESP32 USB serial port: $EspSerialPort"
}
if ($Lan) {
    Write-Host "LAN mode is enabled. On the phone, use http://<this-PC-IPv4>:8000/dashboard while both devices are on the same Wi-Fi."
    Write-Host "If Windows Firewall asks, allow private-network access only."
}
Write-Host "Logs: $logDir"

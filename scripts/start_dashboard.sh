#!/usr/bin/env bash
# Starts the local forecast API, an ESP32 receiver, and browser dashboard on macOS.
set -euo pipefail

project_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
serial_port=""
wifi=false
# "auto" listens for the ESP32's local UDP announcement. It survives DHCP IP
# changes and does not rely on a hotspot supporting mDNS (.local names).
esp_wifi_host="auto"
open_browser=true
lan=false

usage() {
  cat <<'EOF'
Usage: ./start_dashboard.sh [--serial-port /dev/cu.usbserial...] [--wifi]
                            [--esp-wifi-host auto] [--no-browser] [--lan]

Default: receive telemetry via the ESP32 USB serial port. The USB cable must
stay connected and Arduino Serial Monitor must be closed.

--wifi receives telemetry via the ESP32 local Wi-Fi TCP endpoint instead.
It auto-discovers the ESP32 after DHCP assigns its address, including when a
phone hotspot cannot resolve mDNS. Use --esp-wifi-host with an explicit IP or
esp32-sensors.local only as a troubleshooting fallback.

--lan is explicit opt-in and binds FastAPI to 0.0.0.0 for phones on the same
Wi-Fi. Without it, the service remains available only at 127.0.0.1.
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --serial-port|-p)
      serial_port="${2:-}"
      [[ -n "$serial_port" ]] || { echo "--serial-port needs a value." >&2; exit 2; }
      shift 2
      ;;
    --wifi) wifi=true; shift ;;
    --esp-wifi-host)
      esp_wifi_host="${2:-}"
      [[ -n "$esp_wifi_host" ]] || { echo "--esp-wifi-host needs a value." >&2; exit 2; }
      shift 2
      ;;
    --no-browser) open_browser=false; shift ;;
    --lan) lan=true; shift ;;
    --help|-h) usage; exit 0 ;;
    *) echo "Unknown option: $1" >&2; usage >&2; exit 2 ;;
  esac
done

venv_python="$project_root/.venv/bin/python"
runtime_dir="$project_root/runtime"
log_dir="$runtime_dir/logs"
pid_file="$runtime_dir/dashboard-processes-macos.json"
dashboard_url="http://127.0.0.1:8000/dashboard"

is_alive() {
  [[ -n "${1:-}" ]] && kill -0 "$1" 2>/dev/null
}

read_pid() {
  [[ -f "$pid_file" ]] || return 0
  python3 -c 'import json, sys; print(json.load(open(sys.argv[1])).get(sys.argv[2], ""))' "$pid_file" "$1" 2>/dev/null || true
}

requested_transport="usb"
if [[ "$wifi" == true ]]; then requested_transport="wifi"; fi

start_process() {
  local name="$1"
  local previous_pid="$2"
  shift 2
  if is_alive "$previous_pid"; then
    echo "$name is already running (PID $previous_pid)." >&2
    printf '%s\n' "$previous_pid"
    return
  fi
  nohup "$venv_python" -u -m dual_forecast.cli "$@" \
    </dev/null >"$log_dir/$name.out.log" 2>"$log_dir/$name.err.log" &
  local new_pid=$!
  echo "Started $name (PID $new_pid)." >&2
  printf '%s\n' "$new_pid"
}

has_fresh_live_telemetry() {
  local payload
  payload="$(curl --silent --fail http://127.0.0.1:8000/v1/dashboard/latest 2>/dev/null || true)"
  [[ -n "$payload" ]] || return 1
  "$venv_python" -c '
import json, sys
from datetime import datetime, timezone
snapshot = json.loads(sys.stdin.read()).get("snapshot")
if not snapshot:
    raise SystemExit(1)
received = datetime.fromisoformat(snapshot["receivedAt"].replace("Z", "+00:00"))
raise SystemExit(0 if (datetime.now(timezone.utc) - received).total_seconds() <= 15 else 1)
' <<<"$payload"
}

if [[ ! -x "$venv_python" ]]; then
  echo "Creating Python virtual environment..."
  python3 -m venv "$project_root/.venv"
fi

if ! "$venv_python" -c 'import serial, qrcode' >/dev/null 2>&1; then
  echo "Installing project dependencies..."
  "$venv_python" -m pip install -r "$project_root/requirements.txt"
fi

mkdir -p "$log_dir"
if [[ "$wifi" != true && -z "$serial_port" ]]; then
  ports=(/dev/cu.wchusbserial* /dev/cu.usbserial* /dev/cu.SLAB_USBtoUART* /dev/cu.usbmodem*)
  existing_ports=()
  for port in "${ports[@]}"; do
    [[ -e "$port" ]] && existing_ports+=("$port")
  done
  if [[ ${#existing_ports[@]} -eq 1 ]]; then
    serial_port="${existing_ports[0]}"
    echo "Detected ESP32 USB serial port: $serial_port"
  else
    echo "Cannot determine the ESP32 port. Run: ./start_dashboard.sh --serial-port /dev/cu.wchusbserial10" >&2
    [[ ${#existing_ports[@]} -gt 0 ]] && printf 'Available candidates: %s\n' "${existing_ports[*]}" >&2
    exit 1
  fi
fi

if [[ "$wifi" != true && ! -e "$serial_port" ]]; then
  echo "ESP32 serial port does not exist: $serial_port" >&2
  exit 1
fi

previous_service_pid="$(read_pid servicePid)"
previous_receiver_pid="$(read_pid receiverPid)"
previous_transport="$(read_pid transport)"
previous_wifi_host="$(read_pid espWifiHost)"
# A receiver opened for USB cannot switch itself to TCP (or vice versa).
# It is safe to replace only the managed receiver PID stored by this script.
# Restart Wi-Fi mode too when its endpoint selection changed, so upgrading from
# an old mDNS/IP launch to the new automatic discovery takes effect directly.
if is_alive "$previous_receiver_pid" && { [[ -n "$previous_transport" && "$previous_transport" != "$requested_transport" ]] || [[ "$wifi" == true && "$previous_transport" == "wifi" && "$previous_wifi_host" != "$esp_wifi_host" ]]; }; then
  kill "$previous_receiver_pid"
  echo "Stopped previous $previous_transport ESP32 receiver (PID $previous_receiver_pid) to apply updated connection settings." >&2
  previous_receiver_pid=""
fi
if [[ "$wifi" != true ]] && lsof "$serial_port" >/dev/null 2>&1 && ! is_alive "$previous_receiver_pid"; then
  echo "Serial port is already in use: $serial_port" >&2
  echo "Close Arduino Serial Monitor or stop the existing receiver first." >&2
  exit 1
fi

server_host="127.0.0.1"
if [[ "$lan" == true ]]; then server_host="0.0.0.0"; fi
service_pid="$(start_process forecast-service "$previous_service_pid" serve --host "$server_host" --port 8000)"
for _ in {1..10}; do
  curl --silent --fail http://127.0.0.1:8000/health >/dev/null 2>&1 && break
  sleep 1
done
curl --silent --fail http://127.0.0.1:8000/health >/dev/null || {
  echo "Forecast service did not start. See $log_dir/forecast-service.err.log" >&2
  exit 1
}
if [[ "$wifi" == true ]]; then
  receiver_pid="$(start_process esp32-receiver "$previous_receiver_pid" receive-esp32 --esp-host "$esp_wifi_host")"
else
  receiver_pid="$(start_process esp32-receiver "$previous_receiver_pid" receive-esp32-serial --serial-port "$serial_port")"
fi

printf '{\n  "servicePid": %s,\n  "receiverPid": %s,\n  "transport": "%s",\n  "serialPort": "%s",\n  "espWifiHost": "%s"\n}\n' \
  "$service_pid" "$receiver_pid" "$requested_transport" "$serial_port" "$esp_wifi_host" >"$pid_file"

live_ready=false
for _ in {1..15}; do
  if has_fresh_live_telemetry; then
    live_ready=true
    break
  fi
  sleep 1
done
if [[ "$live_ready" == true ]]; then
  echo "Live ESP32 telemetry confirmed."
else
  echo "Warning: dashboard has not received fresh ESP32 telemetry yet." >&2
  if [[ "$wifi" == true ]]; then
    echo "Check $log_dir/esp32-receiver.out.log. Confirm ESP32 joined Wi-Fi; its TCP server will announce itself automatically." >&2
  else
    echo "Check $log_dir/esp32-receiver.out.log and close Arduino Serial Monitor if the port is busy." >&2
  fi
fi

if [[ "$open_browser" == true ]]; then
  if open -Ra "Google Chrome" >/dev/null 2>&1; then
    open -a "Google Chrome" --args "--app=$dashboard_url" --start-fullscreen
  elif open -Ra "Microsoft Edge" >/dev/null 2>&1; then
    open -a "Microsoft Edge" --args "--app=$dashboard_url" --start-fullscreen
  else
    open "$dashboard_url"
  fi
fi

echo "Dashboard ready: $dashboard_url"
if [[ "$wifi" == true ]]; then
  if [[ "$esp_wifi_host" == "auto" ]]; then
    echo "ESP32 Wi-Fi telemetry: automatic local discovery (UDP 3334 → TCP 3333)"
  else
    echo "ESP32 Wi-Fi telemetry: $esp_wifi_host:3333"
  fi
else
  echo "ESP32 USB serial: $serial_port"
fi
if [[ "$lan" == true ]]; then
  echo "LAN mode is enabled. On the phone, use http://<this-Mac-IPv4>:8000/dashboard while both devices are on the same Wi-Fi."
  echo "If macOS Firewall asks, allow the Python process on the local network."
fi
echo "Logs: $log_dir"

#!/usr/bin/env bash
# Starts the local forecast API, USB serial receiver, and browser dashboard on macOS.
set -euo pipefail

project_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
serial_port=""
open_browser=true

usage() {
  cat <<'EOF'
Usage: ./start_dashboard.sh [--serial-port /dev/cu.usbserial...] [--no-browser]

The ESP32 USB cable must stay connected. Close Arduino Serial Monitor first,
because only one program can use the serial port at a time.
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --serial-port|-p)
      serial_port="${2:-}"
      [[ -n "$serial_port" ]] || { echo "--serial-port needs a value." >&2; exit 2; }
      shift 2
      ;;
    --no-browser) open_browser=false; shift ;;
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

if [[ ! -x "$venv_python" ]]; then
  echo "Creating Python virtual environment..."
  python3 -m venv "$project_root/.venv"
fi

if ! "$venv_python" -c 'import serial' >/dev/null 2>&1; then
  echo "Installing project dependencies..."
  "$venv_python" -m pip install -r "$project_root/requirements.txt"
fi

mkdir -p "$log_dir"
if [[ -z "$serial_port" ]]; then
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

if [[ ! -e "$serial_port" ]]; then
  echo "ESP32 serial port does not exist: $serial_port" >&2
  exit 1
fi

previous_service_pid="$(read_pid servicePid)"
previous_receiver_pid="$(read_pid receiverPid)"
if lsof "$serial_port" >/dev/null 2>&1 && ! is_alive "$previous_receiver_pid"; then
  echo "Serial port is already in use: $serial_port" >&2
  echo "Close Arduino Serial Monitor or stop the existing receiver first." >&2
  exit 1
fi

service_pid="$(start_process forecast-service "$previous_service_pid" serve --host 127.0.0.1 --port 8000)"
for _ in {1..10}; do
  curl --silent --fail http://127.0.0.1:8000/health >/dev/null 2>&1 && break
  sleep 1
done
curl --silent --fail http://127.0.0.1:8000/health >/dev/null || {
  echo "Forecast service did not start. See $log_dir/forecast-service.err.log" >&2
  exit 1
}
receiver_pid="$(start_process esp32-receiver "$previous_receiver_pid" receive-esp32-serial --serial-port "$serial_port")"

printf '{\n  "servicePid": %s,\n  "receiverPid": %s,\n  "serialPort": "%s"\n}\n' \
  "$service_pid" "$receiver_pid" "$serial_port" >"$pid_file"

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
echo "ESP32 USB serial: $serial_port"
echo "Logs: $log_dir"

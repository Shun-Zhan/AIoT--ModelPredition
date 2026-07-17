#!/usr/bin/env bash
# Stops processes started by scripts/start_dashboard.sh on macOS.
set -euo pipefail

project_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
pid_file="$project_root/runtime/dashboard-processes-macos.json"

if [[ ! -f "$pid_file" ]]; then
  echo "No managed macOS dashboard processes found."
  exit 0
fi

read_pid() {
  python3 -c 'import json, sys; print(json.load(open(sys.argv[1])).get(sys.argv[2], ""))' "$pid_file" "$1" 2>/dev/null || true
}

for field in receiverPid servicePid; do
  pid="$(read_pid "$field")"
  if [[ -n "$pid" ]] && kill -0 "$pid" 2>/dev/null; then
    kill "$pid"
    echo "Stopped $field ($pid)."
  fi
done
rm -f "$pid_file"

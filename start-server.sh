#!/usr/bin/env bash
set -euo pipefail

APP_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
RUNTIME_DIR="$APP_DIR/.runtime"
PID_FILE="$RUNTIME_DIR/server.pid"
LOG_FILE="$RUNTIME_DIR/server.log"
SERVER_PORT="${FNIRS_PORT:-8080}"

init_command="$(tr '\0' ' ' < /proc/1/cmdline 2>/dev/null || true)"
if [[ "$init_command" == *"bwrap"* && "$init_command" == *"--die-with-parent"* ]]; then
  echo "This SSH account runs inside a die-with-parent sandbox."
  echo "Use: bash run-server.sh (and keep the SSH session open)."
  echo "Ask the administrator for systemd/container access for unattended service."
  exit 2
fi

mkdir -p "$RUNTIME_DIR"
if [[ -s "$PID_FILE" ]]; then
  running_pid="$(<"$PID_FILE")"
  running_command="$(tr '\0' ' ' < "/proc/$running_pid/cmdline" 2>/dev/null || true)"
  if [[ "$running_command" == *"$APP_DIR/server.py"* ]]; then
    echo "fNIRS dashboard is already running (PID $running_pid)"
    exit 0
  fi
fi

nohup "$APP_DIR/.conda-env/bin/python" "$APP_DIR/server.py" \
  --host 0.0.0.0 --port "$SERVER_PORT" >>"$LOG_FILE" 2>&1 </dev/null &
server_pid=$!
echo "$server_pid" > "$PID_FILE"
echo "fNIRS dashboard started: PID $server_pid, port $SERVER_PORT"
echo "Log: $LOG_FILE"

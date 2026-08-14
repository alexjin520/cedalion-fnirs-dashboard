#!/usr/bin/env bash
set -euo pipefail

APP_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
PID_FILE="$APP_DIR/.runtime/server.pid"

if [[ ! -s "$PID_FILE" ]]; then
  echo "fNIRS dashboard is not running"
  exit 1
fi

server_pid="$(<"$PID_FILE")"
server_command="$(tr '\0' ' ' < "/proc/$server_pid/cmdline" 2>/dev/null || true)"
if [[ "$server_command" == *"$APP_DIR/server.py"* ]]; then
  echo "fNIRS dashboard is running (PID $server_pid)"
  exit 0
fi

echo "fNIRS dashboard is not running (stale PID file)"
exit 1

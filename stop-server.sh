#!/usr/bin/env bash
set -euo pipefail

APP_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
PID_FILE="$APP_DIR/.runtime/server.pid"

if [[ ! -s "$PID_FILE" ]]; then
  echo "fNIRS dashboard is not running"
  exit 0
fi

server_pid="$(<"$PID_FILE")"
server_command="$(tr '\0' ' ' < "/proc/$server_pid/cmdline" 2>/dev/null || true)"
if [[ "$server_command" != *"$APP_DIR/server.py"* ]]; then
  echo "PID file is stale; no dashboard process was stopped"
  exit 1
fi

kill "$server_pid"
for _ in {1..40}; do
  if ! kill -0 "$server_pid" 2>/dev/null; then
    : > "$PID_FILE"
    echo "fNIRS dashboard stopped"
    exit 0
  fi
  sleep 0.25
done

echo "Dashboard did not stop within 10 seconds (PID $server_pid)"
exit 1

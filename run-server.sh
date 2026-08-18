#!/usr/bin/env bash
set -euo pipefail

APP_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
SERVER_PORT="${FNIRS_PORT:-10000}"

cd "$APP_DIR"
exec "$APP_DIR/.conda-env/bin/python" "$APP_DIR/server.py" \
  --host 0.0.0.0 --port "$SERVER_PORT"

#!/usr/bin/env bash

set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PORT="${1:-${PORT:-8000}}"
HOST="${HOST:-127.0.0.1}"

cd "$ROOT_DIR"
exec python3 "$ROOT_DIR/scripts/live_preview.py" --host "$HOST" --port "$PORT"

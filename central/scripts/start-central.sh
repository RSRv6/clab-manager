#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
APP_DIR="$(cd "$SCRIPT_DIR/../.." && pwd)"

HOST="${CENTRAL_HOST:-0.0.0.0}"
PORT="${CENTRAL_PORT:-80}"
TLS_CERT="${CENTRAL_TLS_CERT:-}"
TLS_KEY="${CENTRAL_TLS_KEY:-}"

UVICORN_BIN="$APP_DIR/.venv/bin/uvicorn"
ARGS=(src.app:app --host "$HOST" --port "$PORT")

if [[ -n "$TLS_CERT" || -n "$TLS_KEY" ]]; then
  if [[ -z "$TLS_CERT" || -z "$TLS_KEY" ]]; then
    echo "Both CENTRAL_TLS_CERT and CENTRAL_TLS_KEY must be set together." >&2
    exit 1
  fi
  ARGS+=(--ssl-certfile "$TLS_CERT" --ssl-keyfile "$TLS_KEY")
fi

exec "$UVICORN_BIN" "${ARGS[@]}"
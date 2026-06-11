#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
AGENT_DIR="${VLM_AGENT_DIR:-$(cd "$SCRIPT_DIR/.." && pwd)}"
VENV_DIR="${VLM_AGENT_VENV:-$AGENT_DIR/.venv}"
HOST="${VLM_AGENT_HOST:-0.0.0.0}"
PORT="${VLM_AGENT_PORT:-8081}"

echo "[1/4] Installing runtime packages"
sudo apt-get update
sudo apt-get install -y python3 python3-venv python3-pip

echo "[2/4] Creating/updating virtual environment"
python3 -m venv "$VENV_DIR"
"$VENV_DIR/bin/pip" install --upgrade pip wheel

echo "[3/4] Installing dependencies"
"$VENV_DIR/bin/pip" install -r "$AGENT_DIR/requirements.txt"

echo "[4/4] Starting VM agent on ${HOST}:${PORT}"
cd "$AGENT_DIR"
exec "$VENV_DIR/bin/uvicorn" src.agent:app --host "$HOST" --port "$PORT"
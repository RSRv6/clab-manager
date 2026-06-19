#!/bin/bash
# Raccourci pour lancer les tests d'intégration VLM
# Usage: ./vlm-test.sh [options]
# Exemple: ./vlm-test.sh --password monpass --skip reconfigure
# Variables: VLM_USER, VLM_PASSWORD, VLM_URL

VENV="${VLM_VENV:-/home/vlm/virtual-labs-management/.venv}"
PYTHON="${VENV}/bin/python3"

if [ ! -f "$PYTHON" ]; then
    PYTHON=$(which python3)
fi

exec "$PYTHON" "$(dirname "$0")/tests/vlm_integration_test.py" "$@"

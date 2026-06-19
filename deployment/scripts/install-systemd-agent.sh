#!/usr/bin/env bash
set -euo pipefail

APP_DIR="/opt/virtual-labs-management"
APP_USER="vlm-central"
AGENT_HOST="0.0.0.0"
AGENT_PORT="8081"
AGENT_CONFIG=""
PERSISTENT_AGENT_CONFIG="/etc/virtual-labs-management/vm-agent/config.yaml"
AGENT_LOG_DIR=""
AGENT_LOG_LEVEL="INFO"
AGENT_ALLOW_INSECURE_NO_TOKEN="false"
SERVICE_NAME="vlm-agent"
LAB_CONFIG_DB_ROOT=""

CONFIG_PROVIDED="false"
LOG_DIR_PROVIDED="false"
AGENT_TOKEN=""
AGENT_NAME=""

while [[ $# -gt 0 ]]; do
  case "$1" in
    --app-dir) APP_DIR="$2"; shift 2 ;;
    --app-user) APP_USER="$2"; shift 2 ;;
    --host) AGENT_HOST="$2"; shift 2 ;;
    --port) AGENT_PORT="$2"; shift 2 ;;
    --config) AGENT_CONFIG="$2"; CONFIG_PROVIDED="true"; shift 2 ;;
    --log-dir) AGENT_LOG_DIR="$2"; LOG_DIR_PROVIDED="true"; shift 2 ;;
    --log-level) AGENT_LOG_LEVEL="$2"; shift 2 ;;
    --allow-insecure-no-token) AGENT_ALLOW_INSECURE_NO_TOKEN="$2"; shift 2 ;;
    --service-name) SERVICE_NAME="$2"; shift 2 ;;
    --token) AGENT_TOKEN="$2"; shift 2 ;;
    --agent-name) AGENT_NAME="$2"; shift 2 ;;
    --lab-config-db-root) LAB_CONFIG_DB_ROOT="$2"; shift 2 ;;
    *) echo "Unknown argument: $1"; exit 1 ;;
  esac
done

if [[ "$CONFIG_PROVIDED" == "false" ]]; then
  AGENT_CONFIG="$PERSISTENT_AGENT_CONFIG"
fi

if [[ "$LOG_DIR_PROVIDED" == "false" ]]; then
  AGENT_LOG_DIR="$APP_DIR/log"
fi

install -d -m 0755 /etc/default /etc/systemd/system "$AGENT_LOG_DIR" "$(dirname "$AGENT_CONFIG")" "$(dirname "$PERSISTENT_AGENT_CONFIG")"

# Ensure the service user exists and is in the docker group (required for clab/docker access)
if id "$APP_USER" &>/dev/null; then
  usermod -aG docker "$APP_USER" 2>/dev/null && echo "Added $APP_USER to docker group" || true
else
  echo "WARNING: user $APP_USER does not exist — service may fail to start"
fi

# Set ownership of the log directory to the service user
chown "$APP_USER":"$APP_USER" "$AGENT_LOG_DIR" 2>/dev/null || true

# Derive default LAB_CONFIG_DB_ROOT from service user home if not provided
if [[ -z "$LAB_CONFIG_DB_ROOT" ]]; then
  USER_HOME=$(getent passwd "$APP_USER" | cut -d: -f6)
  LAB_CONFIG_DB_ROOT="${USER_HOME}/labs/config_db"
fi
# Ensure the directory exists and belongs to the service user
install -d -m 0755 -o "$APP_USER" -g "$APP_USER" "$LAB_CONFIG_DB_ROOT" 2>/dev/null || true

# Create the config file if it does not exist yet (regardless of CONFIG_PROVIDED,
# because the file may simply not be present on a fresh machine).
if [[ ! -f "$AGENT_CONFIG" ]]; then
  if [[ -f "$APP_DIR/vm-agent/config.yaml" ]]; then
    cp "$APP_DIR/vm-agent/config.yaml" "$AGENT_CONFIG"
    echo "Copied config from $APP_DIR/vm-agent/config.yaml"
  else
    cat > "$AGENT_CONFIG" <<EOF
agent:
  name: "${AGENT_NAME:-clab-vm-agent}"
  host: "0.0.0.0"
  port: ${AGENT_PORT}
  api_token: "${AGENT_TOKEN:-change-me}"
EOF
    echo "Created default config at $AGENT_CONFIG"
  fi
fi

cat > /etc/default/vlm-agent <<EOF
AGENT_HOST=$AGENT_HOST
AGENT_PORT=$AGENT_PORT
AGENT_CONFIG=$AGENT_CONFIG
VM_AGENT_LOG_DIR=$AGENT_LOG_DIR
VM_AGENT_LOG_LEVEL=$AGENT_LOG_LEVEL
AGENT_ALLOW_INSECURE_NO_TOKEN=$AGENT_ALLOW_INSECURE_NO_TOKEN
LAB_CONFIG_DB_ROOT=$LAB_CONFIG_DB_ROOT
EOF

cat > "/etc/systemd/system/${SERVICE_NAME}.service" <<EOF
[Unit]
Description=Virtual Labs Management - VM Agent API
After=network.target

[Service]
Type=simple
User=$APP_USER
Group=$APP_USER
WorkingDirectory=$APP_DIR/vm-agent
EnvironmentFile=/etc/default/vlm-agent
ExecStart=$APP_DIR/.venv/bin/uvicorn src.agent:app --host \${AGENT_HOST} --port \${AGENT_PORT}
Restart=always
RestartSec=3
LimitNOFILE=65535

[Install]
WantedBy=multi-user.target
EOF

# Patch token and agent name into the config (file is guaranteed to exist at this point)
if [[ -n "$AGENT_TOKEN" ]]; then
  if grep -q "api_token:" "$AGENT_CONFIG"; then
    sed -i "s|api_token:.*|api_token: \"${AGENT_TOKEN}\"|" "$AGENT_CONFIG"
  else
    printf '\n  api_token: "%s"\n' "${AGENT_TOKEN}" >> "$AGENT_CONFIG"
  fi
  echo "Patched api_token in $AGENT_CONFIG"
fi
if [[ -n "$AGENT_NAME" ]]; then
  if grep -q "^  name:" "$AGENT_CONFIG"; then
    sed -i "s|^  name:.*|  name: \"${AGENT_NAME}\"|" "$AGENT_CONFIG"
  else
    sed -i "/^agent:/a\\  name: \"${AGENT_NAME}\"" "$AGENT_CONFIG"
  fi
  echo "Patched agent name in $AGENT_CONFIG"
fi

systemctl daemon-reload
echo "Installed ${SERVICE_NAME}.service and /etc/default/vlm-agent"

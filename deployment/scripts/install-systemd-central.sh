#!/usr/bin/env bash
set -euo pipefail

APP_DIR="/opt/virtual-labs-management"
APP_USER="root"
CENTRAL_HOST="0.0.0.0"
CENTRAL_PORT="80"
CENTRAL_CONFIG=""
CENTRAL_LOG_DIR=""
CENTRAL_LOG_LEVEL="INFO"
CENTRAL_TLS_CERT=""
CENTRAL_TLS_KEY=""
CENTRAL_FORCE_HTTPS="false"
CENTRAL_BOOTSTRAP_ADMIN_PASSWORD=""
CENTRAL_ALLOW_INSECURE_SESSION_SECRET="false"
CENTRAL_ALLOW_INSECURE_DEFAULT_ADMIN_PASSWORD="false"
SERVICE_NAME="vlm-central"

CONFIG_PROVIDED="false"
LOG_DIR_PROVIDED="false"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --app-dir) APP_DIR="$2"; shift 2 ;;
    --app-user) APP_USER="$2"; shift 2 ;;
    --host) CENTRAL_HOST="$2"; shift 2 ;;
    --port) CENTRAL_PORT="$2"; shift 2 ;;
    --config) CENTRAL_CONFIG="$2"; CONFIG_PROVIDED="true"; shift 2 ;;
    --log-dir) CENTRAL_LOG_DIR="$2"; LOG_DIR_PROVIDED="true"; shift 2 ;;
    --log-level) CENTRAL_LOG_LEVEL="$2"; shift 2 ;;
    --tls-cert) CENTRAL_TLS_CERT="$2"; shift 2 ;;
    --tls-key) CENTRAL_TLS_KEY="$2"; shift 2 ;;
    --force-https) CENTRAL_FORCE_HTTPS="$2"; shift 2 ;;
    --bootstrap-admin-password) CENTRAL_BOOTSTRAP_ADMIN_PASSWORD="$2"; shift 2 ;;
    --allow-insecure-session-secret) CENTRAL_ALLOW_INSECURE_SESSION_SECRET="$2"; shift 2 ;;
    --allow-insecure-default-admin-password) CENTRAL_ALLOW_INSECURE_DEFAULT_ADMIN_PASSWORD="$2"; shift 2 ;;
    --service-name) SERVICE_NAME="$2"; shift 2 ;;
    *) echo "Unknown argument: $1"; exit 1 ;;
  esac
done

if [[ "$CONFIG_PROVIDED" == "false" ]]; then
  CENTRAL_CONFIG="$APP_DIR/central/config.yaml"
fi

if [[ "$LOG_DIR_PROVIDED" == "false" ]]; then
  CENTRAL_LOG_DIR="$APP_DIR/log"
fi

install -d -m 0755 /etc/default /etc/systemd/system "$CENTRAL_LOG_DIR"

cat > /etc/default/vlm-central <<EOF
CENTRAL_HOST=$CENTRAL_HOST
CENTRAL_PORT=$CENTRAL_PORT
CENTRAL_CONFIG=$CENTRAL_CONFIG
CENTRAL_LOG_DIR=$CENTRAL_LOG_DIR
CENTRAL_LOG_LEVEL=$CENTRAL_LOG_LEVEL
CENTRAL_TLS_CERT=$CENTRAL_TLS_CERT
CENTRAL_TLS_KEY=$CENTRAL_TLS_KEY
CENTRAL_FORCE_HTTPS=$CENTRAL_FORCE_HTTPS
CENTRAL_BOOTSTRAP_ADMIN_PASSWORD=$CENTRAL_BOOTSTRAP_ADMIN_PASSWORD
CENTRAL_ALLOW_INSECURE_SESSION_SECRET=$CENTRAL_ALLOW_INSECURE_SESSION_SECRET
CENTRAL_ALLOW_INSECURE_DEFAULT_ADMIN_PASSWORD=$CENTRAL_ALLOW_INSECURE_DEFAULT_ADMIN_PASSWORD
EOF

cat > "/etc/systemd/system/${SERVICE_NAME}.service" <<EOF
[Unit]
Description=Virtual Labs Management - Central API
After=network.target

[Service]
Type=simple
User=$APP_USER
Group=$APP_USER
WorkingDirectory=$APP_DIR/central
EnvironmentFile=/etc/default/vlm-central
ExecStart=/usr/bin/env bash $APP_DIR/central/scripts/start-central.sh
Restart=always
RestartSec=3
LimitNOFILE=65535

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
echo "Installed ${SERVICE_NAME}.service and /etc/default/vlm-central"

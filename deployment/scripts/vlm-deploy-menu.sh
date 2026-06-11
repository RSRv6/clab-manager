#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

COMPONENT=""
REPO_URL=""
BRANCH="main"
APP_DIR="/opt/virtual-labs-management"
APP_USER="root"
SYNC_MODE="pull"

CENTRAL_HOST="0.0.0.0"
CENTRAL_PORT="80"
CENTRAL_CONFIG=""
CENTRAL_SERVICE="vlm-central"

AGENT_HOST="0.0.0.0"
AGENT_PORT="8081"
AGENT_CONFIG=""
AGENT_SERVICE="vlm-agent"

CENTRAL_CONFIG_PROVIDED="false"
AGENT_CONFIG_PROVIDED="false"

usage() {
  cat <<'EOF'
Usage:
  sudo bash deployment/scripts/vlm-deploy-menu.sh [options]

Options:
  --component <central|agent|both>
  --repo-url <git_url>
  --branch <branch>
  --app-dir <path>
  --app-user <user>
  --sync-mode <pull|force>

  --central-host <host>
  --central-port <port>
  --central-config <path>
  --central-service <name>

  --agent-host <host>
  --agent-port <port>
  --agent-config <path>
  --agent-service <name>

  -h, --help

If a value is missing, the script asks interactively.
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --component) COMPONENT="$2"; shift 2 ;;
    --repo-url) REPO_URL="$2"; shift 2 ;;
    --branch) BRANCH="$2"; shift 2 ;;
    --app-dir) APP_DIR="$2"; shift 2 ;;
    --app-user) APP_USER="$2"; shift 2 ;;
    --sync-mode) SYNC_MODE="$2"; shift 2 ;;

    --central-host) CENTRAL_HOST="$2"; shift 2 ;;
    --central-port) CENTRAL_PORT="$2"; shift 2 ;;
    --central-config) CENTRAL_CONFIG="$2"; CENTRAL_CONFIG_PROVIDED="true"; shift 2 ;;
    --central-service) CENTRAL_SERVICE="$2"; shift 2 ;;

    --agent-host) AGENT_HOST="$2"; shift 2 ;;
    --agent-port) AGENT_PORT="$2"; shift 2 ;;
    --agent-config) AGENT_CONFIG="$2"; AGENT_CONFIG_PROVIDED="true"; shift 2 ;;
    --agent-service) AGENT_SERVICE="$2"; shift 2 ;;

    -h|--help) usage; exit 0 ;;
    *) echo "Unknown argument: $1"; usage; exit 1 ;;
  esac
done

prompt_default() {
  local label="$1"
  local default_value="$2"
  local answer
  read -r -p "$label [$default_value]: " answer
  if [[ -z "$answer" ]]; then
    echo "$default_value"
  else
    echo "$answer"
  fi
}

if [[ -z "$COMPONENT" ]]; then
  echo "What do you want to install/update?"
  select option in central agent both; do
    case "$option" in
      central|agent|both)
        COMPONENT="$option"
        break
        ;;
      *) echo "Invalid choice" ;;
    esac
  done
fi

if [[ "$COMPONENT" != "central" && "$COMPONENT" != "agent" && "$COMPONENT" != "both" ]]; then
  echo "--component must be central, agent or both"
  exit 1
fi

if [[ -z "$REPO_URL" ]]; then
  read -r -p "Git repository URL: " REPO_URL
fi

if [[ -z "$REPO_URL" ]]; then
  echo "Repository URL is required"
  exit 1
fi

BRANCH="$(prompt_default "Branch" "$BRANCH")"
APP_DIR="$(prompt_default "Application directory" "$APP_DIR")"
APP_USER="$(prompt_default "Service user" "$APP_USER")"
SYNC_MODE="$(prompt_default "Sync mode (pull/force)" "$SYNC_MODE")"

if [[ "$CENTRAL_CONFIG_PROVIDED" == "false" ]]; then
  CENTRAL_CONFIG="$APP_DIR/central/config.yaml"
fi

if [[ "$AGENT_CONFIG_PROVIDED" == "false" ]]; then
  AGENT_CONFIG="$APP_DIR/vm-agent/config.yaml"
fi

if [[ "$SYNC_MODE" != "pull" && "$SYNC_MODE" != "force" ]]; then
  echo "Sync mode must be pull or force"
  exit 1
fi

run_central() {
  echo "=== CENTRAL CONFIG ==="
  CENTRAL_HOST="$(prompt_default "Central bind host" "$CENTRAL_HOST")"
  CENTRAL_PORT="$(prompt_default "Central port" "$CENTRAL_PORT")"
  CENTRAL_CONFIG="$(prompt_default "Central config path" "$CENTRAL_CONFIG")"
  CENTRAL_SERVICE="$(prompt_default "Central service name" "$CENTRAL_SERVICE")"

  bash "$SCRIPT_DIR/install-systemd-central.sh" \
    --app-dir "$APP_DIR" \
    --app-user "$APP_USER" \
    --host "$CENTRAL_HOST" \
    --port "$CENTRAL_PORT" \
    --config "$CENTRAL_CONFIG" \
    --service-name "$CENTRAL_SERVICE"

  bash "$SCRIPT_DIR/vlm-deploy-update.sh" \
    --role central \
    --repo-url "$REPO_URL" \
    --branch "$BRANCH" \
    --app-dir "$APP_DIR" \
    --app-user "$APP_USER" \
    --sync-mode "$SYNC_MODE" \
    --service-name "$CENTRAL_SERVICE" \
    --healthcheck-url "http://127.0.0.1:${CENTRAL_PORT}/health"

  echo "=== CENTRAL CHECKS ==="
  systemctl is-active "$CENTRAL_SERVICE"
  curl -fsS "http://127.0.0.1:${CENTRAL_PORT}/health" >/dev/null
  echo "Central OK"
}

run_agent() {
  echo "=== VM-AGENT CONFIG ==="
  AGENT_HOST="$(prompt_default "Agent bind host" "$AGENT_HOST")"
  AGENT_PORT="$(prompt_default "Agent port" "$AGENT_PORT")"
  AGENT_CONFIG="$(prompt_default "Agent config path" "$AGENT_CONFIG")"
  AGENT_SERVICE="$(prompt_default "Agent service name" "$AGENT_SERVICE")"

  bash "$SCRIPT_DIR/install-systemd-agent.sh" \
    --app-dir "$APP_DIR" \
    --app-user "$APP_USER" \
    --host "$AGENT_HOST" \
    --port "$AGENT_PORT" \
    --config "$AGENT_CONFIG" \
    --service-name "$AGENT_SERVICE"

  bash "$SCRIPT_DIR/vlm-deploy-update.sh" \
    --role agent \
    --repo-url "$REPO_URL" \
    --branch "$BRANCH" \
    --app-dir "$APP_DIR" \
    --app-user "$APP_USER" \
    --sync-mode "$SYNC_MODE" \
    --service-name "$AGENT_SERVICE" \
    --healthcheck-url "http://127.0.0.1:${AGENT_PORT}/health"

  echo "=== VM-AGENT CHECKS ==="
  systemctl is-active "$AGENT_SERVICE"
  curl -fsS "http://127.0.0.1:${AGENT_PORT}/health" >/dev/null
  echo "VM-Agent OK"
}

echo "Component: $COMPONENT"
echo "Repo URL:  $REPO_URL"
echo "Branch:    $BRANCH"
echo "App dir:   $APP_DIR"
echo "App user:  $APP_USER"
echo "Sync mode: $SYNC_MODE"

if [[ "$COMPONENT" == "central" ]]; then
  run_central
elif [[ "$COMPONENT" == "agent" ]]; then
  run_agent
else
  run_central
  run_agent
fi

echo "Done"

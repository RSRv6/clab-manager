#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd "$SCRIPT_DIR/../.." && pwd)"
DEPLOY_SCRIPT="$SCRIPT_DIR/deploy-over-ssh.sh"

SSH_USER=""
REPO_URL=""
BRANCH="main"
APP_DIR="/opt/virtual-labs-management"
APP_USER="root"
SYNC_MODE="pull"

AGENT_HOSTS_FILE="$ROOT_DIR/deployment/hosts/agent_hosts.txt"
CENTRAL_HOSTS_FILE=""
UPDATE_CENTRAL="false"

SSH_PASSWORD=""
SUDO_PASSWORD=""
REPO_SSH_PASSWORD=""

AGENT_PORT="8081"
CENTRAL_PORT="80"

usage() {
  cat <<'EOF'
Usage:
  update-fleet.sh \
    --ssh-user <user> \
    [--repo-url <git_url>] \
    [--branch <branch>] \
    [--agent-hosts-file <file>] \
    [--update-central <true|false>] \
    [--central-hosts-file <file>] \
    [--app-dir <path>] \
    [--app-user <user>] \
    [--sync-mode <pull|force>] \
    [--agent-port <port>] \
    [--central-port <port>] \
    [--ssh-password <password>] \
    [--sudo-password <password>] \
    [--repo-ssh-password <password>]

Examples:
  update-fleet.sh --ssh-user ubuntu
  update-fleet.sh --ssh-user ubuntu --update-central true --central-hosts-file deployment/hosts/central_hosts.txt
EOF
}

resolve_repo_url() {
  if [[ -n "$REPO_URL" ]]; then
    return
  fi

  REPO_URL="$(git -C "$ROOT_DIR" config --get remote.origin.url 2>/dev/null || true)"
  REPO_URL="$(echo "$REPO_URL" | xargs)"

  if [[ -z "$REPO_URL" ]]; then
    echo "Unable to auto-detect repository URL from local git remote 'origin'."
    echo "Use --repo-url <git_url> or configure: git -C '$ROOT_DIR' remote add origin <git_url>"
    exit 1
  fi

  echo "Auto-detected repo URL: $REPO_URL"
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --ssh-user) SSH_USER="$2"; shift 2 ;;
    --repo-url) REPO_URL="$2"; shift 2 ;;
    --branch) BRANCH="$2"; shift 2 ;;
    --agent-hosts-file) AGENT_HOSTS_FILE="$2"; shift 2 ;;
    --update-central) UPDATE_CENTRAL="$2"; shift 2 ;;
    --central-hosts-file) CENTRAL_HOSTS_FILE="$2"; shift 2 ;;
    --app-dir) APP_DIR="$2"; shift 2 ;;
    --app-user) APP_USER="$2"; shift 2 ;;
    --sync-mode) SYNC_MODE="$2"; shift 2 ;;
    --agent-port) AGENT_PORT="$2"; shift 2 ;;
    --central-port) CENTRAL_PORT="$2"; shift 2 ;;
    --ssh-password) SSH_PASSWORD="$2"; shift 2 ;;
    --sudo-password) SUDO_PASSWORD="$2"; shift 2 ;;
    --repo-ssh-password) REPO_SSH_PASSWORD="$2"; shift 2 ;;
    -h|--help) usage; exit 0 ;;
    *) echo "Unknown argument: $1"; usage; exit 1 ;;
  esac
done

if [[ -z "$SSH_USER" ]]; then
  echo "--ssh-user is required"
  usage
  exit 1
fi

resolve_repo_url

if [[ "$UPDATE_CENTRAL" != "true" && "$UPDATE_CENTRAL" != "false" ]]; then
  echo "--update-central must be true or false"
  exit 1
fi

if [[ ! -f "$AGENT_HOSTS_FILE" ]]; then
  echo "Agent hosts file not found: $AGENT_HOSTS_FILE"
  exit 1
fi

if [[ "$UPDATE_CENTRAL" == "true" && -z "$CENTRAL_HOSTS_FILE" ]]; then
  default_central_hosts="$ROOT_DIR/deployment/hosts/central_hosts.txt"
  if [[ -f "$default_central_hosts" ]]; then
    CENTRAL_HOSTS_FILE="$default_central_hosts"
  else
    echo "Central update requested but no central hosts file found."
    echo "Provide --central-hosts-file <file>."
    exit 1
  fi
fi

if [[ -n "$CENTRAL_HOSTS_FILE" && ! -f "$CENTRAL_HOSTS_FILE" ]]; then
  echo "Central hosts file not found: $CENTRAL_HOSTS_FILE"
  exit 1
fi

common_args=(
  --ssh-user "$SSH_USER"
  --repo-url "$REPO_URL"
  --branch "$BRANCH"
  --app-dir "$APP_DIR"
  --app-user "$APP_USER"
  --sync-mode "$SYNC_MODE"
)

if [[ -n "$SSH_PASSWORD" ]]; then
  common_args+=(--ssh-password "$SSH_PASSWORD")
fi
if [[ -n "$SUDO_PASSWORD" ]]; then
  common_args+=(--sudo-password "$SUDO_PASSWORD")
fi
if [[ -n "$REPO_SSH_PASSWORD" ]]; then
  common_args+=(--repo-ssh-password "$REPO_SSH_PASSWORD")
fi

echo "[1/2] Updating all vm-agents"
bash "$DEPLOY_SCRIPT" \
  --role agent \
  --hosts-file "$AGENT_HOSTS_FILE" \
  --port "$AGENT_PORT" \
  "${common_args[@]}"

if [[ "$UPDATE_CENTRAL" == "true" ]]; then
  echo "[2/2] Updating central"
  bash "$DEPLOY_SCRIPT" \
    --role central \
    --hosts-file "$CENTRAL_HOSTS_FILE" \
    --port "$CENTRAL_PORT" \
    "${common_args[@]}"
fi

echo "Fleet update completed successfully"

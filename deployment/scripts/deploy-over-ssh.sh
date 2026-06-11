#!/usr/bin/env bash
set -euo pipefail

ROLE=""
HOSTS_FILE=""
SSH_USER=""
REPO_URL=""
BRANCH="main"
APP_DIR="/opt/virtual-labs-management"
APP_USER="root"
SYNC_MODE="pull"
ALLOW_DIRTY_WORKTREE="false"
# Accept passwords from environment to avoid exposing them in process listing
SSH_PASSWORD="${VLM_SSH_PASSWORD:-}"
SUDO_PASSWORD="${VLM_SUDO_PASSWORD:-}"
REPO_SSH_PASSWORD="${VLM_REPO_SSH_PASSWORD:-}"

SERVICE_NAME=""
HOST_BIND="0.0.0.0"
PORT=""
CONFIG_PATH=""
LOG_DIR="/opt/virtual-labs-management/log"
LOG_LEVEL="INFO"
CENTRAL_CONFIG_SYNC=""
SYNC_CENTRAL_CONFIG=""
LOG_DIR_PROVIDED="false"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

usage() {
  cat <<'EOF'
Usage:
  deploy-over-ssh.sh \
    --role <central|agent> \
    --hosts-file <file> \
    --ssh-user <user> \
    [--repo-url <git_url>] \
    [--branch <branch>] \
    [--app-dir <path>] \
    [--app-user <user>] \
    [--sync-mode <pull|force>] \
    [--allow-dirty-worktree] \
    [--ssh-password <password>] \
    [--sudo-password <password>] \
    [--repo-ssh-password <password>] \
    [--service-name <name>] \
    [--host-bind <0.0.0.0>] \
    [--port <80|443|8081>] \
    [--config-path <path>] \
    [--log-dir <path>] \
    [--log-level <INFO|DEBUG>] \
    [--sync-central-config <true|false>] \
    [--central-config-sync <path>]

Hosts file format:
  - Legacy: one IP/hostname per line
      192.168.1.101
  - Legacy with per-host ssh user
      192.168.1.101,ubuntu
  - Agent extended format (for future central sync):
      id,name,ip,token,port[,ssh_user]
      vm1,clab-vm-01,192.168.1.101,change-me,8081,ubuntu

Examples:
  deploy-over-ssh.sh --role central --hosts-file ./central_hosts.txt --ssh-user ubuntu --repo-url git@github.com:org/repo.git --branch prod --port 80
  deploy-over-ssh.sh --role agent --hosts-file ./agent_hosts.txt --ssh-user ubuntu --repo-url git@github.com:org/repo.git --branch prod --port 8081
EOF
}

resolve_repo_url() {
  if [[ -n "$REPO_URL" ]]; then
    return
  fi

  local root_dir
  root_dir="$(cd "$SCRIPT_DIR/../.." && pwd)"
  REPO_URL="$(git -C "$root_dir" config --get remote.origin.url 2>/dev/null || true)"
  REPO_URL="$(echo "$REPO_URL" | xargs)"

  if [[ -z "$REPO_URL" ]]; then
    echo "Unable to auto-detect repository URL from local git remote 'origin'."
    echo "Use --repo-url <git_url> or configure: git -C '$root_dir' remote add origin <git_url>"
    exit 1
  fi

  echo "Auto-detected repo URL: $REPO_URL"
}

validate_repo_url() {
  case "$REPO_URL" in
    /*|./*|../*|file://*)
      echo "Refusing repo URL '$REPO_URL': deploy-over-ssh.sh must clone from a remote Git URL reachable by the target hosts."
      echo "Use an SSH/HTTPS repository URL such as 'ssh://user@host/path/repo.git' or 'git@host:org/repo.git'."
      exit 1
      ;;
  esac
}

ensure_clean_local_worktree() {
  local root_dir
  root_dir="$(cd "$SCRIPT_DIR/../.." && pwd)"

  if ! git -C "$root_dir" rev-parse --is-inside-work-tree >/dev/null 2>&1; then
    return
  fi

  if [[ "$ALLOW_DIRTY_WORKTREE" == "true" ]]; then
    return
  fi

  if ! git -C "$root_dir" diff --quiet --ignore-submodules -- || ! git -C "$root_dir" diff --cached --quiet --ignore-submodules --; then
    echo "Local workspace has uncommitted changes in $root_dir."
    echo "deploy-over-ssh.sh deploys the remote Git branch, not your local unpublished changes."
    echo "Commit and push first, or rerun with --allow-dirty-worktree if that mismatch is intentional."
    exit 1
  fi

  if [[ -n "$(git -C "$root_dir" ls-files --others --exclude-standard)" ]]; then
    echo "Local workspace has untracked files in $root_dir."
    echo "deploy-over-ssh.sh deploys the remote Git branch, not your local unpublished changes."
    echo "Commit and push first, or rerun with --allow-dirty-worktree if that mismatch is intentional."
    exit 1
  fi
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --role) ROLE="$2"; shift 2 ;;
    --hosts-file) HOSTS_FILE="$2"; shift 2 ;;
    --ssh-user) SSH_USER="$2"; shift 2 ;;
    --repo-url) REPO_URL="$2"; shift 2 ;;
    --branch) BRANCH="$2"; shift 2 ;;
    --app-dir) APP_DIR="$2"; shift 2 ;;
    --app-user) APP_USER="$2"; shift 2 ;;
    --sync-mode) SYNC_MODE="$2"; shift 2 ;;
    --allow-dirty-worktree) ALLOW_DIRTY_WORKTREE="true"; shift ;;
    --ssh-password) SSH_PASSWORD="$2"; shift 2 ;;
    --sudo-password) SUDO_PASSWORD="$2"; shift 2 ;;
    --repo-ssh-password) REPO_SSH_PASSWORD="$2"; shift 2 ;;
    --service-name) SERVICE_NAME="$2"; shift 2 ;;
    --host-bind) HOST_BIND="$2"; shift 2 ;;
    --port) PORT="$2"; shift 2 ;;
    --config-path) CONFIG_PATH="$2"; shift 2 ;;
    --log-dir) LOG_DIR="$2"; LOG_DIR_PROVIDED="true"; shift 2 ;;
    --log-level) LOG_LEVEL="$2"; shift 2 ;;
    --sync-central-config) SYNC_CENTRAL_CONFIG="$2"; shift 2 ;;
    --central-config-sync) CENTRAL_CONFIG_SYNC="$2"; shift 2 ;;
    -h|--help) usage; exit 0 ;;
    *) echo "Unknown argument: $1"; usage; exit 1 ;;
  esac
done

if [[ -z "$ROLE" || -z "$HOSTS_FILE" || -z "$SSH_USER" ]]; then
  echo "Missing required arguments"
  usage
  exit 1
fi

resolve_repo_url
validate_repo_url
ensure_clean_local_worktree

if [[ ! -f "$HOSTS_FILE" ]]; then
  echo "Hosts file not found: $HOSTS_FILE"
  exit 1
fi

if [[ "$ROLE" != "central" && "$ROLE" != "agent" ]]; then
  echo "--role must be central or agent"
  exit 1
fi

if [[ -z "$SYNC_CENTRAL_CONFIG" ]]; then
  if [[ "$ROLE" == "agent" ]]; then
    SYNC_CENTRAL_CONFIG="true"
  else
    SYNC_CENTRAL_CONFIG="false"
  fi
fi

if [[ "$SYNC_CENTRAL_CONFIG" != "true" && "$SYNC_CENTRAL_CONFIG" != "false" ]]; then
  echo "--sync-central-config must be true or false"
  exit 1
fi

if [[ -z "$SERVICE_NAME" ]]; then
  SERVICE_NAME="vlm-${ROLE}"
fi

if [[ -z "$PORT" ]]; then
  if [[ "$ROLE" == "central" ]]; then
    PORT="80"
  else
    PORT="8081"
  fi
fi

if [[ -z "$CONFIG_PATH" ]]; then
  if [[ "$ROLE" == "central" ]]; then
    CONFIG_PATH="$APP_DIR/central/config.yaml"
  else
    CONFIG_PATH="/etc/virtual-labs-management/vm-agent/config.yaml"
  fi
fi

if [[ "$LOG_DIR_PROVIDED" == "false" ]]; then
  LOG_DIR="$APP_DIR/log"
fi

if [[ "$ROLE" == "central" ]]; then
  BOOTSTRAP_SCRIPT="$SCRIPT_DIR/install-systemd-central.sh"
  HEALTH_URL="http://127.0.0.1:${PORT}/health"
else
  BOOTSTRAP_SCRIPT="$SCRIPT_DIR/install-systemd-agent.sh"
  HEALTH_URL="http://127.0.0.1:${PORT}/health"
fi

if [[ -z "$CENTRAL_CONFIG_SYNC" ]]; then
  CENTRAL_CONFIG_SYNC="$SCRIPT_DIR/../../central/config.yaml"
fi

UPDATE_SCRIPT="$SCRIPT_DIR/vlm-deploy-update.sh"

ssh_cmd() {
  local ssh_password="${1:-}"
  shift
  if [[ -n "$ssh_password" ]]; then
    sshpass -p "$ssh_password" ssh -o StrictHostKeyChecking=no -o ForwardAgent=yes "$@"
  else
    ssh -o StrictHostKeyChecking=no -o ForwardAgent=yes "$@"
  fi
}

scp_cmd() {
  local ssh_password="${1:-}"
  shift
  if [[ -n "$ssh_password" ]]; then
    sshpass -p "$ssh_password" scp -o StrictHostKeyChecking=no "$@"
  else
    scp -o StrictHostKeyChecking=no "$@"
  fi
}

echo "Deploying role=$ROLE to hosts in $HOSTS_FILE"

while IFS= read -r raw_line <&3; do
  line="${raw_line%%#*}"
  line="$(echo "$line" | xargs)"
  [[ -z "$line" ]] && continue

  host="$line"
  target_ssh_user="$SSH_USER"
  target_ssh_password="$SSH_PASSWORD"
  target_sudo_password="$SUDO_PASSWORD"
  _agent_token=""
  _agent_name=""
  if [[ "$ROLE" == "agent" && "$line" == *,*,*,*,* ]]; then
    IFS=',' read -r _agent_id _agent_name _agent_ip _agent_token _agent_port _agent_ssh_user _agent_ssh_password _agent_sudo_password _extra <<< "$line"
    _agent_ip="$(echo "${_agent_ip:-}" | xargs)"
    _agent_ssh_user="$(echo "${_agent_ssh_user:-}" | xargs)"
    _agent_ssh_password="$(echo "${_agent_ssh_password:-}" | xargs)"
    _agent_sudo_password="$(echo "${_agent_sudo_password:-}" | xargs)"
    _agent_token="$(echo "${_agent_token:-}" | xargs)"
    _agent_name="$(echo "${_agent_name:-}" | xargs)"
    if [[ -n "$_agent_ip" ]]; then
      host="$_agent_ip"
    fi
    if [[ -n "$_agent_ssh_user" ]]; then
      target_ssh_user="$_agent_ssh_user"
    fi
    if [[ -n "$_agent_ssh_password" ]]; then
      target_ssh_password="$_agent_ssh_password"
    fi
    if [[ -n "$_agent_sudo_password" ]]; then
      target_sudo_password="$_agent_sudo_password"
    fi
  elif [[ "$line" == *,* ]]; then
    IFS=',' read -r _host _host_ssh_user _extra <<< "$line"
    _host="$(echo "${_host:-}" | xargs)"
    _host_ssh_user="$(echo "${_host_ssh_user:-}" | xargs)"
    if [[ -n "$_host" ]]; then
      host="$_host"
    fi
    if [[ -n "$_host_ssh_user" ]]; then
      target_ssh_user="$_host_ssh_user"
    fi
  fi

  echo "--- [$host] copy scripts"
  scp_cmd "$target_ssh_password" "$UPDATE_SCRIPT" "$BOOTSTRAP_SCRIPT" "${target_ssh_user}@${host}:/tmp/"

  echo "--- [$host] install systemd"
  if [[ -n "$target_sudo_password" ]]; then
    ssh_cmd "$target_ssh_password" "${target_ssh_user}@${host}" "sudo -S SSH_AUTH_SOCK=\$SSH_AUTH_SOCK bash /tmp/$(basename "$BOOTSTRAP_SCRIPT") \
      --app-dir '$APP_DIR' \
      --app-user '$APP_USER' \
      --host '$HOST_BIND' \
      --port '$PORT' \
      --config '$CONFIG_PATH' \
      --log-dir '$LOG_DIR' \
      --log-level '$LOG_LEVEL' \
      --service-name '$SERVICE_NAME' \
      --token '$_agent_token' \
      --agent-name '$_agent_name'" <<<"$target_sudo_password"
  else
    ssh_cmd "$target_ssh_password" "${target_ssh_user}@${host}" "sudo -n SSH_AUTH_SOCK=\$SSH_AUTH_SOCK bash /tmp/$(basename "$BOOTSTRAP_SCRIPT") \
      --app-dir '$APP_DIR' \
      --app-user '$APP_USER' \
      --host '$HOST_BIND' \
      --port '$PORT' \
      --config '$CONFIG_PATH' \
      --log-dir '$LOG_DIR' \
      --log-level '$LOG_LEVEL' \
      --service-name '$SERVICE_NAME' \
      --token '$_agent_token' \
      --agent-name '$_agent_name'"
  fi

  echo "--- [$host] deploy/update app"
  if [[ -n "$target_sudo_password" ]]; then
    ssh_cmd "$target_ssh_password" "${target_ssh_user}@${host}" "sudo -S SSH_AUTH_SOCK=\$SSH_AUTH_SOCK bash /tmp/$(basename "$UPDATE_SCRIPT") \
      --role '$ROLE' \
      --repo-url '$REPO_URL' \
      --branch '$BRANCH' \
      --app-dir '$APP_DIR' \
      --app-user '$APP_USER' \
      --sync-mode '$SYNC_MODE' \
      --service-name '$SERVICE_NAME' \
      --healthcheck-url '$HEALTH_URL' \
      --repo-ssh-password '$REPO_SSH_PASSWORD'" <<<"$target_sudo_password"
  else
    ssh_cmd "$target_ssh_password" "${target_ssh_user}@${host}" "sudo -n SSH_AUTH_SOCK=\$SSH_AUTH_SOCK bash /tmp/$(basename "$UPDATE_SCRIPT") \
      --role '$ROLE' \
      --repo-url '$REPO_URL' \
      --branch '$BRANCH' \
      --app-dir '$APP_DIR' \
      --app-user '$APP_USER' \
      --sync-mode '$SYNC_MODE' \
      --service-name '$SERVICE_NAME' \
      --healthcheck-url '$HEALTH_URL' \
      --repo-ssh-password '$REPO_SSH_PASSWORD'"
  fi

  echo "--- [$host] done"
done 3< "$HOSTS_FILE"

if [[ "$ROLE" == "agent" && "$SYNC_CENTRAL_CONFIG" == "true" ]]; then
  echo "Synchronizing agent inventory into central config: $CENTRAL_CONFIG_SYNC"
  python3 "$SCRIPT_DIR/sync-central-agents-config.py" \
    --hosts-file "$HOSTS_FILE" \
    --central-config "$CENTRAL_CONFIG_SYNC" \
    --default-port "$PORT"
fi

echo "All done"

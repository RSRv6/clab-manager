#!/usr/bin/env bash
set -euo pipefail

ROLE=""
REPO_URL=""
BRANCH="main"
APP_DIR="/opt/virtual-labs-management"
APP_USER="root"
SYNC_MODE="pull"
SERVICE_NAME=""
HEALTHCHECK_URL=""
REPO_SSH_PASSWORD=""

usage() {
  cat <<'EOF'
Usage:
  vlm-deploy-update.sh \
    --role <central|agent> \
    --repo-url <git_url> \
    [--branch <branch>] \
    [--app-dir <path>] \
    [--app-user <user>] \
    [--sync-mode <pull|force>] \
    [--service-name <systemd_service>] \
    [--healthcheck-url <url>] \
    [--repo-ssh-password <password>]

Examples:
  vlm-deploy-update.sh --role central --repo-url git@github.com:org/repo.git --app-user clab --service-name vlm-central
  vlm-deploy-update.sh --role agent --repo-url git@github.com:org/repo.git --branch prod --sync-mode force --service-name vlm-agent
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --role)
      ROLE="$2"
      shift 2
      ;;
    --repo-url)
      REPO_URL="$2"
      shift 2
      ;;
    --branch)
      BRANCH="$2"
      shift 2
      ;;
    --app-dir)
      APP_DIR="$2"
      shift 2
      ;;
    --app-user)
      APP_USER="$2"
      shift 2
      ;;
    --sync-mode)
      SYNC_MODE="$2"
      shift 2
      ;;
    --service-name)
      SERVICE_NAME="$2"
      shift 2
      ;;
    --healthcheck-url)
      HEALTHCHECK_URL="$2"
      shift 2
      ;;
    --repo-ssh-password)
      REPO_SSH_PASSWORD="$2"
      shift 2
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      echo "Unknown argument: $1"
      usage
      exit 1
      ;;
  esac
done

if [[ -z "$ROLE" || -z "$REPO_URL" ]]; then
  echo "--role and --repo-url are required"
  usage
  exit 1
fi

if [[ "$ROLE" != "central" && "$ROLE" != "agent" ]]; then
  echo "--role must be central or agent"
  exit 1
fi

if [[ "$SYNC_MODE" != "pull" && "$SYNC_MODE" != "force" ]]; then
  echo "--sync-mode must be pull or force"
  exit 1
fi

if [[ -z "$SERVICE_NAME" ]]; then
  if [[ "$ROLE" == "central" ]]; then
    SERVICE_NAME="vlm-central"
  else
    SERVICE_NAME="vlm-agent"
  fi
fi

VENV_DIR="$APP_DIR/.venv"

is_ssh_repo() {
  [[ "$REPO_URL" == ssh://* || "$REPO_URL" == *@*:* ]]
}

git_repo_cmd() {
  if is_ssh_repo; then
    if [[ -n "$REPO_SSH_PASSWORD" ]]; then
      SSHPASS="$REPO_SSH_PASSWORD" GIT_SSH_COMMAND="ssh -o StrictHostKeyChecking=accept-new" sshpass -e git "$@"
    else
      GIT_SSH_COMMAND="ssh -o StrictHostKeyChecking=accept-new" git "$@"
    fi
  else
    git "$@"
  fi
}

git_repo_cmd_as_app_user() {
  if [[ "$APP_USER" == "root" ]]; then
    git_repo_cmd "$@"
    return
  fi

  if is_ssh_repo; then
    if [[ -n "$REPO_SSH_PASSWORD" ]]; then
      sudo -u "$APP_USER" env SSHPASS="$REPO_SSH_PASSWORD" GIT_SSH_COMMAND="ssh -o StrictHostKeyChecking=accept-new" sshpass -e git "$@"
    else
      sudo -u "$APP_USER" env GIT_SSH_COMMAND="ssh -o StrictHostKeyChecking=accept-new" git "$@"
    fi
  else
    sudo -u "$APP_USER" git "$@"
  fi
}

echo "[1/6] Installing runtime packages"
apt-get update -y
apt-get install -y git python3 python3-venv python3-pip curl
if is_ssh_repo && [[ -n "$REPO_SSH_PASSWORD" ]]; then
  apt-get install -y sshpass
fi

echo "[2/6] Ensuring ownership"
if [[ "$APP_USER" != "root" ]] && ! id -u "$APP_USER" >/dev/null 2>&1; then
  echo "User '$APP_USER' does not exist, creating it"
  useradd -m -s /bin/bash "$APP_USER"
fi

parent_dir="$(dirname "$APP_DIR")"
install -d -m 0755 "$parent_dir"
if [[ "$APP_USER" != "root" ]]; then
  chown "$APP_USER":"$APP_USER" "$parent_dir"
fi

echo "[3/6] Getting source code"
if [[ ! -d "$APP_DIR/.git" ]]; then
  rm -rf "$APP_DIR"
  git_repo_cmd_as_app_user clone --branch "$BRANCH" "$REPO_URL" "$APP_DIR"
else
  git_repo_cmd_as_app_user -C "$APP_DIR" remote set-url origin "$REPO_URL"
  git_repo_cmd_as_app_user -C "$APP_DIR" fetch --all --prune
  if [[ "$SYNC_MODE" == "force" ]]; then
    git_repo_cmd_as_app_user -C "$APP_DIR" checkout "$BRANCH"
    git_repo_cmd_as_app_user -C "$APP_DIR" reset --hard "origin/$BRANCH"
    git_repo_cmd_as_app_user -C "$APP_DIR" clean -fd
  else
    git_repo_cmd_as_app_user -C "$APP_DIR" checkout "$BRANCH"
    git_repo_cmd_as_app_user -C "$APP_DIR" pull --ff-only origin "$BRANCH"
  fi
fi

chown -R "$APP_USER":"$APP_USER" "$APP_DIR"

echo "[4/6] Installing Python dependencies"
sudo -u "$APP_USER" python3 -m venv "$VENV_DIR"
sudo -u "$APP_USER" "$VENV_DIR/bin/pip" install --upgrade pip wheel
if [[ "$ROLE" == "central" ]]; then
  sudo -u "$APP_USER" "$VENV_DIR/bin/pip" install -r "$APP_DIR/central/requirements.txt"
else
  sudo -u "$APP_USER" "$VENV_DIR/bin/pip" install -r "$APP_DIR/vm-agent/requirements.txt"
fi

echo "[5/6] Restarting service $SERVICE_NAME"
systemctl daemon-reload
systemctl enable "$SERVICE_NAME"
systemctl restart "$SERVICE_NAME"
systemctl is-active --quiet "$SERVICE_NAME"

echo "[6/6] Optional healthcheck"
if [[ -n "$HEALTHCHECK_URL" ]]; then
  for _ in $(seq 1 20); do
    if curl -fsS "$HEALTHCHECK_URL" >/dev/null 2>&1; then
      echo "Healthcheck OK: $HEALTHCHECK_URL"
      exit 0
    fi
    sleep 2
  done
  echo "Healthcheck failed: $HEALTHCHECK_URL"
  exit 1
fi

echo "Deployment completed successfully"

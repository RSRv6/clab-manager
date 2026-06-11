#!/usr/bin/env bash
# vlm-push-agents.sh — Mise à jour robuste des agents via SCP direct
#
# Principe :
#   1. Lit le fichier hosts (format CSV 8 colonnes)
#   2. Pour chaque agent, indépendamment (pas de set -e global) :
#      a. Vérifie la santé avant déploiement (pré-check)
#      b. Crée un backup de vm-agent/src/ sur l'agent
#      c. Copie les sources locales vm-agent/src/ via SCP (rsync sur SSH)
#      d. Redémarre le service vlm-agent
#      e. Attend le retour du healthcheck (15 s max)
#      f. Si KO → restaure le backup et redémarre (rollback automatique)
#   3. Affiche un résumé coloré OK / FAIL / SKIP par hôte
#
# Pas de git, pas de pip, pas d'apt sur les agents.
# Suppose que l'environnement Python est déjà installé (setup initial fait par deploy-over-ssh.sh).
#
# Usage :
#   vlm-push-agents.sh [--hosts-file <file>] [--src-dir <path>] [--app-dir <path>]
#                      [--service <name>] [--port <n>] [--skip-precheck] [--dry-run]
#
# Hosts file format (8 colonnes) :
#   vm_id,name,ip,token,port,ssh_user,ssh_password,sudo_password

set -uo pipefail

# ── Couleurs ──────────────────────────────────────────────────────────────────
RED='\033[0;31m'; GRN='\033[0;32m'; YEL='\033[0;33m'
BLU='\033[0;34m'; CYN='\033[0;36m'; BOLD='\033[1m'; NC='\033[0m'
info()    { echo -e "${BLU}[INFO]${NC}  $*"; }
ok()      { echo -e "${GRN}[ OK ]${NC}  $*"; }
warn()    { echo -e "${YEL}[WARN]${NC}  $*"; }
fail()    { echo -e "${RED}[FAIL]${NC}  $*"; }
step()    { echo -e "${BOLD}${CYN}━━ $* ${NC}"; }

# ── Valeurs par défaut ────────────────────────────────────────────────────────
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd "$SCRIPT_DIR/../.." && pwd)"

HOSTS_FILE="$ROOT_DIR/deployment/hosts/agent_hosts_all.txt"
SRC_DIR="$ROOT_DIR/vm-agent/src"
APP_DIR="/opt/virtual-labs-management"
SERVICE_NAME="vlm-agent"
AGENT_PORT="8081"
SKIP_PRECHECK="false"
DRY_RUN="false"
HEALTH_TIMEOUT=30   # secondes max pour le healthcheck post-deploy
SSH_TIMEOUT=10      # timeout SSH/SCP par commande

usage() {
  cat <<EOF
Usage: $(basename "$0") [options]

Options:
  --hosts-file <file>   Fichier hosts agents (défaut: deployment/hosts/agent_hosts_all.txt)
  --src-dir <path>      Source locale à copier (défaut: vm-agent/src)
  --app-dir <path>      Répertoire app sur l'agent (défaut: /opt/virtual-labs-management)
  --service <name>      Nom du service systemd (défaut: vlm-agent)
  --port <n>            Port healthcheck (défaut: 8081)
  --skip-precheck       Ne pas vérifier la santé avant déploiement
  --dry-run             Affiche ce qui serait fait, sans exécuter
  -h, --help            Affiche cette aide

Format du fichier hosts (CSV 8 colonnes) :
  vm_id,name,ip,token,port,ssh_user,ssh_password,sudo_password

Exemple :
  $(basename "$0") --hosts-file deployment/hosts/agent_hosts_all.txt
EOF
}

# ── Parsing des arguments ─────────────────────────────────────────────────────
while [[ $# -gt 0 ]]; do
  case "$1" in
    --hosts-file)   HOSTS_FILE="$2"; shift 2 ;;
    --src-dir)      SRC_DIR="$2"; shift 2 ;;
    --app-dir)      APP_DIR="$2"; shift 2 ;;
    --service)      SERVICE_NAME="$2"; shift 2 ;;
    --port)         AGENT_PORT="$2"; shift 2 ;;
    --skip-precheck) SKIP_PRECHECK="true"; shift ;;
    --dry-run)      DRY_RUN="true"; shift ;;
    -h|--help)      usage; exit 0 ;;
    *) echo "Argument inconnu: $1"; usage; exit 1 ;;
  esac
done

# ── Vérifications préalables ──────────────────────────────────────────────────
if [[ ! -f "$HOSTS_FILE" ]]; then
  fail "Fichier hosts introuvable: $HOSTS_FILE"
  exit 1
fi

if [[ ! -d "$SRC_DIR" ]]; then
  fail "Répertoire source introuvable: $SRC_DIR"
  exit 1
fi

if ! command -v sshpass &>/dev/null; then
  fail "sshpass n'est pas installé (sudo apt-get install -y sshpass)"
  exit 1
fi

DEST_SRC="$APP_DIR/vm-agent/src"
BACKUP_BASE="$APP_DIR/vm-agent/.src-backup"

# ── Fonctions SSH/SCP ─────────────────────────────────────────────────────────
_ssh() {
  local pass="$1"; shift
  # -n : stdin depuis /dev/null pour éviter que SSH consomme le stdin du while-loop
  sshpass -p "$pass" ssh \
    -o StrictHostKeyChecking=no \
    -o ConnectTimeout=$SSH_TIMEOUT \
    -o BatchMode=no \
    -n \
    "$@"
}

_scp_dir() {
  local pass="$1"; local src="$2"; local dst="$3"
  sshpass -p "$pass" scp \
    -o StrictHostKeyChecking=no \
    -o ConnectTimeout=$SSH_TIMEOUT \
    -r "$src" "$dst"
}

# Exécute une commande sudo sur l'agent (passe le mot de passe via stdin)
_sudo() {
  local ssh_pass="$1"; local sudo_pass="$2"; local user_host="$3"; shift 3
  _ssh "$ssh_pass" "$user_host" "echo '$sudo_pass' | sudo -S $* 2>&1"
}

healthcheck() {
  local ip="$1"; local port="$2"
  curl -fsS --max-time 5 "http://$ip:$port/health" &>/dev/null
}

wait_healthy() {
  local ip="$1"; local port="$2"; local timeout="$3"
  local elapsed=0
  while [[ $elapsed -lt $timeout ]]; do
    if healthcheck "$ip" "$port"; then
      return 0
    fi
    sleep 2
    elapsed=$((elapsed + 2))
  done
  return 1
}

# ── Compteurs ─────────────────────────────────────────────────────────────────
total=0
count_ok=0
count_fail=0
count_skip=0
declare -A host_results   # ip -> OK|FAIL|SKIP:raison

# ── Boucle principale ─────────────────────────────────────────────────────────
echo ""
step "VLM Push Agents — $(date '+%Y-%m-%d %H:%M:%S')"
info "Source  : $SRC_DIR"
info "Dest    : $DEST_SRC"
info "Service : $SERVICE_NAME"
info "Hosts   : $HOSTS_FILE"
[[ "$DRY_RUN" == "true" ]] && warn "MODE DRY-RUN activé, aucune modification ne sera appliquée"
echo ""

while IFS= read -r raw_line; do
  # Ignorer commentaires et lignes vides
  line="${raw_line%%#*}"
  line="$(echo "$line" | xargs 2>/dev/null || echo "$line")"
  [[ -z "$line" ]] && continue

  total=$((total + 1))

  # Parser les 8 colonnes
  IFS=',' read -r _id _name ip _token _port ssh_user ssh_pass sudo_pass _extra <<< "$line"
  ip="$(echo "$ip" | xargs)"
  ssh_user="$(echo "$ssh_user" | xargs)"
  ssh_pass="$(echo "$ssh_pass" | xargs)"
  sudo_pass="$(echo "$sudo_pass" | xargs)"
  _name="$(echo "$_name" | xargs)"

  label="${_name:-$ip}"
  user_host="$ssh_user@$ip"

  step "[$label] $ip"

  if [[ "$DRY_RUN" == "true" ]]; then
    info "DRY-RUN: scp $SRC_DIR → $user_host:$DEST_SRC puis restart $SERVICE_NAME"
    host_results["$ip"]="SKIP:dry-run"
    count_skip=$((count_skip + 1))
    echo ""
    continue
  fi

  # ── a. Pré-check ──────────────────────────────────────────────────────────
  pre_healthy=false
  if [[ "$SKIP_PRECHECK" == "false" ]]; then
    if healthcheck "$ip" "$AGENT_PORT"; then
      pre_healthy=true
      info "Pré-check: agent UP"
    else
      warn "Pré-check: agent DOWN (déploiement quand même tenté)"
    fi
  else
    info "Pré-check ignoré (--skip-precheck)"
    pre_healthy=true
  fi

  # ── b. Backup sur l'agent ─────────────────────────────────────────────────
  ts="$(date '+%Y%m%d_%H%M%S')"
  backup_path="${BACKUP_BASE}-${ts}"
  info "Backup: $DEST_SRC → $backup_path"
  # The ssh user owns the app directory — no sudo needed for backup
  if ! _ssh "$ssh_pass" "$user_host" "cp -a '$DEST_SRC' '$backup_path' 2>&1 && echo 'backup_ok'" | grep -q backup_ok; then
    warn "Backup échoué (répertoire peut-être inexistant sur agent neuf, on continue)"
  fi

  # ── c. Copie SCP ──────────────────────────────────────────────────────────
  # Créer un répertoire temporaire dans /tmp en tant que ssh_user (pas de sudo)
  # /tmp uses sticky bit but the ssh user can write there freely
  tmp_dir="/tmp/vlm-src-push-$ts"
  if ! _ssh "$ssh_pass" "$user_host" "mkdir -p '$tmp_dir' && echo mkdir_ok" | grep -q mkdir_ok; then
    fail "Impossible de créer $tmp_dir sur $ip"
    host_results["$ip"]="FAIL:mkdir tmp échoué"
    count_fail=$((count_fail + 1))
    echo ""
    continue
  fi

  info "SCP: copie de $SRC_DIR vers $user_host:$tmp_dir/"
  # scp -r localdir user@host:/tmp/dir/ → copie le contenu de localdir dans /tmp/dir/
  # Le dossier arrivé: /tmp/dir/$basename_src/
  src_basename="$(basename "$SRC_DIR")"

  if ! _scp_dir "$ssh_pass" "$SRC_DIR" "$ssh_user@$ip:$tmp_dir/"; then
    fail "SCP échoué vers $ip"
    host_results["$ip"]="FAIL:SCP échoué"
    count_fail=$((count_fail + 1))
    _ssh "$ssh_pass" "$user_host" "rm -rf '$tmp_dir'" 2>/dev/null || true
    echo ""
    continue
  fi

  # Remplacer le répertoire de destination (mv atomique)
  # The app dir is owned by ssh_user → no sudo needed for rm/mv
  replace_cmd="rm -rf '${DEST_SRC}' && mv '${tmp_dir}/${src_basename}' '${DEST_SRC}' && rm -rf '${tmp_dir}'"
  if ! _ssh "$ssh_pass" "$user_host" "$replace_cmd 2>&1 && echo replace_ok" | grep -q replace_ok; then
    fail "Remplacement de $DEST_SRC échoué sur $ip"
    host_results["$ip"]="FAIL:remplacement src échoué"
    count_fail=$((count_fail + 1))
    _ssh "$ssh_pass" "$user_host" "rm -rf '$tmp_dir'" 2>/dev/null || true
    # Rollback backup
    if [[ "$pre_healthy" == "true" ]]; then
      warn "Rollback: restauration du backup $backup_path"
      _ssh "$ssh_pass" "$user_host" "rm -rf '$DEST_SRC' && mv '$backup_path' '$DEST_SRC'" 2>/dev/null || true
    fi
    echo ""
    continue
  fi

  ok "Fichiers copiés"

  # ── d. Restart service ────────────────────────────────────────────────────
  info "Restart: $SERVICE_NAME"
  if ! _sudo "$ssh_pass" "$sudo_pass" "$user_host" "systemctl restart '$SERVICE_NAME'"; then
    fail "systemctl restart $SERVICE_NAME échoué sur $ip"
    host_results["$ip"]="FAIL:restart échoué"
    count_fail=$((count_fail + 1))
    # Rollback si on avait un agent sain avant
    if [[ "$pre_healthy" == "true" ]]; then
      warn "Rollback: restauration du backup et redémarrage"
      _sudo "$ssh_pass" "$sudo_pass" "$user_host" \
        "rm -rf '$DEST_SRC' && mv '$backup_path' '$DEST_SRC' && systemctl restart '$SERVICE_NAME'" 2>/dev/null || true
    fi
    echo ""
    continue
  fi

  # ── e. Healthcheck post-deploy ────────────────────────────────────────────
  info "Healthcheck (attente max ${HEALTH_TIMEOUT}s)..."
  if wait_healthy "$ip" "$AGENT_PORT" "$HEALTH_TIMEOUT"; then
    ok "Agent opérationnel: http://$ip:$AGENT_PORT/health"
    # Supprimer le backup maintenant qu'on sait que le deploy est bon
    _ssh "$ssh_pass" "$user_host" "rm -rf '$backup_path'" 2>/dev/null || true
    host_results["$ip"]="OK"
    count_ok=$((count_ok + 1))
  else
    # ── f. Rollback automatique ───────────────────────────────────────────
    fail "Healthcheck KO après ${HEALTH_TIMEOUT}s — ROLLBACK en cours"
    rollback_ok=false
    if _ssh "$ssh_pass" "$user_host" \
        "rm -rf '$DEST_SRC' && mv '$backup_path' '$DEST_SRC'"; then
      _sudo "$ssh_pass" "$sudo_pass" "$user_host" "systemctl restart '$SERVICE_NAME'" 2>/dev/null || true
      sleep 5
      if wait_healthy "$ip" "$AGENT_PORT" 15; then
        warn "Rollback OK — agent restauré à l'état précédent"
        rollback_ok=true
      fi
    fi
    if [[ "$rollback_ok" == "false" ]]; then
      fail "ROLLBACK ÉCHOUÉ — intervention manuelle requise sur $ip"
      host_results["$ip"]="FAIL:healthcheck+rollback KO"
    else
      host_results["$ip"]="FAIL:deploy KO (rollback OK)"
    fi
    count_fail=$((count_fail + 1))
  fi

  echo ""
done < "$HOSTS_FILE"

# ── Résumé ────────────────────────────────────────────────────────────────────
echo ""
step "Résumé"
echo -e "  Agents traités : ${BOLD}$total${NC}"
echo -e "  ${GRN}${BOLD}OK  : $count_ok${NC}"
if [[ $count_fail -gt 0 ]]; then
  echo -e "  ${RED}${BOLD}FAIL: $count_fail${NC}"
else
  echo -e "  FAIL: $count_fail"
fi
if [[ $count_skip -gt 0 ]]; then
  echo -e "  ${YEL}SKIP: $count_skip${NC}"
fi
echo ""
echo -e "  Détail par hôte:"
for ip in "${!host_results[@]}"; do
  result="${host_results[$ip]}"
  if [[ "$result" == "OK" ]]; then
    echo -e "    ${GRN}✓${NC} $ip"
  elif [[ "$result" == SKIP:* ]]; then
    echo -e "    ${YEL}~${NC} $ip  (${result#SKIP:})"
  else
    echo -e "    ${RED}✗${NC} $ip  (${result#FAIL:})"
  fi
done
echo ""

if [[ $count_fail -gt 0 ]]; then
  fail "$count_fail agent(s) en échec"
  exit 1
fi

ok "Tous les agents mis à jour avec succès ($count_ok/$total)"
exit 0

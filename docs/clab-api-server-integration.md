# Intégration clab-api-server

## Vue d'ensemble

VLM supporte deux modes de collecte de données pour les VMs :

| Mode | Source | Quand l'utiliser |
|------|--------|-----------------|
| **vm-agent only** | vm-agent (port 8081) | VMs sans clab-api-server installé |
| **clab-api** | clab-api-server (port 8080) + vm-agent | VMs avec clab-api-server — source de vérité pour l'état des labs |

Le mode est activé par VM dans `central/config.yaml` :

```yaml
agents:
- id: vm1
  name: clab-vm-01
  ip: 198.18.21.101
  port: 8081
  token: <vm-agent-token>
  use_clab_api_server: true          # active l'intégration
  clab_api_base_url: http://198.18.21.101:8080
  clab_api_user: vlm-central
  clab_api_password: <password>
```

## Architecture

```
Central Server
    │
    ├─ vm-agent (:8081)  ← labs non-migrés, SSH/config management
    │
    └─ clab-api (:8080)  ← source de vérité pour labs migrés
         ├─ GET /api/v1/labs          → liste et état des labs
         ├─ GET /api/v1/health/metrics → ressources CPU/RAM/Disk
         └─ GET /api/v1/labs/{name}/topology/yaml → topologie pour le graphe
```

Quand `use_clab_api_server: true` :
- Les **infos labs** (état, node_count, topology) viennent du clab-api
- Les **ressources** (CPU/RAM/Disk) viennent du clab-api (fallback vm-agent)
- Les **actions lab** (start/stop/redeploy) passent par le **vm-agent** via clab
- Les **actions config** (reconfigure, config-diff, export, set-default) passent par le **vm-agent** directement

## Installation du clab-api-server sur une VM

### Prérequis

- User `vlm-central` créé avec accès `docker` group
- Binaire `clab-api-server` ≥ v0.4.0 dans `/usr/local/bin/`

### Étapes

```bash
# 1. Créer l'utilisateur vlm-central
useradd -m -s /bin/bash vlm-central
echo "vlm-central:vlm-C3ntr@l-2026" | chpasswd
usermod -aG docker,clab_admins vlm-central

# 2. Copier le binaire (depuis une VM déjà déployée ou depuis GitHub releases)
cp /path/to/clab-api-server /usr/local/bin/clab-api-server
chmod +x /usr/local/bin/clab-api-server

# 3. Configurer clab-api-server
mkdir -p /etc/clab-api-server
cat > /etc/clab-api-server/clab-api-server.env << EOF
API_PORT=8080
JWT_SECRET=vlm-jwt-s3cr3t-clab-api-server-2026-rand
JWT_EXPIRATION=24h
TLS_ENABLE=false
GIN_MODE=release
EOF

# 4. PAM authentication
echo "@include common-auth" > /etc/pam.d/clab-api

# 5. Service systemd
cat > /etc/systemd/system/clab-api-server.service << 'EOF'
[Unit]
Description=containerlab API server
After=network.target docker.service
Requires=docker.service

[Service]
Type=simple
User=root
EnvironmentFile=/etc/clab-api-server/clab-api-server.env
ExecStart=/usr/local/bin/clab-api-server
Restart=always
RestartSec=5
LimitNOFILE=65535

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable --now clab-api-server

# 6. Migrer le vm-agent vers vlm-central
sed -i 's/^User=.*/User=vlm-central/' /etc/systemd/system/vlm-agent.service
sed -i 's/^Group=.*/Group=vlm-central/' /etc/systemd/system/vlm-agent.service

USER_HOME=$(getent passwd vlm-central | cut -d: -f6)
mkdir -p "${USER_HOME}/labs/config_db"
chown -R vlm-central:vlm-central "${USER_HOME}/labs"

# Mettre à jour LAB_CONFIG_DB_ROOT
sed -i "s|LAB_CONFIG_DB_ROOT=.*|LAB_CONFIG_DB_ROOT=${USER_HOME}/labs/config_db|" \
  /etc/default/vlm-agent || \
  echo "LAB_CONFIG_DB_ROOT=${USER_HOME}/labs/config_db" >> /etc/default/vlm-agent

chown -R vlm-central:vlm-central /opt/virtual-labs-management/log

systemctl daemon-reload
systemctl restart vlm-agent

# 7. Copier les topologies
cp /home/clab21/labs/*.yaml /home/clab21/labs/*.yml "${USER_HOME}/labs/" 2>/dev/null || true
chown -R vlm-central:vlm-central "${USER_HOME}/labs/"
```

### Script automatisé

Le script `deployment/scripts/install-systemd-agent.sh` gère maintenant ces étapes automatiquement avec `--app-user vlm-central` (valeur par défaut).

## Gestion des positions de graphe

Les positions des nœuds dans le graphe de topologie sont stockées dans :
```
central/data/lab_graph_positions.json
```

Format :
```json
{
  "nom-du-lab": {
    "NomNoeud": {"x": 160.0, "y": 280.0},
    ...
  }
}
```

### Synchronisation automatique

Quand un lab est découvert via clab-api sans positions dans le JSON central, le collector :
1. Interroge le vm-agent pour récupérer les positions (qui lit les fichiers `.annotations.json` locaux)
2. Persiste automatiquement les positions dans `lab_graph_positions.json`

Les polls suivants utilisent directement le JSON central sans requête supplémentaire.

### Ajout manuel de positions

Si les positions ne sont pas encore dans les annotations containerlab, les ajouter directement :

```python
import json

with open("central/data/lab_graph_positions.json") as f:
    data = json.load(f)

data["mon-lab"] = {
    "Node1": {"x": 100.0, "y": 200.0},
    "Node2": {"x": 300.0, "y": 200.0},
}

with open("central/data/lab_graph_positions.json", "w") as f:
    json.dump(data, f, indent=2)
```

## Points d'attention

### Ne pas utiliser `docker restart` sur des containers containerlab

`docker restart` supprime les interfaces veth gérées par containerlab (interfaces data eth2+). Cela empêche iouyap de démarrer et coupe la connectivité SSH.

**Toujours utiliser** :
- VLM GUI → Start/Stop du lab
- `clab stop` + `clab start` (ou redeploy via VLM)

### Labs IOL (cisco_iol) — SSH après redeploy

Les containers IOL ont besoin que `crypto key generate rsa modulus 2048` soit dans le `boot_config.txt` de démarrage pour que SSH fonctionne après un redeploy (NVRAM vierge sur les nouveaux containers).

Le fichier `boot_config.txt` est généré par containerlab lors du premier déploiement et inclut cette commande. Les configs sauvegardées via `set-default-config` ne l'incluent pas — elles sont à appliquer via `reconfigure` après le démarrage SSH.

### Ownership des labs

Tous les labs sur une VM doivent être gérés par le même utilisateur que le vm-agent (`vlm-central`). Éviter de mixer `clab21` et `vlm-central` pour éviter les conflits de state containerlab.

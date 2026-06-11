# MODE OPÉRATOIRE SANS ANSIBLE – Déploiement & mise à jour (scripts)

Ce mode opératoire est fait pour ton cas:
- pas d’Ansible,
- déploiement/mise à jour via scripts + SSH,
- Git possiblement non initialisé.

## 1) Créer le dépôt Git (si non créé)

Dans le projet local:

```bash
cd /chemin/vers/virtual-labs-management
git init
git add .
git commit -m "Initial commit virtual-labs-management"
git branch -M main
git remote add origin <URL_DU_NOUVEAU_REPO>
git push -u origin main
```

Exemple URL:
- HTTPS: `https://github.com/<user>/virtual-labs-management.git`
- SSH: `git@github.com:<user>/virtual-labs-management.git`

## 2) Préparer les fichiers hôtes

Créer 2 fichiers sur la machine d’admin:

`deployment/hosts/central_hosts.txt`

```txt
10.10.10.10
```

`deployment/hosts/agent_hosts.txt`

```txt
vm1,clab-vm-01,10.10.10.21,change-me,8081,ubuntu
vm2,clab-vm-02,10.10.10.22,change-me,8081,ubuntu
```

Format recommandé: `id,name,ip,token,port[,ssh_user]`

`ssh_user` est optionnel et surcharge `--ssh-user` pour l'hôte concerné.

## 3) Déploiement initial du central

```bash
cd /chemin/vers/virtual-labs-management
chmod +x deployment/scripts/*.sh

deployment/scripts/deploy-over-ssh.sh \
  --role central \
  --hosts-file deployment/hosts/central_hosts.txt \
  --ssh-user ubuntu \
  --branch main \
  --app-dir /opt/virtual-labs-management \
  --app-user clab-user \
  --port 8090 \
  --sync-mode pull
```

### Option plus simple: script interactif local (menu)

Si tu veux un seul script qui te demande quoi installer (`central`, `agent`, `both`) et enchaîne pull + install + services + checks:

```bash
cd /opt/virtual-labs-management
chmod +x deployment/scripts/*.sh
sudo bash deployment/scripts/vlm-deploy-menu.sh
```

Le script te demandera:
- le repo Git,
- la branche,
- l’utilisateur de service,
- ce que tu veux installer,
- les paramètres de port/config.

## 4) Déploiement initial des VM agents

```bash
deployment/scripts/deploy-over-ssh.sh \
  --role agent \
  --hosts-file deployment/hosts/agent_hosts.txt \
  --ssh-user ubuntu \
  --branch main \
  --app-dir /opt/virtual-labs-management \
  --app-user clab-user \
  --port 8081 \
  --sync-mode pull
```

Par défaut, pour `--role agent`, le script synchronise automatiquement les nouveaux agents dans `central/config.yaml`.
Utiliser `--sync-central-config false` pour désactiver ce comportement.

## 4.b) Mise à jour globale en une commande (agents + option central)

Script recommandé:

```bash
chmod +x deployment/scripts/update-fleet.sh

deployment/scripts/update-fleet.sh \
  --ssh-user ubuntu \
  --branch main
```

Ce script:
- met à jour tous les vm-agents listés dans `deployment/hosts/agent_hosts.txt`,
- redémarre les services (via le workflow existant),
- fait les healthchecks,
- synchronise les nouveaux agents dans `central/config.yaml`.

Pour mettre à jour aussi le central dans la même commande:

```bash
deployment/scripts/update-fleet.sh \
  --ssh-user ubuntu \
  --branch main \
  --update-central true \
  --central-hosts-file deployment/hosts/central_hosts.txt
```

## 5) Mise à jour après modification de code

### 5.1 Push du code

```bash
cd /chemin/vers/virtual-labs-management
git add .
git commit -m "Update features"
git push origin main
```

### 5.2 Update des serveurs

Central:

```bash
deployment/scripts/deploy-over-ssh.sh \
  --role central \
  --hosts-file deployment/hosts/central_hosts.txt \
  --ssh-user ubuntu \
  --branch main \
  --app-user clab-user \
  --sync-mode pull
```

Agents:

```bash
deployment/scripts/deploy-over-ssh.sh \
  --role agent \
  --hosts-file deployment/hosts/agent_hosts.txt \
  --ssh-user ubuntu \
  --branch main \
  --app-user clab-user \
  --sync-mode pull
```

## 6) Rollback rapide

Si tu dois revenir à l’état exact du remote (écrase local serveur):

```bash
deployment/scripts/deploy-over-ssh.sh \
  --role agent \
  --hosts-file deployment/hosts/agent_hosts.txt \
  --ssh-user ubuntu \
  --branch main \
  --app-user clab-user \
  --sync-mode force
```

Même principe pour le central (`--role central`).

## 7) Vérifications

Sur central:

```bash
ssh ubuntu@10.10.10.10 "systemctl status vlm-central --no-pager"
ssh ubuntu@10.10.10.10 "curl -sS http://127.0.0.1:8090/health"
```

Sur un agent:

```bash
ssh ubuntu@10.10.10.21 "systemctl status vlm-agent --no-pager"
ssh ubuntu@10.10.10.21 "curl -sS http://127.0.0.1:8081/health"
```

## 8) Notes importantes

- Le script installe: `git`, `python3`, `venv`, `pip`, `curl`.
- Le repo est auto-détecté depuis le remote Git local `origin` (option `--repo-url` possible pour override).
- Les services utilisés:
  - `vlm-central`
  - `vlm-agent`
- Les env files générés:
  - `/etc/default/vlm-central`
  - `/etc/default/vlm-agent`

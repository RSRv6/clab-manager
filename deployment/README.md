# MODE OPÉRATOIRE COMPLET – Déploiement & mise à jour (Central + VM Agents)

Ce document décrit une procédure **standardisée, répétable et exploitable** pour:
- déployer le central et les agents sur les serveurs finaux,
- mettre à jour rapidement après ajout/modification de fonctions,
- revenir en arrière (rollback) en cas de problème.

La stack d’automatisation repose sur:
- `systemd` (exécution persistante des services),
- `deployment/scripts/vlm-deploy-update.sh` (script idempotent d’installation/update),
- `deployment/ansible/playbook.yml` (orchestration central + agents).

---

## 0) Pré-requis

### 0.1 Côté machine d’administration (Ansible)
- Linux avec accès SSH vers tous les serveurs cibles.
- `ansible` installé.
- Une clé SSH autorisée sur les serveurs.

Exemple installation rapide:

```bash
sudo apt-get update
sudo apt-get install -y ansible sshpass
```

### 0.2 Côté serveurs cibles (central + agents)
- OS Linux avec `sudo`.
- Accès réseau:
	- vers le repo Git,
	- du central vers les agents (HTTP API agent).

### 0.3 Variables applicatives à connaître
- URL du dépôt (`vlm_repo_url`).
- Branche de déploiement (`vlm_branch`, ex: `main` ou `prod`).
- Utilisateur de service (`vlm_app_user`, ex: `clab-user`).
- Ports:
	- central (par défaut 80 ; 443 si certificat TLS configuré),
	- agent (par défaut 8081).

---

## 1) Préparation des fichiers Ansible

Depuis la racine du projet:

```bash
cd deployment/ansible
cp inventory.ini.example inventory.ini
cp group_vars/all.yml.example group_vars/all.yml
```

### 1.1 Renseigner l’inventaire

Fichier: `deployment/ansible/inventory.ini`

```ini
[central_hosts]
central-01 ansible_host=10.10.10.10 ansible_user=ubuntu

[vm_agents]
agent-01 ansible_host=10.10.10.21 ansible_user=ubuntu
agent-02 ansible_host=10.10.10.22 ansible_user=ubuntu
```

### 1.2 Renseigner les variables globales

Fichier: `deployment/ansible/group_vars/all.yml`

Champs minimum à adapter:
- `vlm_repo_url`
- `vlm_branch`
- `vlm_app_user`
- `central_port`
- `agent_port`
- éventuellement `vlm_sync_mode` (`pull` ou `force`)

---

## 2) Vérification pré-déploiement (obligatoire)

Depuis `deployment/ansible`:

```bash
ansible -i inventory.ini all -m ping
```

Résultat attendu:
- tous les hôtes répondent `pong`.

Si échec:
- vérifier connectivité SSH,
- vérifier la clé SSH,
- vérifier `ansible_user` et droits sudo.

---

## 3) Déploiement initial (J0)

Commande unique:

```bash
cd deployment/ansible
ansible-playbook -i inventory.ini playbook.yml
```

### 3.1 Ce que fait exactement le playbook
1. crée/valide l’utilisateur applicatif,
2. installe `/usr/local/bin/vlm-deploy-update.sh`,
3. génère les fichiers d’environnement:
	 - `/etc/default/vlm-central`
	 - `/etc/default/vlm-agent`
4. installe les unités `systemd`:
	 - `vlm-central.service`
	 - `vlm-agent.service`
5. clone/synchronise le dépôt dans `vlm_app_dir`,
6. crée/met à jour le `.venv`, installe les dépendances,
7. redémarre les services,
8. fait un healthcheck local (`/health`).

---

## 4) Procédure standard de mise à jour (RUN)

Après commit/push de code:

```bash
cd deployment/ansible
ansible-playbook -i inventory.ini playbook.yml
```

**Important:** la commande est identique au déploiement initial (idempotence).

### 4.1 Modes Git disponibles

Dans `group_vars/all.yml`:
- `vlm_sync_mode: pull` (recommandé en RUN)
	- fait `git pull --ff-only`
- `vlm_sync_mode: force` (maintenance/urgence)
	- force l’état exact de `origin/<branch>`
	- (`reset --hard` + `clean -fd`)

---

## 5) Rollback (retour arrière)

### Méthode recommandée
1. identifier le commit/tag stable précédent,
2. positionner `vlm_branch` sur ce tag/branche stable,
3. relancer le playbook.

Exemple:

```bash
# dans group_vars/all.yml
vlm_branch: "v1.2.3"

# puis
ansible-playbook -i inventory.ini playbook.yml
```

Pour forcer une remise à plat complète du code:

```yaml
vlm_sync_mode: "force"
```

---

## 6) Vérifications post-déploiement (checklist)

### 6.1 Services

Sur central:

```bash
systemctl status vlm-central --no-pager
journalctl -u vlm-central -n 100 --no-pager
curl -sS http://127.0.0.1:80/health
```

Sur chaque agent:

```bash
systemctl status vlm-agent --no-pager
journalctl -u vlm-agent -n 100 --no-pager
curl -sS http://127.0.0.1:8081/health
```

### 6.2 Fonctionnel
- depuis le central, vérifier que tous les agents sont `online`,
- lancer une action simple (ex: refresh state),
- vérifier les logs:
	- `log/central.log`
	- `log/vm-agent.log`

---

## 7) Exploitation quotidienne (MCO)

### 7.1 Commandes utiles

```bash
systemctl restart vlm-central
systemctl restart vlm-agent
systemctl daemon-reload
```

### 7.2 Relance d’une seule cible (exemple)

```bash
ansible-playbook -i inventory.ini playbook.yml --limit central_hosts
ansible-playbook -i inventory.ini playbook.yml --limit vm_agents
ansible-playbook -i inventory.ini playbook.yml --limit agent-01
```

---

## 8) Dépannage rapide

### Cas 1: service down après update
- vérifier `journalctl -u vlm-central` ou `journalctl -u vlm-agent`,
- vérifier que les fichiers `/etc/default/vlm-*` contiennent les bons chemins/ports,
- relancer:

```bash
systemctl daemon-reload
systemctl restart vlm-central
systemctl restart vlm-agent
```

### Cas 2: central ne voit pas un agent
- vérifier `base_url` + token dans la config central,
- tester depuis le central:

```bash
curl -sS http://<ip-agent>:8081/health
```

### Cas 3: incohérence de code sur cible
- passer temporairement `vlm_sync_mode: force`,
- relancer le playbook.

---

## 9) Bonnes pratiques recommandées

- Déployer depuis une branche dédiée (ex: `prod`).
- Tagger chaque release (`vX.Y.Z`) avant déploiement.
- Garder `pull` en mode normal, utiliser `force` seulement si nécessaire.
- Toujours exécuter la checklist post-déploiement.
- Conserver ce MODOP comme procédure d’astreinte.

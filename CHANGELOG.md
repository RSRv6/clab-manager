# Changelog

## [Unreleased] — branche feat/clab-api-server

### Nouvelles fonctionnalités

#### Intégration clab-api-server (srl-labs)

Support d'un second backend de collecte par VM, activable par `use_clab_api_server: true` dans `central/config.yaml`.

- **Source de vérité** : clab-api-server (port 8080) devient la source principale pour l'état des labs sur les VMs migrées ; le vm-agent reste utilisé pour les actions SSH (reconfigure, config-diff, export, set-default)
- **Authentification JWT** : `central/src/services/clab_api_auth.py` — cache de tokens JWT par VM avec renouvellement automatique (TTL 55 min)
- **Normalisation** : `_norm_clab_labs`, `_norm_clab_resources` — conversion du format clab-api vers le format VLM unifié
- **Routage des actions** : `invoke_lab_action` route automatiquement vers clab-api ou vm-agent selon l'action et la disponibilité
- **`node_count`** : les labs IOL affichent maintenant le bon nombre de nœuds (était "0 nœuds")
- **Ressources CPU/RAM/Disk** : format corrigé, les métriques s'affichent correctement (était "-")

#### Graphe de topologie — positions automatiques

- `_load_graph_positions` / `_save_graph_positions` : lecture et persistance des positions dans `central/data/lab_graph_positions.json`
- `_enrich_graph` enrichi : interroge le vm-agent comme fallback si le JSON central ne contient pas les positions d'un lab, puis persiste automatiquement les positions découvertes
- `central/data/lab_graph_positions.json` : positions initiales pour InfraNet-lab, InfraCom-lab, sr-mpls-ds-6p-5pe-dh

#### Déploiement — vm-agent en `vlm-central`

`deployment/scripts/install-systemd-agent.sh` :
- Utilisateur par défaut : `root` → `vlm-central`
- Ajout automatique au groupe `docker`
- Configuration de `LAB_CONFIG_DB_ROOT` depuis le home de l'utilisateur service
- Option `--lab-config-db-root` pour override

### Corrections de bugs

#### vm-agent — `config_management_service.py`

- **Python 3.12 `PermissionError`** : `_resolve_lab_db_dir` appelait `Path.exists()` sur des répertoires inaccessibles (ex. `/home/vlm-central/.clab/`). Python 3.12 lève `PermissionError` au lieu de retourner `False`. Ajout de `try/except OSError` sur tous les appels `exists()` et `stat()`.

#### UI — Modal Reconfigure

- **Noms tronqués** (`lab-modals.js`) : `String(router.name).split("-").pop()` prenait le dernier segment après `-` → `CE-11` devenait `"11"`, `...-S` devenait `"S"`. Corrigé en retirant le préfixe `clab-{labName}-` s'il est présent, sans modifier le reste du nom.

#### UI — Topology Builder

- **Images Docker non filtrées** (`topology-builder.js`) : le dropdown d'images affichait toutes les images de la VM sans distinction de famille. Ajout d'un filtre par mots-clés famille (iol/xrd pour Cisco, vmx pour Juniper, etc.).

### Améliorations UI

#### Panneau Admin — badge clab-api-server (`admin-panel.js`)

Chaque carte VM affiche maintenant :
- Badge cyan `● clab-api-server + URL` si `use_clab_api_server: true`
- Badge gris `● vm-agent only` sinon

### Documentation

- `docs/clab-api-server-integration.md` : guide d'installation, architecture, gestion des positions, points d'attention

---

## [1.0.0] — 2026-03-17

- Initial release — VLM community edition

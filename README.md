# agent-saas

Dashboard FastAPI pour gérer des instances Hermes sur Kubernetes. Crée, liste et gère des agents avec interface WebUI, sans Helm ni manifests statiques.

## Architecture

- **Un Deployment par instance** nommé `agent-{hash8}-{subdomain}` (ex: `agent-9ae62006-vjourne-agent`).
- **3 containers par pod** : hermes-agent (port 8642), hermes-dashboard (port 9119), hermes-webui (port 8787).
- **2 PVCs** : `{name}-data` (config, sessions), `{name}-workspace` (fichiers de travail).
- **1 ConfigMap** : `{name}-config` (config.yaml générée depuis les variables d'environnement).
- **1 Ingress** par instance avec cert-manager TLS sur `*.ailab.infocepo.com`.

## Configuration

L'application se configure via des variables d'environnement :

| Variable | Défaut | Description |
|---|---|---|
| `LLM_BASE_URL` | `https://api-nothink.ailab.infocepo.com/v1` | Endpoint API LLM |
| `LLM_API_KEY` | (vide) | Clé API LLM |
| `LLM_PROVIDER` | `infocepo-alias` | Nom du provider |
| `LLM_MODEL` | `ai-thinking` | Modèle par défaut |
| `NOTHINK_API_KEY` | (vide) | Clé API NoThink |
| `NOTHINK_API_URL` | `https://api-nothink.ailab.infocepo.com/v1` | URL NoThink |
| `HERMES_TOOLSETS` | `hermes-cli` | Toolsets disponibles |

Déploiement en production :

```bash
kubectl -n demo1 patch configmap agents-saas-app -p \
  '{"data":{"NOTHINK_API_KEY":"<key>","NOTHINK_API_URL":"https://...","LLM_API_KEY":"<key>"}}' \
  --type merge  # Note: ce patch n'existe pas, utiliser kubectl patch configmap...
```

En pratique, mettre à jour les variables d'environnement dans le ConfigMap et redémarrer le déploiement :

```bash
kubectl -n demo1 set env deploy/agents-saas LLM_API_KEY=<key> LLM_BASE_URL=https://...
kubectl -n demo1 rollout restart deploy/agents-saas
```

## API

### `GET /health`

Retourne le statut de l'application.

```bash
curl http://localhost:8899/health
# {"status":"ok"}
```

### `GET /api/instances`

Liste toutes les instances déployées.

```bash
curl http://localhost:8899/api/instances
# [
#   {"name": "agent-9ae62006-vjourne-agent", "subdomain": "vjourne-agent",
#    "url": "https://vjourne-agent.ailab.infocepo.com", "status": "Running"},
#   ...
# ]
```

### `POST /api/deploy`

Crée une nouvelle instance d'agent.

```bash
curl http://localhost:8899/api/deploy -X POST \
  -H 'Content-Type: application/json' \
  -d '{"subdomain": "my-agent"}'
# {
#   "success": true,
#   "name": "agent-abc12345-my-agent",
#   "subdomain": "my-agent",
#   "url": "https://my-agent.ailab.infocepo.com"
# }
```

### `POST /api/change-password`

Change le mot de passe d'une instance existante. Génère un nouveau hash PBKDF2 et met à jour les données dans le PVC via un pod éphémère.

```bash
curl http://localhost:8899/api/change-password -X POST \
  -H 'Content-Type: application/json' \
  -d '{"subdomain": "my-agent", "new_password": "new-secret"}'
# {"success": true, "subdomain": "my-agent"}
```

Le pod de l'instance est automatiquement recréé pour appliquer le nouveau hash.

## Déploiement initial

L'application agents-saas elle-même se déploie via un ConfigMap :

```bash
# 1. Copier app.py dans le ConfigMap
kubectl -n demo1 create configmap agents-saas-app \
  --from-file=app.py=app.py \
  --dry-run=client -o yaml | kubectl -n demo1 apply -f -

# 2. Redémarrer le déploiement
kubectl -n demo1 rollout restart deploy/agents-saas
```

## Endpoints internes de l'instance

Chaque instance déployée expose 3 ports en interne :

| Container | Port | Service |
|---|---|---|
| hermes-agent | 8642 | Gateway Hermes |
| hermes-dashboard | 9119 | Interface de supervision |
| hermes-webui | 8787 | Interface Web utilisateur |

Le service Kubernetes mape le port 80 vers le port 8787 (WebUI).

## Tests

```bash
python3 -m pytest tests/test_app.py -v
```

## Suppression d'instance

```bash
kubectl -n demo1 delete deployment agent-<hash>-<subdomain>
kubectl -n demo1 delete pvc agent-<hash>-<subdomain>-data agent-<hash>-<subdomain>-workspace
kubectl -n demo1 delete configmap agent-<hash>-<subdomain>-config
kubectl -n demo1 delete service agent-<hash>-<subdomain>-svc
kubectl -n demo1 delete ingress agent-<hash>-<subdomain>-ingress
```

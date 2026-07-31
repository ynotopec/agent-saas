# Agents SaaS — Deploy from Zero

Dashboard FastAPI pour gérer des instances Hermes sur Kubernetes. Crée, liste et gère des agents avec interface WebUI, sans Helm ni manifests statiques.

## Architecture

- **Un Deployment par instance** nommé `agent-{hash8}-{subdomain}` (ex: `agent-9ae62006-vjourne-agent`).
- **3 containers par pod** : hermes-agent (port 8642), hermes-dashboard (port 9119), hermes-webui (port 8787).
- **2 PVCs** : `{name}-data` (config, sessions), `{name}-workspace` (fichiers de travail).
- **1 ConfigMap** : `{name}-config` (config.yaml générée depuis les variables d'environnement).
- **1 Ingress** par instance avec cert-manager TLS sur `*.ailab.infocepo.com`.

## Quick Start

### Lancer localement (dev)

```bash
cd agent-saas
pip install fastapi uvicorn pydantic argon2-cffi
python3 app.py
# Dashboard accessible sur http://localhost:8000
```

### Déployer sur Kubernetes (production)

Voir [DEPLOY_GUIDE.md](./DEPLOY_GUIDE.md) pour l'installation complète sur un cluster vierge.

```bash
# 1. Prérequis : ingress controller + cert-manager
# 2. Appliquer les manifests
kubectl apply -f manifests/01-rbac.yaml -n demo1
kubectl apply -f manifests/02-secrets.yaml -n demo1
kubectl create configmap agents-saas-app \
  --from-file=app.py=app.py \
  --dry-run=client -o yaml | kubectl apply -f - -n demo1
kubectl apply -f manifests/03-deployment.yaml -n demo1
kubectl apply -f manifests/04-service.yaml -n demo1
kubectl apply -f manifests/05-ingress.yaml -n demo1

# 3. Vérifier
kubectl -n demo1 rollout status deploy/agents-saas --timeout=60s
```

## API

### `POST /api/deploy`

Déploie une nouvelle instance agent.

```bash
curl -X POST http://localhost:8000/api/deploy \
  -H "Content-Type: application/json" \
  -d '{"subdomain": "my-agent"}'
```

Réponse :
```json
{
  "name": "agent-9ae62006-my-agent",
  "subdomain": "my-agent",
  "url": "https://my-agent.ailab.infocepo.com",
  "status": "deploying"
}
```

### `GET /api/instances`

Liste toutes les instances déployées.

```bash
curl http://localhost:8000/api/instances
```

### `GET /health`

Retourne le statut de l'application.

```bash
curl http://localhost:8000/health
# {"status":"ok"}
```

## Configuration

L'application se configure via des variables d'environnement :

| Variable | Défaut | Description |
|----------|--------|-------------|
| `LLM_BASE_URL` | `https://api-nothink.ailab.infocepo.com/v1` | Endpoint API LLM |
| `LLM_API_KEY` | (secret) | Clé API LLM |
| `LLM_PROVIDER` | `infocepo-alias` | Nom du provider |
| `LLM_MODEL` | `ai-thinking` | Modèle par défaut |
| `API_SERVER_KEY` | (secret) | Clé d'auth API Server |
| `DEPLOY_TOKEN` | (secret) | Token de déploiement |

## Structure

```
agent-saas/
├── app.py              # FastAPI dashboard (deploy par ConfigMap)
├── deploy-agent.sh     # Script pour déployer une instance agent individuelle
├── deploy-agent-example.sh  # Exemple de déploiement avec variables
├── .env.example        # Variables d'environnement
├── manifests/          # Manifests K8s complets pour installer le SaaS
│   ├── 01-rbac.yaml    # ServiceAccount, Role, RoleBinding
│   ├── 02-secrets.yaml # Secrets (LLM_API_KEY, HERMES_WEBUI_PASSWORD)
│   ├── 03-deployment.yaml  # Deployment agents-saas
│   ├── 04-service.yaml     # Service ClusterIP
│   ├── 05-ingress.yaml     # Ingress avec TLS cert-manager
│   └── 06-clusterissuer.yaml # ClusterIssuer Let's Encrypt
├── charts/             # Helm chart pour les instances (pas le SaaS)
│   └── hermes-webui/
├── tests/              # Tests pytest
└── DEPLOY_GUIDE.md     # Guide complet d'installation
```

=======
# Agents SaaS — Dashboard de Déploiement Multi-Instances

## Vue d'ensemble

Dashboard SaaS déployé sur Kubernetes (cluster `demo1`, namespace `demo1`) qui permet de déployer dynamiquement des instances **Hermes WebUI** individuelles, accessibles via sous-domaines :

- **Dashboard** : `https://agents-saas.ailab.infocepo.com`
- **Instances** : `https://agent-<hash>-<subdomain>.<subdomain>.ailab.infocepo.com`

Chaque instance est un agent Hermes complet avec sa configuration, son espace de travail et son stockage persistant.

## Architecture

```
┌─────────────────────────────────────────────────────┐
│  agents-saas.ailab.infocepo.com (Dashboard)         │
│  ┌───────────────────────────────────────────────┐  │
│  │  agents-saas (Deployment)                     │  │
│  │  ┌───────────────────────────────────────┐   │  │
│  │  │  agents-saas-sa  (ServiceAccount)      │   │  │
│  │  │  agents-saas-role  (RBAC)             │   │  │
│  │  │  K8s REST API pour créer des instances│   │  │
│  │  └───────────────────────────────────────┘   │  │
│  └───────────────────────────────────────────────┘  │
│         │                                             │
│         │  API: POST /api/deploy {subdomain: "foo"}  │
│         ▼                                             │
│  ┌───────────────────────────────────────────────┐  │
│  │  Pour chaque instance, crée :                │  │
│  │  • PVC {name}-data (20Gi microk8s-hostpath)  │  │
│  │  • PVC {name}-workspace (20Gi)               │  │
│  │  • ConfigMap {name}-config (config.yaml)     │  │
│  │  • Deployment {name}                         │  │
│  │  • Service {name}-svc                        │  │
│  │  • Ingress TLS {name}.{subdomain}.ailab...  │  │
│  └───────────────────────────────────────────────┘  │
└─────────────────────────────────────────────────────┘

┌─────────────────────────────────────────────────────┐
│  instance: agent-<hash>-<subdomain>                 │
│  ┌───────────────────────────────────────────────┐  │
│  │  init-webui-data     → copie data image→PVC   │  │
│  │  copy-agent-src      → copie hermes-agent→PVC │  │
│  │  copy-hermes-config  → copie config→PVC       │  │
│  │  ──────────────────────────────────────────    │  │
│  │  webui (hermes-webui:latest) :8080             │  │
│  │    /home/hermeswebui/.hermes ← PVC            │  │
│  │    /.hermes            ← symlink → PVC        │  │
│  │    /workspace          ← PVC                  │  │
│  │    /etc/hermes-config/config.yaml             │  │
│  └───────────────────────────────────────────────┘  │
└─────────────────────────────────────────────────────┘
```

## Composants

### Dashboard (agents-saas)

- **Image** : `python:3.12-slim` (via ConfigMap)
- **Framework** : FastAPI
- **Port** : 8000
- **Fonction** : Dashboard HTML + API REST pour créer des instances

Fichiers :
- `app.py` : Frontend HTML (dashboard) + Backend FastAPI (CRUD K8s)
- `requirements.txt` : `fastapi`, `uvicorn`, `pydantic`
- `deploy.yaml` : Manifestes K8s statiques (sa pour RBAC)

### Instance (agents dynamiques)

- **Image** : `ghcr.io/nesquena/hermes-webui:latest`
- **InitContainer** : `nousresearch/hermes-agent:latest` (copie source → PVC)
- **Port** : 8080
- **Configuration** : LLM `ai-thinking` via `api-nothink.ailab.infocepo.com`

## Déploiement

### Déploiement du dashboard

```bash
# ServiceAccount + RBAC (permet au dashboard de créer des instances)
kubectl --kubeconfig ~/.kube/demo1.ailab.infocepo.com.config -n demo1 apply -f deploy.yaml

# Recréer le ConfigMap avec le code source
kubectl --kubeconfig ~/.kube/demo1.ailab.infocepo.com.config -n demo1 \
  delete configmap agents-saas-app 2>/dev/null || true
kubectl --kubeconfig ~/.kube/demo1.ailab.infocepo.com.config -n demo1 \
  create configmap agents-saas-app --from-file=/home/openclaw/work/agents-saas/app.py

# Redémarrer le dashboard
kubectl --kubeconfig ~/.kube/demo1.ailab.infocepo.com.config -n demo1 \
  rollout restart deployment/agents-saas
```

### Déploiement d'une instance via le dashboard

1. Accéder à `https://agents-saas.ailab.infocepo.com`
2. Entrer un nom de sous-domaine (ex: `mon-projet`)
3. Le dashboard crée automatiquement :
   - PVCs persistants (20Gi chacun)
   - ConfigMap avec `config.yaml`
   - Deployment avec les 3 initContainers nécessaires
   - Service ClusterIP
   - Ingress TLS (cert-manager, letsencrypt-prod)

### Déploiement manuel d'une instance

Créer un ConfigMap + Deployment avec les 3 initContainers (voir `app.py` pour le modèle exact).

## Résolution de problèmes

### `AIAgent not available`

**Cause** : Le source `hermes-agent` n'est pas monté dans le conteneur.

**Solution** : Vérifier que les 3 initContainers sont présents et ont réussi :

```bash
kubectl --kubeconfig ~/.kube/demo1.ailab.infocepo.com.config -n demo1 \
  get deployment <deployment-name> -o jsonpath='{.spec.template.spec.initContainers[*].name}'
```

Les initContainers requis :
1. `init-webui-data` (image : `ghcr.io/nesquena/hermes-webui:latest`)
2. `copy-agent-src` (image : `nousresearch/hermes-agent:latest`)
3. `copy-hermes-config` (image : `alpine:3.19`)

**Patch automatique d'une instance existante** :
```bash
# Voir app.py lignes ~148-299 pour le deployment_body complet
# Puis :
kubectl --kubeconfig ~/.kube/demo1.ailab.infocepo.com.config -n demo1 \
  patch deployment <name> --type=strategic -p '<deployment_body_json>'
```

### `state.db has no 'source' column`

**Warning non-critique** : Schéma de base de données ancien. L'agent fonctionne mais les sessions ne sont pas listables.

## Variables d'environnement

| Variable | Valeur | Rôle |
|----------|--------|------|
| `LLM_BASE_URL` | `https://api-nothink.ailab.infocepo.com/v1` | Endpoint LLM |
| `LLM_API_KEY` | `AntonioPacheco$999` | Clé API |
| `LLM_PROVIDER` | `infocepo-alias` | Provider |
| `LLM_MODEL` | `ai-thinking` | Modèle |

## Ressources

- **Repository** : `https://github.com/ynotopec/agent-saas`
- **Dashboard** : `https://agents-saas.ailab.infocepo.com`
- **Images** :
  - `ghcr.io/nesquena/hermes-webui:latest` (WebUI)
  - `nousresearch/hermes-agent:latest` (source agent)
  - `python:3.12-slim` (dashboard)
  - `alpine:3.19` (copy-config initContainer)

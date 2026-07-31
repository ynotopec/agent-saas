---
name: agents-saas-full-deploy
description: Déployer le SaaS agents-saas sur un cluster K8s vierge — manifest complet (RBAC, deployment, service, ingress).
---

# Installation complète du SaaS agents-saas

## Prérequis non-standards

| Prerequis | Détail |
|-----------|--------|
| **Ingress controller nginx** | class=`public`, cert-manager intégré |
| **cert-manager** | ClusterIssuer `letsencrypt-prod` (Let's Encrypt) |
| **Storage** | `microk8s-hostpath` ou `nfs-client` (PVC 20Gi par instance) |
| **DNS wildcard** | Cloudflare SNI-routing `*.ailab.infocepo.com` → ingress IP |
| **RBAC namespace-scoped** | Le SaaS opère UNIQUEMENT dans le namespace `demo1` |
| **ServiceAccount dedicated** | `agents-saas-sa` avec Role permissions |

## Manifests complets

### 1. RBAC

```yaml
apiVersion: v1
kind: ServiceAccount
metadata:
  name: agents-saas-sa
  namespace: demo1
  labels:
    app: agents-saas
---
apiVersion: rbac.authorization.k8s.io/v1
kind: Role
metadata:
  name: agents-saas-role
  namespace: demo1
  labels:
    app: agents-saas
rules:
- apiGroups: [""]
  resources: ["pods", "persistentvolumeclaims", "services", "configmaps", "secrets"]
  verbs: ["get", "list", "watch", "create", "update", "patch", "delete"]
- apiGroups: ["apps"]
  resources: ["deployments"]
  verbs: ["get", "list", "watch", "create", "update", "patch", "delete"]
- apiGroups: ["networking.k8s.io"]
  resources: ["ingresses"]
  verbs: ["get", "list", "watch", "create", "update", "patch", "delete"]
- apiGroups: ["cert-manager.io"]
  resources: ["certificates"]
  verbs: ["get", "list", "watch", "create", "update", "patch", "delete"]
---
apiVersion: rbac.authorization.k8s.io/v1
kind: RoleBinding
metadata:
  name: agents-saas-binding
  namespace: demo1
  labels:
    app: agents-saas
subjects:
- kind: ServiceAccount
  name: agents-saas-sa
  namespace: demo1
roleRef:
  kind: Role
  name: agents-saas-role
  apiGroup: rbac.authorization.k8s.io
```

### 2. Secrets

```yaml
apiVersion: v1
kind: Secret
metadata:
  name: agents-saas-secrets
  namespace: demo1
  labels:
    app: agents-saas
type: Opaque
stringData:
  LLM_API_KEY: "AntonioPacheco$999"
  HERMES_WEBUI_PASSWORD: "CHANGE_ME"
```

### 3. Deployment

```yaml
apiVersion: apps/v1
kind: Deployment
metadata:
  name: agents-saas
  namespace: demo1
  labels:
    app: agents-saas
spec:
  replicas: 1
  selector:
    matchLabels:
      app: agents-saas
  template:
    metadata:
      labels:
        app: agents-saas
    spec:
      serviceAccountName: agents-saas-sa
      containers:
      - name: agents-saas
        image: python:3.12-slim
        command: ["python3", "/app/app.py"]
        ports:
        - containerPort: 8000
        env:
        - name: LLM_BASE_URL
          value: "https://api-nothink.ailab.infocepo.com/v1"
        - name: LLM_API_KEY
          valueFrom:
            secretKeyRef:
              name: agents-saas-secrets
              key: LLM_API_KEY
        - name: LLM_PROVIDER
          value: "infocepo-alias"
        - name: LLM_MODEL
          value: "ai-thinking"
        - name: API_SERVER_KEY
          value: "ce1dfb04ec3c143320c9ed3d348e32d85d5144898547875d86ad382ae184b88e"
        - name: DEPLOY_TOKEN
          value: "test-token-123"
        resources:
          limits:
            cpu: "500m"
            memory: "1Gi"
        readinessProbe:
          httpGet:
            path: /health
            port: 8000
          initialDelaySeconds: 3
          periodSeconds: 10
        livenessProbe:
          httpGet:
            path: /health
            port: 8000
          initialDelaySeconds: 5
          periodSeconds: 30
        volumeMounts:
        - name: app-files
          mountPath: /app
        - name: pip-packages
          mountPath: /usr/local/lib/python3.12/site-packages
      initContainers:
      - name: install-pip-packages
        image: python:3.12-slim
        command: ["pip3", "install", "fastapi", "uvicorn", "pydantic", "argon2-cffi", "--target", "/packages"]
        volumeMounts:
        - name: pip-packages
          mountPath: /packages
      volumes:
      - name: app-files
        configMap:
          name: agents-saas-app
      - name: pip-packages
        emptyDir: {}
```

### 4. Service

```yaml
apiVersion: v1
kind: Service
metadata:
  name: agents-saas-svc
  namespace: demo1
  labels:
    app: agents-saas
spec:
  selector:
    app: agents-saas
  ports:
  - port: 80
    targetPort: 8000
    protocol: TCP
  type: ClusterIP
```

### 5. Ingress

```yaml
apiVersion: networking.k8s.io/v1
kind: Ingress
metadata:
  name: agents-saas-ingress
  namespace: demo1
  labels:
    app: agents-saas
  annotations:
    cert-manager.io/cluster-issuer: letsencrypt-prod
    nginx.ingress.kubernetes.io/ssl-redirect: "true"
    nginx.ingress.kubernetes.io/proxy-cache-bypass: no-cache
    nginx.ingress.kubernetes.io/no-cache: "true"
    nginx.ingress.kubernetes.io/proxy-no-cache: "true"
spec:
  ingressClassName: public
  tls:
  - hosts:
    - agents-saas.ailab.infocepo.com
    secretName: agents-saas-tls
  rules:
  - host: agents-saas.ailab.infocepo.com
    http:
      paths:
      - path: /
        pathType: Prefix
        backend:
          service:
            name: agents-saas-svc
            port:
              number: 80
```

### 6. ClusterIssuer

```yaml
apiVersion: cert-manager.io/v1
kind: ClusterIssuer
metadata:
  name: letsencrypt-prod
spec:
  acme:
    server: https://acme-v02.api.letsencrypt.org/directory
    email: admin@ailab.infocepo.com
    privateKeySecretRef:
      name: letsencrypt-prod-key
    solvers:
    - http01:
        ingress:
          ingressClassName: public
```

## Déploiement complet

```bash
# 0. Ingress controller (microk8s)
microk8s enable ingress

# 1. RBAC
kubectl apply -f 01-rbac.yaml

# 2. Secrets
kubectl apply -f 02-secrets.yaml

# 3. ConfigMap (app.py)
kubectl create configmap agents-saas-app \
  --from-file=app.py=/home/ai-agent/work/agent-saas/app.py \
  --dry-run=client -o yaml | kubectl apply -f - -n demo1

# 4. Deployment
kubectl apply -f 03-deployment.yaml

# 5. Service
kubectl apply -f 04-service.yaml

# 6. Ingress
kubectl apply -f 05-ingress.yaml

# 7. Vérifier
kubectl -n demo1 rollout status deploy/agents-saas --timeout=60s
```

## Variables à configurer

| Variable | Valeur par défaut | Où configurer |
|----------|------------------|---------------|
| `LLM_BASE_URL` | `https://api-nothink.ailab.infocepo.com/v1` | Deployment env |
| `LLM_API_KEY` | (secret) | `agents-saas-secrets` |
| `LLM_PROVIDER` | `infocepo-alias` | Deployment env |
| `LLM_MODEL` | `ai-thinking` | Deployment env |
| `API_SERVER_KEY` | `ce1dfb...` | Deployment env |
| `DEPLOY_TOKEN` | `test-token-123` | Deployment env |
| `HERMES_WEBUI_PASSWORD` | `CHANGE_ME` | `agents-saas-secrets` |

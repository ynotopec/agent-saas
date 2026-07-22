#!/usr/bin/env python3
"""
Generate K8s manifests for agents-saas dashboard.
Creates: SA, Role, RoleBinding, Deployment, ConfigMap, Service, Ingress.
Uses local kubectl to create resources in K8s.
"""
import subprocess
import os

NAMESPACE = "demo1"
KUBECONFIG = "/home/openclaw/.kube/demo1.ailab.infocepo.com.config"
DOMAIN_SUFFIX = ".ailab.infocepo.com"

def ensure_kubeconfig():
    """Create a temporary kubeconfig with SA token for the agents-saas pod."""
    # Get the cluster server URL from kubeconfig
    result = subprocess.run(
        ["kubectl", "--kubeconfig", KUBECONFIG, "config", "view", "-o", "jsonpath={.clusters[0].cluster.server}"],
        capture_output=True, text=True
    )
    cluster_url = result.stdout.strip()
    print(f"Cluster URL: {cluster_url}")
    return cluster_url

def generate_manifest():
    cluster_url = ensure_kubeconfig()
    
    # Read app.py content from file
    app_py_path = "/home/openclaw/work/agents-saas/app.py"
    with open(app_py_path, "r") as f:
        app_py = f.read()
    
    # Read deploy-agent.sh content from file
    deploy_sh_path = "/home/openclaw/work/agents-saas/deploy-agent.sh"
    with open(deploy_sh_path, "r") as f:
        deploy_sh = f.read()
    
    manifest = f"""---
# ServiceAccount for agents-saas pod to manage K8s resources
apiVersion: v1
kind: ServiceAccount
metadata:
  name: agents-saas-sa
  namespace: {NAMESPACE}
---
# Role to create/delete instances
apiVersion: rbac.authorization.k8s.io/v1
kind: Role
metadata:
  name: agents-saas-role
  namespace: {NAMESPACE}
rules:
  - apiGroups: ["apps"]
    resources: ["deployments"]
    verbs: ["get", "list", "watch", "create", "update", "patch", "delete"]
  - apiGroups: [""]
    resources: ["services", "configmaps", "persistentvolumeclaims"]
    verbs: ["get", "list", "watch", "create", "update", "patch", "delete"]
  - apiGroups: ["networking.k8s.io"]
    resources: ["ingresses"]
    verbs: ["get", "list", "watch", "create", "update", "patch", "delete"]
  - apiGroups: ["cert-manager.io"]
    resources: ["certificates"]
    verbs: ["get", "list", "watch", "create", "update", "patch", "delete"]
  - apiGroups: [""]
    resources: ["secrets"]
    verbs: ["get", "list", "watch", "create", "update", "patch", "delete"]
---
# Bind SA to role
apiVersion: rbac.authorization.k8s.io/v1
kind: RoleBinding
metadata:
  name: agents-saas-binding
  namespace: {NAMESPACE}
subjects:
  - kind: ServiceAccount
    name: agents-saas-sa
    namespace: {NAMESPACE}
roleRef:
  kind: Role
  name: agents-saas-role
  apiGroup: rbac.authorization.k8s.io
---
# Deployment
apiVersion: apps/v1
kind: Deployment
metadata:
  name: agents-saas
  namespace: {NAMESPACE}
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
        image: ghcr.io/nesquena/hermes-webui:latest
        command:
        - sh
        - -c
        - |
          pip install fastapi uvicorn pydantic >/dev/null 2>&1
          exec python3 /app/app.py
        ports:
        - containerPort: 8000
          name: http
          protocol: TCP
        livenessProbe:
          httpGet:
            path: /health
            port: 8000
          initialDelaySeconds: 10
          periodSeconds: 30
        readinessProbe:
          httpGet:
            path: /health
            port: 8000
          initialDelaySeconds: 5
          periodSeconds: 10
        resources:
          limits:
            cpu: "500m"
            memory: 1Gi
          requests:
            cpu: "100m"
            memory: 128Mi
        volumeMounts:
        - name: app-files
          mountPath: /app
        env:
        - name: KUBERNETES_CLUSTER_URL
          value: "{cluster_url}"
        - name: SA_TOKEN
          valueFrom:
            secretKeyRef:
              name: agents-saas-sa-token
              key: token
      volumes:
      - name: app-files
        configMap:
          name: agents-saas-config
---
# ConfigMap with app code and deploy script
apiVersion: v1
kind: ConfigMap
metadata:
  name: agents-saas-config
  namespace: {NAMESPACE}
data:
  app.py: |
{app_py.encode().decode('unicode_escape')}
  deploy-agent.sh: |
{deploy_sh.encode().decode('unicode_escape')}
---
# Service
apiVersion: v1
kind: Service
metadata:
  name: agents-saas-svc
  namespace: {NAMESPACE}
  labels:
    app: agents-saas
spec:
  type: ClusterIP
  ports:
  - port: 80
    targetPort: 8000
    protocol: TCP
    name: http
  selector:
    app: agents-saas
---
# Ingress
apiVersion: networking.k8s.io/v1
kind: Ingress
metadata:
  name: agents-saas-ingress
  namespace: {NAMESPACE}
  annotations:
    cert-manager.io/cluster-issuer: letsencrypt-prod
    nginx.ingress.kubernetes.io/ssl-redirect: "true"
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
"""
    
    return manifest

if __name__ == "__main__":
    manifest = generate_manifest()
    with open("/home/openclaw/work/agents-saas/generated.yaml", "w") as f:
        f.write(manifest)
    print("Generated manifests written to /home/openclaw/work/agents-saas/generated.yaml")

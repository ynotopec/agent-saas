#!/usr/bin/env python3
"""
Simple FastAPI server for agents-saas dashboard.
Uses K8s REST API directly (no kubectl needed).
"""
import os
import hashlib
import json
from datetime import datetime
from fastapi import FastAPI
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel

class DeployRequest(BaseModel):
    subdomain: str

app = FastAPI(title="Agent SaaS Dashboard")
NAMESPACE = "demo1"

# K8s ServiceAccount token and CA
SA_TOKEN_PATH = "/var/run/secrets/kubernetes.io/serviceaccount/token"
CA_PATH = "/var/run/secrets/kubernetes.io/serviceaccount/ca.crt"

def get_token():
    try:
        with open(SA_TOKEN_PATH) as f:
            return f.read().strip()
    except:
        return None

def _api_base(resource_type):
    """Build correct K8s API base URL based on resource type."""
    api_map = {
        "deployments": "apps/v1",
        "ingresses": "networking.k8s.io/v1",
        "certificates": "cert-manager.io/v1",
    }
    version = api_map.get(resource_type, "v1")
    if version == "v1":
        return f"https://kubernetes.default.svc/api/v1"
    parts = version.split("/")
    group = parts[0]
    ver = parts[1]
    return f"https://kubernetes.default.svc/apis/{group}/{ver}"

def k8s_get(resource, label_selector=None):
    import urllib.request
    try:
        token = get_token()
        if not token:
            return []
        base = _api_base(resource)
        url = f"{base}/namespaces/{NAMESPACE}/{resource}"
        if label_selector:
            url += f"?labelSelector={label_selector}"
        req = urllib.request.Request(url)
        req.add_header("Authorization", f"Bearer {token}")
        req.add_header("Accept", "application/json")
        with urllib.request.urlopen(req, cafile=CA_PATH) as resp:
            return json.loads(resp.read())
    except Exception:
        return {"items": []}

def k8s_post(resource_type, body):
    import urllib.request
    try:
        token = get_token()
        if not token:
            return False
        data = json.dumps(body).encode()
        base = _api_base(resource_type)
        url = f"{base}/namespaces/{NAMESPACE}/{resource_type}"
        req = urllib.request.Request(
            url,
            data=data,
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
            method="POST"
        )
        with urllib.request.urlopen(req, cafile=CA_PATH) as resp:
            resp.read()
        return True
    except Exception as e:
        return False

def list_instances():
    res = k8s_get("pods", label_selector="app=agent-instance")
    instances = []
    for pod in res.get("items", []):
        labels = pod.get("metadata", {}).get("labels", {})
        if labels.get("app") == "agent-instance":
            name = pod["metadata"]["name"]
            name_parts = name.replace("agent-", "").split("-", 1)
            instances.append({
                "name": name,
                "subdomain": name_parts[1] if len(name_parts) > 1 else name,
                "url": f"https://{name}.agents-saas.ailab.infocepo.com",
                "status": "Running"
            })
    return instances

def deploy_instance(subdomain: str) -> dict:
    hash8 = hashlib.sha256(datetime.now().isoformat().encode()).hexdigest()[:8]
    name = f"agent-{hash8}-{subdomain}"
    domain = f"{name}.{subdomain}.ailab.infocepo.com"

    try:
        # Create PVCs
        for pvc in [f"{name}-data", f"{name}-workspace"]:
            k8s_post("persistentvolumeclaims", {
                "apiVersion": "v1", "kind": "PersistentVolumeClaim",
                "metadata": {"name": pvc, "namespace": NAMESPACE, "labels": {"app": "agent-instance"}},
                "spec": {"accessModes": ["ReadWriteOnce"], "storageClassName": "microk8s-hostpath",
                         "resources": {"requests": {"storage": "20Gi"}}}
            })

        # Create ConfigMap
        k8s_post("configmaps", {
            "apiVersion": "v1", "kind": "ConfigMap",
            "metadata": {"name": f"{name}-config", "namespace": NAMESPACE, "labels": {"app": "agent-instance"}},
            "data": {"config.yaml": "model:\n  default: ai-thinking\n  provider: custom\n  context_length: 262144\n  base_url: http://10.10.0.2:8571/v1\n  api_key: AntonioPacheco$999\nproviders:\n  ai-nothink:\n    name: ai-nothink\n    type: openai\n    api_url: https://api-nothink.ailab.infocepo.com/v1\n    api_key: AntonioPacheco$999\nfallback_providers: []\ntoolsets:\n  - hermes-cli"}
        })

        # Create Deployment
        k8s_post("deployments", {
            "apiVersion": "apps/v1", "kind": "Deployment",
            "metadata": {"name": name, "namespace": NAMESPACE, "labels": {"app": "agent-instance", "agent-instance": subdomain, "agent-hash": hash8}},
            "spec": {
                "replicas": 1,
                "selector": {"matchLabels": {"app": "agent-instance", "agent-instance": subdomain}},
                "template": {
                    "metadata": {"labels": {"app": "agent-instance", "agent-instance": subdomain, "agent-hash": hash8}},
                    "spec": {
                        "containers": [{
                            "name": "webui", "image": "ghcr.io/nesquena/hermes-webui:latest",
                            "imagePullPolicy": "Always", "ports": [{"containerPort": 8080}],
                            "env": [
                                {"name": "HERMES_WEBUI_STATE_DIR", "value": "/home/hermeswebui/.hermes/webui"},
                                {"name": "HERMES_WEBUI_PORT", "value": "8080"},
                                {"name": "HERMES_WEBUI_HOST", "value": "0.0.0.0"},
                                {"name": "HERMES_WEBUI_SKIP_ONBOARDING", "value": "1"},
                                {"name": "HERMES_CONFIG", "value": "/etc/hermes-config/config.yaml"}
                            ],
                            "volumeMounts": [
                                {"name": "agent-data", "mountPath": "/home/hermeswebui/.hermes"},
                                {"name": "hermes-config", "mountPath": "/etc/hermes-config", "readOnly": True}
                            ]
                        }],
                        "volumes": [
                            {"name": "hermes-config", "configMap": {"name": f"{name}-config"}},
                            {"name": "agent-data", "persistentVolumeClaim": {"claimName": f"{name}-data"}}
                        ]
                    }
                }
            }
        })

        # Create Service
        k8s_post("services", {
            "apiVersion": "v1", "kind": "Service",
            "metadata": {"name": f"{name}-svc", "namespace": NAMESPACE, "labels": {"app": "agent-instance"}},
            "spec": {"ports": [{"port": 80, "targetPort": 8080}],
                     "selector": {"app": "agent-instance", "agent-instance": subdomain}}
        })

        # Create Ingress
        k8s_post("ingresses", {
            "apiVersion": "networking.k8s.io/v1", "kind": "Ingress",
            "metadata": {"name": f"{name}-ingress", "namespace": NAMESPACE,
                         "annotations": {"cert-manager.io/cluster-issuer": "letsencrypt-prod",
                                         "nginx.ingress.kubernetes.io/ssl-redirect": "true"}},
            "spec": {
                "ingressClassName": "public",
                "tls": [{"hosts": [domain], "secretName": f"{name}-tls"}],
                "rules": [{"host": domain, "http": {"paths": [{"path": "/", "pathType": "Prefix",
                        "backend": {"service": {"name": f"{name}-svc", "port": {"number": 80}}}}]}}]
            }
        })

        return {"success": True, "name": name, "subdomain": subdomain, "hash": hash8, "url": f"https://{domain}"}
    except Exception as e:
        return {"error": str(e)}

@app.get("/", response_class=HTMLResponse)
def index():
    return HTMLResponse(CONTENT)

@app.get("/api/instances")
def api_instances():
    return JSONResponse(list_instances())

@app.post("/api/deploy")
def api_deploy(req: DeployRequest):
    return JSONResponse(deploy_instance(req.subdomain))

@app.get("/health")
def health():
    return {"status": "ok"}

CONTENT = '''<!DOCTYPE html>
<html lang="fr">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Agents SaaS Dashboard</title>
    <style>
        * { margin: 0; padding: 0; box-sizing: border-box; }
        body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif; background: #f5f5f5; color: #333; }
        .container { max-width: 800px; margin: 0 auto; padding: 2rem; }
        h1 { font-size: 2rem; margin-bottom: 1rem; color: #1a73e8; }
        .card { background: white; border-radius: 8px; box-shadow: 0 2px 4px rgba(0,0,0,0.1); padding: 2rem; margin-bottom: 1rem; }
        .form-group { margin-bottom: 1rem; }
        label { display: block; margin-bottom: 0.5rem; font-weight: 500; }
        input[type="text"] { width: 100%; padding: 0.75rem; border: 1px solid #ddd; border-radius: 4px; font-size: 1rem; }
        input:focus { outline: none; border-color: #1a73e8; box-shadow: 0 0 0 2px rgba(26,115,232,0.2); }
        button { background: #1a73e8; color: white; padding: 0.75rem 1.5rem; border: none; border-radius: 4px; font-size: 1rem; cursor: pointer; }
        button:hover { background: #1557b0; }
        button:disabled { background: #ccc; cursor: not-allowed; }
        .result { margin-top: 1rem; padding: 1rem; border-radius: 4px; }
        .success { background: #d4edda; color: #155724; border: 1px solid #c3e6cb; }
        .error { background: #f8d7da; color: #721c24; border: 1px solid #f5c6cb; }
        .instances { margin-top: 2rem; }
        .instances h2 { margin-bottom: 1rem; }
        .instance-item { background: #f8f9fa; padding: 1rem; border-radius: 4px; margin-bottom: 0.5rem; display: flex; justify-content: space-between; align-items: center; }
        .instance-item a { color: #1a73e8; text-decoration: none; }
        .instance-item a:hover { text-decoration: underline; }
    </style>
</head>
<body>
    <div class="container">
        <h1>🚀 Agents SaaS Dashboard</h1>
        <div class="card">
            <h2>Déployer une nouvelle instance</h2>
            <form id="deploy-form">
                <div class="form-group">
                    <label for="subdomain">Sous-domaine :</label>
                    <input type="text" id="subdomain" name="subdomain" placeholder="ex: mon-projet" required>
                </div>
                <button type="submit" id="deploy-btn">Déployer</button>
            </form>
            <div id="result"></div>
        </div>
        <div class="instances">
            <h2>Instances existantes</h2>
            <div id="instances-list">Chargement...</div>
        </div>
    </div>
    <script>
        async function deploy(e) {
            e.preventDefault();
            const subdomain = document.getElementById('subdomain').value;
            const btn = document.getElementById('deploy-btn');
            const result = document.getElementById('result');
            btn.disabled = true;
            btn.textContent = 'Déploiement...';
            try {
                const resp = await fetch('/api/deploy', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ subdomain })
                });
                const data = await resp.json();
                if (data.success) {
                    result.innerHTML = '<div class="result success">✅ Déployé !<br><a href="' + data.url + '" target="_blank">' + data.url + '</a></div>';
                    document.getElementById('subdomain').value = '';
                    loadInstances();
                } else {
                    result.innerHTML = '<div class="result error">❌ ' + (data.error || 'Erreur') + '</div>';
                }
            } catch (err) {
                result.innerHTML = '<div class="result error">❌ ' + err.message + '</div>';
            } finally {
                btn.disabled = false;
                btn.textContent = 'Déployer';
            }
        }
        async function loadInstances() {
            const list = document.getElementById('instances-list');
            try {
                const resp = await fetch('/api/instances');
                const instances = await resp.json();
                if (instances.length === 0) {
                    list.innerHTML = '<p>Aucune instance.</p>';
                    return;
                }
                list.innerHTML = instances.map(i =>
                    '<div class="instance-item"><strong>' + i.subdomain + '</strong> → <a href="' + i.url + '" target="_blank">' + i.url + '</a></div>'
                ).join('');
            } catch (err) {
                list.innerHTML = '<p>Erreur : ' + err.message + '</p>';
            }
        }
        document.getElementById('deploy-form').addEventListener('submit', deploy);
        loadInstances();
    </script>
</body>
</html>'''

if __name__ == '__main__':
    import uvicorn
    uvicorn.run(app, host='0.0.0.0', port=8000, workers=1)

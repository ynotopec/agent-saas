#!/usr/bin/env python3
"""Simple FastAPI server for agents-saas dashboard. Uses K8s REST API directly."""
import os
import hashlib
import json
import re
from datetime import datetime
from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel

class DeployRequest(BaseModel):
    subdomain: str

app = FastAPI(title="Agent SaaS Dashboard")
NAMESPACE = "demo1"
SA_TOKEN_PATH = "/var/run/secrets/kubernetes.io/serviceaccount/token"
CA_PATH = "/var/run/secrets/kubernetes.io/serviceaccount/ca.crt"

# Env vars for configurable LLM settings (defaults from local config)
LLM_BASE_URL = os.environ.get("LLM_BASE_URL", "https://api-nothink.ailab.infocepo.com/v1")
LLM_API_KEY = os.environ.get("LLM_API_KEY", "")
LLM_PROVIDER = os.environ.get("LLM_PROVIDER", "infocepo-alias")
LLM_MODEL = os.environ.get("LLM_MODEL", "ai-thinking")
SUBDOMAIN_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")

def validate_subdomain(subdomain: str) -> str:
    """Return a Kubernetes/DNS-safe subdomain or raise ValueError."""
    normalized = subdomain.strip()
    if not SUBDOMAIN_RE.fullmatch(normalized):
        raise ValueError(
            "Subdomain must be 1-63 characters and contain only lowercase "
            "letters, numbers, and single hyphens between characters."
        )
    if "--" in normalized:
        raise ValueError("Subdomain must not contain consecutive hyphens.")
    return normalized

def get_token():
    try:
        with open(SA_TOKEN_PATH) as f:
            return f.read().strip()
    except:
        return None

def _api_base(resource_type):
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
            raise RuntimeError("Kubernetes service account token is unavailable")
        data = json.dumps(body).encode()
        base = _api_base(resource_type)
        url = f"{base}/namespaces/{NAMESPACE}/{resource_type}"
        req = urllib.request.Request(url, data=data,
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
            method="POST")
        with urllib.request.urlopen(req, cafile=CA_PATH) as resp:
            resp.read()
        return True
    except Exception as e:
        raise RuntimeError(f"Failed to create {resource_type}: {e}") from e

def list_instances():
    res = k8s_get("pods", label_selector="app=agent-instance")
    instances = []
    for pod in res.get("items", []):
        labels = pod.get("metadata", {}).get("labels", {})
        if labels.get("app") == "agent-instance":
            name = pod["metadata"]["name"]
            name_parts = name.replace("agent-", "").split("-", 1)
            subdomain = labels.get("agent-instance") or (name_parts[1] if len(name_parts) > 1 else name)
            instances.append({
                "name": name,
                "subdomain": subdomain,
                "url": f"https://{name}.{subdomain}.ailab.infocepo.com",
                "status": pod.get("status", {}).get("phase", "Unknown")
            })
    return instances

def build_config() -> str:
    """Generate hermes config.yaml from env vars or defaults."""
    nothink_api_key = os.environ.get("NOTHINK_API_KEY", "")
    nothink_api_url = os.environ.get("NOTHINK_API_URL", "https://api-nothink.ailab.infocepo.com/v1")
    toolsets = os.environ.get("HERMES_TOOLSETS", "hermes-cli")
    return (
        f"model:\n"
        f"  default: {LLM_MODEL}\n"
        f"  provider: {LLM_PROVIDER}\n"
        f"  context_length: 262144\n"
        f"  base_url: {LLM_BASE_URL}\n"
        f"  api_key: {LLM_API_KEY}\n"
        f"providers:\n"
        f"  ai-nothink:\n"
        f"    name: ai-nothink\n"
        f"    type: openai\n"
        f"    api_url: {nothink_api_url}\n"
        f"    api_key: {nothink_api_key}\n"
        f"fallback_providers: []\n"
        f"custom_providers:\n"
        f"  - name: {LLM_PROVIDER}\n"
        f"    base_url: {LLM_BASE_URL}\n"
        f"    api_key: {LLM_API_KEY}\n"
        f"    model: {LLM_MODEL}\n"
        f"toolsets:\n"
        f"  - {toolsets}"
    )

def deploy_instance(subdomain: str) -> dict:
    subdomain = validate_subdomain(subdomain)
    hash8 = hashlib.sha256(datetime.now().isoformat().encode()).hexdigest()[:8]
    name = f"agent-{hash8}-{subdomain}"
    domain = f"{name}.{subdomain}.ailab.infocepo.com"

    try:
        config = build_config()

        for pvc in [f"{name}-data", f"{name}-workspace"]:
            k8s_post("persistentvolumeclaims", {
                "apiVersion": "v1", "kind": "PersistentVolumeClaim",
                "metadata": {"name": pvc, "namespace": NAMESPACE, "labels": {"app": "agent-instance"}},
                "spec": {"accessModes": ["ReadWriteOnce"], "storageClassName": "microk8s-hostpath",
                         "resources": {"requests": {"storage": "20Gi"}}}
            })

        k8s_post("configmaps", {
            "apiVersion": "v1", "kind": "ConfigMap",
            "metadata": {"name": f"{name}-config", "namespace": NAMESPACE, "labels": {"app": "agent-instance"}},
            "data": {"config.yaml": config}
        })

        # Build the deployment manifest as a dict (mirrors the working hermes-webui deployment)
        deployment_body = {
            "apiVersion": "apps/v1",
            "kind": "Deployment",
            "metadata": {
                "name": name,
                "namespace": NAMESPACE,
                "labels": {
                    "app": "agent-instance",
                    "agent-instance": subdomain,
                    "agent-hash": hash8
                }
            },
            "spec": {
                "replicas": 1,
                "selector": {
                    "matchLabels": {
                        "app": "agent-instance",
                        "agent-instance": subdomain
                    }
                },
                "template": {
                    "metadata": {
                        "labels": {
                            "app": "agent-instance",
                            "agent-instance": subdomain,
                            "agent-hash": hash8
                        }
                    },
                    "spec": {
                        "initContainers": [
                            # Init 1: copy initial data from image to PVC
                            {
                                "name": "init-webui-data",
                                "image": "ghcr.io/nesquena/hermes-webui:latest",
                                "command": ["sh", "-c"],
                                "args": [
                                    'echo "=== Initializing WebUI data ==="\n'
                                    'mkdir -p /data\n'
                                    'cp -a /home/hermeswebui/* /data/ 2>/dev/null || true\n'
                                    'cp -a /home/hermeswebui/.bashrc /home/hermeswebui/.profile /data/ 2>/dev/null || true\n'
                                    'chown -R 1024:1024 /data\n'
                                    'echo "=== WebUI data ready ==="\n'
                                ],
                                "securityContext": {"runAsUser": 0},
                                "resources": {"limits": {"cpu": "200m", "memory": "2Gi"}},
                                "volumeMounts": [
                                    {"mountPath": "/data", "name": "agent-data"}
                                ]
                            },
                            # Init 2: copy hermes-agent source from image to PVC
                            {
                                "name": "copy-agent-src",
                                "image": "nousresearch/hermes-agent:latest",
                                "command": ["sh", "-c"],
                                "args": [
                                    'echo "=== Copying hermes-agent source ==="\n'
                                    'mkdir -p /data/hermes-agent\n'
                                    'cd /opt/hermes\n'
                                    'if command -v rsync >/dev/null 2>&1; then\n'
                                    '  rsync -a . /data/hermes-agent/\n'
                                    'else\n'
                                    '  cp -a . /data/hermes-agent/\n'
                                    'fi\n'
                                    'chown -R 1024:1024 /data\n'
                                    'echo "=== Agent source copied ==="\n'
                                    'ls -la /data/hermes-agent/\n'
                                ],
                                "securityContext": {"runAsUser": 0},
                                "resources": {"limits": {"cpu": "500m", "memory": "1Gi"}},
                                "volumeMounts": [
                                    {"mountPath": "/data", "name": "agent-data"}
                                ]
                            },
                            # Init 3: copy config.yaml from ConfigMap to PVC
                            {
                                "name": "copy-hermes-config",
                                "image": "alpine:3.19",
                                "command": ["sh", "-c"],
                                "args": [
                                    'cat /etc/hermes-config/config.yaml > /data/config.yaml &&\n'
                                    'rm -rf /.hermes 2>/dev/null; ln -sf /home/hermeswebui/.hermes /.hermes &&\n'
                                    'echo "Config copied + symlink created"\n'
                                ],
                                "securityContext": {"runAsUser": 0},
                                "volumeMounts": [
                                    {"mountPath": "/etc/hermes-config", "name": "hermes-config", "readOnly": True},
                                    {"mountPath": "/data", "name": "agent-data"}
                                ]
                            }
                        ],
                        "containers": [{
                            "name": "webui",
                            "image": "ghcr.io/nesquena/hermes-webui:latest",
                            "imagePullPolicy": "Always",
                            "command": ["/bin/bash", "-c"],
                            "args": [
                                "set -e\necho \"=== Hermes WebUI starting ===\"\necho \"Agent source:\"\nls -la /home/hermeswebui/.hermes/hermes-agent/ 2>/dev/null | head -20 || echo \"NOT FOUND\"\necho \"Starting...\"\nexec /hermeswebui_init.bash\n"
                            ],
                            "ports": [{"containerPort": 8080, "name": "http", "protocol": "TCP"}],
                            "env": [
                                {"name": "HERMES_WEBUI_STATE_DIR", "value": "/home/hermeswebui/.hermes/webui"},
                                {"name": "HERMES_WEBUI_PORT", "value": "8080"},
                                {"name": "HERMES_WEBUI_HOST", "value": "0.0.0.0"},
                                {"name": "HERMES_WEBUI_WORKSPACE", "value": "/workspace"},
                                {"name": "HERMES_WEBUI_SKIP_ONBOARDING", "value": "1"},
                                {"name": "HERMES_WEBUI_PASSWORD"},
                                {"name": "HERMES_HOME", "value": "/home/hermeswebui/.hermes"},
                                {"name": "HERMES_CONFIG", "value": "/etc/hermes-config/config.yaml"},
                                {"name": "LLM_BASE_URL", "value": LLM_BASE_URL},
                                {"name": "LLM_API_KEY", "value": LLM_API_KEY},
                                {"name": "LLM_PROVIDER", "value": LLM_PROVIDER},
                                {"name": "LLM_MODEL", "value": LLM_MODEL},
                            ],
                            "livenessProbe": {
                                "httpGet": {"path": "/", "port": 8080, "scheme": "HTTP"},
                                "initialDelaySeconds": 30,
                                "periodSeconds": 30,
                                "failureThreshold": 3,
                                "timeoutSeconds": 5
                            },
                            "readinessProbe": {
                                "httpGet": {"path": "/", "port": 8080, "scheme": "HTTP"},
                                "initialDelaySeconds": 10,
                                "periodSeconds": 10,
                                "failureThreshold": 3,
                                "timeoutSeconds": 5,
                                "successThreshold": 1
                            },
                            "resources": {
                                "limits": {"cpu": "500m", "memory": "2Gi"},
                                "requests": {"cpu": "100m", "memory": "128Mi"}
                            },
                            "securityContext": {
                                "runAsUser": 1024,
                                "runAsNonRoot": True,
                                "runAsGroup": 1024
                            },
                            "volumeMounts": [
                                {"mountPath": "/home/hermeswebui/.hermes", "name": "agent-data"},
                                {"mountPath": "/workspace", "name": "workspace-data"},
                                {"mountPath": "/.hermes", "name": "agent-data"},
                                {"mountPath": "/etc/hermes-config", "name": "hermes-config", "readOnly": True}
                            ]
                        }],
                        "volumes": [
                            {"name": "hermes-config", "configMap": {"name": f"{name}-config"}},
                            {"name": "agent-data", "persistentVolumeClaim": {"claimName": f"{name}-data"}},
                            {"name": "workspace-data", "persistentVolumeClaim": {"claimName": f"{name}-workspace"}}
                        ]
                    }
                }
            }
        }
        k8s_post("deployments", deployment_body)

        k8s_post("services", {
            "apiVersion": "v1", "kind": "Service",
            "metadata": {"name": f"{name}-svc", "namespace": NAMESPACE, "labels": {"app": "agent-instance"}},
            "spec": {"ports": [{"port": 80, "targetPort": 8080}],
                     "selector": {"app": "agent-instance", "agent-instance": subdomain}}
        })

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

        return {"success": True, "name": name, "subdomain": subdomain, "hash": hash8,
                "url": f"https://{domain}"}
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
    try:
        return JSONResponse(deploy_instance(req.subdomain))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e

@app.get("/health")
def health():
    return {"status": "ok"}

CONTENT = """<!DOCTYPE html>
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
</html>"""

if __name__ == '__main__':
    import uvicorn
    uvicorn.run(app, host='0.0.0.0', port=8000, workers=1)

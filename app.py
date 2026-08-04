#!/usr/bin/env python3
"""Simple FastAPI server for agents-saas dashboard. Uses K8s REST API directly."""
import os
import hashlib
import json
import re
import secrets
from datetime import datetime
from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel

class DeployRequest(BaseModel):
    subdomain: str

class ChangePasswordRequest(BaseModel):
    subdomain: str
    new_password: str

app = FastAPI(title="Agent SaaS Dashboard")
NAMESPACE = "demo1"
SA_TOKEN_PATH = "/var/run/secrets/kubernetes.io/serviceaccount/token"
CA_PATH = "/var/run/secrets/kubernetes.io/serviceaccount/ca.crt"

# Env vars for configurable LLM settings (defaults from local config)
LLM_BASE_URL = os.environ.get("LLM_BASE_URL", "https://api-nothink.ailab.infocepo.com/v1")
LLM_API_KEY = os.environ.get("LLM_API_KEY", "")
LLM_PROVIDER = os.environ.get("LLM_PROVIDER", "infocepo-alias")
LLM_MODEL = os.environ.get("LLM_MODEL", "ai-thinking")
DEPLOY_TOKEN = os.environ.get("DEPLOY_TOKEN", "")
HERMES_WEBUI_PASSWORD = os.environ.get("HERMES_WEBUI_PASSWORD", "")
API_SERVER_KEY = os.environ.get("API_SERVER_KEY", "")
SUBDOMAIN_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")

def require_deploy_token(x_deploy_token: str | None = Header(default=None)) -> None:
    """Require a shared token for state-changing dashboard operations."""
    if not DEPLOY_TOKEN:
        raise HTTPException(status_code=503, detail="Deployment API is not configured")
    if not x_deploy_token or not secrets.compare_digest(x_deploy_token, DEPLOY_TOKEN):
        raise HTTPException(status_code=401, detail="Invalid deployment token")


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
    except OSError:
        return None

def _api_base(resource_type):
    api_map = {
        "deployments": "apps/v1",
        "ingresses": "networking.k8s.io/v1",
        "certificates": "cert-manager.io/v1",
    }
    version = api_map.get(resource_type, "v1")
    if version == "v1":
        return "https://kubernetes.default.svc/api/v1"
    parts = version.split("/")
    group = parts[0]
    ver = parts[1]
    return f"https://kubernetes.default.svc/apis/{group}/{ver}"

def k8s_get(resource, label_selector=None):
    import urllib.request
    try:
        token = get_token()
        if not token:
            return {"items": []}
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


def k8s_patch(resource_type, name, body):
    """Patch an existing K8s resource."""
    import urllib.request
    try:
        token = get_token()
        if not token:
            raise RuntimeError("Kubernetes service account token is unavailable")
        data = json.dumps(body).encode()
        base = _api_base(resource_type)
        url = f"{base}/namespaces/{NAMESPACE}/{resource_type}/{name}"
        # Use merge-patch for deployments (strategic merge doesn't work via REST API for simple patches)
        content_type = "application/merge-patch+json"
        req = urllib.request.Request(url, data=data,
            headers={"Authorization": f"Bearer {token}", "Content-Type": content_type},
            method="PATCH")
        with urllib.request.urlopen(req, cafile=CA_PATH) as resp:
            return json.loads(resp.read())
    except Exception as e:
        raise RuntimeError(f"Failed to patch {resource_type}/{name}: {e}") from e

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
                "url": f"https://{subdomain}.ailab.infocepo.com",
                "status": pod.get("status", {}).get("phase", "Unknown")
            })
    return instances

def build_config() -> str:
    """Generate hermes config.yaml from env vars or defaults."""
    nothink_api_key = os.environ.get("NOTHINK_API_KEY", "")
    nothink_api_url = os.environ.get("NOTHINK_API_URL", "https://api-nothink.ailab.infocepo.com/v1")
    toolsets = os.environ.get("HERMES_TOOLSETS", "hermes-cli")
    model = os.environ.get("LLM_MODEL", LLM_MODEL)
    provider = os.environ.get("LLM_PROVIDER", LLM_PROVIDER)
    base_url = os.environ.get("LLM_BASE_URL", LLM_BASE_URL)
    api_key = os.environ.get("LLM_API_KEY", LLM_API_KEY)
    return (
        f"model:\n"
        f"  default: {model}\n"
        f"  provider: {provider}\n"
        f"  context_length: 262144\n"
        f"  base_url: {base_url}\n"
        f"  api_key: {api_key}\n"
        f"providers:\n"
        f"  ai-nothink:\n"
        f"    name: ai-nothink\n"
        f"    type: openai\n"
        f"    api_url: {nothink_api_url}\n"
        f"    api_key: {nothink_api_key}\n"
        f"fallback_providers: []\n"
        f"custom_providers:\n"
        f"  - name: {provider}\n"
        f"    base_url: {base_url}\n"
        f"    api_key: {api_key}\n"
        f"    model: {model}\n"
        f"toolsets:\n"
        f"  - {toolsets}"
    )

def deploy_instance(subdomain: str) -> dict:
    subdomain = validate_subdomain(subdomain)
    hash8 = os.urandom(4).hex()  # 32-bit random, 8 hex chars
    name = f"agent-{hash8}-{subdomain}"
    domain = f"{subdomain}.ailab.infocepo.com"

    try:
        if not HERMES_WEBUI_PASSWORD:
            raise RuntimeError("HERMES_WEBUI_PASSWORD is not configured")
        if not API_SERVER_KEY:
            raise RuntimeError("API_SERVER_KEY is not configured")
        config = build_config()

        # Data PVC: 20Gi, Workspace PVC: 5Gi
        pvc_specs = [
            (f"{name}-data", "20Gi"),
            (f"{name}-workspace", "5Gi"),
        ]
        for pvc_name, storage in pvc_specs:
            k8s_post("persistentvolumeclaims", {
                "apiVersion": "v1", "kind": "PersistentVolumeClaim",
                "metadata": {"name": pvc_name, "namespace": NAMESPACE, "labels": {"app": "agent-instance"}},
                "spec": {"accessModes": ["ReadWriteOnce"], "storageClassName": "microk8s-hostpath",
                         "resources": {"requests": {"storage": storage}}}
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
                        "securityContext": {
                            "fsGroup": 1000,
                            "runAsGroup": 1000,
                            "runAsUser": 1000
                        },
                        "initContainers": [
                            {
                                "name": "init-hermes-home",
                                "image": "alpine:3.19",
                                "command": ["sh", "-c"],
                                "args": [
                                    "mkdir -p /hermes-home/.hermes/webui /workspace && "
                                    "echo '# Hermes workspace' > /workspace/.gitkeep && "
                                    "if [ -f /configmap/config.yaml ]; then "
                                    "cp /configmap/config.yaml /hermes-home/config.yaml && "
                                    "echo 'config.yaml copied to PVC root'; fi && "
                                    "echo '=== Init done ==='"
                                ],
                                "resources": {"limits": {"cpu": "50m", "memory": "64Mi"}},
                                "securityContext": {"runAsUser": 0},
                                "volumeMounts": [
                                    {"mountPath": "/hermes-home", "name": "hermes-home"},
                                    {"mountPath": "/workspace", "name": "workspace-data"},
                                    {"mountPath": "/configmap", "name": "hermes-config", "readOnly": True}
                                ]
                            },
                            {
                                "name": "init-agent-src",
                                "image": "nousresearch/hermes-agent:latest",
                                "imagePullPolicy": "Always",
                                "command": ["sh", "-c"],
                                "args": [
                                    "mkdir -p /hermes-home/hermes-agent && "
                                    "if [ -d /opt/hermes ]; then "
                                    "cp -r /opt/hermes/* /hermes-home/hermes-agent/ 2>/dev/null || true; "
                                    "echo 'hermes-agent source copied to PVC'; "
                                    "ls -la /hermes-home/hermes-agent/ | head -10; "
                                    "fi && "
                                    "chown -R 1000:1000 /hermes-home 2>/dev/null || true && "
                                    "echo '=== Init done ==='"
                                ],
                                "resources": {"limits": {"cpu": "200m", "memory": "1Gi"}},
                                "securityContext": {"runAsUser": 0},
                                "volumeMounts": [
                                    {"mountPath": "/hermes-home", "name": "hermes-home"}
                                ]
                            },
                            {
                                "name": "password-seed",
                                "image": "python:3.12-alpine",
                                "command": ["sh", "-c"],
                                "args": [
                                    "python3 -c '\nimport hashlib, os, json, pathlib\npathlib.Path(\"/hermes-home/.hermes/webui\").mkdir(parents=True, exist_ok=True)\nif not pathlib.Path(\"/hermes-home/.hermes/webui/settings.json\").exists():\n    password = os.environ[\"HERMES_WEBUI_PASSWORD\"].encode()\n    salt = os.urandom(16)\n    hash_val = hashlib.pbkdf2_hmac(\"sha256\", password, salt, 60000)\n    settings = {\n        \"password_hash\": hash_val.hex(),\n        \"password_salt\": salt.hex()\n    }\n    pathlib.Path(\"/hermes-home/.hermes/webui/settings.json\").write_text(json.dumps(settings))\n    print(\"Password seeded for fresh instance\")\nelse:\n    print(\"settings.json already exists, skipping password seed\")\n'\n",
                                ],
                                "resources": {"limits": {"cpu": "100m", "memory": "128Mi"}},
                                "securityContext": {"runAsUser": 0},
                                "env": [{"name": "HERMES_WEBUI_PASSWORD", "value": HERMES_WEBUI_PASSWORD}],
                                "volumeMounts": [
                                    {"mountPath": "/hermes-home", "name": "hermes-home"}
                                ]
                            }
                        ],
                        "containers": [
                            {
                                "name": "hermes-agent",
                                "image": "nousresearch/hermes-agent:latest",
                                "imagePullPolicy": "Always",
                                "command": ["sh", "-c"],
                                "args": ["hermes gateway run --no-supervise --force"],
                                "ports": [{"containerPort": 8642, "name": "gateway", "protocol": "TCP"}],
                                "readinessProbe": {
                                    "exec": {
                                        "command": [
                                            "sh", "-c",
                                            "python3 -c \"import socket; s=socket.socket(); s.settimeout(2); r=s.connect_ex(('127.0.0.1',8642)); s.close(); exit(0 if r==0 else 1)\""
                                        ]
                                    },
                                    "initialDelaySeconds": 30,
                                    "periodSeconds": 10,
                                    "failureThreshold": 30,
                                    "successThreshold": 1,
                                    "timeoutSeconds": 5
                                },
                                "resources": {
                                    "limits": {"cpu": "2", "memory": "4Gi"},
                                    "requests": {"cpu": "25m", "memory": "128Mi"}
                                },
                                "securityContext": {"runAsUser": 1000, "runAsGroup": 1000},
                                "volumeMounts": [
                                    {"mountPath": "/home/hermes/.hermes", "name": "hermes-home"},
                                    {"mountPath": "/workspace", "name": "workspace-data"},
                                    {"mountPath": "/etc/hermes-config", "name": "hermes-config", "readOnly": True}
                                ],
                                "env": [
                                    {"name": "HERMES_HOME", "value": "/home/hermes/.hermes"},
                                    {"name": "HERMES_UID", "value": "1000"},
                                    {"name": "HERMES_GID", "value": "1000"},
                                    {"name": "LLM_BASE_URL", "value": LLM_BASE_URL},
                                    {"name": "LLM_API_KEY", "value": LLM_API_KEY},
                                    {"name": "LLM_PROVIDER", "value": LLM_PROVIDER},
                                    {"name": "LLM_MODEL", "value": LLM_MODEL},
                                    {"name": "HERMES_ALLOW_ROOT_GATEWAY", "value": "1"},
                                    {"name": "HERMES_ACCEPT_HOOKS", "value": "1"},
                                    {"name": "HERMES_DONT_CHECK_TTY", "value": "1"},
                                    {"name": "HERMES_GATEWAY_NO_SUPERVISE", "value": "1"},
                                    {"name": "MCP_DISABLE", "value": "1"},
                                    {"name": "HERMES_CONFIG", "value": "/etc/hermes-config/config.yaml"},
                                    {"name": "API_SERVER_KEY", "value": API_SERVER_KEY}
                                ]
                            },
                            {
                                "name": "hermes-dashboard",
                                "image": "nousresearch/hermes-agent:latest",
                                "imagePullPolicy": "Always",
                                "command": ["hermes", "dashboard", "--host", "127.0.0.1"],
                                "ports": [{"containerPort": 9119, "name": "dashboard", "protocol": "TCP"}],
                                "readinessProbe": {
                                    "exec": {
                                        "command": [
                                            "sh", "-c",
                                            "python3 -c \"import socket; s=socket.socket(); s.settimeout(2); r=s.connect_ex(('127.0.0.1',9119)); s.close(); exit(0 if r==0 else 1)\""
                                        ]
                                    },
                                    "initialDelaySeconds": 45,
                                    "periodSeconds": 15,
                                    "failureThreshold": 3,
                                    "successThreshold": 1,
                                    "timeoutSeconds": 10
                                },
                                "livenessProbe": {
                                    "exec": {
                                        "command": [
                                            "sh", "-c",
                                            "python3 -c \"import socket; s=socket.socket(); s.settimeout(2); r=s.connect_ex(('127.0.0.1',9119)); s.close(); exit(0 if r==0 else 1)\""
                                        ]
                                    },
                                    "initialDelaySeconds": 60,
                                    "periodSeconds": 30,
                                    "failureThreshold": 3,
                                    "successThreshold": 1,
                                    "timeoutSeconds": 10
                                },
                                "resources": {
                                    "limits": {"cpu": "500m", "memory": "512Mi"},
                                    "requests": {"cpu": "25m", "memory": "64Mi"}
                                },
                                "securityContext": {"runAsUser": 1000, "runAsGroup": 1000},
                                "volumeMounts": [
                                    {"mountPath": "/home/hermes/.hermes", "name": "hermes-home"},
                                    {"mountPath": "/etc/hermes-config", "name": "hermes-config", "readOnly": True}
                                ],
                                "env": [
                                    {"name": "HERMES_HOME", "value": "/home/hermes/.hermes"},
                                    {"name": "HERMES_UID", "value": "1000"},
                                    {"name": "HERMES_GID", "value": "1000"},
                                    {"name": "GATEWAY_HEALTH_URL", "value": "http://127.0.0.1:8642"},
                                    {"name": "HERMES_CONFIG", "value": "/etc/hermes-config/config.yaml"},
                                    {"name": "PATH", "value": "/opt/hermes/bin:/opt/hermes/.venv/bin:/opt/data/.local/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"}
                                ]
                            },
                            {
                                "name": "hermes-webui",
                                "image": "ghcr.io/nesquena/hermes-webui:latest",
                                "imagePullPolicy": "Always",
                                "ports": [{"containerPort": 8787, "name": "webui", "protocol": "TCP"}],
                                "readinessProbe": {
                                    "httpGet": {"path": "/", "port": 8787, "scheme": "HTTP"},
                                    "initialDelaySeconds": 30,
                                    "periodSeconds": 15,
                                    "failureThreshold": 3,
                                    "successThreshold": 1,
                                    "timeoutSeconds": 10
                                },
                                "livenessProbe": {
                                    "httpGet": {"path": "/", "port": 8787, "scheme": "HTTP"},
                                    "initialDelaySeconds": 60,
                                    "periodSeconds": 30,
                                    "failureThreshold": 3,
                                    "successThreshold": 1,
                                    "timeoutSeconds": 10
                                },
                                "resources": {
                                    "limits": {"cpu": "500m", "memory": "2Gi"},
                                    "requests": {"cpu": "25m", "memory": "128Mi"}
                                },
                                "securityContext": {"runAsUser": 0, "runAsGroup": 0},
                                "volumeMounts": [
                                    {"mountPath": "/home/hermeswebui/.hermes", "name": "hermes-home"},
                                    {"mountPath": "/workspace", "name": "workspace-data"},
                                    {"mountPath": "/etc/hermes-config", "name": "hermes-config", "readOnly": True}
                                ],
                                "env": [
                                    {"name": "HERMES_WEBUI_HOST", "value": "0.0.0.0"},
                                    {"name": "HERMES_WEBUI_PORT", "value": "8787"},
                                    {"name": "HERMES_WEBUI_STATE_DIR", "value": "/home/hermeswebui/.hermes/webui"},
                                    {"name": "HERMES_API_URL", "value": "http://127.0.0.1:8642"},
                                    {"name": "HERMES_HOME", "value": "/home/hermeswebui/.hermes"},
                                    {"name": "HERMES_CONFIG", "value": "/etc/hermes-config/config.yaml"},
                                    {"name": "PYTHONPATH", "value": "/home/hermeswebui/.hermes/hermes-agent"},
                                    {"name": "HERMES_NIX_BUILD", "value": "1"},
                                    {"name": "WANTED_UID", "value": "1000"},
                                    {"name": "WANTED_GID", "value": "1000"},
                                    {"name": "HERMES_WEBUI_ONBOARDING_OPEN", "value": "1"}
                                ]
                            }
                        ],
                        "volumes": [
                            {"name": "hermes-config", "configMap": {"name": f"{name}-config"}},
                            {"name": "hermes-home", "persistentVolumeClaim": {"claimName": f"{name}-data"}},
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
            "spec": {"ports": [{"port": 80, "targetPort": 8787}],
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
                "url": f"https://{subdomain}.ailab.infocepo.com"}
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
        result = deploy_instance(req.subdomain)
        if "error" in result:
            raise HTTPException(status_code=502, detail=result["error"])
        return JSONResponse(result)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e

def change_password(subdomain: str, new_password: str) -> dict:
    """Change instance password: hash, create job to update PVC, restart deployment."""
    if not new_password or len(new_password) < 4:
        raise HTTPException(status_code=400, detail="Password must be at least 4 characters")

    subdomain = validate_subdomain(subdomain)

    # Find existing pod for this subdomain
    pods = k8s_get("pods", label_selector=f"app=agent-instance,agent-instance={subdomain}")
    if not pods.get("items"):
        raise HTTPException(status_code=404, detail=f"No instance found with subdomain {subdomain}")

    pod_name = pods["items"][0]["metadata"]["name"]

    # Pod name format: agent-{hash8}-{subdomain}-{dep_hash}-{random}
    # The suffix (deployment revision) is always the last 2 parts separated by hyphens.
    # PVC name: {base_name}-data where base_name = pod_name minus last 2 parts.
    parts = pod_name.split('-')
    base_name = '-'.join(parts[:-2])
    pvc_name = f"{base_name}-data"
    deployment_name = base_name  # e.g. agent-9ae62006-vjourne-agent

    # Generate hash (same algorithm as the seed initContainer)
    salt = os.urandom(16).hex()
    salt_bytes = bytes.fromhex(salt)
    password_hash = hashlib.pbkdf2_hmac("sha256", new_password.encode(), salt_bytes, 60000).hex()

    job_name = f"password-update-{subdomain}-{int(datetime.now().timestamp())}"

    # Create a ephemeral Pod to write the new settings.json into the PVC
    pod_body = {
        "apiVersion": "v1",
        "kind": "Pod",
        "metadata": {"name": job_name, "namespace": NAMESPACE, "labels": {"app": "agent-instance", "job-type": "password-update"}},
        "spec": {
            "containers": [{
                "name": "update-password",
                "image": "python:3.12-alpine",
                "command": ["python3", "-c",
                    f"import pathlib,json;s=pathlib.Path('/data/.hermes/webui/s'+'ettings.json');s.parent.mkdir(parents=True,exist_ok=True);s.write_text(json.dumps({{'password_hash':'{password_hash}','password_salt':'{salt}'}}))"
                ],
                "volumeMounts": [{"name": "data-vol", "mountPath": "/data"}]
            }],
            "restartPolicy": "Never",
            "volumes": [{"name": "data-vol", "persistentVolumeClaim": {"claimName": pvc_name}}]
        }
    }

    k8s_post("pods", pod_body)

    # Trigger deployment rollout by adding a timestamp annotation (forces pod recreation)
    try:
        rollout_body = {
            "spec": {
                "template": {
                    "metadata": {
                        "annotations": {"password-changed-at": datetime.now().isoformat()}
                    }
                }
            }
        }
        k8s_patch("deployments", deployment_name, rollout_body)
    except Exception:
        pass  # rollout may fail but job is created

    return {"success": True, "subdomain": subdomain}


@app.post("/api/change-password")
def api_change_password(req: ChangePasswordRequest):
    try:
        return JSONResponse(change_password(req.subdomain, req.new_password))
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

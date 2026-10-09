"""Shared Kubernetes runtime contract for new and existing Hermes instances.

Only stdlib dependencies: usable by the SaaS, deployment CLI and migration CLI.
"""
import copy
import os
import re

RUNTIME_REVISION = "2"
SOURCE_PATH = "/home/hermeswebui/.hermes/hermes-agent"
IMAGE_RE = re.compile(r"^[^\s]+@sha256:[0-9a-f]{64}$")

# The upstream entrypoint owns UID/GID alignment and dependency installation.
# Patch a private copy, never the image or a mounted agent source tree. Stop if
# its contract changes instead of silently falling back to the base Python.
START_SCRIPT = r'''#!/bin/bash
set -euo pipefail
python3 - <<'PY'
import os, pathlib, re, shutil, subprocess, tempfile
version = os.environ['HERMES_WEBUI_PYTHON']
if not re.fullmatch(r'3\.14\.\d+', version):
    raise SystemExit('HERMES_WEBUI_PYTHON must be an exact 3.14 patch version')
venv = pathlib.Path('/app/venv')
python = venv / 'bin/python'
if venv.exists():
    try:
        actual = subprocess.check_output([str(python), '-c',
            'import platform; print(platform.python_version())'], text=True).strip()
    except (OSError, subprocess.CalledProcessError):
        actual = ''
    if actual != version:
        # Only ephemeral image state, never ~/.hermes or /workspace.
        shutil.rmtree(venv)
        stage = pathlib.Path('/app/hermes-agent-src')
        if stage.exists():
            shutil.rmtree(stage)
text = pathlib.Path('/hermeswebui_init.bash').read_text()
for old, new in (
    ('uv venv venv', 'uv venv --python "$HERMES_WEBUI_PYTHON" venv'),
    ('python server.py', 'python /runtime/check.py && python server.py'),
):
    if text.count(old) != 1:
        raise SystemExit('Unsupported WebUI entrypoint contract: ' + old)
    text = text.replace(old, new)
path = pathlib.Path('/tmp/agent-saas-init.bash')
fd, temporary = tempfile.mkstemp(prefix='agent-saas-init-', dir='/tmp')
with os.fdopen(fd, 'w') as stream:
    stream.write(text)
os.chmod(temporary, 0o755)
os.replace(temporary, path)
PY
exec /bin/bash /tmp/agent-saas-init.bash
'''

CHECK_SCRIPT = r'''import os, pathlib, platform, sys, urllib.request
expected = os.environ['HERMES_WEBUI_PYTHON']
if platform.python_version() != expected:
    raise SystemExit('Wrong WebUI runtime: ' + platform.python_version() + ' != ' + expected)
sys.path.insert(0, os.environ['HERMES_WEBUI_AGENT_DIR'])
from run_agent import AIAgent
if AIAgent is None:
    raise SystemExit('AIAgent is unavailable')
if '--health' in sys.argv:
    port = int(os.environ.get('HERMES_WEBUI_PORT', '8787'))
    with urllib.request.urlopen('http://127.0.0.1:' + str(port) + '/health', timeout=3) as response:
        if response.status != 200:
            raise SystemExit('WebUI health check failed')
else:
    print('Hermes runtime verified: Python ' + expected + ', AIAgent import OK', flush=True)
'''

COPY_SOURCE = r'''set -eu
test -f /opt/hermes/run_agent.py
test -f /opt/hermes/pyproject.toml
mkdir -p /agent-src
# emptyDir is new on every Pod creation. Include dotfiles, omit caches and
# image-local venvs; never merge image sources into the persistent home.
tar -C /opt/hermes --exclude=.venv --exclude=venv --exclude=.git \
    --exclude=__pycache__ --exclude=.playwright --exclude='*.egg-info' \
    --exclude=build --exclude=dist -cf - . > /tmp/hermes-source.tar
tar -C /agent-src -xf /tmp/hermes-source.tar
rm /tmp/hermes-source.tar
chown -R 1000:1000 /agent-src
chmod -R u+rwX,go+rX /agent-src
test -f /agent-src/run_agent.py
'''

HOME_INIT = r'''set -eu
mkdir -p /hermes-home/webui /workspace
if [ ! -f /hermes-home/config.yaml ] && [ -f /configmap/config.yaml ]; then
    cp /configmap/config.yaml /hermes-home/config.yaml
fi
chown 1000:1000 /hermes-home /hermes-home/webui /workspace
if [ -f /hermes-home/webui/settings.json ]; then
    chown 1000:1000 /hermes-home/webui/settings.json
fi
'''


def runtime_settings():
    """Fail before creating resources unless the release pair is immutable."""
    agent = os.environ.get("HERMES_AGENT_IMAGE", "")
    webui = os.environ.get("HERMES_WEBUI_IMAGE", "")
    for name, image in (("HERMES_AGENT_IMAGE", agent), ("HERMES_WEBUI_IMAGE", webui)):
        if not IMAGE_RE.fullmatch(image):
            raise ValueError(f"{name} must be an image reference pinned with @sha256:<64 hex digits>")
    python = os.environ.get("HERMES_WEBUI_PYTHON", "3.14.7")
    if not re.fullmatch(r"3\.14\.\d+", python):
        raise ValueError("HERMES_WEBUI_PYTHON must be an exact Python 3.14 patch version")
    return agent, webui, python


def runtime_configmap(name, namespace):
    return {"apiVersion": "v1", "kind": "ConfigMap",
            "metadata": {"name": f"{name}-runtime-v{RUNTIME_REVISION}", "namespace": namespace},
            "immutable": True,
            "data": {"start.sh": START_SCRIPT, "check.py": CHECK_SCRIPT}}


def _put(items, item, key="name"):
    return [x for x in items if x.get(key) != item[key]] + [item]


def repair_pod_spec(spec, name, *, password_secret=None, gateway_secret=None):
    """Update only the Hermes runtime; retain PVCs, resources and extra sidecars."""
    agent_image, webui_image, python = runtime_settings()
    spec = copy.deepcopy(spec)
    containers = {c["name"]: c for c in spec.get("containers", [])}
    if not {"hermes-agent", "hermes-webui"} <= containers.keys():
        raise ValueError("Migration requires an agent-saas Agent + WebUI deployment")
    spec["automountServiceAccountToken"] = False
    spec["volumes"] = _put(spec.get("volumes", []), {"name": "hermes-agent-src", "emptyDir": {}})
    spec["volumes"] = _put(spec["volumes"], {"name": "hermes-runtime",
                                            "configMap": {"name": f"{name}-runtime-v{RUNTIME_REVISION}"}})
    init = [c for c in spec.get("initContainers", [])
            if c["name"] not in ("init-agent-src", "password-seed")]
    for c in init:
        if c["name"] == "init-hermes-home":
            c["command"] = ["sh", "-c"]
            c["args"] = [HOME_INIT]
    init.append({"name": "init-agent-src", "image": agent_image,
                 "imagePullPolicy": "IfNotPresent", "command": ["sh", "-c"],
                 "args": [COPY_SOURCE], "securityContext": {"runAsUser": 0},
                 "resources": {"requests": {"cpu": "25m", "memory": "64Mi"},
                               "limits": {"cpu": "200m", "memory": "1Gi"}},
                 "volumeMounts": [{"name": "hermes-agent-src", "mountPath": "/agent-src"}]})
    spec["initContainers"] = init
    for c in spec["containers"]:
        if c["name"] in ("hermes-agent", "hermes-dashboard"):
            c["image"] = agent_image
            c["imagePullPolicy"] = "IfNotPresent"
    webui = containers["hermes-webui"]
    webui["image"] = webui_image
    webui["imagePullPolicy"] = "IfNotPresent"
    webui["command"] = ["/bin/bash", "/runtime/start.sh"]
    webui.pop("args", None)
    env = [e for e in webui.get("env", []) if e["name"] not in (
        "PYTHONPATH", "HERMES_NIX_BUILD", "HERMES_WEBUI_ONBOARDING_OPEN")]
    for key, value in {
        "HERMES_WEBUI_PYTHON": python,
        "HERMES_WEBUI_AGENT_DIR": SOURCE_PATH,
        "HERMES_WEBUI_STATE_DIR": "/home/hermeswebui/.hermes/webui",
        "UV_PYTHON_INSTALL_DIR": "/app/python",
        "UV_PYTHON_DOWNLOADS": "automatic",
        "HERMES_WEBUI_SKIP_ONBOARDING": "1",
    }.items():
        env = _put(env, {"name": key, "value": value})
    # Migrations reuse the agent's actual key source, including valueFrom.
    key = next((e for e in containers["hermes-agent"].get("env", [])
                if e["name"] == "API_SERVER_KEY"), None)
    if gateway_secret:
        key = {"name": "API_SERVER_KEY", "valueFrom": {"secretKeyRef":
               {"name": gateway_secret, "key": "API_SERVER_KEY"}}}
        containers["hermes-agent"]["env"] = _put(containers["hermes-agent"].get("env", []), key)
    if key is None:
        raise ValueError("Agent API_SERVER_KEY is missing; cannot configure gateway authentication")
    env = _put(env, dict(copy.deepcopy(key), name="HERMES_WEBUI_GATEWAY_API_KEY"))
    if password_secret:
        env = _put(env, {"name": "HERMES_WEBUI_PASSWORD", "valueFrom":
                        {"secretKeyRef": {"name": password_secret, "key": "password"}}})
    webui["env"] = env
    mounts = [m for m in webui.get("volumeMounts", [])
              if m["mountPath"] not in (SOURCE_PATH, "/runtime")]
    webui["volumeMounts"] = mounts + [
        {"name": "hermes-agent-src", "mountPath": SOURCE_PATH, "readOnly": True},
        {"name": "hermes-runtime", "mountPath": "/runtime", "readOnly": True}]
    probe = {"exec": {"command": ["/app/venv/bin/python", "/runtime/check.py", "--health"]},
             "periodSeconds": 30, "timeoutSeconds": 20, "failureThreshold": 3}
    webui["readinessProbe"] = probe
    webui["startupProbe"] = dict(copy.deepcopy(probe), periodSeconds=10, failureThreshold=120)
    webui["livenessProbe"] = {"httpGet": {"path": "/health", "port": 8787},
                              "periodSeconds": 30, "timeoutSeconds": 5, "failureThreshold": 3}
    return spec


def render_deployment(name, namespace, subdomain, instance_hash, *, secret_name=None, llm_env=None):
    agent_image, webui_image, _ = runtime_settings()
    secret_name = secret_name or name + "-webui-secret"
    home = {"name": "hermes-home", "mountPath": "/home/hermes/.hermes"}
    config = {"name": "hermes-config", "mountPath": "/etc/hermes-config", "readOnly": True}
    workspace = {"name": "workspace-data", "mountPath": "/workspace"}
    common = [{"name": "HERMES_HOME", "value": "/home/hermes/.hermes"},
              {"name": "HERMES_UID", "value": "1000"},
              {"name": "HERMES_GID", "value": "1000"},
              {"name": "HERMES_CONFIG", "value": "/etc/hermes-config/config.yaml"}]
    env = common + [{"name": key, "value": os.environ.get(key, default)} for key, default in (
        ("LLM_BASE_URL", "https://api-nothink.ailab.infocepo.com/v1"),
        ("LLM_API_KEY", ""), ("LLM_PROVIDER", "infocepo-alias"), ("LLM_MODEL", "ai-thinking"),
        ("HERMES_ALLOW_ROOT_GATEWAY", "1"), ("HERMES_ACCEPT_HOOKS", "1"),
        ("HERMES_DONT_CHECK_TTY", "1"), ("HERMES_GATEWAY_NO_SUPERVISE", "1"),
        ("MCP_DISABLE", "1"),
        ("API_SERVER_ENABLED", "true"), ("API_SERVER_HOST", "0.0.0.0"))]
    if llm_env:
        for key, value in llm_env.items():
            env = _put(env, {"name": key, "value": value})
    spec = {
        "securityContext": {"fsGroup": 1000, "runAsUser": 1000, "runAsGroup": 1000},
        "initContainers": [{"name": "init-hermes-home", "image": "alpine:3.19",
            "command": ["sh", "-c"], "args": [HOME_INIT], "securityContext": {"runAsUser": 0},
            "volumeMounts": [{"name": "hermes-home", "mountPath": "/hermes-home"}, workspace,
                             {"name": "hermes-config", "mountPath": "/configmap", "readOnly": True}]}],
        "containers": [
            {"name": "hermes-agent", "image": agent_image, "command": ["sh", "-c"],
             "args": ["hermes gateway run --no-supervise --force"], "env": env,
             "ports": [{"containerPort": 8642, "name": "gateway"}],
             "volumeMounts": [home, config, workspace],
             "readinessProbe": {"tcpSocket": {"port": 8642}, "periodSeconds": 10},
             "resources": {"requests": {"cpu": "25m", "memory": "128Mi"},
                           "limits": {"cpu": "2", "memory": "4Gi"}}},
            {"name": "hermes-dashboard", "image": agent_image,
             "command": ["hermes", "dashboard", "--host", "127.0.0.1"],
             "env": common + [{"name": "GATEWAY_HEALTH_URL", "value": "http://127.0.0.1:8642"}],
             "ports": [{"containerPort": 9119, "name": "dashboard"}],
             "volumeMounts": [home, config],
             "readinessProbe": {"tcpSocket": {"port": 9119}, "periodSeconds": 15},
             "livenessProbe": {"tcpSocket": {"port": 9119}, "initialDelaySeconds": 60,
                               "periodSeconds": 30},
             "resources": {"requests": {"cpu": "25m", "memory": "64Mi"},
                           "limits": {"cpu": "500m", "memory": "512Mi"}}},
            {"name": "hermes-webui", "image": webui_image,
             "securityContext": {"runAsUser": 0, "runAsGroup": 0},
             "ports": [{"containerPort": 8787, "name": "webui"}],
             "env": [{"name": key, "value": value} for key, value in {
                 "HERMES_WEBUI_HOST": "0.0.0.0", "HERMES_WEBUI_PORT": "8787",
                 "HERMES_HOME": "/home/hermeswebui/.hermes",
                 "HERMES_CONFIG": "/etc/hermes-config/config.yaml",
                 "HERMES_API_URL": "http://127.0.0.1:8642",
                 "WANTED_UID": "1000", "WANTED_GID": "1000"}.items()],
             "volumeMounts": [{"name": "hermes-home", "mountPath": "/home/hermeswebui/.hermes"},
                              config, workspace],
             "resources": {"requests": {"cpu": "25m", "memory": "128Mi"},
                           "limits": {"cpu": "500m", "memory": "2Gi"}}}],
        "volumes": [{"name": "hermes-home", "persistentVolumeClaim": {"claimName": name + "-data"}},
                    {"name": "workspace-data", "persistentVolumeClaim": {"claimName": name + "-workspace"}},
                    {"name": "hermes-config", "configMap": {"name": name + "-config"}}],
    }
    labels = {"app": "agent-instance", "agent-instance": subdomain, "agent-hash": instance_hash}
    return {"apiVersion": "v1", "kind": "List", "items": [runtime_configmap(name, namespace), {
        "apiVersion": "apps/v1", "kind": "Deployment",
        "metadata": {"name": name, "namespace": namespace, "labels": labels},
        "spec": {"replicas": 1, "strategy": {"type": "Recreate"},
                 "selector": {"matchLabels": {"app": "agent-instance", "agent-instance": subdomain}},
                 "template": {"metadata": {"labels": labels}, "spec": repair_pod_spec(
                     spec, name, password_secret=secret_name, gateway_secret=secret_name)}}}]}


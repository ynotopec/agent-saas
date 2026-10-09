"""Shared Kubernetes runtime contract for new and existing Hermes instances.

Only stdlib dependencies: usable by the SaaS, deployment CLI and migration CLI.
"""
import copy
import os
from release_policy import production_images

SOURCE_PATH = "/home/hermeswebui/.hermes/hermes-agent"
# Probe only: it never changes the official interpreter or entrypoint.
PROBE_SCRIPT = r'''import urllib.request
from run_agent import AIAgent
assert AIAgent is not None
with urllib.request.urlopen("http://127.0.0.1:8787/health", timeout=3) as response:
    assert response.status == 200
'''

COPY_SOURCE = r'''set -eu
test -f /opt/hermes/run_agent.py
test -f /opt/hermes/pyproject.toml
mkdir -p /agent-src
# emptyDir is new on every Pod creation. Include dotfiles, omit caches and
# image-local venvs; never merge image sources into the persistent home.
tar -C /opt/hermes --exclude=.venv --exclude=venv --exclude=.git \
    --exclude=__pycache__ --exclude=.playwright --exclude='*.egg-info' \
    --exclude=build --exclude=dist --exclude=.install.lock -cf - . > /tmp/hermes-source.tar
tar -C /agent-src -xf /tmp/hermes-source.tar
rm /tmp/hermes-source.tar
chown -R 1000:1000 /agent-src
chmod -R u+rwX,go+rX,go-w /agent-src
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


# Agent image supplies python-dotenv. Preserve unrelated settings and quoted,
# multiline values using its parser, rather than treating .env as shell lines.
RECONCILE_ENV = r'''import io, os, pathlib, tempfile
from dotenv.parser import parse_stream
home = pathlib.Path(os.environ["HERMES_HOME"])
if not os.environ.get("API_SERVER_KEY", "").strip():
    raise SystemExit("Explicit API_SERVER_KEY is required for reconciliation")
p = home / ".env"
if p.is_symlink():
    raise SystemExit("Refusing a symlink .env")
if p.exists():
    original = p.read_text()
    bindings = list(parse_stream(io.StringIO(original)))
    if any(b.error for b in bindings):
        raise SystemExit("Invalid .env; refusing to rewrite it")
    updated = "".join(b.original.string for b in bindings if b.key != "API_SERVER_KEY")
    if updated != original:
        backup_dir = home / "backups" / "runtime-repair"
        backup_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(backup_dir, 0o700)
        os.chown(backup_dir, 1000, 1000)
        fd, backup = tempfile.mkstemp(prefix="env-before-key-reconcile-", dir=backup_dir)
        with os.fdopen(fd, "w") as f:
            f.write(original)
            f.flush(); os.fsync(f.fileno())
        os.chown(backup, 1000, 1000)
        fd, staged = tempfile.mkstemp(prefix=".env-reconcile-", dir=home)
        try:
            with os.fdopen(fd, "w") as f:
                f.write(updated)
                f.flush(); os.fsync(f.fileno())
            os.chown(staged, 1000, 1000)
            os.replace(staged, p)
        finally:
            if os.path.exists(staged): os.unlink(staged)
        print("Reconciled inherited API_SERVER_KEY; private backup saved")
'''


def runtime_settings():
    """Use the checked-in fixed pair; raw image env vars cannot override it."""
    return production_images()


def _put(items, item, key="name"):
    return [x for x in items if x.get(key) != item[key]] + [item]


def repair_pod_spec(spec, name, *, password_secret=None, gateway_secret=None, images=None):
    """Update only the Hermes runtime; retain PVCs, resources and extra sidecars."""
    agent_image, webui_image = images or runtime_settings()
    spec = copy.deepcopy(spec)
    containers = {c["name"]: c for c in spec.get("containers", [])}
    if not {"hermes-agent", "hermes-webui"} <= containers.keys():
        raise ValueError("Migration requires an agent-saas Agent + WebUI deployment")
    spec["automountServiceAccountToken"] = False
    spec["volumes"] = _put(spec.get("volumes", []), {"name": "hermes-agent-src", "emptyDir": {}})
    spec["volumes"] = [v for v in spec["volumes"] if v["name"] != "hermes-runtime"]
    legacy_password = next((e for c in spec.get("initContainers", []) if c["name"] == "password-seed"
                            for e in c.get("env", []) if e["name"] == "HERMES_WEBUI_PASSWORD"), None)
    init = [c for c in spec.get("initContainers", [])
            if c["name"] not in ("init-agent-src", "password-seed", "reconcile-gateway-env")]
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
            c.pop("command", None)
            c["env"] = [e for e in c.get("env", []) if e["name"] != "HERMES_GATEWAY_NO_SUPERVISE"]
            c["args"] = ["gateway", "run"] if c["name"] == "hermes-agent" else ["dashboard", "--host", "127.0.0.1"]
            c["securityContext"] = dict(c.get("securityContext", {}), runAsUser=0, runAsGroup=0)
    webui = containers["hermes-webui"]
    webui["image"] = webui_image
    webui["imagePullPolicy"] = "IfNotPresent"
    # Let the official image choose its entrypoint, CMD and interpreter.
    webui.pop("command", None)
    webui.pop("args", None)
    env = [e for e in webui.get("env", []) if e["name"] not in (
        "PYTHONPATH", "HERMES_NIX_BUILD", "HERMES_WEBUI_ONBOARDING_OPEN",
        "HERMES_WEBUI_PYTHON", "UV_PYTHON_INSTALL_DIR", "UV_PYTHON_DOWNLOADS", "UV_PYTHON")]
    for key, value in {
        "HERMES_WEBUI_AGENT_DIR": SOURCE_PATH,
        "HERMES_WEBUI_STATE_DIR": "/home/hermeswebui/.hermes/webui",
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
    if key is None or ("value" in key and not key["value"].strip()):
        raise ValueError("Agent API_SERVER_KEY is missing; cannot configure gateway authentication")
    agent = containers["hermes-agent"]
    home_env = next((e for e in agent.get("env", []) if e["name"] == "HERMES_HOME"),
                    {"name": "HERMES_HOME", "value": "/home/hermes/.hermes"})
    home_path = home_env.get("value")
    home_mount = next((m for m in agent.get("volumeMounts", []) if m["mountPath"] == home_path), None)
    if home_mount is None or home_mount.get("readOnly"):
        raise ValueError("Agent HERMES_HOME requires an explicit writable volume mount")
    spec["initContainers"].append({
        "name": "reconcile-gateway-env", "image": agent_image,
        "imagePullPolicy": "IfNotPresent",
        "command": ["/opt/hermes/.venv/bin/python", "-c"], "args": [RECONCILE_ENV],
        "env": [copy.deepcopy(home_env), copy.deepcopy(key)],
        "securityContext": {"runAsUser": 0, "runAsGroup": 0},
        "resources": {"requests": {"cpu": "25m", "memory": "32Mi"},
                      "limits": {"cpu": "200m", "memory": "128Mi"}},
        "volumeMounts": [copy.deepcopy(home_mount)]})
    env = _put(env, dict(copy.deepcopy(key), name="HERMES_WEBUI_GATEWAY_API_KEY"))
    if legacy_password and not any(e["name"] == "HERMES_WEBUI_PASSWORD" for e in env):
        env = _put(env, copy.deepcopy(legacy_password))
    if password_secret:
        env = _put(env, {"name": "HERMES_WEBUI_PASSWORD", "valueFrom":
                        {"secretKeyRef": {"name": password_secret, "key": "password"}}})
    webui["env"] = env
    mounts = [m for m in webui.get("volumeMounts", [])
              if m["mountPath"] not in (SOURCE_PATH, "/runtime")]
    webui["volumeMounts"] = mounts + [
        {"name": "hermes-agent-src", "mountPath": SOURCE_PATH, "readOnly": True}]
    probe = {"exec": {"command": ["/app/venv/bin/python", "-c", PROBE_SCRIPT]},
             "periodSeconds": 30, "timeoutSeconds": 20, "failureThreshold": 3}
    webui["readinessProbe"] = probe
    webui["startupProbe"] = dict(copy.deepcopy(probe), periodSeconds=10, failureThreshold=120)
    webui["livenessProbe"] = {"httpGet": {"path": "/health", "port": 8787},
                              "periodSeconds": 30, "timeoutSeconds": 5, "failureThreshold": 3}
    return spec


def render_deployment(name, namespace, subdomain, instance_hash, *, secret_name=None, llm_env=None, images=None):
    agent_image, webui_image = images or runtime_settings()
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
        ("HERMES_DONT_CHECK_TTY", "1"),
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
            {"name": "hermes-agent", "image": agent_image, "args": ["gateway", "run"], "env": env,
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
             "readinessProbe": {"exec": {"command": ["sh", "-c", "python3 -c 'import socket; s=socket.create_connection((\"127.0.0.1\",9119),timeout=3); s.close()'"]}, "periodSeconds": 15},
             "livenessProbe": {"exec": {"command": ["sh", "-c", "python3 -c 'import socket; s=socket.create_connection((\"127.0.0.1\",9119),timeout=3); s.close()'"]}, "initialDelaySeconds": 60, "periodSeconds": 30},
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
    return {"apiVersion": "v1", "kind": "List", "items": [{
        "apiVersion": "apps/v1", "kind": "Deployment",
        "metadata": {"name": name, "namespace": namespace, "labels": labels},
        "spec": {"replicas": 1, "progressDeadlineSeconds": 1800, "strategy": {"type": "Recreate"},
                 "selector": {"matchLabels": {"app": "agent-instance", "agent-instance": subdomain}},
                 "template": {"metadata": {"labels": labels}, "spec": repair_pod_spec(
                     spec, name, password_secret=secret_name, gateway_secret=secret_name, images=(agent_image, webui_image))}}}]}


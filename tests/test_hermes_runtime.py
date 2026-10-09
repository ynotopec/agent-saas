import copy
import importlib.util
import json
import pathlib
import subprocess
import sys
from unittest.mock import patch

import pytest

import hermes_runtime as runtime

ROOT = pathlib.Path(__file__).resolve().parents[1]


def load_script(name):
    spec = importlib.util.spec_from_file_location(name.replace("-", "_"), ROOT / "scripts" / (name + ".py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def release(monkeypatch):
    monkeypatch.setenv("HERMES_AGENT_IMAGE", "nousresearch/hermes-agent@sha256:" + "a" * 64)
    monkeypatch.setenv("HERMES_WEBUI_IMAGE", "ghcr.io/nesquena/hermes-webui@sha256:" + "b" * 64)
    monkeypatch.setenv("HERMES_WEBUI_PYTHON", "3.14.7")


@pytest.fixture
def existing(release):
    deployment = load_script("render-hermes-deployment").render("agent-test", "demo1", "test", "abc")["items"][1]
    deployment["metadata"]["resourceVersion"] = "42"
    spec = deployment["spec"]["template"]["spec"]
    spec["volumes"] = [v for v in spec["volumes"] if v["name"] not in ("hermes-agent-src", "hermes-runtime")]
    webui = next(c for c in spec["containers"] if c["name"] == "hermes-webui")
    webui["volumeMounts"] = [m for m in webui["volumeMounts"] if m["name"] not in ("hermes-agent-src", "hermes-runtime")]
    webui["env"] += [{"name": "HERMES_NIX_BUILD", "value": "1"},
                     {"name": "PYTHONPATH", "value": "/old/source"}]
    spec["containers"].append({"name": "custom-sidecar", "image": "example:test"})
    spec["nodeSelector"] = {"kubernetes.io/arch": "arm64"}
    return deployment


def test_migration_preserves_storage_and_extra_sidecars(existing):
    before = copy.deepcopy(existing)
    patch = load_script("repair-existing-hermes").plan(existing)
    assert existing == before
    assert patch["metadata"]["resourceVersion"] == "42"
    result = patch["spec"]["template"]["spec"]
    assert [v for v in result["volumes"] if "persistentVolumeClaim" in v] == [
        v for v in before["spec"]["template"]["spec"]["volumes"] if "persistentVolumeClaim" in v]
    assert result["containers"][-1] == {"name": "custom-sidecar", "image": "example:test"}
    assert result["nodeSelector"] == {"kubernetes.io/arch": "arm64"}
    assert result["automountServiceAccountToken"] is False


def test_repair_is_idempotent_and_authenticates_gateway(existing):
    spec = existing["spec"]["template"]["spec"]
    once = runtime.repair_pod_spec(spec, "agent-test")
    assert runtime.repair_pod_spec(once, "agent-test") == once
    webui = next(c for c in once["containers"] if c["name"] == "hermes-webui")
    env = {e["name"]: e for e in webui["env"]}
    assert "PYTHONPATH" not in env and "HERMES_NIX_BUILD" not in env
    assert env["HERMES_WEBUI_GATEWAY_API_KEY"]["valueFrom"]["secretKeyRef"]["key"] == "API_SERVER_KEY"
    assert next(m for m in webui["volumeMounts"] if m["mountPath"] == runtime.SOURCE_PATH)["readOnly"]
    assert webui["readinessProbe"]["exec"]["command"][-1] == "--health"


@pytest.mark.parametrize("value", ["", "nousresearch/hermes-agent:latest", "example:v1", "example@sha256:bad"])
def test_mutable_or_missing_images_rejected_before_creating_resources(value, monkeypatch, release):
    import app
    monkeypatch.setenv("HERMES_AGENT_IMAGE", value)
    with patch.object(app, "k8s_post") as create:
        assert "error" in app.deploy_instance("test")
        create.assert_not_called()


def test_home_init_preserves_config_and_canonical_settings(tmp_path):
    home, workspace, config = [tmp_path / name for name in ("home", "workspace", "config")]
    (home / "webui").mkdir(parents=True)
    (home / ".hermes/webui").mkdir(parents=True)
    config.mkdir()
    (home / "config.yaml").write_text("user config")
    (config / "config.yaml").write_text("replacement")
    (home / "webui/settings.json").write_text('{"theme":"dark"}')
    (home / ".hermes/webui/settings.json").write_text('{"theme":"old"}')
    # Filesystem behavior is real; substitute the container paths and UID tool.
    script = runtime.HOME_INIT.replace("/hermes-home", str(home)).replace("/workspace", str(workspace)).replace("/configmap", str(config))
    script = script.replace("chown 1000:1000", "true")
    subprocess.run(["bash", "-ec", script], check=True)
    assert (home / "config.yaml").read_text() == "user config"
    assert json.loads((home / "webui/settings.json").read_text()) == {"theme": "dark"}
    (home / "webui/settings.json").unlink()
    subprocess.run(["bash", "-ec", script], check=True)
    assert not (home / "webui/settings.json").exists()
    assert (home / ".hermes/webui/settings.json").exists()


def test_runtime_check_rejects_wrong_python_and_missing_agent(tmp_path, monkeypatch):
    import platform
    checker = tmp_path / "check.py"
    checker.write_text(runtime.CHECK_SCRIPT)
    monkeypatch.setenv("HERMES_WEBUI_AGENT_DIR", str(tmp_path))
    monkeypatch.setenv("HERMES_WEBUI_PYTHON", "0.0.0")
    wrong = subprocess.run([sys.executable, str(checker)], capture_output=True, text=True)
    assert wrong.returncode != 0 and "Wrong WebUI runtime" in wrong.stderr
    monkeypatch.setenv("HERMES_WEBUI_PYTHON", platform.python_version())
    missing = subprocess.run([sys.executable, str(checker)], capture_output=True, text=True)
    assert missing.returncode != 0 and "run_agent" in missing.stderr
    (tmp_path / "run_agent.py").write_text("class AIAgent: pass\n")
    valid = subprocess.run([sys.executable, str(checker)], capture_output=True, text=True)
    assert valid.returncode == 0 and "AIAgent import OK" in valid.stdout


def test_bootstrap_rebuilds_wrong_venv_and_rejects_entrypoint_drift(tmp_path, release):
    code = runtime.START_SCRIPT.split("python3 - <<'PY'\n", 1)[1].split("\nPY\n", 1)[0]
    code = code.replace("/app/", str(tmp_path / "app") + "/")
    code = code.replace("/hermeswebui_init.bash", str(tmp_path / "upstream.bash"))
    code = code.replace("'/tmp'", repr(str(tmp_path)))
    code = code.replace("/tmp/agent-saas-init.bash", str(tmp_path / "patched.bash"))
    (tmp_path / "upstream.bash").write_text("uv venv venv\npython server.py\n")
    old = tmp_path / "app/venv/bin/python"
    old.parent.mkdir(parents=True)
    old.write_text("#!/bin/sh\necho 3.12.0\n")
    old.chmod(0o755)
    (tmp_path / "home").mkdir()
    (tmp_path / "home/state.db").write_text("sessions")
    result = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert not old.parent.parent.exists()
    assert (tmp_path / "home/state.db").read_text() == "sessions"
    assert 'uv venv --python "$HERMES_WEBUI_PYTHON" venv' in (tmp_path / "patched.bash").read_text()
    (tmp_path / "upstream.bash").write_text("new unsupported upstream script\n")
    result = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
    assert result.returncode != 0 and "Unsupported WebUI entrypoint contract" in result.stderr


def test_dry_run_has_no_mutations(existing, monkeypatch):
    module = load_script("repair-existing-hermes")
    calls = []
    def run(command, **kwargs):
        calls.append(command)
        return subprocess.CompletedProcess(command, 0, json.dumps({"items": [existing]}))
    monkeypatch.setattr(module.subprocess, "run", run)
    module.main(["demo1"])
    assert len(calls) == 1 and calls[0][3] == "get"


def test_failed_rollout_restores_canary_and_stops(existing, monkeypatch, tmp_path):
    module = load_script("repair-existing-hermes")
    second = copy.deepcopy(existing)
    second["metadata"]["name"] = "agent-z"
    calls = []
    def run(command, **kwargs):
        calls.append((command, kwargs))
        if "rollout" in command:
            raise subprocess.CalledProcessError(1, command)
        if command[3:5] == ["get", "deployment"]:
            return subprocess.CompletedProcess(command, 0, json.dumps(
                {"items": [existing, second]} if "-l" in command else existing))
        if "rollback.json" in command[-1]:
            rollback = json.loads(pathlib.Path(command[-1]).read_text())
            assert rollback["spec"]["template"] == existing["spec"]["template"]
        return subprocess.CompletedProcess(command, 0, "")
    monkeypatch.setattr(module.subprocess, "run", run)
    with pytest.raises(subprocess.CalledProcessError):
        module.main(["demo1", "--apply", "--backup-dir", str(tmp_path / "backup")])
    patch_calls = [command for command, _ in calls if "patch" in command]
    assert len(patch_calls) == 2
    assert all("agent-z" not in command for command in patch_calls)
    assert (tmp_path / "backup/agent-test.before.json").stat().st_mode & 0o777 == 0o600

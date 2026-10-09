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
def existing(release):
    deployment = load_script("render-hermes-deployment").render("agent-test", "demo1", "test", "abc")["items"][0]
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
    assert webui["readinessProbe"]["exec"]["command"][-1] == runtime.PROBE_SCRIPT


@pytest.mark.parametrize("value", ["", "nousresearch/hermes-agent:latest", "example:v1", "example@sha256:bad"])
def test_mutable_or_missing_images_rejected_before_creating_resources(value, release):
    import app
    record = json.loads(release.read_text())
    record['agent']['image'] = value
    release.write_text(json.dumps(record))
    with patch.object(app, "k8s_post") as create:
        assert "error" in app.deploy_instance("test")
        create.assert_not_called()


def test_official_entrypoint_and_python_are_preserved(existing):
    spec = runtime.repair_pod_spec(existing['spec']['template']['spec'], 'agent-test')
    webui = next(c for c in spec['containers'] if c['name'] == 'hermes-webui')
    assert 'command' not in webui and 'args' not in webui
    names = {e['name'] for e in webui['env']}
    assert not {'HERMES_WEBUI_PYTHON', 'UV_PYTHON', 'HERMES_NIX_BUILD'} & names
    assert 'password-seed' not in {c['name'] for c in spec['initContainers']}


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
        if command[3] == "patch" and "rollback.json" in command[-1]:
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


def test_source_copy_excludes_venvs_and_transient_locks(tmp_path):
    src, dst = tmp_path / 'image-source', tmp_path / 'copied-source'
    src.mkdir()
    (src / 'run_agent.py').write_text('class AIAgent: pass')
    (src / 'pyproject.toml').write_text('[project]')
    (src / '.hidden').write_text('included')
    (src / 'tools').mkdir()
    (src / 'tools/.install.lock').write_text('lock')
    (src / '.venv').mkdir(); (src / '.venv/python').write_text('excluded')
    script = runtime.COPY_SOURCE.replace('/opt/hermes', str(src)).replace('/agent-src', str(dst))
    script = script.replace('/tmp/hermes-source.tar', str(tmp_path / 'source.tar')).replace('chown -R 1000:1000', 'true')
    subprocess.run(['bash', '-ec', script], check=True)
    assert (dst / '.hidden').read_text() == 'included'
    assert not (dst / '.venv').exists()
    assert not (dst / 'tools/.install.lock').exists()
    assert dst.stat().st_mode & 0o022 == 0


def test_native_password_inherited_from_legacy_seed(existing):
    spec = existing['spec']['template']['spec']
    webui = next(c for c in spec['containers'] if c['name'] == 'hermes-webui')
    webui['env'] = [e for e in webui['env'] if e['name'] != 'HERMES_WEBUI_PASSWORD']
    password = {'name': 'HERMES_WEBUI_PASSWORD', 'valueFrom': {'secretKeyRef': {'name': 'legacy-auth', 'key': 'password'}}}
    spec['initContainers'].append({'name': 'password-seed', 'env': [password]})
    repaired = runtime.repair_pod_spec(spec, 'agent-test')
    webui = next(c for c in repaired['containers'] if c['name'] == 'hermes-webui')
    assert password in webui['env']
    assert 'password-seed' not in {c['name'] for c in repaired['initContainers']}


def test_repair_restores_s6_and_reconciles_pvc_key(existing):
    spec = existing['spec']['template']['spec']
    agent = next(c for c in spec['containers'] if c['name'] == 'hermes-agent')
    agent['env'].append({'name': 'HERMES_GATEWAY_NO_SUPERVISE', 'value': '1'})
    agent['args'] = ['gateway', 'run', '--no-supervise', '--force']
    fixed = runtime.repair_pod_spec(spec, 'test')
    agent = next(c for c in fixed['containers'] if c['name'] == 'hermes-agent')
    assert agent['args'] == ['gateway', 'run']
    assert not any(e['name'] == 'HERMES_GATEWAY_NO_SUPERVISE' for e in agent['env'])
    reconcile = next(c for c in fixed['initContainers'] if c['name'] == 'reconcile-gateway-env')
    key = next(e for e in agent['env'] if e['name'] == 'API_SERVER_KEY')
    assert reconcile['env'][1] == key
    assert reconcile['image'] == agent['image']
    assert reconcile['volumeMounts'][0] in agent['volumeMounts']
    assert runtime.repair_pod_spec(fixed, 'test') == fixed


def test_pvc_reconciliation_preserves_secrets_multiline_and_backup(tmp_path, monkeypatch):
    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    monkeypatch.setenv('API_SERVER_KEY', 'container-key')
    original = '# keep comment\nexport API_SERVER_KEY="old\nkey"\nOTHER="keep\nme"\nAPI_SERVER_KEY=duplicate\n'
    env = tmp_path / '.env'; env.write_text(original)
    # Avoid requiring root in developer/CI tests; content and modes are real.
    with patch('os.chown'):
        exec(runtime.RECONCILE_ENV, {})
        exec(runtime.RECONCILE_ENV, {})
    assert env.read_text() == '# keep comment\nOTHER="keep\nme"\n'
    backups = list((tmp_path / 'backups/runtime-repair').iterdir())
    assert len(backups) == 1 and backups[0].read_text() == original
    assert backups[0].stat().st_mode & 0o777 == 0o600
    assert env.stat().st_mode & 0o777 == 0o600


@pytest.mark.parametrize('bad', ['API_SERVER_KEY="unterminated', None])
def test_pvc_reconciliation_rejects_invalid_or_symlink_env(tmp_path, monkeypatch, bad):
    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    monkeypatch.setenv('API_SERVER_KEY', 'container-key')
    env = tmp_path / '.env'
    if bad is None:
        target = tmp_path / 'external'; target.write_text('API_SERVER_KEY=old\n')
        env.symlink_to(target)
    else:
        env.write_text(bad)
    before = env.read_text()
    with pytest.raises(SystemExit):
        exec(runtime.RECONCILE_ENV, {})
    assert env.read_text() == before
    assert not (tmp_path / 'backups').exists()


@pytest.mark.parametrize('key_source', ['literal', 'inherited-secret', 'new-secret'])
def test_shared_home_writers_receive_same_gateway_key(existing, key_source):
    spec = existing['spec']['template']['spec']
    agent = next(c for c in spec['containers'] if c['name'] == 'hermes-agent')
    dashboard = next(c for c in spec['containers'] if c['name'] == 'hermes-dashboard')
    key = {'name': 'API_SERVER_KEY', 'value': 'explicit-agent-key'} if key_source == 'literal' else {
        'name': 'API_SERVER_KEY', 'valueFrom': {'secretKeyRef': {'name': 'inherited-key', 'key': 'api-key'}}}
    agent['env'] = runtime._put(agent['env'], key)
    dashboard['env'] += [{'name': 'API_SERVER_KEY', 'value': 'stale-dashboard-key'},
                         {'name': 'API_SERVER_KEY', 'value': 'duplicate-dashboard-key'}]
    fixed = runtime.repair_pod_spec(spec, 'test', gateway_secret='new-key' if key_source == 'new-secret' else None)
    containers = {c['name']: c for c in fixed['containers']}
    expected = {'name': 'API_SERVER_KEY', 'valueFrom': {'secretKeyRef': {'name': 'new-key', 'key': 'API_SERVER_KEY'}}} if key_source == 'new-secret' else key
    for name in ('hermes-agent', 'hermes-dashboard'):
        assert [e for e in containers[name]['env'] if e['name'] == 'API_SERVER_KEY'] == [expected]
    assert next(e for e in containers['hermes-webui']['env'] if e['name'] == 'HERMES_WEBUI_GATEWAY_API_KEY') == dict(expected, name='HERMES_WEBUI_GATEWAY_API_KEY')
    assert runtime.repair_pod_spec(fixed, 'test') == fixed
    assert dashboard['env'][-1]['value'] == 'duplicate-dashboard-key'


def test_new_deployment_provisions_shared_key_and_webui_password(release):
    obj = runtime.render_deployment('new-agent', 'demo1', 'new', 'hash', secret_name='runtime-secret')
    containers = {c['name']: c for c in obj['items'][0]['spec']['template']['spec']['containers']}
    for name in ('hermes-agent', 'hermes-dashboard'):
        key = next(e for e in containers[name]['env'] if e['name'] == 'API_SERVER_KEY')
        assert key['valueFrom']['secretKeyRef'] == {'name': 'runtime-secret', 'key': 'API_SERVER_KEY'}
    password = next(e for e in containers['hermes-webui']['env'] if e['name'] == 'HERMES_WEBUI_PASSWORD')
    assert password['valueFrom']['secretKeyRef'] == {'name': 'runtime-secret', 'key': 'password'}

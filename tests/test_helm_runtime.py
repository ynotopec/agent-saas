import json
import os
import pathlib
import shutil
import subprocess
import pytest
import yaml
import hermes_runtime as runtime

ROOT = pathlib.Path(__file__).resolve().parents[1]
HELM = os.environ.get('HELM') or shutil.which('helm')


def test_chart_scripts_match_shared_runtime():
    for filename, content in {'probe.py': runtime.PROBE_SCRIPT, 'copy-source.sh': runtime.COPY_SOURCE}.items():
        assert (ROOT / 'charts/hermes-webui/runtime' / filename).read_text() == content


@pytest.mark.skipif(not HELM, reason='Helm binary required')
@pytest.mark.parametrize('seed', [False, True])
def test_chart_renders_pinned_sources_and_official_runtime(seed):
    command = [HELM, 'template', 'canary', str(ROOT / 'charts/hermes-webui')]
    if seed:
        command += ['--set', 'hermesConfig.seed.enabled=true', '--set-string', 'hermesConfig.seed.config=model: {}']
    objects = list(yaml.safe_load_all(subprocess.check_output(command, text=True)))
    deployment = next(o for o in objects if o and o.get('kind') == 'Deployment')
    spec = deployment['spec']['template']['spec']
    webui = spec['containers'][0]
    assert '@sha256:' in webui['image']
    assert 'command' not in webui and 'args' not in webui
    assert spec['initContainers'][-1]['name'] == 'init-agent-src'
    assert bool(any(c['name'] == 'seed-hermes-config' for c in spec['initContainers'])) == seed
    assert webui['startupProbe']['exec']['command'][-1] == runtime.PROBE_SCRIPT
    assert deployment['spec']['progressDeadlineSeconds'] == 1800
    env = {e['name']: e for e in webui['env']}
    assert env['WANTED_UID']['value'] == '1000'
    assert 'HERMES_WEBUI_PYTHON' not in env


@pytest.mark.skipif(not HELM, reason='Helm binary required')
def test_chart_rejects_digest_drift():
    result = subprocess.run([HELM, 'template', 'canary', str(ROOT / 'charts/hermes-webui'),
                             '--set-string', 'image.digest=sha256:' + 'a' * 64], capture_output=True, text=True)
    assert result.returncode != 0 and 'differ' in result.stderr


@pytest.mark.skipif(not HELM, reason='Helm binary required')
def test_chart_rejects_too_recent_release(tmp_path):
    import datetime as dt
    record = json.loads((ROOT / 'releases/hermes-images.json').read_text())
    record['webui']['published_at'] = dt.datetime.now(dt.timezone.utc).isoformat()
    values = tmp_path / 'values.json'; values.write_text(json.dumps({'release': record}))
    result = subprocess.run([HELM, 'template', 'canary', str(ROOT / 'charts/hermes-webui'), '-f', str(values)], capture_output=True, text=True)
    assert result.returncode != 0 and 'younger' in result.stderr

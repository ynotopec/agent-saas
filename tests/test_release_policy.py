import datetime as dt
import json
import pytest
from release_policy import check_release, delay_days, production_images


def test_pinned_pair_age_boundary(release):
    record = json.loads(release.read_text())
    published = dt.datetime.fromisoformat(record['webui']['published_at'].replace('Z', '+00:00'))
    with pytest.raises(ValueError, match='younger'):
        check_release(record, 7, published + dt.timedelta(days=7, microseconds=-1))
    assert check_release(record, 7, published + dt.timedelta(days=7))[0] == record['agent']['image']


@pytest.mark.parametrize('value', ['-1', '1.5', 'wrong', ''])
def test_invalid_delay_rejected(value):
    with pytest.raises(ValueError):
        delay_days(value)


def test_delay_configurable_and_no_latest_bypass(release, monkeypatch):
    assert delay_days('14') == 14
    monkeypatch.setenv('HERMES_AGENT_IMAGE', 'nousresearch/hermes-agent:latest')
    assert '@sha256:' in production_images()[0]
    record = json.loads(release.read_text())
    record['webui']['published_at'] = dt.datetime.now(dt.timezone.utc).isoformat()
    release.write_text(json.dumps(record))
    with pytest.raises(ValueError, match='younger'):
        production_images()


def test_failed_canary_does_not_modify_pins(release, monkeypatch):
    import importlib.util
    from pathlib import Path
    spec = importlib.util.spec_from_file_location('release_cli', Path(__file__).parents[1] / 'scripts/hermes-release.py')
    module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
    before = release.read_bytes()
    monkeypatch.setattr(module.subprocess, 'check_output', lambda *a, **k: json.dumps({
        'spec': {'template': {'spec': {'containers': []}}}}))
    with pytest.raises(ValueError, match='exact pinned'):
        module.main(['validate', '--deployment', 'canary'])
    assert release.read_bytes() == before

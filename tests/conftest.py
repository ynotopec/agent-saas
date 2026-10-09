import json
from pathlib import Path
import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def release(monkeypatch, tmp_path):
    record = json.loads((ROOT / 'releases/hermes-images.json').read_text())
    path = tmp_path / 'hermes-images.json'
    path.write_text(json.dumps(record))
    monkeypatch.setenv('HERMES_RELEASE_FILE', str(path))
    monkeypatch.setenv('HERMES_RELEASE_DELAY_DAYS', '7')
    monkeypatch.delenv('HERMES_TARGET_ARCH', raising=False)
    return path

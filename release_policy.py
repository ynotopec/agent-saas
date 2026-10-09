"""Read immutable official image pins; never resolve a mutable tag at deployment."""
import datetime as dt
import json
import os
from pathlib import Path
import re

IMAGE_RE = re.compile(r'^[^\s]+@sha256:[0-9a-f]{64}$')
DEFAULT_DELAY_DAYS = 7


def delay_days(value=None):
    value = os.environ.get('HERMES_RELEASE_DELAY_DAYS', str(DEFAULT_DELAY_DAYS)) if value is None else value
    if not re.fullmatch(r'[0-9]+', str(value)):
        raise ValueError('HERMES_RELEASE_DELAY_DAYS must be a non-negative integer')
    return int(value)


def timestamp(value):
    parsed = dt.datetime.fromisoformat(value.replace('Z', '+00:00'))
    if parsed.tzinfo is None:
        raise ValueError('Image publication dates must include a timezone')
    return parsed


def read_json(path):
    try:
        record = json.loads(Path(path).read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f'Cannot read fixed Hermes image manifest: {path}') from exc
    if not isinstance(record, dict):
        raise ValueError('Image manifest must be a JSON object')
    return record


def check_release(record, days=None, now=None):
    now = now or dt.datetime.now(dt.timezone.utc)
    if record.get('schema_version') != 1:
        raise ValueError('Unsupported image manifest schema')
    for name, repository in [('agent', 'nousresearch/hermes-agent'), ('webui', 'ghcr.io/nesquena/hermes-webui')]:
        image = record.get(name, {})
        ref = image.get('image', '')
        if not IMAGE_RE.fullmatch(ref) or ref.split('@')[0] != repository:
            raise ValueError(f'{name} must use the official repository pinned by sha256 digest')
        if image.get('tag') in ('latest', 'stable', 'main', 'experimental', None):
            raise ValueError(f'{name} must identify its fixed version tag')
        published = timestamp(image['published_at'])
        if now - published < dt.timedelta(days=delay_days(days)):
            raise ValueError(f'{name} image is younger than the configured release delay')
        if set(image.get('architectures', [])) != {'amd64', 'arm64'}:
            raise ValueError(f'{name} manifest must cover amd64 and arm64')
    return record['agent']['image'], record['webui']['image']


def production_file():
    return Path(os.environ.get('HERMES_RELEASE_FILE', str(Path(__file__).parent / 'releases/hermes-images.json')))


def production_images():
    return check_release(read_json(production_file()))

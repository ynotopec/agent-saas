#!/bin/bash
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

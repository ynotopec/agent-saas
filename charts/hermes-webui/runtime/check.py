import os, pathlib, platform, sys, urllib.request
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

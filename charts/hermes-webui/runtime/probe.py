import urllib.request
from run_agent import AIAgent
assert AIAgent is not None
with urllib.request.urlopen("http://127.0.0.1:8787/health", timeout=3) as response:
    assert response.status == 200

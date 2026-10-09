"""Executed inside WebUI by hermes-release validate; performs two real LLM turns.

Uses container credentials without printing them. Leaves a named canary session
for inspection. This is an explicit release check, never a readiness probe.
"""
import http.cookies
import json
import os
import subprocess
import time
import urllib.parse
import urllib.request
from run_agent import AIAgent
from api.auth import csrf_token_for_session

assert AIAgent is not None
assert subprocess.check_output(['/bin/sh', '-c', 'printf hermes-canary'], text=True) == 'hermes-canary'
webui = 'http://127.0.0.1:8787'
with urllib.request.urlopen(webui + '/health', timeout=5) as response:
    assert response.status == 200
key = os.environ.get('HERMES_WEBUI_GATEWAY_API_KEY', '').strip()
password = os.environ.get('HERMES_WEBUI_PASSWORD', '')
if not key or not password:
    raise RuntimeError('Canary requires gateway key and WebUI password in container env')
gateway = (os.environ.get('HERMES_WEBUI_GATEWAY_BASE_URL') or os.environ.get('HERMES_API_URL') or 'http://127.0.0.1:8642').rstrip('/')
prompt = 'Reply with exactly PONG. Do not use tools.'

def post(url, body, headers=None):
    request = urllib.request.Request(url, data=json.dumps(body).encode(),
                                     headers={'Content-Type': 'application/json', **(headers or {})})
    return urllib.request.urlopen(request, timeout=180)

with post(gateway + '/v1/chat/completions', {
    'messages': [{'role': 'user', 'content': prompt}], 'stream': False},
    {'Authorization': 'Bearer ' + key}) as response:
    result = json.load(response)
assert result['choices'][0]['message']['content'].strip() == 'PONG', 'Gateway chat did not return PONG'
print('Gateway authenticated chat PONG OK')

with post(webui + '/api/auth/login', {'password': password}) as response:
    assert json.load(response).get('ok') is True
    cookie = http.cookies.SimpleCookie()
    for value in response.headers.get_all('Set-Cookie', []):
        cookie.load(value)
name = os.environ.get('HERMES_WEBUI_COOKIE_NAME', 'hermes_session')
if name not in cookie:
    raise RuntimeError('Login did not provide an authenticated WebUI session')
value = cookie[name].value
headers = {'Cookie': name + '=' + value, 'X-Hermes-CSRF-Token': csrf_token_for_session(value) or ''}
with post(webui + '/api/session/new', {'title': 'Runtime release canary', 'worktree': False}, headers) as response:
    session = json.load(response)['session']
with post(webui + '/api/chat/start', {'session_id': session['session_id'], 'message': prompt}, headers) as response:
    stream = json.load(response)['stream_id']
request = urllib.request.Request(webui + '/api/chat/stream?' + urllib.parse.urlencode({'stream_id': stream}), headers=headers)
deadline = time.monotonic() + 180
with urllib.request.urlopen(request, timeout=180) as response:
    event = ''
    for line in response:
        if time.monotonic() > deadline:
            raise TimeoutError('WebUI chat deadline exceeded')
        text = line.decode().strip()
        if text.startswith('event:'):
            event = text[6:].strip()
        elif text.startswith('data:'):
            data = json.loads(text[5:])
            if event in ('apperror', 'error', 'cancel'):
                raise RuntimeError('WebUI chat failed (' + event + ')')
            if event == 'done':
                messages = data.get('session', {}).get('messages', [])
                replies = [m.get('content', '') for m in messages if m.get('role') == 'assistant']
                assert replies and replies[-1].strip() == 'PONG', 'WebUI chat did not return PONG'
                break
    else:
        raise RuntimeError('WebUI chat ended without a completed reply')
print('AIAgent + health + shell + gateway chat + WebUI chat OK')

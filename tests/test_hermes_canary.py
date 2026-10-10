"""Exercise the canary HTTP contract, including the user's health-green/chat-401 case."""
import contextlib
import http.server
import json
from pathlib import Path
import sys
import threading
import types
import urllib.error
import pytest


@pytest.mark.parametrize('failure', [None, 'gateway-key', 'webui-chat', 'missing-reply', 'missing-password'])
def test_canary_checks_authenticated_chats(monkeypatch, failure):
    calls = []

    class Handler(http.server.BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def send(self, data, status=200, cookie=False, sse=False):
            self.send_response(status)
            if cookie:
                self.send_header('Set-Cookie', 'hermes_session=canary-cookie; HttpOnly')
            self.end_headers()
            self.wfile.write(data.encode() if sse else json.dumps(data).encode())

        def do_GET(self):
            calls.append(self.path)
            if self.path == '/health':
                self.send({'ok': True}); return
            assert self.headers['Cookie'] == 'hermes_session=canary-cookie'
            if failure == 'webui-chat':
                self.send('event: apperror\ndata: {"message":"Invalid gateway API key"}\n\n', sse=True)
            else:
                messages = [] if failure == 'missing-reply' else [{'role': 'assistant', 'content': 'PONG'}]
                self.send('event: done\ndata: ' + json.dumps({'session': {'messages': messages}}) + '\n\n', sse=True)

        def do_POST(self):
            calls.append(self.path)
            body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
            if self.path == '/v1/chat/completions':
                assert self.headers['Authorization'] == 'Bearer container-key'
                if failure == 'gateway-key':
                    self.send({'error': 'Invalid gateway API key'}, 401); return
                self.send({'choices': [{'message': {'content': 'PONG'}}]}); return
            if self.path == '/api/auth/login':
                assert body['password'] == 'container-password'
                self.send({'ok': True}, cookie=True); return
            assert self.headers['Cookie'] == 'hermes_session=canary-cookie'
            assert self.headers['X-Hermes-CSRF-Token'] == 'csrf-token'
            if self.path == '/api/session/new':
                self.send({'session': {'session_id': 'canary-session'}})
            elif self.path == '/api/chat/start':
                assert body['session_id'] == 'canary-session'
                self.send({'stream_id': 'canary-stream'})
            else:
                self.send({}, 404)

    agent = types.ModuleType('run_agent'); agent.AIAgent = object
    auth = types.ModuleType('api.auth'); auth.csrf_token_for_session = lambda cookie: 'csrf-token'
    monkeypatch.setitem(sys.modules, 'run_agent', agent)
    monkeypatch.setitem(sys.modules, 'api.auth', auth)
    monkeypatch.setenv('HERMES_WEBUI_GATEWAY_API_KEY', 'container-key')
    monkeypatch.setenv('HERMES_WEBUI_PASSWORD', '' if failure == 'missing-password' else 'container-password')
    monkeypatch.delenv('HERMES_WEBUI_COOKIE_NAME', raising=False)
    with http.server.ThreadingHTTPServer(('127.0.0.1', 0), Handler) as server:
        thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
        url = 'http://127.0.0.1:' + str(server.server_port)
        monkeypatch.setenv('HERMES_WEBUI_GATEWAY_BASE_URL', url)
        script = (Path(__file__).parents[1] / 'scripts/hermes-canary.py').read_text().replace('http://127.0.0.1:8787', url)
        expected = contextlib.nullcontext() if failure is None else pytest.raises((urllib.error.HTTPError, RuntimeError, AssertionError))
        try:
            with expected:
                exec(compile(script, 'hermes-canary.py', 'exec'), {})
        finally:
            server.shutdown(); thread.join()
    if failure == 'missing-password':
        assert calls == ['/health']
        return
    assert '/v1/chat/completions' in calls
    if failure != 'gateway-key':
        assert '/api/chat/start' in calls and '/api/chat/stream?stream_id=canary-stream' in calls

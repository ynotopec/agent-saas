"""Run the actual dashboard JavaScript against a small DOM/fetch harness."""
import json
import re
import shutil
import subprocess
import pytest
from app import CONTENT


def test_deploy_token_field_removed():
    """The create form must not expose a deployment-token field anymore."""
    assert 'deploy-token' not in CONTENT
    assert 'Jeton de déploiement' not in CONTENT
    assert 'X-Deploy-Token' not in CONTENT


def test_password_fields_mandatory():
    """Both password fields are always visible, required, and not hidden."""
    group = re.search(r'<div class="form-group" id="password-confirmation-group"[^>]*>', CONTENT).group(0)
    assert 'hidden' not in group
    assert 'id="instance-password"' in CONTENT and 'required' in re.search(
        r'<input[^>]*id="instance-password"[^>]*>', CONTENT).group(0)
    assert 'required' in re.search(
        r'<input[^>]*id="password-confirmation"[^>]*>', CONTENT).group(0)


def test_mandatory_password_form_flow():
    node = shutil.which('node')
    if not node:
        pytest.skip('Node is required for the dashboard form check')
    script = re.search(r'<script>(.*?)</script>', CONTENT, re.S).group(1)
    harness = r'''
const vm = require('node:vm');
const assert = require('node:assert/strict');
const elements = {};
const calls = [];
const document = {getElementById(id) {
  return elements[id] ||= {value: '', type: 'password', hidden: false,
    listeners: {}, attributes: {}, validity: '', reported: false,
    addEventListener(name, fn) { this.listeners[name] = fn; },
    setAttribute(name, value) { this.attributes[name] = value; },
    setCustomValidity(value) { this.validity = value; },
    reportValidity() { this.reported = true; }};
}};
const context = vm.createContext({document, console, assert, elements, calls,
  fetch: async (url, options) => {
    if (url === '/api/instances') return {json: async () => []};
    calls.push(JSON.parse(options.body));
    return {status: 200, json: async () => ({success: true, url: 'https://agent.example'})};
  }
});
vm.runInContext(SCRIPT, context);
vm.runInContext(`(async () => {
  const password = elements['instance-password'];
  const confirmation = elements['password-confirmation'];
  // Empty password: deploy must be blocked, nothing sent to the API.
  document.getElementById('subdomain').value = 'default';
  await deploy({preventDefault() {}});
  assert.equal(calls.length, 0);
  assert.equal(password.reported, true);
  // Mismatched confirmation: still blocked.
  password.value = 'chosen-password';
  password.listeners.input();
  confirmation.value = 'different-password';
  confirmation.listeners.input();
  await deploy({preventDefault() {}});
  assert.equal(calls.length, 0);
  assert.equal(confirmation.reported, true);
  // Toggle reveals both fields.
  elements['toggle-password'].listeners.click();
  assert.equal(password.type, 'text');
  assert.equal(confirmation.type, 'text');
  // Matching password: request is sent with both fields.
  confirmation.value = 'chosen-password';
  confirmation.listeners.input();
  assert.equal(confirmation.validity, '');
  document.getElementById('subdomain').value = 'custom';
  await deploy({preventDefault() {}});
  assert.equal(calls.length, 1);
  assert.equal(calls[0].password, 'chosen-password');
  assert.equal(calls[0].password_confirmation, 'chosen-password');
  assert.equal(password.value, '');
  assert.equal(confirmation.value, '');
  assert.equal(password.type, 'password');
})().catch(error => { console.error(error); process.exitCode = 1; });`, context);
'''
    harness = harness.replace('vm.runInContext(SCRIPT, context);', 'vm.runInContext(' + json.dumps(script) + ', context);')
    harness = harness.replace('document, console, assert, elements, calls,', 'document, console, assert, elements, calls, process,')
    result = subprocess.run([node, '-e', harness], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr

"""Run the actual dashboard JavaScript against a small DOM/fetch harness."""
import json
import re
import shutil
import subprocess
import pytest
from app import CONTENT


def test_optional_password_form_flow():
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
    listeners: {}, attributes: {}, validity: '',
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
  const group = elements['password-confirmation-group'];
  assert.equal(group.hidden, true);
  document.getElementById('subdomain').value = 'default';
  await deploy({preventDefault() {}});
  assert.equal(calls.length, 1);
  assert.equal(JSON.stringify(calls[0]), JSON.stringify({subdomain: 'default'}));
  password.value = 'chosen-password';
  password.listeners.input();
  assert.equal(group.hidden, false);
  assert.equal(confirmation.required, true);
  confirmation.value = 'different-password';
  confirmation.listeners.input();
  await deploy({preventDefault() {}});
  assert.equal(calls.length, 1);
  assert.equal(confirmation.reported, true);
  elements['toggle-password'].listeners.click();
  assert.equal(password.type, 'text');
  assert.equal(confirmation.type, 'text');
  confirmation.value = 'chosen-password';
  confirmation.listeners.input();
  assert.equal(confirmation.validity, '');
  document.getElementById('subdomain').value = 'custom';
  await deploy({preventDefault() {}});
  assert.equal(calls.length, 2);
  assert.equal(calls[1].password, 'chosen-password');
  assert.equal(calls[1].password_confirmation, 'chosen-password');
  assert.equal(password.value, '');
  assert.equal(confirmation.value, '');
  assert.equal(password.type, 'password');
  assert.equal(group.hidden, true);
  assert.equal(confirmation.required, false);
})().catch(error => { console.error(error); process.exitCode = 1; });`, context);
'''
    # Pass only a non-sensitive script; the default password never enters HTML.
    harness = harness.replace('vm.runInContext(SCRIPT, context);', 'vm.runInContext(' + json.dumps(script) + ', context);')
    harness = harness.replace('document, console, assert, elements, calls,', 'document, console, assert, elements, calls, process,')
    result = subprocess.run([node, '-e', harness], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr

#!/usr/bin/env python3
"""Check fixed images, export Helm values or validate a deployed canary."""
import argparse
import json
from pathlib import Path
import subprocess
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from release_policy import check_release, delay_days, production_file, read_json

CANARY_CHECK = '''from run_agent import AIAgent
import subprocess, urllib.request
assert AIAgent is not None
with urllib.request.urlopen("http://127.0.0.1:8787/health", timeout=5) as response:
    assert response.status == 200
assert subprocess.check_output(["/bin/sh", "-c", "printf hermes-canary"], text=True) == "hermes-canary"
print("AIAgent + health + shell OK")
'''


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=('check', 'helm-values', 'validate'))
    parser.add_argument('--release', default=str(production_file()))
    parser.add_argument('--delay-days', default=None)
    parser.add_argument('--namespace', default='demo1')
    parser.add_argument('--deployment')
    args = parser.parse_args(argv)
    record = read_json(args.release)
    agent, webui = check_release(record, args.delay_days)
    if args.command == 'helm-values':
        arepo, adigest = agent.split('@')
        wrepo, wdigest = webui.split('@')
        print(json.dumps({'agentImage': {'repository': arepo, 'digest': adigest},
                          'image': {'repository': wrepo, 'digest': wdigest},
                          'release': record, 'releaseDelayDays': delay_days(args.delay_days)}, indent=2))
    elif args.command == 'check':
        print(agent + '\n' + webui)
    else:
        if not args.deployment:
            parser.error('validate requires --deployment')
        base = ['kubectl', '-n', args.namespace]
        obj = json.loads(subprocess.check_output(base + ['get', 'deployment', args.deployment, '-o', 'json'], text=True))
        images = {c['name']: c['image'] for c in obj['spec']['template']['spec']['containers']}
        if images.get('hermes-agent') != agent or images.get('hermes-webui') != webui:
            raise ValueError('Canary is not using the exact pinned pair')
        status = obj.get('status', {})
        replicas = obj['spec'].get('replicas', 1)
        if replicas < 1 or status.get('availableReplicas', 0) != replicas or status.get('updatedReplicas', 0) != replicas or status.get('observedGeneration', 0) < obj['metadata']['generation']:
            raise ValueError('Canary rollout is not available and up to date')
        subprocess.check_call(base + ['exec', 'deployment/' + args.deployment, '-c', 'hermes-webui', '--',
                                     '/app/venv/bin/python', '-c', CANARY_CHECK])


if __name__ == '__main__':
    try:
        main()
    except (ValueError, OSError, KeyError, subprocess.SubprocessError) as exc:
        print('Release check failed: ' + str(exc), file=sys.stderr)
        sys.exit(1)

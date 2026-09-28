#!/usr/bin/env python3
"""Deterministic OpenStack CLI stand-in for the NixOS integration test."""
import json
import sys
import time
from pathlib import Path

args = sys.argv[1:]
assert args[:2] == ['--os-compute-api-version', '2.30'], args
args = args[2:]
with Path('/tmp/openstack-calls.jsonl').open('a') as log:
    log.write(json.dumps(args) + '\n')
state_file = Path('/tmp/openstack-hosts.json')
state = json.loads(state_file.read_text()) if state_file.exists() else {}
if args[:3] == ['compute', 'service', 'list']:
    print(json.dumps([{'Host': host, 'Status': 'enabled', 'State': health}
                      for host, health in [('compute-a', 'up'), ('compute-b', 'up'), ('compute-down', 'down')]]))
elif args[:2] == ['server', 'show']:
    print(json.dumps({'OS-EXT-SRV-ATTR:host': state.get(args[2], 'compute-a'),
                      'status': 'ACTIVE', 'OS-EXT-STS:task_state': None}))
elif args[:2] == ['server', 'migrate']:
    assert '--live-migration' in args and '--wait' in args, args
    target = args[args.index('--host') + 1]
    assert target in ('compute-a', 'compute-b') and target != state.get(args[-1], 'compute-a')
    time.sleep(8)
    if Path('/tmp/openstack-fail').exists():
        print('No valid host found', file=sys.stderr)
        sys.exit(1)
    state[args[-1]] = target
    state_file.write_text(json.dumps(state))
else:
    raise AssertionError(args)

#!/usr/bin/env python3
"""Verify gateway migration using real, uniquely named Docker network fixtures."""
from __future__ import annotations
import argparse
import json
import os
from pathlib import Path
import sys
import tempfile
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from scripts.migrate_hub import Docker
from scripts import migrate_hub_proxy as proxy


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', required=True, type=Path)
    args = parser.parse_args()
    output = args.output.resolve()
    docker = Docker()
    prefix = 'codepier-proxy-fixture-' + uuid.uuid4().hex[:12]
    old_name, new_name = prefix + '-old', prefix + '-new'
    original_cwd = Path.cwd()
    try:
        for name, project in ((old_name, prefix), (new_name, 'codepier')):
            docker.run(['network', 'create', '--label', 'com.docker.compose.project=' + project,
                        '--label', 'com.docker.compose.network=default', name])
        old, new = [docker.json(['network', 'inspect', name])[0]['IPAM']['Config'][0]['Gateway']
                    for name in (old_name, new_name)]
        assert old != new
        with tempfile.TemporaryDirectory(prefix=prefix) as directory:
            root = Path(directory)
            (root / '.env').write_text('FORWARDED_ALLOW_IPS="127.0.0.1,' + old + ',10.0.0.8"\n')
            (root / '.env').chmod(0o600)
            (root / 'compose.yml').write_text('name: codepier\nservices:\n  hub:\n    image: python:3.13-slim-bookworm\n    environment:\n      FORWARDED_ALLOW_IPS: "${FORWARDED_ALLOW_IPS:-127.0.0.1}"\nnetworks:\n  default:\n    name: ' + new_name + '\n')
            os.chdir(root)
            config = docker.json(['compose', 'config', '--format', 'json'])
            container = {'Config': {'Labels': {'com.docker.compose.service': 'hub'}},
                         'NetworkSettings': {'Networks': {old_name: {'Gateway': old}}}}
            plans = proxy.preflight(config, [container], docker, prefix)
            assert len(plans) == 1
            # The persisted plan must still work after retirement/interruption.
            docker.run(['network', 'rm', old_name])
            result = proxy.apply(root, json.loads(json.dumps(plans)), docker)
            expected = '127.0.0.1,' + new + ',10.0.0.8'
            assert result['current'] == expected
            assert proxy.trust(docker.json(['compose', 'config', '--format', 'json'])) == expected
            assert proxy.apply(root, plans, docker) == {'changed': False}
            report = {'status': 'passed', 'scope': 'unique disposable networks; no production services',
                      'old_gateway': old, 'new_gateway': new, 'compose_env_verified': True,
                      'retired_network_resume_verified': True, 'idempotent': True}
    finally:
        os.chdir(original_cwd)
        for name in (old_name, new_name):
            assert name.startswith(prefix)
            docker.run(['network', 'rm', name], check=False)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, indent=2) + '\n')
    print(json.dumps(report))


if __name__ == '__main__':
    main()

#!/usr/bin/env python3
"""Record deterministic public source build inputs; no network or publication."""
from __future__ import annotations
import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess

ROOT = Path(__file__).resolve().parents[1]


def source_build_record(root, directory, context):
    root, directory = Path(root), Path(directory)
    commit = subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=root, text=True).strip()
    if commit != context['GITHUB_SHA']:
        raise ValueError('Build checkout does not match the workflow commit')
    subprocess.run(['git', 'diff', '--exit-code', 'HEAD', '--'], cwd=root, check=True)
    check = json.loads((directory / 'public-check.json').read_text())
    panel_check = json.loads((directory / 'panel-update-check.json').read_text())
    if check.get('passed') is not True or panel_check.get('passed') is not True:
        raise ValueError('Both public archive checks must pass')
    version = check['version']
    if panel_check.get('version') != version or panel_check.get('inventory_sha256') != check['inventory_sha256']:
        raise ValueError('Source and panel archive checks do not describe the same source')
    expected = {f'codepier-{version}-source{suffix}' for suffix in ('-full.zip', '-full.manifest.json', '.zip', '.manifest.json')}
    assets = {}
    for path in sorted(directory.glob('codepier-*-source*')):
        if path.suffix not in {'.json', '.zip'}:
            continue
        if path.is_symlink() or not path.is_file():
            raise ValueError('Source assets must be regular files')
        raw = path.read_bytes()
        assets[path.name] = {'sha256': hashlib.sha256(raw).hexdigest(), 'bytes': len(raw)}
    if set(assets) != expected:
        raise ValueError('Exactly four source release assets are required')
    return {'schema': 1, 'repository': context['GITHUB_REPOSITORY'], 'commit': commit,
            'tree': subprocess.check_output(['git', 'rev-parse', 'HEAD^{tree}'], cwd=root, text=True).strip(),
            'run_id': int(context['GITHUB_RUN_ID']), 'run_attempt': int(context['GITHUB_RUN_ATTEMPT']),
            'event': context['GITHUB_EVENT_NAME'], 'workflow_path': '.github/workflows/ci.yml',
            'workflow_sha256': hashlib.sha256((root / '.github/workflows/ci.yml').read_bytes()).hexdigest(),
            'source_inventory_sha256': check['inventory_sha256'], 'assets': assets}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--directory', type=Path, default=ROOT / 'dist')
    args = parser.parse_args()
    record = source_build_record(ROOT, args.directory, os.environ)
    (args.directory / 'source-build.json').write_text(json.dumps(record, sort_keys=True, indent=2) + '\n')
    print(json.dumps(record, sort_keys=True))


if __name__ == '__main__':
    main()

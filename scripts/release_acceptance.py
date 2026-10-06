#!/usr/bin/env python3
"""Bind exhaustive regression evidence to a committed, verified public ZIP."""
from __future__ import annotations
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import sys
import tempfile
import zipfile

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from scripts.check_release import check_archive, check_source
from scripts.regression_plan import fingerprint, merge_summaries


def write_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, sort_keys=True, indent=2) + '\n')


def bind_commit(root, commit, files):
    if not re.fullmatch(r'[a-f0-9]{40}|[a-f0-9]{64}', commit):
        raise ValueError('An exact commit is required')
    actual = subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=root, text=True).strip()
    if actual != commit:
        raise ValueError('Checkout does not match the expected commit')
    subprocess.run(['git', 'diff', '--exit-code', 'HEAD', '--'], cwd=root, check=True,
                   stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    tracked = set(subprocess.check_output(['git', 'ls-tree', '-r', '--name-only', '-z', 'HEAD'],
                                         cwd=root).decode().split('\0'))
    if not set(files) <= tracked:
        raise ValueError('Public source includes uncommitted files')


def extract_verified(archive, destination, files):
    archive, destination = Path(archive).resolve(), Path(destination).resolve()
    checked = check_archive(archive, files)
    if destination.exists():
        raise ValueError('Acceptance destination must not exist')
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='codepier-public-', dir=destination.parent) as temporary:
        stage = Path(temporary) / 'source'
        stage.mkdir()
        with zipfile.ZipFile(archive) as bundle:
            for name, expected in files.items():
                path = stage / name
                if not path.resolve().is_relative_to(stage):
                    raise ValueError('Public source path escapes destination')
                path.parent.mkdir(parents=True, exist_ok=True)
                with bundle.open(name) as source:
                    raw = source.read(expected['bytes'] + 1)
                if len(raw) != expected['bytes'] or hashlib.sha256(raw).hexdigest() != expected['sha256']:
                    raise ValueError('Archive changed while extracting')
                path.write_bytes(raw)
                path.chmod(expected['mode'])
            manifest = ''.join(item['sha256'] + '  ' + name + '\n' for name, item in sorted(files.items()))
            manifest_path = stage / 'MANIFEST.sha256'
            manifest_path.write_text(manifest)
            manifest_mode = stat.S_IMODE(bundle.getinfo('MANIFEST.sha256').external_attr >> 16)
            manifest_path.chmod(manifest_mode)
            checked['manifest_sha256'] = hashlib.sha256(manifest.encode()).hexdigest()
            checked['manifest_mode'] = manifest_mode
        _, extracted = check_source(stage)
        if extracted != files or check_archive(archive, files)['sha256'] != checked['sha256']:
            raise ValueError('Extracted source or archive changed')
        os.replace(stage, destination)
    return checked


def prepare(root, archive, destination, proof, commit):
    root, destination = Path(root).resolve(), Path(destination).resolve()
    if destination == root or destination.is_relative_to(root):
        raise ValueError('Acceptance source must be isolated from the checkout')
    report, files = check_source(root)
    bind_commit(root, commit, files)
    checked = extract_verified(archive, destination, files)
    record = {'schema': 1, 'commit': commit, 'archive_sha256': checked['sha256'],
              'archive_bytes': checked['bytes'], 'manifest_sha256': checked['manifest_sha256'],
              'manifest_mode': checked['manifest_mode'], 'source_inventory_sha256': report['inventory_sha256'],
              'files': files, 'commit_binding_verified': True, 'post_regression_source_verified': False}
    write_json(proof, record)
    return record


def finish(root, archive, proof):
    record = json.loads(Path(proof).read_text())
    record['post_regression_source_verified'] = False
    write_json(proof, record)
    root = Path(root).resolve()
    report, files = check_source(root)
    manifest = root / 'MANIFEST.sha256'
    if (manifest.is_symlink() or hashlib.sha256(manifest.read_bytes()).hexdigest() != record['manifest_sha256']
            or stat.S_IMODE(manifest.stat().st_mode) != record['manifest_mode']):
        raise ValueError('Extracted manifest changed during regression')
    if files != record['files'] or report['inventory_sha256'] != record['source_inventory_sha256']:
        raise ValueError('Public source changed during regression')
    if check_archive(Path(archive), files)['sha256'] != record['archive_sha256']:
        raise ValueError('Acceptance archive changed')
    record['post_regression_source_verified'] = True
    write_json(proof, record)
    return record


def verify_reports(reports, commit):
    if len(reports) != 4:
        raise ValueError('All four public-archive shards are required')
    summaries, proofs = [], []
    for report in map(Path, reports):
        summary = json.loads(report.read_text())
        proof = json.loads((report.parent / 'archive-proof.json').read_text())
        before = json.loads((report.parent / 'source-before.json').read_text())
        after = json.loads((report.parent / 'source-after.json').read_text())
        if (proof.get('commit') != commit or proof.get('commit_binding_verified') is not True
                or proof.get('post_regression_source_verified') is not True):
            raise ValueError('Missing or stale commit/archive verification')
        if before != after or fingerprint(before) != summary['source_inventory_sha256']:
            raise ValueError('Regression source fingerprint differs')
        if any(name not in proof['files'] or proof['files'][name]['sha256'] != digest
               for name, digest in before.items()):
            raise ValueError('Regression did not use the verified public source')
        summaries.append(summary)
        proofs.append(proof)
    merge_summaries(summaries)
    first = proofs[0]
    if any(proof != first for proof in proofs[1:]):
        raise ValueError('Shards tested different public archives')
    return {'verified': True, 'commit': commit, 'archive_sha256': first['archive_sha256'],
            'source_inventory_sha256': first['source_inventory_sha256'],
            'public_files': len(first['files']), 'shards': 4}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='command', required=True)
    for name in ('prepare', 'finish'):
        command = commands.add_parser(name)
        command.add_argument('--root', type=Path, required=True)
        command.add_argument('--archive', type=Path, required=True)
        command.add_argument('--proof', type=Path, required=True)
        if name == 'prepare':
            command.add_argument('--destination', type=Path, required=True)
            command.add_argument('--commit', required=True)
    command = commands.add_parser('verify')
    command.add_argument('--commit', required=True)
    command.add_argument('--output', type=Path, required=True)
    command.add_argument('reports', nargs='+', type=Path)
    args = parser.parse_args()
    if args.command == 'prepare':
        result = prepare(args.root, args.archive, args.destination, args.proof, args.commit)
    elif args.command == 'finish':
        result = finish(args.root, args.archive, args.proof)
    else:
        result = verify_reports(args.reports, args.commit)
        write_json(args.output, result)
    print(json.dumps({key: value for key, value in result.items() if key != 'files'}, sort_keys=True))


if __name__ == '__main__':
    main()

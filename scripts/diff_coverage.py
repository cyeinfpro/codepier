#!/usr/bin/env python3
"""Report coverage of changed Python statements; never invent a global threshold."""
from __future__ import annotations
import argparse
import json
from pathlib import Path, PurePosixPath
import re
import subprocess

ROOT = Path(__file__).resolve().parents[1]
SOURCES = {'agent', 'hub', 'shared', 'scripts'}


def changed_lines(diff):
    result, name = {}, None
    for line in diff.splitlines():
        if line.startswith('+++ '):
            name = line[4:]
            if name.startswith('"'):
                name = json.loads(name)
            if not name.startswith('b/'):
                name = None;continue
            name = name[2:]
            path = PurePosixPath(name)
            if not path.parts or path.parts[0] not in SOURCES or path.suffix != '.py' or '..' in path.parts:
                name = None;continue
            result.setdefault(name, set())
        elif name and (match := re.match(r'@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@', line)):
            start, count = int(match[1]), int(match[2] or 1)
            if count > 1000000:
                raise ValueError('Unexpected diff hunk size')
            result[name].update(range(start, start + count))
    return result


def report(changed, coverage):
    files, unmeasured = {}, []
    for name, lines in sorted(changed.items()):
        if not lines:
            continue
        entry = coverage.get('files', {}).get(name)
        if entry is None:
            unmeasured.append(name);continue
        executed = set(entry['executed_lines'])
        missing = set(entry['missing_lines'])
        relevant = set(lines) & (executed | missing) - set(entry.get('excluded_lines', []))
        files[name] = {'statements': len(relevant), 'covered': len(relevant & executed),
                       'missing_lines': sorted(relevant - executed)}
    count = sum(item['statements'] for item in files.values())
    passed = sum(item['covered'] for item in files.values())
    return {'policy': 'report-only; changed executable Python statements, not branch or whole-repository coverage',
            'statements': count, 'covered': passed, 'percent': round(100 * passed / count, 2) if count else None,
            'measurement_complete': not unmeasured, 'unmeasured_files': unmeasured, 'files': files}


def git_changes(root, base):
    root = Path(root)
    resolved = subprocess.check_output(['git', 'rev-parse', '--verify', '--end-of-options', base + '^{commit}'], cwd=root, text=True).strip()
    if not re.fullmatch(r'[a-f0-9]{40,64}', resolved):
        raise ValueError('Unrecognized Git base commit')
    diff = subprocess.check_output(['git', '-c', 'core.quotePath=false', 'diff', '--no-ext-diff', '--no-textconv',
        '--no-renames', '--unified=0', resolved, '--', *sorted(SOURCES)], cwd=root, text=True)
    changed = changed_lines(diff)
    untracked = subprocess.check_output(['git', 'ls-files', '--others', '--exclude-standard', '-z', '--', *sorted(SOURCES)], cwd=root).decode().split('\0')
    for name in filter(None, untracked):
        path = root / name
        if path.suffix == '.py' and not path.is_symlink():
            if path.stat().st_size > 8 * 1024 * 1024:
                raise ValueError('Untracked Python source exceeds the review budget')
            changed[name] = set(range(1, len(path.read_text(encoding='utf-8').splitlines()) + 1))
    return resolved, changed


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--coverage', type=Path, required=True)
    parser.add_argument('--base', required=True, help='Exact PR/push base commit; use HEAD for an uncommitted local review')
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    if args.coverage.is_symlink() or not args.coverage.is_file() or args.coverage.stat().st_size > 128 * 1024 * 1024:
        parser.error('Coverage JSON must be a bounded regular file')
    base, changed = git_changes(ROOT, args.base)
    result = {'base_commit': base, **report(changed, json.loads(args.coverage.read_text(encoding='utf-8')))}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2) + '\n', encoding='utf-8')
    print(json.dumps({key: value for key, value in result.items() if key != 'files'}, indent=2))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())

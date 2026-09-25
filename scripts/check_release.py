#!/usr/bin/env python3
"""Validate public release inputs and an optional source archive, without publishing.

This checks consistency, bounded source selection, obvious credential literals,
and ZIP integrity. It is not a guarantee that arbitrary source text is secret-free.
"""
from __future__ import annotations

import argparse
import ast
import hashlib
from html.parser import HTMLParser
from urllib.parse import urlsplit, parse_qs
import json
import os
from pathlib import Path
import re
import stat
import runpy
import tempfile
import sys
import zipfile

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from scripts.build_source_bundle import DIRECTORIES, EXCLUDED_NAMES, REQUIRED_FILES, PUBLIC_DOCS, PANEL_UPDATE_EXCLUDES, include


CREDENTIAL_PATTERNS = {
    'private-key': re.compile(r'-----BEGIN (?:RSA |EC |OPENSSH |DSA )?PRIVATE KEY-----'),
    'github-token': re.compile(r'\b(?:gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{50,})\b'),
    'aws-access-id': re.compile(r'\b(?:AKIA|ASIA)[A-Z0-9]{16}\b'),
    'api-secret': re.compile(r'\bsk-(?:proj-|svcacct-)?[A-Za-z0-9_-]{32,}\b'),
}
TEXT_SUFFIXES = {'.py', '.js', '.mjs', '.css', '.html', '.json', '.md', '.txt',
                 '.sh', '.ps1', '.cmd', '.yml', '.yaml', '.toml', '.example', '.in'}


def public_files(root=ROOT):
    """Return the complete selected public input inventory; fail on unsafe files."""
    files = {}
    for current, directories, names in os.walk(root, followlinks=False):
        parent = Path(current)
        kept = []
        for name in sorted(directories):
            if name in EXCLUDED_NAMES or name.startswith('.venv'):
                continue
            if parent == root and name not in DIRECTORIES:
                continue
            child = parent / name
            relative_directory = child.relative_to(root).as_posix()
            if relative_directory.startswith('docs/') and not any(
                document.startswith(relative_directory + '/') for document in PUBLIC_DOCS):
                continue
            if child.is_symlink():
                raise ValueError('Source directory is a symlink: ' + child.relative_to(root).as_posix())
            kept.append(name)
        directories[:] = kept
        for name in sorted(names):
            path = parent / name
            relative = path.relative_to(root)
            if not include(relative, public=True):
                continue
            info = path.lstat()
            if not stat.S_ISREG(info.st_mode) or info.st_size > 8 * 1024 * 1024:
                raise ValueError('Public source is not a bounded regular file: ' + relative.as_posix())
            raw = path.read_bytes()
            files[relative.as_posix()] = {'sha256': hashlib.sha256(raw).hexdigest(),
                                        'bytes': len(raw), 'mode': stat.S_IMODE(info.st_mode)}
    return files


def version(root):
    tree = ast.parse((root / 'shared/util.py').read_text(encoding='utf-8'))
    for item in tree.body:
        if isinstance(item, ast.Assign) and any(isinstance(t, ast.Name) and t.id == 'VERSION' for t in item.targets):
            value = ast.literal_eval(item.value)
            if isinstance(value, str) and re.fullmatch(r'\d+\.\d+\.\d+(?:-[a-zA-Z0-9.-]+)?', value):
                return value
    raise ValueError('Missing or invalid source VERSION')


def check_compose_image(root, current_version):
    text = (root / 'compose.yml').read_text(encoding='utf-8')
    images = re.findall(r'^    image:\s*([^\n]+)$', text, re.M)
    expected = 'codepier:' + current_version
    allowed = {expected, '"' + expected + '"', "'" + expected + "'",
               '"${CODEPIER_HUB_IMAGE:-' + expected + '}"'}
    if len(images) != 1 or images[0].strip() not in allowed:
        raise ValueError('Compose image version differs from source VERSION')


def check_web_assets(root, current_version):
    """Validate real asset URLs, not obsolete per-feature cache strings."""
    class Assets(HTMLParser):
        def __init__(self):
            super().__init__()
            self.urls = []
        def handle_starttag(self, tag, attrs):
            values = dict(attrs)
            if tag == 'script' and values.get('src'):
                self.urls.append(values['src'])
            elif tag == 'link' and values.get('rel') in {'stylesheet', 'icon'} and values.get('href'):
                self.urls.append(values['href'])
    parser = Assets()
    parser.feed((root / 'web/index.html').read_text(encoding='utf-8'))
    assets = {}
    for url in parser.urls:
        parsed = urlsplit(url)
        if parsed.scheme or parsed.netloc or not parsed.path.startswith('/static/') or parsed.fragment:
            raise ValueError('Unexpected external or non-static asset: ' + parsed.path)
        relative = Path(parsed.path.removeprefix('/static/'))
        path = root / 'web' / relative
        if relative.is_absolute() or '..' in relative.parts or '%' in str(relative) or path.is_symlink() or not path.is_file():
            raise ValueError('Missing or unsafe browser asset: ' + relative.as_posix())
        if relative.as_posix() in assets:
            raise ValueError('Duplicate browser asset: ' + relative.as_posix())
        if relative.parts[0] == 'vendor':
            if len(relative.parts) < 3 or not re.search(r'-\d+\.\d+\.\d+$', relative.parts[1]):
                raise ValueError('Vendor asset has no explicit package version: ' + relative.as_posix())
        elif parse_qs(parsed.query, keep_blank_values=True).get('v') != ['codepier-' + current_version]:
            raise ValueError('Browser cache version differs from source VERSION: ' + relative.as_posix())
        assets[relative.as_posix()] = url
    if not assets:
        raise ValueError('No browser assets selected by index.html')
    return assets


def check_source(root=ROOT):
    files = public_files(root)
    required = REQUIRED_FILES - {'LOCAL_RELEASE.json'}
    required |= PUBLIC_DOCS
    required |= {'README.md', 'LICENSE', 'SECURITY.md', 'CONTRIBUTING.md', 'CHANGELOG.md',
                 'RELEASE.json', '.github/workflows/ci.yml', 'docs/RELEASING.md'}
    missing = sorted(required - files.keys())
    if missing:
        raise ValueError('Missing public release inputs: ' + ', '.join(missing))
    release = json.loads((root / 'RELEASE.json').read_text(encoding='utf-8'))
    current_version = version(root)
    if release.get('name') != 'CodePier' or release.get('version') != current_version:
        raise ValueError('RELEASE.json does not match CodePier source identity')
    check_compose_image(root, current_version)
    browser_assets = check_web_assets(root, current_version)
    from scripts.release_policy import check_generated_assets, check_public_links, check_supported_branch, check_readme_version
    check_generated_assets(root)
    check_public_links(root, files)
    check_supported_branch(root, current_version)
    check_readme_version(root, current_version)
    findings = []
    for name in files:
        path = root / name
        if path.suffix not in TEXT_SUFFIXES and path.name not in {'codepier', 'Dockerfile'}:
            continue
        try:
            text = path.read_text(encoding='utf-8')
        except UnicodeError as exc:
            raise ValueError('Selected text source is not UTF-8: ' + name) from exc
        for kind, pattern in CREDENTIAL_PATTERNS.items():
            for match in pattern.finditer(text):
                findings.append({'path': name, 'line': text.count('\n', 0, match.start()) + 1, 'kind': kind})
    if findings:
        # Do not echo a credential into CI logs.
        raise ValueError('Possible credential literals; review paths only: ' + json.dumps(findings))
    return {'version': current_version, 'files': len(files), 'source_bytes': sum(x['bytes'] for x in files.values()),
            'public_profile': True, 'credential_pattern_findings': 0, 'browser_assets': len(browser_assets),
            'inventory_sha256': hashlib.sha256(json.dumps(files, sort_keys=True).encode()).hexdigest()}, files


MAX_ARCHIVE_BYTES = 256 * 1024 * 1024
ZIP_READ_CHUNK = 64 * 1024


def member_digest(archive, entry, expected_size):
    """Bound each decompressor read; declared ZIP sizes alone are not a budget."""
    digest = hashlib.sha256()
    size = 0
    with archive.open(entry) as member:
        while True:
            chunk = member.read(min(ZIP_READ_CHUNK, expected_size - size + 1))
            if not chunk:
                break
            size += len(chunk)
            if size > expected_size:
                raise ValueError('Archive content size differs from current source: ' + entry.filename)
            digest.update(chunk)
    if size != expected_size:
        raise ValueError('Archive content size differs from current source: ' + entry.filename)
    # ZipExtFile verifies CRC when this bounded stream reaches EOF.
    return digest.hexdigest()


def check_archive(path, expected):
    path = Path(path)
    manifest = ''.join(expected[name]['sha256'] + '  ' + name + '\n' for name in sorted(expected)).encode()
    flags = os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0) | getattr(os, 'O_NONBLOCK', 0)
    with os.fdopen(os.open(path, flags), 'rb') as raw:
        before = os.fstat(raw.fileno())
        if not stat.S_ISREG(before.st_mode):
            raise ValueError('Archive must be a regular file')
        if before.st_size > MAX_ARCHIVE_BYTES:
            raise ValueError('Archive size exceeds 256 MiB')
        with zipfile.ZipFile(raw) as archive:
            entries = archive.infolist()
            names = [entry.filename for entry in entries]
            if len(names) != len(set(names)) or set(names) != set(expected) | {'MANIFEST.sha256'}:
                raise ValueError('Archive contains duplicate, missing or unexpected entries')
            timestamps = set()
            # Check every entry before decompressing any member. Other codecs
            # can ignore read limits inside ZipExtFile and are not build outputs.
            for entry in entries:
                if not stat.S_ISREG(entry.external_attr >> 16):
                    raise ValueError('Archive contains a non-regular entry: ' + entry.filename)
                if entry.flag_bits & 0x41:
                    raise ValueError('Archive contains an encrypted entry: ' + entry.filename)
                if entry.flag_bits & ~0x80e:
                    raise ValueError('Archive contains unsupported ZIP flags: ' + entry.filename)
                if entry.compress_type not in {zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED}:
                    raise ValueError('Archive contains unsupported compression: ' + entry.filename)
                timestamps.add(entry.date_time)
                if entry.filename == 'MANIFEST.sha256':
                    if entry.file_size != len(manifest):
                        raise ValueError('Archive manifest size differs from current source')
                else:
                    item = expected[entry.filename]
                    if entry.file_size != item['bytes']:
                        raise ValueError('Archive content size differs from current source: ' + entry.filename)
                    if stat.S_IMODE(entry.external_attr >> 16) != item['mode']:
                        raise ValueError('Archive executable mode differs: ' + entry.filename)
            if len(timestamps) != 1:
                raise ValueError('ZIP timestamps are not deterministic')
            for entry in entries:
                if entry.filename == 'MANIFEST.sha256':
                    if member_digest(archive, entry, len(manifest)) != hashlib.sha256(manifest).hexdigest():
                        raise ValueError('Archive manifest does not describe every source file')
                else:
                    item = expected[entry.filename]
                    if member_digest(archive, entry, item['bytes']) != item['sha256']:
                        raise ValueError('Archive content differs from current source: ' + entry.filename)
        # Hash the same pinned file that was verified, not a possibly replaced
        # path. Bound the archive itself and retain only one chunk in memory.
        raw.seek(0)
        digest = hashlib.sha256()
        size = 0
        while chunk := raw.read(min(ZIP_READ_CHUNK, before.st_size - size + 1)):
            size += len(chunk)
            if size > before.st_size:
                raise ValueError('Archive changed during verification')
            digest.update(chunk)
        if size != before.st_size:
            raise ValueError('Archive changed during verification')
        after = os.fstat(raw.fileno())
        current = path.lstat()
        identity = lambda info: (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)
        if identity(before) != identity(after) or identity(before) != identity(current):
            raise ValueError('Archive changed or was replaced during verification')
    return {'archive': str(path), 'sha256': digest.hexdigest(),
            'bytes': before.st_size, 'verified_against_current_source': True}


def check_panel_update(path, expected):
    """Verify actual release bytes with both the frozen 1.13 and current updater."""
    from scripts import panel_update_source
    files = {name: item for name, item in expected.items() if name not in PANEL_UPDATE_EXCLUDES}
    result = check_archive(path, files)
    with zipfile.ZipFile(path) as archive:
        version = json.loads(archive.read('RELEASE.json'))['version']
    release = {'version': version, 'bytes': result['bytes'], 'sha256': result['sha256']}
    legacy = runpy.run_path(str(ROOT / 'tests/fixtures/panel_update_source_v1_13_0.py'))
    with tempfile.TemporaryDirectory(prefix='codepier-upgrade-check-') as directory:
        for label, unpack in [('1.13.0', legacy['unpack_bundle']), ('current', panel_update_source.unpack_bundle)]:
            try:
                unpack(path, Path(directory) / label, release)
            except Exception as exc:
                raise ValueError('Panel updater compatibility failed: ' + label + ': ' + str(exc)) from exc
    return {**result, 'panel_updaters_verified': ['1.13.0', 'current']}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--bundle', type=Path)
    parser.add_argument('--output', type=Path)
    parser.add_argument('--panel-update', action='store_true', help='Verify the panel update profile and installed updater compatibility')
    args = parser.parse_args()
    if args.panel_update and not args.bundle:
        parser.error('--panel-update requires --bundle')
    try:
        result, files = check_source()
        if args.bundle:
            result.update(check_panel_update(args.bundle, files) if args.panel_update else check_archive(args.bundle, files))
        result['passed'] = True
    except (OSError, ValueError, EOFError, zipfile.BadZipFile) as exc:
        result = {'passed': False, 'error': str(exc)}
    text = json.dumps(result, ensure_ascii=False, indent=2) + '\n'
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(text, encoding='utf-8')
    print(text, end='')
    return 0 if result['passed'] else 1


if __name__ == '__main__':
    raise SystemExit(main())

#!/usr/bin/env python3
"""Generate the panel entrypoint and content-addressed asset inventory.

VERSION is read as a literal, without importing application/dependency code.
--check verifies checked-in artifacts without rewriting them. Build JavaScript
before running this command; no package download occurs here.
"""
from __future__ import annotations

import argparse
import ast
import hashlib
from html import escape, unescape
import json
from pathlib import Path, PurePosixPath
import re
from urllib.parse import urlencode, urlsplit

ROOT = Path(__file__).resolve().parents[1]
REFERENCE = re.compile(r'((?:src|href)=")([^"\n]+)(")')


def version(root: Path = ROOT) -> str:
    tree = ast.parse((root / 'shared/util.py').read_text(encoding='utf-8'))
    values = [ast.literal_eval(node.value) for node in tree.body if isinstance(node, ast.Assign)
              and any(isinstance(target, ast.Name) and target.id == 'VERSION' for target in node.targets)]
    if len(values) != 1 or not isinstance(values[0], str) or not re.fullmatch(r'\d+\.\d+\.\d+', values[0]):
        raise ValueError('VERSION must be one semantic-version string literal')
    return values[0]


def generate(root: Path = ROOT) -> dict[str, bytes]:
    web = root / 'web'
    template = (web / 'index.source.html').read_text(encoding='utf-8')
    release = version(root)
    assets = {}

    def replace(match):
        parsed = urlsplit(unescape(match[2]))
        if parsed.scheme or parsed.netloc or not parsed.path.startswith('/static/'):
            return match[0]
        name = parsed.path.removeprefix('/static/')
        path = PurePosixPath(name)
        if not name or path.is_absolute() or '..' in path.parts or str(path) != name or '%' in name or '\\' in name:
            raise ValueError('Unsafe static asset name: ' + name)
        file = web.joinpath(*path.parts)
        if any(parent.is_symlink() for parent in [file, *file.parents] if parent != root.parent):
            raise ValueError('Symlinked static assets are not reproducible')
        raw = file.read_bytes()
        if len(raw) > 8 * 1024 * 1024:
            raise ValueError('Static asset exceeds the release file budget: ' + name)
        digest = hashlib.sha256(raw).hexdigest()
        assets[name] = {'sha256': digest, 'bytes': len(raw)}
        query = urlencode({'v': 'codepier-' + release, 'h': digest})
        return match[1] + escape('/static/' + name + '?' + query, quote=True) + match[3]

    html = REFERENCE.sub(replace, template)
    if '{{VERSION}}' in html or not assets or 'core/bundle.js' not in assets:
        raise ValueError('Panel asset template is incomplete')
    manifest = {'version': release, 'algorithm': 'sha256', 'assets': dict(sorted(assets.items())),
                'template_sha256': hashlib.sha256(template.encode()).hexdigest()}
    return {'web/index.html': html.encode(),
            'web/assets-manifest.json': (json.dumps(manifest, ensure_ascii=False, indent=2) + '\n').encode()}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--check', action='store_true')
    args = parser.parse_args()
    outputs = generate()
    changed = [name for name, body in outputs.items() if not (ROOT / name).is_file() or (ROOT / name).read_bytes() != body]
    if not args.check:
        for name in changed:
            (ROOT / name).write_bytes(outputs[name])
    print(('Outdated panel assets: ' if args.check else 'Generated panel assets: ') + (', '.join(changed) or 'none'))
    return int(args.check and bool(changed))


if __name__ == '__main__':
    raise SystemExit(main())

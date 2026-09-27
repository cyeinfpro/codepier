"""Small executable policies for public documentation and generated release bytes."""
from __future__ import annotations
import re
from pathlib import Path, PurePosixPath
from urllib.parse import unquote, urlsplit


def check_public_links(root, files):
    root = Path(root).resolve()
    broken = []
    for name in sorted(files):
        if not name.endswith('.md'):
            continue
        text = (root / name).read_text(encoding='utf-8')
        text = re.sub(r'(?ms)^(`{3,}|~{3,})[^\n]*\n.*?^\1\s*$', '', text)
        text = re.sub(r'`[^`\n]*`', '', text)
        for target in re.findall(r'\]\(([^)\n]+)\)', text):
            target = target.strip().split(' "', 1)[0].strip('<>')
            parsed = urlsplit(target)
            if parsed.scheme in {'https', 'http', 'mailto'} or not parsed.path and not parsed.scheme:
                continue
            path = unquote(parsed.path)
            if parsed.scheme or parsed.netloc or '\\' in path or path.startswith('/'):
                broken.append((name, target));continue
            resolved = (root / PurePosixPath(name).parent / path).resolve()
            if not resolved.is_relative_to(root) or resolved.relative_to(root).as_posix() not in files:
                broken.append((name, target))
    if broken:
        raise ValueError('Public documentation links select missing/private files: ' + repr(broken))


def check_generated_assets(root):
    from scripts.build_web_assets import generate
    for name, expected in generate(Path(root)).items():
        if not (Path(root) / name).is_file() or (Path(root) / name).read_bytes() != expected:
            raise ValueError('Generated panel asset is stale: ' + name)


def check_supported_branch(root, version):
    text = (Path(root) / 'SECURITY.md').read_text(encoding='utf-8')
    expected = '.'.join(version.split('.')[:2]) + '.x'
    values = re.findall(r'当前发布分支为\s*\*\*([0-9]+\.[0-9]+\.x)\*\*', text)
    if values != [expected]:
        raise ValueError('SECURITY supported branch differs from source VERSION')


def check_readme_version(root, version):
    text = (Path(root) / 'README.md').read_text(encoding='utf-8')
    patterns = [r'img\.shields\.io/badge/version-(\d+\.\d+\.\d+)-',
                r'当前仓库的 `RELEASE\.json` 标记为 \*\*(\d+\.\d+\.\d+) /',
                r'^- \*\*Version:\*\* (\d+\.\d+\.\d+)\s*$']
    if any(re.findall(pattern, text, re.M) != [version] for pattern in patterns):
        raise ValueError('README current version differs from source VERSION')

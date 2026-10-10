"""Bounded GitHub Release downloads. Standard library only; never executes a bundle."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path, PurePosixPath
import re
import stat
import time
from urllib.error import HTTPError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener
import zipfile

DEFAULT_REPOSITORY = 'cyeinfpro/codepier'
MAX_ARCHIVE = 128 * 1024 * 1024
MAX_FILE = 8 * 1024 * 1024
MAX_FILES = 10000
REQUIRED = {
    'Dockerfile', 'RELEASE.json', 'requirements.txt', 'requirements-agent.txt',
    'hub/app.py', 'hub/panel_update.py', 'hub/agent_install.py',
    'shared/util.py', 'shared/panel_maintenance.py', 'agent/__main__.py',
    'web/index.html', 'web/panel-update.js', 'web/panel-update.css', 'scripts/install_agent.py',
    'scripts/agent_lifecycle.py', 'deploy/install-from-hub.sh',
    'deploy/install-from-hub.ps1', 'LICENSE',
}
ROOT_FILES = {
    'Dockerfile', 'compose.yml', 'RELEASE.json', 'MANIFEST.sha256', 'LICENSE',
    'README.md', 'CHANGELOG.md', 'SECURITY.md', 'CONTRIBUTING.md', 'install.sh',
    'codepier', 'codepier.ps1', 'ruff.toml', '.dockerignore', '.gitignore',
    '.gitattributes', '.env.example', 'requirements.txt', 'requirements-agent.txt',
    'requirements-bridge.txt', 'requirements-dev.txt', 'requirements-compat.txt',
    'requirements-tools.txt',
}
ROOT_DIRS = {'hub', 'agent', 'shared', 'web', 'scripts', 'deploy', 'docs', 'tests', 'skills', '.github'}


class UpdateError(Exception):
    def __init__(self, code, message, status=400):
        super().__init__(message)
        self.code, self.message, self.status = code, message, status


def repository_name(value):
    if not isinstance(value, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,99}/[A-Za-z0-9][A-Za-z0-9_.-]{0,99}', value):
        raise UpdateError('INVALID_REPOSITORY', '更新来源必须是明确的 GitHub owner/repository')
    return value


def version_tuple(value):
    if not isinstance(value, str) or not re.fullmatch(r'(0|[1-9][0-9]{0,5})\.(0|[1-9][0-9]{0,5})\.(0|[1-9][0-9]{0,5})', value):
        raise UpdateError('INVALID_VERSION', '只接受正式的三段式发布版本')
    return tuple(int(part) for part in value.split('.'))


def source_version(root):
    raw = (Path(root) / 'shared/util.py').read_text(encoding='utf-8')
    match = re.search(r'^VERSION\s*=\s*[\"\']([0-9.]+)[\"\']\s*$', raw, re.M)
    if not match:
        raise UpdateError('INVALID_VERSION', '源码中缺少明确版本号')
    version_tuple(match[1])
    return match[1]


class TrustedRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        parsed = urlsplit(newurl)
        allowed = {'github.com', 'release-assets.githubusercontent.com', 'objects.githubusercontent.com'}
        if (parsed.scheme != 'https' or parsed.hostname not in allowed or
                parsed.username or parsed.password or parsed.port not in (None, 443) or
                urlsplit(req.full_url).hostname == 'api.github.com'):
            raise UpdateError('UNTRUSTED_REDIRECT', 'GitHub 下载跳转超出允许的 HTTPS 来源')
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def fetch(url, *, limit, destination=None, deadline_seconds=60):
    parsed = urlsplit(url)
    if (parsed.scheme != 'https' or parsed.hostname not in {'api.github.com', 'github.com'} or
            parsed.username or parsed.password or parsed.port not in (None, 443)):
        raise UpdateError('UNTRUSTED_URL', '拒绝非 GitHub HTTPS 更新地址')
    request = Request(url, headers={'User-Agent': 'CodePier-Panel-Updater',
                                   'Accept': 'application/vnd.github+json' if parsed.hostname == 'api.github.com' else 'application/octet-stream',
                                   'X-GitHub-Api-Version': '2026-03-10'})
    end = time.monotonic() + deadline_seconds
    output = None
    try:
        with build_opener(TrustedRedirect()).open(request, timeout=20) as response:
            if response.status != 200:
                raise UpdateError('GITHUB_UNAVAILABLE', 'GitHub 未返回完整下载内容', 502)
            length = response.headers.get('Content-Length')
            if length and (not length.isdigit() or int(length) > limit):
                raise UpdateError('DOWNLOAD_LIMIT', 'GitHub 下载文件超过允许大小')
            if destination is not None:
                output = Path(destination).open('xb')
            content, total = bytearray(), 0
            while True:
                if time.monotonic() >= end:
                    raise UpdateError('DOWNLOAD_TIMEOUT', 'GitHub 下载超时，请检查服务器网络', 504)
                chunk = response.read1(min(65536, limit + 1 - total))
                if not chunk:
                    break
                total += len(chunk)
                if total > limit:
                    raise UpdateError('DOWNLOAD_LIMIT', 'GitHub 下载文件超过允许大小')
                if output is not None:
                    output.write(chunk)
                else:
                    content.extend(chunk)
            return total if output is not None else bytes(content)
    except HTTPError as exc:
        code, message = ('NO_RELEASE', 'GitHub 尚无可用正式 Release；不会退回 main 分支执行未发布代码') if exc.code == 404 else ('GITHUB_UNAVAILABLE', 'GitHub 请求失败或限流，请稍后重新检查')
        raise UpdateError(code, message, 502) from exc
    except UpdateError:
        raise
    except (OSError, ValueError) as exc:
        raise UpdateError('GITHUB_UNAVAILABLE', '无法完整读取 GitHub 下载，请检查服务器网络', 502) from exc
    finally:
        if output is not None:
            output.close()


def latest_release(repository=DEFAULT_REPOSITORY):
    repository = repository_name(repository)
    try:
        release = json.loads(fetch(f'https://api.github.com/repos/{repository}/releases/latest', limit=2 * 1024 * 1024))
        if not isinstance(release, dict) or release.get('draft') is not False or release.get('prerelease') is not False:
            raise ValueError('Not a stable release')
        tag = release['tag_name']
        if not isinstance(tag, str) or not isinstance(release.get('assets'), list) or any(not isinstance(a, dict) for a in release['assets']):
            raise ValueError('Invalid tag or assets')
        version = tag.removeprefix('v')
        version_tuple(version)
        name = f'codepier-{version}-source.zip'
        assets = [asset for asset in release['assets'] if asset.get('name') == name and asset.get('state') == 'uploaded']
        if len(assets) != 1:
            raise UpdateError('RELEASE_ASSET_MISSING', f'正式 Release 需要上传 {name} 源码包')
        asset = assets[0]
        digest = asset.get('digest', '')
        if not isinstance(digest, str) or not re.fullmatch(r'sha256:[a-f0-9]{64}', digest):
            raise UpdateError('RELEASE_DIGEST_MISSING', 'GitHub Release 源码包缺少 SHA-256；拒绝未校验更新')
        size = asset['size']
        if type(size) is not int or not 0 < size <= MAX_ARCHIVE:
            raise ValueError('Invalid size')
        url = f'https://github.com/{repository}/releases/download/{tag}/{name}'
        if asset['browser_download_url'] != url or type(release['id']) is not int or type(asset['id']) is not int:
            raise ValueError('Invalid release identity')
        return {'repository': repository, 'release_id': release['id'], 'asset_id': asset['id'],
                'version': version, 'tag': tag, 'sha256': digest[7:], 'bytes': size, 'url': url,
                'notes': str(release.get('body') or '')[:12000],
                'published_at': str(release.get('published_at') or '')[:50], 'checked_at': time.time()}
    except (KeyError, TypeError, ValueError) as exc:
        raise UpdateError('INVALID_RELEASE', 'GitHub 正式 Release 元数据不完整或格式不正确', 502) from exc


def safe_name(name):
    path = PurePosixPath(name)
    if (not name or len(name) > 512 or '\\' in name or ':' in name or
            any(ord(c) < 32 or ord(c) == 127 for c in name) or
            path.is_absolute() or any(p in {'', '.', '..'} for p in name.split('/')) or
            path.as_posix() != name):
        raise UpdateError('UNSAFE_ARCHIVE', '源码包包含不安全路径')
    if (len(path.parts) == 1 and name not in ROOT_FILES or
            len(path.parts) > 1 and path.parts[0] not in ROOT_DIRS):
        raise UpdateError('UNSAFE_ARCHIVE', '源码包包含未允许的顶层文件')
    if any(p in {'.git', '.env', 'master.key', 'config.json', 'pairing.json', '__pycache__', 'node_modules'} for p in path.parts):
        raise UpdateError('UNSAFE_ARCHIVE', '源码包混入运行状态或私密配置')
    return path


def unpack_bundle(archive_path, destination, release):
    """Verify the external digest AND exact manifest before writing any member."""
    archive_path, destination = Path(archive_path), Path(destination)
    if archive_path.stat().st_size != release['bytes'] or archive_path.stat().st_size > MAX_ARCHIVE:
        raise UpdateError('BUNDLE_SIZE_MISMATCH', '源码包长度与 GitHub 声明不一致')
    sha = hashlib.sha256()
    with archive_path.open('rb') as stream:
        for block in iter(lambda: stream.read(65536), b''):
            sha.update(block)
    if sha.hexdigest() != release['sha256']:
        raise UpdateError('BUNDLE_DIGEST_MISMATCH', '源码包 SHA-256 校验失败，未修改运行版本')
    if destination.exists():
        raise UpdateError('STAGING_EXISTS', '候选源码目录已经存在，拒绝覆盖')
    try:
        with zipfile.ZipFile(archive_path) as archive:
            entries = archive.infolist()
            if not 1 <= len(entries) <= MAX_FILES:
                raise ValueError('Member limit')
            names, folded, total = set(), set(), 0
            for info in entries:
                safe_name(info.filename)
                mode = info.external_attr >> 16
                if (info.is_dir() or info.filename in names or info.filename.casefold() in folded or
                        stat.S_IFMT(mode) not in (0, stat.S_IFREG) or mode & 0o7000 or info.flag_bits & ~0x808 or
                        info.compress_type not in {zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED} or
                        not 0 <= info.file_size <= MAX_FILE):
                    raise ValueError('Unsafe member')
                names.add(info.filename)
                folded.add(info.filename.casefold())
                total += info.file_size
                if total > MAX_ARCHIVE:
                    raise ValueError('Expanded limit')
            if not REQUIRED | {'MANIFEST.sha256'} <= names:
                raise ValueError('Required runtime files missing')
            manifest = {}
            for line in archive.read('MANIFEST.sha256').decode('utf-8').splitlines():
                digest, name = line.split('  ', 1)
                safe_name(name)
                if name in manifest or not re.fullmatch(r'[a-f0-9]{64}', digest):
                    raise ValueError('Invalid manifest')
                manifest[name] = digest
            if set(manifest) != names - {'MANIFEST.sha256'}:
                raise ValueError('Manifest coverage')
            for name, expected in manifest.items():
                if hashlib.sha256(archive.read(name)).hexdigest() != expected:
                    raise ValueError('Manifest mismatch')
            metadata = json.loads(archive.read('RELEASE.json'))
            if metadata.get('name') != 'CodePier' or metadata.get('version') != release['version']:
                raise ValueError('Version mismatch')
            destination.mkdir(mode=0o700, parents=True)
            for info in entries:
                target = destination / info.filename
                target.parent.mkdir(parents=True, exist_ok=True)
                with target.open('xb') as output:
                    output.write(archive.read(info))
                target.chmod(0o755 if (info.external_attr >> 16) & 0o111 else 0o644)
            if source_version(destination) != release['version']:
                raise ValueError('Runtime version mismatch')
    except UpdateError:
        raise
    except (OSError, ValueError, KeyError, zipfile.BadZipFile, UnicodeError) as exc:
        raise UpdateError('INVALID_BUNDLE', '源码包清单、路径、版本或压缩内容校验失败') from exc
    return destination

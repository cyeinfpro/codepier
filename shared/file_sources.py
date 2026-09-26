"""Reviewed native-file sources shared by configuration, import and diagnostics.

Never trust a storage-provider suffix or a caller-supplied file_id. New native
hosts require an independently verified host-file roundtrip or owner approval.
Signed URLs, object paths and file IDs must not appear in this registry/logs.
"""
from __future__ import annotations

import ipaddress
import re
from types import MappingProxyType
from urllib.parse import urlsplit

FILE_SOURCE_POLICY_VERSION = '2026-09-26'
FILE_SOURCE_PROVIDERS = MappingProxyType({
    'openai': (
        'files.oaiusercontent.com',
        'cdn.openai.com',
        # Observed through the native top-level file parameter on 2026-09-26.
        # Exact storage account only: other Azure tenants/prefixes are untrusted.
        'oaisdmntprkoreacentral.blob.core.windows.net',
    ),
})
DEFAULT_FILE_HOSTS = tuple(dict.fromkeys(host for hosts in FILE_SOURCE_PROVIDERS.values() for host in hosts))
DEFAULT_MAX_IMPORT_BYTES = 128 * 1024 * 1024
MAX_IMPORT_BYTES = 512 * 1024 * 1024
_LABEL = re.compile(r'[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\Z')


def normalize_file_host(value: str) -> str:
    """Canonicalize one exact DNS name; never accept patterns, URLs or IPs."""
    if not isinstance(value, str) or not value or len(value) > 254 or not value.isascii():
        raise ValueError('文件来源应为精确 ASCII 主机名')
    host = value.lower().removesuffix('.')
    if len(host) > 253 or '.' not in host or any(not _LABEL.fullmatch(label) for label in host.split('.')):
        raise ValueError('文件来源不接受通配符、URL、端口、凭据或无效 DNS 标签')
    try:
        ipaddress.ip_address(host)
    except ValueError:
        return host
    raise ValueError('文件来源必须为 DNS 主机名，不能是 IP 地址')


def normalize_file_hosts(value: object, label: str) -> list[str]:
    if not isinstance(value, (list, tuple)) or len(value) > 20:
        raise ValueError(label + ' 必须为至多 20 个精确主机名的数组')
    # Empty is an intentional deny-all base, not a request for defaults.
    return list(dict.fromkeys(normalize_file_host(host) for host in value))


def file_source_hosts(config: dict) -> tuple[str, ...]:
    """Preserve explicit restrictions; extensions add only owner-named hosts."""
    base = normalize_file_hosts(config.get('file_hosts', DEFAULT_FILE_HOSTS), 'file_hosts')
    extra = normalize_file_hosts(config.get('extra_file_hosts', []), 'extra_file_hosts')
    return tuple(dict.fromkeys([*base, *extra]))


def source_metadata(value: object) -> dict[str, str]:
    """Return bounded scheme/host only, even for a rejected or malformed URL."""
    result = {'source_scheme': '', 'source_host': ''}
    if not isinstance(value, str) or len(value) > 8192:
        return result
    try:
        parsed = urlsplit(value)
        if re.fullmatch(r'[a-z][a-z0-9+.-]{0,31}', parsed.scheme):
            result['source_scheme'] = parsed.scheme
        result['source_host'] = normalize_file_host(parsed.hostname or '')
    except (ValueError, UnicodeError):
        pass
    return result

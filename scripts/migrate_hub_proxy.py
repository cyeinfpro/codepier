"""Move explicitly trusted host-proxy gateways across owned Compose networks.

Never trust an entire subnet, external network, or arbitrary forwarded header.
The plan is captured before retiring old networks; apply after Compose creates
new networks and before starting the public Hub.
"""
from __future__ import annotations
import ipaddress
import os
from pathlib import Path
import re
import shlex
import stat
import tempfile

KEY = 'FORWARDED_ALLOW_IPS'


def trust(config):
    return str(config.get('services', {}).get('hub', {}).get('environment', {}).get(KEY, '127.0.0.1'))


def tokens(value):
    return [part.strip() for part in value.split(',') if part.strip()]


def preflight(config, containers, docker, legacy_project):
    trusted = tokens(trust(config))
    plans = []
    hub = config.get('services', {}).get('hub', {})
    attached = hub.get('networks', {})
    for container in containers:
        if container.get('Config', {}).get('Labels', {}).get('com.docker.compose.service') != 'hub':
            continue
        for name, endpoint in container.get('NetworkSettings', {}).get('Networks', {}).items():
            gateways = [endpoint.get('Gateway'), endpoint.get('IPv6Gateway')]
            selected = [gateway for gateway in gateways if gateway and gateway in trusted]
            if not selected:
                continue
            old = docker.json(['network', 'inspect', name])[0]
            labels = old.get('Labels') or {}
            key = labels.get('com.docker.compose.network')
            new = config.get('networks', {}).get(key, {})
            if (labels.get('com.docker.compose.project') != legacy_project or
                    old.get('Driver') != 'bridge' or key not in attached or
                    not new or new.get('external') or new.get('driver', 'bridge') != 'bridge'):
                raise RuntimeError('Trusted proxy gateway belongs to an unmanaged network; configure FORWARDED_ALLOW_IPS explicitly before migration')
            for gateway in selected:
                plans.append({'network': key, 'target': new['name'], 'old_gateway': gateway,
                              'ip_version': ipaddress.ip_address(gateway).version})
    return plans


def atomic_env(path, content, original_stat):
    fd, temporary = tempfile.mkstemp(prefix='.codepier-proxy-', dir=path.parent)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8', newline='') as stream:
            stream.write(content)
            os.fchmod(stream.fileno(), stat.S_IMODE(original_stat.st_mode))
            if (os.getuid(), os.getgid()) != (original_stat.st_uid, original_stat.st_gid):
                os.fchown(stream.fileno(), original_stat.st_uid, original_stat.st_gid)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def apply(root, plans, docker):
    if not plans:
        return {'changed': False}
    config = docker.json(['compose', 'config', '--format', 'json'])
    current = trust(config)
    replacements = {}
    for plan in plans:
        if plan['old_gateway'] not in tokens(current):
            continue  # Already migrated, or the operator intentionally changed trust.
        key = plan['network']
        network = config.get('networks', {}).get(key, {})
        if (network.get('name') != plan['target'] or network.get('external') or
                key not in config.get('services', {}).get('hub', {}).get('networks', {})):
            raise RuntimeError('Proxy network selection changed; no trust configuration was modified')
        target = docker.json(['network', 'inspect', plan['target']])[0]
        labels = target.get('Labels') or {}
        if (target.get('Driver') != 'bridge' or labels.get('com.docker.compose.project') != 'codepier' or
                labels.get('com.docker.compose.network') != key):
            raise RuntimeError('New proxy network ownership is unverified')
        candidates = {row['Gateway'] for row in (target.get('IPAM') or {}).get('Config') or []
                      if row.get('Gateway') and ipaddress.ip_address(row['Gateway']).version == plan['ip_version']}
        if len(candidates) != 1:
            raise RuntimeError('New proxy gateway is missing or ambiguous')
        gateway = candidates.pop()
        if plan['old_gateway'] in replacements and replacements[plan['old_gateway']] != gateway:
            raise RuntimeError('Trusted proxy gateway maps to multiple new networks')
        replacements[plan['old_gateway']] = gateway
    updated = ','.join(dict.fromkeys(replacements.get(item, item) for item in tokens(current)))
    if tokens(updated) == tokens(current):
        return {'changed': False}
    if KEY in os.environ:
        raise RuntimeError('Unset exported FORWARDED_ALLOW_IPS and persist it in .env before gateway migration')
    path = Path(root) / '.env'
    if path.is_symlink() or not path.is_file():
        raise RuntimeError('Proxy migration requires a regular .env file')
    original_stat = path.stat()
    original = path.read_bytes().decode('utf-8')
    lines = original.splitlines(keepends=True)
    matches = [i for i, line in enumerate(lines) if re.match(r'^\s*(?:export\s+)?FORWARDED_ALLOW_IPS\s*=', line)]
    if len(matches) > 1:
        raise RuntimeError('Duplicate FORWARDED_ALLOW_IPS entries; no configuration was modified')
    if matches:
        index = matches[0]
        raw = lines[index].split('=', 1)[1]
        parsed = shlex.split(raw, comments=True)
        if '$' in raw or len(parsed) != 1 or tokens(parsed[0]) != tokens(current):
            raise RuntimeError('FORWARDED_ALLOW_IPS is not a matching literal .env value; configure it explicitly')
        lines[index] = KEY + '=' + updated + '\n'
        replacement = ''.join(lines)
    else:
        replacement = original + ('' if original.endswith('\n') else '\n') + KEY + '=' + updated + '\n'
    if path.read_bytes().decode('utf-8') != original:
        raise RuntimeError('.env changed during proxy migration')
    atomic_env(path, replacement, original_stat)
    try:
        effective = trust(docker.json(['compose', 'config', '--format', 'json']))
        if tokens(effective) != tokens(updated):
            raise RuntimeError('Compose overrides FORWARDED_ALLOW_IPS; configure the selected overlay explicitly')
    except BaseException:
        # Do not overwrite a concurrent operator edit while restoring our change.
        if path.read_bytes().decode('utf-8') == replacement:
            atomic_env(path, original, original_stat)
        raise
    return {'changed': True, 'previous': current, 'current': updated}

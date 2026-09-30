"""Atomic resource sets with FIFO conflict fairness and cancellation cleanup."""
from __future__ import annotations

import asyncio
import os
import sys
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from shared.util import DevError


@dataclass(frozen=True)
class Claim:
    namespace: str
    kind: str
    name: str
    write: bool

    def conflicts(self, other):
        if self.namespace != other.namespace or self.kind != other.kind or not (self.write or other.write):
            return False
        if self.kind == 'service':
            return self.name == other.name
        left, right = PurePosixPath(self.name), PurePosixPath(other.name)
        return left == right or left in right.parents or right in left.parents


def overlaps(left, right):
    return any(a.conflicts(b) for a in left for b in right)


class ResourceQueue:
    def __init__(self):
        self.condition = asyncio.Condition()
        self.active = {}
        self.waiting = []

    @asynccontextmanager
    async def slot(self, operation_id, claims, on_wait):
        token = object()
        entry = (token, operation_id, claims)
        try:
            async with self.condition:
                self.waiting.append(entry)
                while True:
                    blockers = [identifier for identifier, held in self.active.values() if overlaps(claims, held)]
                    for waiter, identifier, requested in self.waiting:
                        if waiter is token:
                            break
                        if overlaps(claims, requested):
                            blockers.append(identifier)
                    if not blockers:
                        break
                    on_wait(blockers[:64])
                    await self.condition.wait()
                self.waiting.remove(entry)
                self.active[token] = (operation_id, claims)
            yield
        finally:
            async with self.condition:
                if entry in self.waiting:
                    self.waiting.remove(entry)
                self.active.pop(token, None)
                self.condition.notify_all()


PATH_READ_TOOLS = frozenset({'fs_tree', 'fs_read', 'fs_read_many', 'fs_search'})


def canonical_path(path):
    name = Path(path).expanduser().resolve().as_posix()
    # macOS commonly uses case-insensitive APFS. Conservatively alias case even
    # on case-sensitive volumes: extra serialization is safer than missing a
    # conflict, including paths whose final component does not yet exist.
    return name.casefold() if os.name == 'nt' or sys.platform == 'darwin' else name


def claims_for(engine, tool, project, args, root):
    if tool in PATH_READ_TOOLS:
        paths = args['paths'] if tool == 'fs_read_many' else [args['path']]
        try:
            return [Claim('agent', 'path', canonical_path(engine.path(root, path)), False) for path in paths]
        except (DevError, OSError):
            if tool != 'fs_read_many':
                raise
            # Batch reads report path failures per item. Keep a conservative
            # claim if a rejected path becomes readable while this job waits.
            return [Claim('agent', 'path', canonical_path(root), False)]
    if tool == 'edit' and args.get('changes'):
        paths = [change['path'] for change in args['changes']] + [change['destination'] for change in args['changes'] if change.get('destination')]
        return [Claim('agent', 'path', canonical_path(engine.path(root, path, False)), True) for path in paths]
    if tool in {'read', 'write', 'edit', 'download_artifact'}:
        path = engine.path(root, args['path'], False).resolve()
        return [Claim('agent', 'path', canonical_path(path), tool != 'read')]
    if tool != 'exec':
        return []
    if args.get('task'):
        task = engine.config.get('tasks', {}).get(args['task'], {})
        # This is local task policy, never caller-declared resource policy.
        # Public exec(task=...) must preserve legacy tasks_run's exclusion.
        return [Claim('agent', 'path', canonical_path(root), not task.get('allow_read_concurrency', False))]
    remote = project.get('_core_ssh')
    namespace = f"ssh:{remote['host']}:{remote['port']}" if remote else 'agent'
    cwd = Path(args['cwd']).expanduser()
    if not cwd.is_absolute():
        cwd = root / cwd
    claims = []
    for resource in args['resources']:
        name = resource['name']
        if resource['kind'] == 'path':
            if remote:
                # Relative remote paths are scoped conservatively under '/', as
                # the login shell's real cwd is not available before execution.
                if '..' in PurePosixPath(name).parts:
                    raise DevError('INVALID_RESOURCE', '远程资源路径不接受 ..，请使用绝对路径')
                name = '/' if not PurePosixPath(name).is_absolute() else str(PurePosixPath(name))
            else:
                path = Path(name).expanduser()
                name = canonical_path(path if path.is_absolute() else cwd / path)
        claims.append(Claim(namespace, resource['kind'], name, resource['mode'] == 'write'))
    return claims

"""Atomic resource sets with FIFO conflict fairness and cancellation cleanup."""
from __future__ import annotations

import asyncio
import os
import sys
from contextlib import asynccontextmanager
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path, PurePosixPath

from shared.util import DevError


@lru_cache(maxsize=4096)
def _path_scope(name):
    # Claims are normally canonical already; normalize direct/internal claims
    # too. Cache only bounded path metadata, never file contents or authority.
    path = PurePosixPath(name)
    return path.anchor, path.parts


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
        left_anchor, left = _path_scope(self.name)
        right_anchor, right = _path_scope(other.name)
        return left_anchor == right_anchor and (left[:len(right)] == right or right[:len(left)] == left)


def overlaps(left, right):
    return any(a.conflicts(b) for a in left for b in right)


async def finish_cleanup(awaitable):
    """Own queue cleanup through repeated cancellation, then propagate it."""
    task = asyncio.create_task(awaitable)
    cancelled = False
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            cancelled = True
    task.result()
    if cancelled:
        raise asyncio.CancelledError


class ResourceQueue:
    def __init__(self):
        self.condition = asyncio.Condition()
        self.active = {}
        self.waiting = []
        self._events = {}
        self._callbacks = {}
        self._reported = {}
        self._listeners = []
        self._entries = {}
        self._dependencies = {}
        self._dependents = {}

    def add_listener(self, listener):
        """Register a synchronous joint-admission drain on this event loop.

        Listeners share ``condition`` with this queue and never await while
        inspecting or granting resources. Waiting coroutines have private events;
        a resource release never wakes unrelated blocked coroutines.
        """
        self._listeners.append(listener)

    def enqueue(self, token, operation_id, claims):
        claims = tuple(claims)
        dependencies = dict.fromkeys(held_token for held_token, (_, held) in self._entries.items()
                                     if overlaps(claims, held))
        self._entries[token] = (operation_id, claims)
        self._dependencies[token] = dependencies
        for blocker in dependencies:
            self._dependents.setdefault(blocker, set()).add(token)
        self.waiting.append((token, operation_id, claims))

    def blockers(self, token):
        # Dependencies are fixed against earlier owners/waiters on enqueue.
        # Granting an earlier waiter does not change conflict order; release or
        # cancellation removes only its reverse-indexed dependent edges.
        return list(dict.fromkeys(self._entries[blocker][0]
                    for blocker in self._dependencies.get(token, {})))[:64]

    def blocked(self, token):
        return bool(self._dependencies.get(token))

    def grant(self, token):
        """Move a prechecked waiter to active without yielding or notifying."""
        entry = next(item for item in self.waiting if item[0] is token)
        self.waiting.remove(entry)
        self.active[token] = (entry[1], entry[2])
        self._dependencies.pop(token, None)

    def discard(self, token):
        self.waiting[:] = [item for item in self.waiting if item[0] is not token]
        self.active.pop(token, None)
        self._entries.pop(token, None)
        for blocker in self._dependencies.pop(token, {}):
            dependents = self._dependents.get(blocker)
            if dependents is not None:
                dependents.discard(token)
                if not dependents:
                    self._dependents.pop(blocker, None)
        for dependent in self._dependents.pop(token, ()):
            self._dependencies.get(dependent, {}).pop(token, None)
        self._events.pop(token, None)
        self._callbacks.pop(token, None)
        self._reported.pop(token, None)

    def drain(self):
        """Grant standalone claims, then jointly eligible scheduler tickets.

        Earlier conflicting waiters remain in the same FIFO, including waiters
        that need execution capacity. Independent paths and shared reads may
        pass; a new reader cannot continually overtake a pending writer.
        """
        for token, _, _ in tuple(self.waiting):
            event = self._events.get(token)
            if event is None:
                continue  # A scheduler ticket must acquire both budgets.
            blockers = self.blockers(token)
            if not blockers:
                self.grant(token)
                self._reported.pop(token, None)
                event.set()
            elif self._reported.get(token) != blockers:
                self._reported[token] = blockers
                callback = self._callbacks.get(token)
                if callback:
                    callback(blockers)
        for listener in self._listeners:
            listener()

    def try_reserve(self, operation_id, claims):
        """Atomically reserve without waiting; existing conflicting waiters win."""
        if self.condition.locked():
            return None
        if any(identifier == operation_id for identifier, _ in self.active.values()):
            return None
        if any(identifier == operation_id for _, identifier, _ in self.waiting):
            return None
        if any(overlaps(claims, held) for _, held in self.active.values()):
            return None
        if any(overlaps(claims, requested) for _, _, requested in self.waiting):
            return None
        token = object()
        self.active[token] = self._entries[token] = (operation_id, tuple(claims))
        return token

    async def release(self, token):
        await finish_cleanup(self._release(token))

    async def _release(self, token):
        async with self.condition:
            self.discard(token)
            self.drain()

    @asynccontextmanager
    async def slot(self, operation_id, claims, on_wait):
        token = object()
        event = asyncio.Event()
        added = False
        try:
            async with self.condition:
                self.enqueue(token, operation_id, claims)
                self._events[token] = event
                self._callbacks[token] = on_wait
                added = True
                self.drain()
            await event.wait()
            yield
        finally:
            if added:
                await self.release(token)


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
    if tool == 'read' and args.get('operation') == 'batch':
        try:
            return [Claim('agent', 'path', canonical_path(engine.path(root, item['path'], False)), False)
                    for item in args['options']['items']]
        except (DevError, OSError):
            # Per-item errors belong to the durable read result, not admission.
            # Revalidate every path at execution, under a conservative root claim.
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

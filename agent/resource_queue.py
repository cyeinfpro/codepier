"""Atomic resource sets with FIFO conflict fairness and cancellation cleanup."""
from __future__ import annotations

import asyncio
import os
import sys
import time
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
    holder: str = 'file'
    stable: bool = False

    def conflicts(self, other, *, relaxed=True):
        if self.namespace != other.namespace or self.kind != other.kind or not (self.write or other.write):
            return False
        if self.kind == 'service':
            return self.name == other.name
        if relaxed and self.holder != other.holder:
            file_claim, process_claim = (self, other) if self.holder == 'file' else (other, self)
            if not file_claim.write or (not process_claim.write and not process_claim.stable):
                return False
        left_anchor, left = _path_scope(self.name)
        right_anchor, right = _path_scope(other.name)
        return left_anchor == right_anchor and (left[:len(right)] == right or right[:len(left)] == left)


def overlaps(left, right, *, relaxed=True):
    return any(a.conflicts(b, relaxed=relaxed) for a in left for b in right)


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
    def __init__(self, *, semantics='strict', clock=time.monotonic, bypass_seconds=5):
        self.relaxed = semantics == 'relaxed'
        self.clock = clock
        self.bypass_seconds = bypass_seconds
        self._started = {}
        self._bounded = {}
        self._bypass_roots = {}
        self._bypass_callbacks = {}
        self._timer = None
        self._live = {}
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

    def enqueue(self, token, operation_id, claims, *, bounded=False, strict=False, on_bypass=None):
        claims = tuple(claims)
        dependencies = dict.fromkeys(held_token for held_token, (_, held) in self._entries.items()
                                     if overlaps(claims, held, relaxed=self.relaxed))
        self._bounded[token] = bounded and not strict
        self._bypass_callbacks[token] = on_bypass
        self._entries[token] = (operation_id, claims)
        self._dependencies[token] = dependencies
        for blocker in dependencies:
            self._dependents.setdefault(blocker, set()).add(token)
        self.waiting.append((token, operation_id, claims))

    def reconfigure(self, semantics):
        self.relaxed = semantics == 'relaxed'
        self._bypass_roots.clear()
        self._dependents.clear()
        for token, (_, claims) in self._entries.items():
            if token not in self.active:
                preceding = []
                for other, (_, held) in self._entries.items():
                    if other is token:
                        break
                    if overlaps(claims, held, relaxed=self.relaxed):
                        preceding.append(other)
                self._dependencies[token] = dict.fromkeys(preceding)
                for blocker in preceding:
                    self._dependents.setdefault(blocker, set()).add(token)
        self.drain()

    def blockers(self, token):
        claims = self._entries.get(token, (None, ()))[1]
        tokens = list(self._dependencies.get(token, {}))
        tokens += [held_token for held_token, (_, held) in self.active.items()
                   if held_token is not token and overlaps(claims, held, relaxed=self.relaxed)]
        return list(dict.fromkeys(self._entries[blocker][0]
                    for blocker in tokens if blocker in self._entries))[:64]

    def _active_roots(self, token, seen=None):
        # The admitted queue permits 1024 waiters. A valid sparse dependency
        # chain can exceed Python's recursion limit, so traverse iteratively.
        seen = set() if seen is None else seen
        pending, roots = [token], set()
        while pending:
            current = pending.pop()
            if current in seen:
                continue
            seen.add(current)
            if current in self.active:
                roots.add(current)
            else:
                pending.extend(self._dependencies.get(current, {}))
        return roots

    def _strict_bypass_allowed(self, token, dependencies):
        """Convoy relief that keeps strict-mode ordering guarantees.

        Under strict semantics a short read could otherwise wait for a queued
        command that is itself waiting for a long-running root (diagnosed
        2026-11-06: one 272 s Git command held up an exec and every read queued
        behind it). A read-only file operation may overtake *queued process
        holders* only. It never overtakes a queued edit/write, so read-your-
        writes for file edits is preserved, and it never writes itself. It still
        cannot overlap any *active* holder (``blocked`` checks that first), and
        the long-running-root and writer-turn gates below apply unchanged.
        """
        claims = self._entries.get(token, (None, ()))[1]
        if not claims or any(claim.write or claim.holder != 'file' for claim in claims):
            return False
        for blocker in dependencies:
            held = self._entries.get(blocker, (None, ()))[1]
            for other in held:
                if other.holder != 'process' and any(mine.conflicts(other, relaxed=False) for mine in claims):
                    return False
        return True

    def _bypassable(self, token):
        if not self._bounded.get(token):
            return False
        dependencies = self._dependencies.get(token, {})
        if not dependencies:
            return False
        if not self.relaxed and not self._strict_bypass_allowed(token, dependencies):
            return False
        now = self.clock()
        for blocker in dependencies:
            if blocker in self.active:
                return False
            roots = self._active_roots(blocker)
            gate = self._bypass_roots.get(blocker)
            if gate is not None:
                roots &= gate
            if not any(now - self._started.get(root, now) >= self.bypass_seconds for root in roots):
                return False
        return True

    def blocked(self, token):
        claims = self._entries.get(token, (None, ()))[1]
        # A bypassed younger file operation becomes a real owner. An older
        # waiter must not be granted until that owner has released its claim.
        if any(held_token is not token and overlaps(claims, held, relaxed=self.relaxed)
               for held_token, (_, held) in self.active.items()):
            return True
        return bool(self._dependencies.get(token)) and not self._bypassable(token)

    def _schedule_bypass(self):
        # Strict mode has a narrower bypass (see _strict_bypass_allowed) that
        # still needs the timer to notice when a root has run long enough.
        if self._timer is not None or not self.waiting or not self.active:
            return
        now = self.clock()
        deadlines = [at + self.bypass_seconds for at in self._started.values()
                     if at + self.bypass_seconds > now]
        if deadlines:
            self._timer = asyncio.get_running_loop().call_later(
                max(.001, min(deadlines) - now), self._wake_bypass)

    def _wake_bypass(self):
        self._timer = None
        # Queue transitions contain no awaits while the condition is held.
        # Event-loop callbacks cannot interrupt one of those transitions.
        self.drain()

    def live_writers(self, claims, operation_id=None):
        if not self.relaxed:
            return []
        return list(dict.fromkeys(identifier for identifier, held in self.active.values()
            if identifier != operation_id and any(
                a.holder == 'file' and not a.write and b.holder == 'process' and b.write
                and a.conflicts(b, relaxed=False) for a in claims for b in held)))[:64]

    def concurrency_for(self, operation_id):
        writers = set()
        for token, (identifier, _) in self.active.items():
            if identifier == operation_id:
                writers.update(self._live.get(token, ()))
        return {'mode': 'live', 'overlapping_writers': sorted(writers)[:64]} if writers else None

    def grant(self, token):
        """Move a prechecked waiter to active without yielding or notifying."""
        entry = next(item for item in self.waiting if item[0] is token)
        self.waiting.remove(entry)
        dependencies = self._dependencies.get(token, {})
        bypassed = list(dependencies)
        if bypassed:
            for blocker in bypassed:
                self._bypass_roots.setdefault(blocker, self._active_roots(blocker))
        self.active[token] = (entry[1], entry[2])
        self._started[token] = self.clock()
        if self.relaxed:
            for reader_token, (reader_id, reader_claims) in self.active.items():
                for writer_id, writer_claims in self.active.values():
                    if reader_id != writer_id and any(
                        a.holder == 'file' and not a.write and b.holder == 'process' and b.write
                        and a.conflicts(b, relaxed=False) for a in reader_claims for b in writer_claims):
                        self._live.setdefault(reader_token, set()).add(writer_id)
        self._dependencies.pop(token, None)
        for blocker in dependencies:
            self._dependents.get(blocker, set()).discard(token)
        callback = self._bypass_callbacks.get(token)
        if bypassed and callback:
            callback(len(bypassed))

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
        self._live.pop(token, None)
        self._started.pop(token, None)
        self._bounded.pop(token, None)
        self._bypass_roots.pop(token, None)
        self._bypass_callbacks.pop(token, None)
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
            if not self.blocked(token):
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
        self._schedule_bypass()
        if not self.waiting and self._timer is not None:
            self._timer.cancel()
            self._timer = None

    def try_reserve(self, operation_id, claims, *, bounded=False, strict=False, on_bypass=None):
        """The same conflict and bounded-overtake predicate as queued grants."""
        if self.condition.locked():
            return None
        if any(identifier == operation_id for identifier, _ in self._entries.values()):
            return None
        token = object()
        self.enqueue(token, operation_id, claims, bounded=bounded, strict=strict, on_bypass=on_bypass)
        if self.blocked(token):
            self.discard(token)
            return None
        self.grant(token)
        return token

    async def release(self, token):
        await finish_cleanup(self._release(token))

    async def _release(self, token):
        async with self.condition:
            self.discard(token)
            self.drain()

    @asynccontextmanager
    async def slot(self, operation_id, claims, on_wait, *, bounded=False, strict=False, on_bypass=None):
        token = object()
        event = asyncio.Event()
        added = False
        try:
            async with self.condition:
                self.enqueue(token, operation_id, claims, bounded=bounded, strict=strict, on_bypass=on_bypass)
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
SCOPED_READ_TOOLS = frozenset({'searches_start', 'code_symbols', 'history_list'})
BOUNDED_FILE_TOOLS = PATH_READ_TOOLS | SCOPED_READ_TOOLS | {'read', 'write', 'edit'}


def canonical_path(path):
    name = Path(path).expanduser().resolve().as_posix()
    # macOS commonly uses case-insensitive APFS. Conservatively alias case even
    # on case-sensitive volumes: extra serialization is safer than missing a
    # conflict, including paths whose final component does not yet exist.
    return name.casefold() if os.name == 'nt' or sys.platform == 'darwin' else name


def _worktree_creation_claims(engine, root, operation_id=None):
    """Coordinate Git metadata and the actual publish target, not unrelated source.

    Git pointers are read only to calculate conflict keys. They never establish
    filesystem authority; Workspaces.create retains its existing root, local
    write, symlink, identity, and configured-runtime checks.
    """
    import hashlib
    from agent.run_directories import operation_name

    root = Path(root)
    metadata = root / '.git'
    paths = [metadata]
    if metadata.is_file():
        try:
            with metadata.open('r', encoding='utf-8') as stream:
                pointer = stream.read(4097)
            if len(pointer) > 4096 or not pointer.startswith('gitdir: ') or '\n' in pointer.rstrip('\r\n'):
                raise ValueError('invalid gitdir pointer')
            destination = pointer[len('gitdir: '):].strip()
            if not destination:
                raise ValueError('empty gitdir pointer')
            git_dir = Path(destination)
            metadata = git_dir if git_dir.is_absolute() else root / git_dir
            paths.append(metadata)
        except (OSError, UnicodeError, ValueError) as exc:
            raise DevError('WORKTREE_METADATA_UNAVAILABLE', 'Cannot safely identify Git metadata before worktree creation', 409) from exc
    common = metadata / 'commondir'
    if common.is_file():
        try:
            with common.open('r', encoding='utf-8') as stream:
                pointer = stream.read(4097)
            if len(pointer) > 4096 or not pointer.strip() or '\n' in pointer.rstrip('\r\n'):
                raise ValueError('invalid common directory pointer')
            common_dir = Path(pointer.strip())
            paths.append(common_dir if common_dir.is_absolute() else metadata / common_dir)
        except (OSError, UnicodeError, ValueError) as exc:
            raise DevError('WORKTREE_METADATA_UNAVAILABLE', 'Cannot safely identify shared Git metadata', 409) from exc
    configured = engine.config.get('integrations', {}).get('worktree_directory', '')
    base = Path(configured).expanduser() if configured else root.parent / 'CodePier-Worktrees'
    if not base.is_absolute():
        raise DevError('WORKTREE_DIRECTORY', 'Worktree directory must be absolute')
    parent = base / hashlib.sha256(str(root).encode()).hexdigest()[:16]
    # Legacy callers without an operation identity conservatively coordinate
    # the project-specific parent. The runner supplies the exact operation ID.
    target = parent / operation_name(operation_id) if operation_id is not None else parent
    paths.append(target)
    return [Claim('agent', 'path', canonical_path(root), False, 'process', True),
            *[Claim('agent', 'path', path, True, 'process')
              for path in dict.fromkeys(canonical_path(path) for path in paths)]]


def claims_for(engine, tool, project, args, root, operation_id=None):
    if tool in SCOPED_READ_TOOLS:
        return [Claim('agent', 'path', canonical_path(engine.path(root, args.get('path') or '.')), False)]
    if tool == 'worktrees_create':
        return _worktree_creation_claims(engine, root, operation_id)
    if tool == 'project_checkpoint':
        # Checkpoint writes only operation-private journal storage. Its stable
        # source read freezes file writes without a source-root writer.
        return [Claim('agent', 'path', canonical_path(root), False, 'process', True)]
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
        return [Claim('agent', 'path', canonical_path(root), not task.get('allow_read_concurrency', False), 'process', True)]
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
        claims.append(Claim(namespace, resource['kind'], name, resource['mode'] == 'write', 'process', resource['mode'] == 'read_stable'))
    return claims

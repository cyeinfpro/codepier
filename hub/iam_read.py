"""Ephemeral authorization snapshots for synchronous read-only call chains.

Not an HTTP/session cache: a scope is discarded before any await can occur.
Membership, identity freshness and policy are reloaded by every subsequent call,
stream item and post-await check. Writes invalidate even an enclosing snapshot.
"""
from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from functools import wraps
from inspect import iscoroutinefunction, isgeneratorfunction, isasyncgenfunction

_active = ContextVar('codepier_authorization_read', default=None)


class ReadScope:
    def __init__(self, store):
        self.store = store
        self.active = True
        self.revision = store.db.total_changes
        self.memo = {}

    def current(self):
        revision = self.store.db.total_changes
        if revision != self.revision:
            self.memo.clear()
            self.revision = revision
        return self.memo


def memo(store):
    scope = _active.get()
    return scope.current() if scope and scope.active and scope.store is store else None


@contextmanager
def read_scope(store):
    if not getattr(store, 'iam_enabled', False):
        yield
        return
    existing = _active.get()
    if existing and existing.active and existing.store is store:
        yield
        return
    with store.lock:
        scope = ReadScope(store)
        token = _active.set(scope)
        try:
            yield
        finally:
            # Also invalidate references copied into a newly created task's
            # Context. No task may retain the caller's authorization snapshot.
            scope.active = False
            scope.memo.clear()
            _active.reset(token)


def read_decision(function):
    if iscoroutinefunction(function) or isgeneratorfunction(function) or isasyncgenfunction(function):
        raise TypeError('Authorization read scopes cannot wrap asynchronous or streaming functions')

    @wraps(function)
    def wrapped(first, *args, **kwargs):
        store = getattr(first, 'store', None)
        if store is None:
            store = getattr(getattr(first, 'runtime', None), 'store', first)
        with read_scope(store):
            return function(first, *args, **kwargs)
    return wrapped


def principal_key(principal):
    return ('principal', principal.user_id, principal.space_id, principal.grant_id,
            principal.profile_id, principal.role_id, principal.authorization_mode,
            principal.user_epoch, principal.identity_id, principal.actor,
            principal.admin, principal.instance_admin,
            frozenset(principal.scopes), frozenset(principal.projects))

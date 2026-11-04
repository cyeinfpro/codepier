"""Request-local read projection reuse; no authorization survives a request.

Only synchronous projection entry points establish a scope. Every Store write
invalidates memoized values, including writes rolled back within the request.
Nested views share the same bounded cache; a later request always reauthorizes.
"""
from contextvars import ContextVar
from copy import deepcopy
from functools import wraps

_current = ContextVar('collaboration_projection_cache', default=None)


def projection(function):
    @wraps(function)
    def wrapped(self, *args, **kwargs):
        if _current.get() is not None:
            return function(self, *args, **kwargs)
        token = _current.set({})
        try:
            # Hold the existing reentrant Store boundary for a coherent,
            # synchronous read projection and total_changes generation.
            with self.store.lock:
                return function(self, *args, **kwargs)
        finally:
            _current.reset(token)
    return wrapped


def memoized(function):
    @wraps(function)
    def wrapped(self, *args, **kwargs):
        cache = _current.get()
        if cache is None:
            return function(self, *args, **kwargs)
        store = self.store
        key = (id(self), function.__qualname__, store.db.total_changes,
               store.session_revision, repr(args), repr(sorted(kwargs.items())))
        if key in cache:
            return deepcopy(cache[key])
        value = function(self, *args, **kwargs)
        if len(cache) < 2048:
            cache[key] = deepcopy(value)
        return value
    return wrapped


@memoized
def project(collaboration, project_id, principal):
    return collaboration.runtime.project(project_id, principal)

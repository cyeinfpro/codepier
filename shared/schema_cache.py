"""Bounded singleflight for pure, version-pinned Pydantic schema generation.

Only code-defined schemas are cached. No user, grant, project, file result or
authorization decision enters this cache. Each caller receives its own copy.
"""
from __future__ import annotations

from collections import OrderedDict
from copy import deepcopy
from threading import RLock

_LIMIT = 256
_LOCK = RLock()
_CACHE = OrderedDict()


def schema_for(subject):
    """Support BaseModel classes and TypeAdapter instances after model rebuild."""
    method = getattr(subject, 'model_json_schema', None) or subject.json_schema
    generation = getattr(subject, '__pydantic_core_schema__', None)
    if generation is None:
        generation = getattr(subject, 'core_schema', None)
    if generation is None:
        return method()  # Unknown builders are never treated as version-pinned.
    key = (subject, getattr(method, '__func__', method))
    with _LOCK:
        entry = _CACHE.get(key)
        if entry is None or entry[0] is not generation:
            value = method()
            _CACHE[key] = (generation, value)
        else:
            value = entry[1]
        _CACHE.move_to_end(key)
        while len(_CACHE) > _LIMIT:
            _CACHE.popitem(last=False)
        return deepcopy(value)

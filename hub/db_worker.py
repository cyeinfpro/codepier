"""Run complete synchronous authorization/transaction phases off the ASGI loop."""
from __future__ import annotations

import asyncio
import inspect
from functools import wraps


async def run_db(store, function, /, *args, **kwargs):
    # Production uses Store's dedicated, cancellation-draining worker. The
    # fallback supports small read-only service doubles with no Hub database.
    runner = getattr(store, "run", None)
    if runner is not None:
        return await runner(function, *args, **kwargs)
    return await asyncio.to_thread(function, *args, **kwargs)


def database_endpoint(store):
    """Keep the async endpoint contract while the entire body runs as one job.

    The decorated function must not await or manipulate loop-owned objects.
    Request authentication and dependent writes remain in the SAME job.
    """
    def decorate(function):
        if inspect.iscoroutinefunction(function):
            raise TypeError("database_endpoint requires a synchronous function")

        @wraps(function)
        async def endpoint(*args, **kwargs):
            return await run_db(store, function, *args, **kwargs)

        endpoint.__signature__ = inspect.signature(function, eval_str=True)
        return endpoint
    return decorate

"""Await owned asynchronous or disk work through repeated cancellation."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import ParamSpec, TypeVar

P = ParamSpec("P")
T = TypeVar("T")


async def settle_task(work: asyncio.Task[T]) -> T:
    """Cancellation cannot abandon a worker that still owns resources."""
    try:
        return await asyncio.shield(work)
    except asyncio.CancelledError:
        settled = asyncio.gather(work, return_exceptions=True)
        while not settled.done():
            try:
                await asyncio.shield(settled)
            except asyncio.CancelledError:
                continue
        raise


async def disk_call(function: Callable[P, T], *args: P.args, **kwargs: P.kwargs) -> T:
    return await settle_task(asyncio.create_task(asyncio.to_thread(function, *args, **kwargs)))

"""Stop work when the HTTP client disconnects.

The tool loops previously kept calling upstream models and MCP tools after the
caller had gone away. ``run_until_disconnected`` races the work against a
polling watcher and cancels the work when the client disappears.
"""

from __future__ import annotations

import asyncio
import contextlib
from typing import Any, Awaitable, TypeVar

T = TypeVar("T")

DEFAULT_DISCONNECT_POLL_SECONDS = 0.1


class ClientDisconnected(Exception):
    """The caller closed the connection while work was still running."""


async def _watch_for_disconnect(
    request: Any, interval: float = DEFAULT_DISCONNECT_POLL_SECONDS
) -> None:
    while True:
        if await request.is_disconnected():
            return
        await asyncio.sleep(interval)


async def run_until_disconnected(
    awaitable: Awaitable[T],
    request: Any,
    *,
    poll_interval: float = DEFAULT_DISCONNECT_POLL_SECONDS,
) -> T:
    """Await ``awaitable``, but cancel it if ``request`` disconnects first."""
    if request is None or not hasattr(request, "is_disconnected"):
        # Direct callers and tests often have no HTTP request object.
        return await awaitable

    task = asyncio.ensure_future(awaitable)
    watcher = asyncio.create_task(_watch_for_disconnect(request, poll_interval))
    try:
        done, _ = await asyncio.wait(
            {task, watcher}, return_when=asyncio.FIRST_COMPLETED
        )
        if watcher in done and not task.done():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
            raise ClientDisconnected()
        watcher.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await watcher
        return await task
    finally:
        for pending in (task, watcher):
            if not pending.done():
                pending.cancel()


__all__ = [
    "ClientDisconnected",
    "DEFAULT_DISCONNECT_POLL_SECONDS",
    "run_until_disconnected",
]

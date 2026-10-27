"""ASGI entry point for the hosted web server.

Resolves the boat from the Host header (see ``registry.resolve_boat``) and hands
the request, unchanged, to that boat's own app from the pool. ``/healthz`` is
answered here so nginx/systemd checks work with no boats configured.
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from typing import TYPE_CHECKING

from loguru import logger
from starlette.responses import PlainTextResponse

from helmlog.hosting.registry import resolve_boat

if TYPE_CHECKING:
    from starlette.types import Receive, Scope, Send

    from helmlog.hosting.pool import BoatAppPool
    from helmlog.hosting.registry import Boat, BoatRegistry

_EVICT_INTERVAL_S = 60.0
_REREAD_S = 5.0


class HostedDispatcher:
    def __init__(self, registry: BoatRegistry, pool: BoatAppPool, base_domain: str | None) -> None:
        self._registry = registry
        self._pool = pool
        self._base_domain = base_domain
        self._boats: list[Boat] = []
        self._boats_mtime: int | None = None
        self._boats_read_at = 0.0
        self._evictor: asyncio.Task[None] | None = None

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "lifespan":
            await self._lifespan(receive, send)
            return
        if scope["type"] == "http" and scope["path"] == "/healthz":
            await PlainTextResponse("ok")(scope, receive, send)
            return

        boat = resolve_boat(await self._current_boats(), _host(scope), self._base_domain)
        if boat is None or boat.db_path is None:
            if scope["type"] == "websocket":
                await send({"type": "websocket.close", "code": 1008})
            else:
                await PlainTextResponse("No boat is hosted at this address", 404)(
                    scope, receive, send
                )
            return

        entry = await self._pool.acquire(boat)
        try:
            await entry.app(scope, receive, send)
        finally:
            await self._pool.release(entry)

    async def _current_boats(self) -> list[Boat]:
        # The CLI edits the registry while the server runs. Re-read it when the
        # file changes, and every few seconds regardless, since two edits can
        # share one coarse filesystem timestamp tick.
        mtime = self._registry.mtime_ns()
        now = time.monotonic()
        if mtime != self._boats_mtime or now - self._boats_read_at > _REREAD_S:
            self._boats = await asyncio.to_thread(self._registry.list)
            self._boats_mtime = mtime
            self._boats_read_at = now
        return self._boats

    async def _lifespan(self, receive: Receive, send: Send) -> None:
        while True:
            message = await receive()
            if message["type"] == "lifespan.startup":
                self._evictor = asyncio.create_task(self._evict_loop())
                await send({"type": "lifespan.startup.complete"})
            elif message["type"] == "lifespan.shutdown":
                if self._evictor is not None:
                    self._evictor.cancel()
                    with contextlib.suppress(asyncio.CancelledError):
                        await self._evictor
                await self._pool.close_all()
                await send({"type": "lifespan.shutdown.complete"})
                return

    async def _evict_loop(self) -> None:
        while True:
            await asyncio.sleep(_EVICT_INTERVAL_S)
            try:
                await self._pool.evict_idle()
            except Exception:  # noqa: BLE001 — keep evicting on later ticks
                logger.exception("Idle boat eviction failed")


def _host(scope: Scope) -> str | None:
    for name, value in scope.get("headers", []):
        if name == b"host":
            return bytes(value).decode("latin-1")
    return None

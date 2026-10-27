"""Per-boat app pool for the hosted web server.

Each boat gets an ordinary ``create_app(storage, hosted=True)`` bound to its own
``Storage``. Apps open on first request, close after ``idle_s`` with no traffic,
and are replaced when the registry points the boat at a newly imported file.
A replaced or idle app is only closed once its in-flight requests (including
open WebSockets) have finished.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING

from loguru import logger

from helmlog.storage import Storage, StorageConfig

if TYPE_CHECKING:
    from starlette.types import ASGIApp

    from helmlog.hosting.registry import Boat, BoatRegistry


@dataclass(eq=False)
class PoolEntry:
    slug: str
    db_path: str
    storage: Storage
    app: ASGIApp
    last_used: float
    inflight: int = 0
    retired: bool = False


class BoatAppPool:
    def __init__(self, registry: BoatRegistry, idle_s: float = 900.0) -> None:
        self._registry = registry
        self._idle_s = idle_s
        self._entries: dict[str, PoolEntry] = {}
        self._lock = asyncio.Lock()

    def open_slugs(self) -> list[str]:
        return sorted(self._entries)

    async def acquire(self, boat: Boat) -> PoolEntry:
        """Return the boat's app, opening or swapping it as needed. Pair with release()."""
        if boat.db_path is None:
            raise ValueError(f"Boat {boat.slug} has no imported data")
        async with self._lock:
            entry = self._entries.get(boat.slug)
            if entry is not None and entry.db_path != boat.db_path:
                logger.info("Boat {} has a new database; switching over", boat.slug)
                del self._entries[boat.slug]
                entry.retired = True
                if entry.inflight == 0:
                    await _close(entry)
                entry = None
            if entry is None:
                entry = await self._open(boat.slug, boat.db_path)
                self._entries[boat.slug] = entry
            entry.inflight += 1
            entry.last_used = time.monotonic()
            return entry

    async def release(self, entry: PoolEntry) -> None:
        async with self._lock:
            entry.inflight -= 1
            entry.last_used = time.monotonic()
            if entry.retired and entry.inflight == 0:
                await _close(entry)

    async def evict_idle(self, now: float | None = None) -> list[str]:
        """Close boats with no in-flight requests for longer than idle_s."""
        now = time.monotonic() if now is None else now
        evicted: list[str] = []
        async with self._lock:
            for slug, entry in list(self._entries.items()):
                if entry.inflight == 0 and now - entry.last_used > self._idle_s:
                    del self._entries[slug]
                    await _close(entry)
                    evicted.append(slug)
        return evicted

    async def close_all(self) -> None:
        async with self._lock:
            for entry in self._entries.values():
                await _close(entry)
            self._entries.clear()

    async def _open(self, slug: str, db_path: str) -> PoolEntry:
        from helmlog.web import create_app

        storage = Storage(StorageConfig(db_path=db_path))
        await storage.connect()
        app = create_app(storage, hosted=True, videos_dir=self._registry.videos_dir(slug))
        logger.info("Opened boat {} ({})", slug, db_path)
        return PoolEntry(
            slug=slug, db_path=db_path, storage=storage, app=app, last_used=time.monotonic()
        )


async def _close(entry: PoolEntry) -> None:
    try:
        await entry.storage.close()
    except Exception:  # noqa: BLE001 — closing must not take the server down
        logger.exception("Error closing storage for boat {}", entry.slug)
    else:
        logger.info("Closed boat {}", entry.slug)

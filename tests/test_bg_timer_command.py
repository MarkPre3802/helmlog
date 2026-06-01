"""Tests for POST /api/race-start/bg-timer-command."""

from __future__ import annotations

import os
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

from helmlog.web import create_app

if TYPE_CHECKING:
    from helmlog.storage import Storage


def _make_writer(*, available: bool = True) -> MagicMock:
    writer = MagicMock()
    if available:
        writer.send = AsyncMock()
    else:
        writer.send = AsyncMock(side_effect=RuntimeError("CAN bus not available"))
    return writer


@pytest.mark.asyncio
async def test_start_command_sent(storage: Storage) -> None:
    writer = _make_writer()
    app = create_app(storage, can_writer=writer)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as c:
        resp = await c.post("/api/race-start/bg-timer-command", json={"command": "start"})
    assert resp.status_code == 200
    writer.send.assert_awaited_once_with("start", None)


@pytest.mark.asyncio
async def test_stop_command_sent(storage: Storage) -> None:
    writer = _make_writer()
    app = create_app(storage, can_writer=writer)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as c:
        resp = await c.post("/api/race-start/bg-timer-command", json={"command": "stop"})
    assert resp.status_code == 200
    writer.send.assert_awaited_once_with("stop", None)


@pytest.mark.asyncio
async def test_set_command_sent_with_minutes(storage: Storage) -> None:
    writer = _make_writer()
    app = create_app(storage, can_writer=writer)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as c:
        resp = await c.post("/api/race-start/bg-timer-command", json={"command": "set", "minutes": 5})
    assert resp.status_code == 200
    writer.send.assert_awaited_once_with("set", 5)


@pytest.mark.asyncio
async def test_set_without_minutes_returns_422(storage: Storage) -> None:
    writer = _make_writer()
    app = create_app(storage, can_writer=writer)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as c:
        resp = await c.post("/api/race-start/bg-timer-command", json={"command": "set"})
    assert resp.status_code == 422
    writer.send.assert_not_awaited()


@pytest.mark.asyncio
async def test_503_when_can_writer_unavailable(storage: Storage) -> None:
    app = create_app(storage, can_writer=None)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as c:
        resp = await c.post("/api/race-start/bg-timer-command", json={"command": "start"})
    assert resp.status_code == 503

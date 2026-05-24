"""Tests for scripts/simrad_timer_sk.py — decoder logic and HelmLog publish behaviour."""

from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

# The script lives in scripts/, not src/helmlog/, so add it to path.
sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))

import simrad_timer_sk as sut  # noqa: E402


# ── decoder tests ─────────────────────────────────────────────────────────────

class TestDecodeStartStop:
    def _payload(self, cmd_byte: int) -> bytes:
        return bytes([0x41, 0x9F, 0xFF, 0xFF, 0x01, 0x17, cmd_byte, 0x00, 0xFF, 0xFF, 0xFF, 0xFF])

    def test_start(self) -> None:
        assert sut.decode_start_stop(self._payload(0x3D)) is sut.TimerAction.START

    def test_stop(self) -> None:
        assert sut.decode_start_stop(self._payload(0x3E)) is sut.TimerAction.STOP

    def test_nearest_minute(self) -> None:
        assert sut.decode_start_stop(self._payload(0x3F)) is sut.TimerAction.NEAREST_MINUTE

    def test_reset(self) -> None:
        assert sut.decode_start_stop(self._payload(0x40)) is sut.TimerAction.RESET

    def test_unknown_command_returns_none(self) -> None:
        assert sut.decode_start_stop(self._payload(0x00)) is None

    def test_wrong_manufacturer_returns_none(self) -> None:
        bad = bytes([0x00, 0x00, 0xFF, 0xFF, 0x01, 0x17, 0x3D, 0x00])
        assert sut.decode_start_stop(bad) is None

    def test_too_short_returns_none(self) -> None:
        assert sut.decode_start_stop(bytes([0x41, 0x9F, 0xFF])) is None


class TestDecodeSetTimer:
    _DISCRIMINATOR = bytes([0x07, 0x42, 0x00, 0x01])

    def _payload(self, minutes: int) -> bytes:
        return bytes([0x41, 0x9F, 0xFF, 0xFF, 0xFF, 0xFF]) + self._DISCRIMINATOR + bytes([minutes, 0xFF, 0xFF, 0xFF])

    def test_set_5_minutes(self) -> None:
        assert sut.decode_set_timer(self._payload(5)) == 5

    def test_set_10_minutes(self) -> None:
        assert sut.decode_set_timer(self._payload(10)) == 10

    def test_broadcast_discriminator_ignored(self) -> None:
        # Running-state broadcast has 02 00 00 01 at [6:10] — must be ignored.
        broadcast = bytes([0x41, 0x9F, 0xFF, 0xFF, 0xFF, 0xFF, 0x02, 0x00, 0x00, 0x01, 0x9B, 0x09, 0x00, 0x00])
        assert sut.decode_set_timer(broadcast) is None

    def test_wrong_manufacturer_returns_none(self) -> None:
        bad = bytes([0x00, 0x00]) + bytes(12)
        assert sut.decode_set_timer(bad) is None

    def test_too_short_returns_none(self) -> None:
        assert sut.decode_set_timer(bytes([0x41, 0x9F, 0xFF, 0xFF, 0xFF, 0xFF])) is None


# ── publisher tests ───────────────────────────────────────────────────────────

def _make_publisher() -> tuple[sut.HelmLogPublisher, AsyncMock]:
    """Return a HelmLogPublisher wired to a mock httpx.AsyncClient."""
    pub = sut.HelmLogPublisher.__new__(sut.HelmLogPublisher)
    pub._url = "http://test/api/internal/timer-event"  # type: ignore[attr-defined]
    pub._headers = {}  # type: ignore[attr-defined]
    mock_response = MagicMock()
    mock_response.raise_for_status = MagicMock()
    mock_response.elapsed = MagicMock()
    mock_response.elapsed.microseconds = 0
    mock_client = AsyncMock()
    mock_client.post = AsyncMock(return_value=mock_response)
    pub._client = mock_client  # type: ignore[attr-defined]
    return pub, mock_client


def _posted_body(mock_client: AsyncMock) -> dict:
    return mock_client.post.call_args.kwargs["json"]


def _ts() -> datetime:
    return datetime(2026, 5, 18, 17, 0, 0, tzinfo=timezone.utc)


@pytest.mark.asyncio
async def test_publish_start_sends_running() -> None:
    pub, client = _make_publisher()
    event = sut.TimerEvent(action=sut.TimerAction.START, minutes=None, timestamp=_ts())
    await pub.publish(event)
    body = _posted_body(client)
    assert body["path"] == sut.SK_PATH_STATE
    assert body["value"] == "running"


@pytest.mark.asyncio
async def test_publish_stop_sends_stopped() -> None:
    pub, client = _make_publisher()
    event = sut.TimerEvent(action=sut.TimerAction.STOP, minutes=None, timestamp=_ts())
    await pub.publish(event)
    body = _posted_body(client)
    assert body["path"] == sut.SK_PATH_STATE
    assert body["value"] == "stopped"


@pytest.mark.asyncio
async def test_publish_nearest_minute_sends_state() -> None:
    pub, client = _make_publisher()
    event = sut.TimerEvent(action=sut.TimerAction.NEAREST_MINUTE, minutes=None, timestamp=_ts())
    await pub.publish(event)
    body = _posted_body(client)
    assert body["path"] == sut.SK_PATH_STATE
    assert body["value"] == "nearest-minute"


@pytest.mark.asyncio
async def test_publish_reset_sends_state() -> None:
    pub, client = _make_publisher()
    event = sut.TimerEvent(action=sut.TimerAction.RESET, minutes=None, timestamp=_ts())
    await pub.publish(event)
    body = _posted_body(client)
    assert body["path"] == sut.SK_PATH_STATE
    assert body["value"] == "reset"


@pytest.mark.asyncio
async def test_publish_set_sends_duration_in_seconds() -> None:
    pub, client = _make_publisher()
    event = sut.TimerEvent(action=sut.TimerAction.SET, minutes=5, timestamp=_ts())
    await pub.publish(event)
    body = _posted_body(client)
    assert body["path"] == sut.SK_PATH_DURATION
    assert body["value"] == 300  # 5 min × 60


@pytest.mark.asyncio
async def test_publish_set_6_minutes() -> None:
    pub, client = _make_publisher()
    event = sut.TimerEvent(action=sut.TimerAction.SET, minutes=6, timestamp=_ts())
    await pub.publish(event)
    body = _posted_body(client)
    assert body["path"] == sut.SK_PATH_DURATION
    assert body["value"] == 360  # 6 min × 60

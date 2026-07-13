"""Tests for CANWriter (src/helmlog/can_writer.py) — outbound B&G commands.

Frames are reassembled with FastPacketBuffer and decoded with
helmlog.nmea2000.decode() so these tests validate the writer against the
same decoder the inbound bridge uses — a round-trip check, not just a
byte-offset assertion.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from helmlog.can_writer import CANWriter
from helmlog.nmea2000 import PGN_SIMRAD_START_STOP, FastPacketBuffer, SimradTimerRecord, decode

_SOURCE_ADDR = 0x7E  # CANWriter's claimed address


def _writer_with_mock_bus() -> tuple[CANWriter, MagicMock]:
    writer = CANWriter()
    writer._bus = MagicMock()
    return writer, writer._bus


def _decode_sent_command(bus: MagicMock, pgn: int) -> SimradTimerRecord | None:
    buf = FastPacketBuffer()
    result: SimradTimerRecord | None = None
    for call in bus.send.call_args_list:
        frame = call.args[0]
        payload = buf.feed(pgn, _SOURCE_ADDR, bytes(frame.data))
        if payload is not None:
            decoded = decode(pgn, payload, _SOURCE_ADDR, 0.0)
            assert isinstance(decoded, SimradTimerRecord)
            result = decoded
    return result


@pytest.mark.asyncio
async def test_send_boat_end_ping() -> None:
    writer, bus = _writer_with_mock_bus()
    await writer.send("boat_end_ping")
    record = _decode_sent_command(bus, PGN_SIMRAD_START_STOP)
    assert record is not None
    assert record.action == "boat_end_ping"


@pytest.mark.asyncio
async def test_send_pin_end_ping() -> None:
    writer, bus = _writer_with_mock_bus()
    await writer.send("pin_end_ping")
    record = _decode_sent_command(bus, PGN_SIMRAD_START_STOP)
    assert record is not None
    assert record.action == "pin_end_ping"


@pytest.mark.asyncio
async def test_send_without_bus_raises() -> None:
    writer = CANWriter()
    with pytest.raises(RuntimeError):
        await writer.send("boat_end_ping")

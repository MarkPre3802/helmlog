#!/usr/bin/env python3
"""
Simrad Race Timer NMEA 2000 → Signal K bridge.

Reads Simrad/B&G countdown timer CAN frames, reassembles Fast Packets,
decodes the timer commands, and publishes the results to Signal K via
WebSocket delta messages.

PGNs decoded
  130845  (0x1FF1D)  Set Timer      – sets countdown duration in minutes
  130850  (0x1FF22)  Start/Stop     – starts or stops the countdown

Signal K paths written (under vessels.self)
  racing.startTimer.secondsRemaining  – duration in seconds  (on SET)
  racing.startTimer.state             – "running" | "stopped"

Payload layout (from candump analysis, Simrad mfr code 0x9F41)
  Start/Stop/Reset/Nearest-Minute  (PGN 130850, 12 bytes):
    [0-1]  41 9F   Simrad manufacturer ID
    [2-3]  FF FF   reserved
    [4-5]  01 17   reserved
    [6]    3D=start / 3E=stop / 3F=nearest-full-minute / 40=reset
    [7-11] padding

  Set Timer  (PGN 130845, 14 bytes):
    [0-1]  41 9F   Simrad manufacturer ID
    [2-5]  FF FF FF FF  reserved
    [6-9]  07 42 00 01  SET command discriminator (broadcast has 02 00 00 01)
    [10]   minutes (0x03 / 0x04 / 0x05 …)
    [11-13] padding

  Running-state broadcast  (also PGN 130845, sent on start and every ~30 s):
    [0-1]  41 9F   Simrad manufacturer ID
    [2-5]  FF FF FF FF  reserved
    [6-9]  02 00 00 01  discriminator — not a SET command; ignored
    [10-11] remaining time (encoding TBD)
    [12-13] padding

Usage
  python simrad_timer_sk.py [--channel can0] [--signalk http://localhost:3000]

Requirements
  pip install python-can websockets
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Optional

import can
from websockets.asyncio.client import ClientConnection
from websockets.asyncio.client import connect as _ws_connect

log = logging.getLogger(__name__)

# ── PGNs ──────────────────────────────────────────────────────────────────────
PGN_SET_TIMER  = 130845   # 0x1FF1D  CAN ID pattern: 0x_DFF1D__
PGN_START_STOP = 130850   # 0x1FF22  CAN ID pattern: 0x_9FF22__

TARGET_PGNS = frozenset({PGN_SET_TIMER, PGN_START_STOP})

# ── Simrad manufacturer ID (bytes 0-1 of each payload, little-endian 0x9F41) ─
MFR_B0 = 0x41
MFR_B1 = 0x9F

# ── Command byte at payload[6] in PGN 130850 ──────────────────────────────────
CMD_START          = 0x3D
CMD_STOP           = 0x3E
CMD_NEAREST_MINUTE = 0x3F
CMD_RESET          = 0x40

# ── Signal K paths ────────────────────────────────────────────────────────────
SK_PATH_SECONDS = "racing.startTimer.secondsRemaining"
SK_PATH_STATE   = "racing.startTimer.state"


# ── Data model ────────────────────────────────────────────────────────────────

class TimerAction(Enum):
    START          = "start"
    STOP           = "stop"
    RESET          = "reset"
    NEAREST_MINUTE = "nearest_minute"
    SET            = "set"


@dataclass
class TimerEvent:
    action:    TimerAction
    minutes:   Optional[int]   # populated only for SET
    timestamp: datetime


# ── NMEA 2000 helpers ─────────────────────────────────────────────────────────

def pgn_from_can_id(can_id: int) -> int:
    """Extract PGN from a 29-bit NMEA 2000 / J1939 CAN extended ID."""
    dp = (can_id >> 24) & 0x1
    pf = (can_id >> 16) & 0xFF
    ps = (can_id >> 8)  & 0xFF
    # PDU2 (PF >= 0xF0): PS is Group Extension — included in the PGN.
    # PDU1 (PF <  0xF0): PS is the destination address — not part of PGN.
    return (dp << 16) | (pf << 8) | (ps if pf >= 0xF0 else 0)


def source_addr(can_id: int) -> int:
    return can_id & 0xFF


class FastPacketBuffer:
    """Reassembles NMEA 2000 Fast Packet multi-frame payloads.

    Keyed on (pgn, source_address, sequence_number) so concurrent streams
    from different ECUs on the same PGN never collide.
    """

    def __init__(self) -> None:
        self._sessions: dict[tuple[int, int, int], dict] = {}

    def feed(self, pgn: int, sa: int, raw: bytes) -> Optional[bytes]:
        """Feed one CAN frame.  Returns the complete payload once all frames
        have arrived, otherwise None."""
        if len(raw) < 2:
            return None

        seq   = (raw[0] >> 5) & 0x7
        frame = raw[0] & 0x1F
        key   = (pgn, sa, seq)

        if frame == 0:
            total = raw[1]
            self._sessions[key] = {
                "total": total,
                "data":  bytearray(raw[2:]),
                "next":  1,
            }
            if total <= 6:  # fits entirely in the first frame
                return bytes(self._sessions.pop(key)["data"][:total])

        else:
            s = self._sessions.get(key)
            if s is None or s["next"] != frame:
                self._sessions.pop(key, None)
                return None
            s["data"].extend(raw[1:])
            s["next"] += 1
            if len(s["data"]) >= s["total"]:
                return bytes(self._sessions.pop(key)["data"][:s["total"]])

        return None


# ── Payload decoders ──────────────────────────────────────────────────────────

def _is_simrad(p: bytes) -> bool:
    return len(p) >= 2 and p[0] == MFR_B0 and p[1] == MFR_B1


def decode_start_stop(payload: bytes) -> Optional[TimerAction]:
    """Return START, STOP, RESET, or NEAREST_MINUTE from a PGN-130850 payload, or None."""
    if not _is_simrad(payload) or len(payload) < 7:
        return None
    return {
        CMD_START:          TimerAction.START,
        CMD_STOP:           TimerAction.STOP,
        CMD_NEAREST_MINUTE: TimerAction.NEAREST_MINUTE,
        CMD_RESET:          TimerAction.RESET,
    }.get(payload[6])


_SET_TIMER_DISCRIMINATOR = bytes([0x07, 0x42, 0x00, 0x01])  # payload[6:10] for SET command


def decode_set_timer(payload: bytes) -> Optional[int]:
    """Return countdown minutes from a PGN-130845 payload, or None."""
    if not _is_simrad(payload) or len(payload) < 11:
        return None
    # The device also broadcasts running-state updates on this PGN (payload[6] == 0x02).
    # Only process genuine SET commands identified by the discriminator bytes at [6:10].
    if payload[6:10] != _SET_TIMER_DISCRIMINATOR:
        return None
    return int(payload[10])


# ── Signal K publisher ────────────────────────────────────────────────────────

class SignalKPublisher:
    """Publishes timer events to Signal K via WebSocket delta messages."""

    def __init__(self, base_url: str, vessel: str = "self", token: str = "") -> None:
        ws_base = base_url.rstrip("/").replace("http://", "ws://").replace("https://", "wss://")
        qs = "?subscribe=none" + (f"&token={token}" if token else "")
        self._uri         = ws_base + "/signalk/v1/stream" + qs
        self._vessel      = vessel
        self._ws: Optional[ClientConnection] = None
        self._set_seconds: int = 0               # last duration from SET command
        self._start_time: Optional[datetime] = None  # wall-clock time of last START/RESET

    async def __aenter__(self) -> "SignalKPublisher":
        await self._connect()
        return self

    async def __aexit__(self, *_: object) -> None:
        if self._ws is not None:
            await self._ws.close()
            self._ws = None

    async def _connect(self) -> None:
        self._ws = await _ws_connect(self._uri)
        log.info("Connected to Signal K WebSocket: %s", self._uri)

    async def publish(self, event: TimerEvent) -> None:
        ts = event.timestamp.strftime("%Y-%m-%dT%H:%M:%S.000Z")

        if event.action is TimerAction.SET:
            self._set_seconds = (event.minutes or 0) * 60
            await self._send(SK_PATH_SECONDS, self._set_seconds, ts)
            log.info("SET timer → %d min (%d s)", event.minutes, self._set_seconds)

        elif event.action is TimerAction.START:
            self._start_time = event.timestamp
            await self._send(SK_PATH_STATE, "running", ts)
            log.info("Timer STARTED")

        elif event.action is TimerAction.STOP:
            self._start_time = None
            await self._send(SK_PATH_STATE, "stopped", ts)
            log.info("Timer STOPPED")

        elif event.action is TimerAction.RESET:
            self._start_time = event.timestamp
            await self._send(SK_PATH_SECONDS, self._set_seconds, ts)
            await self._send(SK_PATH_STATE, "running", ts)
            log.info("Timer RESET → %d s", self._set_seconds)

        elif event.action is TimerAction.NEAREST_MINUTE:
            snapped = self._snap_to_nearest_minute(event.timestamp)
            self._start_time = event.timestamp  # reset elapsed from this new base
            self._set_seconds = snapped
            await self._send(SK_PATH_SECONDS, snapped, ts)
            await self._send(SK_PATH_STATE, "running", ts)
            log.info("Timer NEAREST MINUTE → %d s", snapped)

    def _snap_to_nearest_minute(self, now: datetime) -> int:
        """Return secondsRemaining rounded to the nearest whole minute."""
        if self._start_time is None or self._set_seconds == 0:
            return self._set_seconds
        elapsed = (now - self._start_time).total_seconds()
        remaining = max(0.0, self._set_seconds - elapsed)
        return round(remaining / 60) * 60

    async def _send(self, sk_path: str, value: object, timestamp: str) -> None:
        if self._ws is None:
            try:
                await self._connect()
            except Exception as exc:
                log.warning("Signal K reconnect failed: %s", exc)
                return
        assert self._ws is not None
        delta: dict[str, Any] = {
            "context": f"vessels.{self._vessel}",
            "updates": [{
                "source": {"label": "simrad-timer", "type": "NMEA2000"},
                "timestamp": timestamp,
                "values": [{"path": sk_path, "value": value}],
            }],
        }
        try:
            await self._ws.send(json.dumps(delta))
            log.debug("WS delta  %-45s = %s", sk_path, value)
        except Exception as exc:
            log.warning("Signal K send failed  path=%s  error=%s", sk_path, exc)
            self._ws = None


# ── CAN reader ────────────────────────────────────────────────────────────────

async def run(channel: str, publisher: SignalKPublisher) -> None:
    buf = FastPacketBuffer()

    bus      = can.Bus(channel=channel, interface="socketcan")
    reader   = can.AsyncBufferedReader()
    notifier = can.Notifier(bus, [reader])

    log.info("Listening on %s  target PGNs: %s", channel, sorted(TARGET_PGNS))

    try:
        async for msg in reader:
            if not msg.is_extended_id:
                continue

            cid = msg.arbitration_id
            pgn = pgn_from_can_id(cid)
            if pgn not in TARGET_PGNS:
                continue

            sa      = source_addr(cid)
            payload = buf.feed(pgn, sa, bytes(msg.data))
            if payload is None:
                continue

            log.debug("PGN %d  SA 0x%02X  payload: %s",
                      pgn, sa, payload.hex(" "))

            ts    = datetime.now(timezone.utc)
            event: Optional[TimerEvent] = None

            if pgn == PGN_START_STOP:
                action = decode_start_stop(payload)
                if action is not None:
                    event = TimerEvent(action=action, minutes=None, timestamp=ts)

            elif pgn == PGN_SET_TIMER:
                minutes = decode_set_timer(payload)
                if minutes is not None:
                    event = TimerEvent(action=TimerAction.SET, minutes=minutes, timestamp=ts)

            if event is not None:
                await publisher.publish(event)

    finally:
        notifier.stop()
        bus.shutdown()
        log.info("CAN bus closed")


# ── Entry point ───────────────────────────────────────────────────────────────

def main() -> None:
    ap = argparse.ArgumentParser(
        description="Bridge Simrad NMEA 2000 race timer → Signal K"
    )
    ap.add_argument(
        "--channel", default="can0",
        help="CAN interface name  (default: can0)",
    )
    ap.add_argument(
        "--signalk", default="http://localhost:3000",
        help="Signal K server base URL  (default: http://localhost:3000)",
    )
    ap.add_argument(
        "--vessel", default="self",
        help="Signal K vessel context  (default: self)",
    )
    ap.add_argument(
        "--token", default="", nargs="?", const="",
        help="Signal K bearer token (from SK Admin → Security → Access Requests)",
    )
    ap.add_argument(
        "--log-level", default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
    )
    args = ap.parse_args()

    logging.basicConfig(
        level=args.log_level,
        format="%(asctime)s  %(levelname)-8s  %(message)s",
        datefmt="%H:%M:%S",
    )

    async def amain() -> None:
        async with SignalKPublisher(args.signalk, args.vessel, args.token) as pub:
            await run(args.channel, pub)

    asyncio.run(amain())


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""
Simrad Race Timer NMEA 2000 → HelmLog bridge.

Reads Simrad/B&G countdown timer CAN frames, reassembles Fast Packets,
decodes the timer commands, and POSTs events directly to HelmLog.

PGNs decoded (via helmlog.nmea2000)
  130845  (0x1FF1D)  Set Timer      – sets countdown duration in minutes
  130850  (0x1FF22)  Start/Stop     – starts or stops the countdown

Usage
  python simrad_timer_sk.py [--channel can0] [--helmlog http://localhost:3002]

Requirements
  pip install python-can httpx
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import time

import can
import httpx

from helmlog.can_reader import extract_pgn
from helmlog.nmea2000 import (
    FAST_PACKET_PGNS,
    FastPacketBuffer,
    SimradTimerRecord,
    decode,
)

log = logging.getLogger(__name__)

# ── Signal K path constants (used as identifiers in the POST body) ─────────────
SK_PATH_STATE = "racing.startTimer.state"
SK_PATH_DURATION = "racing.startTimer.duration"

# Source address claimed by CANWriter in can_writer.py (_SA = 0x7E).
# Frames we transmit echo back on the bus; ignoring them prevents a feedback
# loop where our own boat/pin-end ping commands trigger _post_ping again.
_OWN_SA = 0x7E

# ── Action → SK path/value mapping ────────────────────────────────────────────
_ACTION_TO_POST: dict[str, tuple[str, object]] = {
    "start": (SK_PATH_STATE, "running"),
    "stop": (SK_PATH_STATE, "stopped"),
    "reset": (SK_PATH_STATE, "reset"),
    "nearest_minute": (SK_PATH_STATE, "nearest-minute"),
}

# ── Line-ping actions → existing manual ping endpoints ────────────────────────
# Reuses the same /api/race-start/ping/{end} routes the crew UI calls, rather
# than a separate internal endpoint — see docs/specs/simradtimerintegration.md.
_PING_ACTION_TO_END: dict[str, str] = {
    "boat_end_ping": "boat",
    "pin_end_ping": "pin",
}


# ── HelmLog direct publisher ──────────────────────────────────────────────────


class HelmLogPublisher:
    """Posts timer events directly to HelmLog, bypassing Signal K.

    Uses httpx.AsyncClient with a persistent connection so back-to-back
    events (e.g. SET immediately followed by START) don't each pay TCP
    connection-setup overhead.  The client is opened/closed via the async
    context manager.
    """

    def __init__(self, base_url: str, token: str = "", ping_token: str = "") -> None:
        self._base_url = base_url.rstrip("/")
        self._url = self._base_url + "/api/internal/timer-event"
        self._headers: dict[str, str] = {}
        if token:
            self._headers["Authorization"] = f"Bearer {token}"
        # Ping endpoints reuse the crew-auth ping routes, gated by a device
        # API key (see /admin/devices) rather than HELMLOG_TIMER_TOKEN.
        self._ping_headers: dict[str, str] = {}
        if ping_token:
            self._ping_headers["Authorization"] = f"Bearer {ping_token}"
        self._client: httpx.AsyncClient | None = None

    async def __aenter__(self) -> HelmLogPublisher:
        self._client = httpx.AsyncClient(timeout=2.0)
        return self

    async def __aexit__(self, *_: object) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    async def publish(self, record: SimradTimerRecord) -> None:
        if record.action in _PING_ACTION_TO_END:
            await self._post_ping(_PING_ACTION_TO_END[record.action])
            return

        ts = (
            record.timestamp.strftime("%Y-%m-%dT%H:%M:%S.")
            + f"{record.timestamp.microsecond // 1000:03d}Z"
        )
        if record.action == "set":
            await self._post(SK_PATH_DURATION, (record.minutes or 0) * 60, ts)
            log.info("SET timer → %d min", record.minutes)
        else:
            sk_path, value = _ACTION_TO_POST[record.action]
            await self._post(sk_path, value, ts)
            log.info("Timer %s", record.action.upper())

    async def _post(self, path: str, value: object, ts: str) -> None:
        assert self._client is not None, "use as async context manager"
        try:
            r = await self._client.post(
                self._url,
                json={"path": path, "value": value, "ts": ts},
                headers=self._headers,
            )
            r.raise_for_status()
            log.debug("POST %s = %s  (%d ms)", path, value, r.elapsed.microseconds // 1000)
        except httpx.HTTPError as exc:
            log.warning("HelmLog POST failed  path=%s  error=%s", path, exc)

    async def _post_ping(self, end: str) -> None:
        """POST to the existing manual ping endpoint with no lat/lon, so the
        server falls back to the latest GPS fix (storage.latest_position())."""
        assert self._client is not None, "use as async context manager"
        url = f"{self._base_url}/api/race-start/ping/{end}"
        try:
            r = await self._client.post(url, json={}, headers=self._ping_headers)
            r.raise_for_status()
            log.info("Line ping → %s end", end)
        except httpx.HTTPError as exc:
            log.warning("HelmLog ping POST failed  end=%s  error=%s", end, exc)


# ── CAN reader ────────────────────────────────────────────────────────────────


def _source_addr(can_id: int) -> int:
    return can_id & 0xFF


async def run(channel: str, publisher: HelmLogPublisher) -> None:
    buf = FastPacketBuffer()
    bus = can.Bus(channel=channel, interface="socketcan")
    loop = asyncio.get_running_loop()

    log.info("Listening on %s  target PGNs: %s", channel, sorted(FAST_PACKET_PGNS))

    try:
        while True:
            # bus.recv() is blocking; run it in the thread pool so the event
            # loop stays free for the httpx publishes happening concurrently.
            # can.Notifier + AsyncBufferedReader require the running loop to be
            # passed explicitly in Python 3.10+; this approach avoids that
            # fragility entirely.
            msg: can.Message | None = await loop.run_in_executor(None, bus.recv, 1.0)
            if msg is None:
                continue
            if not msg.is_extended_id:
                continue

            pgn = extract_pgn(msg.arbitration_id)
            if pgn not in FAST_PACKET_PGNS:
                continue

            sa = _source_addr(msg.arbitration_id)
            if sa == _OWN_SA:
                continue  # skip our own echoes — prevents boat/pin-end ping feedback loop
            payload = buf.feed(pgn, sa, bytes(msg.data))
            if payload is None:
                continue

            log.debug("PGN %d  SA 0x%02X  payload: %s", pgn, sa, payload.hex(" "))

            record = decode(pgn, payload, sa, msg.timestamp or time.time())
            if isinstance(record, SimradTimerRecord):
                await publisher.publish(record)
    finally:
        bus.shutdown()
        log.info("CAN bus closed")


# ── Entry point ───────────────────────────────────────────────────────────────


def main() -> None:
    ap = argparse.ArgumentParser(description="Bridge Simrad NMEA 2000 race timer → HelmLog")
    ap.add_argument(
        "--channel",
        default="can0",
        help="CAN interface name  (default: can0)",
    )
    ap.add_argument(
        "--helmlog",
        default="http://localhost:3002",
        help="HelmLog base URL  (default: http://localhost:3002)",
    )
    ap.add_argument(
        "--token",
        default="",
        nargs="?",
        const="",
        help="Bearer token matching HELMLOG_TIMER_TOKEN in HelmLog .env (optional)",
    )
    ap.add_argument(
        "--ping-token",
        default="",
        nargs="?",
        const="",
        help=(
            "Bearer token for a device API key (see /admin/devices) with crew role "
            "and scope covering POST /api/race-start/ping/* (optional)"
        ),
    )
    ap.add_argument(
        "--log-level",
        default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
    )
    args = ap.parse_args()

    logging.basicConfig(
        level=args.log_level,
        format="%(asctime)s  %(levelname)-8s  %(message)s",
        datefmt="%H:%M:%S",
    )

    async def amain() -> None:
        async with HelmLogPublisher(args.helmlog, args.token, args.ping_token) as pub:
            await run(args.channel, pub)

    asyncio.run(amain())


if __name__ == "__main__":
    main()

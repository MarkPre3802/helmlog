"""Probe GoPro/MP4 metadata and match it to a HelmLog session.

This is an experiment for aligning a camera recording to a race using the
file's embedded metadata (creation timestamp and GPS tags) and the HelmLog
session history.
"""

from __future__ import annotations

import json
import os
import re
import struct
import subprocess
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from loguru import logger


def _ts(value: str | None) -> datetime | None:
    if value is None:
        return None
    try:
        dt = datetime.fromisoformat(value)
    except ValueError:
        # ffprobe may emit ISO 8601 with a trailing Z
        if value.endswith("Z"):
            try:
                dt = datetime.fromisoformat(value[:-1])
                dt = dt.replace(tzinfo=UTC)
            except ValueError:
                return None
        else:
            return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


def _parse_location_string(value: str) -> tuple[float, float] | None:
    if not value:
        return None
    value = value.strip()

    # ISO6709-style: +47.123456-122.123456/ or -47.123456+122.123456/
    m = re.match(r"^(?P<lat>[+-]?\d+(?:\.\d+)?)(?P<lon>[+-]?\d+(?:\.\d+)?)(?:[+-]\d+(?:\.\d+)?/?)?$", value)
    if m:
        try:
            lat = float(m.group("lat"))
            lon = float(m.group("lon"))
        except ValueError:
            return None
        if -90.0 <= lat <= 90.0 and -180.0 <= lon <= 180.0:
            return lat, lon

    # Named latitude/longitude pairs.
    lat_match = re.search(r"lat(?:itude)?\s*[:=]\s*([+-]?\d+(?:\.\d+)?)", value, re.I)
    lon_match = re.search(r"lon(?:gitude)?\s*[:=]\s*([+-]?\d+(?:\.\d+)?)", value, re.I)
    if lat_match and lon_match:
        try:
            lat = float(lat_match.group(1))
            lon = float(lon_match.group(1))
        except ValueError:
            return None
        if -90.0 <= lat <= 90.0 and -180.0 <= lon <= 180.0:
            return lat, lon

    # Comma/space separated lat lon.
    parts = re.split(r"[\s,;]+", value)
    if len(parts) >= 2:
        try:
            lat = float(parts[0])
            lon = float(parts[1])
        except ValueError:
            return None
        if -90.0 <= lat <= 90.0 and -180.0 <= lon <= 180.0:
            return lat, lon

    return None


def _collect_tags(data: dict[str, Any]) -> dict[str, str]:
    tags: dict[str, str] = {}
    fmt = data.get("format") or {}
    if isinstance(fmt, dict):
        for key, value in (fmt.get("tags") or {}).items():
            if value is not None:
                tags[str(key)] = str(value)
    for stream in data.get("streams", []):
        if not isinstance(stream, dict):
            continue
        for key, value in (stream.get("tags") or {}).items():
            if value is not None:
                tags.setdefault(str(key), str(value))
    return tags


def _parse_gpsu(s: str) -> datetime | None:
    """Parse a GoPro GPSU timestamp string: YYMMDDHHMMSS[.sss]."""
    try:
        dt = datetime.strptime(s[:12], "%y%m%d%H%M%S")
        return dt.replace(tzinfo=UTC)
    except ValueError:
        return None


class GoProProbeError(RuntimeError):
    pass


@dataclass(frozen=True)
class GpsFix:
    """One GPS UTC timestamp extracted from GPMF telemetry."""

    utc: datetime


@dataclass(frozen=True)
class GoProVideo:
    path: Path
    duration_s: float | None = None
    creation_utc: datetime | None = None  # from file tags — often wrong on GoPro
    gps_position: tuple[float, float] | None = None
    tags: dict[str, str] = field(default_factory=dict)
    gpmf_track: list[GpsFix] = field(default_factory=list)
    gps_source: str = "none"  # "gpmf", "tags", "none"

    @property
    def start_utc(self) -> datetime | None:
        # GPMF GPS time is authoritative; file creation_utc is unreliable on GoPro.
        if self.gpmf_track:
            return self.gpmf_track[0].utc
        return self.creation_utc

    @property
    def end_utc(self) -> datetime | None:
        if self.gpmf_track:
            return self.gpmf_track[-1].utc
        if self.creation_utc is None or self.duration_s is None:
            return None
        return self.creation_utc + timedelta(seconds=self.duration_s)


def _find_gpmf_stream_index(streams: list[dict[str, Any]]) -> int | None:
    """Return the stream index of the GoPro GPMF telemetry track, if present."""
    for s in streams:
        tag = s.get("codec_tag_string", "")
        handler = s.get("tags", {}).get("handler_name", "")
        if tag == "gpmd" or "gopro met" in handler.lower():
            idx = s.get("index")
            if isinstance(idx, int):
                return idx
    return None


def _extract_gpmf_track(path: Path, stream_index: int) -> list[GpsFix]:
    """Extract GPS UTC fixes from the GPMF telemetry stream embedded in the MP4.

    Uses ffmpeg to dump the raw GPMF binary, then scans for GPSU records
    (type 'U') which carry the GPS clock time as "YYMMDDHHMMSS.sss".
    Only fixes with year >= 2020 are kept (earlier years = no GPS lock yet).
    """
    ffmpeg_cmd = os.environ.get("HELMLOG_FFMPEG", "ffmpeg")
    try:
        result = subprocess.run(
            [
                ffmpeg_cmd,
                "-y",
                "-i", str(path),
                "-map", f"0:{stream_index}",
                "-codec", "copy",
                "-f", "rawvideo",
                "pipe:1",
            ],
            check=True,
            capture_output=True,
            timeout=60,
        )
    except FileNotFoundError:
        logger.warning("ffmpeg not found; skipping GPMF GPS extraction")
        return []
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
        logger.warning("ffmpeg GPMF extraction failed: {}", exc)
        return []

    data = result.stdout
    fixes: list[GpsFix] = []
    i = 0
    while i < len(data) - 8:
        if data[i : i + 4] != b"GPSU":
            i += 1
            continue
        typ = data[i + 4]
        size = data[i + 5]
        repeat = struct.unpack(">H", data[i + 6 : i + 8])[0]
        payload_len = size * repeat
        payload = data[i + 8 : i + 8 + payload_len]
        total = 8 + payload_len
        if total % 4:
            total += 4 - (total % 4)
        i += total

        if typ != 0x55:  # 'U' = char/string
            continue
        ts_str = payload.decode("ascii", errors="replace").strip("\x00")
        dt = _parse_gpsu(ts_str)
        if dt and dt.year >= 2020:
            fixes.append(GpsFix(utc=dt))

    logger.debug("GPMF track: {} valid GPS fixes from stream {}", len(fixes), stream_index)
    return fixes


def probe_video(path: Path, timezone: str = "UTC") -> GoProVideo:
    if not path.exists():
        raise GoProProbeError(f"File does not exist: {path}")
    try:
        ffprobe_cmd = os.environ.get("HELMLOG_FFPROBE", "ffprobe")
        result = subprocess.run(
            [
                ffprobe_cmd,
                "-v",
                "quiet",
                "-print_format",
                "json",
                "-show_entries",
                "format=duration:format_tags:stream_tags:streams",
                str(path),
            ],
            check=True,
            capture_output=True,
            text=True,
            timeout=30,
        )
    except FileNotFoundError as exc:
        raise GoProProbeError("ffprobe is not installed or not on PATH") from exc
    except subprocess.CalledProcessError as exc:
        raise GoProProbeError(f"ffprobe failed: {exc.stderr.strip()}") from exc
    except subprocess.TimeoutExpired as exc:
        raise GoProProbeError("ffprobe timed out") from exc

    try:
        data = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise GoProProbeError("ffprobe returned invalid JSON") from exc

    tags = _collect_tags(data)
    duration_s = None
    fmt = data.get("format") or {}
    if isinstance(fmt, dict):
        duration = fmt.get("duration")
        if duration is not None:
            try:
                duration_s = float(duration)
            except (ValueError, TypeError):
                duration_s = None

    creation_time = tags.get("creation_time")
    creation_utc = None
    if creation_time is not None:
        parsed = _ts(creation_time)
        if parsed is None:
            try:
                naive = datetime.fromisoformat(creation_time)
            except ValueError:
                parsed = None
            else:
                tz = ZoneInfo(timezone)
                parsed = naive.replace(tzinfo=tz).astimezone(UTC)
        creation_utc = parsed

    gps_position = None
    for key in [
        "com.apple.quicktime.location.ISO6709",
        "location",
        "location-eng",
        "GPSLatitude",
        "GPSLongitude",
    ]:
        if key not in tags:
            continue
        if key == "GPSLatitude" or key == "GPSLongitude":
            continue
        gps_position = _parse_location_string(tags[key])
        if gps_position is not None:
            break
    if gps_position is None and "GPSLatitude" in tags and "GPSLongitude" in tags:
        try:
            gps_position = (float(tags["GPSLatitude"]), float(tags["GPSLongitude"]))
        except ValueError:
            gps_position = None

    # Extract GPMF GPS track for accurate GPS-clock timestamps.
    streams = data.get("streams") or []
    gpmf_index = _find_gpmf_stream_index(streams)
    gpmf_track: list[GpsFix] = []
    gps_source = "none"
    if gpmf_index is not None:
        gpmf_track = _extract_gpmf_track(path, gpmf_index)
        if gpmf_track:
            gps_source = "gpmf"
    if not gpmf_track and gps_position is not None:
        gps_source = "tags"

    return GoProVideo(
        path=path,
        duration_s=duration_s,
        creation_utc=creation_utc,
        gps_position=gps_position,
        tags=tags,
        gpmf_track=gpmf_track,
        gps_source=gps_source,
    )


def match_sessions_to_video(
    video: GoProVideo,
    sessions: list[dict[str, Any]],
    *,
    min_overlap_s: float = 1.0,
) -> list[dict[str, Any]]:
    if video.start_utc is None or video.duration_s is None:
        return []
    video_end = video.end_utc
    if video_end is None:
        return []

    candidates: list[dict[str, Any]] = []
    for session in sessions:
        start = session.get("start_utc")
        if not isinstance(start, str):
            continue
        try:
            session_start = datetime.fromisoformat(start)
        except ValueError:
            continue
        end = session.get("end_utc")
        session_end = None
        if isinstance(end, str):
            try:
                session_end = datetime.fromisoformat(end)
            except ValueError:
                session_end = None
        if session_end is None:
            session_end = video_end

        overlap_start = max(video.start_utc, session_start)
        overlap_end = min(video_end, session_end)
        overlap = (overlap_end - overlap_start).total_seconds()
        if overlap <= min_overlap_s:
            continue

        candidates.append(
            {
                "session": session,
                "overlap_s": overlap,
                "video_fraction": overlap / video.duration_s,
            }
        )

    candidates.sort(key=lambda item: item["overlap_s"], reverse=True)
    return candidates

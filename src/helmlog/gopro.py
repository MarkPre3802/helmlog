"""Probe GoPro/MP4 metadata and match it to a HelmLog session.

Supports two timestamp sources, in descending accuracy order:
  1. GPMF GPS track  — GPS-disciplined UTC, sub-second accuracy.
  2. MP4 creation_time tag — camera wall clock; often wrong after battery drain.
"""

from __future__ import annotations

import calendar
import contextlib
import json
import os
import re
import struct
import subprocess
import tempfile
import time as _time
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path  # noqa: TC003  (used at runtime in probe_video body)
from typing import Any
from zoneinfo import ZoneInfo

from loguru import logger

# ---------------------------------------------------------------------------
# Timestamp helpers
# ---------------------------------------------------------------------------


def _ts(value: str | None) -> datetime | None:
    if value is None:
        return None
    try:
        dt = datetime.fromisoformat(value)
    except ValueError:
        if value.endswith("Z"):
            try:
                dt = datetime.fromisoformat(value[:-1]).replace(tzinfo=UTC)
            except ValueError:
                return None
        else:
            return None
    return dt.replace(tzinfo=UTC) if dt.tzinfo is None else dt.astimezone(UTC)


def _parse_dt(value: object) -> datetime | None:
    if isinstance(value, datetime):
        return value if value.tzinfo is not None else value.replace(tzinfo=UTC)
    if isinstance(value, str):
        try:
            dt = datetime.fromisoformat(value)
            return dt if dt.tzinfo is not None else dt.replace(tzinfo=UTC)
        except ValueError:
            return None
    return None


# ---------------------------------------------------------------------------
# GPS location string helpers (ffprobe tag parsing)
# ---------------------------------------------------------------------------


def _parse_location_string(value: str) -> tuple[float, float] | None:
    if not value:
        return None
    value = value.strip()

    # ISO6709 requires explicit sign on both lat and lon; optional altitude then "/"
    _ISO6709 = r"^(?P<lat>[+-]\d+(?:\.\d+)?)(?P<lon>[+-]\d+(?:\.\d+)?)(?:[+-]\d+(?:\.\d+)?)?/?$"
    m = re.match(_ISO6709, value)
    if m:
        try:
            lat, lon = float(m.group("lat")), float(m.group("lon"))
        except ValueError:
            return None
        if -90.0 <= lat <= 90.0 and -180.0 <= lon <= 180.0:
            return lat, lon

    lat_m = re.search(r"lat(?:itude)?\s*[:=]\s*([+-]?\d+(?:\.\d+)?)", value, re.I)
    lon_m = re.search(r"lon(?:gitude)?\s*[:=]\s*([+-]?\d+(?:\.\d+)?)", value, re.I)
    if lat_m and lon_m:
        try:
            lat, lon = float(lat_m.group(1)), float(lon_m.group(1))
        except ValueError:
            return None
        if -90.0 <= lat <= 90.0 and -180.0 <= lon <= 180.0:
            return lat, lon

    parts = re.split(r"[\s,;]+", value)
    if len(parts) >= 2:
        try:
            lat, lon = float(parts[0]), float(parts[1])
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


# ---------------------------------------------------------------------------
# GPMF (GoPro Metadata Format) parser
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class GpmfPoint:
    """One GPS sample from the GoPro GPMF telemetry track."""

    utc: datetime
    lat: float
    lon: float
    speed_mps: float


def _gpmf_records(data: bytes) -> list[tuple[str, str, bytes]]:
    """Flat-parse a GPMF byte stream into (key, type_char, payload) tuples.

    Recursively descends into container records (type == '\\x00').
    """
    records: list[tuple[str, str, bytes]] = []
    i = 0
    while i + 8 <= len(data):
        try:
            key = data[i : i + 4].decode("ascii")
        except UnicodeDecodeError:
            break
        type_char = chr(data[i + 4])
        elem_size = data[i + 5]
        repeat = struct.unpack(">H", data[i + 6 : i + 8])[0]
        payload_size = elem_size * repeat
        aligned = (payload_size + 3) & ~3
        payload = data[i + 8 : i + 8 + payload_size]
        i += 8 + aligned

        if type_char == "\x00":
            # Container — recurse into its payload
            records.extend(_gpmf_records(payload))
        else:
            records.append((key, type_char, payload))
    return records


def _parse_gpsu(payload: bytes) -> datetime | None:
    """Parse a GPSU payload (ASCII UTC timestamp 'YYMMDDHHmmss.sss…') to UTC datetime."""
    try:
        raw = payload.rstrip(b"\x00").decode("ascii").strip()
    except UnicodeDecodeError:
        return None
    for fmt in ("%y%m%d%H%M%S.%f", "%y%m%d%H%M%S"):
        try:
            t = _time.strptime(raw[:12], fmt[:12])
            return datetime.fromtimestamp(calendar.timegm(t), tz=UTC)
        except (ValueError, IndexError):
            continue
    return None


def _parse_gpmf_gps(data: bytes) -> list[GpmfPoint]:
    """Extract GPS points from raw GPMF bytes.

    Returns points in chronological order with GPS-disciplined UTC timestamps.
    Only includes points with a valid GPS fix.
    """
    records = _gpmf_records(data)

    points: list[GpmfPoint] = []
    gpsu: datetime | None = None
    scal: list[float] = [1.0]
    gps_fix = 0

    for key, type_char, payload in records:
        if key == "GPSU":
            gpsu = _parse_gpsu(payload)

        elif key == "GPSF":
            if len(payload) >= 4 and type_char in ("L", "l"):
                fmt = ">I" if type_char == "L" else ">i"
                gps_fix = struct.unpack(fmt, payload[:4])[0]

        elif key == "SCAL":
            # Scale factors: one or more int16/int32 values
            if type_char in ("s", "S"):
                n = len(payload) // 2
                fmt_char = "h" if type_char == "s" else "H"
                scal = [float(v) for v in struct.unpack(f">{n}{fmt_char}", payload[:n * 2])]
            elif type_char in ("l", "L"):
                n = len(payload) // 4
                fmt_char = "i" if type_char == "l" else "I"
                scal = [float(v) for v in struct.unpack(f">{n}{fmt_char}", payload[:n * 4])]

        elif key == "GPS5" and type_char == "l" and gpsu is not None and gps_fix > 0:
            # GPS5: lat lon alt speed2D speed3D — each int32, scale by SCAL
            elem_size = 5 * 4
            n_samples = len(payload) // elem_size
            cur_scal = scal  # capture current scal to avoid B023 (loop-variable closure)

            def _scale(idx: int, raw: int, s: list[float] = cur_scal) -> float:
                sv = s[min(idx, len(s) - 1)]
                return raw / sv if sv else 0.0

            for j in range(n_samples):
                off = j * elem_size
                if off + elem_size > len(payload):
                    break
                vals = struct.unpack(">iiiii", payload[off : off + elem_size])
                lat = _scale(0, vals[0])
                lon = _scale(1, vals[1])
                speed_mps = _scale(4, vals[4])
                if not (-90.0 <= lat <= 90.0 and -180.0 <= lon <= 180.0):
                    continue
                if lat == 0.0 and lon == 0.0:
                    continue
                points.append(GpmfPoint(utc=gpsu, lat=lat, lon=lon, speed_mps=speed_mps))

    return points


def _find_gpmd_stream(path: Path, ffprobe_cmd: str) -> int | None:
    """Return the stream index of the gpmd (GoPro metadata) track, or None."""
    try:
        result = subprocess.run(
            [ffprobe_cmd, str(path)],
            capture_output=True,
            text=True,
            timeout=15,
        )
        output = result.stderr + result.stdout
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return None

    # Handle both "Stream #0:3(eng)" and "Stream #0:3[0x4](eng)" formats
    m = re.search(r"Stream #\d:(\d+)(?:\[0x[0-9a-f]+\])?\([^)]*\): Data: \w+ \(gpmd", output, re.I)
    if m:
        return int(m.group(1))
    # Fallback: without language tag
    m = re.search(r"Stream #\d:(\d+)(?:\[0x[0-9a-f]+\])?: Data: \w+ \(gpmd", output, re.I)
    if m:
        return int(m.group(1))
    return None


def extract_gpmf_track(
    path: Path, ffprobe_cmd: str = "ffprobe", ffmpeg_cmd: str = "ffmpeg"
) -> list[GpmfPoint]:
    """Extract GPS points from a GoPro MP4's GPMF telemetry track.

    Returns an empty list if the file has no gpmd stream or no GPS fix.
    Raises GoProProbeError on hard failures (ffmpeg not found, extraction error).
    """
    stream_idx = _find_gpmd_stream(path, ffprobe_cmd)
    if stream_idx is None:
        logger.debug("No gpmd stream found in {}", path.name)
        return []

    # Extract the raw GPMF binary via ffmpeg
    with tempfile.NamedTemporaryFile(suffix=".bin", delete=False) as tmp:
        tmp_path = Path(tmp.name)

    try:
        try:
            subprocess.run(
                [
                    ffmpeg_cmd,
                    "-y",
                    "-i",
                    str(path),
                    "-codec",
                    "copy",
                    "-map",
                    f"0:{stream_idx}",
                    "-f",
                    "rawvideo",
                    str(tmp_path),
                ],
                check=True,
                capture_output=True,
                timeout=120,
            )
        except FileNotFoundError as exc:
            raise GoProProbeError("ffmpeg is not installed or not on PATH") from exc
        except subprocess.CalledProcessError as exc:
            msg = exc.stderr.decode(errors="replace")[:200]
            raise GoProProbeError(f"ffmpeg GPMF extraction failed: {msg}") from exc
        except subprocess.TimeoutExpired as exc:
            raise GoProProbeError("ffmpeg timed out extracting GPMF track") from exc

        raw = tmp_path.read_bytes()
    finally:
        tmp_path.unlink(missing_ok=True)

    points = _parse_gpmf_gps(raw)
    logger.debug("GPMF: {} GPS points extracted from {}", len(points), path.name)
    return points


# ---------------------------------------------------------------------------
# Public dataclasses and probe entry point
# ---------------------------------------------------------------------------


class GoProProbeError(RuntimeError):
    pass


@dataclass(frozen=True)
class GoProVideo:
    path: Path
    duration_s: float | None = None
    creation_utc: datetime | None = None
    gps_position: tuple[float, float] | None = None
    tags: dict[str, str] = field(default_factory=dict)
    gpmf_track: tuple[GpmfPoint, ...] = field(default_factory=tuple)

    @property
    def start_utc(self) -> datetime | None:
        return self.creation_utc

    @property
    def end_utc(self) -> datetime | None:
        if self.creation_utc is None or self.duration_s is None:
            return None
        return self.creation_utc + timedelta(seconds=self.duration_s)

    @property
    def gps_source(self) -> str:
        """Human-readable label for the timestamp source."""
        return "gpmf" if self.gpmf_track else "tag"


def probe_video(path: Path, timezone: str = "UTC") -> GoProVideo:
    """Probe a GoPro/MP4 file for timing and position metadata.

    Tries GPMF GPS track first (GPS-disciplined UTC); falls back to the
    MP4 creation_time tag.  Never raises on missing GPS — returns a
    GoProVideo with gpmf_track=() and whatever the tag provides.
    """
    if not path.exists():
        raise GoProProbeError(f"File does not exist: {path}")

    ffprobe_cmd = os.environ.get("HELMLOG_FFPROBE", "ffprobe")
    ffmpeg_cmd = os.environ.get("HELMLOG_FFMPEG", "ffmpeg")

    # --- ffprobe for duration, creation_time tag, and any embedded GPS tag ---
    try:
        result = subprocess.run(
            [
                ffprobe_cmd,
                "-v",
                "quiet",
                "-print_format",
                "json",
                "-show_entries",
                "format=duration:format_tags:stream_tags",
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
        probe_data = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise GoProProbeError("ffprobe returned invalid JSON") from exc

    tags = _collect_tags(probe_data)

    duration_s: float | None = None
    fmt = probe_data.get("format") or {}
    if isinstance(fmt, dict):
        with contextlib.suppress(KeyError, ValueError, TypeError):
            duration_s = float(fmt["duration"])

    # Tag-based creation time (fallback)
    tag_creation_utc: datetime | None = None
    raw_ct = tags.get("creation_time")
    if raw_ct is not None:
        parsed = _ts(raw_ct)
        if parsed is None:
            try:
                naive = datetime.fromisoformat(raw_ct)
                parsed = naive.replace(tzinfo=ZoneInfo(timezone)).astimezone(UTC)
            except ValueError:
                pass
        tag_creation_utc = parsed

    # Tag-based GPS position (ffprobe may expose it for non-GoPro cameras)
    tag_gps_position: tuple[float, float] | None = None
    for key in ("com.apple.quicktime.location.ISO6709", "location", "location-eng"):
        if key in tags:
            tag_gps_position = _parse_location_string(tags[key])
            if tag_gps_position is not None:
                break
    if tag_gps_position is None and "GPSLatitude" in tags and "GPSLongitude" in tags:
        with contextlib.suppress(ValueError):
            tag_gps_position = (float(tags["GPSLatitude"]), float(tags["GPSLongitude"]))

    # --- GPMF GPS track (preferred source) ---
    gpmf_track: tuple[GpmfPoint, ...] = ()
    try:
        points = extract_gpmf_track(path, ffprobe_cmd, ffmpeg_cmd)
        gpmf_track = tuple(points)
    except GoProProbeError as exc:
        logger.debug("GPMF extraction skipped: {}", exc)

    # Resolve final creation_utc and gps_position: prefer GPMF over tags
    creation_utc: datetime | None
    gps_position: tuple[float, float] | None
    if gpmf_track:
        creation_utc = gpmf_track[0].utc
        gps_position = (gpmf_track[0].lat, gpmf_track[0].lon)
        logger.debug(
            "Using GPMF GPS timestamp: {} (tag was {})", creation_utc, tag_creation_utc
        )
    else:
        creation_utc = tag_creation_utc
        gps_position = tag_gps_position

    return GoProVideo(
        path=path,
        duration_s=duration_s,
        creation_utc=creation_utc,
        gps_position=gps_position,
        tags=tags,
        gpmf_track=gpmf_track,
    )


# ---------------------------------------------------------------------------
# Session matching
# ---------------------------------------------------------------------------


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
        session_start = _parse_dt(session.get("start_utc"))
        if session_start is None:
            continue
        session_end = _parse_dt(session.get("end_utc")) or video_end

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

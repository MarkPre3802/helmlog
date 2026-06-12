"""Tests for gopro.py — video probing helpers and session matching."""

from __future__ import annotations

import json
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from helmlog.gopro import (
    GoProProbeError,
    GoProVideo,
    _parse_dt,
    _parse_gpmf_gps,
    _parse_gpsu,
    _parse_location_string,
    match_sessions_to_video,
    probe_video,
)

# ---------------------------------------------------------------------------
# _parse_dt
# ---------------------------------------------------------------------------


def test_parse_dt_datetime_aware() -> None:
    dt = datetime(2024, 6, 1, 10, 0, tzinfo=UTC)
    assert _parse_dt(dt) == dt


def test_parse_dt_datetime_naive() -> None:
    dt = datetime(2024, 6, 1, 10, 0)
    result = _parse_dt(dt)
    assert result is not None
    assert result.tzinfo is UTC


def test_parse_dt_iso_string_with_tz() -> None:
    result = _parse_dt("2024-06-01T10:00:00+00:00")
    assert result == datetime(2024, 6, 1, 10, 0, tzinfo=UTC)


def test_parse_dt_iso_string_naive() -> None:
    result = _parse_dt("2024-06-01T10:00:00")
    assert result is not None
    assert result.tzinfo is UTC


def test_parse_dt_invalid_string() -> None:
    assert _parse_dt("not-a-date") is None


def test_parse_dt_none() -> None:
    assert _parse_dt(None) is None


def test_parse_dt_int() -> None:
    assert _parse_dt(42) is None


# ---------------------------------------------------------------------------
# _parse_location_string
# ---------------------------------------------------------------------------


def test_parse_location_iso6709() -> None:
    result = _parse_location_string("+47.123456-122.123456/")
    assert result is not None
    lat, lon = result
    assert abs(lat - 47.123456) < 1e-6
    assert abs(lon - -122.123456) < 1e-6


def test_parse_location_named_fields() -> None:
    result = _parse_location_string("latitude: 47.5, longitude: -122.3")
    assert result is not None
    assert abs(result[0] - 47.5) < 1e-6
    assert abs(result[1] - -122.3) < 1e-6


def test_parse_location_comma_separated() -> None:
    result = _parse_location_string("47.5,-122.3")
    assert result is not None
    assert abs(result[0] - 47.5) < 1e-6


def test_parse_location_empty() -> None:
    assert _parse_location_string("") is None


def test_parse_location_out_of_range() -> None:
    assert _parse_location_string("+91.0-122.0/") is None


# ---------------------------------------------------------------------------
# GoProVideo properties
# ---------------------------------------------------------------------------


def _video(
    duration_s: float | None = 3600.0,
    creation_utc: datetime | None = datetime(2024, 6, 1, 10, 0, tzinfo=UTC),
) -> GoProVideo:
    return GoProVideo(path=Path("/tmp/test.mp4"), duration_s=duration_s, creation_utc=creation_utc)


def test_start_utc_equals_creation_utc() -> None:
    v = _video()
    assert v.start_utc == v.creation_utc


def test_end_utc_computed() -> None:
    v = _video(duration_s=3600.0, creation_utc=datetime(2024, 6, 1, 10, 0, tzinfo=UTC))
    assert v.end_utc == datetime(2024, 6, 1, 11, 0, tzinfo=UTC)


def test_end_utc_none_if_no_creation() -> None:
    v = _video(creation_utc=None)
    assert v.end_utc is None


def test_end_utc_none_if_no_duration() -> None:
    v = _video(duration_s=None)
    assert v.end_utc is None


# ---------------------------------------------------------------------------
# match_sessions_to_video
# ---------------------------------------------------------------------------

_VIDEO_START = datetime(2024, 6, 1, 10, 0, tzinfo=UTC)
_VIDEO_DUR = 3600.0  # 10:00–11:00 UTC


def _base_video() -> GoProVideo:
    return GoProVideo(
        path=Path("/tmp/race.mp4"),
        duration_s=_VIDEO_DUR,
        creation_utc=_VIDEO_START,
    )


def _session(
    start: datetime,
    end: datetime | None = None,
    sid: int = 1,
    name: str = "Race 1",
) -> dict[str, object]:
    return {
        "id": sid,
        "name": name,
        "start_utc": start,
        "end_utc": end,
    }


def test_match_full_overlap() -> None:
    v = _base_video()
    sessions = [_session(start=_VIDEO_START, end=_VIDEO_START + timedelta(hours=1))]
    results = match_sessions_to_video(v, sessions, min_overlap_s=1)
    assert len(results) == 1
    assert results[0]["overlap_s"] == pytest.approx(3600.0)
    assert results[0]["video_fraction"] == pytest.approx(1.0)


def test_match_partial_overlap() -> None:
    v = _base_video()
    # session starts 30 min before video ends → 30 min overlap
    session_start = _VIDEO_START + timedelta(minutes=30)
    sessions = [_session(start=session_start, end=_VIDEO_START + timedelta(hours=2))]
    results = match_sessions_to_video(v, sessions, min_overlap_s=1)
    assert len(results) == 1
    assert results[0]["overlap_s"] == pytest.approx(1800.0)


def test_match_no_overlap() -> None:
    v = _base_video()
    # session entirely before video
    sessions = [
        _session(start=_VIDEO_START - timedelta(hours=2), end=_VIDEO_START - timedelta(hours=1))
    ]
    results = match_sessions_to_video(v, sessions, min_overlap_s=1)
    assert results == []


def test_match_below_min_overlap() -> None:
    v = _base_video()
    # session overlaps by only 2 seconds
    sessions = [
        _session(
            start=_VIDEO_START + timedelta(seconds=3598), end=_VIDEO_START + timedelta(hours=2)
        )
    ]
    results = match_sessions_to_video(v, sessions, min_overlap_s=5)
    assert results == []


def test_match_sorted_by_overlap_desc() -> None:
    v = _base_video()
    s1 = _session(start=_VIDEO_START, end=_VIDEO_START + timedelta(minutes=10), sid=1)
    s2 = _session(start=_VIDEO_START, end=_VIDEO_START + timedelta(hours=1), sid=2)
    results = match_sessions_to_video(v, [s1, s2], min_overlap_s=1)
    assert results[0]["session"]["id"] == 2
    assert results[1]["session"]["id"] == 1


def test_match_string_timestamps() -> None:
    v = _base_video()
    sessions = [
        {
            "id": 1,
            "name": "Race",
            "start_utc": "2024-06-01T10:00:00+00:00",
            "end_utc": "2024-06-01T11:00:00+00:00",
        }
    ]
    results = match_sessions_to_video(v, sessions, min_overlap_s=1)
    assert len(results) == 1


def test_match_no_end_utc_uses_video_end() -> None:
    v = _base_video()
    sessions = [_session(start=_VIDEO_START, end=None)]
    results = match_sessions_to_video(v, sessions, min_overlap_s=1)
    assert len(results) == 1
    assert results[0]["overlap_s"] == pytest.approx(3600.0)


def test_match_video_missing_start_returns_empty() -> None:
    v = GoProVideo(path=Path("/tmp/x.mp4"), duration_s=3600.0, creation_utc=None)
    sessions = [_session(start=_VIDEO_START)]
    assert match_sessions_to_video(v, sessions) == []


def test_match_session_bad_start_skipped() -> None:
    v = _base_video()
    sessions = [{"id": 1, "start_utc": "not-a-date", "end_utc": None}]
    assert match_sessions_to_video(v, sessions, min_overlap_s=1) == []


# ---------------------------------------------------------------------------
# probe_video — ffprobe mocking
# ---------------------------------------------------------------------------

_FFPROBE_JSON = json.dumps(
    {
        "format": {
            "duration": "3600.0",
            "tags": {
                "creation_time": "2024-06-01T10:00:00.000000Z",
                "com.apple.quicktime.location.ISO6709": "+47.600000-122.330000/",
            },
        },
        "streams": [],
    }
)


def _mock_run(returncode: int = 0, stdout: str = _FFPROBE_JSON, stderr: str = "") -> MagicMock:
    m = MagicMock()
    m.stdout = stdout
    m.stderr = stderr
    m.returncode = returncode
    return m


# The second subprocess.run call is the ffprobe gpmd-stream probe (returns no gpmd → no GPMF)
_NO_GPMD = _mock_run(stdout="", stderr="no metadata stream")


def test_probe_video_basic(tmp_path: Path) -> None:
    """Probe falls back to ffprobe tag when no GPMF track is present."""
    mp4 = tmp_path / "test.mp4"
    mp4.write_bytes(b"\x00")
    # Call 1: ffprobe JSON metadata; Call 2: ffprobe stream detect (no gpmd)
    with patch("subprocess.run", side_effect=[_mock_run(), _NO_GPMD]):
        video = probe_video(mp4)
    assert video.gps_source == "tag"
    assert video.duration_s == pytest.approx(3600.0)
    assert video.creation_utc == datetime(2024, 6, 1, 10, 0, tzinfo=UTC)
    assert video.gps_position is not None
    lat, lon = video.gps_position
    assert abs(lat - 47.6) < 1e-4
    assert abs(lon - -122.33) < 1e-4


def test_probe_video_file_not_found(tmp_path: Path) -> None:
    with pytest.raises(GoProProbeError, match="does not exist"):
        probe_video(tmp_path / "missing.mp4")


def test_probe_video_ffprobe_not_installed(tmp_path: Path) -> None:
    mp4 = tmp_path / "test.mp4"
    mp4.write_bytes(b"\x00")
    with (
        patch("subprocess.run", side_effect=FileNotFoundError),
        pytest.raises(GoProProbeError, match="ffprobe is not installed"),
    ):
        probe_video(mp4)


def test_probe_video_ffprobe_error(tmp_path: Path) -> None:
    mp4 = tmp_path / "test.mp4"
    mp4.write_bytes(b"\x00")
    err = subprocess.CalledProcessError(1, "ffprobe", stderr="bad file")
    with (
        patch("subprocess.run", side_effect=err),
        pytest.raises(GoProProbeError, match="ffprobe failed"),
    ):
        probe_video(mp4)


def test_probe_video_invalid_json(tmp_path: Path) -> None:
    mp4 = tmp_path / "test.mp4"
    mp4.write_bytes(b"\x00")
    with (
        patch("subprocess.run", return_value=_mock_run(stdout="not-json")),
        pytest.raises(GoProProbeError, match="invalid JSON"),
    ):
        probe_video(mp4)


def test_probe_video_no_gps(tmp_path: Path) -> None:
    mp4 = tmp_path / "test.mp4"
    mp4.write_bytes(b"\x00")
    payload = json.dumps({"format": {"duration": "600.0", "tags": {}}, "streams": []})
    with patch("subprocess.run", side_effect=[_mock_run(stdout=payload), _NO_GPMD]):
        video = probe_video(mp4)
    assert video.gps_position is None


def test_probe_video_timeout(tmp_path: Path) -> None:
    mp4 = tmp_path / "test.mp4"
    mp4.write_bytes(b"\x00")
    with (
        patch("subprocess.run", side_effect=subprocess.TimeoutExpired("ffprobe", 30)),
        pytest.raises(GoProProbeError, match="timed out"),
    ):
        probe_video(mp4)


# ---------------------------------------------------------------------------
# GPMF parser unit tests
# ---------------------------------------------------------------------------

import struct as _struct  # noqa: E402  (needed here for helper functions)


def _gpmf_record(key: str, type_char: str, data: bytes) -> bytes:
    """Build a single GPMF record (key + header + padded data)."""
    elem_size = len(data)
    repeat = 1
    padded = data + b"\x00" * ((4 - len(data) % 4) % 4)
    return key.encode("ascii") + type_char.encode("ascii") + bytes([elem_size, 0, repeat]) + padded


def _gpmf_multi(key: str, type_char: str, elem_size: int, repeat: int, data: bytes) -> bytes:
    padded = data + b"\x00" * ((4 - len(data) % 4) % 4)
    repeat_hi = (repeat >> 8) & 0xFF
    repeat_lo = repeat & 0xFF
    return (
        key.encode("ascii")
        + type_char.encode("ascii")
        + bytes([elem_size, repeat_hi, repeat_lo])
        + padded
    )


def _build_gpmf_gps_stream(
    gpsu_str: str = "200601100000",  # 2020-06-01 10:00:00 UTC
    lat_raw: int = 476000000,  # 47.6 × 1e7
    lon_raw: int = -1223300000,  # -122.33 × 1e7
    speed_raw: int = 50000,  # 5.0 m/s × 1e4
    scal: int = 10000000,
    gps_fix: int = 3,
) -> bytes:
    """Build a minimal GPMF binary with one GPS sample."""
    # GPSU: 12-byte ASCII timestamp
    gpsu_bytes = gpsu_str.encode("ascii").ljust(16, b"\x00")
    gpsu_rec = b"GPSU" + b"U" + bytes([len(gpsu_bytes), 0, 1]) + gpsu_bytes

    # GPSF: GPS fix (int32 big-endian)
    gpsf_data = _struct.pack(">i", gps_fix)
    gpsf_rec = b"GPSF" + b"l" + bytes([4, 0, 1]) + gpsf_data

    # SCAL: single int32 scale factor
    scal_data = _struct.pack(">i", scal)
    scal_rec = b"SCAL" + b"l" + bytes([4, 0, 1]) + scal_data

    # GPS5: lat lon alt speed2d speed3d (5 × int32)
    gps5_data = _struct.pack(">iiiii", lat_raw, lon_raw, 0, 0, speed_raw)
    gps5_rec = b"GPS5" + b"l" + bytes([20, 0, 1]) + gps5_data

    return gpsu_rec + gpsf_rec + scal_rec + gps5_rec


def test_parse_gpsu_basic() -> None:
    payload = b"200601100000\x00\x00\x00\x00"
    result = _parse_gpsu(payload)
    assert result is not None
    assert result.year == 2020
    assert result.month == 6
    assert result.day == 1
    assert result.hour == 10
    assert result.tzinfo is UTC


def test_parse_gpsu_invalid() -> None:
    assert _parse_gpsu(b"notadate") is None


def test_parse_gpmf_gps_basic() -> None:
    gpmf = _build_gpmf_gps_stream()
    points = _parse_gpmf_gps(gpmf)
    assert len(points) == 1
    p = points[0]
    assert abs(p.lat - 47.6) < 1e-4
    assert abs(p.lon - -122.33) < 1e-4
    assert p.utc.year == 2020
    assert p.utc.tzinfo is UTC


def test_parse_gpmf_gps_no_fix() -> None:
    gpmf = _build_gpmf_gps_stream(gps_fix=0)
    points = _parse_gpmf_gps(gpmf)
    assert points == []


def test_parse_gpmf_gps_zero_coords_skipped() -> None:
    gpmf = _build_gpmf_gps_stream(lat_raw=0, lon_raw=0)
    points = _parse_gpmf_gps(gpmf)
    assert points == []


def test_parse_gpmf_gps_empty_bytes() -> None:
    assert _parse_gpmf_gps(b"") == []


def test_probe_video_uses_gpmf_timestamp(tmp_path: Path) -> None:
    """When GPMF track is present, probe_video uses GPS timestamp over tag."""
    mp4 = tmp_path / "test.mp4"
    mp4.write_bytes(b"\x00")

    # ffprobe JSON says creation_time is 2016 (bad clock)
    bad_tag_json = json.dumps(
        {
            "format": {
                "duration": "963.963",
                "tags": {"creation_time": "2016-03-21T04:59:29.000000Z"},
            },
            "streams": [],
        }
    )

    # Fake gpmd stream in ffprobe stream output
    stream_detect = _mock_run(
        stderr="Stream #0:3[0x4](eng): Data: bin_data (gpmd / 0x646D7067), 35 kb/s"
    )

    # GPMF binary has GPS fix at 2024-06-01
    gpmf_bytes = _build_gpmf_gps_stream(gpsu_str="240601120000")

    def fake_run(cmd: list[str], **kwargs: object) -> MagicMock:
        cmd_str = " ".join(str(c) for c in cmd)
        if "json" in cmd_str:
            return _mock_run(stdout=bad_tag_json)
        if "ffmpeg" in cmd_str:
            # Write the GPMF bytes to the output file
            out_path = cmd[-1]
            Path(str(out_path)).write_bytes(gpmf_bytes)
            return _mock_run()
        return stream_detect  # ffprobe stream detection

    with patch("subprocess.run", side_effect=fake_run):
        video = probe_video(mp4)

    assert video.gps_source == "gpmf"
    assert video.creation_utc is not None
    assert video.creation_utc.year == 2024
    assert video.creation_utc.month == 6
    assert len(video.gpmf_track) == 1
    assert abs(video.gpmf_track[0].lat - 47.6) < 1e-4

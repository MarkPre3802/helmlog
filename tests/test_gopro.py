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
    sessions = [_session(start=_VIDEO_START - timedelta(hours=2), end=_VIDEO_START - timedelta(hours=1))]
    results = match_sessions_to_video(v, sessions, min_overlap_s=1)
    assert results == []


def test_match_below_min_overlap() -> None:
    v = _base_video()
    # session overlaps by only 2 seconds
    sessions = [_session(start=_VIDEO_START + timedelta(seconds=3598), end=_VIDEO_START + timedelta(hours=2))]
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


def test_probe_video_basic(tmp_path: Path) -> None:
    mp4 = tmp_path / "test.mp4"
    mp4.write_bytes(b"\x00")
    with patch("subprocess.run", return_value=_mock_run()) as mock_sub:
        video = probe_video(mp4)
    mock_sub.assert_called_once()
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
    with patch("subprocess.run", side_effect=FileNotFoundError):
        with pytest.raises(GoProProbeError, match="ffprobe is not installed"):
            probe_video(mp4)


def test_probe_video_ffprobe_error(tmp_path: Path) -> None:
    mp4 = tmp_path / "test.mp4"
    mp4.write_bytes(b"\x00")
    err = subprocess.CalledProcessError(1, "ffprobe", stderr="bad file")
    with patch("subprocess.run", side_effect=err):
        with pytest.raises(GoProProbeError, match="ffprobe failed"):
            probe_video(mp4)


def test_probe_video_invalid_json(tmp_path: Path) -> None:
    mp4 = tmp_path / "test.mp4"
    mp4.write_bytes(b"\x00")
    with patch("subprocess.run", return_value=_mock_run(stdout="not-json")):
        with pytest.raises(GoProProbeError, match="invalid JSON"):
            probe_video(mp4)


def test_probe_video_no_gps(tmp_path: Path) -> None:
    mp4 = tmp_path / "test.mp4"
    mp4.write_bytes(b"\x00")
    payload = json.dumps({"format": {"duration": "600.0", "tags": {}}, "streams": []})
    with patch("subprocess.run", return_value=_mock_run(stdout=payload)):
        video = probe_video(mp4)
    assert video.gps_position is None


def test_probe_video_timeout(tmp_path: Path) -> None:
    mp4 = tmp_path / "test.mp4"
    mp4.write_bytes(b"\x00")
    with patch("subprocess.run", side_effect=subprocess.TimeoutExpired("ffprobe", 30)):
        with pytest.raises(GoProProbeError, match="timed out"):
            probe_video(mp4)

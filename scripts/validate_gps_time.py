"""Spike: validate whether a GPS-tagged video's embedded creation_time is
actually GPS-disciplined, or just an uncorrected camera clock alongside an
independently-obtained GPS location fix (#websitehosting spec, decision 14).

Usage:
    uv run python scripts/validate_gps_time.py <video_path> [--db data/logger.db]

With --db, also cross-references the video's creation_utc against the
closest recorded position in HelmLog's own (GPS-disciplined) telemetry to
see how far off the camera's clock actually is, if at all.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import subprocess
import sys
from pathlib import Path


def dump_all_tags(path: Path) -> None:
    """Print every ffprobe format+stream tag, unfiltered -- gopro.py's
    probe_video() only extracts creation_time + one location tag; this
    shows everything else that might hint at GPS-time-discipline."""
    result = subprocess.run(
        [
            "ffprobe",
            "-v",
            "quiet",
            "-print_format",
            "json",
            "-show_entries",
            "format=duration,format_tags:stream=codec_type:stream_tags",
            str(path),
        ],
        check=True,
        capture_output=True,
        text=True,
        timeout=30,
    )
    data = json.loads(result.stdout)
    print("=== Raw ffprobe format+stream tags ===")
    print(json.dumps(data, indent=2))


async def cross_check_against_telemetry(creation_utc, db_path: str) -> None:
    from datetime import timedelta

    from helmlog.storage import Storage, StorageConfig

    storage = Storage(StorageConfig(db_path=db_path))
    await storage.connect()
    try:
        window_start = creation_utc - timedelta(minutes=10)
        window_end = creation_utc + timedelta(minutes=10)
        positions = await storage.query_range("positions", window_start, window_end)
        print(f"\n=== Telemetry near creation_utc ({creation_utc.isoformat()}) ===")
        print(f"positions in +/-10min window: {len(positions)}")
        if positions:
            closest = min(
                positions,
                key=lambda p: abs(
                    (creation_utc - __import__("datetime").datetime.fromisoformat(p["ts"]).replace(tzinfo=creation_utc.tzinfo)).total_seconds()
                ),
            )
            print(f"closest recorded position ts: {closest['ts']}")
    finally:
        await storage.close()


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("video_path")
    parser.add_argument("--db", default=None, help="Path to a HelmLog SQLite DB to cross-check against")
    parser.add_argument("--timezone", default="UTC")
    args = parser.parse_args()

    path = Path(args.video_path).expanduser().resolve()
    if not path.is_file():
        print(f"Not a file: {path}", file=sys.stderr)
        sys.exit(1)

    from helmlog.gopro import GoProProbeError, probe_video

    try:
        video = probe_video(path, args.timezone)
    except GoProProbeError as exc:
        print(f"probe_video failed: {exc}", file=sys.stderr)
        sys.exit(1)

    print("=== gopro.py probe_video() result ===")
    print(f"path:         {video.path}")
    print(f"duration_s:   {video.duration_s}")
    print(f"creation_utc: {video.creation_utc}")
    print(f"gps_position: {video.gps_position}  (source: {video.gps_source})")
    print(f"has_gps_tag:  {video.gps_position is not None}")

    dump_all_tags(path)

    if args.db and video.creation_utc is not None:
        await cross_check_against_telemetry(video.creation_utc, args.db)
    elif args.db:
        print("\nNo creation_utc found -- can't cross-check against telemetry.")


if __name__ == "__main__":
    asyncio.run(main())

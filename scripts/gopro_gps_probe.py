"""GoPro GPS alignment probe.

Reads a GoPro .mp4 or .gpx file, finds the matching HelmLog race in the DB,
and reports how well the GPS tracks align.  Both clocks are disciplined to the
same GPS constellation so the offset should be < 1 s.

MP4 input requires gopro2gpx:
    pip install gopro2gpx

Usage
-----
    python scripts/gopro_gps_probe.py GOPRO.mp4          # extract GPS from video
    python scripts/gopro_gps_probe.py GOPRO.gpx          # use pre-exported GPX
    python scripts/gopro_gps_probe.py GOPRO.mp4 --db /path/to/helmlog.db
    python scripts/gopro_gps_probe.py GOPRO.mp4 --race-id 42
    python scripts/gopro_gps_probe.py GOPRO.mp4 --race-id 42 --video-url https://youtu.be/...

The last form also prints a ready-to-run ``helmlog link-video`` command.
"""

from __future__ import annotations

import argparse
import math
import sqlite3
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import NamedTuple


# ---------------------------------------------------------------------------
# Data types
# ---------------------------------------------------------------------------

class GpsPoint(NamedTuple):
    ts: datetime     # UTC
    lat: float       # degrees
    lon: float       # degrees


# ---------------------------------------------------------------------------
# GPX parsing
# ---------------------------------------------------------------------------

_GPX_NS = {
    "gpx": "http://www.topografix.com/GPX/1/1",
    "gpx10": "http://www.topografix.com/GPX/1/0",
}


def _parse_time(s: str) -> datetime:
    """Parse an ISO-8601 UTC string from a GPX file."""
    s = s.strip()
    # Handle both Z suffix and +00:00
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    return datetime.fromisoformat(s).astimezone(UTC)


def load_gpx(path: Path) -> list[GpsPoint]:
    """Return all track points from a GPX file, sorted by time."""
    tree = ET.parse(path)
    root = tree.getroot()
    tag = root.tag  # e.g. {http://www.topografix.com/GPX/1/1}gpx

    # Determine namespace prefix from root tag
    ns = ""
    if "{" in tag:
        ns = tag[tag.index("{") : tag.index("}") + 1]

    points: list[GpsPoint] = []
    for trkpt in root.iter(f"{ns}trkpt"):
        lat = float(trkpt.get("lat", "0"))
        lon = float(trkpt.get("lon", "0"))
        time_el = trkpt.find(f"{ns}time")
        if time_el is None or not time_el.text:
            continue
        points.append(GpsPoint(ts=_parse_time(time_el.text), lat=lat, lon=lon))

    points.sort(key=lambda p: p.ts)
    return points


# ---------------------------------------------------------------------------
# MP4 → GPX extraction
# ---------------------------------------------------------------------------

def extract_gpx_from_mp4(mp4_path: Path) -> Path:
    """Extract GPS telemetry from a GoPro MP4 and return path to a temp GPX file.

    Requires gopro2gpx (pip install gopro2gpx).  Raises RuntimeError if not
    installed or if the video has no GPS track.
    """
    try:
        result = subprocess.run(
            ["gopro2gpx", "--help"],
            capture_output=True, timeout=5,
        )
    except FileNotFoundError:
        raise RuntimeError(
            "gopro2gpx is not installed.\n"
            "  Install it with:  pip install gopro2gpx\n"
            "  Then re-run this script."
        ) from None

    tmp = tempfile.NamedTemporaryFile(suffix=".gpx", delete=False)
    tmp.close()
    gpx_path = Path(tmp.name)

    try:
        result = subprocess.run(
            ["gopro2gpx", str(mp4_path), str(gpx_path)],
            capture_output=True, text=True, timeout=120,
        )
    except subprocess.TimeoutExpired:
        raise RuntimeError("gopro2gpx timed out — is the file accessible?") from None

    if result.returncode != 0:
        msg = result.stderr.strip() or result.stdout.strip()
        raise RuntimeError(f"gopro2gpx failed (exit {result.returncode}): {msg}")

    if not gpx_path.exists() or gpx_path.stat().st_size == 0:
        raise RuntimeError(
            "gopro2gpx produced no output — the video may have no GPS track.\n"
            "  Check the camera had a GPS fix before recording started."
        )

    return gpx_path


# ---------------------------------------------------------------------------
# DB helpers
# ---------------------------------------------------------------------------

def _db_connect(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    return conn


def load_race_positions(conn: sqlite3.Connection, race_id: int) -> list[GpsPoint]:
    """Load GPS positions for a specific race from the DB."""
    cur = conn.execute(
        "SELECT ts, latitude_deg, longitude_deg FROM positions"
        " WHERE race_id = ? ORDER BY ts ASC",
        (race_id,),
    )
    rows = cur.fetchall()
    if not rows:
        # Fall back: positions table may not have race_id populated — use
        # the race time window from the races table.
        race_cur = conn.execute(
            "SELECT start_utc, end_utc FROM races WHERE id = ?", (race_id,)
        )
        race = race_cur.fetchone()
        if race is None:
            return []
        end_utc = race["end_utc"] or datetime.now(UTC).isoformat()
        cur = conn.execute(
            "SELECT ts, latitude_deg, longitude_deg FROM positions"
            " WHERE ts >= ? AND ts <= ? ORDER BY ts ASC",
            (race["start_utc"], end_utc),
        )
        rows = cur.fetchall()

    return [GpsPoint(ts=_parse_time(r["ts"]), lat=r["latitude_deg"], lon=r["longitude_deg"]) for r in rows]


def find_best_race(conn: sqlite3.Connection, gpx: list[GpsPoint]) -> int | None:
    """Find the race whose time window best overlaps the GPX track."""
    if not gpx:
        return None
    gpx_start = gpx[0].ts
    gpx_end = gpx[-1].ts

    # Expand search window by ±1 h to handle dock-to-dock recordings
    window_start = (gpx_start - timedelta(hours=1)).isoformat()
    window_end = (gpx_end + timedelta(hours=1)).isoformat()

    cur = conn.execute(
        "SELECT id, start_utc, end_utc FROM races"
        " WHERE start_utc <= ? AND (end_utc IS NULL OR end_utc >= ?)"
        " ORDER BY start_utc DESC",
        (window_end, window_start),
    )
    candidates = cur.fetchall()
    if not candidates:
        return None
    if len(candidates) == 1:
        return int(candidates[0]["id"])

    # Multiple races: pick the one with the most positional overlap
    best_id = None
    best_overlap = timedelta(0)
    for row in candidates:
        r_start = _parse_time(row["start_utc"])
        r_end = _parse_time(row["end_utc"]) if row["end_utc"] else gpx_end
        overlap_start = max(gpx_start, r_start)
        overlap_end = min(gpx_end, r_end)
        overlap = max(timedelta(0), overlap_end - overlap_start)
        if overlap > best_overlap:
            best_overlap = overlap
            best_id = int(row["id"])
    return best_id


# ---------------------------------------------------------------------------
# Alignment maths
# ---------------------------------------------------------------------------

def _haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance in metres."""
    R = 6_371_000.0
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlam = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlam / 2) ** 2
    return 2 * R * math.asin(math.sqrt(a))


@dataclass
class AlignmentResult:
    gpx_count: int
    db_count: int
    overlap_s: float          # seconds of time overlap
    matched_pairs: int        # DB points that found a GPX neighbour within 5 s
    mean_dist_m: float        # mean positional distance of matched pairs
    max_dist_m: float         # worst positional distance of matched pairs
    time_offset_s: float      # estimated clock offset: gpx_ts - db_ts (positive = GoPro ahead)
    offset_std_s: float       # std-dev of per-pair time offsets


def compute_alignment(gpx: list[GpsPoint], db: list[GpsPoint]) -> AlignmentResult:
    """
    For each DB point, find the nearest-time GPX point and measure:
      - positional distance (to check the tracks actually match)
      - timestamp difference (to measure clock offset)

    We use a sliding window on the GPX list for O(n) matching.
    """
    if not gpx or not db:
        return AlignmentResult(
            gpx_count=len(gpx), db_count=len(db),
            overlap_s=0, matched_pairs=0,
            mean_dist_m=0, max_dist_m=0,
            time_offset_s=0, offset_std_s=0,
        )

    gpx_start = gpx[0].ts
    gpx_end = gpx[-1].ts
    db_start = db[0].ts
    db_end = db[-1].ts
    overlap_start = max(gpx_start, db_start)
    overlap_end = min(gpx_end, db_end)
    overlap_s = max(0.0, (overlap_end - overlap_start).total_seconds())

    MATCH_WINDOW_S = 5.0
    gi = 0
    dists: list[float] = []
    offsets: list[float] = []

    for dp in db:
        if dp.ts < overlap_start or dp.ts > overlap_end:
            continue
        # Advance gi to the closest GPX point in time
        while gi < len(gpx) - 1 and abs((gpx[gi + 1].ts - dp.ts).total_seconds()) < abs((gpx[gi].ts - dp.ts).total_seconds()):
            gi += 1
        dt = abs((gpx[gi].ts - dp.ts).total_seconds())
        if dt > MATCH_WINDOW_S:
            continue
        dist = _haversine_m(dp.lat, dp.lon, gpx[gi].lat, gpx[gi].lon)
        dists.append(dist)
        offsets.append((gpx[gi].ts - dp.ts).total_seconds())

    if not dists:
        return AlignmentResult(
            gpx_count=len(gpx), db_count=len(db),
            overlap_s=overlap_s, matched_pairs=0,
            mean_dist_m=0, max_dist_m=0,
            time_offset_s=0, offset_std_s=0,
        )

    mean_dist = sum(dists) / len(dists)
    max_dist = max(dists)
    mean_offset = sum(offsets) / len(offsets)
    variance = sum((o - mean_offset) ** 2 for o in offsets) / len(offsets)
    std_offset = math.sqrt(variance)

    return AlignmentResult(
        gpx_count=len(gpx),
        db_count=len(db),
        overlap_s=overlap_s,
        matched_pairs=len(dists),
        mean_dist_m=mean_dist,
        max_dist_m=max_dist,
        time_offset_s=mean_offset,
        offset_std_s=std_offset,
    )


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------

def _fmt_ts(ts: datetime) -> str:
    return ts.strftime("%Y-%m-%d %H:%M:%S UTC")


def _default_db_path() -> Path:
    """Return the default DB path used by a local helmlog install."""
    candidates = [
        Path.home() / ".helmlog" / "helmlog.db",
        Path("/var/lib/helmlog/helmlog.db"),
        Path("helmlog.db"),
    ]
    for p in candidates:
        if p.exists():
            return p
    return Path("helmlog.db")


def main() -> None:
    parser = argparse.ArgumentParser(description="Probe GoPro GPS alignment against HelmLog race data")
    parser.add_argument("input", type=Path, help="GoPro .mp4 or .gpx file")
    parser.add_argument("--db", type=Path, default=None, help="Path to helmlog.db (auto-detected if omitted)")
    parser.add_argument("--race-id", type=int, default=None, help="Race ID to compare against (auto-detected from time overlap if omitted)")
    parser.add_argument("--video-url", default=None, help="YouTube URL — if given, prints a ready-to-run link-video command")
    args = parser.parse_args()

    if not args.input.exists():
        print(f"ERROR: File not found: {args.input}", file=sys.stderr)
        sys.exit(1)

    # --- Extract GPX from MP4 if needed ---
    _tmp_gpx: Path | None = None
    gpx_path = args.input
    if args.input.suffix.lower() in (".mp4", ".mov", ".m4v"):
        print(f"Extracting GPS track from {args.input.name} ...")
        try:
            gpx_path = extract_gpx_from_mp4(args.input)
            _tmp_gpx = gpx_path
            print(f"  Extracted to temporary GPX: {gpx_path}")
        except RuntimeError as e:
            print(f"ERROR: {e}", file=sys.stderr)
            sys.exit(1)

    # --- Load GPX ---
    print(f"Loading GPX: {gpx_path}")
    gpx = load_gpx(gpx_path)
    if _tmp_gpx:
        _tmp_gpx.unlink(missing_ok=True)
    if not gpx:
        print("ERROR: No track points found in GPX file.", file=sys.stderr)
        sys.exit(1)

    print(f"  {len(gpx)} track points  {_fmt_ts(gpx[0].ts)} → {_fmt_ts(gpx[-1].ts)}")
    duration_m = (gpx[-1].ts - gpx[0].ts).total_seconds() / 60
    print(f"  Duration: {duration_m:.1f} min")

    # --- Open DB ---
    db_path = args.db or _default_db_path()
    if not db_path.exists():
        print(f"ERROR: DB not found: {db_path}  (use --db to specify)", file=sys.stderr)
        sys.exit(1)

    conn = _db_connect(db_path)

    # --- Find / validate race ---
    if args.race_id is not None:
        race_id = args.race_id
    else:
        race_id = find_best_race(conn, gpx)
        if race_id is None:
            print("\nNo matching race found in the DB for this GPX time window.", file=sys.stderr)
            print("Use --race-id to specify a race manually.", file=sys.stderr)
            sys.exit(1)
        print(f"\nAuto-detected race: {race_id}")

    race_cur = conn.execute("SELECT id, start_utc, end_utc, date FROM races WHERE id = ?", (race_id,))
    race_row = race_cur.fetchone()
    if race_row is None:
        print(f"ERROR: Race {race_id} not found in DB.", file=sys.stderr)
        sys.exit(1)

    print(f"\nRace {race_id}  ({race_row['date']})")
    print(f"  start: {race_row['start_utc']}")
    print(f"  end:   {race_row['end_utc'] or '(still open)'}")

    # --- Load DB positions ---
    db_pos = load_race_positions(conn, race_id)
    if not db_pos:
        print("\nWARNING: No positions found in DB for this race.")
        print("  The positions table may not have race_id populated for older races.")
        print("  Try --race-id with a recent race, or check that Signal K position data was recorded.")
    else:
        print(f"\nDB positions: {len(db_pos)} points  {_fmt_ts(db_pos[0].ts)} → {_fmt_ts(db_pos[-1].ts)}")

    # --- Alignment ---
    if db_pos:
        print("\nComputing alignment ...")
        result = compute_alignment(gpx, db_pos)

        print(f"\n{'='*50}")
        print("ALIGNMENT REPORT")
        print(f"{'='*50}")
        print(f"  GPX points:          {result.gpx_count}")
        print(f"  DB points:           {result.db_count}")
        print(f"  Time overlap:        {result.overlap_s:.0f} s  ({result.overlap_s/60:.1f} min)")
        print(f"  Matched pairs:       {result.matched_pairs}")

        if result.matched_pairs == 0:
            print("\n  ⚠  No matched pairs — tracks may not overlap spatially.")
            print("     Check that the GPX and race are from the same day/location.")
        else:
            print(f"\n  Mean positional distance: {result.mean_dist_m:.1f} m")
            print(f"  Max positional distance:  {result.max_dist_m:.1f} m")
            print(f"\n  Clock offset (GoPro - HelmLog): {result.time_offset_s:+.2f} s  (σ = {result.offset_std_s:.2f} s)")

            if result.mean_dist_m < 20 and abs(result.time_offset_s) < 2:
                verdict = "✓  EXCELLENT — tracks align well, GPS clocks agree."
            elif result.mean_dist_m < 50 and abs(result.time_offset_s) < 5:
                verdict = "~  GOOD — minor drift, sync should work fine."
            elif result.mean_dist_m < 200:
                verdict = "⚠  PARTIAL — some drift; check GPS fix quality."
            else:
                verdict = "✗  POOR — tracks diverge; wrong race or no GPS fix."
            print(f"\n  Verdict: {verdict}")

        print(f"{'='*50}")

    # --- Sync parameters for link-video ---
    # The sync point: at gpx[0].ts, the video was at offset 0.
    # Adjust for clock offset: video_offset = 0 when UTC = gpx[0].ts - offset
    if db_pos and result.matched_pairs > 0:
        # The GoPro clock leads HelmLog by time_offset_s.
        # video t=0 corresponds to UTC = gpx[0].ts - time_offset_s
        adjusted_start_utc = gpx[0].ts - timedelta(seconds=result.time_offset_s)
        sync_utc_iso = adjusted_start_utc.isoformat().replace("+00:00", "Z")
        print(f"\nSync parameters:")
        print(f"  --sync-utc    {sync_utc_iso}")
        print(f"  --sync-offset 0.0  (start of video = t=0)")
        print(f"  (or equivalently: video offset at {_fmt_ts(gpx[0].ts)} is {result.time_offset_s:.2f} s)")

        # Always print the local-video link command for the MP4
        if args.input.suffix.lower() in (".mp4", ".mov", ".m4v"):
            print(f"\nLink this video to the race (replace RACE_ID and adjust URL if needed):")
            print(f"  curl -X POST http://localhost/api/sessions/{race_id}/local-video \\")
            print(f"    -H 'Content-Type: application/json' \\")
            print(f"    -d '{{\"local_path\": \"{args.input}\", \"sync_utc\": \"{sync_utc_iso}\", \"sync_offset_s\": 0.0}}'")

        if args.video_url:
            print(f"\nReady-to-run link-video command (YouTube):")
            print(f"  helmlog link-video \\")
            print(f"    --url '{args.video_url}' \\")
            print(f"    --sync-utc '{sync_utc_iso}' \\")
            print(f"    --sync-offset 0.0")

    # --- Summary of all races (if auto-detected, show alternatives) ---
    if args.race_id is None:
        print(f"\nOther races in the DB (most recent 10):")
        all_cur = conn.execute(
            "SELECT id, date, start_utc, end_utc FROM races ORDER BY start_utc DESC LIMIT 10"
        )
        for r in all_cur.fetchall():
            marker = " <-- matched" if r["id"] == race_id else ""
            print(f"  race {r['id']:4d}  {r['date']}  {r['start_utc'][:19]}{marker}")

    conn.close()


if __name__ == "__main__":
    main()

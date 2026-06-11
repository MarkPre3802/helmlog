"""Route handlers for videos."""

from __future__ import annotations

import mimetypes
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse, RedirectResponse, StreamingResponse

from helmlog.auth import require_auth
from helmlog.routes._helpers import LocalVideoCreate, VideoCreate, VideoUpdate, audit, get_storage

router = APIRouter()


def _video_deep_link(row: dict[str, Any], at_utc: datetime | None = None) -> dict[str, Any]:
    """Augment a race_videos row with a computed YouTube deep-link.

    If *at_utc* is supplied the link jumps to that moment in the video.
    Otherwise the link just opens the video from the beginning.
    """
    from helmlog.video import VideoSession  # local import to avoid circular deps

    sync_utc = datetime.fromisoformat(row["sync_utc"])
    duration_s = row["duration_s"]

    out = dict(row)
    if at_utc is not None and duration_s is not None:
        vs = VideoSession(
            url=row["youtube_url"],
            video_id=row["video_id"],
            title=row["title"],
            duration_s=duration_s,
            sync_utc=sync_utc,
            sync_offset_s=row["sync_offset_s"],
        )
        out["deep_link"] = vs.url_at(at_utc)
    else:
        out["deep_link"] = None
    return out


@router.get("/api/sessions/{session_id}/videos")
async def api_list_videos(
    request: Request,
    session_id: int,
    at: str | None = None,
    _user: dict[str, Any] = Depends(require_auth("viewer")),  # noqa: B008
) -> JSONResponse:
    """List videos linked to a session.

    Optional ``?at=<UTC ISO 8601>`` param computes a deep-link to that
    moment in each video.
    """
    storage = get_storage(request)
    # Videos are only supported on races (not audio sessions).
    cur = await storage._conn().execute("SELECT id FROM races WHERE id = ?", (session_id,))
    if await cur.fetchone() is None:
        raise HTTPException(status_code=404, detail="Session not found")
    rows = await storage.list_race_videos(session_id)
    at_utc: datetime | None = None
    if at:
        try:
            at_utc = datetime.fromisoformat(at)
            if at_utc.tzinfo is None:
                at_utc = at_utc.replace(tzinfo=UTC)
        except ValueError:
            raise HTTPException(status_code=422, detail="Invalid 'at' timestamp")  # noqa: B904
    return JSONResponse([_video_deep_link(r, at_utc) for r in rows])


@router.get("/api/sessions/{session_id}/videos/redirect")
async def api_videos_redirect(
    request: Request,
    session_id: int,
    at: str | None = None,
) -> RedirectResponse:
    """Redirect to the YouTube deep-link for a specific moment in the session's first video.

    Returns ``302 Location`` to the computed YouTube URL (with ``?t=<seconds>``).
    Returns ``404`` if the session doesn't exist or has no linked videos.
    Returns ``422`` if ``at`` is missing or cannot be parsed.
    """
    storage = get_storage(request)
    if not at:
        raise HTTPException(status_code=422, detail="'at' query parameter is required")
    try:
        at_utc = datetime.fromisoformat(at)
        if at_utc.tzinfo is None:
            at_utc = at_utc.replace(tzinfo=UTC)
    except ValueError:
        raise HTTPException(status_code=422, detail="Invalid 'at' timestamp")  # noqa: B904
    cur = await storage._conn().execute("SELECT id FROM races WHERE id = ?", (session_id,))
    if await cur.fetchone() is None:
        raise HTTPException(status_code=404, detail="Session not found")
    rows = await storage.list_race_videos(session_id)
    if not rows:
        raise HTTPException(status_code=404, detail="No videos linked to this session")
    # Use the first video by created_at (list_race_videos returns ASC order).
    row = rows[0]
    enriched = _video_deep_link(row, at_utc)
    url = enriched["deep_link"] or row["youtube_url"]
    return RedirectResponse(url=url, status_code=302)


@router.get("/api/videos/redirect")
async def api_videos_redirect_by_time(
    request: Request,
    at: str | None = None,
) -> RedirectResponse:
    """Resolve the race active at ``at`` and redirect to its first video.

    Designed for Grafana Data Links — no session_id required.  Grafana
    passes ``${__value.time:date:iso}`` as the ``at`` parameter and this
    endpoint resolves the correct race automatically.

    Returns ``302 Location`` to the YouTube deep-link with ``?t=<seconds>``.
    Returns ``404`` if no race covers that timestamp or the race has no video.
    Returns ``422`` if ``at`` is missing or cannot be parsed.
    """
    storage = get_storage(request)
    if not at:
        raise HTTPException(status_code=422, detail="'at' query parameter is required")
    try:
        at_utc = datetime.fromisoformat(at)
        if at_utc.tzinfo is None:
            at_utc = at_utc.replace(tzinfo=UTC)
    except ValueError:
        raise HTTPException(status_code=422, detail="Invalid 'at' timestamp")  # noqa: B904

    at_iso = at_utc.isoformat()
    cur = await storage._conn().execute(
        """
        SELECT id FROM races
        WHERE start_utc <= ?
          AND (end_utc >= ? OR end_utc IS NULL)
        ORDER BY start_utc DESC
        LIMIT 1
        """,
        (at_iso, at_iso),
    )
    race_row = await cur.fetchone()
    if race_row is None:
        raise HTTPException(status_code=404, detail="No race found at this timestamp")

    session_id = race_row["id"]
    rows = await storage.list_race_videos(session_id)
    if not rows:
        raise HTTPException(status_code=404, detail="No videos linked to this session")

    row = rows[0]
    enriched = _video_deep_link(row, at_utc)
    url = enriched["deep_link"] or row["youtube_url"]
    return RedirectResponse(url=url, status_code=302)


@router.post("/api/sessions/{session_id}/videos", status_code=201)
async def api_add_video(
    request: Request,
    session_id: int,
    body: VideoCreate,
    _user: dict[str, Any] = Depends(require_auth("crew")),  # noqa: B008
) -> JSONResponse:
    """Link a YouTube video to a race session.

    The caller supplies a sync point: a UTC wall-clock time and the
    corresponding video player position (seconds).  This pins the video
    timeline to logger time.
    """
    storage = get_storage(request)
    cur = await storage._conn().execute("SELECT id FROM races WHERE id = ?", (session_id,))
    if await cur.fetchone() is None:
        raise HTTPException(status_code=404, detail="Session not found")

    # Parse the sync UTC
    try:
        sync_utc = datetime.fromisoformat(body.sync_utc)
        if sync_utc.tzinfo is None:
            sync_utc = sync_utc.replace(tzinfo=UTC)
    except ValueError:
        raise HTTPException(status_code=422, detail="Invalid sync_utc timestamp")  # noqa: B904

    # Extract YouTube video ID and fetch metadata via yt-dlp if available
    from helmlog.video import VideoLinker

    video_id = ""
    title = ""
    duration_s: float | None = None
    try:
        linker = VideoLinker()
        vs = await linker.create_session(body.youtube_url, sync_utc, body.sync_offset_s)
        video_id = vs.video_id
        title = vs.title
        duration_s = vs.duration_s
    except Exception:  # noqa: BLE001
        # yt-dlp unavailable or network error — store the URL as-is.
        # Extract video ID from URL heuristically.
        import re

        m = re.search(r"(?:v=|youtu\.be/)([A-Za-z0-9_-]{11})", body.youtube_url)
        video_id = m.group(1) if m else ""
        title = ""
        duration_s = None

    row_id = await storage.add_race_video(
        race_id=session_id,
        youtube_url=body.youtube_url,
        video_id=video_id,
        title=title,
        label=body.label,
        sync_utc=sync_utc,
        sync_offset_s=body.sync_offset_s,
        duration_s=duration_s,
        user_id=_user.get("id"),
    )
    rows = await storage.list_race_videos(session_id)
    row = next(r for r in rows if r["id"] == row_id)
    await audit(request, "video.add", detail=body.youtube_url, user=_user)
    return JSONResponse(_video_deep_link(row), status_code=201)


@router.patch("/api/videos/{video_id}", status_code=200)
async def api_update_video(
    request: Request,
    video_id: int,
    body: VideoUpdate,
    _user: dict[str, Any] = Depends(require_auth("crew")),  # noqa: B008
) -> JSONResponse:
    """Update label or sync calibration on an existing video link."""
    storage = get_storage(request)
    sync_utc: datetime | None = None
    if body.sync_utc is not None:
        try:
            sync_utc = datetime.fromisoformat(body.sync_utc)
            if sync_utc.tzinfo is None:
                sync_utc = sync_utc.replace(tzinfo=UTC)
        except ValueError:
            raise HTTPException(status_code=422, detail="Invalid sync_utc timestamp")  # noqa: B904
    found = await storage.update_race_video(
        video_id,
        label=body.label,
        sync_utc=sync_utc,
        sync_offset_s=body.sync_offset_s,
    )
    if not found:
        raise HTTPException(status_code=404, detail="Video not found")
    await audit(request, "video.update", detail=str(video_id), user=_user)
    return JSONResponse({"id": video_id, "updated": True})


@router.delete("/api/videos/{video_id}", status_code=204)
async def api_delete_video(
    request: Request,
    video_id: int,
    _user: dict[str, Any] = Depends(require_auth("crew")),  # noqa: B008
) -> None:
    """Remove a video link."""
    storage = get_storage(request)
    found = await storage.delete_race_video(video_id)
    if not found:
        raise HTTPException(status_code=404, detail="Video not found")
    await audit(request, "video.delete", detail=str(video_id), user=_user)


@router.post("/api/sessions/{session_id}/local-video", status_code=201)
async def api_add_local_video(
    request: Request,
    session_id: int,
    body: LocalVideoCreate,
    _user: dict[str, Any] = Depends(require_auth("crew")),  # noqa: B008
) -> JSONResponse:
    """Link a locally-served video file to a race session.

    The ``local_path`` must be an absolute path on the server filesystem.
    The sync point (``sync_utc`` + ``sync_offset_s``) pins the video timeline
    to logger time the same way YouTube links do.
    """
    storage = get_storage(request)
    cur = await storage._conn().execute("SELECT id FROM races WHERE id = ?", (session_id,))
    if await cur.fetchone() is None:
        raise HTTPException(status_code=404, detail="Session not found")

    local_path = Path(body.local_path)
    if not local_path.exists():
        raise HTTPException(status_code=422, detail=f"File not found on server: {body.local_path}")

    try:
        sync_utc = datetime.fromisoformat(body.sync_utc)
        if sync_utc.tzinfo is None:
            sync_utc = sync_utc.replace(tzinfo=UTC)
    except ValueError:
        raise HTTPException(status_code=422, detail="Invalid sync_utc timestamp")  # noqa: B904

    # Probe duration via ffprobe if available
    duration_s: float | None = None
    try:
        import subprocess
        result = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "default=noprint_wrappers=1:nokey=1", str(local_path)],
            capture_output=True, text=True, timeout=10,
        )
        duration_s = float(result.stdout.strip())
    except Exception:  # noqa: BLE001
        pass

    row_id = await storage.add_local_race_video(
        race_id=session_id,
        local_path=str(local_path),
        sync_utc=sync_utc,
        sync_offset_s=body.sync_offset_s,
        duration_s=duration_s,
        label=body.label,
        user_id=_user.get("id"),
    )
    rows = await storage.list_race_videos(session_id)
    row = next(r for r in rows if r["id"] == row_id)
    await audit(request, "video.add_local", detail=str(local_path), user=_user)
    return JSONResponse(row, status_code=201)


@router.get("/api/videos/{video_id}/stream")
async def api_stream_local_video(
    request: Request,
    video_id: int,
    _user: dict[str, Any] = Depends(require_auth("viewer")),  # noqa: B008
) -> StreamingResponse:
    """Stream a locally-stored video file with HTTP range support.

    Supports ``Range`` header for browser seek support (required by ``<video>``).
    Only serves files whose path is registered in ``race_videos.local_path``.
    """
    storage = get_storage(request)
    cur = await storage._read_conn().execute(
        "SELECT local_path FROM race_videos WHERE id = ?", (video_id,)
    )
    row = await cur.fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="Video not found")
    local_path_str: str | None = row["local_path"]
    if not local_path_str:
        raise HTTPException(status_code=404, detail="No local file linked to this video")

    file_path = Path(local_path_str)
    if not file_path.exists():
        raise HTTPException(status_code=404, detail="Video file not found on server")

    file_size = file_path.stat().st_size
    mime_type = mimetypes.guess_type(str(file_path))[0] or "video/mp4"

    range_header = request.headers.get("range")
    if range_header:
        # Parse "bytes=start-end"
        try:
            range_val = range_header.strip().replace("bytes=", "")
            start_str, _, end_str = range_val.partition("-")
            start = int(start_str) if start_str else 0
            end = int(end_str) if end_str else file_size - 1
        except ValueError:
            raise HTTPException(status_code=416, detail="Invalid range header")  # noqa: B904
        end = min(end, file_size - 1)
        chunk_size = end - start + 1

        def _iter_range() -> Any:
            with open(file_path, "rb") as f:
                f.seek(start)
                remaining = chunk_size
                while remaining > 0:
                    data = f.read(min(65536, remaining))
                    if not data:
                        break
                    remaining -= len(data)
                    yield data

        return StreamingResponse(
            _iter_range(),
            status_code=206,
            media_type=mime_type,
            headers={
                "Content-Range": f"bytes {start}-{end}/{file_size}",
                "Accept-Ranges": "bytes",
                "Content-Length": str(chunk_size),
            },
        )

    # Full file
    def _iter_file() -> Any:
        with open(file_path, "rb") as f:
            while True:
                data = f.read(65536)
                if not data:
                    break
                yield data

    return StreamingResponse(
        _iter_file(),
        media_type=mime_type,
        headers={
            "Accept-Ranges": "bytes",
            "Content-Length": str(file_size),
        },
    )

"""Boat registry for the hosted web server.

The registry is the only data shared across boats on the server: which boats
exist, which hostnames belong to each, and which SQLite file currently holds
each boat's data. It is a small SQLite file of its own (``registry.db``) so the
``helmlog-hosting`` CLI and the running server can both use it.

Boat databases are imported as *new* files (``boats/<slug>/db/<stamp>.db``),
never overwritten in place: the server holds the current file open in WAL mode,
and replacing it underneath would corrupt it. The server notices the new
``db_path`` and switches over between requests.
"""

from __future__ import annotations

import os
import re
import shutil
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

from loguru import logger

from helmlog.hosting.snapshot import scrub

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

_SLUG_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,30}[a-z0-9])?$")

# Generations kept per boat: the current file plus the previous one, which the
# server may still have open until its in-flight requests finish.
_KEEP_GENERATIONS = 2

_SCHEMA = """
CREATE TABLE IF NOT EXISTS boats (
    slug        TEXT PRIMARY KEY,
    name        TEXT NOT NULL,
    db_path     TEXT,
    is_default  INTEGER NOT NULL DEFAULT 0,
    created_at  TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS boat_hosts (
    host  TEXT PRIMARY KEY,
    slug  TEXT NOT NULL REFERENCES boats(slug) ON DELETE CASCADE
);
"""


class RegistryError(ValueError):
    """Raised for invalid registry operations (bad slug, unknown boat, bad snapshot)."""


@dataclass(frozen=True)
class Boat:
    slug: str
    name: str
    db_path: str | None  # None until the first snapshot is imported
    hosts: tuple[str, ...]
    is_default: bool


def normalize_host(host: str) -> str:
    """Lower-case a Host header value and strip any port and trailing dot."""
    host = host.strip().lower()
    if host.startswith("["):  # IPv6 literal, e.g. [::1]:443
        host = host[1 : host.find("]")] if "]" in host else host[1:]
    elif host.count(":") == 1:
        host = host.split(":", 1)[0]
    return host.rstrip(".")


def resolve_boat(boats: list[Boat], host: str | None, base_domain: str | None) -> Boat | None:
    """Pick the boat a request is for.

    Order: a host explicitly assigned to a boat; then ``<slug>.<base_domain>``;
    then the default boat — but only for hosts *outside* the base domain, so an
    unknown ``<x>.<base_domain>`` is a 404 rather than another boat's data.
    """
    name = normalize_host(host) if host else ""
    for boat in boats:
        if name in boat.hosts:
            return boat
    if base_domain:
        base = normalize_host(base_domain)
        if name.endswith("." + base):
            label = name[: -len(base) - 1]
            return next((b for b in boats if b.slug == label), None)
    return next((b for b in boats if b.is_default), None)


class BoatRegistry:
    """Read/write access to ``<data_dir>/registry.db``."""

    def __init__(self, data_dir: Path) -> None:
        self.data_dir = data_dir
        self.path = data_dir / "registry.db"

    # -- paths ---------------------------------------------------------------

    def boat_dir(self, slug: str) -> Path:
        return self.data_dir / "boats" / slug

    def videos_dir(self, slug: str) -> Path:
        return self.boat_dir(slug) / "videos"

    # -- connection ----------------------------------------------------------

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        """Open the registry, commit on success, and always close."""
        self.data_dir.mkdir(parents=True, exist_ok=True)
        db = sqlite3.connect(self.path, timeout=10)
        try:
            db.execute("PRAGMA foreign_keys = ON")
            db.executescript(_SCHEMA)
            with db:
                yield db
        finally:
            db.close()

    def mtime_ns(self) -> int:
        """Registry file modification time; 0 if it does not exist yet."""
        try:
            return self.path.stat().st_mtime_ns
        except FileNotFoundError:
            return 0

    # -- queries -------------------------------------------------------------

    def list(self) -> list[Boat]:
        with self._connect() as db:
            rows = db.execute(
                "SELECT slug, name, db_path, is_default FROM boats ORDER BY slug"
            ).fetchall()
            hosts: dict[str, list[str]] = {}
            for host, slug in db.execute("SELECT host, slug FROM boat_hosts ORDER BY host"):
                hosts.setdefault(slug, []).append(host)
        return [
            Boat(
                slug=slug,
                name=name,
                db_path=db_path,
                hosts=tuple(hosts.get(slug, ())),
                is_default=bool(is_default),
            )
            for slug, name, db_path, is_default in rows
        ]

    def get(self, slug: str) -> Boat:
        for boat in self.list():
            if boat.slug == slug:
                return boat
        raise RegistryError(f"Unknown boat: {slug}")

    # -- mutations -----------------------------------------------------------

    def add(
        self, slug: str, name: str, hosts: tuple[str, ...] = (), *, is_default: bool = False
    ) -> Boat:
        if not _SLUG_RE.match(slug):
            raise RegistryError(
                f"Invalid boat slug {slug!r}: use 1-32 lower-case letters, digits or '-'"
                " (it becomes the subdomain)"
            )
        with self._connect() as db:
            try:
                db.execute(
                    "INSERT INTO boats (slug, name, created_at) VALUES (?, ?, ?)",
                    (slug, name, datetime.now(UTC).isoformat()),
                )
            except sqlite3.IntegrityError as exc:
                raise RegistryError(f"Boat {slug!r} already exists") from exc
        for host in hosts:
            self.add_host(slug, host)
        if is_default:
            self.set_default(slug)
        return self.get(slug)

    def remove(self, slug: str) -> None:
        """Drop a boat from the registry. Its files are left on disk."""
        self.get(slug)
        with self._connect() as db:
            db.execute("DELETE FROM boats WHERE slug = ?", (slug,))

    def add_host(self, slug: str, host: str) -> None:
        self.get(slug)
        name = normalize_host(host)
        if not name:
            raise RegistryError("Host must not be empty")
        with self._connect() as db:
            try:
                db.execute("INSERT INTO boat_hosts (host, slug) VALUES (?, ?)", (name, slug))
            except sqlite3.IntegrityError as exc:
                raise RegistryError(f"Host {name!r} is already assigned to a boat") from exc

    def set_default(self, slug: str) -> None:
        self.get(slug)
        with self._connect() as db:
            db.execute("UPDATE boats SET is_default = (slug = ?)", (slug,))

    # -- snapshot import -----------------------------------------------------

    def import_snapshot(self, slug: str, snapshot: Path) -> Path:
        """Copy a HelmLog DB snapshot in as the boat's new current database."""
        self.get(slug)
        _validate_snapshot(snapshot)

        db_dir = self.boat_dir(slug) / "db"
        db_dir.mkdir(parents=True, exist_ok=True)
        # Names sort by time, and pruning relies on that. Step forward a
        # microsecond if two imports land on the same clock tick.
        when = datetime.now(UTC)
        while (dest := db_dir / f"{when:%Y%m%dT%H%M%S%fZ}.db").exists():
            when += timedelta(microseconds=1)
        stamp = dest.stem
        tmp = db_dir / f".{stamp}.db.tmp"
        shutil.copyfile(snapshot, tmp)
        try:
            # Normally already done on the Pi (push-to-web.sh); repeated here
            # so nothing sensitive is served even from a hand-made upload.
            scrub(tmp)
        except BaseException:
            tmp.unlink(missing_ok=True)
            raise
        os.replace(tmp, dest)

        with self._connect() as db:
            db.execute("UPDATE boats SET db_path = ? WHERE slug = ?", (str(dest), slug))
        logger.info("Imported {} as {} for boat {}", snapshot, dest.name, slug)
        _prune_generations(db_dir)
        return dest


def _validate_snapshot(snapshot: Path) -> None:
    if not snapshot.is_file():
        raise RegistryError(f"Snapshot not found: {snapshot}")
    try:
        db = sqlite3.connect(f"{snapshot.resolve().as_uri()}?mode=ro", uri=True)
        try:
            (check,) = db.execute("PRAGMA quick_check").fetchone()
            row = db.execute("SELECT MAX(version) FROM schema_version").fetchone()
        finally:
            db.close()
    except sqlite3.DatabaseError as exc:
        raise RegistryError(f"{snapshot} is not a HelmLog database: {exc}") from exc
    if check != "ok":
        raise RegistryError(f"{snapshot} failed integrity check: {check}")
    if row is None or row[0] is None:
        raise RegistryError(f"{snapshot} has no HelmLog schema version")

    from helmlog.storage import _CURRENT_VERSION

    if row[0] > _CURRENT_VERSION:
        raise RegistryError(
            f"{snapshot} is at schema version {row[0]}, newer than this server's"
            f" {_CURRENT_VERSION}. Update the server (scripts/deploy-web.sh) first."
        )


def _prune_generations(db_dir: Path) -> None:
    generations = sorted(db_dir.glob("*.db"))
    for old in generations[:-_KEEP_GENERATIONS]:
        for path in (old, old.with_name(old.name + "-wal"), old.with_name(old.name + "-shm")):
            path.unlink(missing_ok=True)
        logger.info("Pruned old database generation {}", old.name)

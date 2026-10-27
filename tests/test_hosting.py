"""Tests for the multi-boat hosted web server (docs/specs/websitehosting.md)."""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from typing import TYPE_CHECKING

import httpx
import pytest

from helmlog.hosting.dispatcher import HostedDispatcher
from helmlog.hosting.pool import BoatAppPool
from helmlog.hosting.registry import Boat, BoatRegistry, RegistryError, resolve_boat
from helmlog.storage import Storage, StorageConfig

if TYPE_CHECKING:
    from pathlib import Path


async def _make_boat_db(path: Path, race_name: str) -> Path:
    """Create a real HelmLog DB (full schema) holding one closed race."""
    s = Storage(StorageConfig(db_path=str(path)))
    await s.connect()
    start = datetime(2026, 6, 1, 18, 0, tzinfo=UTC)
    race = await s.start_race("Evening", start, "2026-06-01", 1, race_name)
    await s.end_race(race.id, datetime(2026, 6, 1, 19, 0, tzinfo=UTC))
    await s.close()
    return path


def _boat(slug: str, hosts: tuple[str, ...] = (), *, is_default: bool = False) -> Boat:
    return Boat(slug=slug, name=slug.title(), db_path="x.db", hosts=hosts, is_default=is_default)


# ---------------------------------------------------------------------------
# Host resolution
# ---------------------------------------------------------------------------


class TestResolveBoat:
    def test_exact_host_match(self) -> None:
        boats = [_boat("corvo", ("helmlog-web.tail1234.ts.net",)), _boat("creative")]
        assert resolve_boat(boats, "helmlog-web.tail1234.ts.net", None).slug == "corvo"  # type: ignore[union-attr]

    def test_host_is_case_and_port_insensitive(self) -> None:
        boats = [_boat("corvo", ("helmlog-web.tail1234.ts.net",))]
        found = resolve_boat(boats, "HelmLog-Web.tail1234.ts.net:443", None)
        assert found is not None and found.slug == "corvo"

    def test_subdomain_of_base_domain(self) -> None:
        boats = [_boat("corvo"), _boat("creative")]
        found = resolve_boat(boats, "creative.helmlog.org", "helmlog.org")
        assert found is not None and found.slug == "creative"

    def test_unknown_subdomain_does_not_fall_back_to_default(self) -> None:
        # Serving the default boat under another boat's name would be worse
        # than a 404.
        boats = [_boat("corvo", is_default=True)]
        assert resolve_boat(boats, "nosuch.helmlog.org", "helmlog.org") is None

    def test_nested_subdomain_is_not_a_boat(self) -> None:
        boats = [_boat("corvo")]
        assert resolve_boat(boats, "a.corvo.helmlog.org", "helmlog.org") is None

    def test_other_host_falls_back_to_default(self) -> None:
        boats = [_boat("corvo"), _boat("creative", is_default=True)]
        found = resolve_boat(boats, "100.64.0.7", "helmlog.org")
        assert found is not None and found.slug == "creative"

    def test_no_default_and_no_match(self) -> None:
        assert resolve_boat([_boat("corvo")], "elsewhere.example", None) is None

    def test_missing_host_uses_default(self) -> None:
        boats = [_boat("corvo", is_default=True)]
        found = resolve_boat(boats, None, None)
        assert found is not None and found.slug == "corvo"


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------


class TestRegistry:
    def test_add_and_list(self, tmp_path: Path) -> None:
        reg = BoatRegistry(tmp_path)
        reg.add("corvo", "Corvo", hosts=("Corvo.Example",), is_default=True)
        reg.add("creative", "Creative")
        boats = {b.slug: b for b in reg.list()}
        assert boats["corvo"].hosts == ("corvo.example",)
        assert boats["corvo"].is_default
        assert not boats["creative"].is_default
        assert boats["corvo"].db_path is None

    @pytest.mark.parametrize("slug", ["", "Corvo", "-corvo", "corvo-", "cor vo", "a" * 40, "c.o"])
    def test_rejects_bad_slug(self, tmp_path: Path, slug: str) -> None:
        with pytest.raises(RegistryError):
            BoatRegistry(tmp_path).add(slug, "x")

    def test_duplicate_slug_rejected(self, tmp_path: Path) -> None:
        reg = BoatRegistry(tmp_path)
        reg.add("corvo", "Corvo")
        with pytest.raises(RegistryError):
            reg.add("corvo", "Corvo again")

    def test_host_claimed_by_one_boat_only(self, tmp_path: Path) -> None:
        reg = BoatRegistry(tmp_path)
        reg.add("corvo", "Corvo", hosts=("shared.example",))
        reg.add("creative", "Creative")
        with pytest.raises(RegistryError):
            reg.add_host("creative", "shared.example")

    def test_set_default_is_exclusive(self, tmp_path: Path) -> None:
        reg = BoatRegistry(tmp_path)
        reg.add("corvo", "Corvo", is_default=True)
        reg.add("creative", "Creative")
        reg.set_default("creative")
        defaults = [b.slug for b in reg.list() if b.is_default]
        assert defaults == ["creative"]

    def test_remove(self, tmp_path: Path) -> None:
        reg = BoatRegistry(tmp_path)
        reg.add("corvo", "Corvo", hosts=("corvo.example",))
        reg.remove("corvo")
        assert reg.list() == []
        reg.add("creative", "Creative", hosts=("corvo.example",))  # host freed

    def test_unknown_boat(self, tmp_path: Path) -> None:
        with pytest.raises(RegistryError):
            BoatRegistry(tmp_path).set_default("nope")


class TestImportSnapshot:
    async def test_import_sets_db_path(self, tmp_path: Path) -> None:
        reg = BoatRegistry(tmp_path / "hosting")
        reg.add("corvo", "Corvo")
        snap = await _make_boat_db(tmp_path / "snap.db", "R1")

        dest = reg.import_snapshot("corvo", snap)

        boat = reg.get("corvo")
        assert boat.db_path == str(dest)
        assert dest.parent == reg.boat_dir("corvo") / "db"
        with sqlite3.connect(dest) as db:
            assert db.execute("SELECT name FROM races").fetchall() == [("R1",)]

    async def test_each_import_is_a_new_file(self, tmp_path: Path) -> None:
        # The server may have the current file open in WAL mode, so an import
        # must never overwrite it in place.
        reg = BoatRegistry(tmp_path / "hosting")
        reg.add("corvo", "Corvo")
        snap = await _make_boat_db(tmp_path / "snap.db", "R1")
        first = reg.import_snapshot("corvo", snap)
        second = reg.import_snapshot("corvo", snap)
        assert first != second
        assert first.exists() and second.exists()

    async def test_prunes_all_but_two_newest(self, tmp_path: Path) -> None:
        reg = BoatRegistry(tmp_path / "hosting")
        reg.add("corvo", "Corvo")
        snap = await _make_boat_db(tmp_path / "snap.db", "R1")
        paths = [reg.import_snapshot("corvo", snap) for _ in range(4)]
        assert [p.exists() for p in paths] == [False, False, True, True]

    def test_rejects_non_sqlite(self, tmp_path: Path) -> None:
        reg = BoatRegistry(tmp_path / "hosting")
        reg.add("corvo", "Corvo")
        junk = tmp_path / "junk.db"
        junk.write_bytes(b"not a database" * 100)
        with pytest.raises(RegistryError):
            reg.import_snapshot("corvo", junk)
        assert reg.get("corvo").db_path is None

    async def test_rejects_snapshot_newer_than_server(self, tmp_path: Path) -> None:
        # Server code older than the boat would not know the boat's newer tables.
        from helmlog.storage import _CURRENT_VERSION

        reg = BoatRegistry(tmp_path / "hosting")
        reg.add("corvo", "Corvo")
        snap = await _make_boat_db(tmp_path / "snap.db", "R1")
        with sqlite3.connect(snap) as db:
            db.execute("INSERT INTO schema_version (version) VALUES (?)", (_CURRENT_VERSION + 1,))
        with pytest.raises(RegistryError, match="newer"):
            reg.import_snapshot("corvo", snap)

    def test_rejects_sqlite_without_helmlog_schema(self, tmp_path: Path) -> None:
        reg = BoatRegistry(tmp_path / "hosting")
        reg.add("corvo", "Corvo")
        other = tmp_path / "other.db"
        with sqlite3.connect(other) as db:
            db.execute("CREATE TABLE t (x INTEGER)")
        with pytest.raises(RegistryError):
            reg.import_snapshot("corvo", other)


# ---------------------------------------------------------------------------
# Hosted app (create_app(hosted=True)) and dispatcher
# ---------------------------------------------------------------------------


@pytest.fixture
async def two_boats(tmp_path: Path) -> BoatRegistry:
    reg = BoatRegistry(tmp_path / "hosting")
    reg.add("corvo", "Corvo", hosts=("helmlog-web.tail1234.ts.net",), is_default=True)
    reg.add("creative", "Creative")
    reg.import_snapshot("corvo", await _make_boat_db(tmp_path / "a.db", "Corvo race"))
    reg.import_snapshot("creative", await _make_boat_db(tmp_path / "b.db", "Creative race"))
    return reg


async def _race_names(client: httpx.AsyncClient, host: str) -> list[str]:
    r = await client.get("/api/sessions", headers={"host": host})
    assert r.status_code == 200, r.text
    return [s["name"] for s in r.json()["sessions"]]


class TestDispatcher:
    async def test_routes_each_host_to_its_own_db(self, two_boats: BoatRegistry) -> None:
        pool = BoatAppPool(two_boats)
        app = HostedDispatcher(two_boats, pool, base_domain="helmlog.org")
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            assert await _race_names(client, "creative.helmlog.org") == ["Creative race"]
            assert await _race_names(client, "corvo.helmlog.org") == ["Corvo race"]
            assert await _race_names(client, "helmlog-web.tail1234.ts.net") == ["Corvo race"]
        await pool.close_all()

    async def test_unknown_host_is_404(self, two_boats: BoatRegistry) -> None:
        pool = BoatAppPool(two_boats)
        app = HostedDispatcher(two_boats, pool, base_domain="helmlog.org")
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            r = await client.get("/api/sessions", headers={"host": "nosuch.helmlog.org"})
        assert r.status_code == 404

    async def test_boat_without_data_is_404(self, tmp_path: Path) -> None:
        reg = BoatRegistry(tmp_path)
        reg.add("corvo", "Corvo", is_default=True)
        app = HostedDispatcher(reg, BoatAppPool(reg), base_domain=None)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            r = await client.get("/api/sessions")
        assert r.status_code == 404

    async def test_healthz_needs_no_boat(self, tmp_path: Path) -> None:
        reg = BoatRegistry(tmp_path)
        app = HostedDispatcher(reg, BoatAppPool(reg), base_domain=None)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            r = await client.get("/healthz")
        assert r.status_code == 200

    async def test_new_import_is_served_without_restart(
        self, two_boats: BoatRegistry, tmp_path: Path
    ) -> None:
        pool = BoatAppPool(two_boats)
        app = HostedDispatcher(two_boats, pool, base_domain="helmlog.org")
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            assert await _race_names(client, "corvo.helmlog.org") == ["Corvo race"]
            newer = await _make_boat_db(tmp_path / "a2.db", "Corvo race 2")
            two_boats.import_snapshot("corvo", newer)
            assert await _race_names(client, "corvo.helmlog.org") == ["Corvo race 2"]
        await pool.close_all()

    @pytest.mark.parametrize(
        "path",
        [
            "/api/deployment/status",
            "/api/network/profiles",
            "/api/cameras",
            "/api/federation/identity",
        ],
    )
    async def test_host_control_routes_are_absent(self, two_boats: BoatRegistry, path: str) -> None:
        # Deploy, network, camera and federation routes act on the machine
        # they run on — on a shared server that would be the server itself.
        pool = BoatAppPool(two_boats)
        app = HostedDispatcher(two_boats, pool, base_domain="helmlog.org")
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            r = await client.get(path, headers={"host": "corvo.helmlog.org"})
        assert r.status_code == 404
        await pool.close_all()


class TestPool:
    async def test_idle_boats_are_closed(self, two_boats: BoatRegistry) -> None:
        pool = BoatAppPool(two_boats, idle_s=60)
        corvo = two_boats.get("corvo")
        entry = await pool.acquire(corvo)
        await pool.release(entry)
        assert pool.open_slugs() == ["corvo"]

        evicted = await pool.evict_idle(now=entry.last_used + 61)

        assert evicted == ["corvo"]
        assert pool.open_slugs() == []

    async def test_busy_boat_is_not_evicted(self, two_boats: BoatRegistry) -> None:
        pool = BoatAppPool(two_boats, idle_s=60)
        entry = await pool.acquire(two_boats.get("corvo"))
        assert await pool.evict_idle(now=entry.last_used + 600) == []
        await pool.release(entry)
        await pool.close_all()

    async def test_reuses_open_app(self, two_boats: BoatRegistry) -> None:
        pool = BoatAppPool(two_boats)
        corvo = two_boats.get("corvo")
        a = await pool.acquire(corvo)
        await pool.release(a)
        b = await pool.acquire(corvo)
        await pool.release(b)
        assert a is b
        await pool.close_all()


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


class TestCli:
    def test_serve_refuses_auth_disabled(self, tmp_path: Path) -> None:
        # pytest runs with AUTH_DISABLED=true (pyproject); a public server must not.
        from helmlog.hosting.cli import main

        assert main(["--data-dir", str(tmp_path), "serve"]) == 2

    async def test_add_import_list(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        from helmlog.hosting.cli import main

        data = str(tmp_path / "hosting")
        snap = await _make_boat_db(tmp_path / "snap.db", "R1")
        assert (
            main(["--data-dir", data, "boat", "add", "corvo", "--name", "Corvo", "--default"]) == 0
        )
        assert main(["--data-dir", data, "boat", "import", "corvo", str(snap)]) == 0
        capsys.readouterr()
        assert main(["--data-dir", data, "boat", "list"]) == 0
        out = capsys.readouterr().out
        assert "corvo (default)  Corvo" in out
        assert "no data imported" not in out

    def test_registry_errors_exit_1(self, tmp_path: Path) -> None:
        from helmlog.hosting.cli import main

        assert main(["--data-dir", str(tmp_path), "boat", "set-default", "nope"]) == 1


# ---------------------------------------------------------------------------
# Snapshot scrubbing (data-licensing review of the hosted copy)
# ---------------------------------------------------------------------------


async def _seed_sensitive(path: Path) -> None:
    s = Storage(StorageConfig(db_path=str(path)))
    await s.connect()
    db = s._conn()
    uid = await s.create_user("crew@x.com", "Crew", "crew")
    await db.execute(
        "INSERT INTO crew_voice_profiles (user_id, embedding, created_at, updated_at)"
        " VALUES (?, x'00ff', 'now', 'now')",
        (uid,),
    )
    await db.execute(
        "INSERT INTO wlan_profiles (name, ssid, password, created_at)"
        " VALUES ('marina', 'Marina', 'secret-psk', 'now')"
    )
    await db.execute(
        "INSERT INTO auth_sessions (session_id, user_id, created_at, expires_at)"
        " VALUES ('tok', ?, 'now', '2999-01-01')",
        (uid,),
    )
    await db.commit()
    await s.close()


class TestSnapshot:
    async def test_snapshot_strips_sensitive_tables(self, tmp_path: Path) -> None:
        from helmlog.hosting.snapshot import SCRUBBED_TABLES, make_snapshot

        live = await _make_boat_db(tmp_path / "live.db", "R1")
        await _seed_sensitive(live)

        snap = make_snapshot(live, tmp_path / "snap.db")

        with sqlite3.connect(snap) as db:
            for table in SCRUBBED_TABLES:
                assert db.execute(f"SELECT COUNT(*) FROM {table}").fetchone() == (0,), table
            # Review data and logins survive.
            assert db.execute("SELECT name FROM races").fetchall() == [("R1",)]
            assert db.execute("SELECT email FROM users").fetchall() == [("crew@x.com",)]
        # Deleted rows must not linger in free pages of the file.
        assert b"secret-psk" not in snap.read_bytes()

    async def test_snapshot_leaves_live_db_untouched(self, tmp_path: Path) -> None:
        from helmlog.hosting.snapshot import make_snapshot

        live = await _make_boat_db(tmp_path / "live.db", "R1")
        await _seed_sensitive(live)
        make_snapshot(live, tmp_path / "snap.db")
        with sqlite3.connect(live) as db:
            assert db.execute("SELECT COUNT(*) FROM wlan_profiles").fetchone() == (1,)

    async def test_import_scrubs_unscrubbed_upload(self, tmp_path: Path) -> None:
        # Defence in depth: an upload that skipped make_snapshot is scrubbed too.
        from helmlog.hosting.snapshot import SCRUBBED_TABLES

        reg = BoatRegistry(tmp_path / "hosting")
        reg.add("corvo", "Corvo")
        raw = await _make_boat_db(tmp_path / "raw.db", "R1")
        await _seed_sensitive(raw)

        dest = reg.import_snapshot("corvo", raw)

        with sqlite3.connect(dest) as db:
            for table in SCRUBBED_TABLES:
                assert db.execute(f"SELECT COUNT(*) FROM {table}").fetchone() == (0,), table
        assert b"secret-psk" not in dest.read_bytes()

"""Make a boat DB snapshot fit to leave the boat for the hosted web server.

Run on the Pi by ``scripts/push-to-web.sh``:

    python -m helmlog.hosting.snapshot <live.db> <snapshot.db>

Uses SQLite's online backup (consistent while the logger is writing), then
deletes data the review website must not hold and VACUUMs so the deleted rows
don't survive in free pages. The server's import scrubs again in case an
upload skipped this step.
"""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

# Deleted from every copy sent to the hosted server:
#   crew_voice_profiles   speaker voice embeddings — biometric, person-private
#                         (docs/data-licensing.md "Biometrics stay separate")
#   wlan_profiles         the boat's Wi-Fi passwords, in plain text
#   auth_sessions,        live login / reset / invite / peer-replay tokens: a
#   password_reset_tokens token taken from the Pi must not also work on the
#   invite_tokens,        website, and invites must be accepted on the Pi (an
#   invitations,          account created on the website is lost on the next
#   request_nonces        push)
# User accounts and password hashes (users, user_credentials) are kept: crew
# log in to the website with their Pi accounts (spec decision 3).
SCRUBBED_TABLES: tuple[str, ...] = (
    "crew_voice_profiles",
    "wlan_profiles",
    "auth_sessions",
    "password_reset_tokens",
    "invite_tokens",
    "invitations",
    "request_nonces",
)


def scrub(path: Path) -> None:
    """Delete SCRUBBED_TABLES' rows from the DB at *path* and VACUUM it."""
    db = sqlite3.connect(path)
    try:
        present = {
            row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        with db:
            for table in SCRUBBED_TABLES:
                if table in present:
                    db.execute(f"DELETE FROM {table}")  # noqa: S608 — fixed allowlist
        db.execute("PRAGMA journal_mode = DELETE")
        db.execute("VACUUM")
    finally:
        db.close()


def make_snapshot(live: Path, dest: Path) -> Path:
    """Back up *live* to *dest* (consistent, includes WAL contents) and scrub it."""
    src = sqlite3.connect(live, timeout=30)
    try:
        out = sqlite3.connect(dest)
        try:
            src.backup(out)
        finally:
            out.close()
    finally:
        src.close()
    scrub(dest)
    return dest


if __name__ == "__main__":
    if len(sys.argv) != 3:
        sys.exit("usage: python -m helmlog.hosting.snapshot <live.db> <snapshot.db>")
    make_snapshot(Path(sys.argv[1]), Path(sys.argv[2]))

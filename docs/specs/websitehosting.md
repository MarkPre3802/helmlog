Helmlog on a web server

one of the largest limiations of Helmlog is it abillity to run only on the pi which restricts is reach to other users.  The solution is to run all the review (non-race) capabilities over the web by hosting Helmlog on a public web server.

Goals:
1. to allow other users tied to a boat to log in and review race data
2. to allow viedos to be streamed into helmlog on the webserver or on youtube
3. easy upload and syncronization of boat data from the pi to the web server
4. Web server should be hosted in a homeserver to start with then as load increases move to a paid web server in the cloud
5. setup script that makes setting up helmlog on the web easy.

Assumptions:
1. PI has a connection to the internet

---

## Decisions

Resolved through review discussion (2026-08-15):

1. **Multi-boat, not a single-boat mirror.** This is a hosted product other
   boats sign up for, not a personal backup a single owner points at their
   own server. Implies real multi-tenancy, not just an optional mirror.
2. **Sync reuses the existing boat identity, not a new protocol.** The
   co-op federation protocol already gives every boat an Ed25519 keypair and
   authenticated peer-to-peer data exchange (`peer_api.py`/`peer_client.py`,
   see `docs/federation-design.md`). The web server is built as another peer
   in that same protocol rather than a separate sync mechanism — one
   identity/auth model, not two.
3. **Auth: per-boat user tables, synced up like any other data** — not a
   combined central table (superseded 2026-08-15; see below). Each boat's
   existing `auth.py` user table rides along in the same one-way sync as
   decision 2, so the web server holds a *replica* per boat rather than
   owning identity itself. The Pi stays fully authoritative and works
   offline with zero dependency on the web server — this is what resolves
   the "does the Pi still authenticate locally" risk that the combined-table
   version had. Accepted trade-off: a person with access to more than one
   boat needs a separate login per boat — no cross-boat linking layer for
   v1. Minor caveat: a password changed on the Pi won't reach the web
   server until the next sync, so there's a brief window where the web
   server's copy is stale.
3a. **Boat selection is via URL, not a picker UI.** Each boat gets its own
   subdomain on the web server (mirroring the Pi's own tunnel subdomain
   pattern in `docs/https-deployment.md`, e.g. `<boat>.helmlog.org`) — the
   subdomain the user is on determines which boat's (synced) user table
   they're authenticating against. Avoids exposing a directory of boats and
   avoids building a boat-picker UI.
4. **Video is hosted on the web server for v1**, not deferred to
   YouTube-only. Likely the main cost driver behind goal 4's
   homelab-to-paid-cloud migration path.
5. **Sync is one-way: Pi -> web server only**, with one narrow exception —
   **reviewer notes write back to the Pi.** A note is additive (a new row,
   not an edit to existing state), so two people leaving separate notes on
   the same race never conflicts — it doesn't need general two-way sync,
   just a small one-directional write-back channel for notes specifically.
   Everything else stays one-way, avoiding the distributed write-conflict
   resolution that ruled out full two-way sync in the first place.
5a. **Race "clean up" (editing/trimming existing race data, e.g. the kind
   of thing the manual trim feature does) is out of scope for now** —
   dropped 2026-08-16 rather than resolving whether it needs to reach back
   to the Pi. If a reviewer's cleanup action doesn't need to reach the Pi
   at all, the web server's copy would simply diverge from the Pi's raw
   original — revisit if/when this comes back into scope.
6. **The web server is the public front door.** Not an addition alongside
   the Pi's own direct public exposure (Cloudflare Tunnel / Tailscale
   Funnel, per `docs/https-deployment.md`) — it's the primary point of
   public access.
7. **Architecture: a thin multi-tenant layer wrapping the existing Pi
   codebase, not a schema rewrite.** "Tables for each boat" means a
   **separate SQLite file per boat** (byte-for-byte the same schema the Pi
   already runs), not one shared table with a `boat_id` column — so
   `storage.py`, `auth.py`, and every route in `routes/*.py` are reused
   unchanged; they already assume one boat's DB per process via
   `get_storage(request)`. Genuinely new pieces: (a) a subdomain-resolution
   middleware backed by a small top-level boat registry (the *only*
   actually-shared table on the server), (b) a storage pool —
   `get_storage(request)` resolves to a lazily-opened, idle-evictable
   `Storage` instance per boat instead of one baked into `app.state`, (c)
   the sync-ingest endpoint (decision 8). Everything hardware- or
   background-loop-related in `main.py` (`sk_reader.py`, `can_reader.py`,
   `cameras.py`, `audio.py`, weather/tide/monitor/deploy loops) is dropped
   entirely — none of it is meaningful on a review-only server, and this
   lines up with the hardware-isolation boundary the codebase already has.
8. **Sync mechanism: incremental row-push through the federation peer
   protocol (option 2 of two considered).** The alternative — Litestream-
   style byte-level WAL replication — was rejected: it's less code, but
   replicates through an object-storage hop (a third infrastructure
   component) rather than boat-to-server directly, and it's one-directional
   at the byte level, so notes-write-back (decision 5) would need a
   completely separate channel regardless. Option 2 reuses the Ed25519
   signing/verification already in `peer_client.py`/`peer_api.py` — the new
   work is a per-table watermark/cursor ("send everything newer than X"),
   the ingest endpoint that applies incoming rows via `storage.py`'s own
   write methods, an initial full-copy bootstrap for new boats, and
   retry/resume handling — none of which exist yet, but all of it lives in
   one place and carries notes-write-back through the same channel for
   free, no new infrastructure.
9. **Payload scope: raw/source tables only — derived data is recomputed on
   the web server, not synced.** Telemetry (`positions`, `cogsog`, `winds`,
   `speeds`, `headings`, `depths`) and `races` sync; polar baselines,
   maneuver-detection cache, and debrief cache do not — the web server
   recomputes them locally from synced raw data, keeping sync payload size
   down. **Video and audio get their own transfer pipe/storage**, separate
   from the row-sync mechanism (decision 8) entirely — large binary files
   don't fit a "batch of changed rows" model.
10. **v1 syncs new rows only — no edit or delete propagation.** The
   watermark/cursor (decision 8) only ever asks "what's new," not "what
   changed" or "what was removed." Deliberately deferred rather than
   guessing: need customer feedback on whether edit/delete-sync is actually
   needed before committing to a design for it, rather than locking in
   behavior that's hard to unwind later. (Decision 5a's dropped race-cleanup
   scope and this are the same underlying call, made twice for different
   reasons.)
11. **Sync is on-demand only — no schedule, no automatic trigger.** Run
   manually so it happens when the boat actually has good connectivity and
   video files are accessible on the Pi's own storage, not on a timer that
   might fire mid-race or over a bad connection. UI: a **Sync button on the
   Pi's own web UI**, under the existing Network settings page
   (`/admin/network`, `routes/network.py`) — sync sits alongside the
   connectivity controls it depends on, not buried elsewhere.
12. **Resilience: resumable to completion, idempotent, no silent retry.**
   If the connection drops mid-sync, the sync must be resumable and must
   complete — not silently abandon partial progress. The ingest endpoint
   must be idempotent (upsert semantics, not blind insert) so a resumed or
   resent batch never creates duplicate records. If the web server is
   unreachable when sync is triggered, the Pi's UI surfaces a clear alert
   to retry once it's back online — no background queuing or automatic
   retry loop.

## Open questions

Still to be resolved before this is spec-ready:

1. ~~Does the Pi still authenticate anyone locally, or does all auth depend
   on reaching the central server?~~ **Resolved by decision 3** (per-boat
   tables, synced as a replica) — the Pi stays fully authoritative and
   offline-capable.
2. ~~How does one person get access to more than one boat?~~ **Resolved by
   decision 3**: separate login per boat, accepted as-is for v1 — no
   cross-boat linking layer. User creation itself is otherwise unchanged
   from today — same per-boat `auth.py` invite flow, now also usable via
   the web server once synced.
3. ~~Is the web server read-only for review, or can reviewers write
   anything?~~ **Resolved by decision 5**: reviewers can leave notes
   (write back to the Pi). Race cleanup/editing is dropped from scope for
   now (decision 5a) rather than resolved either way.
4. ~~What would the architecture look like end to end?~~ **Resolved by
   decision 7**: per-boat SQLite files behind a thin multi-tenant routing
   layer, existing Pi codebase reused unchanged, hardware/background-loop
   code dropped entirely.
5. ~~What would the sync mechanism be, in detail?~~ **Resolved by decisions
   8-12**: incremental row-push via the federation peer protocol, raw
   tables only (derived data recomputed server-side), video/audio on a
   separate pipe, new-rows-only for v1, on-demand trigger via a button on
   `/admin/network`, and resumable/idempotent delivery with no silent
   retry on an unreachable server.
6. What specs would the web server itself need (compute/storage/bandwidth),
   especially given decision 4 (hosted video) as the likely cost driver?
7. On a homelab starting point, how is the server made reachable from the
   outside (equivalent question to the Pi's own tunnel options in
   `docs/https-deployment.md`, but now for a multi-tenant central server —
   different risk profile than a single boat's own Tailscale Funnel)?

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
3. **Auth: one combined user table on the web server**, not per-boat tables.
   Needed so a user can log in once and see the boat(s) they have access to,
   rather than a separate login per boat. This moves user identity off the
   Pi and onto the central server (see open question below on what that
   means for the Pi's own local/offline auth).
4. **Video is hosted on the web server for v1**, not deferred to
   YouTube-only. Likely the main cost driver behind goal 4's
   homelab-to-paid-cloud migration path.
5. **Sync is one-way: Pi -> web server only.** Simplified from an earlier
   two-way option specifically to avoid distributed write-conflict
   resolution (crew tagging live on the boat vs. a reviewer editing later on
   the web server). Nothing typed into the web server flows back to the
   Pi's SQLite — see open question below on what that means for review
   annotations.
6. **The web server is the public front door.** Not an addition alongside
   the Pi's own direct public exposure (Cloudflare Tunnel / Tailscale
   Funnel, per `docs/https-deployment.md`) — it's the primary point of
   public access.

## Open questions

Still to be resolved before this is spec-ready:

1. **Does the Pi still authenticate anyone locally, or does all auth depend
   on reaching the central server?** Today a boat can run fully
   self-contained over Tailscale with no internet dependency for auth
   (`AUTH_DISABLED=true`, or the Pi's own local user table). If identity
   moves to the central web server (decision 3), does on-boat/offline
   access still work without reaching it? Getting this wrong regresses the
   "works with no connectivity" property the rest of the project is built
   around.
2. **How does a user get tied to a specific boat in the central table?**
   One global user table needs an authorization model on top of it — an
   invite from the boat owner (similar to today's co-op membership
   invites), a self-signup code, or something else. This is "who can grant
   access to this boat's data," not just "where are credentials stored."
3. **Is the web server read-only for review, or can reviewers write
   anything?** Decision 5 (one-way sync) means anything written on the web
   server never reaches the Pi. Does "review race data" (goal 1) mean pure
   viewing, or can a reviewer add notes/tags/moments that live only on the
   web server, separate from the boat's own record? Needs to be explicit
   either way.
4. What would the architecture look like end to end (data flow diagram),
   now that decisions 1-6 narrow the shape?
5. What would the sync behavior be in detail — trigger (on race end? on a
   schedule? on demand?), payload (raw telemetry, exports, video, or some
   subset), and failure/retry handling when the Pi's connection drops
   mid-sync?
6. What specs would the web server itself need (compute/storage/bandwidth),
   especially given decision 4 (hosted video) as the likely cost driver?
7. On a homelab starting point, how is the server made reachable from the
   outside (equivalent question to the Pi's own tunnel options in
   `docs/https-deployment.md`, but now for a multi-tenant central server —
   different risk profile than a single boat's own Tailscale Funnel)?

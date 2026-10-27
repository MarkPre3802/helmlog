#!/usr/bin/env bash
# push-to-web.sh — Upload this boat's data to the HelmLog hosted web server.
#
# Runs ON the Pi. Takes a consistent snapshot of the live SQLite database
# (safe while helmlog is running), copies it to the server over SSH, and
# imports it as the boat's current data. Optionally mirrors video files.
#
# This is the interim manual upload (docs/specs/websitehosting.md): each push
# REPLACES the boat's data on the server with the Pi's. Anything written on
# the website itself (moments, comments, notes) is lost on the next push.
#
# Usage:
#   ./scripts/push-to-web.sh --server USER@HOST --boat SLUG [--db PATH] [--videos DIR]
#
#   --server USER@HOST  SSH target, e.g. mark@helmlog-web (Tailscale name)
#   --boat SLUG         Boat slug registered on the server (helmlog-hosting boat list)
#   --db PATH           Database to push (default: DB_PATH from .env, else data/logger.db)
#   --videos DIR        Also rsync this directory into the boat's videos folder
#
# Walkthrough: docs/web-hosting-deployment.md

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"

GREEN='\033[0;32m'; CYAN='\033[0;36m'; RED='\033[0;31m'; NC='\033[0m'
step() { echo -e "\n${GREEN}==> $*${NC}"; }
info() { echo -e "${CYAN}    $*${NC}"; }
die()  { echo -e "${RED}ERROR:${NC} $*" >&2; exit 1; }

SERVER="" BOAT="" DB="" VIDEOS=""
while [[ $# -gt 0 ]]; do
    case "$1" in
        --server) SERVER="$2"; shift 2 ;;
        --boat) BOAT="$2"; shift 2 ;;
        --db) DB="$2"; shift 2 ;;
        --videos) VIDEOS="$2"; shift 2 ;;
        -h|--help) sed -n '2,20p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; exit 0 ;;
        *) die "Unknown argument: $1 (see --help)" ;;
    esac
done
[[ -n $SERVER && -n $BOAT ]] || die "--server and --boat are required (see --help)"

if [[ -z $DB ]]; then
    if [[ -f $PROJECT_DIR/.env ]]; then
        DB=$(sed -n 's/^DB_PATH=//p' "$PROJECT_DIR/.env" | tail -1 | tr -d '"'"'")
    fi
    DB=${DB:-data/logger.db}
fi
[[ $DB == /* ]] || DB="$PROJECT_DIR/$DB"
[[ -f $DB ]] || die "Database not found: $DB"
[[ -z $VIDEOS || -d $VIDEOS ]] || die "Videos directory not found: $VIDEOS"

SNAP=$(mktemp --suffix=.db)
REMOTE_SNAP="/tmp/helmlog-$BOAT-$(date -u +%Y%m%dT%H%M%SZ).db"
trap 'rm -f "$SNAP"' EXIT

step "1/3 Snapshot $DB"
# Consistent online backup (safe while the logger writes), then strip what the
# website must not hold — voice prints, Wi-Fi passwords, live login/invite
# tokens — before anything leaves the boat. See helmlog/hosting/snapshot.py.
(cd "$PROJECT_DIR" && uv run --no-sync python -m helmlog.hosting.snapshot "$DB" "$SNAP")
info "$(du -h "$SNAP" | cut -f1)"

step "2/3 Upload to $SERVER and import as '$BOAT'"
scp -q -C "$SNAP" "$SERVER:$REMOTE_SNAP"
# shellcheck disable=SC2029  # values are meant to expand locally
ssh "$SERVER" "chmod 644 '$REMOTE_SNAP' && helmlog-hosting boat import '$BOAT' '$REMOTE_SNAP'; rc=\$?; rm -f '$REMOTE_SNAP'; exit \$rc"

if [[ -n $VIDEOS ]]; then
    step "3/3 Videos from $VIDEOS"
    # shellcheck disable=SC2029
    REMOTE_VIDEOS=$(ssh "$SERVER" "helmlog-hosting boat videos-dir '$BOAT'")
    rsync -a --partial --info=progress2 --rsync-path="sudo -u helmlog rsync" \
        "${VIDEOS%/}/" "$SERVER:$REMOTE_VIDEOS/"
else
    step "3/3 Videos — skipped (no --videos)"
fi

step "Done. The website now shows the Pi's data as of this push."

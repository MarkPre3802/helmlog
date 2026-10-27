#!/usr/bin/env bash
# deploy-web.sh — Install or update the HelmLog hosted web server (Debian 12).
#
# Runs ON the server (a Proxmox LXC/VM), as root. First install and every
# later update are the same command; each step is idempotent.
#
# Usage:
#   sudo ./scripts/deploy-web.sh [options]
#
# Options:
#   --repo URL          Git repo to deploy (default: saved value, else the fork)
#   --ref REF           Branch, tag or commit to deploy (default: saved value, else main)
#   --admin USER        Login user allowed to import boat data (default: saved, else $SUDO_USER)
#   --funnel            Expose publicly via Tailscale Funnel (default: tailnet-only serve)
#   --no-tailscale      Skip Tailscale entirely
#   --base-domain D     Serve boats at <slug>.D (for later, with a real domain)
#
# Choices are saved in /etc/helmlog-web/deploy.conf, so an update is just:
#   sudo /opt/helmlog-web/scripts/deploy-web.sh
#
# Walkthrough: docs/web-hosting-deployment.md

set -euo pipefail

APP_DIR=/opt/helmlog-web
ETC_DIR=/etc/helmlog-web
ENV_FILE=$ETC_DIR/helmlog-web.env
CONF_FILE=$ETC_DIR/deploy.conf
DATA_HOME=/var/lib/helmlog-web
SVC_USER=helmlog
PORT=3003
DEFAULT_REPO=https://github.com/MarkPre3802/helmlog.git

GREEN='\033[0;32m'; YELLOW='\033[1;33m'; CYAN='\033[0;36m'; RED='\033[0;31m'; NC='\033[0m'
step() { echo -e "\n${GREEN}==> $*${NC}"; }
warn() { echo -e "${YELLOW}WARN:${NC} $*"; }
info() { echo -e "${CYAN}    $*${NC}"; }
die()  { echo -e "${RED}ERROR:${NC} $*" >&2; exit 1; }

as_svc() { sudo -u "$SVC_USER" -H "$@"; }

# Everything runs inside main() so bash has parsed the whole script before
# step 4 updates the checkout this file may live in.
main() {
    local repo="" ref="" admin="" funnel="" use_tailscale=1 base_domain=""
    while [[ $# -gt 0 ]]; do
        case "$1" in
            --repo) repo="$2"; shift 2 ;;
            --ref) ref="$2"; shift 2 ;;
            --admin) admin="$2"; shift 2 ;;
            --funnel) funnel=1; shift ;;
            --no-tailscale) use_tailscale=0; shift ;;
            --base-domain) base_domain="$2"; shift 2 ;;
            -h|--help) sed -n '2,22p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; exit 0 ;;
            *) die "Unknown argument: $1 (see --help)" ;;
        esac
    done

    [[ $EUID -eq 0 ]] || die "Run as root: sudo $0 $*"

    # Saved choices from earlier runs fill in anything not given now.
    local SAVED_REPO="" SAVED_REF="" SAVED_ADMIN="" SAVED_FUNNEL=""
    # shellcheck source=/dev/null
    [[ -f $CONF_FILE ]] && . "$CONF_FILE"
    repo=${repo:-${SAVED_REPO:-$DEFAULT_REPO}}
    ref=${ref:-${SAVED_REF:-main}}
    admin=${admin:-${SAVED_ADMIN:-${SUDO_USER:-}}}
    funnel=${funnel:-${SAVED_FUNNEL:-}}
    [[ $admin == root ]] && admin=""

    if [[ -r /etc/os-release ]]; then
        # shellcheck source=/dev/null
        . /etc/os-release
        [[ ${VERSION_CODENAME:-} == bookworm ]] \
            || warn "Expected Debian 12 (bookworm), found ${PRETTY_NAME:-unknown}; continuing"
    fi

    # -----------------------------------------------------------------------
    step "1/9 System packages"
    export DEBIAN_FRONTEND=noninteractive
    apt-get update -qq
    # libportaudio2/libsndfile1: the audio modules are imported by the web app
    # (for recording playback) even though nothing records here.
    apt-get install -y -qq git curl ca-certificates nginx sqlite3 rsync sudo openssl \
        libportaudio2 libsndfile1 >/dev/null
    info "ok"

    # -----------------------------------------------------------------------
    step "2/9 Service user '$SVC_USER'"
    if ! id "$SVC_USER" &>/dev/null; then
        useradd --system --create-home --home-dir "$DATA_HOME" --shell /usr/sbin/nologin "$SVC_USER"
        info "created"
    else
        info "exists"
    fi
    install -d -o "$SVC_USER" -g "$SVC_USER" -m 750 "$DATA_HOME" "$DATA_HOME/data"
    install -d -o "$SVC_USER" -g "$SVC_USER" -m 755 "$APP_DIR"

    # -----------------------------------------------------------------------
    step "3/9 uv"
    if ! command -v uv &>/dev/null; then
        curl -LsSf https://astral.sh/uv/install.sh \
            | env UV_INSTALL_DIR=/usr/local/bin UV_NO_MODIFY_PATH=1 sh >/dev/null
    fi
    info "$(uv --version)"

    # -----------------------------------------------------------------------
    step "4/9 Code: $repo @ $ref"
    if [[ ! -d $APP_DIR/.git ]]; then
        as_svc git clone --quiet "$repo" "$APP_DIR"
    else
        as_svc git -C "$APP_DIR" remote set-url origin "$repo"
    fi
    as_svc git -C "$APP_DIR" fetch --quiet --prune --tags origin
    local target="$ref"
    if as_svc git -C "$APP_DIR" rev-parse --verify --quiet "origin/$ref" >/dev/null; then
        target="origin/$ref"
    fi
    as_svc git -C "$APP_DIR" checkout --quiet --force --detach "$target"
    info "now at $(as_svc git -C "$APP_DIR" log --oneline -1)"

    # -----------------------------------------------------------------------
    step "5/9 Python dependencies (first run downloads a lot — Whisper/torch)"
    (cd "$APP_DIR" && as_svc uv sync --frozen --no-dev --quiet)
    info "ok"

    # -----------------------------------------------------------------------
    step "6/9 Configuration ($ENV_FILE)"
    install -d -m 755 "$ETC_DIR"
    if [[ ! -f $ENV_FILE ]]; then
        cat >"$ENV_FILE" <<EOF
# HelmLog hosted web server settings. Created by deploy-web.sh; edit freely,
# then: sudo systemctl restart helmlog-web
HELMLOG_HOSTING_DIR=$DATA_HOME/data
HELMLOG_HOSTING_PORT=$PORT
HELMLOG_HOSTING_IDLE_S=900
# Serve boats at <slug>.<domain> once you have a domain:
#HELMLOG_HOSTING_BASE_DOMAIN=helmlog.example.org
# Never true on this server — helmlog-hosting refuses to start if it is.
AUTH_DISABLED=false
SESSION_SECRET=$(openssl rand -hex 32)
# Used in password-reset emails. Set to this server's https:// address.
#PUBLIC_URL=https://helmlog-web.your-tailnet.ts.net
# Outgoing email for password resets (optional):
#SMTP_HOST=
#SMTP_PORT=587
#SMTP_FROM=
#SMTP_USER=
#SMTP_PASSWORD=
EOF
        info "created (new SESSION_SECRET generated)"
    else
        info "kept existing file"
    fi
    if [[ -n $base_domain ]]; then
        sed -i '/^#\{0,1\}HELMLOG_HOSTING_BASE_DOMAIN=/d' "$ENV_FILE"
        echo "HELMLOG_HOSTING_BASE_DOMAIN=$base_domain" >>"$ENV_FILE"
        info "base domain set to $base_domain"
    fi
    chown root:"$SVC_USER" "$ENV_FILE"
    chmod 640 "$ENV_FILE"
    cat >"$CONF_FILE" <<EOF
SAVED_REPO=$repo
SAVED_REF=$ref
SAVED_ADMIN=$admin
SAVED_FUNNEL=$funnel
EOF

    # The CLI wrapper runs as the service user with the server's settings, so
    # imports land with the right owner and data directory.
    cat >/usr/local/bin/helmlog-hosting <<EOF
#!/bin/sh
if [ "\$(id -un)" != "$SVC_USER" ]; then exec sudo -u "$SVC_USER" "\$0" "\$@"; fi
set -a; . "$ENV_FILE"; set +a
exec "$APP_DIR/.venv/bin/helmlog-hosting" "\$@"
EOF
    chmod 755 /usr/local/bin/helmlog-hosting

    if [[ -n $admin ]]; then
        id "$admin" &>/dev/null || die "--admin user '$admin' does not exist"
        local sudoers=/etc/sudoers.d/helmlog-web
        cat >"$sudoers.tmp" <<EOF
# Lets $admin import boat data (scripts/push-to-web.sh) without a password.
$admin ALL=($SVC_USER) NOPASSWD: /usr/local/bin/helmlog-hosting, /usr/bin/rsync
EOF
        visudo -cf "$sudoers.tmp" >/dev/null || die "generated sudoers file is invalid"
        chmod 440 "$sudoers.tmp"
        mv "$sudoers.tmp" "$sudoers"
        info "$admin may run helmlog-hosting and rsync as $SVC_USER"
    else
        warn "No --admin user: run imports as root, or re-run with --admin <you>"
    fi

    # -----------------------------------------------------------------------
    step "7/9 systemd service helmlog-web"
    cat >/etc/systemd/system/helmlog-web.service <<EOF
[Unit]
Description=HelmLog hosted web server
After=network-online.target
Wants=network-online.target

[Service]
User=$SVC_USER
Group=$SVC_USER
EnvironmentFile=$ENV_FILE
WorkingDirectory=$APP_DIR
ExecStart=$APP_DIR/.venv/bin/helmlog-hosting serve
Restart=on-failure
RestartSec=5
NoNewPrivileges=true
ProtectSystem=full
ProtectHome=true
PrivateTmp=true
ReadWritePaths=$DATA_HOME

[Install]
WantedBy=multi-user.target
EOF
    systemctl daemon-reload
    systemctl enable --quiet helmlog-web
    systemctl restart helmlog-web
    info "restarted"

    # -----------------------------------------------------------------------
    step "8/9 nginx"
    sed "s/__PORT__/$PORT/" "$APP_DIR/scripts/nginx/helmlog-web.conf" \
        >/etc/nginx/sites-available/helmlog-web
    ln -sf /etc/nginx/sites-available/helmlog-web /etc/nginx/sites-enabled/helmlog-web
    rm -f /etc/nginx/sites-enabled/default
    nginx -t -q
    systemctl reload nginx
    info "ok"

    local ok=""
    for _ in $(seq 1 30); do
        if curl -fsS http://127.0.0.1/healthz >/dev/null 2>&1; then ok=1; break; fi
        sleep 1
    done
    [[ -n $ok ]] || die "server did not answer on /healthz — check: journalctl -u helmlog-web -n 50"
    info "health check passed"

    # -----------------------------------------------------------------------
    step "9/9 Tailscale"
    local ts_name=""
    if [[ $use_tailscale -eq 0 ]]; then
        info "skipped (--no-tailscale)"
    else
        if ! command -v tailscale &>/dev/null; then
            curl -fsSL https://tailscale.com/install.sh | sh >/dev/null
        fi
        if ! tailscale status &>/dev/null; then
            warn "Tailscale is not logged in. Run:  sudo tailscale up"
            warn "then re-run this script to publish the site."
        else
            if [[ -n $funnel ]]; then
                tailscale funnel --bg 80 >/dev/null
                info "public via Tailscale Funnel"
            else
                tailscale serve --bg 80 >/dev/null
                info "tailnet-only via Tailscale serve"
            fi
            ts_name=$(tailscale status --json \
                | "$APP_DIR/.venv/bin/python" -c 'import json,sys; print(json.load(sys.stdin)["Self"]["DNSName"].rstrip("."))')
        fi
    fi

    # -----------------------------------------------------------------------
    step "Done — $(as_svc git -C "$APP_DIR" log --oneline -1)"
    echo "  Boats:   helmlog-hosting boat list"
    if [[ -n $ts_name ]]; then
        echo "  URL:     https://$ts_name"
        if ! helmlog-hosting boat list | grep -q "$ts_name"; then
            echo "  First boat (serves on this URL):"
            echo "    helmlog-hosting boat add <slug> --name \"<Boat name>\" --host $ts_name --default"
        fi
    fi
    echo "  Logs:    journalctl -u helmlog-web -f"
    echo "  Data:    push from the Pi with scripts/push-to-web.sh (docs/web-hosting-deployment.md)"
}

main "$@"

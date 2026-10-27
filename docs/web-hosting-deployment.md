# Deploying the HelmLog website (hosted web server)

This guide takes you from nothing to a working HelmLog website: a server on
your Proxmox homelab that lets crew log in and review races, tracks, audio and
video without the boat's Pi being on or reachable.

Design background: [docs/specs/websitehosting.md](specs/websitehosting.md).

## What you get

```
  Pi on the boat                      Proxmox guest (Debian 12)
  ───────────────                     ─────────────────────────────────────────
  helmlog (logging)                   Tailscale serve/funnel  (HTTPS)
  data/logger.db  ── push-to-web.sh ─▶   └─ nginx :80
                     (SSH, on demand)          └─ helmlog-hosting serve :3003
                                                    ├─ corvo    → boats/corvo/db/…db
                                                    └─ creative → boats/creative/db/…db
```

- **One server, many boats.** Each boat has its own SQLite file, the same
  schema the Pi uses, and its own user accounts copied from that boat's Pi.
- **The hostname picks the boat.** For now the server has a single Tailscale
  name, assigned to one *default* boat. Per-boat addresses
  (`corvo.yourdomain`) work once you have a domain (see
  [Later: a real domain](#later-a-real-domain-per-boat-addresses)).
- **Review only.** Live and hardware features aren't on the website: race-start
  timer, cameras, network settings, deploy/update, and co-op/federation.
  Those pages return "not found" there.
- **Data arrives by manual push.** You run one command on the Pi when the boat
  has a good connection. It replaces that boat's data on the website with the
  Pi's current data.

> **Important:** each push *replaces* the boat's data on the website. Anything
> created on the website itself (moments, comments, tags, notes) is lost on
> the next push. Make those changes on the Pi for now. Proper two-way notes
> come with the incremental sync (spec decisions 5 and 8).

## Before you start

- A Proxmox host (spec decision 16: a couple of vCPUs and a few GB RAM is plenty).
- The Tailscale account your boats already use. In the
  [Tailscale admin console](https://login.tailscale.com/admin/dns), turn on
  **MagicDNS** and **HTTPS Certificates**. Both are needed for the
  `https://…ts.net` address.
- SSH access to the Pi, as you use today.

Roughly 30–45 minutes. Most of it is waiting on downloads.

---

## Step 1 — Create the Debian 12 container on Proxmox

A lightweight LXC container is enough. (A full VM also works. If you use one,
skip the `/dev/net/tun` part below.)

1. **Get the template.** In the Proxmox web UI, select your storage (for
   example `local`) → **CT Templates** → **Templates**, search `debian-12`,
   and download `debian-12-standard`.
2. **Create CT** (top-right button):
   - **General:** hostname `helmlog-web`, set a root password, and leave
     **Unprivileged container** ticked.
   - **Template:** `debian-12-standard`.
   - **Disks:** 32 GB. The Python dependencies take several GB (Whisper pulls
     in torch); the rest is boat data and video.
   - **CPU:** 2 cores. **Memory:** 4096 MB, swap 1024 MB.
   - **Network:** DHCP on your LAN bridge (`vmbr0`).
   - Finish, but **don't start it yet**.
3. **Let Tailscale run inside the container.** On the Proxmox host shell
   (Proxmox UI → your node → **Shell**), add two lines to the container's
   config. Replace `<ID>` with the container number, for example `105`:

   ```bash
   cat >> /etc/pve/lxc/<ID>.conf <<'EOF'
   lxc.cgroup2.devices.allow: c 10:200 rwm
   lxc.mount.entry: /dev/net/tun dev/net/tun none bind,create=file
   EOF
   ```

4. **Start the container**, then open its **Console** and log in as `root`.
5. **Create your own login user** with sudo. Use the same username as on the
   Pi to keep things simple:

   ```bash
   apt-get update && apt-get install -y sudo git
   adduser mark
   usermod -aG sudo mark
   ```

From here on, log in as that user (`ssh mark@<container-ip>` from your PC, or
the Proxmox console).

## Step 2 — Install HelmLog with the deploy script

```bash
git clone https://github.com/MarkPre3802/helmlog.git ~/helmlog-setup
sudo bash ~/helmlog-setup/scripts/deploy-web.sh --admin mark
```

`--admin mark` lets your user import boat data without a password prompt
(`/etc/sudoers.d/helmlog-web`). To deploy something other than `main`, add
`--ref <branch-or-tag>`.

The script works through nine steps. It installs the packages, creates a
`helmlog` service user, installs `uv`, checks the code out to
`/opt/helmlog-web`, installs the Python dependencies (the slow part, because
Whisper pulls in torch), and writes the configuration. It then sets up the
`helmlog-web` systemd service and nginx, runs a health check, and handles
Tailscale. The first time, it stops at Tailscale with a warning. That's
expected; Step 3 covers it.

After this, `~/helmlog-setup` isn't needed (`rm -rf ~/helmlog-setup`). The
script maintains its own checkout in `/opt/helmlog-web`.

## Step 3 — Put it on your tailnet

```bash
sudo tailscale up            # opens a login link — approve it in the browser
sudo /opt/helmlog-web/scripts/deploy-web.sh
```

The second run publishes the site with `tailscale serve`, which makes it
reachable from devices on your tailnet only. It prints the address, for
example:

```
  URL:     https://helmlog-web.tail1234.ts.net
  First boat (serves on this URL):
    helmlog-hosting boat add <slug> --name "<Boat name>" --host helmlog-web.tail1234.ts.net --default
```

**Crew without Tailscale:** to reach the site from anywhere, re-run with
`--funnel`. It's then public on the internet at the same address, protected by
HelmLog's login:

```bash
sudo /opt/helmlog-web/scripts/deploy-web.sh --funnel
```

## Step 4 — Register your boat

Pick a short **slug**: lower-case letters, digits and `-`. It becomes the
boat's subdomain later. Use the command the script printed:

```bash
helmlog-hosting boat add corvo --name "Corvo" --host helmlog-web.tail1234.ts.net --default
helmlog-hosting boat list
```

Then set the address used in password-reset emails, and restart:

```bash
sudo nano /etc/helmlog-web/helmlog-web.env
#   PUBLIC_URL=https://helmlog-web.tail1234.ts.net
#   (and SMTP_* if you want reset emails to send — same values as the Pi's .env)
sudo systemctl restart helmlog-web
```

Opening the URL now shows **"No boat is hosted at this address"**. That's
correct: the boat has no data until the first push.

## Step 5 — Push the boat's data from the Pi

### One-time: let the Pi SSH to the server

The Pi needs a HelmLog version that includes `scripts/push-to-web.sh`. Deploy
it to the Pi as usual first (push from Windows, then `git pull` on the Pi).

On the Pi:

```bash
ssh-keygen -t ed25519            # skip if ~/.ssh/id_ed25519 already exists
ssh-copy-id mark@helmlog-web     # the server's Tailscale name; enter mark's password once
ssh mark@helmlog-web helmlog-hosting boat list   # should list corvo, no password asked
```

### Each push

On the Pi, whenever the boat has a decent connection:

```bash
cd ~/helmlog
./scripts/push-to-web.sh --server mark@helmlog-web --boat corvo
```

The script takes a consistent snapshot of `data/logger.db` (safe while
HelmLog is logging), copies it over, and imports it. The server checks the
file and switches to the new data on the next page load, with no restart.

**Videos:** to mirror local video files (the ones the Pi serves at
`/videos/…`), add `--videos`. Only new or changed files are sent, and an
interrupted transfer resumes where it stopped:

```bash
./scripts/push-to-web.sh --server mark@helmlog-web --boat corvo --videos /home/mark/videos
```

YouTube-linked videos need nothing extra. The links travel with the database.

## Step 6 — Log in

Open the URL. Crew log in with **the same email and password they use on the
Pi**, because user accounts come across with the data. To add someone, invite
them on the Pi, then push. A password changed on the Pi reaches the website on
the next push.

---

## Updating the server

```bash
sudo /opt/helmlog-web/scripts/deploy-web.sh                 # latest of the saved branch
sudo /opt/helmlog-web/scripts/deploy-web.sh --ref some-tag  # a specific version (remembered)
```

**Keep the server at least as new as the Pis.** If a Pi runs newer code with
a newer database schema, the import refuses with *"newer than this server's
… Update the server first"*. Update the server, then push again. The server
upgrades older boat databases itself.

## Adding another boat

```bash
helmlog-hosting boat add creative --name "Creative"
```

Push from that boat's Pi with `--boat creative`. With only the Tailscale name,
just one boat (the default) is reachable in a browser. Others need their own
hostname: either a domain (below), or `--host` with another name that resolves
to this server. To switch which boat is the default:
`helmlog-hosting boat set-default creative`.

## Later: a real domain (per-boat addresses)

Once boats outside your tailnet join (spec decisions 3a and 18):

1. Point a wildcard `*.yourdomain` at the server. Cloudflare Tunnel is the
   planned route ([docs/https-deployment.md](https-deployment.md) covers the
   tunnel setup on the Pi; the server is the same with
   `service: http://localhost:80`).
2. `sudo /opt/helmlog-web/scripts/deploy-web.sh --base-domain yourdomain`
3. Every registered boat is then served at `https://<slug>.yourdomain`, with no
   per-boat setup. An unknown `<x>.yourdomain` returns "not found", never
   another boat's data.

Not done yet for multi-domain use: `PUBLIC_URL` is one setting for the whole
server, so password-reset emails link to that one address.

## What's where

| Path | What |
|---|---|
| `/opt/helmlog-web` | code checkout (managed by the deploy script) |
| `/etc/helmlog-web/helmlog-web.env` | server settings (secret, ports, email); edit, then restart |
| `/etc/helmlog-web/deploy.conf` | the deploy script's remembered `--repo/--ref/--admin/--funnel` |
| `/var/lib/helmlog-web/data/registry.db` | which boats exist and their hostnames |
| `/var/lib/helmlog-web/data/boats/<slug>/db/` | the boat's database (current + previous push) |
| `/var/lib/helmlog-web/data/boats/<slug>/videos/` | the boat's mirrored video files |

Back up `/var/lib/helmlog-web/data` if the website ever holds anything the Pis
don't. Today it doesn't: the Pis are the source of truth.

## Privacy note

A push copies the boat's database, **minus** what a review website must not
hold. Before anything leaves the Pi, the snapshot strips crew **voice
profiles** (biometric data, which the policy keeps person-private), the boat's
**Wi-Fi passwords**, and live **login, reset and invite tokens**. The server
strips them again on import. Crew accounts and password hashes are kept so
crew can log in. Audio transcripts, notes and race data are kept, because
that's what the site is for. HelmLog doesn't cache other boats' co-op tracks
locally, so none travel.

The website enforces the same logins, roles and coach-grant limits as the Pi,
because it runs the same code. If you use `--funnel` or a public domain, the
site is on the open internet behind that login, the same as exposing the Pi
through Cloudflare or Funnel today.

**Deletion requests** (a crew member asking to remove their audio, video or
email, for example): delete on the Pi, then push. The website's current copy is
replaced at once. The server keeps the *previous* push's file until the next
push removes it, so push twice to clear it completely. Videos mirrored with
`--videos` aren't deleted by a push: remove them from
`/var/lib/helmlog-web/data/boats/<slug>/videos/` as well. See
[docs/data-licensing.md](data-licensing.md).

## Troubleshooting

| Symptom | Check |
|---|---|
| Deploy script stops at "did not answer on /healthz" | `journalctl -u helmlog-web -n 50` |
| Browser: "No boat is hosted at this address" | No data pushed yet, or the hostname isn't assigned: `helmlog-hosting boat list` and compare the `hosts:` line with the URL |
| Push: `sudo: a password is required` | The deploy script was run without `--admin <you>`. Re-run it with `--admin` |
| Push: "newer than this server's" | Update the server first (see above) |
| Push: "not a HelmLog database" / "integrity check" | Push again. If it repeats, check the Pi's DB with `scripts/validate_snapshot.py` |
| `tailscale up` fails with `/dev/net/tun` errors | Step 1.3: the two `lxc.*` lines are missing from the container config. Add them and restart the container |
| URL doesn't load from a phone | The phone isn't on Tailscale. Install Tailscale on it, or use `--funnel` |
| A page loads but a panel is empty | That panel uses a live or host feature that isn't on the website (see *What you get*) |

Service control: `sudo systemctl status|restart helmlog-web`. Live logs:
`journalctl -u helmlog-web -f`.

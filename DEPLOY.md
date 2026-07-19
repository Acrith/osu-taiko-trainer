# Deploying taiko-trainer

Getting a hosted instance live from scratch. This walks through the whole
loop end-to-end: host, HTTPS, OAuth, backups.

Assumes you've already committed to a hosting provider (Oracle Cloud
Always Free, Hetzner, DigitalOcean, home hardware — see
`ARCHITECTURE.md`'s "Deploy" section for the tradeoffs).

## Prerequisites

- A domain you control (or pick a Cloudflare-owned subdomain via
  Cloudflare Tunnel — no domain purchase needed)
- Docker + Docker Compose on the host
- A registered osu! OAuth application with production redirect URI

## 1. Prepare the host

On a fresh Ubuntu 22.04 / 24.04 box:

```bash
# Install Docker
curl -fsSL https://get.docker.com | sudo sh
sudo usermod -aG docker $USER  # log out + back in for group to take effect

# Clone the repo
git clone https://github.com/Acrith/osu-taiko-trainer.git
cd osu-taiko-trainer
git checkout web
```

On Oracle Cloud Free ARM VM, use `aarch64` Ubuntu images; the Dockerfile
is platform-neutral so builds fine on ARM.

## 2. Configure OAuth for production

Go to <https://osu.ppy.sh/home/account/edit> → OAuth → your app (or
create a new "taiko-trainer public" one). Add a callback URL matching
your production domain:

```
https://taiko.your-domain.com/oauth/callback
```

Keep localhost callbacks too if you still develop locally with the same
app. Copy `client_id` and `client_secret` — you'll paste them into env
in the next step.

## 3. Configure env

```bash
cp .env.example .env
$EDITOR .env
```

Fill in every value. In particular:

- `OSU_OAUTH_REDIRECT_URI` must match exactly what you registered above
- `SESSION_SECRET` should be a fresh random string. Generate one:
  `python3 -c "import secrets; print(secrets.token_urlsafe(32))"`

Never commit `.env`. It's in `.gitignore` (via `*.env`).

## 4. First launch

```bash
docker compose up -d --build
docker compose logs -f taiko-trainer
```

You should see `[auth] running in WEB mode (osu! OAuth login enabled)`
followed by uvicorn's startup lines. If you see `LOCAL mode`, one of
the env vars didn't get through — check `docker compose config`.

At this point the app is listening on `127.0.0.1:8000` on the host but
not accessible externally. Next step is HTTPS + a public URL.

## 5. HTTPS via Cloudflare Tunnel (recommended)

Cloudflare Tunnel gives you a signed HTTPS URL, DDoS protection, and
IP-hiding — all free, without opening any ports on your host firewall.

### Set it up

1. Register your domain with Cloudflare (nameservers pointed to CF).
   Free plan is fine.
2. Cloudflare dashboard → Zero Trust → Networks → Tunnels → Create a
   tunnel. Give it a name (e.g. `taiko-trainer`).
3. Install the connector on your host:

```bash
# Ubuntu / Debian
curl -L --output cloudflared.deb https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-amd64.deb
sudo dpkg -i cloudflared.deb

# Or aarch64 for Oracle Cloud Free ARM:
# curl -L --output cloudflared.deb https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-arm64.deb
```

4. Copy the `cloudflared service install <token>` command from the
   Cloudflare dashboard and run it. Systemd unit gets installed +
   started automatically.
5. In the Cloudflare tunnel config: add a Public Hostname mapping:
   - **Subdomain**: `taiko` (or whatever)
   - **Domain**: your cloudflare-managed domain
   - **Service**: `http://localhost:8000`

Wait ~30 seconds for DNS to propagate. Then visit
`https://taiko.your-domain.com` — you should see the taiko-trainer home
page over real HTTPS.

### Update OAuth redirect URI to match

Now that you have the real production URL, update:
- The osu! OAuth app's callback URL
- The `OSU_OAUTH_REDIRECT_URI` env var
- `docker compose up -d` to restart with the new env

Login should work end-to-end at this point.

## 6. Backups

The `backup` service in `docker-compose.yml` is under the `prod` profile.
Start it:

```bash
docker compose --profile prod up -d backup
```

By default it snapshots the workspace nightly and keeps 7 days. Snapshots
live in the `backups` volume:

```bash
docker compose exec backup ls -1t /backups
```

For offsite storage, wire up a follow-up job that syncs `/backups/` to
Backblaze B2 or Cloudflare R2 (both free tiers cover this at hobby scale).
Simple approach: `rclone` in a second cron container.

## 7. Iteration + updates

```bash
git pull
docker compose up -d --build
```

Schema migrations run automatically via `_migrate_catalog_schema` and
`_migrate_plays_schema` on next connection. The uploader companion's
API tokens survive restarts; users stay logged in via cookie.

## 8. Monitoring

At this scale, "does it respond to a curl?" is enough:

```bash
curl -f https://taiko.your-domain.com/api/status
```

Add uptime monitoring via Uptime Kuma, Better Stack (free tier), or a
simple `curl-cron-then-slack` script when you get bored of checking
manually.

## Troubleshooting

**"invalid redirect_uri" from osu!** — the callback URI in the OAuth
app doesn't exactly match `OSU_OAUTH_REDIRECT_URI`. Check port, scheme,
trailing slashes.

**Login succeeds but header shows anonymous** — session cookie rejected.
Almost always the `secure=true` flag over plain HTTP. Ensure you're
accessing via HTTPS through Cloudflare, not by hitting the container's
localhost port.

**Uploader gets 401 on upload** — token was revoked or the user was
deleted. Have the user mint a new token at `/settings/tokens` and
re-run `taiko-uploader init`.

**Uploads work but /u/{username} shows no data** — the user hasn't
been linked to a per-player DB yet. First login runs
`ensure_player_db_for_user`; second login should be fine. If it
persists, check container logs for errors during the callback.

**Container keeps restarting** — `docker compose logs taiko-trainer`
should show the crash reason. Most common: env vars missing (mode is
`web` but OAuth values aren't set — the app refuses to start).

## Costs

Assuming Cloudflare Tunnel (free) + one small VM:

- Oracle Cloud Always Free ARM VM: **$0/month**
- Hetzner CAX11 ARM: **€3.50/month**
- DigitalOcean droplet 1GB: **$6/month**

Storage is dominated by replay files (~50 KB each). 1000 users × 100
replays each = 5 GB. Free tier of any provider covers this several
times over.

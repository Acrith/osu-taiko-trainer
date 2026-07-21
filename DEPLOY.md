# Deploying taiko-trainer

Getting a hosted instance live from scratch. Also covers the uploader
release + auto-update flow, since the two are wired together.

Assumes you've committed to a hosting provider (Oracle Cloud Always Free,
Hetzner CAX11, DigitalOcean, or home hardware — see the "Costs" section
at the bottom for tradeoffs).

## Prerequisites

- A domain you control OR Cloudflare Tunnel with a subdomain of one of
  their zones
- Docker + Docker Compose on the host
- A registered osu! OAuth app with a production redirect URI

## 1. Host prep

Fresh Ubuntu 22.04 / 24.04:

```bash
# Docker
curl -fsSL https://get.docker.com | sudo sh
sudo usermod -aG docker $USER    # log out + back in for the group to take effect

# Repo
git clone https://github.com/Acrith/osu-taiko-trainer.git
cd osu-taiko-trainer
```

On Oracle Cloud Free ARM VMs use the `aarch64` Ubuntu images —
Dockerfile is platform-neutral and builds fine on ARM.

## 2. osu! OAuth app

<https://osu.ppy.sh/home/account/edit> → OAuth → create (or reuse) an
app. Add a callback URL matching your production domain:

```
https://taiko.your-domain.com/oauth/callback
```

Keep any localhost callbacks you use for local dev. Copy `client_id` +
`client_secret` — you'll paste them into env in the next step.

## 3. `.env`

```bash
cp .env.example .env
$EDITOR .env
```

Fill in all five values:

```
TAIKO_TRAINER_MODE=web
OSU_OAUTH_CLIENT_ID=<from osu>
OSU_OAUTH_CLIENT_SECRET=<from osu>
OSU_OAUTH_REDIRECT_URI=https://taiko.your-domain.com/oauth/callback
SESSION_SECRET=<a fresh random string>
```

Generate a fresh `SESSION_SECRET`:

```bash
python3 -c "import secrets; print(secrets.token_urlsafe(32))"
```

`.env` is gitignored via `*.env`. Never commit it. For production put
these in a systemd `EnvironmentFile=` or your provider's secrets
manager rather than a plaintext file.

## 4. First launch

```bash
docker compose up -d --build
docker compose logs -f taiko-trainer
```

You should see `[auth] running in WEB mode (osu! OAuth login enabled)`
followed by uvicorn's startup lines. If you see `LOCAL mode`, one of
the env vars didn't get through — check `docker compose config`.

At this point the app listens on `127.0.0.1:8000` on the host but isn't
publicly reachable yet.

## 5. HTTPS via Cloudflare Tunnel (recommended)

Cloudflare Tunnel gives you an HTTPS URL, DDoS mitigation, and origin-IP
hiding — free, no firewall ports opened.

1. Register your domain with Cloudflare (nameservers pointed at CF). Free
   plan is fine.
2. CF dashboard → Zero Trust → Networks → Tunnels → Create a tunnel. Name
   it `taiko-trainer`.
3. Install the connector on your host:

   ```bash
   # amd64 (most VPS)
   curl -L --output cloudflared.deb \
     https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-amd64.deb
   sudo dpkg -i cloudflared.deb

   # aarch64 (Oracle Free ARM, Raspberry Pi)
   # curl -L --output cloudflared.deb \
   #   https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-arm64.deb
   ```

4. Copy the `cloudflared service install <token>` command from the CF
   dashboard, run it. Systemd unit installs + starts automatically.
5. In the tunnel's "Public Hostname" tab, add a mapping:
   - **Subdomain**: `taiko` (or whatever)
   - **Domain**: your zone
   - **Service**: `http://localhost:8000`

Wait ~30s for DNS. Visit `https://taiko.your-domain.com` — taiko-trainer
home page loads over real HTTPS.

Update `OSU_OAUTH_REDIRECT_URI` in `.env` + the osu! OAuth app's callback
to match the production URL, then `docker compose up -d` to restart.
Login should work end-to-end now.

## 6. Backups

The `backup` service in `docker-compose.yml` sits under the `prod`
profile:

```bash
docker compose --profile prod up -d backup
```

Snapshots the workspace nightly, keeps 7 days. They land in the
`backups` volume:

```bash
docker compose exec backup ls -1t /backups
```

For offsite, follow up with `rclone` in another cron container syncing
to Backblaze B2 or Cloudflare R2 (both free-tier at hobby scale).

## 7. Updating the server

```bash
git pull
docker compose up -d --build
```

Schema migrations run automatically via `_migrate_catalog_schema` and
`_migrate_plays_schema` on next connection. Sessions survive restarts
(cookie-based). Uploader API tokens survive restarts.

## 8. Uploader releases

The uploader companion (`uploader-app/`) is a Tauri 2 Windows app with
its own CI workflow (`.github/workflows/build-uploader-tauri.yml`).

### Cutting a release

```bash
# Bump the version in these two files (or let CI auto-sync from the tag)
$EDITOR uploader-app/src-tauri/Cargo.toml           # version = "0.3.0"
$EDITOR uploader-app/src-tauri/tauri.conf.json      # "version": "0.3.0"
git commit -am "Uploader: 0.3.0"
git push origin main

git tag uploader-v0.3.0
git push origin uploader-v0.3.0
```

The tag push triggers the workflow. In ~6 minutes:

- Windows CI runner rebuilds the app, signs it with your minisign
  private key (see next section)
- Creates a GitHub Release named `uploader-v0.3.0`
- Attaches `taiko-uploader_0.3.0_x64-setup.exe` (NSIS installer),
  `.sig`, `.msi`, portable `.exe`, and `latest.json`

`/releases/latest` on GitHub now points at this release. Any installed
client on an older version will prompt to update within 3s of next
launch.

### Signing keys — one-time setup

The auto-updater refuses to install anything not signed by the private
key matching the pubkey in `uploader-app/src-tauri/tauri.conf.json`.

```bash
# Generate the keypair
cd uploader-app
npx @tauri-apps/cli signer generate --password ""

# Public key → paste into tauri.conf.json's plugins.updater.pubkey
# Private key → GitHub repo Settings → Secrets → Actions →
#              add TAURI_SIGNING_PRIVATE_KEY (paste the whole file)
```

**Back the private key up outside GitHub** (password manager, encrypted
note). If you lose it, you can't sign updates for the existing pubkey —
every installed client's auto-updater breaks and users have to reinstall
manually with a new pubkey baked in.

### Version-from-tag safety net

The workflow has a `Sync version from tag` step that rewrites
`Cargo.toml` + `tauri.conf.json` from the tag name before building. So
if you forget to bump the version files, the resulting binary still
reports the tag's version. Prevents the "installed a `0.3.0` that
reports itself as `0.2.0` and re-prompts forever" trap.

## 9. Monitoring

At this scale, "does it respond to a curl?" is enough:

```bash
curl -f https://taiko.your-domain.com/api/status
```

For automated alerting: Uptime Kuma or Better Stack (both free at hobby
usage). Or a simple `curl-cron-then-webhook` script.

## Troubleshooting

**"invalid redirect_uri" from osu!** — the callback URL in the OAuth app
doesn't exactly match `OSU_OAUTH_REDIRECT_URI`. Check scheme, host,
port, trailing slash.

**Login succeeds but the header shows anonymous** — session cookie
rejected. Almost always the `secure=true` flag over plain HTTP. Ensure
you're accessing via HTTPS through Cloudflare, not by hitting the
container's localhost port from a browser.

**Uploader gets 401 on upload** — token was revoked or the user was
deleted server-side. Have the user mint a fresh token at
`/settings/tokens` and paste it into the uploader's Settings.

**Uploads work but `/u/<name>` shows no data** — the user hasn't been
linked to a per-player DB yet. First login runs
`ensure_player_db_for_user`; second login should be fine. If it
persists, check container logs for exceptions in the callback.

**Uploader install loops on the same version** — you shipped a binary
whose reported version doesn't match `latest.json`. Almost always a
version-bump mistake. Cut a new tag with the version files matching the
tag name. The `Sync version from tag` CI step prevents this now, but a
release cut before that step existed can have this shape.

**SmartScreen blocks the uploader install** — the binary isn't
code-signed for Windows publisher trust yet. Users click **More info**
→ **Run anyway**. If the app is silently blocked after install:
Right-click the `.exe` → Properties → tick **Unblock** at the bottom
(Mark-of-the-Web attribute).

**Container keeps restarting** — `docker compose logs taiko-trainer`
usually names the problem. Most common: env vars missing (mode is
`web` but OAuth values aren't set — the app refuses to start).

## Costs

Assuming Cloudflare Tunnel (free) + one small VM:

| Host                          | Monthly cost |
|-------------------------------|--------------|
| Oracle Cloud Always Free ARM  | $0           |
| Hetzner CAX11 ARM             | €3.50        |
| DigitalOcean 1GB droplet      | $6           |

Storage is dominated by replay files (~50 KB each). 1000 users × 100
replays each = 5 GB. Any of the above covers that several times over.

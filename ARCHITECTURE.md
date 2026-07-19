# taiko-trainer — hosted web service architecture

Living design doc for the web build. Iterating in-place; if a decision here
turns out wrong under real usage, update the doc rather than let the code
drift silently.

## What we're building

A hosted training analytics service for osu!taiko players. Same skill-vector
+ diagnostic engine that already exists, exposed as a shared web service so:

- Players don't install anything to view their reports
- Uploaded maps and skill data compound into a shared catalog
- Players can inspect each other's profiles (like osu! itself)
- The scoring model can be iterated centrally against real play data

A thin native uploader companion watches the user's `Data/r/` folder locally
and auto-uploads new replays as they appear — restoring the seamless loop the
local app has today.

## Non-goals for v1

- Sync between two devices from a single user (they can log in from both
  browsers and both uploaders will push to the same account, so effectively
  synced anyway; no explicit "multi-device" primitive)
- Editing maps or replays server-side
- Real-time collaboration
- Anything requiring modification to osu! itself

## Architecture at a glance

```
[osu! Songs/ + Data/r/]                  [Browser]
        │                                    │
        ▼                                    ▼
[uploader companion]  ────────► [FastAPI web service] ◄──── [osu! OAuth]
   watches folder                       │
   POSTs new .osr                       ▼
                                   [Postgres]
                                    (shared catalog +
                                     per-user replays/snapshots)
```

**Two client surfaces**, one server:
- Browser = read + configure (Log in with osu!, view report, browse others,
  trigger refresh)
- Uploader companion = write-only (auth token → POST /upload/replay for each
  new file it sees)

The server never talks to the user's disk. Everything crosses the wire as
either a browser action or a POST from the uploader.

## Data model

Multi-tenant with two shapes of table:

### Shared tables (no user_id)

Public information — the same for everyone, deduplicated across the whole
service.

- `maps` — catalog. Deduped by md5. First user to upload a map contributes
  it to the global pool; everyone else's replays link to that same row.
- `catalog_meta` — service-level config

### Per-user tables (`user_id` FK)

Owned by one user, visible per that user's privacy settings.

- `users` — profile. One row per authenticated osu! account.
- `replays` — the .osr uploads with judgment/classification/mods
- `snapshots` — session skill vectors
- `player_info` → collapses into `users` (osu profile fields live there)

Migration from the current per-player SQLite: existing local `<name>.db`
maps 1:1 to `(users.id = X, replays.user_id = X)` rows. The migration
script (Task #68) handles the shape change.

### `users` schema

```sql
CREATE TABLE users (
  id                  BIGSERIAL PRIMARY KEY,
  osu_user_id         BIGINT UNIQUE NOT NULL,
  osu_username        TEXT NOT NULL,
  osu_avatar_url      TEXT,
  osu_cover_url       TEXT,
  osu_country_code    TEXT,
  osu_global_rank     INTEGER,
  style               TEXT NOT NULL DEFAULT 'unknown',  -- kddk, ddkk, kkdd
  profile_public      BOOLEAN NOT NULL DEFAULT TRUE,
  created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  last_login_at       TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
```

## Auth

- **Login**: osu! OAuth 2.0 authorization code flow. User clicks "Log in
  with osu!" → redirect to osu! → callback with code → exchange for token →
  fetch profile → upsert users row → issue signed session cookie
- **Session cookie**: httpOnly, secure, SameSite=Lax. Contains signed
  `user_id` + issued_at. Expires 30 days, refreshed on each visit
- **Uploader companion auth**: on first launch, opens browser to
  `/uploader/auth`. User approves. Server issues a long-lived API token
  (opaque, revocable) that the uploader stores locally
- **Middleware**:
  - `require_login` — 302 to `/login` if no session
  - `current_user_or_none` — sets `request.state.user` if logged in, None
    otherwise. Used on public routes so we can highlight "this is you"
  - `require_api_token` — for uploader endpoints; header `Authorization:
    Bearer <token>`

## URL structure

```
/                                landing / login prompt for anon; feed for logged in
/login                           OAuth start
/oauth/callback                  OAuth exchange
/logout                          clear session

/u/{osu_username}                public player report (respects profile_public)
/u/{osu_username}/train/{dim}    public per-dim recommendations
/u/{osu_username}/replay/{id}    public replay detail (respects visibility)

/me                              redirect to /u/<your_username>
/settings                        style, profile visibility, delete account
/upload                          drag-and-drop web upload (browser fallback)

/map/{md5}                       shared map detail — leaderboards per mod
/compare/{a}/{b}                 side-by-side skill vectors

/api/v1/replays                  POST — uploader companion / web upload
/api/v1/uploader/token           GET/POST — token issuance
/api/v1/health                   uptime probe
```

Note: player pages moved from `/player/{name}` (local) to `/u/{osu_username}`
(web). `/u/` mirrors GitHub / osu! conventions and disambiguates from other
top-level routes.

## Endpoint boundaries — what the browser vs uploader can do

**Browser** (session cookie):
- Read anything public
- Read own private data
- Trigger refresh on own data
- Delete own data
- Change settings

**Uploader** (API token):
- POST /api/v1/replays with an .osr file
- Nothing else. Explicitly cannot delete, cannot read

Blast-radius design: a compromised uploader token can spam a user's own
replay list at worst, not modify anything else.

## Rate limiting

- Uploader: 100 replays per 5 minutes per token (real bulk uploads finish
  in seconds; genuine over-100 usage is a red flag)
- Browser: 60 requests per minute per session, 30 per minute per IP for
  anon
- Public API: 200 per 5 minutes per IP

## Storage sizing (rough)

Small `.osr`: 20-80 KB. 1000 users × 100 replays each × 60 KB avg = 6 GB.
Well within free-tier limits. Maps table growth is bounded because we dedup
by md5 (there are ~50-100K ranked taiko maps total; if the whole ranked pool
gets ingested, that's ~5 GB of .osu blobs).

## Migration strategy from current local

Two paths, non-mutually-exclusive:

1. **Uploader companion + web upload flow** — new users get everything from
   the moment they log in and point the uploader at Data/r/. Backfill any
   old replays via drag-and-drop bulk upload.

2. **`taiko-trainer migrate --workspace <path>`** — reads a local sqlite
   workspace, POSTs its maps + replays + snapshots to the service under the
   authenticated user's id. Idempotent (map md5 dedup, replay unique on
   (map_md5, played_at)).

The local single-user app keeps working. It's the same codebase; multi-user
mode is `user_id = 1` by default when no auth is configured. This is
important for the transition — the user's local workflow doesn't break
while we build the hosted version.

## What stays untouched

- All scoring logic (`scoring.py`, `features.py`, `player.py`,
  `classification.py`)
- All mod handling (`mods.py`, `judgment.py`)
- All rating dimensions (speed, stamina, gimmick, technical, consistency,
  reading)
- The HTML/CSS in `server.py` — mostly. Some routes rename, some public
  pages get privacy checks, but the render functions are unchanged

The web build is a routing + auth + storage adaptation. Not a rewrite.

## Deploy

- **v1 dev instance**: Docker container on either Oracle Cloud Always Free
  ARM VM or $5/mo Hetzner. Postgres via Neon.tech free tier (0.5 GB) or
  co-located on same box
- **v1 public**: same shape, moved onto a right-sized paid host if needed.
  Cloudflare in front (free) for CDN + HTTPS + basic DDoS
- **Backups**: daily pg_dump to remote object storage (Backblaze B2 free
  tier, or Cloudflare R2)

## Order of implementation

The tasks (#63-#69) run roughly in this order. Each phase leaves the app in
a working state — you can always run the local single-user version off
`main` while `web` is under construction.

1. Auth scaffolding — osu! OAuth flow, session cookies (#63)
2. Schema multi-tenancy — user_id columns, users table, local workspace
   migration to "single user" (#64)
3. Routes adapt to auth-aware — public vs private, session context (#65)
4. Postgres swap — DB abstraction, connection string, migrations (#66)
5. Uploader companion — separate small binary (#67)
6. Local→hosted migration script (#68)
7. Deploy dev instance (#69)

## Decisions deferred until we have a working prototype

- Public API: whether to expose read-only endpoints for third parties
- Comparison / leaderboard UI shapes (do it after real users tell us what
  they want to compare)
- Notifications (probably never)
- Discord bot / integration (probably later, if community interest)
- Custom map ratings (users tagging maps with their own difficulty
  assessment) — interesting but explodes the surface

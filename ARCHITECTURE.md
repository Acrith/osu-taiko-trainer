# taiko-trainer architecture

How the hosted service actually works today. If you're changing the code,
this is the map. When a decision here turns out wrong under real usage,
update the doc rather than let the code silently drift.

## What it is

A hosted training analytics service for osu!taiko players. Uploaded
`.osr` files get parsed against their `.osu` map, judged for
timing/miss/verdict per note, and folded into a six-dimensional skill
snapshot the user can compare against theirs from last week or against
the leaderboard.

## Components

```
[osu! Songs/ + Data/r/]                        [Browser]
        │                                          │
        ▼                                          ▼
[Tauri uploader companion]  ────► [FastAPI + SQLite] ◄──── [osu! OAuth]
   watches folder                       │
   POSTs new .osr                       ▼
                                 workspace/*.db
                                (shared catalog + per-user replays)
```

**Three surfaces**, one server:

- **Browser** (session cookie): read own + public reports, browse
  leaderboards, upload one-off replays, edit settings.
- **Uploader companion** (bearer token): auto-uploads `.osr` files.
  Explicitly cannot read anything back — blast-radius design keeps a
  compromised uploader token to "spam my own replay list."
- **Python CLI** (no auth): local single-user use. Same code paths, just
  runs against a local workspace directory instead of the hosted one.

## Storage

**SQLite, not Postgres.** The Postgres swap is on the backlog (task #66)
but has never surfaced as a real bottleneck. A workspace directory
contains:

```
workspace/
├── catalog.db      shared map catalog. One row per unique md5;
│                   raw .osu blob stored so replays are portable.
└── <username>.db   one file per user. Their .osr blobs (as content),
                    judgment results, mods, ratings, snapshots.
```

Both live on the server filesystem, backed up nightly (see
`docker-compose.yml`'s `backup` profile).

### Key tables

Catalog:

- `users` — one row per osu! account. `osu_user_id`, `osu_username`,
  `osu_avatar_url`, `osu_cover_url`, `osu_country_code`,
  `osu_global_rank`. `profile_public` gates the `/u/{name}` route.
- `api_tokens` — hashed uploader tokens. `owner_user_id`, `hash`,
  `last_used_at`.
- `maps` — deduped by md5. Stores parsed `.osu` blob + the six
  dimension ratings. Rating lives here (not per-user) because a map's
  difficulty is the same for everyone.

Per-player DB (`<username>.db`):

- `player_info` — style (`kddk` / `ddkk` / `kkdd` / `unknown`),
  osu profile mirror, map search roots.
- `replays` — one row per uploaded `.osr`. Full content BLOB, judgment
  counts, effective mod-adjusted ratings, `content_hash` (sha256 of
  first 512 bytes — used by the uploader to cross-reference).
- `snapshots` — the skill vector at each point in time. Recomputed on
  every ingest. Rows: `skill_speed`, `skill_stamina`, `skill_gimmick`,
  `skill_technical`, `skill_consistency`, `skill_reading`,
  `replays_used`, `latest_replay_played_at`.

## Auth

- **Web login**: osu! OAuth 2.0 authorization code flow. Callback runs
  `ensure_player_db_for_user` (creates their per-player DB if missing)
  and issues a signed session cookie (`itsdangerous`-backed, HttpOnly,
  SameSite=Lax, 30-day expiry rolling).
- **Uploader auth**: bearer token minted at `/settings/tokens`. Stored
  hashed server-side; opaque + revocable. Rate-limited implicitly via
  `INSERT OR IGNORE` uniqueness on `(map_md5, played_at)`.
- **Middleware**: three helpers on the request path — `current_user`
  (session cookie → user row or None), `require_login` (302 to `/`),
  `require_api_token` (parses `Authorization: Bearer …`).

Local mode has no auth — `TAIKO_TRAINER_MODE=local` (or unset) skips
OAuth entirely and treats everyone as user_id=1.

## Routes

Web pages:

```
/                                landing / feed
/login  /oauth/callback  /logout OAuth flow

/u/{osu_username}                public player report
/u/{osu_username}/train/{dim}    per-dim recommendations
/replay/{player}/{id}            one replay's judgment breakdown
/replay/{player}/{id}/inspect    per-note inspector for debugging scoring
/replay/{player}/{id}/osr        download the raw .osr file

/me                              redirect to /u/<self>
/settings                        style, profile visibility, delete account
/settings/tokens                 mint / revoke uploader tokens

/leaderboards                    total-skill top-N + six per-dim columns
/leaderboards/{dim}              full ranking for one dimension

/maps                            browsable map catalog with filters
/map/{md5}                       map detail — ratings + top plays

/upload#companion                install instructions + download button
/upload                          drag-drop web upload (one-off imports)
/download                        302 → /upload#companion
```

Uploader-facing API:

```
GET  /api/v1/whoami              identity for the Home identity band
GET  /api/v1/me/skill            skill snapshot + total-skill rank
GET  /api/v1/me/replays          user's replays + content hashes
POST /api/v1/replays             upload one .osr (multipart)
POST /api/v1/maps                seed catalog with a .osu (idempotent)
```

Public health:

```
GET  /api/status                 uptime + workspace stats
```

## Scoring pipeline

Same code path whether triggered by browser upload, uploader companion
POST, or CLI. Follow `workflow.py::add_replay`:

1. **Parse the .osr** (`osr_parser`). Extract player name, mods,
   played_at, key events, reported counts. Detect if lazer via
   `game_version >= 30_000_000` or extra bytes past the stable layout.
2. **Resolve the map**. Try catalog first by md5. If missing, walk the
   player's configured Songs roots. If still missing and osu! API is
   configured, fall back to a live API fetch.
3. **Judge every note** (`judgment.judge_replay`). Pair each hittable
   note with the earliest color-matching key-down within the OD-scaled
   miss window. Classify as GREAT / OK / MISS. Lazer mode skips the
   stable notelock rule.
4. **Extract features** (`features.extract_features`). Per-map: BPM
   distribution, density windows, mono-color runs, SV changes,
   note-diameter kickbacks, denden count.
5. **Rate the map** (`scoring.rate_map`). Six numbers from the features
   + the effective mods bitfield. Cached on `maps.rating_{dim}` — only
   recomputed on `refresh`.
6. **Classify missing** (`classification.classify_misses`). Bucket each
   miss by cause (speed cap, denden overload, mono-run fatigue, etc.).
7. **Compute effective rating**. Base map rating × mod multipliers × an
   accuracy scaling. Stored on the replay row.
8. **Update snapshot** (`player.compute_player_skill`). Per-dim, keep
   the best score per (map_title, map_diff), sort desc, weight by
   `0.9 ** rank`, sum. Same structural pattern as pp.

## Uploader companion

Tauri 2 app under `uploader-app/`. See `uploader-app/README.md` for
implementation details; key contract:

- Watches the configured folder via `notify`.
- New `.osr` → 500ms settle → HTTP multipart to `/api/v1/replays`.
- Retries with exponential backoff (2s → 60s cap) on 5xx / network.
- Auth failures (401), foreign-replay (403), duplicates (409) are
  terminal skips; recorded locally so we don't re-attempt.
- SKIPPED_HISTORIC snapshot on startup — every existing `.osr` is
  marked as "already seen" so the watcher only touches genuinely-new
  plays. Explicit `Backfill` / selective-upload paths let users opt
  into historic imports.
- Emits status events to the UI (`starting` / `watching` / `uploading`
  / `error` / `no_config`) with a shared `Mutex<StatusPayload>` slot
  so late-attaching listeners still see the current state via
  `get_current_status` command.
- Auto-updates against signed `latest.json` on GitHub Releases —
  minisign signature verified against the pubkey baked into
  `tauri.conf.json`.

## Rate ratings — the six dimensions

| Dim         | What it measures                                    |
|-------------|-----------------------------------------------------|
| speed       | Motor tempo demand. Peak sustained BPM at density.  |
| stamina     | Per-note strain accumulation over a whole map.      |
| gimmick     | SV surprises, aspect kickbacks, denden overload.    |
| technical   | Sustained mid-density divisor complexity.           |
| consistency | Note-diameter timing tightness across a session.    |
| reading     | Physics-based runway_ms + motor-cognitive coupling. |

Each ranges 0..∞ but real maps top out around 4000-6000 per dim.
Formulas live in `scoring.py`; see `player.py` for the aggregation.

Playstyle affects **stamina** (KDDK never does same-hand consecutive
hits at fast tempo; DDKK naturally does on mono-color runs) and
**consistency** (cheese detection). The `style` column on
`player_info` drives this — set via `/settings` or `taiko-trainer
player`.

## Non-goals

- Real-time collaboration
- Modifying maps or replays server-side
- osu! itself talking to us — no plugin, no mod, no injection
- Postgres migration until write concurrency actually matters

## What's not built yet

- `#66` SQLite → Postgres. Deferred until write concurrency matters.
- `#105` Per-map replay history + progression chart on the map detail
  page.
- `#113` Identity gate when a user renames on osu!.
- `#117` Split-ladder view (stable-only vs. lazer-including).

## Deploy shape

Docker Compose on a small VM behind Cloudflare Tunnel. See
`DEPLOY.md` for the walkthrough. Costs $0–6/month at hobby scale.

"""Split-schema persistence: catalog.db + per-player <player>.db.

Two SQLite databases per workspace:

    catalog.db          — the maps catalog (shared across players)
        maps            — one row per unique .osu, including the raw content BLOB
                          so the DB is fully self-contained
        catalog_meta    — versioning / update markers

    <player>.db         — one file per player. Filename == player name.
        player_info     — { name, style, notes }
        replays         — one row per .osr, INCLUDING the raw .osr content BLOB
                          (foreign key by md5 to catalog.maps)
        snapshots       — 5-D skill vectors over time
        map_roots       — local filesystem search paths (client-only)
        config          — local settings

For queries that need map + replay data together we open the player DB and
ATTACH the catalog as `catalog`, so joins look like:
    SELECT r.*, m.title FROM replays r JOIN catalog.maps m ON m.md5 = r.map_md5

Because every parsed file is stored as a BLOB, the DB pair is fully portable:
copy them to any machine and everything works — including re-rating after
scoring formula changes.
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .cheese import CheeseReport
from .classification import FailureSummary
from .features import MapFeatures
from .judgment import JudgedReplay
from .models import TaikoBeatmap, TaikoReplay
from .player import PlayerSkill
from .scoring import DimensionRating


# -----------------------------------------------------------------------------
# Schemas
# -----------------------------------------------------------------------------

_CATALOG_SCHEMA = """
CREATE TABLE IF NOT EXISTS maps (
    md5                    TEXT PRIMARY KEY,
    content                BLOB NOT NULL,           -- raw .osu bytes
    artist                 TEXT NOT NULL,
    title                  TEXT NOT NULL,
    version                TEXT NOT NULL,
    creator                TEXT NOT NULL,
    beatmap_id             INTEGER,
    beatmapset_id          INTEGER,
    mode                   INTEGER,
    duration_s             REAL NOT NULL,
    hittable_notes         INTEGER NOT NULL,
    bpm_min                REAL,
    bpm_max                REAL,
    od                     REAL,
    rating_speed           REAL NOT NULL,
    rating_stamina         REAL NOT NULL,
    rating_gimmick         REAL NOT NULL,
    rating_technical       REAL NOT NULL,
    rating_consistency     REAL NOT NULL,
    rating_reading         REAL,
    parity_mean            REAL NOT NULL,
    parity_hostile_ratio   REAL NOT NULL,
    inserted_at            TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS catalog_meta (
    key                    TEXT PRIMARY KEY,
    value                  TEXT NOT NULL,
    updated_at             TEXT NOT NULL
);

-- Users table for the hosted web build (Task #63, feature branch `web`).
-- Populated by osu! OAuth login. In local mode this table stays empty
-- and the tool falls back to the implicit single-user path.
CREATE TABLE IF NOT EXISTS users (
    id                     INTEGER PRIMARY KEY AUTOINCREMENT,
    osu_user_id            INTEGER UNIQUE NOT NULL,
    osu_username           TEXT NOT NULL,
    osu_avatar_url         TEXT,
    osu_cover_url          TEXT,
    osu_country_code       TEXT,
    osu_global_rank        INTEGER,
    style                  TEXT NOT NULL DEFAULT 'unknown',
    profile_public         INTEGER NOT NULL DEFAULT 1,
    created_at             TEXT NOT NULL,
    last_login_at          TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_users_osu_id ON users(osu_user_id);
CREATE INDEX IF NOT EXISTS idx_users_username ON users(osu_username);

-- API tokens for the uploader companion (Task #67). Each token belongs to
-- one user. Raw token value is shown once at creation and NEVER stored —
-- only a SHA256 hash. The prefix ("tt_uploader_XXXXXX") is kept in plain
-- text so users can identify tokens in the UI without seeing the secret.
CREATE TABLE IF NOT EXISTS api_tokens (
    id                     INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id                INTEGER NOT NULL,
    label                  TEXT NOT NULL,           -- user-supplied ("My laptop")
    prefix                 TEXT NOT NULL,           -- "tt_uploader_XXXXXX" — first 8 chars of raw, for UI
    token_hash             TEXT NOT NULL UNIQUE,    -- sha256 hex of the raw token
    created_at             TEXT NOT NULL,
    last_used_at           TEXT,                    -- NULL until first use
    revoked_at             TEXT                     -- NULL when active
);

CREATE INDEX IF NOT EXISTS idx_api_tokens_user ON api_tokens(user_id);
CREATE INDEX IF NOT EXISTS idx_api_tokens_hash ON api_tokens(token_hash);
"""


_PLAYS_SCHEMA = """
CREATE TABLE IF NOT EXISTS player_info (
    name                TEXT PRIMARY KEY,
    style               TEXT NOT NULL DEFAULT 'kddk',
    notes               TEXT,
    updated_at          TEXT NOT NULL,
    osu_user_id         INTEGER,
    osu_username        TEXT,
    osu_avatar_url      TEXT,
    osu_cover_url       TEXT,
    osu_country_code    TEXT,
    osu_global_rank     INTEGER,
    -- Links this per-player DB to a catalog.users row. NULL in local mode
    -- (no auth); populated in web mode after osu! OAuth login. Enables
    -- lookups like "find the DB for the currently logged-in user".
    user_id             INTEGER
);

CREATE TABLE IF NOT EXISTS replays (
    id                    INTEGER PRIMARY KEY AUTOINCREMENT,
    map_md5               TEXT NOT NULL,
    content               BLOB NOT NULL,
    played_at             TEXT NOT NULL,
    score                 INTEGER,
    accuracy_reported     REAL NOT NULL,
    accuracy_judged       REAL NOT NULL,
    count_great           INTEGER NOT NULL,
    count_ok              INTEGER NOT NULL,
    count_miss            INTEGER NOT NULL,
    delta_mean_ms         REAL,
    delta_stddev_ms       REAL,
    cheese_rate           REAL,
    fast_cheese_pairs     INTEGER,
    classification_json   TEXT,
    miss_patterns_json    TEXT,
    -- Mods this replay was actually played with. NM plays leave these null / 0 / 'NM'
    -- and effective ratings equal the base map's rating (via coalesce at query time).
    mods_bitfield         INTEGER DEFAULT 0,
    mods_label            TEXT DEFAULT 'NM',
    rating_speed_eff       REAL,
    rating_stamina_eff     REAL,
    rating_gimmick_eff     REAL,
    rating_technical_eff   REAL,
    rating_consistency_eff REAL,
    rating_reading_eff     REAL,
    inserted_at           TEXT NOT NULL,
    UNIQUE(map_md5, played_at)
);

CREATE INDEX IF NOT EXISTS idx_replays_map ON replays(map_md5);
CREATE INDEX IF NOT EXISTS idx_replays_played ON replays(played_at DESC);

CREATE TABLE IF NOT EXISTS snapshots (
    id                       INTEGER PRIMARY KEY AUTOINCREMENT,
    computed_at              TEXT NOT NULL,
    latest_replay_played_at  TEXT NOT NULL,   -- the played_at that triggered this snapshot
    replays_used             INTEGER NOT NULL,
    skill_speed              REAL NOT NULL,
    skill_stamina            REAL NOT NULL,
    skill_gimmick            REAL NOT NULL,
    skill_technical          REAL NOT NULL,
    skill_consistency        REAL NOT NULL,
    skill_reading            REAL
);

CREATE INDEX IF NOT EXISTS idx_snapshots_time ON snapshots(computed_at DESC);

CREATE TABLE IF NOT EXISTS map_roots (
    path                  TEXT PRIMARY KEY,
    added_at              TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS config (
    key                   TEXT PRIMARY KEY,
    value                 TEXT NOT NULL,
    updated_at            TEXT NOT NULL
);
"""


def _now() -> str:
    # Microsecond precision so rapid successive snapshots order correctly.
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


# -----------------------------------------------------------------------------
# Workspace + connection helpers
# -----------------------------------------------------------------------------

DEFAULT_WORKSPACE = "."
CATALOG_FILENAME = "catalog.db"


def catalog_path(workspace: str | Path = DEFAULT_WORKSPACE) -> Path:
    return Path(workspace) / CATALOG_FILENAME


def player_db_path(workspace: str | Path, player: str) -> Path:
    return Path(workspace) / f"{player}.db"


def open_catalog(workspace: str | Path = DEFAULT_WORKSPACE) -> sqlite3.Connection:
    """Open (and init) the catalog DB in a workspace."""
    p = catalog_path(workspace)
    p.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(p))
    conn.row_factory = sqlite3.Row
    conn.executescript(_CATALOG_SCHEMA)
    _migrate_catalog_schema(conn)
    conn.commit()
    return conn


def _migrate_catalog_schema(conn: sqlite3.Connection) -> None:
    """Add columns that were introduced after the initial catalog schema."""
    existing = {r["name"] for r in conn.execute("PRAGMA table_info(maps)")}
    if "rating_reading" not in existing:
        conn.execute("ALTER TABLE maps ADD COLUMN rating_reading REAL")
    # api_tokens table (Task #67) — CREATE IF NOT EXISTS in _CATALOG_SCHEMA
    # handles the fresh case; nothing to migrate for existing catalogs
    # since the whole table is new (older catalogs simply won't have it
    # until this runs). The CREATE from the schema block above already
    # ran by the time we're here, so no explicit ADD needed here.
    conn.commit()


def open_plays(workspace: str | Path, player: str) -> sqlite3.Connection:
    """Open (and init) a per-player DB in a workspace.

    The catalog DB is ATTACHed as `catalog`, so cross-DB joins work in one query.
    """
    ws = Path(workspace)
    ws.mkdir(parents=True, exist_ok=True)
    plays = player_db_path(ws, player)
    catalog = catalog_path(ws)

    # Init catalog first so ATTACH always succeeds.
    open_catalog(ws).close()

    conn = sqlite3.connect(str(plays))
    conn.row_factory = sqlite3.Row
    conn.executescript(_PLAYS_SCHEMA)
    _migrate_plays_schema(conn)
    conn.execute(f"ATTACH DATABASE '{catalog}' AS catalog")
    # Ensure a player_info row exists (default style: unknown until user sets it).
    exists = conn.execute("SELECT 1 FROM player_info WHERE name = ?", (player,)).fetchone()
    if not exists:
        conn.execute(
            "INSERT INTO player_info (name, style, notes, updated_at) VALUES (?, ?, ?, ?)",
            (player, "unknown", None, _now()),
        )
    conn.commit()
    return conn


def _migrate_plays_schema(conn: sqlite3.Connection) -> None:
    """Add columns that were introduced after the initial schema. SQLite has no
    'ADD COLUMN IF NOT EXISTS', so check pragma_table_info first."""
    existing = {r["name"] for r in conn.execute("PRAGMA table_info(player_info)")}
    for col, ddl in (
        ("osu_user_id",      "ALTER TABLE player_info ADD COLUMN osu_user_id INTEGER"),
        ("osu_username",     "ALTER TABLE player_info ADD COLUMN osu_username TEXT"),
        ("osu_avatar_url",   "ALTER TABLE player_info ADD COLUMN osu_avatar_url TEXT"),
        ("osu_cover_url",    "ALTER TABLE player_info ADD COLUMN osu_cover_url TEXT"),
        ("osu_country_code", "ALTER TABLE player_info ADD COLUMN osu_country_code TEXT"),
        ("osu_global_rank",  "ALTER TABLE player_info ADD COLUMN osu_global_rank INTEGER"),
        # Web-mode multi-tenancy: links this per-player DB to catalog.users.
        # NULL in local mode; populated on first login in web mode. Task #64.
        ("user_id",          "ALTER TABLE player_info ADD COLUMN user_id INTEGER"),
    ):
        if col not in existing:
            conn.execute(ddl)
    replays_existing = {r["name"] for r in conn.execute("PRAGMA table_info(replays)")}
    for col, ddl in (
        ("miss_patterns_json",     "ALTER TABLE replays ADD COLUMN miss_patterns_json TEXT"),
        ("mods_bitfield",          "ALTER TABLE replays ADD COLUMN mods_bitfield INTEGER DEFAULT 0"),
        ("mods_label",             "ALTER TABLE replays ADD COLUMN mods_label TEXT DEFAULT 'NM'"),
        ("rating_speed_eff",       "ALTER TABLE replays ADD COLUMN rating_speed_eff REAL"),
        ("rating_stamina_eff",     "ALTER TABLE replays ADD COLUMN rating_stamina_eff REAL"),
        ("rating_gimmick_eff",     "ALTER TABLE replays ADD COLUMN rating_gimmick_eff REAL"),
        ("rating_technical_eff",   "ALTER TABLE replays ADD COLUMN rating_technical_eff REAL"),
        ("rating_consistency_eff", "ALTER TABLE replays ADD COLUMN rating_consistency_eff REAL"),
        ("rating_reading_eff",     "ALTER TABLE replays ADD COLUMN rating_reading_eff REAL"),
    ):
        if col not in replays_existing:
            conn.execute(ddl)
    snap_existing = {r["name"] for r in conn.execute("PRAGMA table_info(snapshots)")}
    if "skill_reading" not in snap_existing:
        conn.execute("ALTER TABLE snapshots ADD COLUMN skill_reading REAL")
    conn.commit()


def discover_players(workspace: str | Path = DEFAULT_WORKSPACE) -> list[str]:
    """Return player names for every <name>.db in the workspace (excluding catalog)."""
    ws = Path(workspace)
    if not ws.exists():
        return []
    out = []
    for p in sorted(ws.glob("*.db")):
        if p.name == CATALOG_FILENAME:
            continue
        # Sanity: does it have a player_info row?
        try:
            conn = sqlite3.connect(str(p))
            row = conn.execute("SELECT name FROM player_info LIMIT 1").fetchone()
            conn.close()
            if row:
                out.append(row[0])
        except sqlite3.Error:
            continue
    return out


# -----------------------------------------------------------------------------
# Catalog: maps
# -----------------------------------------------------------------------------

def upsert_map(
    conn: sqlite3.Connection,
    beatmap: TaikoBeatmap,
    features: MapFeatures,
    rating: DimensionRating,
    content: bytes,
) -> None:
    """Insert (or replace) a map row with its raw content + cached rating."""
    r = rating.as_dict()
    # `conn` may be a plays-DB connection with catalog ATTACHed, in which case
    # we route the write to catalog.maps. If it's a raw catalog connection,
    # main.maps is the same table.
    target_schema = "catalog" if _has_attached_catalog(conn) else "main"
    conn.execute(
        f"""
        INSERT OR REPLACE INTO {target_schema}.maps (
            md5, content,
            artist, title, version, creator, beatmap_id, beatmapset_id,
            mode, duration_s, hittable_notes, bpm_min, bpm_max, od,
            rating_speed, rating_stamina, rating_gimmick, rating_technical, rating_consistency,
            rating_reading,
            parity_mean, parity_hostile_ratio,
            inserted_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            beatmap.beatmap_md5, content,
            beatmap.meta.artist, beatmap.meta.title, beatmap.meta.version, beatmap.meta.creator,
            beatmap.meta.beatmap_id, beatmap.meta.beatmapset_id,
            beatmap.mode,
            features.density.duration_s,
            features.hittable_notes,
            features.movement.bpm_min, features.movement.bpm_max,
            beatmap.difficulty.overall_difficulty,
            r["speed"], r["stamina"], r["gimmick"], r["technical"], r["consistency"],
            r.get("reading", 0.0),
            features.parity.mean, features.parity.hostile_ratio,
            _now(),
        ),
    )
    conn.commit()


def get_catalog_meta(conn: sqlite3.Connection, key: str) -> str | None:
    """Read a key from catalog_meta (workspace-level config)."""
    schema = "catalog" if _has_attached_catalog(conn) else "main"
    row = conn.execute(
        f"SELECT value FROM {schema}.catalog_meta WHERE key = ?", (key,)
    ).fetchone()
    return row["value"] if row else None


def set_catalog_meta(conn: sqlite3.Connection, key: str, value: str) -> None:
    schema = "catalog" if _has_attached_catalog(conn) else "main"
    conn.execute(
        f"INSERT INTO {schema}.catalog_meta (key, value, updated_at) VALUES (?, ?, ?) "
        f"ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at",
        (key, value, _now()),
    )
    conn.commit()


def _has_attached_catalog(conn: sqlite3.Connection) -> bool:
    for row in conn.execute("PRAGMA database_list"):
        if row["name"] == "catalog":
            return True
    return False


def get_all_maps(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    schema = "catalog" if _has_attached_catalog(conn) else "main"
    # Exclude the content blob from listings — callers explicitly request it.
    rows = conn.execute(
        f"""SELECT md5, artist, title, version, creator, beatmap_id, beatmapset_id,
                   mode, duration_s, hittable_notes, bpm_min, bpm_max, od,
                   rating_speed, rating_stamina, rating_gimmick, rating_technical, rating_consistency,
                   COALESCE(rating_reading, 0) AS rating_reading,
                   parity_mean, parity_hostile_ratio, inserted_at
            FROM {schema}.maps"""
    ).fetchall()
    return [dict(r) for r in rows]


def get_map(conn: sqlite3.Connection, md5: str) -> dict[str, Any] | None:
    schema = "catalog" if _has_attached_catalog(conn) else "main"
    row = conn.execute(
        f"""SELECT md5, artist, title, version, creator, beatmap_id, beatmapset_id,
                   mode, duration_s, hittable_notes, bpm_min, bpm_max, od,
                   rating_speed, rating_stamina, rating_gimmick, rating_technical, rating_consistency,
                   COALESCE(rating_reading, 0) AS rating_reading,
                   parity_mean, parity_hostile_ratio, inserted_at
            FROM {schema}.maps WHERE md5 = ?""",
        (md5,),
    ).fetchone()
    return dict(row) if row else None


def get_map_content(conn: sqlite3.Connection, md5: str) -> bytes | None:
    schema = "catalog" if _has_attached_catalog(conn) else "main"
    row = conn.execute(
        f"SELECT content FROM {schema}.maps WHERE md5 = ?", (md5,)
    ).fetchone()
    return bytes(row["content"]) if row else None


# -----------------------------------------------------------------------------
# Player info + config + map roots (in plays.db)
# -----------------------------------------------------------------------------

def upsert_player(
    conn: sqlite3.Connection,
    name: str,
    style: str = "kddk",
    notes: str | None = None,
) -> None:
    valid = {"kddk", "ddkk", "kkdd", "unknown"}
    style_lc = style.lower()
    if style_lc not in valid:
        raise ValueError(f"style must be one of {sorted(valid)}, got {style!r}")
    conn.execute(
        """
        INSERT INTO player_info (name, style, notes, updated_at) VALUES (?, ?, ?, ?)
        ON CONFLICT(name) DO UPDATE SET
            style = excluded.style,
            notes = COALESCE(excluded.notes, player_info.notes),
            updated_at = excluded.updated_at
        """,
        (name, style_lc, notes, _now()),
    )
    conn.commit()


def get_player(conn: sqlite3.Connection, name: str) -> dict[str, Any] | None:
    row = conn.execute("SELECT * FROM player_info WHERE name = ?", (name,)).fetchone()
    return dict(row) if row else None


def set_osu_profile(
    conn: sqlite3.Connection,
    name: str,
    user_id: int | None,
    username: str | None,
    avatar_url: str | None,
    country_code: str | None,
    global_rank: int | None,
    cover_url: str | None = None,
) -> None:
    """Link an osu! profile (from osu_api.lookup_user) to a player."""
    conn.execute(
        """
        UPDATE player_info SET
            osu_user_id = ?,
            osu_username = ?,
            osu_avatar_url = ?,
            osu_cover_url = ?,
            osu_country_code = ?,
            osu_global_rank = ?,
            updated_at = ?
        WHERE name = ?
        """,
        (user_id, username, avatar_url, cover_url, country_code, global_rank, _now(), name),
    )
    conn.commit()


# -----------------------------------------------------------------------------
# Users (web mode) — one row per authenticated osu! account.
# -----------------------------------------------------------------------------

def upsert_user_from_osu(
    conn: sqlite3.Connection,
    osu_user_id: int,
    osu_username: str,
    osu_avatar_url: str = "",
    osu_cover_url: str = "",
    osu_country_code: str = "",
    osu_global_rank: int | None = None,
) -> int:
    """Insert or refresh the users row for this osu! account. Returns the
    users.id (local primary key), which is what session cookies carry.

    Refreshes username/avatar/cover on every login — osu! profile can change,
    and stale display data is a bad user experience."""
    schema = "catalog" if _has_attached_catalog(conn) else "main"
    now = _now()
    conn.execute(
        f"""
        INSERT INTO {schema}.users (
            osu_user_id, osu_username, osu_avatar_url, osu_cover_url,
            osu_country_code, osu_global_rank,
            created_at, last_login_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(osu_user_id) DO UPDATE SET
            osu_username     = excluded.osu_username,
            osu_avatar_url   = excluded.osu_avatar_url,
            osu_cover_url    = excluded.osu_cover_url,
            osu_country_code = excluded.osu_country_code,
            osu_global_rank  = excluded.osu_global_rank,
            last_login_at    = excluded.last_login_at
        """,
        (
            osu_user_id, osu_username, osu_avatar_url, osu_cover_url,
            osu_country_code, osu_global_rank,
            now, now,
        ),
    )
    row = conn.execute(
        f"SELECT id FROM {schema}.users WHERE osu_user_id = ?", (osu_user_id,)
    ).fetchone()
    conn.commit()
    return int(row["id"])


def get_user_by_id(conn: sqlite3.Connection, user_id: int) -> dict[str, Any] | None:
    schema = "catalog" if _has_attached_catalog(conn) else "main"
    row = conn.execute(
        f"SELECT * FROM {schema}.users WHERE id = ?", (user_id,)
    ).fetchone()
    return dict(row) if row else None


def get_user_by_username(conn: sqlite3.Connection, username: str) -> dict[str, Any] | None:
    """Case-insensitive lookup by osu_username. Used for /u/{username} routes."""
    schema = "catalog" if _has_attached_catalog(conn) else "main"
    row = conn.execute(
        f"SELECT * FROM {schema}.users WHERE LOWER(osu_username) = LOWER(?)",
        (username,),
    ).fetchone()
    return dict(row) if row else None


def set_user_profile_public(
    conn: sqlite3.Connection, user_id: int, public: bool
) -> None:
    """Toggle the user's profile_public flag. When False, /u/{osu_username}
    404s for anyone except the owner. Owner self-view always works."""
    schema = "catalog" if _has_attached_catalog(conn) else "main"
    conn.execute(
        f"UPDATE {schema}.users SET profile_public = ? WHERE id = ?",
        (1 if public else 0, int(user_id)),
    )
    conn.commit()


# -----------------------------------------------------------------------------
# Leaderboards + map database queries (web mode)
#
# The current architecture keeps replays + snapshots in per-user .db files.
# Global aggregations (top-N users by skill dim, top-N plays on a map) need
# to iterate every file. That's O(N users) sqlite opens per page load — fine
# at hundreds of users, would need materialization at thousands.
#
# Small in-process cache keeps sequential clicks fast without a real cache
# layer. TTL 60s so a new upload appears within a minute.
# -----------------------------------------------------------------------------

import time as _time

_LEADERBOARD_CACHE: dict[tuple, tuple[float, Any]] = {}
_CACHE_TTL_S = 60.0


def _cached(key: tuple, fn):
    """Simple TTL cache. Key must be hashable; fn is a zero-arg callable that
    computes the value on miss. Not thread-safe, but the FastAPI event loop
    serializes per request anyway."""
    now = _time.time()
    hit = _LEADERBOARD_CACHE.get(key)
    if hit and (now - hit[0]) < _CACHE_TTL_S:
        return hit[1]
    val = fn()
    _LEADERBOARD_CACHE[key] = (now, val)
    return val


def top_users_by_skill(
    workspace: str | Path,
    dim: str,
    limit: int = 50,
) -> list[dict[str, Any]]:
    """Rank public-profile users by their latest snapshot's skill_{dim}.
    Returns list of dicts with osu_username, avatar, dim value, plus
    the six dim numbers for a quick sub-row display.

    Iterates each user's DB — O(users). Cached for TTL so rapid clicks
    across dim tabs share results."""
    _DIMS = ("speed", "stamina", "gimmick", "technical", "consistency", "reading")
    if dim not in _DIMS:
        raise ValueError(f"unknown dim {dim!r}")

    def compute():
        ws = Path(workspace)
        cat = open_catalog(ws)
        users = cat.execute(
            """
            SELECT id, osu_username, osu_avatar_url, osu_country_code, osu_global_rank
            FROM users WHERE profile_public = 1
            """
        ).fetchall()
        cat.close()

        rows: list[dict[str, Any]] = []
        for u in users:
            player_name = find_player_name_for_user(ws, int(u["id"]))
            if not player_name:
                continue
            p = player_db_path(ws, player_name)
            if not p.exists():
                continue
            try:
                pconn = sqlite3.connect(str(p))
                pconn.row_factory = sqlite3.Row
                snap = pconn.execute(
                    """
                    SELECT skill_speed, skill_stamina, skill_gimmick,
                           skill_technical, skill_consistency, skill_reading,
                           replays_used, latest_replay_played_at
                    FROM snapshots ORDER BY id DESC LIMIT 1
                    """
                ).fetchone()
                pconn.close()
            except sqlite3.OperationalError:
                continue
            if not snap:
                continue
            row = {
                "osu_username": u["osu_username"],
                "osu_avatar_url": u["osu_avatar_url"],
                "osu_country_code": u["osu_country_code"],
                "osu_global_rank": u["osu_global_rank"],
                "replays": int(snap["replays_used"]),
                "latest_played_at": snap["latest_replay_played_at"],
            }
            for d in _DIMS:
                row[d] = float(snap[f"skill_{d}"] or 0)
            rows.append(row)

        rows.sort(key=lambda r: -r[dim])
        return rows[:limit]

    return _cached(("top_users", str(workspace), dim, limit), compute)


def top_plays_for_map(
    workspace: str | Path,
    map_md5: str,
    limit: int = 30,
) -> list[dict[str, Any]]:
    """Best plays on a given map, ordered by accuracy_judged. Includes the
    player's osu_username + avatar so the list is renderable without
    additional lookups. Mods stay per-play (not grouped) — the caller can
    tab by mods_label if desired.

    Iterates each user's replays — O(users). Cached."""
    def compute():
        ws = Path(workspace)
        cat = open_catalog(ws)
        users = cat.execute(
            """
            SELECT id, osu_username, osu_avatar_url
            FROM users WHERE profile_public = 1
            """
        ).fetchall()
        cat.close()

        plays: list[dict[str, Any]] = []
        for u in users:
            player_name = find_player_name_for_user(ws, int(u["id"]))
            if not player_name:
                continue
            p = player_db_path(ws, player_name)
            if not p.exists():
                continue
            try:
                pconn = sqlite3.connect(str(p))
                pconn.row_factory = sqlite3.Row
                rows = pconn.execute(
                    """
                    SELECT id, played_at, accuracy_judged, count_great,
                           count_ok, count_miss, mods_label
                    FROM replays WHERE map_md5 = ?
                    ORDER BY accuracy_judged DESC LIMIT 5
                    """,
                    (map_md5,),
                ).fetchall()
                pconn.close()
            except sqlite3.OperationalError:
                continue
            for r in rows:
                plays.append({
                    "replay_id": int(r["id"]),
                    "player_name": player_name,
                    "osu_username": u["osu_username"],
                    "osu_avatar_url": u["osu_avatar_url"],
                    "accuracy": float(r["accuracy_judged"]),
                    "great": int(r["count_great"]),
                    "ok": int(r["count_ok"]),
                    "miss": int(r["count_miss"]),
                    "mods_label": r["mods_label"] or "NM",
                    "played_at": r["played_at"],
                })

        plays.sort(key=lambda p: -p["accuracy"])
        return plays[:limit]

    return _cached(("top_plays_map", str(workspace), map_md5, limit), compute)


def browse_maps(
    conn: sqlite3.Connection,
    dim_sort: str = "rating_speed",
    min_rating: float = 0.0,
    search: str = "",
    limit: int = 100,
    offset: int = 0,
) -> tuple[list[dict[str, Any]], int]:
    """Paginated map catalog browse. Returns (rows, total_count). Only
    counts pass rating filter + search substring on title/version/creator.

    dim_sort must be one of the rating columns; other input goes through
    parameterized queries so it's safe from injection."""
    _ALLOWED_SORT = {
        "rating_speed", "rating_stamina", "rating_gimmick",
        "rating_technical", "rating_consistency", "rating_reading",
        "bpm_max", "hittable_notes", "duration_s", "inserted_at",
    }
    if dim_sort not in _ALLOWED_SORT:
        dim_sort = "rating_speed"

    schema = "catalog" if _has_attached_catalog(conn) else "main"
    like = f"%{search.strip()}%" if search else "%"

    total = conn.execute(
        f"""
        SELECT COUNT(*) FROM {schema}.maps
        WHERE (title LIKE ? OR version LIKE ? OR creator LIKE ?)
          AND COALESCE({dim_sort}, 0) >= ?
        """,
        (like, like, like, float(min_rating)),
    ).fetchone()[0]

    rows = conn.execute(
        f"""
        SELECT md5, artist, title, version, creator, beatmap_id, beatmapset_id,
               duration_s, hittable_notes, bpm_min, bpm_max, od,
               rating_speed, rating_stamina, rating_gimmick,
               rating_technical, rating_consistency,
               COALESCE(rating_reading, 0) AS rating_reading
        FROM {schema}.maps
        WHERE (title LIKE ? OR version LIKE ? OR creator LIKE ?)
          AND COALESCE({dim_sort}, 0) >= ?
        ORDER BY {dim_sort} DESC
        LIMIT ? OFFSET ?
        """,
        (like, like, like, float(min_rating), int(limit), int(offset)),
    ).fetchall()
    return [dict(r) for r in rows], int(total)


def delete_user_completely(
    workspace: str | Path, user_id: int
) -> dict[str, Any]:
    """Irreversible account delete. Wipes:
    - users row (removes login identity)
    - api_tokens rows (revokes anything they'd shipped to a machine)
    - the linked per-player .db file (all their replay data)

    Returns a summary of what got removed so the caller can log or
    confirm to the user. If the user isn't found, returns zeros.

    Deliberately opens fresh connections rather than taking one from the
    caller — the multi-file delete has to be atomic-ish, and doing it
    inline with an existing catalog connection risks leaving orphaned
    state on partial failure."""
    ws = Path(workspace)
    summary: dict[str, Any] = {
        "user_deleted": False,
        "tokens_revoked": 0,
        "player_db_deleted": None,
        "replays_lost": 0,
    }

    # Find + delete the per-player DB first — while we still have the
    # linkage. If this fails we haven't touched the catalog yet.
    player_name = find_player_name_for_user(ws, int(user_id))
    if player_name:
        p = player_db_path(ws, player_name)
        # Best-effort record what we're wiping for the summary.
        if p.exists():
            try:
                probe = sqlite3.connect(str(p))
                summary["replays_lost"] = int(
                    probe.execute("SELECT COUNT(*) FROM replays").fetchone()[0]
                )
                probe.close()
            except sqlite3.OperationalError:
                pass
            try:
                p.unlink()
                summary["player_db_deleted"] = player_name
            except OSError:
                pass

    # Now catalog cleanup: revoke all tokens + delete users row.
    cat = open_catalog(ws)
    tok_cur = cat.execute(
        "DELETE FROM users WHERE id = ?", (int(user_id),)
    )
    if (tok_cur.rowcount or 0) > 0:
        summary["user_deleted"] = True
    # Cascade tokens by hand — no FK enforcement in SQLite by default.
    tk_cur = cat.execute(
        "DELETE FROM api_tokens WHERE user_id = ?", (int(user_id),)
    )
    summary["tokens_revoked"] = int(tk_cur.rowcount or 0)
    cat.commit()
    cat.close()
    return summary


# -----------------------------------------------------------------------------
# API tokens (uploader companion, Task #67)
# -----------------------------------------------------------------------------

_TOKEN_PREFIX = "tt_uploader_"


def _hash_token(raw: str) -> str:
    """SHA256 of the raw token, hex-encoded. Constant-length so
    HMAC-style constant-time compare works cleanly, and one-way so a
    DB dump doesn't reveal usable tokens."""
    import hashlib
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def create_api_token(
    conn: sqlite3.Connection,
    user_id: int,
    label: str,
) -> str:
    """Generate a new API token for `user_id`, store its hash, and return
    the RAW token string. This is the only moment the raw token exists —
    the caller must display it to the user immediately. It cannot be
    recovered later (only hash is stored).

    Format: `tt_uploader_<43-char-urlsafe-base64>`. The prefix makes
    tokens visually identifiable in logs, config files, and secret-scanning
    tools. 32 bytes of entropy is well beyond what's needed for auth."""
    import secrets
    schema = "catalog" if _has_attached_catalog(conn) else "main"
    raw = _TOKEN_PREFIX + secrets.token_urlsafe(32)
    token_hash = _hash_token(raw)
    # First 20 chars of the raw token are shown in the UI ("tt_uploader_XXXX...")
    # so the user can distinguish tokens without exposing the whole secret.
    display_prefix = raw[:20]
    conn.execute(
        f"""
        INSERT INTO {schema}.api_tokens
            (user_id, label, prefix, token_hash, created_at)
        VALUES (?, ?, ?, ?, ?)
        """,
        (int(user_id), label.strip() or "unnamed", display_prefix, token_hash, _now()),
    )
    conn.commit()
    return raw


def verify_api_token(conn: sqlite3.Connection, raw: str) -> int | None:
    """Look up a raw token → user_id, updating last_used_at on success.
    Returns None on any failure (unknown, revoked, or malformed).

    Uses constant-time comparison on the hash lookup so timing side
    channels can't leak partial hash matches."""
    if not raw or not raw.startswith(_TOKEN_PREFIX):
        return None
    schema = "catalog" if _has_attached_catalog(conn) else "main"
    token_hash = _hash_token(raw)
    row = conn.execute(
        f"""
        SELECT id, user_id, revoked_at FROM {schema}.api_tokens
        WHERE token_hash = ?
        """,
        (token_hash,),
    ).fetchone()
    if not row or row["revoked_at"] is not None:
        return None
    # Best-effort update; not critical if this fails (e.g. read-only replica).
    try:
        conn.execute(
            f"UPDATE {schema}.api_tokens SET last_used_at = ? WHERE id = ?",
            (_now(), int(row["id"])),
        )
        conn.commit()
    except sqlite3.OperationalError:
        pass
    return int(row["user_id"])


def list_api_tokens(conn: sqlite3.Connection, user_id: int) -> list[dict[str, Any]]:
    """All tokens belonging to `user_id`, active + revoked, newest first.
    Never includes token_hash — display-only fields."""
    schema = "catalog" if _has_attached_catalog(conn) else "main"
    rows = conn.execute(
        f"""
        SELECT id, label, prefix, created_at, last_used_at, revoked_at
        FROM {schema}.api_tokens
        WHERE user_id = ?
        ORDER BY (revoked_at IS NULL) DESC, created_at DESC
        """,
        (int(user_id),),
    ).fetchall()
    return [dict(r) for r in rows]


def revoke_api_token(
    conn: sqlite3.Connection, user_id: int, token_id: int
) -> bool:
    """Mark a token as revoked (still visible in the settings UI, but
    verify_api_token rejects it). Ownership-scoped so User A can't revoke
    User B's tokens. Returns True if a row was actually revoked."""
    schema = "catalog" if _has_attached_catalog(conn) else "main"
    cur = conn.execute(
        f"""
        UPDATE {schema}.api_tokens
        SET revoked_at = ?
        WHERE id = ? AND user_id = ? AND revoked_at IS NULL
        """,
        (_now(), int(token_id), int(user_id)),
    )
    conn.commit()
    return (cur.rowcount or 0) > 0


# -----------------------------------------------------------------------------
# Player linkage (workspace)
# -----------------------------------------------------------------------------

def link_player_to_user(conn: sqlite3.Connection, player_name: str, user_id: int) -> None:
    """Set the user_id on a per-player DB's player_info row. Called when a
    user logs in via OAuth and we resolve which per-player DB is theirs."""
    conn.execute(
        "UPDATE player_info SET user_id = ?, updated_at = ? WHERE name = ?",
        (int(user_id), _now(), player_name),
    )
    conn.commit()


def _replay_count(workspace: str | Path, player_name: str) -> int:
    """Count replay rows in a per-player .db without triggering the full
    open_plays init/attach. Used to detect empty stub DBs during first-login
    repair. Returns 0 if the file doesn't exist or has no replays table."""
    p = player_db_path(workspace, player_name)
    if not p.exists():
        return 0
    try:
        conn = sqlite3.connect(str(p))
        row = conn.execute("SELECT COUNT(*) FROM replays").fetchone()
        conn.close()
        return int(row[0]) if row else 0
    except sqlite3.OperationalError:
        return 0


def find_player_name_for_user(
    workspace: str | Path, user_id: int
) -> str | None:
    """Given a user_id (from catalog.users), find which per-player DB in the
    workspace belongs to them. Returns the player name (filename stem) or
    None if not linked yet. Scans each *.db's player_info.user_id."""
    ws = Path(workspace)
    if not ws.exists():
        return None
    for p in sorted(ws.glob("*.db")):
        if p.name == CATALOG_FILENAME:
            continue
        try:
            conn = sqlite3.connect(str(p))
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                "SELECT name FROM player_info WHERE user_id = ? LIMIT 1", (int(user_id),)
            ).fetchone()
            conn.close()
            if row:
                return str(row["name"])
        except sqlite3.OperationalError:
            # Old DB that hasn't been migrated yet — user_id column missing.
            # Safe to skip; a subsequent open_plays() call will migrate it.
            continue
    return None


def ensure_player_db_for_user(
    workspace: str | Path,
    user: dict[str, Any],
) -> str:
    """Web-mode helper: ensure a per-player DB exists for this user and is
    linked back to their users row. Returns the player name (which is the
    filename stem and the URL segment for /u/{name}).

    Idempotent — calling twice for the same user is safe. On second call,
    finds the existing DB and just refreshes the osu! profile fields.

    Naming convention: `<osu_username>.db`, so /u/{osu_username} lookups
    are direct file-system lookups. If the user changes their osu!
    username later, we do NOT rename the file — the old name stays as the
    stable identifier for their history; the profile display fields track
    the current name."""
    existing = find_player_name_for_user(workspace, int(user["id"]))

    # Repair a specific bug shape: on very early web-mode logins the code
    # created `<name>-<id>.db` as a fresh empty file instead of adopting
    # the pre-existing `<name>.db` from local mode. If we detect that
    # pattern (linked file is empty AND an unlinked matching-name file
    # exists), unlink the empty one and let the adoption path pick up the
    # real data.
    if existing is not None:
        empty_linked = _replay_count(workspace, existing) == 0
        target_name = user["osu_username"]
        target_path = player_db_path(workspace, target_name)
        if empty_linked and existing != target_name and target_path.exists():
            unlinked = sqlite3.connect(str(target_path))
            unlinked.row_factory = sqlite3.Row
            try:
                row = unlinked.execute(
                    "SELECT user_id FROM player_info WHERE name = ? LIMIT 1", (target_name,)
                ).fetchone()
                target_unlinked = row is not None and row["user_id"] is None
                if target_unlinked and _replay_count(workspace, target_name) > 0:
                    # Clear the stale linkage on the empty file so it stops
                    # winning the lookup, then fall through to the adoption
                    # branch below. The empty .db file is left in place; user
                    # can delete it themselves once they've confirmed the
                    # correct data is showing.
                    stale = open_plays(workspace, existing)
                    stale.execute(
                        "UPDATE player_info SET user_id = NULL WHERE name = ?",
                        (existing,),
                    )
                    stale.commit()
                    stale.close()
                    existing = None  # re-enter the create/adopt branch below
            finally:
                unlinked.close()

    if existing is not None:
        # Refresh osu! display fields on the linked player_info row so
        # avatar / cover / rank stay current.
        conn = open_plays(workspace, existing)
        conn.execute(
            """
            UPDATE player_info SET
                osu_user_id      = ?,
                osu_username     = ?,
                osu_avatar_url   = ?,
                osu_cover_url    = ?,
                osu_country_code = ?,
                osu_global_rank  = ?,
                updated_at       = ?
            WHERE name = ?
            """,
            (
                user["osu_user_id"], user["osu_username"],
                user["osu_avatar_url"], user["osu_cover_url"],
                user["osu_country_code"], user["osu_global_rank"],
                _now(), existing,
            ),
        )
        conn.commit()
        conn.close()
        return existing

    # Create a new per-player DB, keyed on the osu_username.
    #
    # If workspace/<osu_username>.db already exists, distinguish two cases:
    #   1. It's UNLINKED (user_id NULL) — this is the common case for
    #      someone who used the local single-user app before web mode
    #      landed. Adopt it: their existing replay data becomes THIS
    #      user's data. Same person, so this is what they want.
    #   2. It's linked to a DIFFERENT user — collision (rare, e.g. two
    #      people with osu! usernames that share a local-mode name).
    #      Suffix with users.id to disambiguate.
    name = user["osu_username"]
    p = player_db_path(workspace, name)
    if p.exists():
        try:
            probe = sqlite3.connect(str(p))
            probe.row_factory = sqlite3.Row
            row = probe.execute(
                "SELECT user_id FROM player_info WHERE name = ? LIMIT 1", (name,)
            ).fetchone()
            probe.close()
        except sqlite3.OperationalError:
            # Pre-migration DB (no user_id column). Adopting is safe —
            # open_plays below will run the migration and add the column.
            row = None
        already_linked = row is not None and row["user_id"] is not None
        if already_linked and int(row["user_id"]) != int(user["id"]):
            # True collision: this file belongs to a different account. Suffix.
            name = f"{name}-{user['id']}"

    conn = open_plays(workspace, name)  # creates the file + player_info row
                                        # (or opens the existing one for adoption)
    conn.execute(
        """
        UPDATE player_info SET
            style            = COALESCE(style, ?),
            user_id          = ?,
            osu_user_id      = ?,
            osu_username     = ?,
            osu_avatar_url   = ?,
            osu_cover_url    = ?,
            osu_country_code = ?,
            osu_global_rank  = ?,
            updated_at       = ?
        WHERE name = ?
        """,
        (
            "unknown", int(user["id"]),
            user["osu_user_id"], user["osu_username"],
            user["osu_avatar_url"], user["osu_cover_url"],
            user["osu_country_code"], user["osu_global_rank"],
            _now(), name,
        ),
    )
    conn.commit()
    conn.close()
    return name


def add_map_root(conn: sqlite3.Connection, path: str) -> None:
    conn.execute(
        "INSERT OR IGNORE INTO map_roots (path, added_at) VALUES (?, ?)",
        (path, _now()),
    )
    conn.commit()


def remove_map_root(conn: sqlite3.Connection, path: str) -> bool:
    cursor = conn.execute("DELETE FROM map_roots WHERE path = ?", (path,))
    conn.commit()
    return (cursor.rowcount or 0) > 0


def list_map_roots(conn: sqlite3.Connection) -> list[str]:
    return [row["path"] for row in conn.execute("SELECT path FROM map_roots ORDER BY added_at")]


def set_config(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute(
        """
        INSERT INTO config (key, value, updated_at) VALUES (?, ?, ?)
        ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at
        """,
        (key, value, _now()),
    )
    conn.commit()


def get_config(conn: sqlite3.Connection, key: str, default: str | None = None) -> str | None:
    row = conn.execute("SELECT value FROM config WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else default


# -----------------------------------------------------------------------------
# Replays
# -----------------------------------------------------------------------------

def update_replay_judgment(
    conn: sqlite3.Connection,
    replay_id: int,
    judged: JudgedReplay,
    classification: FailureSummary | None,
    cheese: CheeseReport | None,
    miss_patterns: list[dict] | None = None,
    mods_bitfield: int = 0,
    mods_label: str = "NM",
    effective_rating: DimensionRating | None = None,
) -> None:
    """Overwrite the judged fields on an existing replay row (rejudge).

    `mods_*` capture what the replay was played with; `effective_rating` is
    the mod-adjusted DimensionRating (differs from the base map rating for
    DT/HR/etc). Passing NM + None leaves eff-rating columns null so
    downstream COALESCE queries fall back to the base map rating."""
    deltas = [j.hit_delta_ms for j in judged.judgments if j.hit_delta_ms is not None]
    if deltas:
        mean = sum(deltas) / len(deltas)
        var = sum((d - mean) ** 2 for d in deltas) / len(deltas)
        stddev = var ** 0.5
    else:
        mean = stddev = None
    classification_json = json.dumps(classification.by_cause) if classification else None
    miss_patterns_json = json.dumps(miss_patterns) if miss_patterns else None
    cheese_rate = cheese.cheese_rate if cheese else None
    fast_cheese = cheese.fast_cheese_pairs if cheese else None
    er = effective_rating
    conn.execute(
        """
        UPDATE replays SET
            accuracy_judged = ?,
            count_great = ?, count_ok = ?, count_miss = ?,
            delta_mean_ms = ?, delta_stddev_ms = ?,
            cheese_rate = ?, fast_cheese_pairs = ?,
            classification_json = ?,
            miss_patterns_json = ?,
            mods_bitfield = ?, mods_label = ?,
            rating_speed_eff = ?, rating_stamina_eff = ?,
            rating_gimmick_eff = ?, rating_technical_eff = ?,
            rating_consistency_eff = ?, rating_reading_eff = ?
        WHERE id = ?
        """,
        (
            judged.accuracy,
            judged.count_great, judged.count_ok, judged.count_miss,
            mean, stddev,
            cheese_rate, fast_cheese,
            classification_json,
            miss_patterns_json,
            int(mods_bitfield), mods_label,
            er.speed if er else None,
            er.stamina if er else None,
            er.gimmick if er else None,
            er.technical if er else None,
            er.consistency if er else None,
            er.reading if er else None,
            replay_id,
        ),
    )
    conn.commit()


def insert_replay(
    conn: sqlite3.Connection,
    replay: TaikoReplay,
    judged: JudgedReplay,
    map_md5: str,
    replay_content: bytes,
    classification: FailureSummary | None = None,
    cheese: CheeseReport | None = None,
    deltas: list[int] | None = None,
    miss_patterns: list[dict] | None = None,
    mods_bitfield: int = 0,
    mods_label: str = "NM",
    effective_rating: DimensionRating | None = None,
) -> int:
    if deltas is None:
        deltas = [j.hit_delta_ms for j in judged.judgments if j.hit_delta_ms is not None]
    if deltas:
        mean = sum(deltas) / len(deltas)
        var = sum((d - mean) ** 2 for d in deltas) / len(deltas)
        stddev = var ** 0.5
    else:
        mean = stddev = None

    total_r = replay.meta.count_300 + replay.meta.count_100 + replay.meta.count_miss
    acc_r = (replay.meta.count_300 + 0.5 * replay.meta.count_100) / total_r if total_r else 0.0

    classification_json = json.dumps(classification.by_cause) if classification else None
    miss_patterns_json = json.dumps(miss_patterns) if miss_patterns else None
    cheese_rate = cheese.cheese_rate if cheese else None
    fast_cheese = cheese.fast_cheese_pairs if cheese else None
    er = effective_rating

    cursor = conn.execute(
        """
        INSERT OR IGNORE INTO replays (
            map_md5, content, played_at, score,
            accuracy_reported, accuracy_judged,
            count_great, count_ok, count_miss,
            delta_mean_ms, delta_stddev_ms,
            cheese_rate, fast_cheese_pairs,
            classification_json,
            miss_patterns_json,
            mods_bitfield, mods_label,
            rating_speed_eff, rating_stamina_eff,
            rating_gimmick_eff, rating_technical_eff, rating_consistency_eff,
            rating_reading_eff,
            inserted_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            map_md5, replay_content,
            replay.meta.timestamp.isoformat(),
            replay.meta.score,
            acc_r, judged.accuracy,
            judged.count_great, judged.count_ok, judged.count_miss,
            mean, stddev,
            cheese_rate, fast_cheese,
            classification_json,
            miss_patterns_json,
            int(mods_bitfield), mods_label,
            er.speed if er else None,
            er.stamina if er else None,
            er.gimmick if er else None,
            er.technical if er else None,
            er.consistency if er else None,
            er.reading if er else None,
            _now(),
        ),
    )
    conn.commit()
    return int(cursor.lastrowid or 0)


def get_replays(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    """Return all replays in the connected plays DB, joined with catalog map info."""
    if not _has_attached_catalog(conn):
        rows = conn.execute(
            "SELECT * FROM replays ORDER BY played_at DESC"
        ).fetchall()
        return [dict(r) for r in rows]
    rows = conn.execute(
        """
        SELECT r.id, r.map_md5, r.played_at, r.score,
               r.accuracy_reported, r.accuracy_judged,
               r.count_great, r.count_ok, r.count_miss,
               r.delta_mean_ms, r.delta_stddev_ms,
               r.cheese_rate, r.fast_cheese_pairs,
               r.classification_json, r.miss_patterns_json, r.inserted_at,
               r.mods_bitfield, r.mods_label,
               m.title AS map_title, m.version AS map_version, m.creator AS map_creator,
               m.beatmap_id, m.beatmapset_id,
               -- Base map ratings (NM), preserved for display of the underlying map difficulty.
               m.rating_speed AS rating_speed_base,
               m.rating_stamina AS rating_stamina_base,
               m.rating_gimmick AS rating_gimmick_base,
               m.rating_technical AS rating_technical_base,
               m.rating_consistency AS rating_consistency_base,
               COALESCE(m.rating_reading, 0)  AS rating_reading_base,
               -- Effective ratings: mod-adjusted for DT/HR/etc, fall back to base for NM
               -- (or old records without eff columns populated). Everything downstream
               -- that used to read rating_* keeps working transparently.
               COALESCE(r.rating_speed_eff,       m.rating_speed)       AS rating_speed,
               COALESCE(r.rating_stamina_eff,     m.rating_stamina)     AS rating_stamina,
               COALESCE(r.rating_gimmick_eff,     m.rating_gimmick)     AS rating_gimmick,
               COALESCE(r.rating_technical_eff,   m.rating_technical)   AS rating_technical,
               COALESCE(r.rating_consistency_eff, m.rating_consistency) AS rating_consistency,
               COALESCE(r.rating_reading_eff,     m.rating_reading, 0)  AS rating_reading
        FROM replays r
        LEFT JOIN catalog.maps m ON m.md5 = r.map_md5
        ORDER BY r.played_at DESC
        """
    ).fetchall()
    return [dict(r) for r in rows]


def get_replay_content(conn: sqlite3.Connection, replay_id: int) -> bytes | None:
    row = conn.execute(
        "SELECT content FROM replays WHERE id = ?", (replay_id,)
    ).fetchone()
    return bytes(row["content"]) if row else None


# -----------------------------------------------------------------------------
# Snapshots
# -----------------------------------------------------------------------------

_SESSION_GAP_HOURS = 4


def _within_same_session(iso_a: str, iso_b: str) -> bool:
    """Two played_at timestamps are 'same session' iff they're within GAP hours."""
    try:
        a = datetime.fromisoformat(iso_a)
        b = datetime.fromisoformat(iso_b)
    except ValueError:
        return False
    return abs((a - b).total_seconds()) <= _SESSION_GAP_HOURS * 3600


def snapshot_player_skill(
    conn: sqlite3.Connection,
    skill: PlayerSkill,
    replays_used: int,
    latest_replay_played_at: str,
) -> int:
    """Store a snapshot, updating the current session's row if this replay belongs to it.

    Rule: if the latest existing snapshot's `latest_replay_played_at` is within
    GAP hours of the new replay, we UPDATE that row (this replay is part of the
    same session). Otherwise INSERT a new row (new session starting).

    Net effect: the snapshots table has ONE row per training session, and the
    row's values reflect the state after that session's most recent replay.
    """
    latest = conn.execute(
        "SELECT id, latest_replay_played_at FROM snapshots ORDER BY computed_at DESC, id DESC LIMIT 1"
    ).fetchone()
    if latest and _within_same_session(latest["latest_replay_played_at"], latest_replay_played_at):
        conn.execute(
            """
            UPDATE snapshots SET
                computed_at = ?,
                latest_replay_played_at = ?,
                replays_used = ?,
                skill_speed = ?, skill_stamina = ?, skill_gimmick = ?,
                skill_technical = ?, skill_consistency = ?, skill_reading = ?
            WHERE id = ?
            """,
            (
                _now(), latest_replay_played_at, replays_used,
                skill.speed, skill.stamina, skill.gimmick, skill.technical, skill.consistency,
                getattr(skill, "reading", 0.0),
                latest["id"],
            ),
        )
        conn.commit()
        return int(latest["id"])

    cursor = conn.execute(
        """
        INSERT INTO snapshots (
            computed_at, latest_replay_played_at, replays_used,
            skill_speed, skill_stamina, skill_gimmick,
            skill_technical, skill_consistency, skill_reading
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            _now(), latest_replay_played_at, replays_used,
            skill.speed, skill.stamina, skill.gimmick, skill.technical, skill.consistency,
            getattr(skill, "reading", 0.0),
        ),
    )
    conn.commit()
    return int(cursor.lastrowid or 0)


def get_latest_snapshot(conn: sqlite3.Connection) -> dict[str, Any] | None:
    row = conn.execute(
        "SELECT * FROM snapshots ORDER BY computed_at DESC, id DESC LIMIT 1"
    ).fetchone()
    return dict(row) if row else None


def get_snapshot_history(conn: sqlite3.Connection, limit: int = 20) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT * FROM snapshots ORDER BY computed_at DESC, id DESC LIMIT ?", (limit,)
    ).fetchall()
    return [dict(r) for r in rows]


def get_snapshot_before(conn: sqlite3.Connection, before_iso: str) -> dict[str, Any] | None:
    """Return the most recent snapshot with computed_at strictly before the given timestamp."""
    row = conn.execute(
        "SELECT * FROM snapshots WHERE computed_at < ? ORDER BY computed_at DESC, id DESC LIMIT 1",
        (before_iso,),
    ).fetchone()
    return dict(row) if row else None


def rebuild_snapshots(conn: sqlite3.Connection, compute_skill_fn) -> int:
    """Delete all snapshots and rebuild one-per-session from the replay history.

    `compute_skill_fn(replay_rows)` computes a PlayerSkill from a list of replay rows
    (with joined map ratings). Used after bulk ingest / refresh, when snapshots
    may have been created out of session order.

    Returns the number of snapshots created.
    """
    # Pull ALL replays with joined map ratings, ordered chronologically.
    if not _has_attached_catalog(conn):
        # No catalog attached — can't rebuild without map ratings.
        return 0

    rows = conn.execute(
        """
        SELECT r.accuracy_judged, r.count_miss, r.played_at,
               m.title, m.version,
               COALESCE(r.rating_speed_eff,       m.rating_speed)       AS rating_speed,
               COALESCE(r.rating_stamina_eff,     m.rating_stamina)     AS rating_stamina,
               COALESCE(r.rating_gimmick_eff,     m.rating_gimmick)     AS rating_gimmick,
               COALESCE(r.rating_technical_eff,   m.rating_technical)   AS rating_technical,
               COALESCE(r.rating_consistency_eff, m.rating_consistency) AS rating_consistency,
               COALESCE(r.rating_reading_eff,     m.rating_reading, 0)  AS rating_reading
        FROM replays r JOIN catalog.maps m ON m.md5 = r.map_md5
        ORDER BY r.played_at ASC
        """
    ).fetchall()

    conn.execute("DELETE FROM snapshots")

    if not rows:
        conn.commit()
        return 0

    # Group replays into sessions (same GAP rule) and snapshot at each session's end.
    def _in_session(a: str, b: str) -> bool:
        return _within_same_session(a, b)

    session_ends: list[tuple[int, str]] = []  # (index_into_rows_last_of_session, played_at)
    for i, r in enumerate(rows):
        if i == 0:
            continue
        if not _in_session(rows[i - 1]["played_at"], r["played_at"]):
            session_ends.append((i - 1, rows[i - 1]["played_at"]))
    # Always add the very last replay's index as a session end.
    session_ends.append((len(rows) - 1, rows[-1]["played_at"]))

    count = 0
    for end_idx, session_end_played_at in session_ends:
        # replays 0..end_idx (inclusive) belong to this session or earlier
        subset = rows[: end_idx + 1]
        skill = compute_skill_fn(subset)
        conn.execute(
            """
            INSERT INTO snapshots (
                computed_at, latest_replay_played_at, replays_used,
                skill_speed, skill_stamina, skill_gimmick,
                skill_technical, skill_consistency, skill_reading
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                _now(), session_end_played_at, len(subset),
                skill.speed, skill.stamina, skill.gimmick, skill.technical, skill.consistency,
                getattr(skill, "reading", 0.0),
            ),
        )
        count += 1
    conn.commit()
    return count


# -----------------------------------------------------------------------------
# Workspace summary
# -----------------------------------------------------------------------------

def workspace_status(workspace: str | Path = DEFAULT_WORKSPACE) -> dict[str, Any]:
    """Quick counts across the workspace: catalog + all discovered player DBs."""
    ws = Path(workspace)
    catalog_conn = open_catalog(ws)
    catalog_stats = {
        "maps": catalog_conn.execute("SELECT COUNT(*) AS n FROM maps").fetchone()["n"],
    }
    catalog_conn.close()

    players = discover_players(ws)
    player_stats = {}
    for player in players:
        pconn = open_plays(ws, player)
        player_stats[player] = {
            "replays": pconn.execute("SELECT COUNT(*) AS n FROM replays").fetchone()["n"],
            "snapshots": pconn.execute("SELECT COUNT(*) AS n FROM snapshots").fetchone()["n"],
            "style": (pconn.execute("SELECT style FROM player_info WHERE name = ?", (player,)).fetchone() or {"style": "unknown"})["style"],
        }
        pconn.close()

    return {
        "workspace": str(ws),
        "catalog": catalog_stats,
        "players": player_stats,
    }

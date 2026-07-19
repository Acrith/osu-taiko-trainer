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
    parity_mean            REAL NOT NULL,
    parity_hostile_ratio   REAL NOT NULL,
    inserted_at            TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS catalog_meta (
    key                    TEXT PRIMARY KEY,
    value                  TEXT NOT NULL,
    updated_at             TEXT NOT NULL
);
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
    osu_global_rank     INTEGER
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
    skill_consistency        REAL NOT NULL
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
    conn.commit()
    return conn


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
    ):
        if col not in existing:
            conn.execute(ddl)
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
            parity_mean, parity_hostile_ratio,
            inserted_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
) -> None:
    """Overwrite the judged fields on an existing replay row (rejudge)."""
    deltas = [j.hit_delta_ms for j in judged.judgments if j.hit_delta_ms is not None]
    if deltas:
        mean = sum(deltas) / len(deltas)
        var = sum((d - mean) ** 2 for d in deltas) / len(deltas)
        stddev = var ** 0.5
    else:
        mean = stddev = None
    classification_json = json.dumps(classification.by_cause) if classification else None
    cheese_rate = cheese.cheese_rate if cheese else None
    fast_cheese = cheese.fast_cheese_pairs if cheese else None
    conn.execute(
        """
        UPDATE replays SET
            accuracy_judged = ?,
            count_great = ?, count_ok = ?, count_miss = ?,
            delta_mean_ms = ?, delta_stddev_ms = ?,
            cheese_rate = ?, fast_cheese_pairs = ?,
            classification_json = ?
        WHERE id = ?
        """,
        (
            judged.accuracy,
            judged.count_great, judged.count_ok, judged.count_miss,
            mean, stddev,
            cheese_rate, fast_cheese,
            classification_json,
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
    cheese_rate = cheese.cheese_rate if cheese else None
    fast_cheese = cheese.fast_cheese_pairs if cheese else None

    cursor = conn.execute(
        """
        INSERT OR IGNORE INTO replays (
            map_md5, content, played_at, score,
            accuracy_reported, accuracy_judged,
            count_great, count_ok, count_miss,
            delta_mean_ms, delta_stddev_ms,
            cheese_rate, fast_cheese_pairs,
            classification_json,
            inserted_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
               r.classification_json, r.inserted_at,
               m.title AS map_title, m.version AS map_version, m.creator AS map_creator,
               m.beatmap_id, m.beatmapset_id,
               m.rating_speed, m.rating_stamina, m.rating_gimmick,
               m.rating_technical, m.rating_consistency
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
                skill_technical = ?, skill_consistency = ?
            WHERE id = ?
            """,
            (
                _now(), latest_replay_played_at, replays_used,
                skill.speed, skill.stamina, skill.gimmick, skill.technical, skill.consistency,
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
            skill_technical, skill_consistency
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            _now(), latest_replay_played_at, replays_used,
            skill.speed, skill.stamina, skill.gimmick, skill.technical, skill.consistency,
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
               m.rating_speed, m.rating_stamina, m.rating_gimmick,
               m.rating_technical, m.rating_consistency
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
                skill_technical, skill_consistency
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                _now(), session_end_played_at, len(subset),
                skill.speed, skill.stamina, skill.gimmick, skill.technical, skill.consistency,
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

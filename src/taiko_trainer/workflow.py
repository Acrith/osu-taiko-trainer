"""Drop-in workflow with self-contained storage.

Every parsed file is stored as a BLOB inside the DB — the workspace (catalog.db
+ <player>.db files) is fully portable to any machine and doesn't depend on
the original filesystem paths.

Public API:
    add_replay(workspace, replay_path, map_path=None)
    add_map(workspace, osu_path)
    roots_add / roots_remove / roots_list(workspace, player, ...)
    status(workspace)
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path

from . import db as db_module
from .cheese import detect_cheese
from .classification import classify_failures, summarize_failures
from .db import (
    add_map_root as db_add_map_root,
    get_map,
    insert_replay,
    list_map_roots,
    open_catalog,
    open_plays,
    remove_map_root as db_remove_map_root,
    snapshot_player_skill,
    upsert_map,
    workspace_status,
)
from .features import extract_features
from .judgment import judge_replay
from .models import TaikoBeatmap
from .osr_parser import parse_osr_file
from .osu_parser import parse_osu_file
from .player import ReplayPerformance, compute_player_skill
from .scoring import DimensionRating, rate_map


@dataclass
class AddResult:
    ok: bool
    message: str
    map_md5: str | None = None
    replay_id: int | None = None
    player: str | None = None


def _read_bytes(path: str | Path) -> bytes:
    return Path(path).read_bytes()


def _parse_bytes_as_osu(content: bytes) -> TaikoBeatmap:
    """Write content to a temp file so parse_osu_file (which reads a Path) can consume it."""
    import tempfile
    with tempfile.NamedTemporaryFile(suffix=".osu", delete=False) as tmp:
        tmp.write(content)
        tmp_path = tmp.name
    try:
        return parse_osu_file(tmp_path)
    finally:
        Path(tmp_path).unlink(missing_ok=True)


def _resolve_map(
    workspace: str | Path,
    target_md5: str,
    explicit_map_path: str | None,
    search_roots: list[str],
) -> tuple[bytes | None, TaikoBeatmap | None]:
    """Try to obtain the map bytes for target_md5 from local sources.

    Order:
      1. Already in catalog.db (returns stored blob)
      2. Explicit --map path (must match md5)
      3. Any registered map root (recursive glob)
      4. TODO: osu! API — see memory/project_osu_api_integration.md
    """
    catalog = open_catalog(workspace)
    existing = get_map(catalog, target_md5)
    if existing:
        content = db_module.get_map_content(catalog, target_md5)
        catalog.close()
        if content:
            return content, _parse_bytes_as_osu(content)

    if explicit_map_path:
        p = Path(explicit_map_path)
        if p.exists():
            content = p.read_bytes()
            if hashlib.md5(content).hexdigest() == target_md5:
                catalog.close()
                return content, _parse_bytes_as_osu(content)
        catalog.close()
        return None, None

    for root in search_roots:
        root_p = Path(root)
        if not root_p.exists():
            continue
        for cand in root_p.rglob("*.osu"):
            try:
                content = cand.read_bytes()
            except OSError:
                continue
            if hashlib.md5(content).hexdigest() == target_md5:
                catalog.close()
                return content, _parse_bytes_as_osu(content)

    catalog.close()
    return None, None


def add_replay(
    workspace: str | Path,
    replay_path: str,
    map_path: str | None = None,
) -> AddResult:
    """Ingest one replay: resolve map, store both file blobs, snapshot player skill."""
    replay_p = Path(replay_path)
    if not replay_p.exists():
        return AddResult(False, f"replay not found: {replay_path}")

    replay_content = replay_p.read_bytes()
    rp = parse_osr_file(replay_p)
    target_md5 = rp.meta.beatmap_md5.lower()
    player = rp.meta.player

    # Find (or download) the map bytes.
    plays = open_plays(workspace, player)
    roots = list_map_roots(plays)
    plays.close()

    map_content, bm = _resolve_map(workspace, target_md5, map_path, roots)
    if bm is None or map_content is None:
        msg = [
            f"could not resolve map with md5 {target_md5}",
            f"  replay says: player={player!r}",
            f"  tried: catalog cache, --map, and {len(roots)} search root(s)",
            "next steps: pass --map <path>, or add a search root via `roots add`,",
            "  or (future) enable osu! API fetch (see memory/project_osu_api_integration.md).",
        ]
        return AddResult(False, "\n".join(msg))

    features = extract_features(bm)
    rating = rate_map(features)

    # Store the map in the catalog (blob + cached rating).
    plays = open_plays(workspace, player)  # this ATTACHes catalog
    upsert_map(plays, bm, features, rating, map_content)

    judged = judge_replay(bm, rp)
    classifications = classify_failures(judged, bm, features)
    summary = summarize_failures(classifications)
    cheese = detect_cheese(judged)

    replay_id = insert_replay(
        plays, rp, judged, target_md5, replay_content,
        classification=summary,
        cheese=cheese,
    )

    # Recompute the player's snapshot including the new replay.
    prows = plays.execute(
        """
        SELECT r.accuracy_judged, r.count_miss,
               m.title, m.version,
               m.rating_speed, m.rating_stamina, m.rating_gimmick,
               m.rating_technical, m.rating_consistency
        FROM replays r JOIN catalog.maps m ON m.md5 = r.map_md5
        """
    ).fetchall()
    perfs = [
        ReplayPerformance(
            map_title=r["title"], map_diff=r["version"],
            map_rating=DimensionRating(
                speed=r["rating_speed"], stamina=r["rating_stamina"],
                gimmick=r["rating_gimmick"], technical=r["rating_technical"],
                consistency=r["rating_consistency"],
            ),
            accuracy=r["accuracy_judged"], misses=r["count_miss"],
        )
        for r in prows
    ]
    skill = compute_player_skill(perfs)
    snapshot_player_skill(
        plays, skill,
        replays_used=len(perfs),
        latest_replay_played_at=rp.meta.timestamp.isoformat(),
    )
    plays.close()

    return AddResult(
        ok=True,
        message=(
            f"added replay #{replay_id}: {player} on {bm.meta.title} [{bm.meta.version}]"
            f" · acc={judged.accuracy*100:.2f}%  misses={judged.count_miss}"
            f" → snapshot: sp={skill.speed:.0f} st={skill.stamina:.0f}"
            f" gi={skill.gimmick:.0f} te={skill.technical:.0f} co={skill.consistency:.0f}"
        ),
        map_md5=target_md5,
        replay_id=replay_id,
        player=player,
    )


def add_map(workspace: str | Path, osu_path: str) -> AddResult:
    """Add a single map to the catalog (blob + rating cached)."""
    p = Path(osu_path)
    if not p.exists():
        return AddResult(False, f"map not found: {osu_path}")

    content = p.read_bytes()
    bm = parse_osu_file(p)
    if bm.mode != 1:
        return AddResult(False, f"skipped: {p.name} is not a taiko map (mode={bm.mode})")

    features = extract_features(bm)
    rating = rate_map(features)

    catalog = open_catalog(workspace)
    upsert_map(catalog, bm, features, rating, content)
    catalog.close()

    r = rating.as_dict()
    return AddResult(
        ok=True,
        message=(
            f"added map: {bm.meta.title} [{bm.meta.version}] mapped by {bm.meta.creator}"
            f" · sp={r['speed']:.0f} st={r['stamina']:.0f} gi={r['gimmick']:.0f}"
            f" te={r['technical']:.0f} co={r['consistency']:.0f}"
        ),
        map_md5=bm.beatmap_md5,
    )


# --- root management (per-player) ----------------------------------------

def roots_add(workspace: str | Path, player: str, root: str) -> str:
    conn = open_plays(workspace, player)
    db_add_map_root(conn, root)
    conn.close()
    return f"root added to {player}.db: {root}"


def roots_remove(workspace: str | Path, player: str, root: str) -> str:
    conn = open_plays(workspace, player)
    removed = db_remove_map_root(conn, root)
    conn.close()
    return f"root removed from {player}.db: {root}" if removed else f"root not found: {root}"


def roots_list(workspace: str | Path, player: str) -> list[str]:
    conn = open_plays(workspace, player)
    roots = list_map_roots(conn)
    conn.close()
    return roots


# --- workspace summary ---------------------------------------------------

def status(workspace: str | Path) -> str:
    s = workspace_status(workspace)
    lines = [f"workspace: {s['workspace']}", f"  catalog: {s['catalog']['maps']} maps"]
    if s["players"]:
        lines.append("  players:")
        for player, stats in s["players"].items():
            lines.append(
                f"    {player:<20} style={stats['style']}  "
                f"replays={stats['replays']}  snapshots={stats['snapshots']}"
            )
    else:
        lines.append("  (no players yet — add a replay to bootstrap one)")
    return "\n".join(lines) + "\n"

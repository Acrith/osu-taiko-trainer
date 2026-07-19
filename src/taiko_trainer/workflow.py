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
from .classification import classify_failures, extract_miss_patterns, summarize_failures
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
from .mods import apply_mods_to_beatmap, parse_mods
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
    progress_cb=None,
) -> tuple[bytes | None, TaikoBeatmap | None, Path | None]:
    """Try to obtain the map bytes for target_md5 from local sources.

    Returns (content, beatmap, source_path).  source_path is the .osu file we
    matched on disk (None if the map came from the catalog cache); the caller
    uses it to ingest sibling difficulties from the same beatmap folder.

    Order:
      1. Already in catalog.db (returns stored blob, source_path=None)
      2. Explicit --map path (must match md5)
      3. Any registered map root (recursive glob)
      4. TODO: osu! API — see memory/project_osu_api_integration.md
    """
    def _report(stage, total=None, done=None, note=""):
        if progress_cb: progress_cb(stage=stage, total=total, done=done, note=note)

    catalog = open_catalog(workspace)
    existing = get_map(catalog, target_md5)
    if existing:
        _report("catalog_hit", note="already have this map cached")
        content = db_module.get_map_content(catalog, target_md5)
        catalog.close()
        if content:
            return content, _parse_bytes_as_osu(content), None

    if explicit_map_path:
        _report("explicit_map", note=f"checking {Path(explicit_map_path).name}")
        p = Path(explicit_map_path)
        if p.exists():
            content = p.read_bytes()
            if hashlib.md5(content).hexdigest() == target_md5:
                catalog.close()
                return content, _parse_bytes_as_osu(content), p
        catalog.close()
        return None, None, None

    for root in search_roots:
        root_p = Path(root)
        if not root_p.exists():
            continue
        _report("search_scan", note=f"enumerating {root_p.name}...")
        candidates = list(root_p.rglob("*.osu"))
        n = len(candidates)
        for i, cand in enumerate(candidates):
            if i % 100 == 0 or i == n - 1:
                _report("search_hash", total=n, done=i,
                        note=f"searching {root_p.name}")
            try:
                content = cand.read_bytes()
            except OSError:
                continue
            if hashlib.md5(content).hexdigest() == target_md5:
                _report("search_hit", total=n, done=i, note=cand.name)
                catalog.close()
                return content, _parse_bytes_as_osu(content), cand

    # osu! API fallback — only if the workspace has OAuth credentials saved.
    from . import osu_api
    if osu_api.is_configured(catalog):
        _report("api_lookup", note=f"asking osu! API for map md5 {target_md5[:8]}...")
        try:
            lookup = osu_api.lookup_beatmap(catalog, target_md5)
        except osu_api.OsuApiError as e:
            _report("api_error", note=str(e)[:120])
            lookup = None
        if lookup and lookup.beatmapset_id:
            _report("api_download", note=f"downloading set {lookup.beatmapset_id} ({lookup.title})")
            try:
                osz_bytes = osu_api.download_osz(lookup.beatmapset_id)
            except osu_api.OsuApiError as e:
                _report("api_error", note=str(e)[:120])
                osz_bytes = None
            if osz_bytes:
                files = osu_api.extract_osu_files_from_osz(osz_bytes)
                # Find the exact match by MD5.
                content = None
                for member_content in files.values():
                    if hashlib.md5(member_content).hexdigest().lower() == target_md5:
                        content = member_content
                        break
                if content:
                    _report("api_hit", note=f"got '{lookup.title} [{lookup.version}]' + {len(files)-1} sibling(s)")
                    # Ingest ALL sibling .osu files right here — we already
                    # have them in memory. No further filesystem scan needed.
                    _ingest_maps_from_memory(catalog, files, exclude_md5=target_md5,
                                             progress_cb=progress_cb)
                    catalog.close()
                    return content, _parse_bytes_as_osu(content), None

    catalog.close()
    return None, None, None


def _ingest_maps_from_memory(
    catalog,
    files: dict[str, bytes],
    exclude_md5: str,
    progress_cb=None,
) -> int:
    """Ingest .osu files whose bytes we already have in memory (typically an
    unpacked .osz downloaded from a mirror). Skips the played diff (excluded
    by MD5) and any duplicate already in catalog.

    Takes an open catalog connection — caller is responsible for closing."""
    def _report(stage, total=None, done=None, note=""):
        if progress_cb: progress_cb(stage=stage, total=total, done=done, note=note)
    added = 0
    items = list(files.items())
    for i, (name, content) in enumerate(items):
        _report("ingest_siblings", total=len(items), done=i, note=name)
        md5 = hashlib.md5(content).hexdigest()
        if md5 == exclude_md5 or get_map(catalog, md5):
            continue
        try:
            bm = _parse_bytes_as_osu(content)
        except Exception:
            continue
        if bm.mode != 1:
            continue
        feats = extract_features(bm)
        rating = rate_map(feats)
        upsert_map(catalog, bm, feats, rating, content)
        added += 1
    return added


def _ingest_sibling_maps(
    workspace: str | Path,
    folder: Path,
    exclude_md5: str,
    progress_cb=None,
) -> int:
    """After we find a played map on disk, ingest every other .osu in the same
    folder. Costs almost nothing (files are local, already an mtime hit) and
    enormously grows the recommendation pool: every uploaded Oni contributes
    its Kantan/Futsuu/Muzukashii/Inner Oni ratings to future suggestions.
    Skipped: the played diff itself (already ingested by the caller), any
    non-taiko map, any md5 already in the catalog. Returns count added."""
    def _report(stage, total=None, done=None, note=""):
        if progress_cb: progress_cb(stage=stage, total=total, done=done, note=note)

    if not folder.exists() or not folder.is_dir():
        return 0
    siblings = [p for p in folder.glob("*.osu")]
    if not siblings:
        return 0

    catalog = open_catalog(workspace)
    added = 0
    for i, cand in enumerate(siblings):
        _report("ingest_siblings", total=len(siblings), done=i, note=f"sibling: {cand.name}")
        try:
            content = cand.read_bytes()
        except OSError:
            continue
        md5 = hashlib.md5(content).hexdigest()
        if md5 == exclude_md5 or get_map(catalog, md5):
            continue
        try:
            bm = _parse_bytes_as_osu(content)
        except Exception:
            continue
        if bm.mode != 1:  # non-taiko
            continue
        features = extract_features(bm)
        rating = rate_map(features)
        upsert_map(catalog, bm, features, rating, content)
        added += 1
    catalog.close()
    return added


def add_replay(
    workspace: str | Path,
    replay_path: str,
    map_path: str | None = None,
    progress_cb=None,
) -> AddResult:
    """Ingest one replay: resolve map, store both file blobs, snapshot player skill.

    progress_cb (optional): called with keyword-args {stage, total, done, note}
    at each pipeline step so a caller (webapp) can render a progress bar.
    """
    def _report(stage, total=None, done=None, note=""):
        if progress_cb: progress_cb(stage=stage, total=total, done=done, note=note)

    replay_p = Path(replay_path)
    if not replay_p.exists():
        return AddResult(False, f"replay not found: {replay_path}")

    _report("parse_replay", note=replay_p.name)
    replay_content = replay_p.read_bytes()
    rp = parse_osr_file(replay_p)
    target_md5 = rp.meta.beatmap_md5.lower()
    player = rp.meta.player

    # Find (or download) the map bytes.
    plays = open_plays(workspace, player)
    roots = list_map_roots(plays)
    plays.close()

    _report("resolve_map", note=f"looking up map md5 {target_md5[:8]}...")
    map_content, bm, source_path = _resolve_map(workspace, target_md5, map_path, roots, progress_cb=progress_cb)
    if bm is None or map_content is None:
        msg = [
            f"could not resolve map with md5 {target_md5}",
            f"  replay says: player={player!r}",
            f"  tried: catalog cache, --map, and {len(roots)} search root(s)",
            "next steps: pass --map <path>, or add a search root via `roots add`,",
            "  or (future) enable osu! API fetch (see memory/project_osu_api_integration.md).",
        ]
        return AddResult(False, "\n".join(msg))

    _report("rate_map", note="computing map features + rating")
    features = extract_features(bm)
    rating = rate_map(features)

    # Store the map in the catalog (blob + cached rating).
    plays = open_plays(workspace, player)  # this ATTACHes catalog
    upsert_map(plays, bm, features, rating, map_content)
    plays.close()

    # Also ingest sibling difficulties from the beatmap folder. Grows the
    # recommendation pool for free — no additional file search, and any
    # sibling ingested here has ratings but no player-play data so nothing
    # inflates the skill vector.
    if source_path is not None:
        added = _ingest_sibling_maps(workspace, source_path.parent, target_md5, progress_cb=progress_cb)
        if added:
            _report("siblings_done", note=f"+{added} sibling difficulties added to catalog")

    plays = open_plays(workspace, player)  # reopen for the rest of the pipeline

    # Apply any active mods (DT/HR/HD/etc) BEFORE judgment + rating so the
    # play is scored against what the player actually experienced. For NM
    # this is a no-op — `apply_mods_to_beatmap` returns `bm` unchanged.
    mods = parse_mods(rp.meta.mods)
    play_bm = apply_mods_to_beatmap(bm, mods)
    play_features = extract_features(play_bm) if mods.alters_map else features
    play_rating = rate_map(play_features) if mods.alters_map else rating

    _report("judge", note=f"running per-note judgment ({mods.label})")
    judged = judge_replay(play_bm, rp, hit_window_mult=mods.hit_window_mult)
    _report("classify", note=f"classifying {judged.count_miss} misses")
    classifications = classify_failures(judged, play_bm, play_features)
    summary = summarize_failures(classifications)
    miss_patterns = extract_miss_patterns(classifications, play_bm.hittable())
    cheese = detect_cheese(judged)

    _report("store", note="writing to database")
    replay_id = insert_replay(
        plays, rp, judged, target_md5, replay_content,
        classification=summary,
        cheese=cheese,
        miss_patterns=miss_patterns,
        mods_bitfield=mods.bitfield,
        mods_label=mods.label,
        effective_rating=play_rating if mods.alters_map else None,
    )

    # Recompute the player's snapshot including the new replay. Use the
    # effective (mod-adjusted) rating so DT plays weigh at the difficulty
    # the player actually cleared — a 99% on DT should count more than a
    # 99% on NM of the same map.
    prows = plays.execute(
        """
        SELECT r.accuracy_judged, r.count_miss,
               m.title, m.version,
               COALESCE(r.rating_speed_eff,       m.rating_speed)       AS rating_speed,
               COALESCE(r.rating_stamina_eff,     m.rating_stamina)     AS rating_stamina,
               COALESCE(r.rating_gimmick_eff,     m.rating_gimmick)     AS rating_gimmick,
               COALESCE(r.rating_technical_eff,   m.rating_technical)   AS rating_technical,
               COALESCE(r.rating_consistency_eff, m.rating_consistency) AS rating_consistency
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
    _report("snapshot", note="recomputing player skill vector")
    skill = compute_player_skill(perfs)
    snapshot_player_skill(
        plays, skill,
        replays_used=len(perfs),
        latest_replay_played_at=rp.meta.timestamp.isoformat(),
    )
    plays.close()
    _report("done", note=f"replay #{replay_id} added")

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

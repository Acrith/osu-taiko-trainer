"""Bulk ingestion + rating refresh (workspace edition).

    taiko-trainer ingest <workspace> <root_dir>
        Walks a folder tree, adds every .osu to catalog and every .osr into the
        appropriate per-player DB. Uses the blob-storage schema so all data
        becomes self-contained.

    taiko-trainer refresh <workspace>
        Re-parses every stored .osu BLOB in the catalog and recomputes its
        rating. Then re-snapshots every player. No filesystem dependency —
        the DB is portable.
"""
from __future__ import annotations

import hashlib
import sys
from pathlib import Path

from .cheese import detect_cheese
from .classification import classify_failures, extract_miss_patterns, summarize_failures
from .db import (
    catalog_path,
    discover_players,
    get_all_maps,
    get_map_content,
    open_catalog,
    open_plays,
    rebuild_snapshots,
    snapshot_player_skill,
    update_replay_judgment,
    upsert_map,
)
from .features import extract_features
from .judgment import judge_replay
from .osr_parser import parse_osr_file
from .osu_parser import parse_osu_file
from .player import ReplayPerformance, compute_player_skill
from .scoring import DimensionRating, rate_map
from .workflow import _parse_bytes_as_osu, add_replay


def ingest(workspace: str, root: str) -> None:
    root_path = Path(root)
    if not root_path.exists():
        print(f"ERROR: root not found: {root_path}", file=sys.stderr)
        sys.exit(1)

    # --- Pass 1: every .osu → catalog ---
    catalog = open_catalog(workspace)
    osu_files = list(root_path.rglob("*.osu"))
    print(f"Found {len(osu_files)} .osu files")
    stored = 0
    for osu_path in osu_files:
        try:
            bm = parse_osu_file(osu_path)
        except Exception as e:
            print(f"  SKIP {osu_path.name}: parse error {e}")
            continue
        if bm.mode != 1:
            continue
        content = osu_path.read_bytes()
        feats = extract_features(bm)
        rating = rate_map(feats)
        upsert_map(catalog, bm, feats, rating, content)
        stored += 1
    catalog.close()
    print(f"Catalog: {stored} taiko maps stored")

    # --- Pass 2: every .osr → per-player plays.db ---
    osr_files = list(root_path.rglob("*.osr"))
    print(f"Found {len(osr_files)} .osr files")
    added = 0
    for osr_path in osr_files:
        try:
            result = add_replay(workspace, str(osr_path))
        except Exception as e:
            print(f"  ERROR {osr_path.name}: {e}")
            continue
        if result.ok:
            added += 1
        else:
            print(f"  SKIP {osr_path.name}: {result.message.splitlines()[0]}")
    print(f"Added {added} replays across players")

    # --- Pass 3: rebuild snapshots chronologically (one per session) ---
    for player in discover_players(workspace):
        conn = open_plays(workspace, player)
        count = _rebuild_for_player(conn)
        conn.close()
        print(f"  {player}: {count} session snapshot(s)")


def _row_to_perf(r) -> ReplayPerformance:
    return ReplayPerformance(
        map_title=r["title"], map_diff=r["version"],
        map_rating=DimensionRating(
            speed=r["rating_speed"], stamina=r["rating_stamina"],
            gimmick=r["rating_gimmick"], technical=r["rating_technical"],
            consistency=r["rating_consistency"],
        ),
        accuracy=r["accuracy_judged"], misses=r["count_miss"],
    )


def _rebuild_for_player(conn) -> int:
    """Rebuild snapshots for the connected plays DB, one per session."""
    return rebuild_snapshots(
        conn,
        compute_skill_fn=lambda rows: compute_player_skill([_row_to_perf(r) for r in rows]),
    )


def refresh_ratings(workspace: str) -> None:
    """Re-parse every stored map blob and recompute its rating. Then re-snapshot every player."""
    catalog = open_catalog(workspace)
    rows = catalog.execute("SELECT md5, title, version FROM maps").fetchall()
    refreshed = 0
    for row in rows:
        md5 = row["md5"]
        content = get_map_content(catalog, md5)
        if content is None:
            print(f"  SKIP {row['title']} [{row['version']}]: no stored content")
            continue
        try:
            bm = _parse_bytes_as_osu(content)
        except Exception as e:
            print(f"  SKIP {row['title']} [{row['version']}]: parse error {e}")
            continue
        feats = extract_features(bm)
        rating = rate_map(feats)
        upsert_map(catalog, bm, feats, rating, content)
        refreshed += 1
    catalog.close()
    print(f"Refreshed {refreshed} maps")

    # Rejudge every stored replay against the fresh map + latest parser,
    # then rebuild snapshots chronologically (one per session).
    import tempfile
    for player in discover_players(workspace):
        conn = open_plays(workspace, player)
        replays = conn.execute("SELECT id, map_md5, content FROM replays").fetchall()
        rejudged = 0
        for r in replays:
            map_bytes = get_map_content(conn, r["map_md5"])
            if not map_bytes:
                continue
            try:
                bm = _parse_bytes_as_osu(map_bytes)
                with tempfile.NamedTemporaryFile(suffix=".osr", delete=False) as tmp:
                    tmp.write(bytes(r["content"]))
                    tmp_path = tmp.name
                try:
                    rp = parse_osr_file(tmp_path)
                finally:
                    Path(tmp_path).unlink(missing_ok=True)
                feats = extract_features(bm)
                judged = judge_replay(bm, rp)
                classes = classify_failures(judged, bm, feats)
                summary = summarize_failures(classes)
                miss_patterns = extract_miss_patterns(classes, bm.hittable())
                cheese = detect_cheese(judged)
                update_replay_judgment(conn, r["id"], judged, summary, cheese, miss_patterns)
                rejudged += 1
            except Exception as e:
                print(f"  {player}: rejudge SKIP replay #{r['id']}: {e}")
        count = _rebuild_for_player(conn)
        conn.close()
        print(f"  {player}: {rejudged}/{len(replays)} replays rejudged  ·  {count} session snapshot(s) rebuilt")


def main() -> None:
    if len(sys.argv) >= 2 and sys.argv[1] == "refresh":
        workspace = sys.argv[2] if len(sys.argv) >= 3 else "."
        refresh_ratings(workspace)
        return
    workspace = sys.argv[1] if len(sys.argv) >= 2 else "."
    root = sys.argv[2] if len(sys.argv) >= 3 else "references"
    ingest(workspace, root)


if __name__ == "__main__":
    main()

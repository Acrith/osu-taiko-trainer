"""Drop maps + replays that no longer meet the ingest gate.

Rules (must match workflow._reject_reason_for_map):
  - mode != 1              (converted std/catch/mania → taiko)
  - duration_s > 600       (marathons)
  - bpm_max > 999          (storyboard-gimmick nonsense timing)

Also deletes replays that reference any removed map (orphan cleanup — replays
whose map_md5 is no longer in catalog can't display anything useful anyway).

Dry-run by default. Pass --commit to actually delete.

Usage on droplet:
    docker compose exec taiko-trainer \\
        uv run python scripts/cleanup_bad_maps.py --workspace /data
    # then, if the preview looks right:
    docker compose exec taiko-trainer \\
        uv run python scripts/cleanup_bad_maps.py --workspace /data --commit
"""
from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path

from taiko_trainer.db import (
    CATALOG_FILENAME,
    catalog_path,
    discover_players,
    player_db_path,
)


def _bad_map_where() -> str:
    return "mode != 1 OR duration_s > 600 OR bpm_max > 999"


def preview(workspace: Path) -> list[dict]:
    cat = sqlite3.connect(str(catalog_path(workspace)))
    cat.row_factory = sqlite3.Row
    rows = cat.execute(
        f"SELECT md5, artist, title, version, mode, duration_s, bpm_max "
        f"FROM maps WHERE {_bad_map_where()}"
    ).fetchall()
    cat.close()
    return [dict(r) for r in rows]


def apply_cleanup(workspace: Path, commit: bool) -> dict:
    bad = preview(workspace)
    bad_md5s = [r["md5"] for r in bad]

    replay_hits = {}  # {player: [replay_id, ...]}
    for player in discover_players(workspace):
        pdb_path = player_db_path(workspace, player)
        if not pdb_path.exists():
            continue
        pdb = sqlite3.connect(str(pdb_path))
        pdb.row_factory = sqlite3.Row
        rows = pdb.execute(
            "SELECT id, map_md5, played_at FROM replays WHERE map_md5 IN ("
            + ",".join(["?"] * len(bad_md5s)) + ")",
            bad_md5s,
        ).fetchall() if bad_md5s else []
        if rows:
            replay_hits[player] = [dict(r) for r in rows]
        pdb.close()

    if not commit:
        return {"maps": bad, "replays": replay_hits, "committed": False}

    # DELETE PHASE.
    cat = sqlite3.connect(str(catalog_path(workspace)))
    cat.execute(f"DELETE FROM maps WHERE {_bad_map_where()}")
    cat.commit()
    cat.close()

    for player, rows in replay_hits.items():
        ids = [r["id"] for r in rows]
        pdb = sqlite3.connect(str(player_db_path(workspace, player)))
        pdb.execute(
            "DELETE FROM replays WHERE id IN ("
            + ",".join(["?"] * len(ids)) + ")",
            ids,
        )
        pdb.commit()
        pdb.close()

    return {"maps": bad, "replays": replay_hits, "committed": True}


def main() -> int:
    ap = argparse.ArgumentParser(prog="cleanup_bad_maps")
    ap.add_argument("--workspace", required=True, help="path to workspace (contains catalog.db)")
    ap.add_argument("--commit", action="store_true",
                    help="actually delete; without this flag, previews only")
    args = ap.parse_args()

    ws = Path(args.workspace)
    if not (ws / CATALOG_FILENAME).exists():
        print(f"no {CATALOG_FILENAME} in {ws}", file=sys.stderr)
        return 2

    result = apply_cleanup(ws, commit=args.commit)

    print(f"maps to remove: {len(result['maps'])}")
    for m in result["maps"]:
        title = f"{m['artist']} - {m['title']} [{m['version']}]"
        reason = []
        if m["mode"] != 1:
            reason.append(f"mode={m['mode']}")
        if m["duration_s"] > 600:
            reason.append(f"duration={int(m['duration_s'])}s")
        if (m["bpm_max"] or 0) > 999:
            reason.append(f"bpm_max={m['bpm_max']:.0f}")
        print(f"  {m['md5'][:12]}...  {title[:70]}  [{', '.join(reason)}]")

    total_replays = sum(len(v) for v in result["replays"].values())
    print(f"\norphan replays to remove: {total_replays}")
    for player, rows in result["replays"].items():
        for r in rows:
            print(f"  {player}#{r['id']}  played_at={r['played_at']}  md5={r['map_md5'][:12]}...")

    if not args.commit:
        print("\n(dry-run — nothing deleted. re-run with --commit to apply.)")
    else:
        print("\ndeleted.")
    return 0


if __name__ == "__main__":
    sys.exit(main())

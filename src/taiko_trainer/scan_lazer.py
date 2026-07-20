"""Retroactive scan: find already-uploaded replays that would fail the
lazer custom-rate gate (i.e. lazer plays where DT/HT uses a non-standard
speed_change, e.g. 1.34× instead of 1.50×).

Dry-run by default. Pass --commit to actually delete the offending
replays. Standard-rate lazer plays and stable plays are untouched.

Usage on droplet:
    docker compose exec taiko-trainer \\
        taiko-trainer scan-lazer --workspace /workspace
    # if the preview looks right:
    docker compose exec taiko-trainer \\
        taiko-trainer scan-lazer --workspace /workspace --commit
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
from taiko_trainer.osr_parser import lazer_custom_rate


def scan(workspace: Path) -> list[dict]:
    """Return one entry per offending replay across every player DB."""
    hits: list[dict] = []
    for player in discover_players(workspace):
        p = player_db_path(workspace, player)
        if not p.exists():
            continue
        pdb = sqlite3.connect(str(p))
        pdb.row_factory = sqlite3.Row
        rows = pdb.execute(
            "SELECT id, map_md5, played_at, mods_label, accuracy_reported, "
            "accuracy_judged, content FROM replays ORDER BY id"
        ).fetchall()
        for r in rows:
            content = bytes(r["content"] or b"")
            rate = lazer_custom_rate(content)
            if rate is None:
                continue
            hits.append({
                "player": player,
                "id": r["id"],
                "map_md5": r["map_md5"],
                "played_at": r["played_at"],
                "mods_label": r["mods_label"],
                "acc_reported": r["accuracy_reported"],
                "acc_judged": r["accuracy_judged"],
                "rate": rate,
            })
        pdb.close()
    return hits


def delete(workspace: Path, hits: list[dict]) -> None:
    """Delete the offending replay rows. Groups by player DB for efficiency."""
    by_player: dict[str, list[int]] = {}
    for h in hits:
        by_player.setdefault(h["player"], []).append(h["id"])
    for player, ids in by_player.items():
        pdb = sqlite3.connect(str(player_db_path(workspace, player)))
        pdb.execute(
            "DELETE FROM replays WHERE id IN (" + ",".join("?" * len(ids)) + ")",
            ids,
        )
        pdb.commit()
        pdb.close()


def main() -> int:
    ap = argparse.ArgumentParser(prog="taiko-trainer scan-lazer")
    ap.add_argument("--workspace", required=True, help="path to workspace (contains catalog.db)")
    ap.add_argument("--commit", action="store_true",
                    help="actually delete; without this flag, previews only")
    args = ap.parse_args()

    ws = Path(args.workspace)
    if not (ws / CATALOG_FILENAME).exists():
        print(f"no {CATALOG_FILENAME} in {ws}", file=sys.stderr)
        return 2

    hits = scan(ws)

    if not hits:
        print("no lazer custom-rate replays found. all plays match stable-standard rates.")
        return 0

    # Group print by player for readability.
    by_player: dict[str, list[dict]] = {}
    for h in hits:
        by_player.setdefault(h["player"], []).append(h)

    print(f"lazer custom-rate replays found: {len(hits)} across {len(by_player)} player(s)")
    print()
    for player, rows in by_player.items():
        print(f"[{player}] {len(rows)} replay(s):")
        for r in rows:
            acc_r = (r["acc_reported"] or 0) * 100
            acc_j = (r["acc_judged"] or 0) * 100
            print(f"  #{r['id']:>5}  {r['played_at'][:19]}  "
                  f"{r['mods_label'] or 'NM':<6}  rate={r['rate']:.2f}×  "
                  f"acc reported={acc_r:.2f}%  judged={acc_j:.2f}%  "
                  f"md5={r['map_md5'][:8]}...")
        print()

    if not args.commit:
        print("(dry-run — nothing deleted. re-run with --commit to apply.)")
    else:
        delete(ws, hits)
        print(f"deleted {len(hits)} replay(s). skill snapshots will re-settle "
              f"on next `taiko-trainer refresh`.")
    return 0


if __name__ == "__main__":
    sys.exit(main())

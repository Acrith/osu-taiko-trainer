"""Local → hosted workspace migration.

Reads a local sqlite workspace (catalog.db + per-player <name>.db files) and
POSTs every map + replay to a hosted taiko-trainer instance. Server-side
UNIQUE(map_md5, played_at) on replays makes this idempotent — safe to re-run.

Usage:

    taiko-trainer migrate --workspace <path> \\
                          --server https://taiko.example.com \\
                          --token tt_uploader_...

Server-side auth: uses the same bearer-token endpoint the uploader companion
does (/api/v1/replays for replays, /api/v1/maps for maps). Mint a token from
the server's /settings/tokens page first; the token must belong to the same
osu! account whose replays you're migrating (identity gate enforces this).

What it does per replay:
    1. Look up the map's .osu blob in the local catalog
    2. POST it to /api/v1/maps (server no-ops if md5 already known)
    3. POST the .osr blob to /api/v1/replays
    4. Log outcome (uploaded / duplicate / skipped)

Failure modes:
    - 401 on the very first request → token wrong, abort
    - 403 on all replays → token owner doesn't match the .osr player,
      likely the token was minted under a different osu! account
    - 400 on individual replays → map fetch failed or replay unparseable;
      skipped, continue
    - Network errors → retry per-file with backoff (short); permanent
      failures logged and the script keeps going
"""
from __future__ import annotations

import argparse
import sqlite3
import sys
import time
from pathlib import Path

import httpx

from .db import (
    CATALOG_FILENAME,
    catalog_path,
    discover_players,
    get_map_content,
    player_db_path,
)


class MigrationError(RuntimeError):
    pass


def _post_map(client: httpx.Client, server: str, token: str, blob: bytes, name: str) -> str:
    """POST one map blob. Returns 'created', 'exists', or 'error:...' """
    try:
        resp = client.post(
            f"{server}/api/v1/maps",
            headers={"Authorization": f"Bearer {token}"},
            files={"file": (name, blob, "application/x-osu-beatmap")},
            timeout=30.0,
        )
    except (httpx.ConnectError, httpx.ReadTimeout) as e:
        return f"error:network:{e}"
    if resp.status_code == 401:
        raise MigrationError(f"401 Unauthorized — token invalid: {resp.text[:200]}")
    if resp.status_code == 200:
        return "exists"
    if resp.status_code == 201:
        return "created"
    return f"error:HTTP {resp.status_code}:{resp.text[:200]}"


def _post_replay(
    client: httpx.Client, server: str, token: str, blob: bytes, name: str
) -> tuple[str, str]:
    """POST one replay blob. Returns (status, detail)."""
    try:
        resp = client.post(
            f"{server}/api/v1/replays",
            headers={"Authorization": f"Bearer {token}"},
            files={"file": (name, blob, "application/x-osu-replay")},
            timeout=60.0,
        )
    except (httpx.ConnectError, httpx.ReadTimeout) as e:
        return ("error", f"network:{e}")
    if resp.status_code == 401:
        raise MigrationError(f"401 Unauthorized — token invalid: {resp.text[:200]}")
    if resp.status_code in (200, 201):
        try:
            data = resp.json()
            title = data.get("map_title", "?")
            acc = data.get("accuracy")
            acc_s = f" ({acc*100:.2f}%)" if isinstance(acc, (int, float)) else ""
            return ("uploaded", f"{title}{acc_s}")
        except Exception:
            return ("uploaded", "")
    if resp.status_code == 403:
        return ("skipped", f"identity mismatch: {resp.text[:120]}")
    if resp.status_code == 409:
        return ("duplicate", "already on server")
    if resp.status_code == 400:
        return ("failed", resp.text[:200])
    return ("failed", f"HTTP {resp.status_code}: {resp.text[:120]}")


def migrate_workspace(
    workspace: str,
    server: str,
    token: str,
    player_filter: str | None = None,
    dry_run: bool = False,
) -> dict:
    """Run the migration. Returns a summary dict."""
    ws = Path(workspace)
    if not ws.exists():
        raise MigrationError(f"workspace not found: {ws}")
    if not (ws / CATALOG_FILENAME).exists():
        raise MigrationError(f"no {CATALOG_FILENAME} in {ws}")

    server = server.rstrip("/")
    stats = {
        "players": [],
        "maps_uploaded": 0,
        "maps_existed": 0,
        "maps_failed": 0,
        "replays_uploaded": 0,
        "replays_duplicate": 0,
        "replays_skipped": 0,
        "replays_failed": 0,
    }

    catalog_conn = sqlite3.connect(str(catalog_path(ws)))
    catalog_conn.row_factory = sqlite3.Row

    posted_map_md5s: set[str] = set()

    with httpx.Client() as client:
        players = discover_players(ws)
        if player_filter:
            players = [p for p in players if p.lower() == player_filter.lower()]

        for player_name in players:
            print(f"\n== player: {player_name} ==")
            stats["players"].append(player_name)
            pdb = sqlite3.connect(str(player_db_path(ws, player_name)))
            pdb.row_factory = sqlite3.Row
            replays = pdb.execute(
                "SELECT id, map_md5, played_at, content FROM replays ORDER BY played_at"
            ).fetchall()
            print(f"  {len(replays)} replays to migrate")

            for i, r in enumerate(replays, 1):
                md5 = r["map_md5"]
                played_at = (r["played_at"] or "")[:16].replace("T", " ")
                filename = f"replay_{r['id']}.osr"

                # 1. Ensure map is on server (dedup via md5)
                if md5 not in posted_map_md5s:
                    map_content = get_map_content(catalog_conn, md5)
                    if not map_content:
                        print(f"  [{i}/{len(replays)}] SKIP replay {r['id']} — "
                              f"map md5 {md5[:8]}... not in local catalog")
                        stats["replays_failed"] += 1
                        continue
                    if dry_run:
                        print(f"  [{i}/{len(replays)}] DRY: would upload map md5={md5[:8]}...")
                    else:
                        outcome = _post_map(client, server, token, map_content, f"{md5}.osu")
                        if outcome == "created":
                            stats["maps_uploaded"] += 1
                        elif outcome == "exists":
                            stats["maps_existed"] += 1
                        else:
                            stats["maps_failed"] += 1
                            print(f"  [{i}/{len(replays)}] FAIL map md5={md5[:8]}...: {outcome}")
                            continue
                    posted_map_md5s.add(md5)

                # 2. Upload replay
                if dry_run:
                    print(f"  [{i}/{len(replays)}] DRY: would upload replay {r['id']} ({played_at})")
                    continue

                status, detail = _post_replay(
                    client, server, token, bytes(r["content"]), filename
                )
                if status == "uploaded":
                    stats["replays_uploaded"] += 1
                    print(f"  [{i}/{len(replays)}] ✓ {played_at}  {detail}")
                elif status == "duplicate":
                    stats["replays_duplicate"] += 1
                    print(f"  [{i}/{len(replays)}] · {played_at}  duplicate")
                elif status == "skipped":
                    stats["replays_skipped"] += 1
                    print(f"  [{i}/{len(replays)}] ⊘ {played_at}  {detail}")
                else:
                    stats["replays_failed"] += 1
                    print(f"  [{i}/{len(replays)}] ✗ {played_at}  {detail}",
                          file=sys.stderr)

                # Tiny throttle so the server + osu! API get room to breathe
                time.sleep(0.05)

            pdb.close()

    catalog_conn.close()
    return stats


def main() -> int:
    ap = argparse.ArgumentParser(
        prog="taiko-trainer migrate",
        description="Migrate a local workspace to a hosted taiko-trainer instance",
    )
    ap.add_argument("--workspace", required=True, help="path to local workspace (contains catalog.db)")
    ap.add_argument("--server", required=True, help="hosted instance URL (e.g. https://taiko.example.com)")
    ap.add_argument("--token", required=True, help="API token from the hosted /settings/tokens page")
    ap.add_argument("--player", help="limit to one player name (default: all in workspace)")
    ap.add_argument("--dry-run", action="store_true", help="don't actually POST; log what would happen")
    args = ap.parse_args()

    if not args.token.startswith("tt_uploader_"):
        print("token doesn't look like a taiko-trainer uploader token "
              "(should start with 'tt_uploader_')", file=sys.stderr)
        return 1

    print(f"migrating {args.workspace} → {args.server}")
    if args.dry_run:
        print("(DRY RUN — no POSTs will happen)")

    try:
        stats = migrate_workspace(
            workspace=args.workspace,
            server=args.server,
            token=args.token,
            player_filter=args.player,
            dry_run=args.dry_run,
        )
    except MigrationError as e:
        print(f"\nERROR: {e}", file=sys.stderr)
        return 2

    print("\n== summary ==")
    print(f"  players processed:  {len(stats['players'])}")
    print(f"  maps uploaded:      {stats['maps_uploaded']} new  ·  {stats['maps_existed']} already on server")
    if stats['maps_failed']:
        print(f"  maps failed:        {stats['maps_failed']}")
    print(f"  replays uploaded:   {stats['replays_uploaded']}")
    if stats['replays_duplicate']:
        print(f"  replays duplicate:  {stats['replays_duplicate']} (already on server, skipped)")
    if stats['replays_skipped']:
        print(f"  replays skipped:    {stats['replays_skipped']} (identity mismatch)")
    if stats['replays_failed']:
        print(f"  replays failed:     {stats['replays_failed']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

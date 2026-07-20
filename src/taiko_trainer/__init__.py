"""osu!taiko training assistant — unified CLI.

    taiko-trainer <subcommand> [args]

The workspace concept: a directory holding `catalog.db` (shared map catalog)
and one `<player>.db` per player (containing their plays + snapshots +
config + map roots). Both are self-contained: every parsed .osu and .osr is
stored as a blob, so a workspace directory is fully portable across machines.

Default workspace = current directory (".").
"""
from __future__ import annotations

import sys


_USAGE = """taiko-trainer — osu!taiko training assistant

Commands (all take a workspace path; defaults to current dir "."):
  status [<ws>]                   quick workspace overview
  ingest <ws> <root>              bulk ingest all .osu/.osr under a root
  add <ws> <replay> [--map <osu>] add ONE replay, auto-resolving its map
  add-map <ws> <osu>              add a single .osu to the catalog
  roots <ws> <player> add|remove|list <path>
                                  per-player map search roots
  refresh <ws>                    re-parse stored blobs + recompute all ratings
  cleanup --workspace <ws> [--commit]
                                  drop maps that fail the ingest gate + orphan replays
                                  (dry-run by default; --commit to apply)
  player <ws> <player> <style> [notes]
                                  register or update a player's playstyle
  report <ws> <player>            training report + suggestions
  validate                        verify reference-map diagonals still pass
  serve [--ws <path>] [--host <h>] [--port <p>]
                                  start the local web UI
  smoke [<osu>] [<osr>]           smoke-test the parsers
  help                            show this message
"""


def main() -> None:
    if len(sys.argv) < 2 or sys.argv[1] in ("help", "-h", "--help"):
        print(_USAGE, end="")
        sys.exit(0)

    cmd = sys.argv[1]
    sys.argv = [sys.argv[0]] + sys.argv[2:]

    if cmd == "status":
        from .workflow import status
        ws = sys.argv[1] if len(sys.argv) >= 2 else "."
        print(status(ws), end="")

    elif cmd == "ingest":
        from .ingest import ingest
        ws = sys.argv[1] if len(sys.argv) >= 2 else "."
        root = sys.argv[2] if len(sys.argv) >= 3 else "references"
        ingest(ws, root)

    elif cmd == "add":
        from .workflow import add_replay
        if len(sys.argv) < 3:
            print("usage: taiko-trainer add <ws> <replay.osr> [--map <osu>]", file=sys.stderr); sys.exit(1)
        ws = sys.argv[1]
        args = sys.argv[2:]
        map_path = None
        if "--map" in args:
            i = args.index("--map")
            if i + 1 >= len(args):
                print("--map requires a path", file=sys.stderr); sys.exit(1)
            map_path = args[i + 1]
            args = args[:i] + args[i + 2:]
        if not args:
            print("usage: taiko-trainer add <ws> <replay.osr> [--map <osu>]", file=sys.stderr); sys.exit(1)
        result = add_replay(ws, args[0], map_path=map_path)
        print(result.message)
        sys.exit(0 if result.ok else 1)

    elif cmd == "add-map":
        from .workflow import add_map
        if len(sys.argv) < 3:
            print("usage: taiko-trainer add-map <ws> <osu>", file=sys.stderr); sys.exit(1)
        result = add_map(sys.argv[1], sys.argv[2])
        print(result.message)
        sys.exit(0 if result.ok else 1)

    elif cmd == "roots":
        from .workflow import roots_add, roots_list, roots_remove
        if len(sys.argv) < 4:
            print("usage: taiko-trainer roots <ws> <player> add|remove|list [<path>]", file=sys.stderr); sys.exit(1)
        ws = sys.argv[1]; player = sys.argv[2]; op = sys.argv[3]
        if op == "list":
            for r in roots_list(ws, player):
                print(r)
        elif op in ("add", "remove"):
            if len(sys.argv) < 5:
                print(f"usage: taiko-trainer roots <ws> <player> {op} <path>", file=sys.stderr); sys.exit(1)
            msg = roots_add(ws, player, sys.argv[4]) if op == "add" else roots_remove(ws, player, sys.argv[4])
            print(msg)
        else:
            print(f"unknown roots op: {op}", file=sys.stderr); sys.exit(2)

    elif cmd == "refresh":
        from .ingest import refresh_ratings
        ws = sys.argv[1] if len(sys.argv) >= 2 else "."
        refresh_ratings(ws)

    elif cmd == "migrate":
        # `taiko-trainer migrate --workspace ... --server ... --token ... [--player X] [--dry-run]`
        # Delegates to migrate.main which parses its own args from sys.argv.
        # NOTE: line 44 above already stripped sys.argv[1] (the "migrate"
        # subcommand), so at this point sys.argv[1:] is already just the flags.
        # Setting sys.argv[0] to a descriptive name for nice error messages.
        from .migrate import main as migrate_main
        sys.argv = ["taiko-trainer migrate"] + sys.argv[1:]
        sys.exit(migrate_main())

    elif cmd == "cleanup":
        # `taiko-trainer cleanup --workspace <path> [--commit]`
        # Drops maps that no longer meet the ingest gate + their orphan replays.
        from .cleanup import main as cleanup_main
        sys.argv = ["taiko-trainer cleanup"] + sys.argv[1:]
        sys.exit(cleanup_main())

    elif cmd == "player":
        from .db import open_plays, upsert_player
        if len(sys.argv) < 4:
            print("usage: taiko-trainer player <ws> <player> <style> [notes]", file=sys.stderr); sys.exit(1)
        ws = sys.argv[1]; player = sys.argv[2]; style = sys.argv[3]
        notes = sys.argv[4] if len(sys.argv) >= 5 else None
        conn = open_plays(ws, player)
        upsert_player(conn, player, style, notes)
        conn.close()
        print(f"Player {player!r} registered with style={style}")

    elif cmd == "report":
        from .db import open_plays
        from .report import build_report, print_report
        if len(sys.argv) < 3:
            print("usage: taiko-trainer report <ws> <player>", file=sys.stderr); sys.exit(1)
        conn = open_plays(sys.argv[1], sys.argv[2])
        rep = build_report(conn)
        conn.close()
        if rep is None:
            print(f"ERROR: no snapshot for {sys.argv[2]!r}", file=sys.stderr); sys.exit(1)
        print_report(rep)

    elif cmd == "serve":
        from .server import serve
        ws = "."; host = "127.0.0.1"; port = 8000
        args = sys.argv[1:]; i = 0
        while i < len(args):
            if args[i] == "--ws" and i + 1 < len(args): ws = args[i + 1]; i += 2; continue
            if args[i] == "--host" and i + 1 < len(args): host = args[i + 1]; i += 2; continue
            if args[i] == "--port" and i + 1 < len(args): port = int(args[i + 1]); i += 2; continue
            i += 1
        serve(workspace=ws, host=host, port=port)

    elif cmd == "validate":
        from .validate import validate
        sys.exit(validate())

    elif cmd == "smoke":
        from .smoke import main as smoke_main
        sys.exit(smoke_main())

    else:
        print(f"unknown command: {cmd}", file=sys.stderr)
        print(_USAGE, end="", file=sys.stderr)
        sys.exit(2)

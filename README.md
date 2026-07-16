# taiko-trainer

A local osu!taiko training assistant. Parses maps and replays, produces a 5-D
map rating (`speed / stamina / gimmick / technical / consistency`), judges
replays against OD-scaled windows, classifies each miss by its likely cause,
detects KDDK cheese moments, aggregates a player skill vector, groups
replays into training sessions, and suggests maps to push your weakest
dimension.

Runs offline. Reads .osu and .osr files. Persists to two SQLite databases
per workspace: `catalog.db` (shared maps) and `<player>.db` (per-player plays).
Both are fully self-contained — the raw .osu and .osr file bytes are stored
as BLOBs, so a workspace directory is portable to any machine.

Ships with a **local web UI** (FastAPI) that you can point your browser at.

## Requirements

- Python 3.12+
- [uv](https://docs.astral.sh/uv/getting-started/installation/) (recommended)
- Git (for pulling updates)

## Install

```bash
git clone <your-repo-url> taiko-trainer
cd taiko-trainer
uv sync
```

That's it. `uv sync` reads `pyproject.toml` + `uv.lock`, creates a virtualenv,
installs dependencies.

## First-time setup

Pick a workspace directory (e.g. `~/taiko`). Point the trainer at your osu!
Songs folder so replays can find their maps automatically. Register yourself:

```bash
mkdir -p ~/taiko
cd ~/taiko

# Point at your Songs folder so replays can resolve their maps
uv run taiko-trainer roots ~/taiko YourName add "C:/Users/you/AppData/Local/osu!/Songs"
#  (or ~/Library/Application Support/osu/Songs on macOS,
#   ~/.local/share/osu/Songs on Linux)

# Register your playstyle (kddk = outer=kat, inner=don; ddkk / kkdd for color-per-hand)
uv run taiko-trainer player ~/taiko YourName kddk

# Add a replay — the trainer resolves the map from your Songs folder automatically
uv run taiko-trainer add ~/taiko "path/to/replay.osr"

# Start the web UI
uv run taiko-trainer serve --ws ~/taiko
# then open http://localhost:8000 in your browser
```

Every subsequent replay is one command:

```bash
uv run taiko-trainer add ~/taiko "path/to/new-replay.osr"
```

## Web UI

```bash
uv run taiko-trainer serve --ws ~/taiko
# defaults: --host 127.0.0.1 --port 8000
```

Pages:
- `/` — home: workspace status, players list, map roots, upload drop-zone
- `/player/<name>` — training report: skill vector, dominant miss causes,
  session comparison, suggested maps
- `/replay/<player>/<id>` — one replay's judgment + classification breakdown

Drop a .osr on the home page and it runs through the full pipeline (map
resolution → judgment → classification → snapshot update) then redirects
you to the updated report.

## CLI reference

Every command takes a workspace path as its first arg. Defaults to `.` when
omitted, so you can `cd` into your workspace and drop the arg.

```
status <ws>                     workspace overview
ingest <ws> <root>              bulk ingest all .osu/.osr under a root
add <ws> <replay> [--map <osu>] add ONE replay, auto-resolving its map
add-map <ws> <osu>              add a single .osu to the catalog
roots <ws> <player> add|remove|list <path>
                                per-player map search roots
refresh <ws>                    re-parse stored blobs + recompute all ratings
                                (run this after pulling a new version)
player <ws> <player> <style> [notes]
                                register or update a player's playstyle
report <ws> <player>            training report in the terminal
validate                        verify reference-map diagonals still pass
serve [--ws <path>] [--host <h>] [--port <p>]
                                start the local web UI
```

## What lives where

```
<workspace>/
├── catalog.db        1-few MB — shared map catalog (parsed .osu content as BLOBs)
├── <player>.db       hundreds of KB — one file per player (their .osr blobs + judged stats)
└── ...
```

Everything is portable. Copy the workspace directory anywhere and it works.

## Updating

```bash
cd taiko-trainer
git pull
uv sync                       # in case dependencies changed
uv run taiko-trainer refresh ~/taiko   # re-rate all cached maps with the new formulas
```

The `refresh` step re-parses every stored .osu BLOB and re-computes its
rating. Your play history is untouched — only the cached ratings and derived
skill snapshots update.

## Playstyles

- **kddk** (default): outer keys = kats, inner = dons. Every note alternates hands (L-R-L-R).
- **ddkk**: left hand plays all dons, right hand plays all kats. Color-per-hand.
- **kkdd**: mirror of ddkk (left = kats).
- **unknown**: no playstyle set. Cheese rate interpretation is best-guess.

The playstyle affects how the "cheese rate" metric is interpreted (KDDK
players never do same-hand consecutive hits at fast tempo; DDKK players
naturally do same-hand on mono-color runs).

## License

Personal use. Not affiliated with osu! or peppy.

# taiko-trainer

An osu!taiko training assistant that ranks your maps and replays on **six
skill dimensions** — speed, stamina, gimmick, technical, consistency,
reading — then tells you which dimension is holding you back and which
specific maps would push it forward.

Ships in two shapes:

- **Hosted** (recommended): use the public instance at
  <https://taiko.umaladder.moe>. Log in with your osu! account, install the
  uploader companion, and every replay you export uploads automatically.
- **Local**: clone the repo, run the CLI + local web UI against your own
  `.osu` + `.osr` files. No network, no accounts. Same scoring code as the
  hosted instance.

## Using the hosted instance

1. **Log in** at <https://taiko.umaladder.moe> with osu! OAuth.
2. **Install the uploader companion** — big button on
   <https://taiko.umaladder.moe/download>. Windows only for now, ~2.4 MB
   NSIS installer, auto-updates.
3. **Paste your uploader token** from `/settings/tokens` into the app,
   confirm your Replays folder, hit Save.
4. **Play**. On the results screen press **F2** to export the replay
   (`.osr`) — the uploader picks it up seconds later, uploads it, and
   pops a toast showing what skill you gained.

That's the whole loop. Your report at `/u/<your-username>` updates in
place; `/leaderboards` ranks everyone with a public profile.

## Using it locally

You'll want this if you're offline, on Linux, or hacking on the scoring
model.

```bash
git clone https://github.com/Acrith/osu-taiko-trainer
cd osu-taiko-trainer
uv sync                      # requires uv — https://docs.astral.sh/uv/
```

Pick a workspace directory and point it at your osu! Songs folder so
replays can find their maps automatically:

```bash
mkdir -p ~/taiko
uv run taiko-trainer roots ~/taiko YourName add "C:/Users/you/AppData/Local/osu!/Songs"
uv run taiko-trainer player ~/taiko YourName kddk    # or ddkk / kkdd
uv run taiko-trainer add ~/taiko "path/to/replay.osr"
uv run taiko-trainer serve --ws ~/taiko              # opens http://localhost:8000
```

Every subsequent replay is one command:

```bash
uv run taiko-trainer add ~/taiko "path/to/new-replay.osr"
```

### CLI reference

Every command takes a workspace path as its first arg. Defaults to `.`
when omitted, so you can `cd` into your workspace and drop the arg.

```
status [<ws>]                       workspace overview
ingest <ws> <root>                  bulk ingest all .osu/.osr under a root
add <ws> <replay> [--map <osu>]     add ONE replay, auto-resolving its map
add-map <ws> <osu>                  add a single .osu to the catalog
roots <ws> <player> add|remove|list <path>
                                    per-player map search roots
refresh <ws>                        re-parse stored blobs + recompute all ratings
                                    (run this after pulling a new version)
migrate --workspace <ws> --server <url> --token <t> [--player <name>] [--dry-run]
                                    push a local workspace to a hosted instance
cleanup --workspace <ws> [--commit] drop maps that fail the ingest gate + orphan replays
                                    (dry-run by default; --commit to apply)
scan-lazer --workspace <ws> [--commit]
                                    find (and optionally delete) lazer replays with
                                    custom-rate DT/HT that predate the ingest gate
player <ws> <player> <style> [notes]
                                    register or update a player's playstyle
report <ws> <player>                training report in the terminal
validate                            verify reference-map diagonals still pass
serve [--ws <path>] [--host <h>] [--port <p>]
                                    start the local web UI
```

### Updating a local install

```bash
git pull
uv sync
uv run taiko-trainer refresh <workspace>   # re-rate cached maps + snapshots
```

`refresh` re-parses every stored `.osu` blob and re-computes its rating
against the new formulas. Play history is untouched — only cached
ratings and the derived skill snapshots update.

## Playstyles

- **kddk** (default): outer keys = kats, inner = dons. Every note
  alternates hands (L-R-L-R). Most common competitive style.
- **ddkk**: left hand plays all dons, right hand plays all kats.
  Color-per-hand.
- **kkdd**: mirror of ddkk (left = kats).
- **unknown**: no playstyle set. Cheese rate interpretation is best-guess.

Playstyle affects the "stamina" and "consistency" dimensions (KDDK
players never do same-hand consecutive hits at fast tempo; DDKK players
naturally do same-hand on mono-color runs).

## Workspace layout

Whether hosted or local, one workspace directory holds:

```
<workspace>/
├── catalog.db        1–few MB — shared map catalog (parsed .osu blobs)
├── <player>.db       hundreds of KB — per-player plays + snapshots
└── ...
```

Everything is portable. Copy the workspace directory anywhere and it
works. The hosted instance uses the same shape server-side.

## More docs

- **`uploader-app/README.md`** — Tauri companion internals + release flow
- **`ARCHITECTURE.md`** — hosted service data model, auth, routes,
  scoring pipeline
- **`DEPLOY.md`** — running your own hosted instance from scratch

## License

Personal use. Not affiliated with osu! or peppy.

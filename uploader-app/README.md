# taiko-trainer uploader

Windows companion app that watches your osu! Replays folder and posts new
`.osr` files to <https://taiko.umaladder.moe>. Auto-updates itself, shows
your live skill rating, and lets you selectively backfill older replays.

Public download page: <https://taiko.umaladder.moe/download>.

## Architecture

- **Frontend**: Svelte 5 + Vite under `src/`. Matches the site's dark
  palette so both surfaces feel like the same product.
- **Backend**: Rust via Tauri 2 under `src-tauri/`. Handles filesystem
  (folder watching via `notify`, reading `.osr` bytes), local SQLite
  state DB (`rusqlite`, bundled), TOML config, and the HTTP upload loop
  (`reqwest` with rustls).
- **Bridge**: Tauri commands (`invoke("name", args)` from JS → Rust
  `#[tauri::command]` handlers) plus events (`app.emit`/`listen` for
  pushed status updates).

Rust modules:

| File                  | Role                                          |
|-----------------------|-----------------------------------------------|
| `config.rs`           | TOML config load/save + folder auto-detect    |
| `state.rs`            | SQLite tracker (which files uploaded already) |
| `folder.rs`           | Scan replays folder + join with state DB      |
| `http.rs`             | reqwest calls (`/replays`, `/whoami`, `/me/*`) |
| `watcher.rs`          | `notify` bridge to async channel               |
| `worker.rs`           | Background task tying everything together     |
| `commands.rs`         | Tauri command handlers exposed to JS          |
| `logging.rs`          | File logger + panic hook                       |
| `lib.rs`              | Tauri Builder setup, plugin registration      |

## Dev setup

Requires:
- Rust stable (`rustup` — <https://rustup.rs>)
- Node.js 20+
- Windows: WebView2 runtime (pre-installed on Win10 21H2+ / Win11)

```bash
cd uploader-app
npm install
npm run tauri dev
```

Hot-reload frontend, Rust rebuilds on change. Config + state DB live in
`~/.taiko-trainer/` on all platforms (matches the Python CLI's location
so state is shared if you also run `taiko-uploader` headlessly).

## File layout on disk

The app writes three files, all in `~/.taiko-trainer/`:

| File               | What it is                                       |
|--------------------|--------------------------------------------------|
| `uploader.toml`    | Config: token, replays folder, server URL        |
| `uploader.state.db`| SQLite of which `.osr` filenames uploaded when   |
| `uploader.log`     | Timestamped log — panics + worker transitions    |

On Windows that's `C:\Users\<you>\.taiko-trainer\`.

## Releases + auto-updater

Cutting a new version is:

```bash
# From any machine with push access
git tag uploader-v0.3.0
git push origin uploader-v0.3.0
```

The `build-uploader-tauri.yml` workflow triggers on `uploader-v*` tags:

1. **Sync version from tag** step rewrites `Cargo.toml` + `tauri.conf.json`
   to match the tag. No manual bumping needed (though you can — it's the
   dev-mode display value).
2. `tauri build` produces the NSIS installer, MSI, portable exe, and
   `.sig` signature files (requires `TAURI_SIGNING_PRIVATE_KEY` env
   from repo secret — see below).
3. PowerShell step generates `latest.json` — the updater manifest
   pointing at the new installer + carrying its detached signature.
4. All artifacts uploaded to a new GitHub Release.

Installed clients check `github.com/Acrith/osu-taiko-trainer/releases/latest/download/latest.json`
on every launch (3s after mount). If a newer version exists, they
verify its signature against the pubkey baked into `tauri.conf.json`,
prompt the user, then run the NSIS installer inline.

### Signing keys

The auto-updater refuses to install anything not signed by the private
key matching the pubkey in `tauri.conf.json`. Setup was one-time:

```bash
npx @tauri-apps/cli signer generate --password ""
# private key → GitHub repo secret TAURI_SIGNING_PRIVATE_KEY
# public key → pasted into src-tauri/tauri.conf.json plugins.updater.pubkey
```

**Don't rotate the pubkey.** Every existing install has the old pubkey
baked in — if you regenerate, all existing clients stop being able to
auto-update and have to manually download a new build.

## Testing the release flow

Bump the version (or let CI auto-sync from tag), push a tag:

```bash
git tag uploader-v0.3.0-shakeout
git push origin uploader-v0.3.0-shakeout
```

Wait ~6 min for CI to publish the release. Relaunch your installed
build; within 3s the update prompt fires. Install & restart, verify
sidebar/About show the new version, remove the shakeout tag if you
don't want it in the release list.

## Config editing without the UI

If the app can't start (missing dep, corrupt config, etc.), you can
edit `~/.taiko-trainer/uploader.toml` directly:

```toml
api_token       = "tt_uploader_XXXXX…"
replays_folder  = "D:/osu!/Replays"
server_url      = "https://taiko.umaladder.moe"
poll_interval_s = 60
```

`server_url` is only written when it differs from the shipped default,
so a normal user's file has 3 lines.

The state DB is safe to delete — deleting it makes the app treat every
`.osr` in the folder as new; the app snapshots them as `SKIPPED_HISTORIC`
on next launch and the watcher only touches genuinely-new plays after
that (matching first-run behavior).

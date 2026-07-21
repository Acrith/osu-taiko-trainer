# taiko-trainer uploader — Tauri edition

Windows companion app that watches your osu! replays folder and uploads new
`.osr` files to <https://taiko.umaladder.moe>. Replaces the tkinter PyInstaller
build (`packaging/`) — that path is deprecated and will be removed once this
one is proven.

## Architecture

- **Frontend**: Svelte 5 + Vite. Lives under `src/`. Renders the UI, matches
  the site's dark palette from `taiko.umaladder.moe` so the two feel like the
  same product.
- **Backend**: Rust via Tauri 2. Lives under `src-tauri/`. Handles the
  filesystem (folder watching, reading `.osr` files), local SQLite state DB,
  TOML config, and the HTTP upload loop against the server.
- **Bridge**: Tauri commands (`invoke("cmd_name", args)` from JS → Rust
  `#[tauri::command]` handlers) plus events (`emit`/`listen` for pushed
  updates like new-upload notifications).

Why Tauri: smaller `.exe` than PyInstaller (~10 MB vs ~60 MB), native
performance, real dark theme via CSS, free auto-update via GitHub Releases
integration, and the frontend can reuse the site's CSS directly.

## Dev setup

Requires:
- Rust stable (`rustup` — <https://rustup.rs>)
- Node.js 20+ (`nvm` or from <https://nodejs.org>)
- On Windows: WebView2 runtime (pre-installed on Win10 21H2+ and Win11)

Install JS deps:

    cd uploader-app
    npm install

Run in dev mode (hot-reload frontend + Rust rebuilds on change):

    npm run tauri dev

Build a release exe locally (produces `src-tauri/target/release/taiko-uploader.exe`):

    npm run tauri build

## Config file layout

Same TOML format as the tkinter build — lives at
`%APPDATA%\taiko-trainer\uploader.toml` on Windows:

```toml
api_token = "tt_uploader_XXXXX…"
replays_folder = "D:\\osu!\\Data\\Replays"
# server_url is hardcoded in the binary; only technical users touch this
server_url = "https://taiko.umaladder.moe"
```

State DB sits next to it as `uploader.db3`.

Log file: `%APPDATA%\taiko-trainer\uploader.log`

## CI

`.github/workflows/build-uploader-tauri.yml` — dispatches to `windows-latest`
with a Rust + Node.js toolchain, runs `npm run tauri build`, uploads the exe
+ MSI/NSIS installers as workflow artifacts, and (on tag push matching
`uploader-v*`) attaches them to a GitHub Release.

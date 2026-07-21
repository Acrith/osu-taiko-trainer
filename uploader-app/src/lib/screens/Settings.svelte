<script>
  import { invoke } from "@tauri-apps/api/core";
  import { open as openPicker } from "@tauri-apps/plugin-dialog";
  import { openUrl, openPath } from "@tauri-apps/plugin-opener";
  import { config, defaultServerUrl } from "../stores.js";

  let cfg = $state(null);
  let srvDefault = $state("https://taiko.umaladder.moe");
  config.subscribe(v => (cfg = v));
  defaultServerUrl.subscribe(v => (srvDefault = v));

  // Working copy — bound to inputs. Committed to disk on Save.
  let form = $state({
    api_token: "",
    replays_folder: "",
    server_url: "",
    poll_interval_s: 60,
  });
  let hydrated = $state(false);
  let saveMsg = $state(null);
  let saving = $state(false);

  // Populate the form the FIRST time cfg lands. Later store updates
  // don't clobber user edits.
  $effect(() => {
    if (cfg && !hydrated) {
      form.api_token = cfg.api_token;
      form.replays_folder = cfg.replays_folder;
      form.server_url = cfg.server_url;
      form.poll_interval_s = cfg.poll_interval_s;
      hydrated = true;
    }
  });

  async function pickFolder() {
    const chosen = await openPicker({
      directory: true,
      multiple: false,
      title: "Pick your osu! replays folder",
    });
    if (typeof chosen === "string") form.replays_folder = chosen;
  }

  // --- Diagnostics section state ------------------------------------------
  let testResult = $state(null);
  let testing    = $state(false);
  let restartMsg = $state(null);
  let restarting = $state(false);
  let logOpen    = $state(false);
  let logText    = $state("");
  let logLoading = $state(false);

  async function testConnection() {
    testing = true;
    testResult = null;
    try {
      testResult = await invoke("test_connection");
    } catch (e) {
      testResult = { ok: false, kind: "error", message: String(e) };
    } finally {
      testing = false;
    }
  }

  async function restartWatcher() {
    restarting = true;
    restartMsg = null;
    try {
      await invoke("restart_watcher");
      restartMsg = { level: "ok", text: "Watcher restart signal sent. See Home for status." };
    } catch (e) {
      restartMsg = { level: "err", text: String(e) };
    } finally {
      restarting = false;
    }
  }

  async function toggleLog() {
    if (logOpen) { logOpen = false; return; }
    logOpen = true;
    await refreshLog();
  }

  async function refreshLog() {
    logLoading = true;
    try {
      logText = await invoke("read_log", { lines: 500 });
    } catch (e) {
      logText = `Failed to read log: ${e}`;
    } finally {
      logLoading = false;
    }
  }

  async function copyLog() {
    try {
      await navigator.clipboard.writeText(logText);
      restartMsg = { level: "ok", text: "Log copied to clipboard." };
    } catch {
      restartMsg = { level: "err", text: "Clipboard access failed — select + Ctrl+C manually." };
    }
  }

  async function openLogFolder() {
    try {
      const p = await invoke("log_folder_path");
      await openPath(p);
    } catch (e) {
      restartMsg = { level: "err", text: `Couldn't open log folder: ${e}` };
    }
  }

  async function detect() {
    const guess = await invoke("detect_replays_folder");
    if (guess) form.replays_folder = guess;
    else saveMsg = { level: "warn", text: "Couldn't auto-detect. Pick it manually." };
  }

  async function save() {
    saving = true;
    saveMsg = null;
    try {
      await invoke("save_config", {
        cfg: {
          api_token: form.api_token.trim(),
          replays_folder: form.replays_folder.trim(),
          server_url: (form.server_url || srvDefault).trim().replace(/\/$/, ""),
          poll_interval_s: Number(form.poll_interval_s) || 60,
        },
      });
      saveMsg = { level: "ok", text: "Saved. Worker restarting with new config." };
      // Re-fetch whoami since token might have changed.
      invoke("fetch_whoami").catch(() => {});
    } catch (e) {
      saveMsg = { level: "err", text: String(e) };
    } finally {
      saving = false;
    }
  }

  async function backfill() {
    saveMsg = null;
    try {
      await invoke("backfill");
      saveMsg = { level: "ok", text: "Backfill queued — Uploads screen will fill in as files upload." };
    } catch (e) {
      saveMsg = { level: "err", text: String(e) };
    }
  }
</script>

<div class="page">
  <div class="eyebrow">Settings</div>
  <h1 class="title">Config</h1>

  <div class="field">
    <label class="k" for="tok">Uploader token</label>
    <input
      id="tok"
      class="v mono"
      type="password"
      bind:value={form.api_token}
      placeholder="tt_uploader_…"
      autocomplete="off"
    />
    <div class="hint">
      Mint one at
      <button class="linkbtn" onclick={() => openUrl(`${srvDefault}/settings/tokens`)}
      >{srvDefault}/settings/tokens</button>.
    </div>
    <div class="warn">
      <b>Not</b> your osu! API key — this is a token issued by
      taiko-trainer specifically for this uploader. Don't share, screenshot,
      or export it: anyone with this token can upload replays as you.
      Revoke + regenerate on the site if it leaks.
    </div>
  </div>

  <div class="field">
    <label class="k" for="folder">Replays folder</label>
    <div class="folder-row">
      <input
        id="folder"
        class="v mono"
        type="text"
        bind:value={form.replays_folder}
        placeholder="C:\Users\…\AppData\Local\osu!\Data\r"
      />
      <button class="btn ghost" onclick={pickFolder}>Browse…</button>
      <button class="btn ghost" onclick={detect}>Auto-detect</button>
    </div>
  </div>

  <div class="field advanced">
    <label class="k" for="srv">Server URL <span class="k-tag mono">advanced</span></label>
    <input
      id="srv"
      class="v mono"
      type="text"
      bind:value={form.server_url}
      placeholder={srvDefault}
    />
    <div class="hint">
      Blank uses the default: <b>{srvDefault}</b>.
    </div>
    <div class="warn warn-light">
      Only change this if you're running the taiko-trainer server yourself
      (localhost development). Pointing it at any other URL sends your
      replays somewhere the uploader wasn't designed for, and the token
      won't authenticate anywhere else — so you'd just see auth errors.
    </div>
  </div>

  <div class="field">
    <label class="k" for="poll">Poll interval (s)</label>
    <input
      id="poll"
      class="v mono narrow"
      type="number"
      min="10"
      max="600"
      bind:value={form.poll_interval_s}
    />
    <div class="hint">Fallback scan cadence in case the OS drops file-watcher events.</div>
  </div>

  <div class="actions">
    <button class="btn primary" onclick={save} disabled={saving}>
      {saving ? "Saving…" : "Save"}
    </button>
    <button class="btn" onclick={backfill}>Backfill folder</button>
  </div>

  {#if saveMsg}
    <div class="msg msg-{saveMsg.level}">{saveMsg.text}</div>
  {/if}

  <!-- Diagnostics: connection test, watcher control, log viewer.
       Sits below the config so someone reporting a bug has one place to
       generate the info we need: hit Test → copy the result, hit Show
       log → Copy log → paste both. -->
  <h2 class="section-title">Diagnostics</h2>

  <div class="diag-row">
    <button class="btn" onclick={testConnection} disabled={testing}>
      {testing ? "Testing…" : "Test connection"}
    </button>
    <button class="btn" onclick={restartWatcher} disabled={restarting}>
      {restarting ? "Restarting…" : "Restart watcher"}
    </button>
    <button class="btn" onclick={toggleLog}>
      {logOpen ? "Hide log" : "Show log"}
    </button>
    <button class="btn ghost" onclick={openLogFolder}>Open folder</button>
  </div>

  {#if testResult}
    <div class="msg msg-{testResult.ok ? 'ok' : 'err'}">{testResult.message}</div>
  {/if}
  {#if restartMsg}
    <div class="msg msg-{restartMsg.level}">{restartMsg.text}</div>
  {/if}

  {#if logOpen}
    <div class="log-panel">
      <div class="log-head">
        <span class="eyebrow">uploader.log · last 500 lines</span>
        <div class="log-actions">
          <button class="btn ghost small" onclick={refreshLog} disabled={logLoading}>
            {logLoading ? "…" : "Refresh"}
          </button>
          <button class="btn ghost small" onclick={copyLog}>Copy</button>
        </div>
      </div>
      <pre class="log-body">{logText || "(empty)"}</pre>
    </div>
  {/if}
</div>

<style>
  .page { max-width: 720px; }
  .title {
    font-family: var(--font-mono);
    font-weight: 500;
    font-size: 24px;
    margin: 4px 0 20px 0;
    color: var(--ink);
  }
  .field {
    display: block;
    padding: 12px 0;
    border-bottom: 1px solid var(--rule);
  }
  .k {
    display: block;
    font-family: var(--font-mono);
    font-size: 10px;
    letter-spacing: 0.14em;
    text-transform: uppercase;
    color: var(--ink-faint);
    margin-bottom: 6px;
  }
  .v {
    width: 100%;
    background: var(--panel);
    border: 1px solid var(--rule);
    color: var(--ink);
    padding: 8px 10px;
    border-radius: 4px;
    font-family: var(--font-mono);
    font-size: 13px;
  }
  .v.narrow { width: 120px; }
  .v:focus { outline: 1px solid var(--accent); }
  .folder-row {
    display: flex;
    gap: 8px;
    align-items: stretch;
  }
  .folder-row .v { flex: 1; }
  .hint {
    font-size: 12px;
    color: var(--ink-muted);
    margin-top: 6px;
    line-height: 1.5;
  }
  .actions {
    display: flex;
    gap: 8px;
    margin-top: 20px;
  }
  .btn {
    font-family: var(--font-mono);
    font-size: 11px;
    letter-spacing: 0.1em;
    text-transform: uppercase;
    background: var(--panel);
    color: var(--ink);
    border: 1px solid var(--rule);
    padding: 8px 14px;
    border-radius: 4px;
    cursor: pointer;
    transition: background 0.08s ease;
  }
  .btn:hover { background: color-mix(in oklab, var(--ink) 8%, var(--panel)); }
  .btn:disabled { opacity: 0.5; cursor: default; }
  .btn.primary {
    background: var(--accent);
    color: white;
    border-color: var(--accent);
  }
  .btn.primary:hover { background: color-mix(in oklab, black 8%, var(--accent)); }
  .btn.ghost { background: transparent; }
  .linkbtn {
    background: none;
    border: none;
    padding: 0;
    color: var(--accent);
    font-family: var(--font-mono);
    font-size: inherit;
    cursor: pointer;
    text-decoration: none;
  }
  .linkbtn:hover { text-decoration: underline; }

  .warn {
    background: var(--accent-faint);
    border: 1px solid var(--accent-soft);
    color: var(--ink);
    padding: 10px 12px;
    border-radius: 4px;
    font-size: 12px;
    line-height: 1.55;
    margin-top: 10px;
  }
  .warn b { color: var(--accent); }
  .warn-light {
    background: color-mix(in oklab, var(--ok) 12%, transparent);
    border-color: color-mix(in oklab, var(--ok) 30%, transparent);
  }
  .warn-light b { color: var(--ok); }
  .k-tag {
    background: color-mix(in oklab, var(--ink-faint) 20%, transparent);
    color: var(--ink-muted);
    padding: 1px 6px;
    border-radius: 3px;
    font-size: 9px;
    letter-spacing: 0.14em;
    margin-left: 8px;
    vertical-align: 1px;
  }

  .section-title {
    font-family: var(--font-mono);
    font-weight: 500;
    font-size: 14px;
    letter-spacing: 0.06em;
    color: var(--ink);
    margin: 32px 0 10px 0;
    padding-top: 20px;
    border-top: 1px solid var(--rule);
  }
  .diag-row {
    display: flex;
    gap: 8px;
    flex-wrap: wrap;
    margin-bottom: 8px;
  }
  .btn.small { padding: 4px 10px; font-size: 10px; }

  .log-panel {
    margin-top: 16px;
    border: 1px solid var(--rule);
    border-radius: 4px;
    background: var(--panel);
    overflow: hidden;
  }
  .log-head {
    display: flex;
    justify-content: space-between;
    align-items: center;
    padding: 8px 12px;
    border-bottom: 1px solid var(--rule);
  }
  .log-actions {
    display: flex;
    gap: 4px;
  }
  .log-body {
    margin: 0;
    padding: 12px;
    max-height: 360px;
    overflow: auto;
    background: color-mix(in oklab, black 20%, var(--panel));
    color: var(--ink);
    font-family: var(--font-mono);
    font-size: 11px;
    line-height: 1.5;
    white-space: pre-wrap;
    word-break: break-word;
    /* User needs to be able to select the text — override the app-wide
       user-select:none from app.css that prevents accidental drag-select
       on the rest of the UI. */
    user-select: text;
    -webkit-user-select: text;
  }
  .msg {
    margin-top: 16px;
    padding: 10px 14px;
    border-radius: 4px;
    font-family: var(--font-mono);
    font-size: 12px;
  }
  .msg-ok   { background: color-mix(in oklab, var(--great) 15%, transparent); color: var(--great); }
  .msg-warn { background: color-mix(in oklab, var(--ok) 15%, transparent); color: var(--ok); }
  .msg-err  { background: var(--accent-faint); color: var(--accent); }
</style>

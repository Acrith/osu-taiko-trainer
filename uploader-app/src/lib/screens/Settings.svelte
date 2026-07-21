<script>
  import { invoke } from "@tauri-apps/api/core";
  import { open } from "@tauri-apps/plugin-dialog";
  import { openUrl } from "@tauri-apps/plugin-opener";
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
    const chosen = await open({
      directory: true,
      multiple: false,
      title: "Pick your osu! replays folder",
    });
    if (typeof chosen === "string") form.replays_folder = chosen;
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
    <label class="k" for="tok">API token</label>
    <input
      id="tok"
      class="v mono"
      type="password"
      bind:value={form.api_token}
      placeholder="tt_uploader_…"
      autocomplete="off"
    />
    <div class="hint">Get one at
      <button class="linkbtn" onclick={() => openUrl(`${srvDefault}/settings/tokens`)}
      >{srvDefault}/settings/tokens</button>.
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

  <div class="field">
    <label class="k" for="srv">Server URL</label>
    <input
      id="srv"
      class="v mono"
      type="text"
      bind:value={form.server_url}
      placeholder={srvDefault}
    />
    <div class="hint">Leave blank to use the default ({srvDefault}). Change only for local development.</div>
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

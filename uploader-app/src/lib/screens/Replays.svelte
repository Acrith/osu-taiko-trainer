<script>
  import { onMount } from "svelte";
  import { invoke } from "@tauri-apps/api/core";
  import { openUrl } from "@tauri-apps/plugin-opener";
  import { listen } from "@tauri-apps/api/event";
  import { config, myReplays } from "../stores.js";

  let entries = $state([]);
  let loading = $state(false);
  let error = $state(null);

  let search = $state("");
  let stateFilter = $state("not_uploaded");
  let selection = $state(new Set());
  let uploading = $state(false);

  let cfg = $state(null);
  config.subscribe(v => (cfg = v));

  let serverReplays = $state(null);
  myReplays.subscribe(v => (serverReplays = v));

  // content_hash → server replay row. When we scan a local file, we look
  // it up here — if it exists, the file is definitively on the server
  // regardless of what the local state DB thinks.
  const serverByHash = $derived.by(() => {
    const m = new Map();
    for (const r of (serverReplays?.replays ?? [])) {
      if (r.content_hash) m.set(r.content_hash, r);
    }
    return m;
  });

  const STATE_LABELS = {
    never_seen: "New",
    historic:   "Historic",
    uploaded:   "Uploaded",
    skipped:    "Skipped",
  };

  // Merge server + local classifications into the final state we display.
  // A file that's on the server is always shown as "uploaded", even if
  // the local state DB thinks it's SKIPPED_HISTORIC.
  function classify(entry) {
    if (entry.content_hash && serverByHash.has(entry.content_hash)) {
      return "uploaded";
    }
    return entry.state;
  }
  function serverMatch(entry) {
    if (!entry.content_hash) return null;
    return serverByHash.get(entry.content_hash) ?? null;
  }

  const filtered = $derived.by(() => {
    const q = search.trim().toLowerCase();
    return entries.map(e => {
      const cls = classify(e);
      const sm = serverMatch(e);
      return {
        ...e,
        display_state: cls,
        // Prefer server-side map data when we have a match, so the Map
        // column populates even for locally-historic rows.
        display_title: sm?.map_title ?? e.map_title,
        display_mods:  sm?.mods ?? e.mods,
        display_acc:   sm?.accuracy ?? e.accuracy,
        replay_id:     sm?.id ?? e.replay_id,
      };
    }).filter(e => {
      if (stateFilter === "not_uploaded") {
        if (e.display_state === "uploaded") return false;
      } else if (stateFilter !== "all" && e.display_state !== stateFilter) {
        return false;
      }
      if (!q) return true;
      const hay = `${e.filename} ${e.display_title ?? ""}`.toLowerCase();
      return hay.includes(q);
    });
  });

  const selectedCount = $derived(
    filtered.filter(e => selection.has(e.filename)).length
  );
  const allFilteredSelected = $derived(
    filtered.length > 0 && filtered.every(e => selection.has(e.filename))
  );

  async function refresh() {
    loading = true;
    error = null;
    try {
      entries = await invoke("list_folder_entries");
      const alive = new Set(entries.map(e => e.filename));
      selection = new Set([...selection].filter(f => alive.has(f)));
      // Also refresh the server-side list — the user might have uploaded
      // through the web UI in another tab.
      invoke("fetch_my_replays").then(r => myReplays.set(r)).catch(() => {});
    } catch (e) {
      error = String(e);
    } finally {
      loading = false;
    }
  }

  onMount(async () => {
    await refresh();
    // Debounce refreshes triggered by upload completions so we don't
    // rescan the folder 10 times during a big backfill run.
    let pending = null;
    const unlisten = await listen("activity-added", () => {
      clearTimeout(pending);
      pending = setTimeout(() => { refresh(); }, 500);
    });
    return () => { clearTimeout(pending); unlisten(); };
  });

  function toggle(name) {
    const next = new Set(selection);
    if (next.has(name)) next.delete(name); else next.add(name);
    selection = next;
  }
  function toggleAllVisible() {
    const next = new Set(selection);
    if (allFilteredSelected) {
      for (const e of filtered) next.delete(e.filename);
    } else {
      for (const e of filtered) next.add(e.filename);
    }
    selection = next;
  }
  async function uploadSelected() {
    if (selection.size === 0 || uploading) return;
    uploading = true;
    error = null;
    try {
      const filenames = [...selection];
      await invoke("upload_files", { filenames });
    } catch (e) {
      error = String(e);
    } finally {
      uploading = false;
    }
  }
  function clearSelection() { selection = new Set(); }

  function relTime(iso) {
    if (!iso) return "—";
    const t = new Date(iso).getTime();
    if (isNaN(t)) return "—";
    const secs = (Date.now() - t) / 1000;
    if (secs < 60) return `${Math.round(secs)}s`;
    if (secs < 3600) return `${Math.round(secs / 60)}m`;
    if (secs < 86400) return `${Math.round(secs / 3600)}h`;
    if (secs < 30 * 86400) return `${Math.round(secs / 86400)}d`;
    return new Date(iso).toISOString().slice(0, 10);
  }
  function fmtSize(n) {
    if (n < 1024) return `${n}B`;
    if (n < 1024 * 1024) return `${Math.round(n / 1024)}KB`;
    return `${(n / (1024 * 1024)).toFixed(1)}MB`;
  }

  function replayUrl(row) {
    if (!row.replay_id || !cfg?.server_url || !serverReplays?.username) return null;
    return `${cfg.server_url}/replay/${serverReplays.username}/${row.replay_id}`;
  }
  function openReplay(row) {
    const u = replayUrl(row);
    if (u) openUrl(u);
  }
</script>

<div class="page">
  <div class="eyebrow">Replays</div>
  <div class="head">
    <h1 class="title">Files in your replays folder</h1>
    <button class="btn" onclick={refresh} disabled={loading}>
      {loading ? "Scanning…" : "Refresh"}
    </button>
  </div>

  <div class="controls">
    <input
      class="search mono"
      type="text"
      placeholder="Filter by filename or map title…"
      bind:value={search}
    />
    <div class="filter-group">
      {#each [
        { k: "not_uploaded", label: "Not uploaded" },
        { k: "never_seen",   label: "New" },
        { k: "historic",     label: "Historic" },
        { k: "skipped",      label: "Skipped" },
        { k: "uploaded",     label: "Uploaded" },
        { k: "all",          label: "All" },
      ] as f (f.k)}
        <button
          class="chip"
          class:active={stateFilter === f.k}
          onclick={() => (stateFilter = f.k)}
        >{f.label}</button>
      {/each}
    </div>
  </div>

  <div class="toolbar">
    <div class="count mono">
      {filtered.length} shown · {selectedCount} selected
    </div>
    <div class="actions">
      <button class="btn ghost" onclick={toggleAllVisible} disabled={filtered.length === 0}>
        {allFilteredSelected ? "Select none" : "Select all shown"}
      </button>
      <button class="btn ghost" onclick={clearSelection} disabled={selection.size === 0}>
        Clear
      </button>
      <button class="btn primary" onclick={uploadSelected}
              disabled={selection.size === 0 || uploading}>
        {uploading ? "Queued — see Home for progress" : `Upload ${selection.size || ""} selected`}
      </button>
    </div>
  </div>

  {#if error}
    <div class="err mono">{error}</div>
  {/if}

  <div class="table-wrap">
    <table class="folder-table">
      <thead>
        <tr>
          <th class="check-col">
            <input
              type="checkbox"
              checked={allFilteredSelected}
              onchange={toggleAllVisible}
            />
          </th>
          <th>Filename</th>
          <th>Modified</th>
          <th>Size</th>
          <th>Status</th>
          <th>Map</th>
        </tr>
      </thead>
      <tbody>
        {#if !loading && filtered.length === 0}
          <tr>
            <td colspan="6" class="empty">
              {entries.length === 0
                ? "No .osr files in the configured folder. Check Settings."
                : "No files match the current filter."}
            </td>
          </tr>
        {/if}
        {#each filtered as e (e.filename)}
          <tr class:selected={selection.has(e.filename)}>
            <td class="check-col">
              <input
                type="checkbox"
                checked={selection.has(e.filename)}
                onchange={() => toggle(e.filename)}
              />
            </td>
            <td class="mono filename">{e.filename}</td>
            <td class="mono muted">{relTime(e.modified_at)}</td>
            <td class="mono muted">{fmtSize(e.size_bytes)}</td>
            <td>
              <span class="pill pill-{e.display_state}">{STATE_LABELS[e.display_state] ?? e.display_state}</span>
            </td>
            <td class="map">
              {#if e.display_title && e.display_title !== "SKIPPED_HISTORIC"}
                {#if replayUrl(e)}
                  <button class="map-link mono" onclick={() => openReplay(e)}>
                    <span class="map-title">{e.display_title}</span>
                    <span class="link-hint mono">↗</span>
                  </button>
                {:else}
                  <span class="map-title">{e.display_title}</span>
                {/if}
                {#if e.display_mods && e.display_mods !== "NM"}
                  <span class="mods mono">+{e.display_mods}</span>
                {/if}
                {#if typeof e.display_acc === "number"}
                  <span class="acc mono">{(e.display_acc * 100).toFixed(2)}%</span>
                {/if}
              {:else}
                <span class="muted">—</span>
              {/if}
            </td>
          </tr>
        {/each}
      </tbody>
    </table>
  </div>
</div>

<style>
  .page { max-width: 100%; }
  .head {
    display: flex;
    justify-content: space-between;
    align-items: baseline;
    margin-bottom: 16px;
  }
  .title {
    font-family: var(--font-mono);
    font-weight: 500;
    font-size: 24px;
    margin: 4px 0;
    color: var(--ink);
  }

  .controls {
    display: flex;
    gap: 12px;
    margin-bottom: 12px;
    flex-wrap: wrap;
  }
  .search {
    flex: 1 1 260px;
    min-width: 200px;
    background: var(--panel);
    border: 1px solid var(--rule);
    border-radius: 4px;
    padding: 6px 10px;
    color: var(--ink);
    font-size: 12px;
  }
  .search:focus { outline: 1px solid var(--accent); }
  .filter-group { display: flex; gap: 4px; flex-wrap: wrap; }
  .chip {
    background: var(--panel);
    color: var(--ink-muted);
    border: 1px solid var(--rule);
    padding: 4px 10px;
    border-radius: 12px;
    font-family: var(--font-mono);
    font-size: 10px;
    letter-spacing: 0.1em;
    text-transform: uppercase;
    cursor: pointer;
  }
  .chip.active {
    background: var(--accent);
    border-color: var(--accent);
    color: white;
  }

  .toolbar {
    display: flex;
    justify-content: space-between;
    align-items: center;
    margin-bottom: 12px;
    gap: 12px;
  }
  .count {
    font-size: 11px;
    color: var(--ink-muted);
    letter-spacing: 0.06em;
  }
  .actions { display: flex; gap: 6px; flex-wrap: wrap; }
  .btn {
    font-family: var(--font-mono);
    font-size: 11px;
    letter-spacing: 0.1em;
    text-transform: uppercase;
    background: var(--panel);
    color: var(--ink);
    border: 1px solid var(--rule);
    padding: 6px 12px;
    border-radius: 4px;
    cursor: pointer;
  }
  .btn:hover { background: color-mix(in oklab, var(--ink) 6%, var(--panel)); }
  .btn:disabled { opacity: 0.4; cursor: default; }
  .btn.ghost { background: transparent; }
  .btn.primary {
    background: var(--accent);
    color: white;
    border-color: var(--accent);
  }
  .btn.primary:hover { background: color-mix(in oklab, black 10%, var(--accent)); }

  .err {
    background: var(--accent-faint);
    color: var(--accent);
    padding: 10px 12px;
    border-radius: 4px;
    font-size: 12px;
    margin-bottom: 12px;
  }

  .table-wrap {
    background: var(--panel);
    border: 1px solid var(--rule);
    border-radius: 6px;
    overflow: auto;
    max-height: calc(100vh - 260px);
  }
  .folder-table {
    width: 100%;
    border-collapse: separate;
    border-spacing: 0;
    font-family: var(--font-mono);
    font-size: 12px;
    font-variant-numeric: tabular-nums;
  }
  th {
    text-align: left;
    padding: 8px 10px;
    font-size: 10px;
    letter-spacing: 0.14em;
    text-transform: uppercase;
    color: var(--ink-faint);
    border-bottom: 1px solid var(--rule);
    background: var(--panel);
    position: sticky;
    top: 0;
    z-index: 1;
  }
  td {
    padding: 8px 10px;
    border-bottom: 1px solid var(--rule);
    color: var(--ink);
    vertical-align: middle;
  }
  tr:last-child td { border-bottom: none; }
  tr.selected td {
    background: color-mix(in oklab, var(--accent) 8%, transparent);
  }
  tr:hover td { background: color-mix(in oklab, var(--ink) 3%, transparent); }

  .check-col { width: 30px; text-align: center; }
  .filename { max-width: 340px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .muted { color: var(--ink-muted); }

  .pill {
    display: inline-block;
    padding: 2px 8px;
    border-radius: 3px;
    font-size: 10px;
    letter-spacing: 0.14em;
    text-transform: uppercase;
    font-weight: 500;
  }
  .pill-never_seen { background: color-mix(in oklab, var(--accent-cool) 20%, transparent); color: var(--accent-cool); }
  .pill-historic   { background: color-mix(in oklab, var(--ink-faint) 15%, transparent); color: var(--ink-muted); }
  .pill-uploaded   { background: color-mix(in oklab, var(--great) 20%, transparent); color: var(--great); }
  .pill-skipped    { background: color-mix(in oklab, var(--ok) 20%, transparent); color: var(--ok); }

  .map { display: flex; gap: 8px; align-items: baseline; flex-wrap: wrap; }
  .map-title { color: var(--ink); }
  .map-link {
    background: none;
    border: none;
    padding: 0;
    color: var(--ink);
    font: inherit;
    cursor: pointer;
    display: inline-flex;
    align-items: baseline;
    gap: 4px;
  }
  .map-link:hover .map-title { color: var(--accent); }
  .map-link:hover .link-hint { color: var(--accent); }
  .link-hint { color: var(--ink-faint); font-size: 10px; }
  .mods { color: var(--accent); font-size: 11px; }
  .acc { color: var(--ink-muted); font-size: 11px; }

  .empty {
    padding: 32px;
    text-align: center;
    color: var(--ink-muted);
    font-size: 13px;
  }

  input[type="checkbox"] {
    accent-color: var(--accent);
    cursor: pointer;
  }
</style>

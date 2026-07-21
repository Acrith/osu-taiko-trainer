<script>
  import { onMount } from "svelte";
  import { invoke } from "@tauri-apps/api/core";
  import { listen } from "@tauri-apps/api/event";

  let entries = $state([]);
  let loading = $state(false);
  let error = $state(null);

  let search = $state("");
  let stateFilter = $state("not_uploaded"); // "all" | "never_seen" | "historic" | "uploaded" | "skipped" | "not_uploaded"
  let selection = $state(new Set());
  let uploading = $state(false);

  const STATE_LABELS = {
    never_seen: "New",
    historic:   "Historic",
    uploaded:   "Uploaded",
    skipped:    "Skipped",
  };

  const filtered = $derived.by(() => {
    const q = search.trim().toLowerCase();
    return entries.filter(e => {
      // Status filter — "not_uploaded" is a compound of everything except
      // the "uploaded" state; matches what most users mean by "stuff still
      // to import".
      if (stateFilter === "not_uploaded") {
        if (e.state === "uploaded") return false;
      } else if (stateFilter !== "all" && e.state !== stateFilter) {
        return false;
      }
      if (!q) return true;
      const hay = `${e.filename} ${e.map_title ?? ""}`.toLowerCase();
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
      // Purge selections for files that vanished from the folder.
      const alive = new Set(entries.map(e => e.filename));
      selection = new Set([...selection].filter(f => alive.has(f)));
    } catch (e) {
      error = String(e);
    } finally {
      loading = false;
    }
  }

  onMount(async () => {
    await refresh();
    // When the worker finishes uploading a file, the row's state changes —
    // refresh so the UI reflects reality without the user hitting Refresh.
    // Debounced so a burst of 10 completions triggers one rescan.
    let pending = null;
    const unlisten = await listen("activity-added", () => {
      clearTimeout(pending);
      pending = setTimeout(() => { refresh(); }, 500);
    });
    return () => { clearTimeout(pending); unlisten(); };
  });

  function toggle(name) {
    // Set operations in Svelte 5 aren't reactive on mutation — reassign
    // to trigger tracking. Same pattern as Map/WeakMap in runes mode.
    const next = new Set(selection);
    if (next.has(name)) next.delete(name);
    else next.add(name);
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
      // Don't clear the selection immediately — the user can see what's
      // still pending as rows flip to "uploaded". They can clear it via
      // "Select none" once satisfied.
    } catch (e) {
      error = String(e);
    } finally {
      uploading = false;
    }
  }

  function clearSelection() { selection = new Set(); }

  // Compact relative time formatter — "2m", "3h", "5d", etc. Full ISO
  // stays in the row's title attribute for hover disclosure.
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
</script>

<div class="page">
  <div class="eyebrow">Import</div>
  <div class="head">
    <h1 class="title">Replays folder</h1>
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
            <td class="mono filename" title={e.filename}>{e.filename}</td>
            <td class="mono muted" title={e.modified_at ?? ""}>{relTime(e.modified_at)}</td>
            <td class="mono muted">{fmtSize(e.size_bytes)}</td>
            <td>
              <span class="pill pill-{e.state}">{STATE_LABELS[e.state] ?? e.state}</span>
            </td>
            <td class="map">
              {#if e.map_title && e.map_title !== "SKIPPED_HISTORIC"}
                <span class="map-title">{e.map_title}</span>
                {#if e.mods && e.mods !== "NM"}<span class="mods mono">+{e.mods}</span>{/if}
                {#if typeof e.accuracy === "number"}
                  <span class="acc mono">{(e.accuracy * 100).toFixed(2)}%</span>
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

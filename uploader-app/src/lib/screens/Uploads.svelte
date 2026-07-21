<script>
  import { recentActivity } from "../stores.js";

  let rows = $state([]);
  recentActivity.subscribe(v => (rows = v));
</script>

<div class="page">
  <div class="eyebrow">Uploads</div>
  <h1 class="title">Recent activity</h1>

  {#if rows.length === 0}
    <div class="empty">
      No uploads yet in this session. Play a taiko map and this list fills in
      automatically. Historical uploads from before v0.2.0 aren't shown here.
    </div>
  {:else}
    <table class="uploads">
      <thead>
        <tr>
          <th>When</th>
          <th>Map</th>
          <th>File</th>
          <th class="right">Status</th>
        </tr>
      </thead>
      <tbody>
        {#each rows as row (row.id)}
          <tr>
            <td class="mono muted">{row.at}</td>
            <td>{row.map_title ?? "—"}</td>
            <td class="mono muted">{row.file_name}</td>
            <td class="right">
              <span class="status status-{row.status}">{row.status}</span>
            </td>
          </tr>
        {/each}
      </tbody>
    </table>
  {/if}

  <div class="eyebrow footnote">Selective upload UI — coming next commit</div>
</div>

<style>
  .page { max-width: 100%; }
  .title {
    font-family: var(--font-mono);
    font-weight: 500;
    font-size: 24px;
    margin: 4px 0 20px 0;
    color: var(--ink);
  }
  .empty {
    background: var(--panel);
    border: 1px dashed var(--rule);
    border-radius: 6px;
    padding: 24px;
    color: var(--ink-muted);
    font-size: 13px;
    line-height: 1.6;
  }
  .uploads {
    width: 100%;
    border-collapse: separate;
    border-spacing: 0;
    font-family: var(--font-mono);
    font-size: 12px;
    font-variant-numeric: tabular-nums;
  }
  .uploads th {
    text-align: left;
    padding: 8px 10px;
    font-size: 10px;
    letter-spacing: 0.14em;
    text-transform: uppercase;
    color: var(--ink-faint);
    border-bottom: 1px solid var(--rule);
  }
  .uploads td {
    padding: 8px 10px;
    border-bottom: 1px solid var(--rule);
    color: var(--ink);
  }
  .right { text-align: right; }
  .muted { color: var(--ink-muted); }
  .status {
    display: inline-block;
    padding: 2px 8px;
    border-radius: 3px;
    font-size: 10px;
    letter-spacing: 0.14em;
    text-transform: uppercase;
  }
  .status-uploaded { background: color-mix(in oklab, var(--great) 20%, transparent); color: var(--great); }
  .status-failed   { background: var(--accent-faint); color: var(--accent); }
  .status-skipped  { background: color-mix(in oklab, var(--ink-faint) 15%, transparent); color: var(--ink-muted); }
  .footnote { margin-top: 24px; }
</style>

<script>
  import { config } from "../stores.js";

  let cfg = $state(null);
  config.subscribe(v => (cfg = v));
</script>

<div class="page">
  <div class="eyebrow">Settings</div>
  <h1 class="title">Config</h1>

  {#if !cfg}
    <div class="empty">
      Config isn't loaded yet — the Rust backend populates this on startup.
      Full settings editor lands in the next commit.
    </div>
  {:else}
    <div class="row">
      <div class="row-k">API token</div>
      <div class="row-v mono">
        {cfg.api_token ? cfg.api_token.slice(0, 12) + "…" : "not set"}
      </div>
    </div>
    <div class="row">
      <div class="row-k">Replays folder</div>
      <div class="row-v mono">{cfg.replays_folder ?? "not set"}</div>
    </div>
    <div class="row">
      <div class="row-k">Server</div>
      <div class="row-v mono">{cfg.server_url}</div>
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
  .empty {
    background: var(--panel);
    border: 1px dashed var(--rule);
    border-radius: 6px;
    padding: 24px;
    color: var(--ink-muted);
    font-size: 13px;
    line-height: 1.6;
  }
  .row {
    display: grid;
    grid-template-columns: 160px 1fr;
    gap: 16px;
    padding: 12px 0;
    border-bottom: 1px solid var(--rule);
    align-items: baseline;
  }
  .row-k {
    font-family: var(--font-mono);
    font-size: 10px;
    letter-spacing: 0.14em;
    text-transform: uppercase;
    color: var(--ink-faint);
  }
  .row-v { color: var(--ink); font-size: 13px; word-break: break-all; }
</style>

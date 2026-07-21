<script>
  import { status, stats } from "../stores.js";

  let s = $state({ state: "idle", message: "Starting…", since: null });
  status.subscribe(v => (s = v));

  let counts = $state({ total_uploaded: 0, session_uploaded: 0, session_failed: 0 });
  stats.subscribe(v => (counts = v));

  const stateLabel = $derived({
    idle: "Idle",
    watching: "Watching",
    uploading: "Uploading",
    error: "Error",
  }[s.state] ?? s.state);
</script>

<div class="page">
  <div class="eyebrow">Status</div>
  <h1 class="title">{stateLabel}</h1>
  <div class="message">{s.message}</div>

  <div class="stats">
    <div class="stat">
      <div class="stat-k">total uploaded</div>
      <div class="stat-v">{counts.total_uploaded}</div>
    </div>
    <div class="stat">
      <div class="stat-k">this session</div>
      <div class="stat-v">{counts.session_uploaded}</div>
    </div>
    <div class="stat">
      <div class="stat-k">session failed</div>
      <div class="stat-v" class:err={counts.session_failed > 0}>
        {counts.session_failed}
      </div>
    </div>
  </div>

  <div class="hint eyebrow">Scaffold — full status wiring lands next commit</div>
</div>

<style>
  .page { max-width: 720px; }
  .title {
    font-family: var(--font-mono);
    font-weight: 500;
    font-size: 32px;
    letter-spacing: -0.015em;
    margin: 4px 0 0 0;
    color: var(--ink);
  }
  .message {
    color: var(--ink-muted);
    font-size: 14px;
    margin-top: 6px;
  }
  .stats {
    display: grid;
    grid-template-columns: repeat(3, 1fr);
    gap: 12px;
    margin-top: 32px;
  }
  .stat {
    background: var(--panel);
    border: 1px solid var(--rule);
    border-radius: 6px;
    padding: 14px 16px;
  }
  .stat-k {
    font-family: var(--font-mono);
    font-size: 10px;
    letter-spacing: 0.14em;
    text-transform: uppercase;
    color: var(--ink-faint);
  }
  .stat-v {
    font-family: var(--font-mono);
    font-size: 22px;
    font-weight: 500;
    color: var(--ink);
    font-variant-numeric: tabular-nums;
    margin-top: 2px;
  }
  .stat-v.err { color: var(--miss); }
  .hint { margin-top: 40px; }
</style>

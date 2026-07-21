<script>
  import { status, stats, whoami, config } from "../stores.js";

  let s = $state({ state: "starting", message: "Starting…", since: null });
  status.subscribe(v => (s = v));

  let counts = $state({ total: 0, uploaded: 0, skipped_historic: 0 });
  stats.subscribe(v => (counts = v));

  let who = $state(null);
  whoami.subscribe(v => (who = v));

  let cfg = $state(null);
  config.subscribe(v => (cfg = v));

  const STATE_LABELS = {
    starting:  "Starting",
    watching:  "Watching",
    uploading: "Uploading",
    error:     "Error",
    no_config: "Setup required",
  };
  const stateLabel = $derived(STATE_LABELS[s.state] ?? s.state);
  const stateClass = $derived(`state state-${s.state}`);
</script>

<div class="page">
  <div class="eyebrow">Status</div>
  <h1 class="title">
    <span class={stateClass}></span>
    {stateLabel}
  </h1>
  <div class="message">{s.message}</div>

  <div class="stats">
    <div class="stat">
      <div class="stat-k">total known</div>
      <div class="stat-v">{counts.total}</div>
    </div>
    <div class="stat">
      <div class="stat-k">uploaded</div>
      <div class="stat-v">{counts.uploaded}</div>
    </div>
    <div class="stat">
      <div class="stat-k">skipped historic</div>
      <div class="stat-v muted">{counts.skipped_historic}</div>
    </div>
  </div>

  {#if who}
    <div class="identity-card">
      <div class="identity-avatar">
        {#if who.avatar_url}
          <img src={who.avatar_url} alt="" />
        {/if}
      </div>
      <div>
        <div class="identity-name">{who.username}</div>
        <div class="identity-sub">
          signed in · #{who.user_id}
          {#if who.country_code}· {who.country_code}{/if}
          {#if who.style}· {who.style}{/if}
        </div>
        <div class="identity-sub">
          {who.server_url ?? cfg?.server_url}
        </div>
      </div>
    </div>
  {:else if s.state === "no_config"}
    <div class="setup-cta">
      <div class="setup-title">Set up in Settings</div>
      <div class="setup-body">
        Paste your uploader token and pick your osu! replays folder. The
        watcher starts automatically as soon as you save.
      </div>
    </div>
  {/if}
</div>

<style>
  .page { max-width: 720px; }
  .title {
    display: flex;
    align-items: center;
    gap: 12px;
    font-family: var(--font-mono);
    font-weight: 500;
    font-size: 32px;
    letter-spacing: -0.015em;
    margin: 4px 0 0 0;
    color: var(--ink);
  }
  .state {
    display: inline-block;
    width: 12px;
    height: 12px;
    border-radius: 50%;
    background: var(--ink-faint);
  }
  .state-starting  { background: var(--ok); animation: pulse 1.6s ease-in-out infinite; }
  .state-watching  { background: var(--great); }
  .state-uploading { background: var(--accent-cool); animation: pulse 1.2s ease-in-out infinite; }
  .state-error     { background: var(--miss); }
  .state-no_config { background: var(--ok); }
  @keyframes pulse {
    0%, 100% { opacity: 1; }
    50%      { opacity: 0.4; }
  }
  .message {
    color: var(--ink-muted);
    font-size: 14px;
    margin-top: 6px;
    line-height: 1.5;
    word-break: break-word;
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
  .stat-v.muted { color: var(--ink-muted); }

  .identity-card {
    display: flex;
    align-items: center;
    gap: 14px;
    padding: 16px;
    background: var(--panel);
    border: 1px solid var(--rule);
    border-radius: 6px;
    margin-top: 24px;
  }
  .identity-avatar {
    width: 56px;
    height: 56px;
    border-radius: 50%;
    overflow: hidden;
    background: color-mix(in oklab, var(--ink) 10%, transparent);
    flex-shrink: 0;
  }
  .identity-avatar img { width: 100%; height: 100%; object-fit: cover; }
  .identity-name {
    font-family: var(--font-mono);
    font-size: 18px;
    font-weight: 500;
    color: var(--ink);
  }
  .identity-sub {
    font-family: var(--font-mono);
    font-size: 11px;
    color: var(--ink-faint);
    letter-spacing: 0.06em;
    margin-top: 2px;
  }

  .setup-cta {
    margin-top: 24px;
    padding: 20px;
    background: var(--accent-faint);
    border: 1px solid var(--accent-soft);
    border-radius: 6px;
    color: var(--accent);
  }
  .setup-title {
    font-family: var(--font-mono);
    font-size: 12px;
    letter-spacing: 0.14em;
    text-transform: uppercase;
    font-weight: 500;
  }
  .setup-body {
    margin-top: 6px;
    color: var(--ink);
    font-size: 13px;
    line-height: 1.6;
  }
</style>

<script>
  import { status, stats, whoami, config, mySkill, recentActivity } from "../stores.js";

  let s = $state({ state: "starting", message: "Starting…", since: null });
  status.subscribe(v => (s = v));

  let counts = $state({ total: 0, uploaded: 0, skipped_historic: 0 });
  stats.subscribe(v => (counts = v));

  let who = $state(null);
  whoami.subscribe(v => (who = v));

  let cfg = $state(null);
  config.subscribe(v => (cfg = v));

  let skill = $state(null);
  mySkill.subscribe(v => (skill = v));

  let activity = $state([]);
  recentActivity.subscribe(v => (activity = v));

  const STATE_LABELS = {
    starting:  "Starting",
    watching:  "Watching",
    uploading: "Uploading",
    error:     "Error",
    no_config: "Setup required",
  };
  const stateLabel = $derived(STATE_LABELS[s.state] ?? s.state);
  const stateClass = $derived(`state-dot state-${s.state}`);

  const DIMS = [
    { k: "speed",       label: "SPE" },
    { k: "stamina",     label: "STA" },
    { k: "gimmick",     label: "GIM" },
    { k: "technical",   label: "TEC" },
    { k: "consistency", label: "CON" },
    { k: "reading",     label: "REA" },
  ];

  function fmtInt(n) {
    return typeof n === "number" ? Math.round(n).toLocaleString() : "—";
  }
  function fmtRank(n) {
    return typeof n === "number" ? `#${n.toLocaleString()}` : "—";
  }

  // Compact when-column formatter. Same-day rows show HH:MM; older show
  // MM-DD HH:MM so a table with entries from multiple days is legible.
  function fmtWhen(v) {
    if (!v) return "—";
    // Accept both RFC 3339 and SQLite datetime('now') "YYYY-MM-DD HH:MM:SS".
    const iso = v.includes("T") ? v : v.replace(" ", "T") + "Z";
    const d = new Date(iso);
    if (isNaN(d.getTime())) return v;
    const now = new Date();
    const sameDay = d.getFullYear() === now.getFullYear()
      && d.getMonth() === now.getMonth()
      && d.getDate() === now.getDate();
    const hh = String(d.getHours()).padStart(2, "0");
    const mm = String(d.getMinutes()).padStart(2, "0");
    if (sameDay) return `${hh}:${mm}`;
    const mo = String(d.getMonth() + 1).padStart(2, "0");
    const dd = String(d.getDate()).padStart(2, "0");
    const sameYear = d.getFullYear() === now.getFullYear();
    return sameYear ? `${mo}-${dd} ${hh}:${mm}` : `${d.getFullYear()}-${mo}-${dd}`;
  }

  const recentTop = $derived(activity.slice(0, 8));
</script>

<div class="page">
  <!-- Leaderboard-row-style identity band, when we have signal for it -->
  {#if who}
    <div class="hero">
      <div class="hero-rank mono">{skill?.has_data ? fmtRank(skill?.rank) : "—"}</div>
      <div class="hero-avatar">
        {#if who.avatar_url}<img src={who.avatar_url} alt="" />{/if}
      </div>
      <div class="hero-name-col">
        <div class="hero-name-row">
          {#if who.country_code}
            <span class="country-tag mono">{who.country_code}</span>
          {/if}
          <span class="hero-username">{who.username}</span>
        </div>
        <div class="hero-sub mono">
          {skill?.has_data ? `${(skill?.replays ?? 0).toLocaleString()} replays` : "no rateable plays yet"}
          {#if who.style} · {who.style.toUpperCase()}{/if}
        </div>
      </div>

      <div class="hero-total mono">
        <span class="hero-total-v">{skill?.has_data ? fmtInt(skill?.total) : "—"}</span>
        <span class="hero-total-k">total</span>
      </div>

      <div class="hero-dims">
        {#each DIMS as d (d.k)}
          <div class="dim">
            <span class="dim-k mono">{d.label}</span>
            <span class="dim-v mono">
              {skill?.has_data ? fmtInt(skill?.[d.k]) : "—"}
            </span>
          </div>
        {/each}
      </div>
    </div>
  {/if}

  <!-- Status strip -->
  <div class="status-strip">
    <div class="status-inner">
      <span class={stateClass}></span>
      <span class="state-label mono">{stateLabel}</span>
      <span class="state-msg">{s.message}</span>
    </div>
    <div class="status-stats mono">
      <span><b>{counts.uploaded}</b> uploaded</span>
      <span class="muted"><b>{counts.total}</b> known</span>
      <span class="muted"><b>{counts.skipped_historic}</b> historic</span>
    </div>
  </div>

  {#if s.state === "no_config"}
    <div class="setup-cta">
      <div class="setup-title mono">Set up in Settings</div>
      <div class="setup-body">
        Paste your uploader token and pick your osu! Replays folder.
        The watcher starts automatically as soon as you save.
      </div>
    </div>
  {/if}

  <!-- Recent activity -->
  <div class="section">
    <h2 class="section-title">Recent uploads</h2>
    {#if activity.length === 0}
      <div class="empty">
        No uploads yet in this session. Play a taiko map and this list
        fills in automatically. Or head to <b>Replays</b> to pick specific
        older files.
      </div>
    {:else}
      <table class="activity">
        <thead>
          <tr>
            <th>When</th>
            <th>Map</th>
            <th>File</th>
            <th class="right">Status</th>
          </tr>
        </thead>
        <tbody>
          {#each recentTop as row (row.id)}
            <tr>
              <td class="mono muted" title={row.at}>{fmtWhen(row.at)}</td>
              <td>{row.map_title ?? "—"}</td>
              <td class="mono muted filename" title={row.file_name}>{row.file_name}</td>
              <td class="right">
                <span class="pill pill-{row.status}">{row.status}</span>
              </td>
            </tr>
          {/each}
        </tbody>
      </table>
      {#if activity.length > recentTop.length}
        <div class="more mono">
          + {activity.length - recentTop.length} more this session
        </div>
      {/if}
    {/if}
  </div>
</div>

<style>
  .page { max-width: 100%; }

  /* Leaderboard-row band */
  .hero {
    display: grid;
    grid-template-columns: 60px 60px minmax(180px, 1fr) auto auto;
    align-items: center;
    gap: 18px;
    padding: 14px 18px;
    background: var(--panel);
    border: 1px solid var(--rule);
    border-radius: 8px;
    margin-bottom: 14px;
  }
  .hero-rank {
    font-size: 20px;
    color: var(--ok);
    text-align: center;
    font-variant-numeric: tabular-nums;
    background: color-mix(in oklab, var(--ok) 15%, transparent);
    padding: 8px 4px;
    border-radius: 4px;
  }
  .hero-avatar {
    width: 60px;
    height: 60px;
    border-radius: 50%;
    overflow: hidden;
    background: color-mix(in oklab, var(--ink) 10%, transparent);
    flex-shrink: 0;
  }
  .hero-avatar img { width: 100%; height: 100%; object-fit: cover; }
  .hero-name-row {
    display: flex;
    align-items: baseline;
    gap: 10px;
  }
  .country-tag {
    background: color-mix(in oklab, var(--ink-faint) 20%, transparent);
    color: var(--ink-muted);
    padding: 2px 8px;
    border-radius: 3px;
    font-size: 10px;
    letter-spacing: 0.14em;
    font-weight: 600;
  }
  .hero-username {
    font-family: var(--font-mono);
    font-size: 20px;
    font-weight: 500;
    color: var(--ink);
    letter-spacing: -0.01em;
  }
  .hero-sub {
    font-size: 11px;
    color: var(--ink-faint);
    margin-top: 4px;
    letter-spacing: 0.06em;
  }
  .hero-total {
    text-align: right;
    padding: 0 12px;
    border-left: 1px solid var(--rule);
  }
  .hero-total-v {
    display: block;
    font-size: 28px;
    color: var(--accent);
    font-weight: 500;
    font-variant-numeric: tabular-nums;
    line-height: 1;
  }
  .hero-total-k {
    display: block;
    font-size: 10px;
    letter-spacing: 0.14em;
    text-transform: uppercase;
    color: var(--ink-faint);
    margin-top: 4px;
  }
  .hero-dims {
    display: grid;
    grid-template-columns: repeat(3, auto);
    gap: 4px 12px;
    padding-left: 12px;
    border-left: 1px solid var(--rule);
  }
  .dim {
    display: flex;
    align-items: baseline;
    gap: 6px;
  }
  .dim-k {
    font-size: 10px;
    letter-spacing: 0.14em;
    color: var(--ink-faint);
    font-weight: 500;
    min-width: 26px;
  }
  .dim-v {
    font-size: 13px;
    color: var(--ink);
    font-variant-numeric: tabular-nums;
    min-width: 40px;
    text-align: right;
  }

  /* Status strip */
  .status-strip {
    display: flex;
    justify-content: space-between;
    align-items: center;
    padding: 10px 16px;
    background: var(--panel);
    border: 1px solid var(--rule);
    border-radius: 6px;
    margin-bottom: 20px;
    gap: 20px;
  }
  .status-inner {
    display: flex;
    align-items: center;
    gap: 10px;
    min-width: 0;
    flex: 1;
  }
  .state-dot {
    width: 10px; height: 10px; border-radius: 50%;
    background: var(--ink-faint);
    flex-shrink: 0;
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
  .state-label {
    font-size: 12px;
    color: var(--ink);
    letter-spacing: 0.06em;
    font-weight: 500;
  }
  .state-msg {
    font-size: 12px;
    color: var(--ink-muted);
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
    flex: 1;
  }
  .status-stats {
    display: flex;
    gap: 16px;
    font-size: 11px;
    color: var(--ink);
    font-variant-numeric: tabular-nums;
    flex-shrink: 0;
  }
  .status-stats b {
    color: var(--ink);
    margin-right: 4px;
    font-weight: 500;
  }
  .status-stats .muted { color: var(--ink-muted); }
  .status-stats .muted b { color: var(--ink-muted); }

  /* No-config CTA */
  .setup-cta {
    margin-bottom: 20px;
    padding: 16px 20px;
    background: var(--accent-faint);
    border: 1px solid var(--accent-soft);
    border-radius: 6px;
  }
  .setup-title {
    font-size: 12px;
    letter-spacing: 0.14em;
    text-transform: uppercase;
    font-weight: 500;
    color: var(--accent);
  }
  .setup-body {
    margin-top: 6px;
    color: var(--ink);
    font-size: 13px;
    line-height: 1.6;
  }

  /* Recent activity */
  .section { margin-top: 4px; }
  .section-title {
    font-family: var(--font-mono);
    font-weight: 500;
    font-size: 14px;
    letter-spacing: 0.06em;
    color: var(--ink);
    margin: 0 0 8px 0;
  }
  .empty {
    background: var(--panel);
    border: 1px dashed var(--rule);
    border-radius: 6px;
    padding: 20px;
    color: var(--ink-muted);
    font-size: 13px;
    line-height: 1.6;
  }
  .activity {
    width: 100%;
    border-collapse: separate;
    border-spacing: 0;
    font-family: var(--font-mono);
    font-size: 12px;
    font-variant-numeric: tabular-nums;
    background: var(--panel);
    border: 1px solid var(--rule);
    border-radius: 6px;
    overflow: hidden;
  }
  .activity th {
    text-align: left;
    padding: 8px 10px;
    font-size: 10px;
    letter-spacing: 0.14em;
    text-transform: uppercase;
    color: var(--ink-faint);
    border-bottom: 1px solid var(--rule);
    background: var(--panel);
  }
  .activity td {
    padding: 8px 10px;
    border-bottom: 1px solid var(--rule);
    color: var(--ink);
  }
  .activity tr:last-child td { border-bottom: none; }
  .right { text-align: right; }
  .muted { color: var(--ink-muted); }
  .filename { max-width: 340px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .pill {
    display: inline-block;
    padding: 2px 8px;
    border-radius: 3px;
    font-size: 10px;
    letter-spacing: 0.14em;
    text-transform: uppercase;
    font-weight: 500;
  }
  .pill-uploaded { background: color-mix(in oklab, var(--great) 20%, transparent); color: var(--great); }
  .pill-failed   { background: var(--accent-faint); color: var(--accent); }
  .pill-skipped  { background: color-mix(in oklab, var(--ok) 20%, transparent); color: var(--ok); }

  .more {
    margin-top: 8px;
    font-size: 11px;
    color: var(--ink-faint);
    letter-spacing: 0.06em;
    text-align: right;
  }
</style>

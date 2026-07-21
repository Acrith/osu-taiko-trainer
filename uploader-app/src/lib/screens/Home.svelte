<script>
  import { openUrl } from "@tauri-apps/plugin-opener";
  import {
    status, stats, whoami, config, mySkill, recentActivity, myReplays,
  } from "../stores.js";

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

  let serverReplays = $state(null);
  myReplays.subscribe(v => (serverReplays = v));

  // Filename → server replay row, so we can link Recent uploads → the
  // proper /replay/{user}/{id} page. Also lets us surface the server's
  // map_title for rows the local state DB doesn't have context for.
  const serverByFilename = $derived.by(() => {
    // No filename key on server rows — we key by content_hash instead,
    // populated on the Replays scan. For Home we key by (map_title, mods)
    // to be tolerant of missing hashes. Not perfect, good enough for a
    // quick-jump link.
    const byTitle = new Map();
    for (const r of (serverReplays?.replays ?? [])) {
      if (r.map_title) byTitle.set(`${r.map_title}|${r.mods ?? "NM"}`, r);
    }
    return byTitle;
  });

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

  function fmtWhen(v) {
    if (!v) return "—";
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

  // Where the map link opens: prefer the server's /replay/{user}/{id}
  // page, fall back to /u/{username} when we don't have the id.
  function replayUrl(row) {
    if (!cfg?.server_url || !serverReplays?.username) return null;
    const sr = serverByFilename.get(`${row.map_title}|${row.mods ?? "NM"}`);
    if (sr) return `${cfg.server_url}/replay/${serverReplays.username}/${sr.id}`;
    return null;
  }
  function profileUrl() {
    if (!cfg?.server_url || !who?.username) return null;
    return `${cfg.server_url}/u/${who.username}`;
  }
  function open(u) { if (u) openUrl(u); }
</script>

<div class="page">
  {#if who}
    <!-- Leaderboard-row-style hero band. Matches the site's .lb-card:
         blurred cover as bg, dark scrim over it, split rank pill on the
         left, name+country+meta in the middle, total on the right, six
         dim chips in a 3x2 grid. -->
    <div class="hero"
         style={who.cover_url ? `--cover-url: url("${who.cover_url}")` : ""}
         class:has-cover={!!who.cover_url}>
      <div class="hero-inner">
        <div class="rank-pill mono">
          <div class="rank-half">
            <span class="rank-k">osu!</span>
            <span class="rank-v">{fmtRank(who.global_rank)}</span>
          </div>
          <div class="rank-half rank-tt">
            <span class="rank-k">trainer</span>
            <span class="rank-v">{skill?.has_data ? fmtRank(skill?.rank) : "—"}</span>
          </div>
        </div>

        <div class="hero-avatar">
          {#if who.avatar_url}<img src={who.avatar_url} alt="" />{/if}
        </div>

        <div class="hero-name-col">
          <div class="hero-name-row">
            {#if who.country_code}
              <span class="country-tag mono">{who.country_code}</span>
            {/if}
            <button class="hero-username" onclick={() => open(profileUrl())}>
              {who.username}
            </button>
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
    </div>
  {/if}

  <div class="status-strip">
    <div class="status-inner">
      <span class={stateClass}></span>
      <span class="state-label mono">{stateLabel}</span>
      <span class="state-msg mono">{s.message}</span>
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
            <th>Mods</th>
            <th class="right">Acc</th>
            <th class="right">Status</th>
          </tr>
        </thead>
        <tbody>
          {#each recentTop as row (row.id)}
            {@const url = replayUrl(row)}
            <tr class:clickable={!!url} onclick={() => url && open(url)}>
              <td class="mono muted when">{fmtWhen(row.at)}</td>
              <td class="map-cell">
                <span class="map-title">{row.map_title ?? "—"}</span>
                {#if url}<span class="link-hint mono">↗</span>{/if}
              </td>
              <td class="mono mods">{row.mods && row.mods !== "NM" ? `+${row.mods}` : ""}</td>
              <td class="mono right acc">
                {typeof row.accuracy === "number" ? `${(row.accuracy * 100).toFixed(2)}%` : ""}
              </td>
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

  /* Hero band — matches the site's .lb-card. Blurred profile-cover
     background wash with a dark scrim so text stays legible. */
  .hero {
    position: relative;
    overflow: hidden;
    border: 1px solid var(--rule);
    border-radius: 8px;
    margin-bottom: 14px;
    background: var(--panel);
  }
  .hero.has-cover::before {
    content: "";
    position: absolute; inset: 0;
    background-image: var(--cover-url);
    background-size: cover;
    background-position: center;
    filter: blur(8px) saturate(1.1);
    opacity: 0.35;
    z-index: 0;
    pointer-events: none;
  }
  .hero.has-cover::after {
    content: "";
    position: absolute; inset: 0;
    background: linear-gradient(90deg,
      color-mix(in oklab, var(--ground) 65%, transparent) 0%,
      color-mix(in oklab, var(--ground) 40%, transparent) 60%,
      color-mix(in oklab, var(--ground) 65%, transparent) 100%);
    z-index: 0;
    pointer-events: none;
  }
  .hero-inner {
    position: relative;
    z-index: 1;
    display: grid;
    align-items: center;
    grid-template-columns: 84px 60px minmax(0, 1fr) 88px auto;
    grid-template-areas: "rank avatar name value chips";
    gap: 16px;
    padding: 14px 18px;
  }

  /* Split rank pill — top half osu!, bottom half taiko-trainer */
  .rank-pill {
    grid-area: rank;
    display: flex;
    flex-direction: column;
    background: rgba(0, 0, 0, 0.25);
    border: 1px solid var(--rule);
    border-radius: 5px;
    overflow: hidden;
    align-self: stretch;
  }
  .rank-half {
    padding: 5px 8px;
    display: flex;
    justify-content: space-between;
    align-items: baseline;
    gap: 6px;
    font-variant-numeric: tabular-nums;
  }
  .rank-half.rank-tt {
    border-top: 1px solid var(--rule);
    background: color-mix(in oklab, var(--accent) 15%, transparent);
  }
  .rank-k {
    font-size: 9px;
    letter-spacing: 0.14em;
    text-transform: uppercase;
    color: var(--ink-faint);
    font-weight: 600;
  }
  .rank-half.rank-tt .rank-k { color: var(--accent); }
  .rank-v {
    font-size: 13px;
    color: var(--ink);
    font-weight: 500;
  }
  .rank-half.rank-tt .rank-v { color: var(--accent); }

  .hero-avatar {
    grid-area: avatar;
    width: 60px;
    height: 60px;
    border-radius: 50%;
    overflow: hidden;
    background: color-mix(in oklab, var(--ink) 10%, transparent);
    border: 1px solid rgba(255, 255, 255, 0.08);
    flex-shrink: 0;
  }
  .hero-avatar img { width: 100%; height: 100%; object-fit: cover; }

  .hero-name-col {
    grid-area: name;
    min-width: 0;
    overflow: hidden;
  }
  .hero-name-row {
    display: flex;
    align-items: baseline;
    gap: 10px;
    min-width: 0;
  }
  .country-tag {
    flex-shrink: 0;
    background: rgba(0, 0, 0, 0.25);
    border: 1px solid var(--rule);
    color: var(--ink-muted);
    padding: 2px 6px;
    border-radius: 3px;
    font-size: 9px;
    letter-spacing: 0.14em;
    font-weight: 600;
  }
  .hero-username {
    background: none;
    border: none;
    padding: 0;
    color: var(--ink);
    font-family: var(--font-mono);
    font-size: 20px;
    font-weight: 500;
    letter-spacing: -0.01em;
    cursor: pointer;
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
    min-width: 0;
    max-width: 100%;
    text-align: left;
  }
  .hero-username:hover { color: var(--accent); }
  .hero-sub {
    font-size: 11px;
    color: var(--ink-faint);
    margin-top: 3px;
    letter-spacing: 0.06em;
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
  }

  .hero-total {
    grid-area: value;
    text-align: right;
    padding: 0 12px;
    border-left: 1px solid var(--rule);
  }
  .hero-total-v {
    display: block;
    font-size: 24px;
    color: var(--accent);
    font-weight: 500;
    font-variant-numeric: tabular-nums;
    line-height: 1.1;
    letter-spacing: -0.01em;
  }
  .hero-total-k {
    display: block;
    font-size: 10px;
    letter-spacing: 0.14em;
    text-transform: uppercase;
    color: var(--ink-faint);
    margin-top: 2px;
  }

  /* Six dim chips — 3x2 grid, mini pills matching site's .lb-otherdim */
  .hero-dims {
    grid-area: chips;
    display: grid;
    grid-template-columns: repeat(3, 76px);
    grid-auto-rows: 1fr;
    gap: 3px;
    align-content: center;
  }
  .dim {
    display: flex;
    justify-content: space-between;
    align-items: center;
    gap: 4px;
    padding: 2px 6px;
    border: 1px solid var(--rule);
    border-radius: 3px;
    background: rgba(0, 0, 0, 0.2);
  }
  .dim-k {
    font-size: 9px;
    letter-spacing: 0.14em;
    color: var(--ink-faint);
    text-transform: uppercase;
    font-weight: 600;
  }
  .dim-v {
    font-size: 11px;
    color: var(--ink);
    font-variant-numeric: tabular-nums;
    font-weight: 500;
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
  .status-stats b { color: var(--ink); margin-right: 4px; font-weight: 500; }
  .status-stats .muted { color: var(--ink-muted); }
  .status-stats .muted b { color: var(--ink-muted); }

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
  .activity tr.clickable { cursor: pointer; }
  .activity tr.clickable:hover td {
    background: color-mix(in oklab, var(--accent) 8%, transparent);
  }
  .right { text-align: right; }
  .muted { color: var(--ink-muted); }
  .when { width: 100px; }
  .mods { color: var(--accent); width: 80px; }
  .acc { width: 70px; color: var(--ink); }
  .map-cell {
    display: flex;
    align-items: baseline;
    gap: 6px;
  }
  .map-title {
    color: var(--ink);
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }
  .link-hint {
    color: var(--ink-faint);
    font-size: 10px;
  }
  tr.clickable:hover .link-hint { color: var(--accent); }

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

  /* Narrow-window fallback: fold the chips onto a second visual row so
     the name column always has enough space to show ~14 chars at least. */
  @media (max-width: 1100px) {
    .hero-inner {
      grid-template-columns: 76px 52px minmax(0, 1fr) 78px;
      grid-template-areas:
        "rank avatar name  value"
        ".    .      chips chips";
      row-gap: 10px;
    }
    .hero-dims { justify-content: start; grid-template-columns: repeat(6, 76px); }
    .hero-avatar { width: 52px; height: 52px; }
    .hero-username { font-size: 18px; }
    .hero-total-v { font-size: 20px; }
  }
</style>

<script>
  import { onMount } from "svelte";
  import { invoke } from "@tauri-apps/api/core";
  import { listen } from "@tauri-apps/api/event";
  import Sidebar from "./lib/Sidebar.svelte";
  import Home from "./lib/screens/Home.svelte";
  import Replays from "./lib/screens/Replays.svelte";
  import Settings from "./lib/screens/Settings.svelte";
  import About from "./lib/screens/About.svelte";
  import {
    currentScreen, status, whoami, config, recentActivity, stats,
    defaultServerUrl, mySkill,
  } from "./lib/stores.js";

  let screen = $state("home");
  currentScreen.subscribe(v => (screen = v));

  onMount(async () => {
    // Prime the stores in parallel — each invoke is independent.
    const [cfg, srv, initStats, initRecent] = await Promise.all([
      invoke("get_config").catch(() => null),
      invoke("default_server_url").catch(() => "https://taiko.umaladder.moe"),
      invoke("get_stats").catch(() => null),
      invoke("get_recent", { limit: 50 }).catch(() => []),
    ]);
    if (cfg) config.set(cfg);
    if (srv) defaultServerUrl.set(srv);
    if (initStats) stats.set(initStats);
    if (initRecent) recentActivity.set(initRecent.map(rowFromRust));

    // Network-dependent fetches — fire in parallel, don't block startup.
    invoke("fetch_whoami").then(w => whoami.set(w)).catch(() => {});
    invoke("fetch_my_skill").then(s => mySkill.set(s)).catch(() => {});

    const unlisten = await Promise.all([
      listen("status-changed", ev => status.set(ev.payload)),
      listen("stats-changed",  ev => stats.set(ev.payload)),
      listen("recent-changed", ev => recentActivity.set(ev.payload.map(rowFromRust))),
      listen("activity-added", ev => {
        recentActivity.update(list => [rowFromActivity(ev.payload), ...list].slice(0, 200));
      }),
      listen("whoami-changed", ev => whoami.set(ev.payload)),
    ]);

    // Pull the current status AFTER attaching the listener so we can't
    // miss the "watching" transition even if it fires before the JS
    // side finishes subscribing.
    const cur = await invoke("get_current_status").catch(() => null);
    if (cur) status.set(cur);

    // Refresh skill data whenever a new upload completes — the server
    // recomputes the snapshot after each replay lands, so the Home
    // leaderboard band should reflect it within a couple seconds.
    let skillRefreshQueued = null;
    const unlistenSkill = await listen("activity-added", () => {
      clearTimeout(skillRefreshQueued);
      skillRefreshQueued = setTimeout(() => {
        invoke("fetch_my_skill").then(s => mySkill.set(s)).catch(() => {});
      }, 1500);
    });

    return () => {
      clearTimeout(skillRefreshQueued);
      unlistenSkill();
      unlisten.forEach(fn => fn());
    };
  });

  function rowFromRust(r) {
    return {
      id: r.filename,
      file_name: r.filename,
      map_title: r.map_title,
      mods: r.mods,
      accuracy: r.accuracy,
      status: r.replay_id ? "uploaded" : (r.map_title === "SKIPPED_HISTORIC" ? "skipped" : "failed"),
      at: r.uploaded_at,  // full ISO / SQLite datetime — formatted at render time
    };
  }
  function rowFromActivity(a) {
    return {
      id: `${a.file_name}-${a.at}`,
      file_name: a.file_name,
      map_title: a.map_title,
      mods: a.mods,
      accuracy: a.accuracy,
      status: a.status,
      at: a.at,
    };
  }
</script>

<div class="app">
  <Sidebar />
  <main class="stage">
    {#if screen === "home"}
      <Home />
    {:else if screen === "replays"}
      <Replays />
    {:else if screen === "settings"}
      <Settings />
    {:else if screen === "about"}
      <About />
    {/if}
  </main>
</div>

<style>
  .app {
    display: grid;
    grid-template-columns: 200px 1fr;
    height: 100vh;
    overflow: hidden;
  }
  .stage {
    overflow-y: auto;
    padding: 24px 28px;
  }
</style>

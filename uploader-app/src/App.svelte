<script>
  import { onMount } from "svelte";
  import { invoke } from "@tauri-apps/api/core";
  import { listen } from "@tauri-apps/api/event";
  import Sidebar from "./lib/Sidebar.svelte";
  import Home from "./lib/screens/Home.svelte";
  import Uploads from "./lib/screens/Uploads.svelte";
  import Settings from "./lib/screens/Settings.svelte";
  import About from "./lib/screens/About.svelte";
  import {
    currentScreen, status, whoami, config, recentActivity, stats,
    defaultServerUrl,
  } from "./lib/stores.js";

  let screen = $state("home");
  currentScreen.subscribe(v => (screen = v));

  onMount(async () => {
    // Prime the stores with the current daemon snapshot. Each invoke is
    // parallelizable — they don't depend on each other.
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

    // Whoami is best-effort; may return null if no config or no network.
    invoke("fetch_whoami").then(w => whoami.set(w)).catch(() => {});

    // Subscribe to worker events. These fire whenever the Rust side
    // pushes state changes.
    const unlisten = await Promise.all([
      listen("status-changed", ev => status.set(ev.payload)),
      listen("stats-changed",  ev => stats.set(ev.payload)),
      listen("recent-changed", ev => recentActivity.set(ev.payload.map(rowFromRust))),
      listen("activity-added", ev => {
        recentActivity.update(list => [rowFromActivity(ev.payload), ...list].slice(0, 200));
      }),
      listen("whoami-changed", ev => whoami.set(ev.payload)),
    ]);
    return () => unlisten.forEach(fn => fn());
  });

  // Convert a Rust state::Record into the row shape the Uploads table expects.
  function rowFromRust(r) {
    return {
      id: r.filename,
      file_name: r.filename,
      map_title: r.map_title,
      mods: r.mods,
      accuracy: r.accuracy,
      status: r.replay_id ? "uploaded" : (r.map_title === "SKIPPED_HISTORIC" ? "skipped" : "failed"),
      at: r.uploaded_at?.slice(11, 16) ?? "",
    };
  }
  // Convert a live activity event payload similarly.
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
    {:else if screen === "uploads"}
      <Uploads />
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

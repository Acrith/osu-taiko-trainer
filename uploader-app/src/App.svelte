<script>
  import { onMount } from "svelte";
  import { get } from "svelte/store";
  import { invoke } from "@tauri-apps/api/core";
  import { listen } from "@tauri-apps/api/event";
  import Sidebar from "./lib/Sidebar.svelte";
  import UploadResultToast from "./lib/UploadResultToast.svelte";
  import { scheduleUpdateCheck } from "./lib/updater.js";
  import Home from "./lib/screens/Home.svelte";
  import Replays from "./lib/screens/Replays.svelte";
  import Settings from "./lib/screens/Settings.svelte";
  import About from "./lib/screens/About.svelte";
  import {
    currentScreen, status, whoami, config, recentActivity, stats,
    defaultServerUrl, mySkill, myReplays, lastGain,
  } from "./lib/stores.js";

  const DIMS = ["speed", "stamina", "gimmick", "technical", "consistency", "reading"];
  // The "before" snapshot captured the moment an upload starts, so once
  // the refreshed skill lands we can diff and populate the toast. One
  // slot is enough — during a burst the last upload wins the toast.
  let pendingSnapshot = null;
  let pendingActivity = null;

  let screen = $state("home");
  currentScreen.subscribe(v => (screen = v));

  onMount(async () => {
    // Fire-and-forget update check on launch (3s delay, silent on
    // failure — see lib/updater.js).
    scheduleUpdateCheck();

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
    invoke("fetch_my_replays").then(r => myReplays.set(r)).catch(() => {});

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

    // Refresh skill + replays list whenever a new upload completes —
    // the server recomputes the snapshot after each replay lands, so
    // the Home band + Replays classification should reflect it within
    // a couple seconds. For successful uploads, also capture the
    // pre-upload skill snapshot so we can compute a delta and show
    // the gain toast when the new snapshot arrives.
    let serverRefreshQueued = null;
    const unlistenServer = await listen("activity-added", ev => {
      const a = ev.payload;
      if (a?.status === "uploaded") {
        // Snapshot "before" as of the moment the upload landed. If a
        // previous upload's refresh hadn't completed yet, we still
        // overwrite — the newest upload's diff is what the user sees.
        pendingSnapshot = get(mySkill);
        pendingActivity = a;
      }
      clearTimeout(serverRefreshQueued);
      serverRefreshQueued = setTimeout(async () => {
        const before = pendingSnapshot;
        const activity = pendingActivity;
        pendingSnapshot = null;
        pendingActivity = null;
        const [newSkill] = await Promise.all([
          invoke("fetch_my_skill").catch(() => null),
          invoke("fetch_my_replays").then(r => myReplays.set(r)).catch(() => {}),
        ]);
        if (newSkill) mySkill.set(newSkill);
        if (activity && newSkill?.has_data && before?.has_data) {
          const dims_delta = {};
          let total_delta = 0;
          for (const d of DIMS) {
            const diff = Math.round((newSkill[d] ?? 0) - (before[d] ?? 0));
            dims_delta[d] = diff;
            total_delta += diff;
          }
          // Only surface the toast when SOMETHING actually moved. A
          // second copy of an already-uploaded map (server 200s with
          // no snapshot change) doesn't earn a popup.
          const anyMove = total_delta !== 0 || DIMS.some(d => dims_delta[d] !== 0);
          if (anyMove) {
            lastGain.set({
              map_title: activity.map_title,
              mods: activity.mods,
              accuracy: activity.accuracy,
              total_delta,
              dims_delta,
              at: Date.now(),
            });
          }
        }
      }, 1500);
    });

    return () => {
      clearTimeout(serverRefreshQueued);
      unlistenServer();
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
  <UploadResultToast />
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

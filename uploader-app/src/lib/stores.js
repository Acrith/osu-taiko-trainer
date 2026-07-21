/*
 * Reactive global state for the uploader UI.
 *
 * The Rust worker pushes updates via Tauri events; App.svelte's onMount
 * subscribes and forwards them into these stores so any screen that
 * reads a store re-renders automatically.
 */
import { writable } from "svelte/store";

export const currentScreen = writable("home");

// { state: "starting" | "watching" | "uploading" | "error" | "no_config",
//   message: string,
//   since: ISO-8601 | null }
export const status = writable({
  state: "starting",
  message: "Starting…",
  since: null,
});

// GET /api/v1/whoami result, populated on startup + after token change.
export const whoami = writable(null);

// The Config the daemon is running with. Editable from Settings.
export const config = writable(null);

// Rolling activity feed. `activity-added` events prepend; older rows
// drop off past 200 to keep the DOM light.
export const recentActivity = writable([]);

// Aggregate counts for the header stats row.
export const stats = writable({
  total: 0,
  uploaded: 0,
  skipped_historic: 0,
});

// The URL the shipped binary points at unless the config overrides it.
export const defaultServerUrl = writable("https://taiko.umaladder.moe");

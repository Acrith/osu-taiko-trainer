/*
 * Reactive global state for the uploader UI.
 *
 * These stores are the SINGLE SOURCE OF TRUTH for anything the sidebar +
 * screens need. Rust pushes updates via Tauri events (`status-changed`,
 * `upload-completed`, …) and event listeners here mutate the stores so
 * the UI reacts without every component polling.
 *
 * We use Svelte 5's $state rune (via `writable` shim for compatibility
 * with the .js file — Svelte 5 accepts either).
 */
import { writable } from "svelte/store";

// Which screen the sidebar currently highlights + renders.
// Values match the keys used by <Sidebar /> and the switch in App.svelte.
export const currentScreen = writable("home");

// The uploader daemon status. Rust emits these transitions via
// `status-changed` events.
//   { state: "idle" | "watching" | "uploading" | "error",
//     message: string,
//     since: ISO-8601 string | null }
export const status = writable({
  state: "idle",
  message: "Starting…",
  since: null,
});

// Result of GET /api/v1/whoami. Populated at startup + on token change.
//   { username, user_id, avatar_url, country_code, style, server_url } | null
export const whoami = writable(null);

// The config the daemon is currently running with. Loaded from disk on
// startup, updated when the Settings screen saves.
//   { api_token, replays_folder, server_url }
export const config = writable(null);

// Recent activity — appended to as uploads happen. Bounded to ~200 rows
// in the UI; older ones drop off.
//   Array<{ id, file_name, map_title, status, at, uploaded_at }>
export const recentActivity = writable([]);

// Aggregate counts for the header stats row.
//   { total_uploaded, session_uploaded, session_failed }
export const stats = writable({
  total_uploaded: 0,
  session_uploaded: 0,
  session_failed: 0,
});

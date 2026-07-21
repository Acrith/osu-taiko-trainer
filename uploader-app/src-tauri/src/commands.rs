//! Tauri commands exposed to the Svelte frontend. Each function is called
//! from JS via `invoke("cmd_name", args)`; return values are serialized
//! to JSON automatically.

use crate::config::{self, Config};
use crate::http;
use crate::state::{Record, State, Stats};
use crate::worker::{StatusPayload, StatusSlot, WorkerCmd};
use std::sync::Arc;
use tauri::State as TauriState;
use tokio::sync::mpsc;

/// Shared state Tauri hands to command functions on every invoke.
pub struct AppState {
    pub state: Arc<State>,
    pub worker_tx: mpsc::Sender<WorkerCmd>,
    /// The most recent status the worker emitted. Read via
    /// `get_current_status` so the frontend doesn't have to race the
    /// `status-changed` event.
    pub status_slot: StatusSlot,
}

#[tauri::command]
pub fn get_config() -> Result<Option<Config>, String> {
    config::load()
}

#[tauri::command]
pub async fn save_config(
    cfg: Config,
    app_state: TauriState<'_, AppState>,
) -> Result<(), String> {
    config::save(&cfg)?;
    // Tell the worker to re-read the config + restart the watcher.
    let _ = app_state.worker_tx.send(WorkerCmd::Reload).await;
    Ok(())
}

#[tauri::command]
pub fn detect_replays_folder() -> Option<String> {
    config::detect_replays_folder().map(|p| p.to_string_lossy().to_string())
}

#[tauri::command]
pub fn default_server_url() -> &'static str {
    config::DEFAULT_SERVER_URL
}

#[tauri::command]
pub fn get_stats(app_state: TauriState<'_, AppState>) -> Result<Stats, String> {
    app_state.state.stats()
}

/// Return the last StatusPayload the worker emitted. Frontend calls this
/// after registering its `status-changed` listener so it doesn't matter
/// whether the worker emitted before or after the listener attached.
#[tauri::command]
pub fn get_current_status(app_state: TauriState<'_, AppState>) -> Result<StatusPayload, String> {
    app_state
        .status_slot
        .lock()
        .map(|s| s.clone())
        .map_err(|e| format!("status slot poisoned: {}", e))
}

#[tauri::command]
pub fn get_recent(
    limit: Option<i64>,
    app_state: TauriState<'_, AppState>,
) -> Result<Vec<Record>, String> {
    app_state.state.recent(limit.unwrap_or(50), false)
}

/// Fire a whoami request now — used by the Home screen when the user
/// asks to "check identity" after re-entering a token.
#[tauri::command]
pub async fn fetch_whoami() -> Result<Option<http::Whoami>, String> {
    let cfg = match config::load()? {
        Some(c) => c,
        None => return Ok(None),
    };
    let client = http::build_client();
    Ok(http::whoami(&client, &cfg).await)
}

/// Kick off a one-shot backfill of every `.osr` in the folder that hasn't
/// already been uploaded. Explicit user action — never fires by itself.
#[tauri::command]
pub async fn backfill(app_state: TauriState<'_, AppState>) -> Result<(), String> {
    app_state
        .worker_tx
        .send(WorkerCmd::Backfill)
        .await
        .map_err(|e| e.to_string())
}

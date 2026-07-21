//! Tauri commands exposed to the Svelte frontend. Each function is called
//! from JS via `invoke("cmd_name", args)`; return values are serialized
//! to JSON automatically.

use crate::config::{self, Config};
use crate::folder::{self, FolderEntry};
use crate::http;
use crate::state::{Record, State, Stats};
use crate::worker::{StatusPayload, StatusSlot, WorkerCmd};
use std::path::PathBuf;
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

/// Read the tail of `~/.taiko-trainer/uploader.log`. `lines` bounds the
/// return so a multi-megabyte log doesn't have to cross the IPC boundary
/// or clog the UI. Default matches roughly a day of typical logging.
#[tauri::command]
pub fn read_log(lines: Option<usize>) -> Result<String, String> {
    let path = config::log_path();
    if !path.exists() {
        return Ok(String::from("(no log file yet — one will appear after the first watcher run)"));
    }
    let content = std::fs::read_to_string(&path)
        .map_err(|e| format!("read {}: {}", path.display(), e))?;
    let n = lines.unwrap_or(500);
    let all: Vec<&str> = content.lines().collect();
    let start = all.len().saturating_sub(n);
    let tail = &all[start..];
    // Prefix with a marker so users pasting the log into a bug report
    // know it's the recent slice rather than the whole file.
    if start > 0 {
        Ok(format!("... ({} earlier lines omitted)\n{}", start, tail.join("\n")))
    } else {
        Ok(tail.join("\n"))
    }
}

/// The directory the log + config + state DB all live in. Frontend calls
/// this to build an "Open folder" button that pops the OS file browser
/// at that path via plugin-opener.
#[tauri::command]
pub fn log_folder_path() -> String {
    config::config_dir().to_string_lossy().to_string()
}

#[derive(serde::Serialize)]
pub struct TestResult {
    pub ok: bool,
    pub kind: &'static str, // "authenticated" | "unauth" | "network" | "no_config"
    pub message: String,
    pub username: Option<String>,
}

/// Hit /api/v1/whoami with the currently-saved token so the user can
/// verify their config works without playing a map.
#[tauri::command]
pub async fn test_connection() -> Result<TestResult, String> {
    let cfg = match config::load()? {
        Some(c) => c,
        None => return Ok(TestResult {
            ok: false, kind: "no_config", username: None,
            message: "Save a token + folder first — nothing to test yet.".into(),
        }),
    };
    let client = http::build_client();
    match http::whoami(&client, &cfg).await {
        Some(who) => Ok(TestResult {
            ok: true, kind: "authenticated", username: Some(who.username.clone()),
            message: format!("Authenticated as {} · server reachable", who.username),
        }),
        None => Ok(TestResult {
            ok: false, kind: "unauth", username: None,
            message: format!("Server didn't accept the token at {}. Either the token is wrong / revoked, or the server URL is bad.", cfg.server_url),
        }),
    }
}

/// Tell the worker to re-read config + restart the watcher loop without
/// touching the saved token/folder. Useful when the server was down
/// during launch or the folder just became available (mounted network
/// share, etc.).
#[tauri::command]
pub async fn restart_watcher(app_state: TauriState<'_, AppState>) -> Result<(), String> {
    app_state
        .worker_tx
        .send(WorkerCmd::Reload)
        .await
        .map_err(|e| e.to_string())
}

/// Fetch the current user's skill snapshot for the Home leaderboard band.
/// None means either no config or the server was unreachable — the
/// frontend keeps its last-known value in either case.
#[tauri::command]
pub async fn fetch_my_skill() -> Result<Option<http::MySkill>, String> {
    let cfg = match config::load()? {
        Some(c) => c,
        None => return Ok(None),
    };
    let client = http::build_client();
    Ok(http::my_skill(&client, &cfg).await)
}

/// Fetch the server's list of stored replays for this user. The Replays
/// screen uses `content_hash` to cross-reference "is this local .osr on
/// the server already?" so HISTORIC files that were uploaded elsewhere
/// classify as UPLOADED instead of just "we haven't sent them yet".
#[derive(serde::Serialize)]
pub struct MyReplaysPayload {
    pub username: String,
    pub replays: Vec<http::MyReplay>,
}

#[tauri::command]
pub async fn fetch_my_replays() -> Result<Option<MyReplaysPayload>, String> {
    let cfg = match config::load()? {
        Some(c) => c,
        None => return Ok(None),
    };
    let client = http::build_client();
    Ok(http::my_replays(&client, &cfg).await
        .map(|(username, replays)| MyReplaysPayload { username, replays }))
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

/// Return every `.osr` in the current replays folder, joined with the
/// state DB so the Import screen can classify each one. Config must be
/// loaded — returns an empty Vec if it isn't (frontend treats that as
/// "no folder configured yet, show CTA to Settings").
#[tauri::command]
pub async fn list_folder_entries(
    app_state: TauriState<'_, AppState>,
) -> Result<Vec<FolderEntry>, String> {
    let cfg = config::load()?;
    let Some(cfg) = cfg else { return Ok(Vec::new()); };
    let folder = PathBuf::from(&cfg.replays_folder);
    if !folder.is_dir() {
        return Ok(Vec::new());
    }
    // Scan reads the directory + does a SELECT per file — offload to a
    // blocking task so the frontend's invoke doesn't stall the UI thread.
    let state_arc = app_state.state.clone();
    tokio::task::spawn_blocking(move || Ok(folder::scan(&state_arc, &folder)))
        .await
        .map_err(|e| format!("scan task join error: {}", e))?
}

/// Upload a specific list of filenames chosen from the Import screen.
/// The filenames are joined against the configured replays folder — the
/// frontend never sends a full path so we can't be tricked into reading
/// files outside the folder.
#[tauri::command]
pub async fn upload_files(
    filenames: Vec<String>,
    app_state: TauriState<'_, AppState>,
) -> Result<(), String> {
    let cfg = config::load()?.ok_or_else(|| "no config".to_string())?;
    let folder = PathBuf::from(&cfg.replays_folder);
    let paths: Vec<PathBuf> = filenames
        .into_iter()
        .filter_map(|f| {
            // Reject anything with a path separator — user-provided names
            // must be a bare filename inside the folder, nothing else.
            if f.contains('/') || f.contains('\\') || f.contains("..") {
                return None;
            }
            let p = folder.join(&f);
            if p.exists() { Some(p) } else { None }
        })
        .collect();
    if paths.is_empty() {
        return Err("no valid files".to_string());
    }
    app_state
        .worker_tx
        .send(WorkerCmd::UploadFiles(paths))
        .await
        .map_err(|e| e.to_string())
}

//! The background task that watches the folder, uploads new replays, and
//! pushes state changes to the frontend as Tauri events.
//!
//! Design mirrors the Python uploader's `watch_and_upload` loop:
//! - On start, snapshot every existing `.osr` as SKIPPED_HISTORIC so the
//!   watcher never uploads pre-existing plays without the user asking.
//! - Then run the notify watcher AND a periodic fallback poll (in case
//!   the OS drops events, sleep/resume gaps, network shares, etc.).
//! - Each new file goes through retry-with-backoff (2s → 60s cap) until
//!   it succeeds, gets a permanent skip verdict, or the app shuts down.

use crate::config::Config;
use crate::http;
use crate::state::{self, State};
use crate::watcher::watch_folder;
use serde::Serialize;
use std::path::{Path, PathBuf};
use std::sync::Arc;
use std::time::Duration;
use tauri::{AppHandle, Emitter};
use tokio::sync::mpsc;
use tokio::time::{interval, sleep};

/// Signals sent to the worker from the outside (commands invoked by JS).
#[derive(Debug)]
pub enum WorkerCmd {
    /// Config changed on disk — re-read + restart the watcher loop.
    Reload,
    /// Backfill: scan the folder and upload EVERYTHING not in state
    /// (including things marked SKIPPED_HISTORIC in a previous session).
    /// User has to opt in explicitly — never happens automatically.
    Backfill,
    /// User asked to shut down. Worker drains and exits.
    Shutdown,
}

/// Public status pushed to the frontend on state changes.
#[derive(Clone, Debug, Serialize)]
pub struct StatusPayload {
    pub state: &'static str, // "starting" | "watching" | "uploading" | "error" | "no_config"
    pub message: String,
    pub since: String,
}

/// One row appended to the frontend's recentActivity store when an upload
/// completes (success, skip, or fail).
#[derive(Clone, Debug, Serialize)]
pub struct ActivityRow {
    pub file_name: String,
    pub map_title: Option<String>,
    pub mods: Option<String>,
    pub accuracy: Option<f64>,
    pub status: &'static str, // "uploaded" | "skipped" | "failed"
    pub at: String,
}

/// Entry point — spawns the worker task on Tauri's shared tokio runtime.
/// Returns the sender the commands module uses to poke the worker.
///
/// `tauri::async_runtime::spawn` wraps `tokio::spawn` but drives it on the
/// runtime tauri already set up; calling `tokio::spawn` directly from the
/// synchronous `setup` closure would panic (no current runtime).
pub fn spawn(app: AppHandle, state: Arc<State>) -> mpsc::Sender<WorkerCmd> {
    let (tx, rx) = mpsc::channel::<WorkerCmd>(16);
    tauri::async_runtime::spawn(async move {
        crate::logging::log_line("worker: task started");
        // Panics inside a spawned task get caught by the panic hook (which
        // writes to the log) — no wrapper needed here. Log the normal
        // exit so we can tell "the loop finished cleanly" from "the loop
        // never even started".
        run(app, state, rx).await;
        crate::logging::log_line("worker: run() returned");
    });
    tx
}

async fn run(app: AppHandle, state: Arc<State>, mut cmd_rx: mpsc::Receiver<WorkerCmd>) {
    crate::logging::log_line("worker::run: entered");
    emit_status(&app, "starting", "Starting…");

    loop {
        crate::logging::log_line("worker::run: loading config");
        let cfg = match crate::config::load() {
            Ok(Some(c)) => {
                crate::logging::log_line(&format!(
                    "worker::run: config loaded — server={} folder={}",
                    c.server_url, c.replays_folder
                ));
                c
            }
            Ok(None) => {
                crate::logging::log_line("worker::run: no config on disk");
                emit_status(
                    &app,
                    "no_config",
                    "No config yet — open Settings to enter your token and replays folder.",
                );
                // Wait for a Reload signal (JS calls it after save_config).
                if !wait_for_reload(&mut cmd_rx).await {
                    return;
                }
                continue;
            }
            Err(e) => {
                crate::logging::log_line(&format!("worker::run: config load error: {}", e));
                emit_status(&app, "error", format!("Config error: {}", e));
                if !wait_for_reload(&mut cmd_rx).await {
                    return;
                }
                continue;
            }
        };

        match run_with_config(app.clone(), state.clone(), cfg, &mut cmd_rx).await {
            NextAction::Reload => continue,
            NextAction::Backfill => {
                // The backfill path also reloads config afterward — falls
                // through to the outer loop naturally.
                continue;
            }
            NextAction::Shutdown => return,
        }
    }
}

enum NextAction {
    Reload,
    Backfill,
    Shutdown,
}

async fn run_with_config(
    app: AppHandle,
    state: Arc<State>,
    cfg: Config,
    cmd_rx: &mut mpsc::Receiver<WorkerCmd>,
) -> NextAction {
    let folder = PathBuf::from(&cfg.replays_folder);
    if !folder.is_dir() {
        emit_status(
            &app,
            "error",
            format!("Replays folder not found: {}", folder.display()),
        );
        return match wait_for_signal(cmd_rx).await {
            Some(WorkerCmd::Shutdown) => NextAction::Shutdown,
            _ => NextAction::Reload,
        };
    }

    // Snapshot every existing .osr as SKIPPED_HISTORIC so the watcher only
    // touches genuinely-new plays. Matches Python's `cmd_run` guarantee.
    snapshot_historic(&state, &folder);

    let (watcher, mut file_rx) = match watch_folder(&folder) {
        Ok((w, rx)) => (w, rx),
        Err(e) => {
            emit_status(&app, "error", format!("Watcher failed: {}", e));
            return match wait_for_signal(cmd_rx).await {
                Some(WorkerCmd::Shutdown) => NextAction::Shutdown,
                _ => NextAction::Reload,
            };
        }
    };

    let client = http::build_client();
    emit_status(
        &app,
        "watching",
        format!("Watching {}", folder.display()),
    );

    // Whoami — best-effort, fires the frontend event even on failure so
    // the sidebar shows "not signed in" vs. staying blank forever.
    let who = http::whoami(&client, &cfg).await;
    let _ = app.emit("whoami-changed", &who);

    // Push initial stats + a snapshot of the most recent activity so the
    // UI has something to render immediately.
    push_stats(&app, &state);
    push_recent(&app, &state);

    // Fallback poll timer — catches missed OS events and covers cases
    // where notify doesn't fire (network shares, sleep/resume, etc.).
    let mut poll = interval(Duration::from_secs(cfg.poll_interval_s));
    // First tick fires immediately; skip it — the snapshot already
    // covered "what's on disk right now".
    poll.tick().await;

    loop {
        tokio::select! {
            biased;
            Some(cmd) = cmd_rx.recv() => match cmd {
                WorkerCmd::Shutdown => {
                    drop(watcher);
                    return NextAction::Shutdown;
                }
                WorkerCmd::Reload => {
                    drop(watcher);
                    return NextAction::Reload;
                }
                WorkerCmd::Backfill => {
                    // For backfill we don't wipe SKIPPED_HISTORIC rows
                    // — instead we upload every .osr regardless of state.
                    // That's the "explicit historic import" behavior.
                    run_backfill(&app, &state, &client, &cfg, &folder).await;
                    // Fall through to keep watching.
                }
            },

            Some(new_path) = file_rx.recv() => {
                // osu! sometimes writes .osr in two flushes — brief settle
                // window before we read the bytes.
                sleep(Duration::from_millis(500)).await;
                if !state.known(&file_name_of(&new_path)) {
                    process_one(&app, &state, &client, &cfg, &new_path).await;
                }
            }

            _ = poll.tick() => {
                if let Ok(entries) = std::fs::read_dir(&folder) {
                    for e in entries.flatten() {
                        let p = e.path();
                        if p.extension().and_then(|s| s.to_str())
                            .map(|s| s.eq_ignore_ascii_case("osr")).unwrap_or(false)
                            && !state.known(&file_name_of(&p))
                        {
                            process_one(&app, &state, &client, &cfg, &p).await;
                        }
                    }
                }
            }
        }
    }
}

/// Snapshot every existing .osr in the folder as SKIPPED_HISTORIC — same
/// guarantee as the Python side: `run` never automatically uploads
/// pre-existing plays. Users must run backfill explicitly.
fn snapshot_historic(state: &State, folder: &Path) {
    let Ok(entries) = std::fs::read_dir(folder) else { return; };
    for e in entries.flatten() {
        let p = e.path();
        if !p.extension().and_then(|s| s.to_str())
            .map(|s| s.eq_ignore_ascii_case("osr")).unwrap_or(false)
        {
            continue;
        }
        let name = file_name_of(&p);
        if !state.known(&name) {
            let _ = state.record(&name, "", None, Some("SKIPPED_HISTORIC"), None, None, None);
        }
    }
}

async fn process_one(
    app: &AppHandle,
    state: &Arc<State>,
    client: &reqwest::Client,
    cfg: &Config,
    path: &Path,
) {
    let name = file_name_of(path);
    emit_status(app, "uploading", format!("Uploading {}", name));

    let mut delay = Duration::from_secs(2);
    let max_delay = Duration::from_secs(60);

    loop {
        let outcome = http::upload_one(client, cfg, path).await;

        if outcome.ok {
            let hash = state::hash_head(path).unwrap_or_default();
            let sm = outcome.summary.as_ref();
            let map_title = sm.and_then(|s| s.get("map_title")).and_then(|v| v.as_str()).map(String::from);
            let map_version = sm.and_then(|s| s.get("map_version")).and_then(|v| v.as_str()).map(String::from);
            let mods = sm.and_then(|s| s.get("mods")).and_then(|v| v.as_str()).map(String::from);
            let accuracy = sm.and_then(|s| s.get("accuracy")).and_then(|v| v.as_f64());
            let _ = state.record(
                &name, &hash, outcome.replay_id,
                map_title.as_deref(), map_version.as_deref(), mods.as_deref(), accuracy,
            );
            emit_activity(app, &name, map_title.as_deref(), mods.as_deref(), accuracy, "uploaded");
            push_stats(app, state);
            push_recent(app, state);
            emit_status(app, "watching", format!("Watching {}", cfg.replays_folder));
            return;
        }

        match outcome.skip_reason.as_deref() {
            Some("duplicate") | Some("foreign_replay") => {
                let hash = state::hash_head(path).unwrap_or_default();
                let _ = state.record(&name, &hash, None, None, None, None, None);
                emit_activity(app, &name, None, None, None, "skipped");
                push_stats(app, state);
                push_recent(app, state);
                emit_status(app, "watching", format!("Watching {}", cfg.replays_folder));
                return;
            }
            Some("unauth") => {
                emit_status(
                    app,
                    "error",
                    "Token unauthorized. Open Settings to re-enter your token.".to_string(),
                );
                emit_activity(app, &name, None, None, None, "failed");
                return;
            }
            _ => {}
        }

        if !outcome.retryable {
            let err = outcome.error.unwrap_or_else(|| "unknown".to_string());
            emit_activity(app, &name, None, None, None, "failed");
            emit_status(app, "error", format!("{}: {}", name, err));
            return;
        }

        // Retry with exponential backoff, capped at 60s per attempt.
        emit_status(
            app,
            "uploading",
            format!("Retrying {} in {}s ({})", name, delay.as_secs(),
                    outcome.error.as_deref().unwrap_or("network error")),
        );
        sleep(delay).await;
        delay = std::cmp::min(delay * 2, max_delay);
    }
}

async fn run_backfill(
    app: &AppHandle,
    state: &Arc<State>,
    client: &reqwest::Client,
    cfg: &Config,
    folder: &Path,
) {
    emit_status(app, "uploading", "Backfilling…".to_string());
    let Ok(entries) = std::fs::read_dir(folder) else { return; };
    let mut files: Vec<PathBuf> = entries
        .flatten()
        .map(|e| e.path())
        .filter(|p| {
            p.extension()
                .and_then(|s| s.to_str())
                .map(|s| s.eq_ignore_ascii_case("osr"))
                .unwrap_or(false)
        })
        .collect();
    files.sort();
    for p in files {
        // For backfill we ignore SKIPPED_HISTORIC rows — treat those as
        // "we haven't actually uploaded this yet, please do." Rows with
        // replay_id set are real prior uploads and stay untouched.
        let name = file_name_of(&p);
        if state.uploaded(&name) {
            continue;
        }
        process_one(app, state, client, cfg, &p).await;
    }
    emit_status(app, "watching", format!("Watching {}", cfg.replays_folder));
}

async fn wait_for_reload(rx: &mut mpsc::Receiver<WorkerCmd>) -> bool {
    while let Some(cmd) = rx.recv().await {
        match cmd {
            WorkerCmd::Reload => return true,
            WorkerCmd::Shutdown => return false,
            _ => {}
        }
    }
    false
}

async fn wait_for_signal(rx: &mut mpsc::Receiver<WorkerCmd>) -> Option<WorkerCmd> {
    rx.recv().await
}

fn file_name_of(p: &Path) -> String {
    p.file_name()
        .and_then(|n| n.to_str())
        .unwrap_or("unknown.osr")
        .to_string()
}

fn now_iso() -> String {
    chrono::Utc::now().to_rfc3339()
}

fn emit_status(app: &AppHandle, s: &'static str, msg: impl Into<String>) {
    let _ = app.emit(
        "status-changed",
        StatusPayload { state: s, message: msg.into(), since: now_iso() },
    );
}

fn emit_activity(
    app: &AppHandle,
    file_name: &str,
    map_title: Option<&str>,
    mods: Option<&str>,
    accuracy: Option<f64>,
    status: &'static str,
) {
    let _ = app.emit(
        "activity-added",
        ActivityRow {
            file_name: file_name.to_string(),
            map_title: map_title.map(String::from),
            mods: mods.map(String::from),
            accuracy,
            status,
            at: chrono::Utc::now().format("%H:%M:%S").to_string(),
        },
    );
}

fn push_stats(app: &AppHandle, state: &State) {
    if let Ok(s) = state.stats() {
        let _ = app.emit("stats-changed", s);
    }
}

fn push_recent(app: &AppHandle, state: &State) {
    if let Ok(rows) = state.recent(50, false) {
        let _ = app.emit("recent-changed", rows);
    }
}

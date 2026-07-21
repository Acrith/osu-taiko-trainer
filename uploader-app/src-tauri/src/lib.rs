//! Tauri app entry point — sets up shared state, spawns the background
//! worker, registers commands + plugins.

mod config;
mod state;
mod http;
mod watcher;
mod worker;
mod commands;

use std::sync::Arc;
use tauri::Manager;

#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
    tauri::Builder::default()
        .plugin(tauri_plugin_opener::init())
        .plugin(tauri_plugin_dialog::init())
        .setup(|app| {
            // Open the state DB in the shared config dir. If it fails
            // (permissions, disk full), we log to stderr — the UI still
            // works, just without persistence.
            let state = Arc::new(
                state::State::open(&config::state_path())
                    .expect("open state DB"),
            );

            // Spawn the background watcher/uploader task. It emits
            // `status-changed`, `activity-added`, `stats-changed`,
            // `recent-changed`, and `whoami-changed` events which the JS
            // side subscribes to.
            let worker_tx = worker::spawn(app.handle().clone(), state.clone());

            app.manage(commands::AppState { state, worker_tx });
            Ok(())
        })
        .invoke_handler(tauri::generate_handler![
            commands::get_config,
            commands::save_config,
            commands::detect_replays_folder,
            commands::default_server_url,
            commands::get_stats,
            commands::get_recent,
            commands::fetch_whoami,
            commands::backfill,
        ])
        .run(tauri::generate_context!())
        .expect("error while running tauri application");
}

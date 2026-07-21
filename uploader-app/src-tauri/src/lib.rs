//! Tauri app entry point — sets up shared state, spawns the background
//! worker, registers commands + plugins.

mod config;
mod state;
mod http;
mod watcher;
mod worker;
mod commands;
mod logging;

use std::sync::Arc;
use tauri::Manager;

#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
    // Wire up file logging + a panic hook FIRST so any later failure
    // (state DB open, plugin init, watcher setup) actually leaves a
    // trace at ~/.taiko-trainer/uploader.log. Release builds have no
    // console attached (windows_subsystem = "windows"), so stderr goes
    // nowhere; without this hook every panic is silent.
    logging::install(config::log_path());

    let result = std::panic::catch_unwind(|| {
        tauri::Builder::default()
            .plugin(tauri_plugin_opener::init())
            .plugin(tauri_plugin_dialog::init())
            .setup(|app| {
                logging::log_line("setup: opening state DB");
                let state = Arc::new(
                    state::State::open(&config::state_path())
                        .map_err(|e| {
                            logging::log_line(&format!("state DB open FAILED: {}", e));
                            e
                        })?,
                );
                logging::log_line("setup: state DB opened, spawning worker");

                let worker_tx = worker::spawn(app.handle().clone(), state.clone());

                app.manage(commands::AppState { state, worker_tx });
                logging::log_line("setup: complete");
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
    });

    if let Err(e) = result {
        // The panic hook already wrote the panic. This just makes sure
        // we don't return normally after catching a top-level panic.
        logging::log_line(&format!("caught top-level panic: {:?}", type_name_of_panic(&e)));
    }
    logging::log_line("=== uploader exiting ===");
}

fn type_name_of_panic(_e: &Box<dyn std::any::Any + Send>) -> &'static str {
    "opaque"
}

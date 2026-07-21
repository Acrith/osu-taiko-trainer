// Entry point shared by desktop `main.rs` and any future mobile shell.
// This first scaffold just opens a Tauri window that hosts the Svelte UI —
// no folder-watching, no upload loop, no state DB yet. Those land in
// follow-up commits (see uploader-app/README.md).
//
// The single `#[tauri::command] fn ping` exists so we can smoke-test the
// JS-Rust bridge from DevTools during development.

#[tauri::command]
fn ping() -> &'static str {
    "pong"
}

#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
    tauri::Builder::default()
        .plugin(tauri_plugin_opener::init())
        .invoke_handler(tauri::generate_handler![ping])
        .run(tauri::generate_context!())
        .expect("error while running tauri application");
}

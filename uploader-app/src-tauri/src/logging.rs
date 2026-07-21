//! Minimal file logger + panic hook so a release build actually leaves
//! a trail when something goes wrong. Writes plain lines to
//! `~/.taiko-trainer/uploader.log` — timestamped, append-mode.
//!
//! Intentionally NOT a full `tracing` setup: this is 40 lines of
//! std-only code, produces a file we can `Read` remotely, and doesn't
//! need CI to install anything extra.

use std::fs::OpenOptions;
use std::io::Write;
use std::path::PathBuf;
use std::sync::Mutex;
use std::sync::OnceLock;

static LOG_PATH: OnceLock<PathBuf> = OnceLock::new();
// Serialize appends so interleaved writes from the panic hook + normal
// log_line calls never garble a line. The file's small and appends are
// cheap; a real logging framework would be overkill.
static LOG_LOCK: Mutex<()> = Mutex::new(());

/// Install the panic hook + record the log path. Call once from `run()`.
pub fn install(path: PathBuf) {
    let _ = std::fs::create_dir_all(path.parent().unwrap_or(&PathBuf::from(".")));
    LOG_PATH.set(path).ok();
    log_line("=== uploader starting ===");

    std::panic::set_hook(Box::new(|info| {
        let payload = if let Some(s) = info.payload().downcast_ref::<&'static str>() {
            (*s).to_string()
        } else if let Some(s) = info.payload().downcast_ref::<String>() {
            s.clone()
        } else {
            "<non-string panic payload>".to_string()
        };
        let loc = info
            .location()
            .map(|l| format!("{}:{}:{}", l.file(), l.line(), l.column()))
            .unwrap_or_else(|| "<unknown location>".to_string());
        log_line(&format!("PANIC at {} — {}", loc, payload));
    }));
}

/// Append one line to the log file. Silently no-ops on I/O error so a
/// bad log write can't cascade into a bigger failure.
pub fn log_line(msg: &str) {
    let Some(path) = LOG_PATH.get() else { return; };
    let _guard = LOG_LOCK.lock();
    let ts = chrono::Utc::now().format("%Y-%m-%d %H:%M:%S");
    if let Ok(mut f) = OpenOptions::new().create(true).append(true).open(path) {
        let _ = writeln!(f, "[{}] {}", ts, msg);
    }
}

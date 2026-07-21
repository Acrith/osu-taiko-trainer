//! Async wrapper around the `notify` crate. Feeds new-file paths onto a
//! tokio mpsc channel; the worker task on the other end debounces + uploads.

use notify::{Config, Event, EventKind, RecommendedWatcher, RecursiveMode, Watcher};
use std::path::{Path, PathBuf};
use tokio::sync::mpsc;

/// Start watching `folder` and return a receiver of paths of newly-created
/// or renamed-into `.osr` files. The `notify` watcher stays owned by this
/// call and lives as long as the returned `_watcher` — drop it to stop.
///
/// The watcher fires from a `notify`-owned thread; we forward events over
/// a std::sync::mpsc channel first and then into a tokio channel via a
/// tiny bridge task, because `notify`'s watcher constructor demands a
/// non-async closure.
pub fn watch_folder(
    folder: &Path,
) -> Result<(RecommendedWatcher, mpsc::Receiver<PathBuf>), String> {
    let (sync_tx, sync_rx) = std::sync::mpsc::channel::<PathBuf>();
    let mut watcher = RecommendedWatcher::new(
        move |res: Result<Event, notify::Error>| {
            if let Ok(ev) = res {
                if !matches!(ev.kind, EventKind::Create(_) | EventKind::Modify(_)) {
                    return;
                }
                for path in ev.paths {
                    if path
                        .extension()
                        .and_then(|s| s.to_str())
                        .map(|s| s.eq_ignore_ascii_case("osr"))
                        .unwrap_or(false)
                    {
                        let _ = sync_tx.send(path);
                    }
                }
            }
        },
        Config::default(),
    )
    .map_err(|e| format!("watcher: {}", e))?;

    watcher
        .watch(folder, RecursiveMode::NonRecursive)
        .map_err(|e| format!("watch {}: {}", folder.display(), e))?;

    // Bridge the sync channel → async channel so the worker can `select!`
    // on it alongside other async signals.
    let (async_tx, async_rx) = mpsc::channel::<PathBuf>(64);
    std::thread::spawn(move || {
        while let Ok(p) = sync_rx.recv() {
            // Blocking send is fine — this thread only exists to feed the
            // worker. If the worker drops the receiver we exit cleanly.
            if async_tx.blocking_send(p).is_err() {
                break;
            }
        }
    });

    Ok((watcher, async_rx))
}

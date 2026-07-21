//! Folder scanning for the Replays screen — walks the replays folder,
//! joins each .osr entry with the state DB, and returns a flat list the
//! frontend can filter + select from. Also computes each file's
//! content_hash so the frontend can cross-reference with the server's
//! `/api/v1/me/replays` list.

use crate::state::{hash_head, State};
use serde::Serialize;
use std::path::Path;
use std::time::UNIX_EPOCH;

#[derive(Clone, Debug, Serialize)]
pub struct FolderEntry {
    pub filename: String,
    /// ISO 8601 UTC timestamp of the last modification. None on Windows FSes
    /// that don't report mtime for the entry (rare) — treat as unknown.
    pub modified_at: Option<String>,
    pub size_bytes: u64,
    /// Classification against the LOCAL state DB alone:
    ///   "never_seen" — not in state DB at all
    ///   "historic"   — was here at first `run` and marked SKIPPED_HISTORIC
    ///   "uploaded"   — successfully sent, server assigned a replay_id
    ///   "skipped"    — server said duplicate / foreign / other terminal skip
    /// The frontend refines this with server data — a "historic" row can
    /// upgrade to "uploaded" if its content_hash matches something on
    /// the server's list.
    pub state: &'static str,
    pub replay_id: Option<i64>,
    pub map_title: Option<String>,
    pub mods: Option<String>,
    pub accuracy: Option<f64>,
    /// sha256 of the first 512 bytes — the fingerprint we cross-reference
    /// with `/api/v1/me/replays` server-side. None on read errors (which
    /// shouldn't happen for a file we just found via read_dir).
    pub content_hash: Option<String>,
}

/// Enumerate all `.osr` files in `folder` and classify each against the
/// state DB. Sorted by modified_at descending (newest plays first) so the
/// UI's default view is the most interesting one.
pub fn scan(state: &State, folder: &Path) -> Vec<FolderEntry> {
    let Ok(entries) = std::fs::read_dir(folder) else {
        return Vec::new();
    };
    let mut out: Vec<FolderEntry> = Vec::new();
    for e in entries.flatten() {
        let path = e.path();
        if !path
            .extension()
            .and_then(|s| s.to_str())
            .map(|s| s.eq_ignore_ascii_case("osr"))
            .unwrap_or(false)
        {
            continue;
        }
        let Some(name) = path.file_name().and_then(|n| n.to_str()).map(String::from) else {
            continue;
        };

        let meta = e.metadata().ok();
        let size_bytes = meta.as_ref().map(|m| m.len()).unwrap_or(0);
        let modified_at = meta
            .as_ref()
            .and_then(|m| m.modified().ok())
            .and_then(|t| t.duration_since(UNIX_EPOCH).ok())
            .map(|d| {
                chrono::DateTime::<chrono::Utc>::from_timestamp(d.as_secs() as i64, 0)
                    .map(|dt| dt.to_rfc3339())
                    .unwrap_or_default()
            });

        // Join with state DB — one SELECT per file. Fast enough for a few
        // thousand entries; if this ever gets slow we can switch to a
        // single "load all state rows into a HashMap" pass.
        let (state_label, replay_id, map_title, mods, accuracy) = match state.lookup(&name) {
            None => ("never_seen", None, None, None, None),
            Some(r) => {
                let label = if r.replay_id.is_some() {
                    "uploaded"
                } else if r.map_title.as_deref() == Some("SKIPPED_HISTORIC") {
                    "historic"
                } else {
                    "skipped"
                };
                (label, r.replay_id, r.map_title, r.mods, r.accuracy)
            }
        };

        // Cheap fingerprint of the first 512 bytes. Only ~1ms per file
        // and lets the frontend definitively say "this .osr is on the
        // server" without re-uploading + eating a 409.
        let content_hash = hash_head(&path).ok();

        out.push(FolderEntry {
            filename: name,
            modified_at,
            size_bytes,
            state: state_label,
            replay_id,
            map_title,
            mods,
            accuracy,
            content_hash,
        });
    }

    // Newest first — most players want to review recent plays first.
    out.sort_by(|a, b| b.modified_at.cmp(&a.modified_at));
    out
}

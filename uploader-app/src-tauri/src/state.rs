//! SQLite-backed tracker for which files have been uploaded — schema
//! matches the Python uploader's `uploader.state.db` so a user's state
//! carries over unchanged if they were previously on the tkinter build.

use rusqlite::{params, Connection};
use serde::Serialize;
use std::path::Path;
use std::sync::Mutex;

pub struct State {
    conn: Mutex<Connection>,
}

#[derive(Clone, Debug, Serialize)]
pub struct Record {
    pub filename: String,
    pub content_hash: String,
    pub replay_id: Option<i64>,
    pub uploaded_at: String,
    pub map_title: Option<String>,
    pub map_version: Option<String>,
    pub mods: Option<String>,
    pub accuracy: Option<f64>,
}

#[derive(Clone, Debug, Serialize)]
pub struct Stats {
    pub total: i64,
    pub uploaded: i64,
    pub skipped_historic: i64,
}

const SCHEMA: &str = "
CREATE TABLE IF NOT EXISTS uploaded (
    filename       TEXT PRIMARY KEY,
    content_hash   TEXT NOT NULL,
    replay_id      INTEGER,
    uploaded_at    TEXT NOT NULL,
    map_title      TEXT,
    map_version    TEXT,
    mods           TEXT,
    accuracy       REAL
);
";

impl State {
    pub fn open(path: &Path) -> Result<Self, String> {
        if let Some(parent) = path.parent() {
            std::fs::create_dir_all(parent).map_err(|e| e.to_string())?;
        }
        let conn = Connection::open(path).map_err(|e| e.to_string())?;
        conn.execute_batch(SCHEMA).map_err(|e| e.to_string())?;
        Ok(Self { conn: Mutex::new(conn) })
    }

    pub fn known(&self, filename: &str) -> bool {
        let conn = self.conn.lock().unwrap();
        conn.query_row(
            "SELECT 1 FROM uploaded WHERE filename = ?1",
            params![filename],
            |_| Ok(()),
        ).is_ok()
    }

    /// True only when a row exists AND we got a replay_id back — i.e. the
    /// server actually stored this replay. SKIPPED_HISTORIC and skip-reason
    /// rows return false. Used by backfill to skip already-uploaded files
    /// while still picking up historic ones the user opts in to.
    pub fn uploaded(&self, filename: &str) -> bool {
        let conn = self.conn.lock().unwrap();
        conn.query_row(
            "SELECT 1 FROM uploaded WHERE filename = ?1 AND replay_id IS NOT NULL",
            params![filename],
            |_| Ok(()),
        ).is_ok()
    }

    /// Insert or replace a row. `uploaded_at` is stamped by SQLite so it's
    /// consistent with the Python side.
    #[allow(clippy::too_many_arguments)]
    pub fn record(
        &self,
        filename: &str,
        content_hash: &str,
        replay_id: Option<i64>,
        map_title: Option<&str>,
        map_version: Option<&str>,
        mods: Option<&str>,
        accuracy: Option<f64>,
    ) -> Result<(), String> {
        let conn = self.conn.lock().unwrap();
        conn.execute(
            "INSERT OR REPLACE INTO uploaded
             (filename, content_hash, replay_id, uploaded_at,
              map_title, map_version, mods, accuracy)
             VALUES (?1, ?2, ?3, datetime('now'), ?4, ?5, ?6, ?7)",
            params![filename, content_hash, replay_id, map_title, map_version, mods, accuracy],
        ).map_err(|e| e.to_string())?;
        Ok(())
    }

    /// Most-recent rows. `include_skipped=false` filters out the
    /// SKIPPED_HISTORIC snapshot markers so the UI shows real uploads.
    pub fn recent(&self, limit: i64, include_skipped: bool) -> Result<Vec<Record>, String> {
        let conn = self.conn.lock().unwrap();
        let sql = if include_skipped {
            "SELECT filename, content_hash, replay_id, uploaded_at, \
             map_title, map_version, mods, accuracy \
             FROM uploaded ORDER BY uploaded_at DESC LIMIT ?1"
        } else {
            "SELECT filename, content_hash, replay_id, uploaded_at, \
             map_title, map_version, mods, accuracy \
             FROM uploaded \
             WHERE COALESCE(map_title,'') != 'SKIPPED_HISTORIC' \
             ORDER BY uploaded_at DESC LIMIT ?1"
        };
        let mut stmt = conn.prepare(sql).map_err(|e| e.to_string())?;
        let rows = stmt
            .query_map(params![limit], |row| {
                Ok(Record {
                    filename: row.get(0)?,
                    content_hash: row.get(1)?,
                    replay_id: row.get(2)?,
                    uploaded_at: row.get(3)?,
                    map_title: row.get(4)?,
                    map_version: row.get(5)?,
                    mods: row.get(6)?,
                    accuracy: row.get(7)?,
                })
            })
            .map_err(|e| e.to_string())?;
        rows.collect::<Result<Vec<_>, _>>()
            .map_err(|e| e.to_string())
    }

    pub fn stats(&self) -> Result<Stats, String> {
        let conn = self.conn.lock().unwrap();
        let total: i64 = conn
            .query_row("SELECT COUNT(*) FROM uploaded", [], |r| r.get(0))
            .map_err(|e| e.to_string())?;
        let skipped_historic: i64 = conn
            .query_row(
                "SELECT COUNT(*) FROM uploaded WHERE map_title = 'SKIPPED_HISTORIC'",
                [],
                |r| r.get(0),
            )
            .map_err(|e| e.to_string())?;
        let uploaded: i64 = conn
            .query_row(
                "SELECT COUNT(*) FROM uploaded WHERE replay_id IS NOT NULL",
                [],
                |r| r.get(0),
            )
            .map_err(|e| e.to_string())?;
        Ok(Stats { total, uploaded, skipped_historic })
    }
}

/// sha256 of the first N bytes of the file. Used to disambiguate osr files
/// that share a filename (rare — osu! stamps names with timestamps — but
/// still possible after moves/renames).
pub fn hash_head(path: &Path) -> Result<String, String> {
    use sha2::{Digest, Sha256};
    use std::io::Read;
    let mut file = std::fs::File::open(path).map_err(|e| e.to_string())?;
    let mut buf = [0u8; 512];
    let n = file.read(&mut buf).map_err(|e| e.to_string())?;
    let mut hasher = Sha256::new();
    hasher.update(&buf[..n]);
    Ok(format!("{:x}", hasher.finalize()))
}

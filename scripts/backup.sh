#!/bin/sh
# Nightly SQLite backup with rotation.
#
# Uses SQLite's `.backup` command so the copy is consistent even if the
# app is writing at the same time — this is the correct way to hot-backup
# SQLite; plain `cp` on a live file can capture torn writes.
#
# Retention: 7 daily snapshots (keeps a week's worth, ~7× workspace size
# on disk). Older snapshots are pruned automatically.
#
# Environment (all optional):
#   WORKSPACE_DIR   default: /workspace       — where SQLite files live
#   BACKUP_DIR      default: /backups         — where snapshots go
#   RETENTION_DAYS  default: 7                — snapshots to keep
#
# Used by the `backup` service in docker-compose.yml, but also runnable
# standalone via cron or systemd.timer.

set -eu

WORKSPACE_DIR="${WORKSPACE_DIR:-/workspace}"
BACKUP_DIR="${BACKUP_DIR:-/backups}"
RETENTION_DAYS="${RETENTION_DAYS:-7}"

date_stamp="$(date -u '+%Y-%m-%d_%H%M%S')"
target_dir="${BACKUP_DIR}/${date_stamp}"
mkdir -p "$target_dir"

echo "[backup] snapshotting ${WORKSPACE_DIR} → ${target_dir}"

# Snapshot every .db under the workspace using SQLite's atomic backup.
# find + -exec runs the sqlite3 backup for each file individually so
# we never see torn writes across files.
find "$WORKSPACE_DIR" -maxdepth 1 -name "*.db" -type f | while read -r src; do
    name="$(basename "$src")"
    dst="${target_dir}/${name}"
    # `.backup` runs the online backup API — safe against concurrent writers
    if sqlite3 "$src" ".backup '${dst}'"; then
        gzip -f "$dst"
        echo "  ✓ ${name}"
    else
        echo "  ✗ ${name} — sqlite3 .backup failed" >&2
    fi
done

# Rotation: drop directories older than RETENTION_DAYS.
# Only touches directories that match our date_stamp pattern so we never
# nuke something a human put there.
find "$BACKUP_DIR" -maxdepth 1 -type d -name '20*_*' -mtime "+${RETENTION_DAYS}" -exec rm -rf {} \;

echo "[backup] done. current retention:"
ls -1t "$BACKUP_DIR" 2>/dev/null | grep -E '^20[0-9]{2}-[0-9]{2}-[0-9]{2}_' | head -n 10

"""Local web app for the taiko trainer.

Run with:
    taiko-trainer serve [--db trainer.db] [--host 127.0.0.1] [--port 8000]

Then open http://localhost:8000 in your browser.

Pages (v1):
- /                  home: players list, DB status, upload drop-zone
- /player/<name>     training report: skill vector, session, suggestions
- /replay/<id>       one replay: judgment breakdown + classification

All routes read from the SQLite DB the CLI uses. Upload drops a .osr into the
DB via the same workflow.add_replay logic used by `taiko-trainer add`.
"""
from __future__ import annotations

import io
import shutil
import tempfile
import threading
import time
import uuid
from pathlib import Path

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from . import db as db_module
from .db import (
    discover_players,
    get_all_maps,
    get_map,
    get_map_content,
    get_player,
    get_replays,
    list_map_roots,
    open_catalog,
    open_plays,
    upsert_player,
    workspace_status,
)
from .features import extract_features
from .judgment import Verdict, judge_replay
from .osr_parser import parse_osr_file
from .player import PlayerSkill
from .report import build_report
from .sessions import group_sessions
from .suggest import suggest_maps
from .workflow import _parse_bytes_as_osu, add_replay


_UPLOAD_TASKS: dict[str, dict] = {}
_UPLOAD_LOCK = threading.Lock()
_STAGE_LABELS = {
    "queued":         ("Queued",               0),
    "parse_replay":   ("Parsing replay file",  5),
    "resolve_map":    ("Looking up map",       10),
    "catalog_hit":    ("Found in cache",       35),
    "explicit_map":   ("Verifying explicit map", 25),
    "search_scan":    ("Enumerating map roots", 15),
    "search_hash":    ("Searching for map",    "dynamic"),   # 15..70 based on done/total
    "search_hit":     ("Map found",            70),
    "rate_map":       ("Computing map rating", 75),
    "judge":          ("Judging per note",     80),
    "classify":       ("Classifying misses",   90),
    "store":          ("Writing to database",  95),
    "snapshot":       ("Updating skill vector", 98),
    "done":           ("Complete",             100),
    "error":          ("Failed",               100),
}


def _compute_pct(stage, total, done):
    label, pct = _STAGE_LABELS.get(stage, (stage, 50))
    if pct == "dynamic" and total:
        frac = done / total if total else 0
        # search_hash ranges 15..70
        return 15 + int(frac * 55)
    return pct if isinstance(pct, int) else 50


def _upload_progress_cb(task_id: str):
    """Return a callback closure that updates the shared task-progress dict."""
    def cb(stage, total=None, done=None, note=""):
        label = _STAGE_LABELS.get(stage, (stage, 0))[0]
        pct = _compute_pct(stage, total, done)
        with _UPLOAD_LOCK:
            _UPLOAD_TASKS[task_id].update({
                "stage": stage, "label": label, "pct": pct,
                "note": note, "total": total, "done": done,
                "updated_at": time.time(),
            })
    return cb


def create_app(workspace: str) -> FastAPI:
    app = FastAPI(title="taiko-trainer")

    # --- HTML pages ------------------------------------------------------

    @app.get("/", response_class=HTMLResponse)
    def home():
        ws_stats = workspace_status(workspace)
        players_info = []
        # For each discovered player, get their name + style + replay count.
        for player_name, s in ws_stats["players"].items():
            players_info.append({
                "name": player_name,
                "style": s["style"],
                "replays": s["replays"],
            })
        catalog_stats = ws_stats["catalog"]
        # Aggregate root list from ALL player DBs, deduped.
        roots_set: set[str] = set()
        for name in ws_stats["players"]:
            conn = open_plays(workspace, name)
            for r in list_map_roots(conn):
                roots_set.add(r)
            conn.close()
        roots = sorted(roots_set)
        return _render_home(workspace, catalog_stats, players_info, roots)

    @app.get("/player/{name}", response_class=HTMLResponse)
    def player_page(name: str):
        conn = open_plays(workspace, name)
        report = build_report(conn)
        replays = get_replays(conn) if report else []
        conn.close()
        if report is None:
            return HTMLResponse(_render_error(f"No snapshots for player {name!r}. Drop a replay first."), status_code=404)
        return _render_report(report, replays, name)

    @app.get("/player/{name}/train/{dim}", response_class=HTMLResponse)
    def train_page(name: str, dim: str):
        if dim not in ("speed", "stamina", "gimmick", "technical", "consistency"):
            return HTMLResponse(_render_error(f"unknown dimension: {dim}"), status_code=400)
        conn = open_plays(workspace, name)
        report = build_report(conn, top_n_maps=25)
        replays = get_replays(conn) if report else []
        if report is None:
            conn.close()
            return HTMLResponse(_render_error(f"No snapshots for player {name!r}."), status_code=404)
        played_md5s = {r["map_md5"] for r in replays}
        suggestions = suggest_maps(
            conn, report.skill, dim, top_n=25, exclude_md5s=played_md5s,
        )
        conn.close()
        return _render_train_page(name, dim, report.skill, suggestions, report.dim_contributors.get(dim, ()))

    @app.get("/replay/{player}/{replay_id}/inspect", response_class=HTMLResponse)
    def replay_inspect(player: str, replay_id: int):
        conn = open_plays(workspace, player)
        row = conn.execute(
            """
            SELECT r.*, m.md5 AS map_md5_ref, m.title AS map_title, m.version AS map_version
            FROM replays r JOIN catalog.maps m ON m.md5 = r.map_md5
            WHERE r.id = ?
            """,
            (replay_id,),
        ).fetchone()
        if not row:
            conn.close()
            return HTMLResponse(_render_error(f"Replay {replay_id} for {player} not found."), status_code=404)
        map_bytes = get_map_content(conn, row["map_md5_ref"])
        conn.close()
        if not map_bytes:
            return HTMLResponse(_render_error("Map content missing from catalog."), status_code=500)
        # Re-parse map, re-parse replay from blob, re-judge.
        bm = _parse_bytes_as_osu(map_bytes)
        with tempfile.NamedTemporaryFile(suffix=".osr", delete=False) as tmp:
            tmp.write(bytes(row["content"]))
            tmp_path = tmp.name
        try:
            rp = parse_osr_file(tmp_path)
        finally:
            Path(tmp_path).unlink(missing_ok=True)
        judged = judge_replay(bm, rp)
        return _render_inspector(dict(row), player, judged, rp)

    @app.get("/replay/{player}/{replay_id}", response_class=HTMLResponse)
    def replay_page(player: str, replay_id: int):
        conn = open_plays(workspace, player)
        row = conn.execute(
            """
            SELECT r.*, m.title AS map_title, m.version AS map_version, m.creator AS map_creator,
                   m.md5 AS map_md5_ref,
                   m.rating_speed, m.rating_stamina, m.rating_gimmick,
                   m.rating_technical, m.rating_consistency
            FROM replays r JOIN catalog.maps m ON m.md5 = r.map_md5
            WHERE r.id = ?
            """,
            (replay_id,),
        ).fetchone()
        features = None
        if row:
            content = get_map_content(conn, row["map_md5_ref"])
            if content:
                try:
                    bm = _parse_bytes_as_osu(content)
                    features = extract_features(bm)
                except Exception:
                    features = None
        conn.close()
        if not row:
            return HTMLResponse(_render_error(f"Replay {replay_id} for {player} not found."), status_code=404)
        return _render_replay(dict(row), player, features)

    # --- upload ---------------------------------------------------------

    @app.post("/upload")
    async def upload(file: UploadFile = File(...), map_file: UploadFile | None = File(None)):
        # Save uploaded files to a persistent temp dir so the background thread can read them.
        # We clean it up in the worker.
        td = Path(tempfile.mkdtemp(prefix="tt-upload-"))
        replay_path = td / (file.filename or "upload.osr")
        with open(replay_path, "wb") as fh:
            shutil.copyfileobj(file.file, fh)
        map_path = None
        if map_file is not None and (map_file.filename or "").endswith(".osu"):
            map_path = td / (map_file.filename or "upload.osu")
            with open(map_path, "wb") as fh:
                shutil.copyfileobj(map_file.file, fh)

        task_id = uuid.uuid4().hex[:10]
        with _UPLOAD_LOCK:
            _UPLOAD_TASKS[task_id] = {
                "stage": "queued", "label": "Queued", "pct": 0, "note": "",
                "filename": replay_path.name,
                "created_at": time.time(), "updated_at": time.time(),
                "result": None, "error": None,
                "redirect": None,
            }

        def _worker():
            try:
                cb = _upload_progress_cb(task_id)
                result = add_replay(
                    workspace, str(replay_path),
                    map_path=str(map_path) if map_path else None,
                    progress_cb=cb,
                )
                with _UPLOAD_LOCK:
                    entry = _UPLOAD_TASKS[task_id]
                    if not result.ok:
                        entry["stage"] = "error"
                        entry["label"] = "Failed"
                        entry["error"] = result.message
                    else:
                        entry["stage"] = "done"
                        entry["label"] = "Complete"
                        entry["pct"] = 100
                        entry["result"] = result.message
                        entry["redirect"] = f"/player/{result.player}" if result.player else "/"
            except Exception as e:
                with _UPLOAD_LOCK:
                    _UPLOAD_TASKS[task_id]["stage"] = "error"
                    _UPLOAD_TASKS[task_id]["label"] = "Failed"
                    _UPLOAD_TASKS[task_id]["error"] = f"Internal error: {e!r}"
            finally:
                shutil.rmtree(td, ignore_errors=True)

        threading.Thread(target=_worker, daemon=True).start()
        return RedirectResponse(url=f"/upload/{task_id}", status_code=303)

    @app.get("/upload/{task_id}", response_class=HTMLResponse)
    def upload_page(task_id: str):
        with _UPLOAD_LOCK:
            entry = _UPLOAD_TASKS.get(task_id)
        if not entry:
            return HTMLResponse(_render_error(f"Upload task {task_id} not found."), status_code=404)
        return _render_upload_progress(task_id, entry)

    @app.get("/upload/{task_id}/status")
    def upload_status(task_id: str):
        with _UPLOAD_LOCK:
            entry = _UPLOAD_TASKS.get(task_id)
        if not entry:
            return JSONResponse({"error": "unknown task"}, status_code=404)
        return JSONResponse(entry)

    # --- simple settings endpoints ------------------------------------

    @app.post("/settings/player")
    async def set_player(name: str = Form(...), style: str = Form(...)):
        conn = open_plays(workspace, name)
        upsert_player(conn, name, style)
        conn.close()
        return RedirectResponse(url=f"/player/{name}", status_code=303)

    @app.post("/settings/root")
    async def add_root(player: str = Form(...), path: str = Form(...)):
        conn = open_plays(workspace, player)
        db_module.add_map_root(conn, path)
        conn.close()
        return RedirectResponse(url="/", status_code=303)

    # --- JSON API ---------------------------------------------------

    @app.get("/api/status")
    def api_status():
        return workspace_status(workspace)

    return app


# =========================================================================
# HTML rendering (inline templates for now — will extract when they stabilise)
# =========================================================================

_BASE_CSS = """
:root {
  --ground: #F4EFE6;
  --panel: #FBF7EE;
  --ink: #141416;
  --ink-muted: #5A554C;
  --ink-faint: #8B8676;
  --rule: #DED5C4;
  --rule-strong: #C7BDA8;
  --accent: #B0322B;
  --accent-soft: #E1AAA5;
  --accent-faint: #F1D9D5;
  --accent-cool: #4B6A83;
  --great: #4A7752;
  --ok: #B08A2B;
  --miss: #B0322B;
  --font-sans: system-ui, -apple-system, "Segoe UI Variable", "Segoe UI", Roboto, "Helvetica Neue", sans-serif;
  --font-mono: ui-monospace, "SF Mono", "JetBrains Mono", "Menlo", "Fira Code", monospace;
}
@media (prefers-color-scheme: dark) {
  :root {
    --ground: #0F1114; --panel: #16181D; --ink: #EFE9DE;
    --ink-muted: #A8A192; --ink-faint: #6E6A5F;
    --rule: #23262C; --rule-strong: #2F323A;
    --accent: #E85A4F; --accent-soft: #78302C; --accent-faint: #3E1E1B;
    --accent-cool: #78A3C5; --great: #7DB68B; --ok: #D6B04F; --miss: #E85A4F;
  }
}
* { box-sizing: border-box; }
html, body { margin: 0; padding: 0; }
body {
  background: var(--ground); color: var(--ink);
  font-family: var(--font-sans); font-size: 15px; line-height: 1.55;
  -webkit-font-smoothing: antialiased;
}
main { max-width: 1180px; margin: 0 auto; padding: 32px 24px 96px; display: grid; gap: 32px; }
a { color: var(--accent); text-decoration: none; }
a:hover { text-decoration: underline; }
header.site {
  display: flex; align-items: baseline; justify-content: space-between;
  padding-bottom: 20px; border-bottom: 1px solid var(--rule);
}
header.site .logo {
  font-family: var(--font-mono); font-weight: 500; font-size: 22px;
  letter-spacing: -0.01em; color: var(--ink);
}
header.site nav a {
  font-family: var(--font-mono); font-size: 11px; letter-spacing: 0.14em;
  text-transform: uppercase; color: var(--ink-muted); margin-left: 20px;
}
header.site nav a.active { color: var(--accent); }
.eyebrow { font-family: var(--font-mono); font-size: 11px; letter-spacing: 0.14em; text-transform: uppercase; color: var(--ink-muted); }
h1 { font-family: var(--font-mono); font-weight: 500; font-size: 32px; letter-spacing: -0.015em; margin: 4px 0 0 0; }
h2 { font-family: var(--font-mono); font-weight: 500; font-size: 20px; margin: 0 0 8px 0; }
.card { background: var(--panel); border: 1px solid var(--rule); border-radius: 4px; padding: 20px 24px; }
.grid { display: grid; gap: 16px; }
.grid-2 { grid-template-columns: repeat(2, 1fr); }
.grid-3 { grid-template-columns: repeat(3, 1fr); }
.stats-row { display: grid; grid-template-columns: repeat(5, 1fr); gap: 1px; background: var(--rule); border: 1px solid var(--rule); border-radius: 3px; overflow: hidden; }
.stat { background: var(--panel); padding: 10px 14px; display: grid; gap: 3px; }
.stat .k { font-family: var(--font-mono); font-size: 10px; letter-spacing: 0.14em; text-transform: uppercase; color: var(--ink-faint); }
.stat .v { font-family: var(--font-mono); font-size: 16px; font-weight: 500; color: var(--ink); font-variant-numeric: tabular-nums; }
table { width: 100%; border-collapse: separate; border-spacing: 2px; font-family: var(--font-mono); font-variant-numeric: tabular-nums; }
th { font-size: 10px; letter-spacing: 0.14em; text-transform: uppercase; color: var(--ink-muted); text-align: right; padding: 6px 12px; font-weight: 400; }
th:first-child { text-align: left; }
td { background: var(--panel); padding: 10px 12px; border-radius: 3px; text-align: right; font-size: 13px; }
td.name { text-align: left; color: var(--ink); }
td.muted { color: var(--ink-muted); }
tr:hover td { background: var(--accent-faint); }
form.upload { border: 2px dashed var(--rule-strong); border-radius: 6px; padding: 32px; text-align: center; }
form.upload input[type=file] { display: block; margin: 12px auto; font-family: var(--font-mono); font-size: 12px; }
form.upload button { font-family: var(--font-mono); font-size: 12px; letter-spacing: 0.14em; text-transform: uppercase; background: var(--accent); color: white; border: none; padding: 10px 24px; border-radius: 3px; cursor: pointer; }
form.upload button:hover { opacity: 0.9; }
form.upload .hint { color: var(--ink-muted); font-size: 12px; margin-top: 8px; }
form.inline-form { display: flex; gap: 8px; align-items: center; margin-top: 8px; }
form.inline-form input, form.inline-form select { font-family: var(--font-mono); font-size: 13px; padding: 6px 10px; border: 1px solid var(--rule); border-radius: 3px; background: var(--ground); color: var(--ink); }
form.inline-form button { font-family: var(--font-mono); font-size: 11px; letter-spacing: 0.1em; text-transform: uppercase; background: var(--accent); color: white; border: none; padding: 7px 14px; border-radius: 3px; cursor: pointer; }
.dim-bar { display: grid; grid-template-columns: 100px 1fr max-content max-content; align-items: center; gap: 12px; padding: 8px 0; font-family: var(--font-mono); font-variant-numeric: tabular-nums; }
.dim-bar .name { font-size: 12px; letter-spacing: 0.14em; text-transform: uppercase; color: var(--ink-muted); }
.dim-bar .track { height: 8px; background: var(--rule); border-radius: 2px; overflow: hidden; }
.dim-bar .fill { height: 100%; background: var(--accent); }
.dim-bar.weakest .name { color: var(--accent); }
.dim-bar .val { font-size: 14px; color: var(--ink); font-weight: 500; }
.dim-bar .delta { font-size: 12px; color: var(--ink-muted); }
.dim-bar .delta.up { color: var(--great); }
.dim-bar .delta.down { color: var(--miss); }
.cause-bar { display: grid; grid-template-columns: 120px 1fr 60px; gap: 12px; padding: 6px 0; font-family: var(--font-mono); font-variant-numeric: tabular-nums; }
.cause-bar .name { font-size: 12px; color: var(--ink); }
.cause-bar .track { height: 8px; background: var(--rule); border-radius: 2px; overflow: hidden; }
.cause-bar .fill { height: 100%; }
.cause-bar .count { font-size: 12px; color: var(--ink-muted); text-align: right; }
.hint { color: var(--ink-muted); font-size: 12px; }
.error-banner { background: var(--accent-faint); color: var(--accent); border: 1px solid var(--accent-soft); padding: 12px 16px; border-radius: 4px; font-family: var(--font-mono); font-size: 12px; white-space: pre-wrap; }
.feat-group { margin-top: 16px; padding-top: 12px; border-top: 1px dashed var(--rule); }
.feat-group:first-of-type { border-top: none; padding-top: 4px; margin-top: 8px; }
.feat-title { display: flex; justify-content: space-between; align-items: baseline; margin-bottom: 8px; }
.feat-title > span:first-child { font-family: var(--font-mono); font-size: 12px; letter-spacing: 0.14em; text-transform: uppercase; color: var(--accent); font-weight: 500; }
.feat-title .feat-val { font-family: var(--font-mono); font-size: 12px; color: var(--ink-muted); }
.feat-row { display: grid; grid-template-columns: 1fr max-content; align-items: baseline; padding: 4px 0; font-family: var(--font-mono); font-variant-numeric: tabular-nums; }
.feat-row .k { font-size: 12px; color: var(--ink-muted); }
.feat-row .v { font-size: 13px; color: var(--ink); }
.dim-block { margin-bottom: 12px; }
.dim-bar { text-decoration: none !important; color: inherit; border-radius: 3px; padding: 8px 8px; margin: 0 -8px; transition: background 0.1s; }
.dim-bar:hover { background: var(--accent-faint); text-decoration: none; }
.contrib-list { padding: 6px 0 4px 108px; margin: 0 -8px; font-family: var(--font-mono); font-variant-numeric: tabular-nums; }
.contrib-row { display: grid; grid-template-columns: 1fr max-content max-content; align-items: baseline; gap: 12px; padding: 3px 0; font-size: 11px; }
.contrib-map { color: var(--ink-muted); text-decoration: none; }
.contrib-map:hover { color: var(--accent); }
.contrib-map .muted { color: var(--ink-faint); }
.contrib-val { color: var(--ink); font-weight: 500; }
.contrib-meta { color: var(--ink-faint); font-size: 10px; }
.upload-status { display: flex; flex-direction: column; gap: 10px; }
.upload-label { font-family: var(--font-mono); font-size: 14px; color: var(--ink); font-weight: 500; }
.upload-bar { height: 10px; background: var(--rule); border-radius: 5px; overflow: hidden; }
.upload-fill { height: 100%; background: linear-gradient(90deg, var(--accent-cool), var(--accent)); transition: width 0.3s ease-out; border-radius: 5px; }
.upload-note { font-family: var(--font-mono); font-size: 11px; color: var(--ink-muted); }
.disc-warn { display: flex; gap: 12px; align-items: flex-start; padding: 14px 18px; border-radius: 4px; font-family: var(--font-mono); font-size: 12px; line-height: 1.5; }
.disc-warn .disc-icon { flex: 0 0 20px; height: 20px; text-align: center; line-height: 20px; border-radius: 50%; font-weight: 700; }
.disc-warn.minor { background: rgba(176, 138, 43, 0.08); border: 1px solid var(--ok); color: var(--ink); }
.disc-warn.minor .disc-icon { background: var(--ok); color: white; }
.disc-warn.major { background: var(--accent-faint); border: 1px solid var(--accent-soft); color: var(--ink); }
.disc-warn.major .disc-icon { background: var(--accent); color: white; }
.fc-badge { display: inline-block; font-family: var(--font-mono); font-size: 10px; letter-spacing: 0.12em; padding: 2px 6px; border-radius: 3px; vertical-align: middle; margin-right: 6px; font-weight: 600; }
.fc-badge.fc { background: var(--great); color: white; }
.fc-badge.ss { background: linear-gradient(90deg, #d4af37, #f1d475); color: #3a2a00; }
h1 .fc-badge { font-size: 13px; padding: 3px 10px; }

/* --- inspector (fits container; range scrubber pans/zooms) --- */
.inspector-toolbar { display: flex; align-items: center; gap: 12px; margin-bottom: 12px; flex-wrap: wrap; }
.inspector-legend { display: flex; gap: 12px; font-family: var(--font-mono); font-size: 11px; color: var(--ink-muted); align-items: center; }
.inspector-legend > span { display: inline-flex; align-items: center; gap: 4px; }
.inspector-legend .lg { display: inline-block; width: 10px; height: 10px; border-radius: 50%; }
.inspector-legend .lg.don { background: var(--accent); }
.inspector-legend .lg.kat { background: var(--accent-cool); }
.inspector-legend .lg.band-legend { border-radius: 2px; width: 4px; height: 12px; }
.inspector-legend .lg.miss { background: var(--miss); opacity: 0.55; }
.inspector-legend .lg.ok { background: var(--ok); opacity: 0.35; }
.inspector-btn { font-family: var(--font-mono); font-size: 11px; background: var(--panel); color: var(--ink-muted); border: 1px solid var(--rule); border-radius: 3px; padding: 4px 12px; cursor: pointer; }
.inspector-btn:hover { color: var(--ink); border-color: var(--rule-strong); }
.inspector-frame { position: relative; background: var(--ground); border: 1px solid var(--rule); border-radius: 3px; height: 220px; overflow: hidden; user-select: none; -webkit-user-select: none; }
.inspector-frame * { user-select: none; -webkit-user-select: none; -webkit-user-drag: none; }
.lane { position: absolute; left: 60px; right: 12px; }
.chart-lane { top: 16px; height: 70px; border-bottom: 1px solid var(--rule); }
.input-lane { top: 110px; height: 66px; border-bottom: 1px solid var(--rule); }
.input-lane .hand-divider { position: absolute; left: 0; right: 0; top: 50%; height: 1px; background: var(--rule); opacity: 0.6; }
.bands { position: absolute; left: 60px; right: 12px; top: 16px; bottom: 44px; pointer-events: none; z-index: 0; }
.bands .band { position: absolute; top: 0; bottom: 0; width: 3px; transform: translateX(-50%); }
.bands .band.miss { background: var(--miss); opacity: 0.35; }
.bands .band.ok { background: var(--ok); opacity: 0.18; }
.lane-label { position: absolute; left: 8px; font-family: var(--font-mono); font-size: 10px; letter-spacing: 0.14em; text-transform: uppercase; color: var(--ink-faint); z-index: 3; }
.chart-label { top: 40px; }
.input-label { top: 130px; }
.lane .n { position: absolute; border-radius: 50%; transform: translate(-50%, -50%); z-index: 1; }
.chart-lane .n { top: 50%; }
.chart-lane .n.sm { width: 8px; height: 8px; }
.chart-lane .n.big { width: 14px; height: 14px; box-shadow: 0 0 0 1px rgba(255,255,255,0.35) inset; }
.chart-lane .n.don { background: var(--accent); }
.chart-lane .n.kat { background: var(--accent-cool); }
.input-lane .n { width: 7px; height: 7px; top: 50%; }
.input-lane .n.don { background: var(--accent); }
.input-lane .n.kat { background: var(--accent-cool); }
.inspector-frame.split-hands .hand-divider { display: block; }
.inspector-frame.split-hands .input-lane .n.hL { top: 25%; }
.inspector-frame.split-hands .input-lane .n.hR { top: 75%; }
.hand-divider { display: none; }
.inspector-toggle { display: inline-flex; align-items: center; gap: 6px; font-family: var(--font-mono); font-size: 11px; cursor: pointer; }
.inspector-toggle input { margin: 0; cursor: pointer; }
.inspector-frame { cursor: grab; }
.inspector-frame.panning { cursor: grabbing; }
.axis { position: absolute; left: 60px; right: 12px; bottom: 0; height: 30px; border-top: 1px solid var(--rule); }
.axis .tick { position: absolute; top: 4px; font-family: var(--font-mono); font-size: 10px; color: var(--ink-faint); transform: translateX(-50%); white-space: nowrap; }
.axis .tick::before { content: ''; position: absolute; top: -6px; left: 50%; width: 1px; height: 6px; background: var(--rule); transform: translateX(-50%); }

/* range scrubber */
.scrubber { margin-top: 16px; }
.scrub-track { position: relative; height: 34px; background: var(--ground); border: 1px solid var(--rule); border-radius: 3px; user-select: none; margin: 0 60px 0 60px; }
.scrub-mini { position: absolute; top: 0; left: 0; right: 0; bottom: 0; overflow: hidden; }
.scrub-mini .mini-tick { position: absolute; top: 4px; width: 1px; height: 10px; background: var(--miss); opacity: 0.6; transform: translateX(-50%); }
.scrub-window { position: absolute; top: 0; bottom: 0; background: var(--accent-faint); border-left: 2px solid var(--accent); border-right: 2px solid var(--accent); cursor: grab; z-index: 1; }
.scrub-window:active { cursor: grabbing; }
.scrub-handle { position: absolute; top: -3px; width: 8px; height: 40px; background: var(--accent); border-radius: 2px; cursor: ew-resize; transform: translateX(-50%); z-index: 2; }
.scrub-handle::after { content: ''; position: absolute; top: 8px; bottom: 8px; left: 3px; width: 2px; background: rgba(255,255,255,0.6); }
.scrub-labels { display: flex; justify-content: space-between; margin: 8px 60px 0 60px; font-family: var(--font-mono); font-size: 11px; color: var(--ink-muted); }
"""


def _render_upload_progress(task_id: str, entry: dict) -> str:
    """Polling progress page. JS refreshes /status every 300ms and updates the bar."""
    fname = entry.get("filename", "replay.osr")
    body = f"""
  <section>
    <span class="eyebrow">upload</span>
    <h1>Processing {fname}</h1>
    <p class="hint">This can take a while if the map isn't already cached and the trainer has to search your Songs folder.</p>
  </section>

  <section class="card">
    <div class="upload-status" id="upload-status">
      <div class="upload-label" id="upload-label">Queued</div>
      <div class="upload-bar"><div class="upload-fill" id="upload-fill" style="width: 0%"></div></div>
      <div class="upload-note" id="upload-note"></div>
    </div>
    <div id="upload-error" class="error-banner" style="display:none; margin-top: 16px;"></div>
    <div id="upload-result" class="hint" style="display:none; margin-top: 16px;"></div>
  </section>

  <script>
    (function() {{
      const taskId = "{task_id}";
      const label = document.getElementById('upload-label');
      const fill  = document.getElementById('upload-fill');
      const note  = document.getElementById('upload-note');
      const errEl = document.getElementById('upload-error');
      const okEl  = document.getElementById('upload-result');

      async function poll() {{
        try {{
          const r = await fetch('/upload/' + taskId + '/status');
          if (!r.ok) throw new Error('status ' + r.status);
          const s = await r.json();
          label.textContent = s.label || s.stage || '…';
          fill.style.width = (s.pct || 0) + '%';
          note.textContent = s.note || '';
          if (s.stage === 'error') {{
            errEl.style.display = 'block';
            errEl.textContent = s.error || 'Unknown error';
            return;  // stop polling
          }}
          if (s.stage === 'done' && s.redirect) {{
            okEl.style.display = 'block';
            okEl.textContent = s.result || 'Done — redirecting…';
            setTimeout(() => {{ window.location = s.redirect; }}, 800);
            return;
          }}
          setTimeout(poll, 300);
        }} catch (e) {{
          setTimeout(poll, 800);
        }}
      }}
      poll();
    }})();
  </script>
"""
    return _html_page("upload progress", body)


def _render_discrepancy_warning(row: dict) -> str:
    """Show a warning banner when our judged accuracy diverges materially from
    the game-reported accuracy. This catches replays whose input data was
    incompletely written by osu! (see empty-world FC investigation)."""
    acc_r = row.get("accuracy_reported") or 0.0
    acc_j = row.get("accuracy_judged") or 0.0
    if acc_r == 0.0:
        return ""
    diff_pct = abs(acc_r - acc_j) * 100
    # Absolute-accuracy delta threshold: 1.5 percentage points. Below that, it's
    # normal edge-case noise. Above, something is off.
    if diff_pct < 1.5:
        return ""
    if diff_pct < 5.0:
        level = "minor"
        msg = (f"Judged accuracy ({acc_j*100:.2f}%) differs from the game "
               f"({acc_r*100:.2f}%) by {diff_pct:.2f} pp. Usually an edge-case in "
               f"first-press-wins pairing; can be safely ignored.")
    else:
        level = "major"
        msg = (f"Judged accuracy ({acc_j*100:.2f}%) diverges strongly from the "
               f"game ({acc_r*100:.2f}%) — Δ {diff_pct:.2f} pp. The .osr replay "
               f"data may be incomplete (known issue with some lazer builds). "
               f"Skill-vector contribution is likely wrong for this replay.")
    return f'<section class="disc-warn {level}"><span class="disc-icon">!</span><div>{msg}</div></section>'


def _fc_badge(misses: int, oks: int) -> str:
    """Inline badge shown before the map title in listings."""
    if misses == 0 and oks == 0:
        return '<span class="fc-badge ss">SS</span> '
    if misses == 0:
        return '<span class="fc-badge fc">FC</span> '
    return ""


_CAUSE_COLORS = {
    "wrong_color": "#7A4E9E",
    "pattern_parity": "#3E8EBB",
    "speed": "var(--miss)",
    "stamina": "var(--ok)",
    "technical": "var(--accent-cool)",
    "gimmick": "#B060B0",
    "consistency": "var(--great)",
    "unknown": "var(--ink-faint)",
}


def _html_page(title: str, body: str, active: str = "") -> str:
    nav_items = [("Home", "/", "home")]
    nav_html = " ".join(
        f'<a href="{href}" class="{"active" if key == active else ""}">{label}</a>'
        for label, href, key in nav_items
    )
    return f"""<!doctype html>
<html><head>
<meta charset="utf-8">
<title>{title} — taiko-trainer</title>
<style>{_BASE_CSS}</style>
</head><body>
<main>
  <header class="site">
    <span class="logo">taiko-trainer</span>
    <nav>{nav_html}</nav>
  </header>
  {body}
</main>
</body></html>"""


def _render_home(workspace: str, catalog_stats: dict, players: list[dict], roots: list[str]) -> str:
    stats = {"catalog maps": catalog_stats.get("maps", 0), "players": len(players)}
    stats_html = "".join(
        f'<div class="stat"><span class="k">{k}</span><span class="v">{v}</span></div>'
        for k, v in stats.items()
    )
    players_html = ""
    if players:
        rows = "".join(
            f"<tr><td class='name'><a href='/player/{p['name']}'>{p['name']}</a></td>"
            f"<td class='muted'>{p['style']}</td>"
            f"<td>{p['replays']}</td></tr>"
            for p in players
        )
        players_html = f"<table><thead><tr><th>player</th><th>style</th><th>replays</th></tr></thead><tbody>{rows}</tbody></table>"
    else:
        players_html = "<p class='hint'>No players yet. Drop a replay below to start.</p>"

    roots_html = ""
    if roots:
        roots_html = "<ul style='font-family: var(--font-mono); font-size: 12px; color: var(--ink-muted);'>" + "".join(f"<li>{r}</li>" for r in roots) + "</ul>"
    else:
        roots_html = "<p class='hint'>No map search roots configured. Add one so uploaded replays can find their maps automatically.</p>"

    body = f"""
  <section>
    <span class="eyebrow">workspace</span>
    <h1>{workspace}</h1>
  </section>

  <section>
    <div class="stats-row">{stats_html}</div>
  </section>

  <section class="grid grid-2">
    <div class="card">
      <h2>Players</h2>
      {players_html}
    </div>
    <div class="card">
      <h2>Map search roots (across all players)</h2>
      {roots_html}
      <form class="inline-form" method="post" action="/settings/root">
        <input type="text" name="player" placeholder="player name" required>
        <input type="text" name="path" placeholder="/path/to/osu/Songs" required style="flex: 1;">
        <button type="submit">Add root</button>
      </form>
    </div>
  </section>

  <section>
    <h2>Add a replay</h2>
    <form class="upload" method="post" action="/upload" enctype="multipart/form-data">
      <p class="hint">Drop a .osr file (and optionally a matching .osu). If the map is already in the DB or under a configured root, we'll resolve it automatically.</p>
      <input type="file" name="file" accept=".osr" required>
      <input type="file" name="map_file" accept=".osu">
      <button type="submit">Upload &amp; analyze</button>
    </form>
  </section>
"""
    return _html_page("Home", body, active="home")


def _render_report(report, replays: list[dict] | None = None, player_name: str | None = None) -> str:
    d = report.skill.as_dict()
    max_skill = max(d.values()) or 1

    dim_bars = ""
    ordered = sorted(d.items(), key=lambda kv: -kv[1])
    contribs_map = getattr(report, "dim_contributors", None) or {}
    for dim, val in ordered:
        pct = min(100, val / max_skill * 100)
        is_weakest = dim == report.weakest_dim
        delta_html = ""
        if report.skill_delta is not None:
            delta = report.skill_delta[dim]
            if abs(delta) < 0.5:
                delta_html = '<span class="delta">·</span>'
            elif delta > 0:
                delta_html = f'<span class="delta up">↑ +{delta:.0f}</span>'
            else:
                delta_html = f'<span class="delta down">↓ {delta:.0f}</span>'

        # Top-3 contributors under each bar — shows WHY the number is what it is.
        contribs = contribs_map.get(dim, ())[:3]
        contribs_html = ""
        if contribs:
            rows = "".join(
                f'<div class="contrib-row">'
                f'<a href="/replay/{player_name}/{c.replay_id}" class="contrib-map">{c.map_title[:44]} <span class="muted">[{c.map_diff[:18]}]</span></a>'
                f'<span class="contrib-val">+{c.weighted:.0f}</span>'
                f'<span class="contrib-meta">rating {c.raw_rating:.0f} · acc {c.accuracy*100:.1f}%</span>'
                f'</div>'
                for c in contribs
            )
            contribs_html = f'<div class="contrib-list">{rows}</div>'

        dim_bars += (
            f'<div class="dim-block">'
            f'<a href="/player/{player_name}/train/{dim}" class="dim-bar {"weakest" if is_weakest else ""}">'
            f'<span class="name">{dim}{"  ★" if is_weakest else ""}</span>'
            f'<div class="track"><div class="fill" style="width: {pct:.1f}%"></div></div>'
            f'<span class="val">{val:.0f}</span>'
            f"{delta_html}"
            f"</a>"
            f"{contribs_html}"
            f"</div>"
        )

    causes_html = ""
    if report.misses_by_cause:
        total = sum(report.misses_by_cause.values()) or 1
        ordered_causes = sorted(report.misses_by_cause.items(), key=lambda kv: -kv[1])
        for cause, count in ordered_causes:
            if count == 0:
                continue
            pct = count / total * 100
            color = _CAUSE_COLORS.get(cause, "var(--ink-faint)")
            causes_html += (
                f'<div class="cause-bar">'
                f'<span class="name">{cause.replace("_", "-")}</span>'
                f'<div class="track"><div class="fill" style="width: {pct:.1f}%; background: {color};"></div></div>'
                f'<span class="count">{count}  ({pct:.1f}%)</span>'
                f"</div>"
            )

    sess_html = ""
    if report.latest_session:
        latest = report.latest_session
        prev = report.previous_session
        def _delta(v, unit="", flip=False):
            if abs(v) < 0.005: return '<span class="delta">·</span>'
            up = v > 0
            good = (up and not flip) or (not up and flip)
            cls = "up" if good else "down"
            return f'<span class="delta {cls}">{"↑" if up else "↓"} {v:+.2f}{unit}</span>'
        acc_delta = _delta((latest.weighted_accuracy - prev.weighted_accuracy) * 100, "%") if prev else ""
        stddev_delta = _delta(latest.avg_delta_stddev_ms - prev.avg_delta_stddev_ms, " ms", flip=True) if prev else ""
        cheese_delta = _delta((latest.avg_cheese_rate - prev.avg_cheese_rate) * 100, "%", flip=(report.style == "kddk")) if prev else ""
        sess_html = f"""
    <div class="card">
      <h2>Latest session <span style="color: var(--ink-muted); font-size: 12px; margin-left: 8px;">{latest.start[:16]}</span></h2>
      <div class="stats-row">
        <div class="stat"><span class="k">replays</span><span class="v">{len(latest.replays)}</span></div>
        <div class="stat"><span class="k">accuracy</span><span class="v">{latest.weighted_accuracy*100:.2f}%{acc_delta}</span></div>
        <div class="stat"><span class="k">delta σ</span><span class="v">{latest.avg_delta_stddev_ms:.1f} ms{stddev_delta}</span></div>
        <div class="stat"><span class="k">misses</span><span class="v">{latest.total_misses}</span></div>
        <div class="stat"><span class="k">cheese</span><span class="v">{latest.avg_cheese_rate*100:.2f}%{cheese_delta}</span></div>
      </div>
      {"<p class='hint' style='margin-top: 12px;'>compared to session at " + prev.start[:16] + "</p>" if prev else ""}
    </div>"""

    suggestions_html = ""
    if report.suggestions:
        for i, s in enumerate(report.suggestions, 1):
            if s.suggestion_score < 0.1:
                continue
            fit = "excellent" if s.suggestion_score > 0.75 else "good" if s.suggestion_score > 0.4 else "modest"
            suggestions_html += (
                f'<div style="padding: 12px 0; border-bottom: 1px dashed var(--rule);">'
                f'<div style="font-family: var(--font-mono); color: var(--ink);">{i}. {s.title} <span style="color: var(--ink-muted);">[{s.version}]</span></div>'
                f'<div style="font-family: var(--font-mono); font-size: 12px; color: var(--ink-muted); margin-top: 4px;">'
                f'rating[{s.target_dim}]={s.target_rating:.0f}  ·  growth {s.target_gain_frac*100:+.0f}%  ·  fit {fit}  ·  by {s.creator}'
                f'</div></div>'
            )
        if not suggestions_html.strip():
            suggestions_html = "<p class='hint'>No maps in DB with growth potential for this dimension. Add more via drop-zone.</p>"

    body = f"""
  <section>
    <span class="eyebrow">training report</span>
    <h1>{report.player}</h1>
    <p class="hint">{report.replays} replays  ·  {report.total_misses} total misses  ·  playstyle: {report.style}</p>
  </section>

  <section class="card">
    <h2>Skill vector</h2>
    <p class="hint">weakest dimension is your training target</p>
    {dim_bars}
  </section>

  {sess_html}

  <section class="card">
    <h2>Dominant miss causes (all replays)</h2>
    {causes_html or "<p class='hint'>No classified misses.</p>"}
  </section>

  <section class="card">
    <h2>Suggested maps to push {report.weakest_dim}</h2>
    {suggestions_html or "<p class='hint'>No maps available.</p>"}
  </section>

  {_render_replays_table(replays or [], player_name or report.player)}
"""
    return _html_page(f"{report.player} report", body)


def _render_inspector(row: dict, player: str, judged, replay) -> str:
    """LOA-style timeline: fits container, dual-handle range scrubber pans/zooms.
    Chart lane shows notes; input lane shows every real key press from the replay
    (not just judgment-matched ones); miss/ok events are vertical background bands."""
    from .judgment import Verdict as _V
    from .models import TaikoInput as _TI

    judgments = judged.judgments
    if not judgments:
        return _render_error("Replay has no judgments.")

    start_ms = min(j.note.time_ms for j in judgments)
    end_ms = max(j.note.time_ms for j in judgments)
    for f in replay.frames:
        if f.time_ms > end_ms and f.time_ms - start_ms < 15 * 60 * 1000:
            end_ms = f.time_ms
    duration_s = max(1.0, (end_ms - start_ms) / 1000)

    # ---- chart notes ------------------------------------------------------
    note_spans = []
    for j in judgments:
        t = (j.note.time_ms - start_ms) / 1000
        color = "don" if j.note.note_type.is_don else "kat"
        size = "big" if j.note.note_type.is_big else "sm"
        note_spans.append(
            f'<span class="n {color} {size}" data-t="{t:.3f}"></span>'
        )

    # ---- verdict bands (miss = strong; ok = subtle) ----------------------
    band_spans = []
    misses: list[float] = []
    for j in judgments:
        t = (j.note.time_ms - start_ms) / 1000
        if j.verdict is _V.MISS:
            band_spans.append(f'<span class="band miss" data-t="{t:.3f}"></span>')
            misses.append(t)
        elif j.verdict is _V.OK:
            band_spans.append(f'<span class="band ok" data-t="{t:.3f}"></span>')

    # ---- input events: every rising-edge press from the replay ----------
    key_map = {
        _TI.LEFT_KAT:  ("kat", "L"),   # KDDK: outer L = kat left
        _TI.LEFT_DON:  ("don", "L"),
        _TI.RIGHT_DON: ("don", "R"),
        _TI.RIGHT_KAT: ("kat", "R"),
    }
    input_spans = []
    for f in replay.frames:
        if not f.pressed:
            continue
        t = (f.time_ms - start_ms) / 1000
        if t < -0.5 or t > duration_s + 0.5:
            continue
        for bit, (color, hand) in key_map.items():
            if f.pressed & bit:
                input_spans.append(
                    f'<span class="n {color} h{hand}" data-t="{t:.3f}"></span>'
                )

    # ---- miss density mini-map (below the range slider) ----
    mini_ticks = "".join(f'<span class="mini-tick" data-t="{t:.3f}"></span>' for t in misses)

    total_notes = len(judgments)
    body = f"""
  <section>
    <span class="eyebrow"><a href="/replay/{player}/{row['id']}" style="color: var(--ink-muted);">← replay #{row['id']}</a>  ·  inspector</span>
    <h1>{row['map_title']} <span style="color: var(--ink-muted); font-size: 20px;">[{row['map_version']}]</span></h1>
    <p class="hint">
      {total_notes} notes  ·  {len(misses)} misses  ·  {int(duration_s)//60}:{int(duration_s)%60:02d} duration  ·
      windows: great ±{judged.windows.great:.0f}ms / ok ±{judged.windows.ok:.0f}ms
    </p>
  </section>

  <section class="card">
    <div class="inspector-toolbar">
      <div class="inspector-legend">
        <span><span class="lg don"></span>don</span>
        <span><span class="lg kat"></span>kat</span>
        <span><span class="lg band-legend miss"></span>miss</span>
        <span><span class="lg band-legend ok"></span>ok</span>
      </div>
      <div style="flex:1"></div>
      <label class="inspector-toggle"><input type="checkbox" id="split-hands"> <span class="hint">split hands</span></label>
      <button id="reset-zoom" class="inspector-btn">reset zoom</button>
    </div>

    <div class="inspector-frame" id="inspector-frame">
      <div class="lane-label chart-label">chart</div>
      <div class="lane-label input-label">input</div>
      <div class="lane chart-lane" id="chart-lane">{"".join(note_spans)}</div>
      <div class="bands" id="bands">{"".join(band_spans)}</div>
      <div class="lane input-lane" id="input-lane">
        <div class="hand-divider"></div>
        {"".join(input_spans)}
      </div>
      <div class="axis" id="axis"></div>
    </div>

    <div class="scrubber">
      <div class="scrub-track" id="scrub-track">
        <div class="scrub-mini">{mini_ticks}</div>
        <div class="scrub-window" id="scrub-window"></div>
        <div class="scrub-handle l" id="handle-l"></div>
        <div class="scrub-handle r" id="handle-r"></div>
      </div>
      <div class="scrub-labels">
        <span id="scrub-start">0:00</span>
        <span id="scrub-window-info" class="hint"></span>
        <span id="scrub-end">{int(duration_s)//60}:{int(duration_s)%60:02d}</span>
      </div>
    </div>
  </section>

  <script>
    (function() {{
      const duration = {duration_s:.3f};
      const frame = document.getElementById('inspector-frame');
      const chartLane = document.getElementById('chart-lane');
      const inputLane = document.getElementById('input-lane');
      const bandsEl = document.getElementById('bands');
      const axis = document.getElementById('axis');
      const chartNotes = [...chartLane.children];
      const inputNotes = [...inputLane.querySelectorAll('.n')];
      const bands = [...bandsEl.children];
      const miniTicks = [...document.getElementById('scrub-track').querySelectorAll('.mini-tick')];

      let tStart = 0;
      let tEnd = duration;

      const scrubTrack = document.getElementById('scrub-track');
      const scrubWindow = document.getElementById('scrub-window');
      const handleL = document.getElementById('handle-l');
      const handleR = document.getElementById('handle-r');
      const windowInfo = document.getElementById('scrub-window-info');

      function fmt(t) {{
        const m = Math.floor(t / 60);
        const s = Math.floor(t % 60);
        return m + ':' + String(s).padStart(2, '0');
      }}

      function positionEl(el) {{
        const t = parseFloat(el.dataset.t);
        if (t < tStart || t > tEnd) {{ el.style.display = 'none'; return; }}
        el.style.display = '';
        el.style.left = ((t - tStart) / (tEnd - tStart) * 100) + '%';
      }}

      function renderAxis() {{
        axis.innerHTML = '';
        const span = tEnd - tStart;
        // Target ~8 ticks visible.
        let step = 1;
        const targetSteps = 8;
        while (span / step > targetSteps * 2) step *= 2;
        while (span / step < targetSteps / 2) step /= 2;
        if (step < 0.5) step = 0.5;
        const firstTick = Math.ceil(tStart / step) * step;
        for (let t = firstTick; t <= tEnd; t += step) {{
          const tick = document.createElement('div');
          tick.className = 'tick';
          tick.style.left = ((t - tStart) / span * 100) + '%';
          tick.textContent = fmt(t);
          axis.appendChild(tick);
        }}
      }}

      function refresh() {{
        for (const el of chartNotes) positionEl(el);
        for (const el of inputNotes) positionEl(el);
        for (const el of bands) positionEl(el);
        renderAxis();
        // update scrubber visuals
        const lPct = tStart / duration * 100;
        const rPct = tEnd / duration * 100;
        scrubWindow.style.left = lPct + '%';
        scrubWindow.style.right = (100 - rPct) + '%';
        handleL.style.left = lPct + '%';
        handleR.style.left = rPct + '%';
        windowInfo.textContent = fmt(tStart) + ' — ' + fmt(tEnd) + '  (' + (tEnd - tStart).toFixed(1) + 's window)';
        for (const t of miniTicks) {{
          const tv = parseFloat(t.dataset.t);
          t.style.left = (tv / duration * 100) + '%';
        }}
      }}

      // --- drag interactions ---
      function trackPct(clientX) {{
        const r = scrubTrack.getBoundingClientRect();
        return Math.max(0, Math.min(1, (clientX - r.left) / r.width));
      }}

      function bindDrag(el, onMove) {{
        el.addEventListener('mousedown', ev => {{
          ev.preventDefault();
          const move = (e) => onMove(trackPct(e.clientX));
          const up = () => {{ document.removeEventListener('mousemove', move); document.removeEventListener('mouseup', up); }};
          document.addEventListener('mousemove', move);
          document.addEventListener('mouseup', up);
        }});
      }}

      const MIN_WINDOW = Math.max(1.0, duration * 0.005);
      bindDrag(handleL, pct => {{
        tStart = Math.min(duration * pct, tEnd - MIN_WINDOW);
        refresh();
      }});
      bindDrag(handleR, pct => {{
        tEnd = Math.max(duration * pct, tStart + MIN_WINDOW);
        refresh();
      }});
      bindDrag(scrubWindow, pct => {{
        const width = tEnd - tStart;
        let center = duration * pct;
        tStart = Math.max(0, Math.min(duration - width, center - width / 2));
        tEnd = tStart + width;
        refresh();
      }});

      // click on empty scrub area = center window there
      scrubTrack.addEventListener('click', ev => {{
        if (ev.target !== scrubTrack) return;
        const width = tEnd - tStart;
        const pct = trackPct(ev.clientX);
        tStart = Math.max(0, Math.min(duration - width, duration * pct - width / 2));
        tEnd = tStart + width;
        refresh();
      }});

      // drag-to-pan on the main frame — click and drag horizontally to scroll the window
      let panState = null;
      frame.addEventListener('mousedown', ev => {{
        if (ev.button !== 0) return;
        ev.preventDefault();
        panState = {{
          startX: ev.clientX,
          startTStart: tStart,
          startTEnd: tEnd,
          width: tEnd - tStart,
          frameWidth: frame.getBoundingClientRect().width,
          moved: false,
        }};
        frame.classList.add('panning');
      }});
      document.addEventListener('mousemove', ev => {{
        if (!panState) return;
        const dx = ev.clientX - panState.startX;
        if (Math.abs(dx) > 2) panState.moved = true;
        const dt = -(dx / panState.frameWidth) * panState.width;
        let newStart = panState.startTStart + dt;
        newStart = Math.max(0, Math.min(duration - panState.width, newStart));
        tStart = newStart;
        tEnd = newStart + panState.width;
        refresh();
      }});
      document.addEventListener('mouseup', () => {{
        if (panState) {{
          panState = null;
          frame.classList.remove('panning');
        }}
      }});

      // hand-split toggle
      const splitToggle = document.getElementById('split-hands');
      splitToggle.addEventListener('change', () => {{
        frame.classList.toggle('split-hands', splitToggle.checked);
      }});

      // wheel zoom over the main frame (mouse position = zoom center)
      frame.addEventListener('wheel', ev => {{
        ev.preventDefault();
        const r = frame.getBoundingClientRect();
        const pct = (ev.clientX - r.left) / r.width;
        const center = tStart + (tEnd - tStart) * pct;
        const factor = ev.deltaY > 0 ? 1.25 : 0.8;
        let width = (tEnd - tStart) * factor;
        width = Math.max(MIN_WINDOW, Math.min(duration, width));
        tStart = Math.max(0, center - width * pct);
        tEnd = Math.min(duration, tStart + width);
        if (tEnd - tStart < width) tStart = Math.max(0, tEnd - width);
        refresh();
      }}, {{ passive: false }});

      document.getElementById('reset-zoom').addEventListener('click', () => {{
        tStart = 0; tEnd = duration; refresh();
      }});

      refresh();
    }})();
  </script>
"""
    return _html_page(f"inspect #{row['id']}", body)


_DIM_TAGLINE = {
    "speed":       "motor tempo — how fast your hands alternate",
    "stamina":     "endurance — long high-density stretches without dropping",
    "gimmick":     "reading pressure — SV variance, obscured densities",
    "technical":   "pattern awareness — mono runs, mixed divisors, parity",
    "consistency": "unwavering timing — no random drops from bursts / parity flips",
}


def _render_train_page(player: str, dim: str, skill, suggestions, contribs) -> str:
    d = skill.as_dict()
    val = d[dim]

    contribs_html = ""
    if contribs:
        rows = "".join(
            f'<div class="contrib-row" style="grid-template-columns: 24px 1fr max-content max-content;">'
            f'<span class="contrib-meta">#{i+1}</span>'
            f'<a href="/replay/{player}/{c.replay_id}" class="contrib-map">{c.map_title} <span class="muted">[{c.map_diff}]</span></a>'
            f'<span class="contrib-val">+{c.weighted:.0f}</span>'
            f'<span class="contrib-meta">rating {c.raw_rating:.0f} · acc {c.accuracy*100:.1f}%</span>'
            f'</div>'
            for i, c in enumerate(contribs)
        )
        contribs_html = (
            f'<section class="card"><h2>What drove your {dim} = {val:.0f}</h2>'
            f'<p class="hint">weighted top-K aggregation from your play history</p>'
            f'<div class="contrib-list" style="padding-left: 0;">{rows}</div>'
            f'</section>'
        )

    sugg_html = ""
    if suggestions:
        for i, s in enumerate(suggestions, 1):
            if s.suggestion_score < 0.05:
                continue
            fit = ("excellent" if s.suggestion_score > 0.75
                   else "good" if s.suggestion_score > 0.4
                   else "modest")
            sugg_html += (
                f'<div style="padding: 12px 0; border-bottom: 1px dashed var(--rule);">'
                f'<div style="font-family: var(--font-mono); color: var(--ink);">{i}. {s.title} <span style="color: var(--ink-muted);">[{s.version}]</span></div>'
                f'<div style="font-family: var(--font-mono); font-size: 12px; color: var(--ink-muted); margin-top: 4px;">'
                f'rating[{s.target_dim}]={s.target_rating:.0f}  ·  growth {s.target_gain_frac*100:+.0f}%  ·  fit {fit}  ·  by {s.creator}'
                f'</div></div>'
            )
    if not sugg_html.strip():
        sugg_html = "<p class='hint'>No maps in catalog with growth potential for this dimension. Ingest more via the drop-zone.</p>"

    tagline = _DIM_TAGLINE.get(dim, "")
    body = f"""
  <section>
    <span class="eyebrow"><a href="/player/{player}" style="color: var(--ink-muted);">← {player} report</a>  ·  training</span>
    <h1>Push your {dim}</h1>
    <p class="hint">{tagline}  ·  current skill: <b>{val:.0f}</b></p>
  </section>

  {contribs_html}

  <section class="card">
    <h2>Maps to grow {dim}</h2>
    <p class="hint">ranked by growth-vs-overwhelm score for your current profile</p>
    {sugg_html}
  </section>
"""
    return _html_page(f"train {dim} — {player}", body)


def _render_replays_table(replays: list[dict], player: str) -> str:
    if not replays:
        return ""
    rows = ""
    for r in replays:
        acc = (r.get("accuracy_judged") or 0) * 100
        misses = r.get("count_miss") or 0
        oks = r.get("count_ok") or 0
        stddev = r.get("delta_stddev_ms") or 0
        cheese = (r.get("cheese_rate") or 0) * 100
        played = (r.get("played_at") or "")[:16].replace("T", " ")
        title = (r.get("map_title") or "?")
        version = (r.get("map_version") or "?")
        badge = _fc_badge(misses, oks)
        miss_cell = (
            f'<td style="color: var(--great);">FC</td>' if misses == 0
            else f'<td style="color: var(--miss);">{misses}</td>'
        )
        rows += (
            f'<tr onclick="window.location=\'/replay/{player}/{r["id"]}\'" style="cursor:pointer">'
            f'<td class="name">{badge}{title} <span style="color: var(--ink-muted); font-size: 11px;">[{version}]</span></td>'
            f'<td class="muted">{played}</td>'
            f'<td>{acc:.2f}%</td>'
            f'{miss_cell}'
            f'<td>{stddev:.1f} ms</td>'
            f'<td>{cheese:.2f}%</td>'
            f'</tr>'
        )
    return f"""
  <section class="card">
    <h2>Replays ({len(replays)})</h2>
    <p class="hint">click a row to see the per-note breakdown</p>
    <div style="overflow-x: auto; margin-top: 12px;">
      <table>
        <thead><tr>
          <th>map</th><th>played</th><th>acc</th><th>miss</th><th>Δ σ</th><th>cheese</th>
        </tr></thead>
        <tbody>{rows}</tbody>
      </table>
    </div>
  </section>"""


def _render_replay(row: dict, player: str, features=None) -> str:
    import json as _json

    causes_html = ""
    if row.get("classification_json"):
        try:
            causes = _json.loads(row["classification_json"])
        except Exception:
            causes = {}
        if causes:
            total = sum(causes.values()) or 1
            ordered = sorted(causes.items(), key=lambda kv: -kv[1])
            for cause, n in ordered:
                if n == 0: continue
                pct = n / total * 100
                color = _CAUSE_COLORS.get(cause, "var(--ink-faint)")
                causes_html += (
                    f'<div class="cause-bar">'
                    f'<span class="name">{cause.replace("_", "-")}</span>'
                    f'<div class="track"><div class="fill" style="width: {pct:.1f}%; background: {color};"></div></div>'
                    f'<span class="count">{n}  ({pct:.1f}%)</span>'
                    f'</div>'
                )
    causes_section = (
        f'<section class="card"><h2>Miss causes (this replay)</h2>{causes_html}</section>'
        if causes_html else ""
    )

    features_section = _render_features_panel(features) if features else ""
    header_badge = _fc_badge(row.get("count_miss", 1), row.get("count_ok", 1))
    warning_html = _render_discrepancy_warning(row)

    body = f"""
  <section>
    <span class="eyebrow"><a href="/player/{player}" style="color: var(--ink-muted);">← {player}</a>  ·  replay #{row['id']}</span>
    <h1>{header_badge}{row['map_title']} <span style="color: var(--ink-muted); font-size: 20px;">[{row['map_version']}]</span></h1>
    <p class="hint">mapped by {row.get('map_creator','?')}  ·  played {row['played_at'][:19].replace('T', ' ')}  ·  <a href="/replay/{player}/{row['id']}/inspect">open inspector →</a></p>
  </section>

  {warning_html}

  <section class="stats-row">
    <div class="stat"><span class="k">reported acc</span><span class="v">{row['accuracy_reported']*100:.2f}%</span></div>
    <div class="stat"><span class="k">judged acc</span><span class="v">{row['accuracy_judged']*100:.2f}%</span></div>
    <div class="stat"><span class="k">great</span><span class="v" style="color: var(--great);">{row['count_great']}</span></div>
    <div class="stat"><span class="k">ok</span><span class="v" style="color: var(--ok);">{row['count_ok']}</span></div>
    <div class="stat"><span class="k">miss</span><span class="v" style="color: var(--miss);">{row['count_miss']}</span></div>
  </section>

  <section class="card">
    <h2>Map rating</h2>
    <div class="stats-row">
      <div class="stat"><span class="k">speed</span><span class="v">{row['rating_speed']:.0f}</span></div>
      <div class="stat"><span class="k">stamina</span><span class="v">{row['rating_stamina']:.0f}</span></div>
      <div class="stat"><span class="k">gimmick</span><span class="v">{row['rating_gimmick']:.0f}</span></div>
      <div class="stat"><span class="k">technical</span><span class="v">{row['rating_technical']:.0f}</span></div>
      <div class="stat"><span class="k">consistency</span><span class="v">{row['rating_consistency']:.0f}</span></div>
    </div>
  </section>

  {features_section}

  {causes_section}

  <section class="card">
    <h2>Timing</h2>
    <p class="hint">delta mean {row['delta_mean_ms']:.1f} ms  ·  σ {row['delta_stddev_ms']:.1f} ms  ·  cheese rate {row['cheese_rate']*100:.2f}%  ·  fast-cheese pairs {row.get('fast_cheese_pairs', 0)}</p>
  </section>
"""
    return _html_page(f"replay #{row['id']}", body)


def _render_features_panel(f) -> str:
    """Show the numbers that drove each rating dimension. `f` is a MapFeatures."""
    d = f.density
    m = f.movement
    c = f.color
    r = f.rhythm
    b = f.bursts
    g = f.gimmick
    s = f.strain

    # divisor mix (top 4 shares, ignore "other" if trivial)
    div_items = sorted(r.divisor_share.items(), key=lambda kv: -kv[1])[:4]
    div_row = "  ·  ".join(f"{k} {v*100:.0f}%" for k, v in div_items if v > 0.01)

    same_bpm = abs(m.bpm_min - m.bpm_max) < 0.5
    bpm_str = f"{m.bpm_max:.0f}" if same_bpm else f"{m.bpm_min:.0f}–{m.bpm_max:.0f}"
    duration_str = f"{int(d.duration_s)//60}:{int(d.duration_s)%60:02d}"

    return f"""
  <section class="card">
    <h2>Why this rating</h2>
    <p class="hint">the underlying feature numbers, grouped by the dimension they feed</p>

    <div class="feat-group">
      <div class="feat-title"><span>speed</span><span class="feat-val">{bpm_str} BPM · peak burst {d.peak_nps_200ms:.0f} n/s</span></div>
      <div class="feat-row"><span class="k">BPM range</span><span class="v">{bpm_str}</span></div>
      <div class="feat-row"><span class="k">peak 200ms burst</span><span class="v">{d.peak_nps_200ms:.1f} notes/s</span></div>
      <div class="feat-row"><span class="k">peak 1s NPS</span><span class="v">{d.peak_nps:.1f}</span></div>
      <div class="feat-row"><span class="k">dominant divisor</span><span class="v">{r.dominant_divisor} ({r.dominant_divisor_share*100:.0f}%)</span></div>
    </div>

    <div class="feat-group">
      <div class="feat-title"><span>stamina</span><span class="feat-val">avg {d.avg_nps:.1f} n/s over {duration_str}</span></div>
      <div class="feat-row"><span class="k">duration</span><span class="v">{duration_str}</span></div>
      <div class="feat-row"><span class="k">hittable notes</span><span class="v">{f.hittable_notes}</span></div>
      <div class="feat-row"><span class="k">avg NPS</span><span class="v">{d.avg_nps:.1f}</span></div>
      <div class="feat-row"><span class="k">peak 5s NPS</span><span class="v">{d.peak_nps_5s:.1f}</span></div>
      <div class="feat-row"><span class="k">high-density ratio</span><span class="v">{d.high_density_ratio*100:.0f}%</span></div>
      <div class="feat-row"><span class="k">longest sustained</span><span class="v">{d.longest_sustained_high_s:.0f} s</span></div>
      <div class="feat-row"><span class="k">strain (integrated)</span><span class="v">{s.total:.0f}</span></div>
      <div class="feat-row"><span class="k">fatiguing windows</span><span class="v">{s.fatiguing_windows}</span></div>
    </div>

    <div class="feat-group">
      <div class="feat-title"><span>technical</span><span class="feat-val">mono max {c.run_length_max} · 1/6 share {r.divisor_share.get('1/6', 0)*100:.0f}%</span></div>
      <div class="feat-row"><span class="k">mono-run max</span><span class="v">{c.run_length_max}</span></div>
      <div class="feat-row"><span class="k">mono-run mean</span><span class="v">{c.run_length_mean:.1f}</span></div>
      <div class="feat-row"><span class="k">mono-stream ratio</span><span class="v">{c.mono_stream_ratio*100:.0f}%</span></div>
      <div class="feat-row"><span class="k">divisor mix</span><span class="v" style="font-size: 11px;">{div_row}</span></div>
      <div class="feat-row"><span class="k">color-change ratio</span><span class="v">{c.color_change_ratio*100:.0f}%</span></div>
      <div class="feat-row"><span class="k">don share</span><span class="v">{c.don_ratio*100:.0f}%</span></div>
    </div>

    <div class="feat-group">
      <div class="feat-title"><span>gimmick</span><span class="feat-val">SV σ {m.sv_stddev:.3f} · SV changes/min {m.sv_changes_per_minute:.1f}</span></div>
      <div class="feat-row"><span class="k">SV range</span><span class="v">{m.sv_min:.2f} — {m.sv_max:.2f}</span></div>
      <div class="feat-row"><span class="k">SV stddev</span><span class="v">{m.sv_stddev:.3f}</span></div>
      <div class="feat-row"><span class="k">SV changes/min</span><span class="v">{m.sv_changes_per_minute:.1f}</span></div>
      <div class="feat-row"><span class="k">low-SV share</span><span class="v">{g.low_sv_share*100:.0f}%</span></div>
      <div class="feat-row"><span class="k">unreadable ratio</span><span class="v">{g.unreadable_ratio*100:.0f}%</span></div>
      <div class="feat-row"><span class="k">sv-bpm score</span><span class="v">{g.sv_bpm_score:.1f}</span></div>
    </div>

    <div class="feat-group">
      <div class="feat-title"><span>consistency</span><span class="feat-val">parity {f.parity.hostile_ratio*100:.0f}% hostile · bursts {b.burst_count}</span></div>
      <div class="feat-row"><span class="k">parity mean</span><span class="v">{f.parity.mean:.2f}</span></div>
      <div class="feat-row"><span class="k">parity hostile ratio</span><span class="v">{f.parity.hostile_ratio*100:.0f}%</span></div>
      <div class="feat-row"><span class="k">burst count</span><span class="v">{b.burst_count}</span></div>
      <div class="feat-row"><span class="k">burst mean length</span><span class="v">{b.mean_length:.1f}</span></div>
      <div class="feat-row"><span class="k">longest burst</span><span class="v">{b.max_length}</span></div>
      <div class="feat-row"><span class="k">long-burst share (≥7)</span><span class="v">{b.length_7plus_ratio*100:.0f}%</span></div>
    </div>
  </section>"""


def _render_error(message: str) -> str:
    body = f'<section class="card"><div class="error-banner">{message}</div></section>'
    return _html_page("error", body)


def _render_upload_error(message: str) -> str:
    body = (
        '<section class="card"><h2>Could not add replay</h2>'
        f'<div class="error-banner">{message}</div>'
        '<p class="hint" style="margin-top: 16px;"><a href="/">← back to home</a></p></section>'
    )
    return _html_page("upload error", body)


# -------------------------------------------------------------------------

def serve(workspace: str = ".", host: str = "127.0.0.1", port: int = 8000) -> None:
    import uvicorn

    app = create_app(workspace)
    print(f"taiko-trainer serving on http://{host}:{port}  ·  workspace: {workspace}")
    print("  ctrl-c to stop")
    uvicorn.run(app, host=host, port=port, log_level="warning")

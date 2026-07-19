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
from .report import _compute_dim_contributors, build_report
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
    "api_lookup":     ("Looking up map on osu! API", 40),
    "api_download":   ("Downloading .osz from mirror", 50),
    "api_hit":        ("osu! API resolved the map", 65),
    "api_error":      ("osu! API failed",       60),
    "rate_map":       ("Computing map rating", 75),
    "ingest_siblings":("Scanning sibling difficulties", 77),
    "siblings_done":  ("Siblings added",       78),
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
    def home(request: Request):
        from . import osu_api
        ws_stats = workspace_status(workspace)
        players_info = []
        for player_name, s in ws_stats["players"].items():
            players_info.append({
                "name": player_name,
                "style": s["style"],
                "replays": s["replays"],
            })
        catalog_stats = ws_stats["catalog"]
        roots_set: set[str] = set()
        for name in ws_stats["players"]:
            conn = open_plays(workspace, name)
            for r in list_map_roots(conn):
                roots_set.add(r)
            conn.close()
        roots = sorted(roots_set)
        # Check osu! API configuration status
        cat = open_catalog(workspace)
        api_configured = osu_api.is_configured(cat)
        cat.close()
        flash_ok = request.query_params.get("ok", "")
        flash_err = request.query_params.get("err", "")
        return _render_home(workspace, catalog_stats, players_info, roots,
                            api_configured=api_configured,
                            flash_ok=flash_ok, flash_err=flash_err)

    @app.get("/player/{name}", response_class=HTMLResponse)
    def player_page(name: str, request: Request):
        conn = open_plays(workspace, name)
        report = build_report(conn)
        replays = get_replays(conn) if report else []
        conn.close()
        if report is None:
            return HTMLResponse(_render_error(f"No snapshots for player {name!r}. Drop a replay first."), status_code=404)
        flash_ok = request.query_params.get("ok", "")
        flash_err = request.query_params.get("err", "")
        return _render_report(report, replays, name, flash_ok=flash_ok, flash_err=flash_err)

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
        # Full contributor list for THIS dim on this dedicated page — the player
        # report shows top 5, but here we want to see the tail too (users may
        # have hundreds of plays).
        full_contribs = _compute_dim_contributors(replays, top_n=100).get(dim, ())
        conn.close()
        return _render_train_page(name, dim, report.skill, suggestions, full_contribs)

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

    @app.get("/replay/{player}/{replay_id}/osr")
    def download_osr(player: str, replay_id: int):
        from fastapi.responses import Response
        conn = open_plays(workspace, player)
        row = conn.execute(
            """
            SELECT r.content, r.played_at, m.title, m.version
            FROM replays r JOIN catalog.maps m ON m.md5 = r.map_md5
            WHERE r.id = ?
            """,
            (replay_id,),
        ).fetchone()
        conn.close()
        if not row:
            raise HTTPException(status_code=404)
        # Sanitize a filename that Windows + macOS will accept.
        raw = f"{player} - {row['title']} [{row['version']}] ({row['played_at'][:10]}) Taiko.osr"
        safe = "".join(c if c.isalnum() or c in " ._-()[]" else "_" for c in raw)
        return Response(
            content=bytes(row["content"]),
            media_type="application/x-osu-replay",
            headers={"Content-Disposition": f'attachment; filename="{safe}"'},
        )

    @app.get("/replay/{player}/{replay_id}", response_class=HTMLResponse)
    def replay_page(player: str, replay_id: int):
        conn = open_plays(workspace, player)
        row = conn.execute(
            """
            SELECT r.*, m.title AS map_title, m.artist AS map_artist,
                   m.version AS map_version, m.creator AS map_creator,
                   m.md5 AS map_md5_ref,
                   m.beatmap_id, m.beatmapset_id,
                   m.duration_s, m.hittable_notes, m.bpm_min, m.bpm_max, m.od,
                   m.rating_speed, m.rating_stamina, m.rating_gimmick,
                   m.rating_technical, m.rating_consistency
            FROM replays r JOIN catalog.maps m ON m.md5 = r.map_md5
            WHERE r.id = ?
            """,
            (replay_id,),
        ).fetchone()
        features = None
        judged = None
        if row:
            content = get_map_content(conn, row["map_md5_ref"])
            if content:
                try:
                    bm = _parse_bytes_as_osu(content)
                    features = extract_features(bm)
                    # Also re-judge to expose per-note timing deltas for the
                    # timing histogram. Cheap (~10-50ms per replay).
                    with tempfile.NamedTemporaryFile(suffix=".osr", delete=False) as tmp:
                        tmp.write(bytes(row["content"]))
                        tmp_path = tmp.name
                    try:
                        rp = parse_osr_file(tmp_path)
                    finally:
                        Path(tmp_path).unlink(missing_ok=True)
                    judged = judge_replay(bm, rp)
                except Exception:
                    features = None
                    judged = None
        conn.close()
        if not row:
            return HTMLResponse(_render_error(f"Replay {replay_id} for {player} not found."), status_code=404)
        return _render_replay(dict(row), player, features, judged)

    # --- upload ---------------------------------------------------------

    @app.get("/api/uploads/active")
    def uploads_active():
        """List uploads that are still in progress or recently completed.
        The base template polls this to render the floating status tray."""
        cutoff = time.time() - 15  # keep done/error entries visible 15s
        summary = []
        with _UPLOAD_LOCK:
            for tid, entry in list(_UPLOAD_TASKS.items()):
                if entry["stage"] not in ("done", "error"):
                    summary.append({"id": tid, **entry})
                elif entry.get("updated_at", 0) >= cutoff:
                    summary.append({"id": tid, **entry})
            # Cleanup: drop entries older than 5 minutes.
            gc_cutoff = time.time() - 300
            for tid in [t for t, e in _UPLOAD_TASKS.items() if e.get("updated_at", 0) < gc_cutoff]:
                _UPLOAD_TASKS.pop(tid, None)
        return JSONResponse(summary)

    @app.post("/upload")
    async def upload(request: Request, file: UploadFile = File(...), map_file: UploadFile | None = File(None)):
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
        # Redirect back to where the user came from; the floating tray in the
        # base template shows progress from any page.
        back = request.headers.get("referer") or "/"
        return RedirectResponse(url=back, status_code=303)

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

    @app.post("/settings/osu-user")
    async def link_osu_user(player: str = Form(...), osu_username: str = Form(...)):
        from . import osu_api
        catalog = open_catalog(workspace)
        try:
            if not osu_api.is_configured(catalog):
                catalog.close()
                return RedirectResponse(url=f"/player/{player}?err=osu-api-not-configured", status_code=303)
            user = osu_api.lookup_user(catalog, osu_username.strip())
        except osu_api.OsuApiError as e:
            catalog.close()
            return RedirectResponse(url=f"/player/{player}?err=osu-lookup-failed:{str(e)[:80]}", status_code=303)
        catalog.close()
        if user is None:
            return RedirectResponse(url=f"/player/{player}?err=osu-user-not-found:{osu_username}", status_code=303)
        conn = open_plays(workspace, player)
        db_module.set_osu_profile(
            conn, player,
            user_id=user.id, username=user.username,
            avatar_url=user.avatar_url, country_code=user.country_code,
            global_rank=user.global_rank_taiko,
            cover_url=user.cover_url,
        )
        conn.close()
        return RedirectResponse(url=f"/player/{player}?ok=osu-linked", status_code=303)

    @app.post("/settings/osu-user/unlink")
    async def unlink_osu_user(player: str = Form(...)):
        conn = open_plays(workspace, player)
        db_module.set_osu_profile(conn, player, user_id=None, username=None,
                                  avatar_url=None, country_code=None, global_rank=None)
        conn.close()
        return RedirectResponse(url=f"/player/{player}?ok=osu-unlinked", status_code=303)

    @app.post("/settings/osu-api")
    async def save_osu_api(client_id: str = Form(...), client_secret: str = Form(...)):
        from . import osu_api
        catalog = open_catalog(workspace)
        try:
            osu_api.save_credentials(catalog, client_id, client_secret)
            # Try to mint a token now so the user gets immediate feedback.
            osu_api._get_token(catalog)
            msg = "?ok=osu-api-configured"
        except Exception as e:
            msg = f"?err=osu-api-invalid:{str(e)[:80]}"
        finally:
            catalog.close()
        return RedirectResponse(url="/" + msg, status_code=303)

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
/* --- weakness pattern list --- */
.weakness-list { display: flex; flex-direction: column; gap: 8px; margin-top: 10px; }
.weakness-row { display: grid; grid-template-columns: 140px 1fr max-content; gap: 12px; align-items: center; padding: 10px 4px; border-bottom: 1px dashed var(--rule); font-family: var(--font-mono); }
.weakness-row:last-child { border-bottom: none; }
.weakness-cause { padding: 4px 10px; border-radius: 3px; font-size: 10px; letter-spacing: 0.12em; text-transform: uppercase; color: var(--ink); text-align: center; }
.weakness-sig { font-size: 14px; color: var(--ink); font-weight: 500; letter-spacing: 0.02em; }
.weakness-sig .wp-K { color: #4aa3d9; font-weight: 700; }
.weakness-sig .wp-D { color: #e55a5a; font-weight: 700; }
.weakness-sig .wp-miss { text-decoration: underline; text-decoration-thickness: 2px; text-underline-offset: 3px; filter: brightness(1.3); }
.weakness-maps { display: flex; flex-wrap: wrap; gap: 6px; margin-top: 6px; }
.weakness-map-chip { font-size: 10px; padding: 2px 8px; background: var(--panel); border: 1px solid var(--rule); border-radius: 3px; color: var(--ink-muted); text-decoration: none; }
.weakness-map-chip:hover { color: var(--accent); border-color: var(--accent-soft); }
.weakness-map-chip .muted { color: var(--ink-faint); }
.weakness-count { font-family: var(--font-mono); font-size: 16px; color: var(--ink); font-variant-numeric: tabular-nums; text-align: right; }
.weakness-count .muted { font-size: 10px; color: var(--ink-muted); display: block; }

/* --- osu! profile link section --- */
.osu-link-linked { display: flex; align-items: center; gap: 16px; padding-top: 8px; }
.osu-link-avatar { width: 72px; height: 72px; border-radius: 8px; object-fit: cover; border: 1px solid var(--rule); }
.hero-country { display: inline-block; padding: 2px 8px; font-size: 10px; letter-spacing: 0.12em; background: rgba(255,255,255,0.14); color: rgba(255,255,255,0.9); border-radius: 3px; }
.hero-rank { font-size: 11px; color: rgba(255,255,255,0.65); }

/* --- osu! avatar portrait inside player hero --- */
.hero-inner.has-avatar { grid-template-columns: 128px 1fr 320px; }
@media (max-width: 900px) {
  .hero-inner.has-avatar { grid-template-columns: 96px 1fr; }
}
.hero-avatar {
  width: 128px; height: 128px;
  border-radius: 12px;
  object-fit: cover;
  align-self: end;
  border: 2px solid rgba(255,255,255,0.16);
  box-shadow: 0 8px 24px rgba(0,0,0,0.4);
}
@media (max-width: 900px) {
  .hero-avatar { width: 96px; height: 96px; }
}

/* --- osu! API status card --- */
.api-status { display: flex; align-items: center; gap: 10px; padding: 12px 14px; border-radius: 4px; font-family: var(--font-mono); font-size: 13px; }
.api-status.connected { background: rgba(74, 119, 82, 0.14); border: 1px solid var(--great); color: var(--ink); }
.api-status.disconnected { background: rgba(176, 138, 43, 0.10); border: 1px solid var(--ok); color: var(--ink); }
.api-status .dot { width: 10px; height: 10px; border-radius: 50%; flex-shrink: 0; }
.api-status.connected .dot { background: var(--great); box-shadow: 0 0 8px rgba(74, 119, 82, 0.5); }
.api-status.disconnected .dot { background: var(--ok); }
.flash { padding: 10px 14px; border-radius: 4px; font-family: var(--font-mono); font-size: 12px; margin-bottom: 12px; }
.flash-ok { background: rgba(74, 119, 82, 0.16); border: 1px solid var(--great); color: var(--great); }
.flash-err { background: var(--accent-faint); border: 1px solid var(--accent-soft); color: var(--accent); }
code { font-family: var(--font-mono); font-size: 12px; background: var(--ground); padding: 1px 5px; border-radius: 3px; color: var(--ink); }

/* --- timing histogram --- */
.timing-hist-wrap { margin-top: 14px; padding-top: 12px; border-top: 1px dashed var(--rule); }
.timing-hist { width: 100%; max-width: 720px; height: auto; display: block; margin: 0 auto; }
.timing-hist .ht-tick { fill: var(--ink-faint); font-family: var(--font-mono); font-size: 10px; letter-spacing: 0.05em; }
.timing-hist-meta { display: flex; justify-content: space-between; gap: 12px; flex-wrap: wrap; margin-top: 8px; font-family: var(--font-mono); font-size: 11px; color: var(--ink-muted); }
.timing-hist-meta b { color: var(--ink); font-weight: 500; }

.eyebrow-row { margin-bottom: -20px; }

/* --- skill radar --- */
.radar-wrap { display: flex; justify-content: center; padding: 8px 0 12px; }
.radar { width: 100%; max-width: 620px; height: auto; }
.replay-toolbar { display: flex; align-items: center; gap: 12px; margin: 8px 0 12px; flex-wrap: wrap; }
.replay-tabs { display: flex; gap: 4px; flex-wrap: wrap; }
.replay-tabs .tab { font-family: var(--font-mono); font-size: 11px; letter-spacing: 0.04em; background: transparent; color: var(--ink-muted); border: 1px solid var(--rule); border-radius: 999px; padding: 5px 12px; cursor: pointer; transition: all 0.15s; }
.replay-tabs .tab:hover { color: var(--ink); border-color: var(--rule-strong); }
.replay-tabs .tab.active { background: var(--accent); color: white; border-color: var(--accent); }
.replay-search { font-family: var(--font-mono); font-size: 12px; padding: 6px 10px; border: 1px solid var(--rule); border-radius: 3px; background: var(--ground); color: var(--ink); flex: 1; min-width: 180px; max-width: 280px; }
.replay-search:focus { outline: none; border-color: var(--accent); }
.forecast-scroll { max-height: 380px; overflow-y: auto; padding-right: 6px; }
.forecast-scroll::-webkit-scrollbar { width: 8px; }
.forecast-scroll::-webkit-scrollbar-track { background: var(--panel); }
.forecast-scroll::-webkit-scrollbar-thumb { background: var(--rule-strong); border-radius: 4px; }
.forecast-grid { display: flex; flex-direction: column; gap: 2px; font-family: var(--font-mono); }
.target-cell { display: flex; flex-direction: column; align-items: center; gap: 2px; }
.target-acc { font-size: 9px; letter-spacing: 0.08em; color: var(--ink-faint); text-transform: uppercase; }
.target-acc-ss { color: #d4af37 !important; font-weight: 700; letter-spacing: 0.12em; }
.target-gain-pos { font-size: 13px; color: var(--great); font-weight: 400; font-variant-numeric: tabular-nums; }
.target-gain-ss { color: #d4af37 !important; font-weight: 700; font-variant-numeric: tabular-nums; text-shadow: 0 0 6px rgba(212, 175, 55, 0.35); }
.target-cell .target-gain-ss { font-size: 13px; }
.target-cell-empty { }
.forecast-row .tr.target-ceiling { font-size: 12px; color: var(--great); font-style: italic; text-align: center; }
.forecast-row .tr.target-ceiling-ss { color: #d4af37; font-weight: 700; text-shadow: 0 0 6px rgba(212, 175, 55, 0.35); }
.forecast-header { position: sticky; top: 0; background: var(--panel); z-index: 2; }
.forecast-row .tr.forecast-improved-hdr { text-align: center; }
.forecast-row .tr.forecast-current-hdr { text-align: center; }
.forecast-row .tr.contrib-val { text-align: center; }
.forecast-row { display: grid; grid-template-columns: 24px 1fr 70px 70px 70px 70px; gap: 12px; padding: 8px 4px; align-items: center; border-bottom: 1px dashed var(--rule); font-variant-numeric: tabular-nums; }
.forecast-row:last-child { border-bottom: none; }
.forecast-header { font-size: 10px; letter-spacing: 0.14em; text-transform: uppercase; color: var(--ink-muted); border-bottom: 1px solid var(--rule); }
.forecast-row .tr { text-align: right; }
.forecast-row .fc-delta { font-size: 12px; }
.forecast-row .contrib-map { color: var(--ink); text-decoration: none; font-size: 12px; }
.forecast-row .contrib-map:hover { color: var(--accent); }
.forecast-row .contrib-map .muted { color: var(--ink-faint); }
.forecast-row .contrib-val { color: var(--ink); font-weight: 500; font-size: 14px; }
.hero-skill-mini { grid-template-columns: repeat(5, 1fr) !important; }
.hero-skill-mini .k { font-size: 8px !important; }
.hero-skill-mini .v { font-size: 12px !important; }
.radar-grid { fill: none; stroke: var(--rule); stroke-width: 1; opacity: 0.55; }
.radar-axis { stroke: var(--rule); stroke-width: 1; opacity: 0.8; }
.radar-skill { fill: var(--accent); fill-opacity: 0.22; stroke: var(--accent); stroke-width: 2; stroke-linejoin: round; }
.radar-vertex { fill: var(--accent); stroke: var(--panel); stroke-width: 2; cursor: pointer; }
.radar-vertex.weakest { fill: var(--miss); }
.radar-label { font-family: var(--font-mono); font-size: 11px; cursor: pointer; }
.radar-label .dim-name { fill: var(--ink-muted); font-size: 10px; letter-spacing: 0.14em; text-transform: uppercase; }
.radar-label .dim-name.weakest { fill: var(--miss); }
.radar-label .dim-val { fill: var(--ink); font-size: 18px; font-weight: 500; font-variant-numeric: tabular-nums; }
.radar-label .radar-delta { font-size: 10px; letter-spacing: 0.06em; font-variant-numeric: tabular-nums; }
.radar-label .radar-delta.up { fill: var(--great); }
.radar-label .radar-delta.down { fill: var(--miss); }
.radar a:hover .radar-vertex { r: 8; }
.radar a:hover .dim-name { fill: var(--accent); }

/* --- player hero style desc chip --- */
.hero-style-desc { font-family: var(--font-mono); font-size: 11px; color: rgba(255,255,255,0.7); letter-spacing: 0.04em; }
.map-hero {
  position: relative;
  min-height: 260px;
  background-size: cover;
  background-position: center;
  background-color: #16181D;
  border-radius: 6px;
  overflow: hidden;
  color: #EFE9DE;
  border: 1px solid var(--rule);
}
.hero-inner {
  position: relative;
  z-index: 2;
  display: grid;
  grid-template-columns: 1fr 320px;
  gap: 32px;
  padding: 28px 32px;
  height: 100%;
  min-height: 260px;
}
@media (max-width: 800px) {
  .hero-inner { grid-template-columns: 1fr; }
}
.hero-left { display: flex; flex-direction: column; justify-content: flex-end; gap: 10px; }
.hero-pill-row { display: flex; align-items: center; gap: 10px; margin-bottom: 4px; }
.diff-pill {
  display: inline-block;
  padding: 5px 14px;
  font-family: var(--font-mono);
  font-size: 12px;
  letter-spacing: 0.06em;
  background: rgba(255,255,255,0.14);
  color: white;
  border-radius: 999px;
  border: 1px solid rgba(255,255,255,0.28);
  backdrop-filter: blur(6px);
}
.hero-fc {
  display: inline-block;
  font-family: var(--font-mono);
  font-size: 11px;
  letter-spacing: 0.14em;
  padding: 4px 10px;
  border-radius: 3px;
  font-weight: 700;
}
.hero-fc.fc { background: var(--great); color: white; }
.hero-fc.ss { background: linear-gradient(90deg, #d4af37, #f1d475); color: #3a2a00; }
.hero-title {
  font-family: var(--font-mono);
  font-size: 42px;
  line-height: 1.05;
  font-weight: 500;
  letter-spacing: -0.02em;
  color: white;
  margin: 0;
  text-shadow: 0 2px 8px rgba(0,0,0,0.5);
  text-wrap: balance;
}
.hero-artist {
  font-family: var(--font-mono);
  font-size: 16px;
  color: rgba(255,255,255,0.72);
  margin: 0;
  letter-spacing: 0.02em;
}
.hero-meta {
  font-family: var(--font-mono);
  font-size: 12px;
  color: rgba(255,255,255,0.55);
  margin: 4px 0 0 0;
}
.hero-actions { display: flex; gap: 10px; margin-top: 14px; flex-wrap: wrap; }
.hero-btn {
  display: inline-block;
  padding: 9px 18px;
  font-family: var(--font-mono);
  font-size: 12px;
  letter-spacing: 0.06em;
  background: rgba(255,255,255,0.10);
  color: rgba(255,255,255,0.9);
  border: 1px solid rgba(255,255,255,0.22);
  border-radius: 4px;
  text-decoration: none;
  backdrop-filter: blur(6px);
  transition: background 0.15s, border-color 0.15s;
}
.hero-btn:hover { background: rgba(255,255,255,0.18); border-color: rgba(255,255,255,0.42); text-decoration: none; }
.hero-btn.primary { background: var(--accent); border-color: var(--accent); }
.hero-btn.primary:hover { background: #d0453e; }
.hero-right {
  display: flex; flex-direction: column; gap: 14px; align-items: stretch;
  background: rgba(0,0,0,0.32);
  border: 1px solid rgba(255,255,255,0.08);
  border-radius: 5px;
  padding: 16px 18px;
  backdrop-filter: blur(8px);
  align-self: start;
}
.hero-scorebox { text-align: center; padding-bottom: 12px; border-bottom: 1px solid rgba(255,255,255,0.08); }
.hero-acc { font-family: var(--font-mono); font-size: 34px; font-weight: 500; color: white; letter-spacing: -0.02em; font-variant-numeric: tabular-nums; }
.hero-acc-sub { font-family: var(--font-mono); font-size: 10px; letter-spacing: 0.1em; text-transform: uppercase; color: rgba(255,255,255,0.55); margin-top: 4px; }
.hero-hits { display: grid; grid-template-columns: repeat(3, 1fr); gap: 4px; }
.hero-hits > div { display: flex; flex-direction: column; align-items: center; padding: 6px 0; font-family: var(--font-mono); }
.hero-hits .k { font-size: 10px; letter-spacing: 0.12em; text-transform: uppercase; }
.hero-hits .k.great { color: var(--great); }
.hero-hits .k.ok    { color: var(--ok); }
.hero-hits .k.miss  { color: var(--miss); }
.hero-hits .v { font-size: 18px; color: white; font-variant-numeric: tabular-nums; margin-top: 2px; }
.hero-mapinfo { display: grid; grid-template-columns: repeat(4, 1fr); gap: 8px; padding-top: 12px; border-top: 1px solid rgba(255,255,255,0.08); }
.hero-mapinfo > div { display: flex; flex-direction: column; align-items: center; font-family: var(--font-mono); }
.hero-mapinfo .k { font-size: 9px; letter-spacing: 0.14em; text-transform: uppercase; color: rgba(255,255,255,0.48); }
.hero-mapinfo .v { font-size: 13px; color: white; font-variant-numeric: tabular-nums; margin-top: 2px; }
.row-link { display: inline-block; padding: 2px 6px; margin: 0 2px; font-size: 10px; letter-spacing: 0.08em; text-transform: uppercase; background: var(--panel); color: var(--ink-muted); border: 1px solid var(--rule); border-radius: 3px; text-decoration: none; }
.row-link:hover { color: var(--accent); border-color: var(--accent-soft); text-decoration: none; }
.row-link-muted { display: inline-block; padding: 2px 6px; margin: 0 2px; font-size: 10px; letter-spacing: 0.08em; text-transform: uppercase; color: var(--ink-faint); opacity: 0.4; }
td.links { text-align: right; white-space: nowrap; }
th.links-col { text-align: right; }
/* skill progression small multiples */
.prog-grid { display: grid; grid-template-columns: repeat(5, 1fr); gap: 12px; margin-top: 4px; }
@media (max-width: 900px) { .prog-grid { grid-template-columns: repeat(2, 1fr); } }
.prog-cell { display: flex; flex-direction: column; gap: 4px; padding: 8px 6px; border: 1px solid var(--rule); border-radius: 4px; background: var(--panel); }
.prog-title { font-family: var(--font-mono); font-size: 10px; letter-spacing: 0.14em; text-transform: uppercase; color: var(--ink-muted); text-align: center; }
.prog-chart { width: 100%; height: 88px; display: block; }
.prog-footer { display: flex; justify-content: space-between; align-items: baseline; padding: 0 4px; font-family: var(--font-mono); font-variant-numeric: tabular-nums; }
.prog-current { font-size: 16px; font-weight: 500; color: var(--ink); }
.prog-delta { font-size: 11px; letter-spacing: 0.04em; }
.prog-delta.up { color: var(--great); }
.prog-delta.down { color: var(--miss); }
.prog-delta.flat { color: var(--ink-faint); }

/* floating upload tray (bottom-right) */
#uploads-tray { position: fixed; right: 20px; bottom: 20px; display: flex; flex-direction: column-reverse; gap: 10px; z-index: 999; pointer-events: none; }
.upload-toast { pointer-events: auto; width: 320px; background: var(--panel); border: 1px solid var(--rule); border-radius: 6px; padding: 12px 14px; box-shadow: 0 8px 24px rgba(0,0,0,0.35); font-family: var(--font-mono); animation: ut-in 0.25s ease-out; }
.upload-toast.leaving { opacity: 0; transform: translateX(20px); transition: opacity 0.5s, transform 0.5s; }
.upload-toast[data-stage="done"] { border-color: var(--great); }
.upload-toast[data-stage="error"] { border-color: var(--miss); }
.ut-header { display: flex; align-items: center; justify-content: space-between; margin-bottom: 8px; }
.ut-label { font-size: 12px; color: var(--ink); font-weight: 500; }
.upload-toast[data-stage="done"] .ut-label { color: var(--great); }
.upload-toast[data-stage="error"] .ut-label { color: var(--miss); }
.ut-close { background: none; border: none; color: var(--ink-faint); font-size: 16px; cursor: pointer; line-height: 1; padding: 0 4px; }
.ut-close:hover { color: var(--ink); }
.ut-bar { height: 4px; background: var(--rule); border-radius: 2px; overflow: hidden; }
.ut-fill { height: 100%; background: linear-gradient(90deg, var(--accent-cool), var(--accent)); transition: width 0.3s; }
.upload-toast[data-stage="done"] .ut-fill { background: var(--great); }
.upload-toast[data-stage="error"] .ut-fill { background: var(--miss); }
.ut-note { font-size: 10px; color: var(--ink-muted); margin-top: 6px; overflow-wrap: anywhere; }
.ut-note a { color: var(--accent); }
.ut-filename { font-size: 10px; color: var(--ink-faint); margin-top: 2px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
@keyframes ut-in { from { opacity: 0; transform: translateY(10px); } to { opacity: 1; transform: none; } }
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


_STYLE_BADGE = {
    "kddk": ("KDDK", "outer=kat, inner=don, L-R alternation"),
    "ddkk": ("DDKK", "L=don, R=kat (color-per-hand)"),
    "kkdd": ("KKDD", "L=kat, R=don (color-per-hand)"),
    "unknown": ("STYLE?", "playstyle not set"),
}

def _colorize_signature(sig: str) -> str:
    """The signature leads with a 5-char color context like 'KDdKD' (lowercase
    marks the missed note). Colorize K=blue, D=red — the standard taiko color
    coding — and mark the missed letter with underline+brightness."""
    # Only touch the leading pattern token (up to first space or ' ·').
    if not sig:
        return ""
    head, sep, tail = sig.partition(" ·")
    tokens: list[str] = []
    for ch in head:
        if ch in "K":
            tokens.append(f'<span class="wp-K">K</span>')
        elif ch in "D":
            tokens.append(f'<span class="wp-D">D</span>')
        elif ch == "k":
            tokens.append(f'<span class="wp-K wp-miss">k</span>')
        elif ch == "d":
            tokens.append(f'<span class="wp-D wp-miss">d</span>')
        else:
            tokens.append(ch)
    return "".join(tokens) + sep + tail


def _render_weakness_patterns(report, player: str) -> str:
    """Cluster the misses across the player's BEST play of each map by pattern
    signature. Surfaces genuine weaknesses evidenced by consistent misses across
    stable records, not one-off bad plays."""
    clusters = getattr(report, "weakness_clusters", ()) or ()
    if not clusters:
        return (
            '<section class="card"><h2>Weakness patterns</h2>'
            '<p class="hint">No pattern data yet. Upload replays (or run <code>refresh</code>) and this section will surface the specific pattern signatures your misses cluster around.</p>'
            '</section>'
        )
    total_misses = sum(c.miss_count for c in clusters)
    rows_html = ""
    for c in clusters:
        pct = c.miss_count / total_misses * 100 if total_misses else 0
        color = _CAUSE_COLORS.get(c.cause, "var(--ink-faint)")
        # Small list of the maps where this pattern shows up (top 5, clickable
        # to the replay detail).
        map_chips = "  ".join(
            f'<a class="weakness-map-chip" href="/replay/{player}/{rid}">{title[:22]} <span class="muted">[{ver[:14]}]</span></a>'
            for (title, ver, rid) in c.maps
        )
        sig_html = _colorize_signature(c.signature)
        rows_html += (
            f'<div class="weakness-row">'
            f'<div class="weakness-cause" style="background: {color}20; border-left: 3px solid {color};">{c.cause.replace("_", "-")}</div>'
            f'<div class="weakness-body">'
            f'<div class="weakness-sig">{sig_html}</div>'
            f'<div class="weakness-maps">{map_chips}</div>'
            f'</div>'
            f'<div class="weakness-count">{c.miss_count}<span class="muted"> misses · {pct:.0f}%</span></div>'
            f'</div>'
        )
    return f"""
  <section class="card">
    <h2>Weakness patterns</h2>
    <p class="hint">Top pattern signatures where your misses cluster, aggregated across the best play of each map. These are the patterns your play history says you genuinely struggle with — improving them moves your accuracy more than random grind.</p>
    <div class="weakness-list">{rows_html}</div>
  </section>"""


def _render_player_flash(flash_ok: str, flash_err: str) -> str:
    """Small banner above the hero for osu! link outcomes."""
    if flash_ok == "osu-linked":
        return '<div class="flash flash-ok">✓ osu! profile linked. Refresh to see the avatar as your hero background.</div>'
    if flash_ok == "osu-unlinked":
        return '<div class="flash flash-ok">✓ osu! profile unlinked.</div>'
    if flash_err.startswith("osu-user-not-found:"):
        who = flash_err[len("osu-user-not-found:"):]
        return f'<div class="flash flash-err">✗ Couldn\'t find osu! user &quot;{who}&quot;.</div>'
    if flash_err.startswith("osu-lookup-failed:"):
        why = flash_err[len("osu-lookup-failed:"):]
        return f'<div class="flash flash-err">✗ osu! lookup failed: {why}</div>'
    if flash_err == "osu-api-not-configured":
        return ('<div class="flash flash-err">✗ osu! API not configured. Set it up on the '
                '<a href="/">home page</a> first.</div>')
    return ""


def _render_osu_link_section(player: str, report) -> str:
    """Settings-style card at the bottom of the player page for linking an
    osu! profile. Shows the linked profile (with avatar) OR a form to link."""
    if getattr(report, "osu_username", None):
        avatar = report.osu_avatar_url or ""
        return f"""
  <section class="card" id="osu-link">
    <h2>Linked osu! profile</h2>
    <div class="osu-link-linked">
      {'<img src="' + avatar + '" alt="avatar" class="osu-link-avatar">' if avatar else ''}
      <div>
        <div style="font-family: var(--font-mono); font-size: 16px; color: var(--ink); font-weight: 500;">
          <a href="https://osu.ppy.sh/users/{report.osu_user_id}" target="_blank" rel="noopener">{report.osu_username}</a>
        </div>
        <div class="hint">
          osu! ID: {report.osu_user_id}{" · country: " + report.osu_country_code if report.osu_country_code else ""}{" · global rank taiko: #" + f"{report.osu_global_rank:,}" if report.osu_global_rank else ""}
        </div>
      </div>
      <form method="post" action="/settings/osu-user/unlink" style="margin-left: auto;">
        <input type="hidden" name="player" value="{player}">
        <button type="submit" class="hero-btn">Unlink</button>
      </form>
    </div>
  </section>"""
    return f"""
  <section class="card" id="osu-link">
    <h2>Link an osu! profile</h2>
    <p class="hint">Pull your avatar / country / taiko rank from osu.ppy.sh and use the avatar as this page's hero background. Requires the osu! API to be configured on the home page first.</p>
    <form class="inline-form" method="post" action="/settings/osu-user" style="margin-top: 12px;">
      <input type="hidden" name="player" value="{player}">
      <input type="text" name="osu_username" placeholder="osu! username (e.g. Acrith)" required style="flex: 1;">
      <button type="submit">Link</button>
    </form>
  </section>"""


def _render_osu_subtitle(report) -> str:
    """Under-title subtitle: shows 'training profile' if not linked, or the
    osu! username + country/rank badge if linked."""
    if not getattr(report, "osu_username", None):
        return "training profile"
    username = report.osu_username
    country = getattr(report, "osu_country_code", "") or ""
    rank = getattr(report, "osu_global_rank", None)
    parts = [f'<a href="https://osu.ppy.sh/users/{report.osu_user_id}" target="_blank" rel="noopener" style="color: inherit;">{username}</a>']
    if country:
        parts.append(f'<span class="hero-country">{country}</span>')
    if rank:
        parts.append(f'<span class="hero-rank">#{rank:,} taiko</span>')
    return "  ·  ".join(parts)


def _render_osu_profile_link(player: str, report) -> str:
    """Renders 'Link osu! profile' button (opens a details+form) or 'Unlink'
    button when already linked."""
    if getattr(report, "osu_username", None):
        return (
            f'<form method="post" action="/settings/osu-user/unlink" style="display: inline;">'
            f'<input type="hidden" name="player" value="{player}">'
            f'<button type="submit" class="hero-btn" style="cursor: pointer; background: transparent;">Unlink osu!</button>'
            f'</form>'
        )
    return f'<a class="hero-btn" href="#osu-link">Link osu! profile ↓</a>'


def _render_player_hero(report, replays: list[dict], player: str) -> str:
    """Player-profile hero card, mirrors the beatmap hero layout."""
    d = report.skill.as_dict()
    total_skill = sum(d.values())
    dominant = max(d.items(), key=lambda kv: kv[1])[0]

    # Aggregates
    unique_maps = len({r.get("map_md5") for r in replays})
    fc_count = sum(1 for r in replays if (r.get("count_miss") or 0) == 0)
    ss_count = sum(1 for r in replays
                    if (r.get("count_miss") or 0) == 0 and (r.get("count_ok") or 0) == 0)
    sess_count = len(getattr(report, "snapshot_history", ()) or ())

    style_short, style_desc = _STYLE_BADGE.get(report.style, (report.style.upper(), ""))

    # Latest session date (from snapshot_history — sorted oldest→newest)
    history = getattr(report, "snapshot_history", ()) or ()
    latest_date = ""
    if history:
        raw = history[-1].get("latest_replay_played_at", "")
        latest_date = raw[:10] if raw else ""

    # Cover image (osu! profile banner) if available — same dimming treatment
    # as the beatmap cover. Otherwise dark base with subtle warm/cool radial
    # hotspots. The avatar renders separately as a portrait next to the info.
    cover_url = getattr(report, "osu_cover_url", None)
    if cover_url:
        bg = (
            "background: "
            "linear-gradient(180deg, rgba(15,17,20,0.35) 0%, rgba(15,17,20,0.92) 100%), "
            f'url("{cover_url}"); '
            "background-size: cover; background-position: center;"
        )
    else:
        bg = ("background: "
              "radial-gradient(ellipse at 100% 20%, rgba(176,50,43,0.28) 0%, transparent 55%), "
              "radial-gradient(ellipse at 0% 100%, rgba(75,106,131,0.22) 0%, transparent 55%), "
              "#16181D;")
    avatar_url = getattr(report, "osu_avatar_url", None)
    avatar_html = (
        f'<img class="hero-avatar" src="{avatar_url}" alt="{report.osu_username or player} avatar">'
        if avatar_url else ""
    )

    return f"""
  <section class="map-hero" style='{bg}'>
    <div class="hero-inner {"has-avatar" if avatar_url else ""}">
      {avatar_html}
      <div class="hero-left">
        <div class="hero-pill-row">
          <span class="diff-pill">{style_short}</span>
          <span class="hero-style-desc">{style_desc}</span>
        </div>
        <h1 class="hero-title">{player}</h1>
        <p class="hero-artist">{_render_osu_subtitle(report)}</p>
        <p class="hero-meta">{report.replays} replays  ·  {sess_count} sessions  ·  {unique_maps} unique maps{("  ·  latest " + latest_date) if latest_date else ""}</p>
        <div class="hero-actions">
          <a class="hero-btn" href="/">← Home</a>
          {_render_osu_profile_link(player, report)}
        </div>
      </div>
      <div class="hero-right">
        <div class="hero-scorebox">
          <div class="hero-acc">{total_skill:.0f}</div>
          <div class="hero-acc-sub">total skill  ·  dominant: {dominant}</div>
        </div>
        <div class="hero-hits">
          <div><span class="k great">FC</span><span class="v">{fc_count}</span></div>
          <div><span class="k ok">SS</span><span class="v">{ss_count}</span></div>
          <div><span class="k miss">misses</span><span class="v">{report.total_misses}</span></div>
        </div>
        <div class="hero-mapinfo hero-skill-mini">
          <div><span class="k">speed</span><span class="v">{d['speed']:.0f}</span></div>
          <div><span class="k">stam</span><span class="v">{d['stamina']:.0f}</span></div>
          <div><span class="k">gim</span><span class="v">{d['gimmick']:.0f}</span></div>
          <div><span class="k">tech</span><span class="v">{d['technical']:.0f}</span></div>
          <div><span class="k">cons</span><span class="v">{d['consistency']:.0f}</span></div>
        </div>
      </div>
    </div>
  </section>"""


def _render_skill_radar(report, player: str) -> str:
    """SVG radar chart of the 5-D skill vector. Values normalized to the map's
    max dimension so shape (relative disparity) reads clearly. Each vertex is
    a click-through to /player/{name}/train/{dim}."""
    import math as _math

    dims = ("speed", "stamina", "gimmick", "technical", "consistency")
    d = report.skill.as_dict()
    max_val = max(d.values()) or 1

    # Extra width on the sides so the "CONSISTENCY" label (11 chars) doesn't
    # clip the leftmost boundary.
    W, H = 620, 400
    cx, cy = W / 2, H / 2 - 6
    r_max = 130

    # 5 axes at 72° intervals, SPEED at top
    angles = {dim: -_math.pi/2 + i * (2*_math.pi/5) for i, dim in enumerate(dims)}

    def pt(dim, r):
        a = angles[dim]
        return (cx + r * _math.cos(a), cy + r * _math.sin(a))

    # Grid rings at 25/50/75/100% of r_max
    rings_svg = ""
    for pct in (0.25, 0.5, 0.75, 1.0):
        r = r_max * pct
        pts = " ".join(f"{pt(dim, r)[0]:.1f},{pt(dim, r)[1]:.1f}" for dim in dims)
        rings_svg += f'<polygon class="radar-grid" points="{pts}"/>'

    # Axis lines from center to outer ring
    axes_svg = ""
    for dim in dims:
        ex, ey = pt(dim, r_max)
        axes_svg += f'<line class="radar-axis" x1="{cx:.1f}" y1="{cy:.1f}" x2="{ex:.1f}" y2="{ey:.1f}"/>'

    # Skill polygon: fill filled at value / max_val
    skill_pts = []
    for dim in dims:
        val = d[dim]
        r = r_max * min(1.0, val / max_val)
        x, y = pt(dim, r)
        skill_pts.append(f"{x:.1f},{y:.1f}")
    skill_poly = f'<polygon class="radar-skill" points="{" ".join(skill_pts)}"/>'

    # Vertices + external labels, each wrapped in a clickable link
    vertices_svg = ""
    delta_map = report.skill_delta or {}
    for dim in dims:
        val = d[dim]
        pct = min(1.0, val / max_val)
        vx, vy = pt(dim, r_max * pct)
        is_weakest = dim == report.weakest_dim
        # Label position — just outside the outer ring
        lr = r_max + 42
        lx, ly = pt(dim, lr)
        # Text-anchor depending on angle
        a = angles[dim]
        ax = _math.cos(a)
        if abs(ax) < 0.2:
            anchor = "middle"
        elif ax > 0:
            anchor = "start"
        else:
            anchor = "end"
        # Delta arrow
        delta_svg = ""
        if delta_map:
            delta = delta_map.get(dim, 0)
            if abs(delta) >= 0.5:
                cls = "up" if delta > 0 else "down"
                sign = "↑" if delta > 0 else "↓"
                delta_svg = f'<tspan class="radar-delta {cls}" dy="15" x="{lx:.1f}">{sign}{delta:+.0f}</tspan>'
        star = " ★" if is_weakest else ""
        cls_extra = " weakest" if is_weakest else ""
        vertices_svg += (
            f'<a href="/player/{player}/train/{dim}">'
            f'<circle class="radar-vertex{cls_extra}" cx="{vx:.1f}" cy="{vy:.1f}" r="6"/>'
            f'<text class="radar-label" x="{lx:.1f}" y="{ly:.1f}" text-anchor="{anchor}">'
            f'<tspan class="dim-name{cls_extra}">{dim}{star}</tspan>'
            f'<tspan class="dim-val" x="{lx:.1f}" dy="16">{val:.0f}</tspan>'
            f'{delta_svg}'
            f'</text>'
            f'</a>'
        )

    return f"""
    <div class="radar-wrap">
      <svg viewBox="0 0 {W} {H}" class="radar" preserveAspectRatio="xMidYMid meet">
        {rings_svg}
        {axes_svg}
        {skill_poly}
        {vertices_svg}
      </svg>
    </div>
    """


def _render_map_hero(row: dict, player: str) -> str:
    """Big visual hero card for a replay's map: cover image + diff pill + title +
    action buttons + play stats. Modeled loosely on the osu! beatmap page hero."""
    setid = row.get("beatmapset_id")
    bid = row.get("beatmap_id")
    # osu.ppy.sh serves beatmap cover images at a predictable URL. Fall back to a
    # solid color if the map has no beatmapset_id (unranked / local-only).
    cover_bg = (
        f'background-image: linear-gradient(180deg, rgba(15,17,20,0.35) 0%, rgba(15,17,20,0.92) 100%), url("https://assets.ppy.sh/beatmaps/{setid}/covers/cover@2x.jpg");'
        if setid else
        'background: linear-gradient(135deg, var(--accent-cool), var(--accent) 90%);'
    )

    # Stats row: reported vs judged accuracy, great/ok/miss, max_combo indicator.
    misses = row.get("count_miss") or 0
    oks = row.get("count_ok") or 0
    combo_indicator = ""
    if misses == 0 and oks == 0:
        combo_indicator = '<span class="hero-fc ss">SS</span>'
    elif misses == 0:
        combo_indicator = '<span class="hero-fc fc">FC</span>'

    # Map metadata compact row: BPM, notes, duration, OD.
    dur_s = int(row.get("duration_s") or 0)
    duration_str = f"{dur_s // 60}:{dur_s % 60:02d}"
    bpm_min = row.get("bpm_min") or 0
    bpm_max = row.get("bpm_max") or 0
    bpm_str = f"{bpm_min:.0f}" if abs(bpm_min - bpm_max) < 0.5 else f"{bpm_min:.0f}–{bpm_max:.0f}"
    hittable = row.get("hittable_notes") or 0
    od = row.get("od") or 0

    beatmap_btn = (
        f'<a class="hero-btn primary" href="https://osu.ppy.sh/beatmaps/{bid}" target="_blank" rel="noopener">Beatmap page</a>'
        if bid else ''
    )
    osr_btn = f'<a class="hero-btn" href="/replay/{player}/{row["id"]}/osr" download>Download .osr</a>'
    inspector_btn = f'<a class="hero-btn" href="/replay/{player}/{row["id"]}/inspect">Open inspector</a>'

    played = row["played_at"][:19].replace("T", " ")
    return f"""
  <section class="map-hero" style='{cover_bg}'>
    <div class="hero-inner">
      <div class="hero-left">
        <div class="hero-pill-row">
          <span class="diff-pill">{row['map_version']}</span>
          {combo_indicator}
        </div>
        <h1 class="hero-title">{row['map_title']}</h1>
        <p class="hero-artist">{row.get('map_artist', '')}</p>
        <p class="hero-meta">mapped by <b>{row.get('map_creator','?')}</b>  ·  played {played}</p>
        <div class="hero-actions">
          {beatmap_btn}
          {osr_btn}
          {inspector_btn}
        </div>
      </div>
      <div class="hero-right">
        <div class="hero-scorebox">
          <div class="hero-acc">{row['accuracy_judged']*100:.2f}%</div>
          <div class="hero-acc-sub">judged accuracy  ·  reported {row['accuracy_reported']*100:.2f}%</div>
        </div>
        <div class="hero-hits">
          <div><span class="k great">great</span><span class="v">{row['count_great']}</span></div>
          <div><span class="k ok">ok</span><span class="v">{row['count_ok']}</span></div>
          <div><span class="k miss">miss</span><span class="v">{row['count_miss']}</span></div>
        </div>
        <div class="hero-mapinfo">
          <div><span class="k">BPM</span><span class="v">{bpm_str}</span></div>
          <div><span class="k">length</span><span class="v">{duration_str}</span></div>
          <div><span class="k">notes</span><span class="v">{hittable}</span></div>
          <div><span class="k">OD</span><span class="v">{od:.1f}</span></div>
        </div>
      </div>
    </div>
  </section>"""


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

<div id="uploads-tray" aria-live="polite"></div>
<script>
(function() {{
  // Polls /api/uploads/active on every page; renders one card per in-flight
  // or recently-completed upload in the bottom-right corner. Uploads survive
  // page navigation because the server keeps them in a shared dict.
  const tray = document.getElementById('uploads-tray');
  if (!tray) return;
  const state = new Map();  // id -> {{el, doneAt}}
  // ids the user closed — must not re-render even if server still lists them.
  // Persisted to sessionStorage so F5 keeps the toast dismissed within this tab.
  const STORAGE_KEY = 'tt-dismissed-uploads';
  const dismissed = new Set();
  try {{
    for (const id of JSON.parse(sessionStorage.getItem(STORAGE_KEY) || '[]')) {{
      dismissed.add(id);
    }}
  }} catch (e) {{}}
  function persistDismissed() {{
    try {{ sessionStorage.setItem(STORAGE_KEY, JSON.stringify([...dismissed])); }} catch (e) {{}}
  }}

  function make(id) {{
    const el = document.createElement('div');
    el.className = 'upload-toast';
    el.innerHTML = `
      <div class="ut-header">
        <span class="ut-label"></span>
        <button class="ut-close" title="dismiss">×</button>
      </div>
      <div class="ut-bar"><div class="ut-fill"></div></div>
      <div class="ut-note"></div>
      <div class="ut-filename"></div>`;
    tray.appendChild(el);
    el.querySelector('.ut-close').addEventListener('click', () => {{
      dismissed.add(id);
      persistDismissed();
      el.remove();
      state.delete(id);
    }});
    return el;
  }}

  async function poll() {{
    let list;
    try {{
      const r = await fetch('/api/uploads/active');
      if (!r.ok) throw new Error('http ' + r.status);
      list = await r.json();
    }} catch(e) {{
      setTimeout(poll, 3000);
      return;
    }}
    const seen = new Set();
    for (const s of list) {{
      if (dismissed.has(s.id)) continue;  // user closed this toast — don't resurrect it
      seen.add(s.id);
      let entry = state.get(s.id);
      if (!entry) {{
        entry = {{ el: make(s.id), doneAt: 0 }};
        state.set(s.id, entry);
      }}
      entry.el.dataset.stage = s.stage;
      entry.el.querySelector('.ut-label').textContent = s.label || s.stage || '';
      entry.el.querySelector('.ut-fill').style.width = (s.pct || 0) + '%';
      entry.el.querySelector('.ut-note').textContent = s.note || '';
      entry.el.querySelector('.ut-filename').textContent = s.filename || '';
      if (s.stage === 'error') {{
        entry.el.querySelector('.ut-note').textContent = s.error || 'failed';
      }} else if (s.stage === 'done' && !entry.doneAt) {{
        entry.doneAt = Date.now();
        if (s.redirect) {{
          const note = entry.el.querySelector('.ut-note');
          note.innerHTML = '';
          const a = document.createElement('a');
          a.href = s.redirect;
          a.textContent = 'view report →';
          note.appendChild(a);
        }}
      }}
    }}
    // Fade + drop toasts whose task disappeared server-side.
    for (const [id, entry] of state) {{
      if (!seen.has(id)) {{
        entry.el.classList.add('leaving');
        setTimeout(() => {{ entry.el.remove(); state.delete(id); }}, 500);
      }}
    }}
    // Clean up dismissed ids the server has already GC'd — otherwise the set
    // grows forever across dismissals within the same tab.
    const active_ids = new Set(list.map(s => s.id));
    let dirty = false;
    for (const id of dismissed) {{
      if (!active_ids.has(id)) {{ dismissed.delete(id); dirty = true; }}
    }}
    if (dirty) persistDismissed();
    setTimeout(poll, 800);
  }}
  poll();
}})();
</script>
</body></html>"""


def _render_home(workspace: str, catalog_stats: dict, players: list[dict], roots: list[str],
                 api_configured: bool = False, flash_ok: str = "", flash_err: str = "") -> str:
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
      <p class="hint">Drop a .osr file (and optionally a matching .osu). If the map is already in the DB, under a configured root, or resolvable via the osu! API, we'll fetch it automatically.</p>
      <input type="file" name="file" accept=".osr" required>
      <input type="file" name="map_file" accept=".osu">
      <button type="submit">Upload &amp; analyze</button>
    </form>
  </section>

  {_render_osu_api_card(api_configured, flash_ok, flash_err)}
"""
    return _html_page("Home", body, active="home")


def _render_osu_api_card(configured: bool, flash_ok: str, flash_err: str) -> str:
    """Show osu! API OAuth status + setup form."""
    banner = ""
    if flash_ok == "osu-api-configured":
        banner = '<div class="flash flash-ok">✓ osu! API credentials saved and validated.</div>'
    elif flash_err.startswith("osu-api-invalid:"):
        banner = f'<div class="flash flash-err">✗ Could not validate credentials: {flash_err[len("osu-api-invalid:"):]}</div>'

    if configured:
        status = (
            '<div class="api-status connected">'
            '<span class="dot"></span>'
            '<span>Connected — replays whose maps aren\'t local will auto-fetch from the osu! API + mirror.</span>'
            '</div>'
        )
        form = ""
        expand_note = '<p class="hint" style="margin-top: 10px;">To re-enter credentials, expand the form below.</p>'
        form = f"""
        <details style="margin-top: 12px;">
          <summary class="hint" style="cursor: pointer;">Update credentials</summary>
          {_render_osu_api_form()}
        </details>
        """
    else:
        status = (
            '<div class="api-status disconnected">'
            '<span class="dot"></span>'
            '<span>Not configured — uploads that need a map missing from your Songs folder will fail.</span>'
            '</div>'
        )
        form = _render_osu_api_form()

    return f"""
  <section class="card">
    <h2>osu! API integration</h2>
    {banner}
    {status}
    <p class="hint" style="margin-top: 10px;">
      One-time setup: go to
      <a href="https://osu.ppy.sh/home/account/edit" target="_blank" rel="noopener">osu.ppy.sh/home/account/edit</a>
      → OAuth → New OAuth Application. Any Application Name and Callback URL <code>http://localhost:8000</code>. Copy the <b>Client ID</b> and <b>Client Secret</b> here.
    </p>
    {form}
  </section>"""


def _render_osu_api_form() -> str:
    return """
      <form class="inline-form" method="post" action="/settings/osu-api" style="flex-direction: column; align-items: stretch; gap: 8px;">
        <input type="text" name="client_id" placeholder="Client ID (number)" required>
        <input type="password" name="client_secret" placeholder="Client Secret" required>
        <button type="submit" style="align-self: flex-start;">Save &amp; validate</button>
      </form>
    """


def _render_report(report, replays: list[dict] | None = None, player_name: str | None = None,
                   flash_ok: str = "", flash_err: str = "") -> str:
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
      <h2>Latest session <span style="color: var(--ink-muted); font-size: 12px; margin-left: 8px;">{latest.start[:16].replace("T", " ")}</span></h2>
      <div class="stats-row">
        <div class="stat"><span class="k">replays</span><span class="v">{len(latest.replays)}</span></div>
        <div class="stat"><span class="k">accuracy</span><span class="v">{latest.weighted_accuracy*100:.2f}%{acc_delta}</span></div>
        <div class="stat"><span class="k">delta σ</span><span class="v">{latest.avg_delta_stddev_ms:.1f} ms{stddev_delta}</span></div>
        <div class="stat"><span class="k">misses</span><span class="v">{latest.total_misses}</span></div>
        <div class="stat"><span class="k">cheese</span><span class="v">{latest.avg_cheese_rate*100:.2f}%{cheese_delta}</span></div>
      </div>
      {"<p class='hint' style='margin-top: 12px;'>compared to session at " + prev.start[:16].replace("T", " ") + "</p>" if prev else ""}
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

    flash_banner = _render_player_flash(flash_ok, flash_err)
    body = f"""
  {flash_banner}
  {_render_player_hero(report, replays or [], player_name or report.player)}

  <section class="card">
    <h2>Skill vector</h2>
    <p class="hint">click any dimension to see maps that push it  ·  ★ = weakest = training target</p>
    {_render_skill_radar(report, player_name or report.player)}
  </section>

  {_render_progression_chart(getattr(report, "snapshot_history", ()))}

  <section class="card">
    <h2>What drove each dimension</h2>
    <p class="hint">top-3 replays per dim, weighted top-K aggregation</p>
    {dim_bars}
  </section>

  {sess_html}

  <section class="card">
    <h2>Dominant miss causes (all replays)</h2>
    {causes_html or "<p class='hint'>No classified misses.</p>"}
  </section>

  {_render_weakness_patterns(report, player_name or report.player)}

  {_render_replays_table(replays or [], player_name or report.player)}

  {_render_osu_link_section(player_name or report.player, report)}
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


_PROGRESSION_DIMS = ("speed", "stamina", "gimmick", "technical", "consistency")

def _render_progression_chart(history: tuple) -> str:
    """5 small multiples of the skill vector across sessions. Each dimension
    gets its own y-scale so a weak dim isn't crushed by a strong one."""
    history = list(history)
    if len(history) < 2:
        return (
            '<section class="card"><h2>Skill progression</h2>'
            '<p class="hint">need at least two training sessions to plot a trend — come back after your next play session.</p>'
            '</section>'
        )

    n = len(history)
    W, H = 240, 88  # per-chart dimensions
    pad_l, pad_r, pad_t, pad_b = 8, 8, 6, 18

    def _fmt_date(iso: str) -> str:
        # "2026-07-18T15:34:23..." → "07-18"
        return iso[5:10]

    columns = []
    for dim in _PROGRESSION_DIMS:
        vals = [s[f"skill_{dim}"] for s in history]
        vmax = max(vals) or 1
        vmin = min(vals)
        # Pad the y-range 5% either side so lines don't touch the edges,
        # and lock the axis to zero at the bottom if the whole trace sits
        # near zero (weak dim) so growth reads clearly.
        span = max(vmax - vmin, 1.0)
        y_lo = max(0, vmin - span * 0.15)
        y_hi = vmax + span * 0.15
        y_range = y_hi - y_lo or 1

        # Compute point coordinates in SVG space.
        def sx(i): return pad_l + (W - pad_l - pad_r) * (i / max(1, n - 1))
        def sy(v): return pad_t + (H - pad_t - pad_b) * (1 - (v - y_lo) / y_range)

        pts = " ".join(f"{sx(i):.1f},{sy(v):.1f}" for i, v in enumerate(vals))
        dots = "".join(
            f'<circle cx="{sx(i):.1f}" cy="{sy(v):.1f}" r="2.5" fill="var(--accent)"/>'
            for i, v in enumerate(vals)
        )
        # Area fill under the line (subtle).
        area = f"M{sx(0):.1f},{pad_t + (H - pad_t - pad_b):.1f} L" + \
               " L".join(f"{sx(i):.1f},{sy(v):.1f}" for i, v in enumerate(vals)) + \
               f" L{sx(n-1):.1f},{pad_t + (H - pad_t - pad_b):.1f} Z"

        current = vals[-1]
        first = vals[0]
        delta = current - first
        delta_class = "up" if delta > 0.5 else ("down" if delta < -0.5 else "flat")
        delta_str = (f"+{delta:.0f}" if delta > 0 else f"{delta:.0f}") if abs(delta) >= 0.5 else "·"

        columns.append(f"""
        <div class="prog-cell">
          <div class="prog-title">{dim}</div>
          <svg class="prog-chart" viewBox="0 0 {W} {H}" preserveAspectRatio="none">
            <path d="{area}" fill="var(--accent)" opacity="0.08"/>
            <polyline points="{pts}" fill="none" stroke="var(--accent)" stroke-width="1.5" stroke-linejoin="round"/>
            {dots}
          </svg>
          <div class="prog-footer">
            <span class="prog-current">{current:.0f}</span>
            <span class="prog-delta {delta_class}">{delta_str}</span>
          </div>
        </div>""")

    first_date = _fmt_date(history[0]["latest_replay_played_at"])
    last_date = _fmt_date(history[-1]["latest_replay_played_at"])

    return f"""
  <section class="card">
    <h2>Skill progression <span class="hint" style="font-size: 12px; font-weight: 400; margin-left: 8px;">across {n} sessions ({first_date} → {last_date})</span></h2>
    <div class="prog-grid">
      {"".join(columns)}
    </div>
    <p class="hint" style="margin-top: 14px;">delta = latest − oldest session in this window</p>
  </section>"""


def _fmt_gain(gain: float) -> str:
    if gain < 0.5:
        return '<span style="color: var(--ink-faint);">—</span>'
    return f'<span style="color: var(--great);">+{gain:.0f}</span>'


_DIM_TAGLINE = {
    "speed":       "motor tempo — how fast your hands alternate",
    "stamina":     "endurance — long high-density stretches without dropping",
    "gimmick":     "reading pressure — SV variance, obscured densities",
    "technical":   "pattern awareness — mono runs, mixed divisors, parity",
    "consistency": "unwavering timing — no random drops from bursts / parity flips",
}


def _render_train_page(player: str, dim: str, skill, suggestions, contribs) -> str:
    from .player import _accuracy_scaling, _DECAY

    d = skill.as_dict()
    val = d[dim]

    # Accuracy ladder — targets get finer-grained as you approach SS.
    _ACC_LADDER = (0.92, 0.94, 0.95, 0.96, 0.97, 0.98, 0.985, 0.99, 0.9925, 0.995, 1.0)

    def _next_targets(cur_acc):
        """Next 3 ladder rungs strictly above the current accuracy."""
        return tuple(x for x in _ACC_LADDER if x > cur_acc + 0.0005)[:3]

    def _potential_gain(c, i, target_acc):
        cur_scale = _accuracy_scaling(c.accuracy)
        tgt_scale = _accuracy_scaling(target_acc)
        if tgt_scale <= cur_scale:
            return 0.0
        weight = _DECAY ** i
        return c.raw_rating * (tgt_scale - cur_scale) * weight

    def _fmt_target_cell(target, gain):
        if target is None or gain < 0.5:
            return '<span class="tr target-cell-empty"></span>'
        is_ss = target >= 1.0
        acc_pct = "SS" if is_ss else f"{target*100:.2f}%".rstrip("0").rstrip(".")
        acc_cls = "target-acc target-acc-ss" if is_ss else "target-acc"
        gain_cls = "target-gain-ss" if is_ss else "target-gain-pos"
        return (
            f'<span class="tr target-cell">'
            f'<span class="{acc_cls}">{acc_pct}</span>'
            f'<span class="{gain_cls}">+{gain:.0f}</span>'
            f'</span>'
        )

    contribs_html = ""
    if contribs:
        header = (
            '<div class="forecast-row forecast-header">'
            '<span></span><span>map</span>'
            '<span class="tr forecast-current-hdr">current</span>'
            '<span class="tr forecast-improved-hdr" style="grid-column: span 3;">if improved</span>'
            '</div>'
        )
        parts = []
        for i, c in enumerate(contribs):
            targets = _next_targets(c.accuracy)
            # Compute (target, gain) pairs and drop the ones with no meaningful
            # gain (rounds to 0 — happens above 99.5% where our _accuracy_scaling
            # saturates). Pad the tail with (None, 0) so grid alignment holds.
            paired = [(t, _potential_gain(c, i, t)) for t in targets]
            useful = [(t, g) for t, g in paired if g >= 0.5]
            while len(useful) < 3:
                useful.append((None, 0.0))
            if not any(g >= 0.5 for t, g in paired):
                if c.accuracy >= 0.9999:
                    ceiling_cls = "target-ceiling target-ceiling-ss"
                    note = "already SS"
                else:
                    ceiling_cls = "target-ceiling"
                    note = "SS gain rounds to 0 at this rank"
                cells_html = (
                    f'<span class="tr {ceiling_cls}" style="grid-column: span 3;">'
                    f'{note}'
                    f'</span>'
                )
            else:
                cells_html = "".join(_fmt_target_cell(t, g) for t, g in useful)
            parts.append(
                f'<div class="forecast-row">'
                f'<span class="contrib-meta">#{i+1}</span>'
                f'<a href="/replay/{player}/{c.replay_id}" class="contrib-map">{c.map_title} <span class="muted">[{c.map_diff}]</span><br><span class="contrib-meta">rating {c.raw_rating:.0f} · acc {c.accuracy*100:.2f}%</span></a>'
                f'<span class="tr contrib-val">{c.weighted:.0f}</span>'
                f'{cells_html}'
                f'</div>'
            )
        rows = "".join(parts)
        n_shown = len(contribs)
        contribs_html = (
            f'<section class="card"><h2>What drove your {dim} = {val:.0f}</h2>'
            f'<p class="hint">weighted top-K aggregation ({n_shown} contributors). '
            f'Right columns show the next accuracy breakpoints above your current — and the gain you\'d earn from hitting each.</p>'
            f'<div class="forecast-scroll"><div class="forecast-grid">{header}{rows}</div></div>'
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
    <h2>New maps to try for {dim}</h2>
    <p class="hint">picked from maps in your catalog you HAVEN'T played yet. Ranked by "will it grow your {dim}?" (higher rating than yours) minus "will it overwhelm you?" (much harder than your profile in the OTHER dims). Empty list usually means your catalog is thin — upload more replays or (future) enable the osu! API map fetcher.</p>
    {sugg_html}
  </section>
"""
    return _html_page(f"train {dim} — {player}", body)


def _render_replays_table(replays: list[dict], player: str) -> str:
    from .player import _accuracy_scaling
    if not replays:
        return ""
    rows = ""
    for idx, r in enumerate(replays):
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
        bid = r.get("beatmap_id")
        osu_link = (
            f'<a class="row-link" href="https://osu.ppy.sh/beatmaps/{bid}" target="_blank" rel="noopener" title="open on osu.ppy.sh">osu!</a>'
            if bid else '<span class="row-link-muted">osu!</span>'
        )
        osr_link = f'<a class="row-link" href="/replay/{player}/{r["id"]}/osr" title="download .osr (opens in osu! if installed)" download>.osr</a>'
        links_cell = f'<td class="links">{osu_link}  {osr_link}</td>'
        # Per-dim contribution (rating × accuracy_scaling), used for sort.
        acc_scale = _accuracy_scaling((r.get("accuracy_judged") or 0))
        c_speed       = (r.get("rating_speed") or 0) * acc_scale
        c_stamina     = (r.get("rating_stamina") or 0) * acc_scale
        c_gimmick     = (r.get("rating_gimmick") or 0) * acc_scale
        c_technical   = (r.get("rating_technical") or 0) * acc_scale
        c_consistency = (r.get("rating_consistency") or 0) * acc_scale
        # data-* used by client-side sort + filter
        raw_played = (r.get("played_at") or "").replace("T", " ")
        title_lc = title.lower() + " " + version.lower()
        rows += (
            f'<tr class="row-nav replay-row" data-href="/replay/{player}/{r["id"]}" '
            f'data-idx="{idx}" data-title="{title_lc}" data-date="{raw_played}" '
            f'data-c-speed="{c_speed:.1f}" data-c-stamina="{c_stamina:.1f}" '
            f'data-c-gimmick="{c_gimmick:.1f}" data-c-technical="{c_technical:.1f}" '
            f'data-c-consistency="{c_consistency:.1f}" '
            f'style="cursor:pointer">'
            f'<td class="name">{badge}{title} <span style="color: var(--ink-muted); font-size: 11px;">[{version}]</span></td>'
            f'<td class="muted">{played}</td>'
            f'<td>{acc:.2f}%</td>'
            f'{miss_cell}'
            f'<td>{stddev:.1f} ms</td>'
            f'<td>{cheese:.2f}%</td>'
            f'{links_cell}'
            f'</tr>'
        )
    return f"""
  <section class="card">
    <h2>Replays ({len(replays)})</h2>
    <div class="replay-toolbar">
      <div class="replay-tabs">
        <button class="tab active" data-sort="date">Recent</button>
        <button class="tab" data-sort="c-speed">Top speed</button>
        <button class="tab" data-sort="c-stamina">Top stamina</button>
        <button class="tab" data-sort="c-gimmick">Top gimmick</button>
        <button class="tab" data-sort="c-technical">Top technical</button>
        <button class="tab" data-sort="c-consistency">Top consistency</button>
      </div>
      <input type="search" class="replay-search" placeholder="filter map title…" aria-label="search replays">
    </div>
    <div style="overflow-x: auto;">
      <table id="replay-table">
        <thead><tr>
          <th>map</th><th>played</th><th>acc</th><th>miss</th><th>Δ σ</th><th>cheese</th><th class="links-col">links</th>
        </tr></thead>
        <tbody id="replay-tbody">{rows}</tbody>
      </table>
    </div>
    <p class="hint" style="margin-top: 8px;">click a row to see per-note breakdown  ·  osu! opens the beatmap page  ·  .osr downloads and opens in-game if installed</p>
    <script>
      (function() {{
        const tbody = document.getElementById('replay-tbody');
        const originalOrder = Array.from(tbody.querySelectorAll('.replay-row'));
        const search = document.querySelector('.replay-search');
        const tabs = document.querySelectorAll('.replay-tabs .tab');

        // Row navigation that respects link clicks.
        tbody.addEventListener('click', ev => {{
          const tr = ev.target.closest('.row-nav');
          if (!tr) return;
          if (ev.target.closest('a')) return;
          window.location = tr.dataset.href;
        }});

        function apply(sortKey, filterText) {{
          const rows = originalOrder.slice();
          if (sortKey === 'date') {{
            rows.sort((a, b) => (b.dataset.date || '').localeCompare(a.dataset.date || ''));
          }} else {{
            rows.sort((a, b) => (parseFloat(b.dataset[toCamel(sortKey)] || 0)) - (parseFloat(a.dataset[toCamel(sortKey)] || 0)));
          }}
          const q = (filterText || '').toLowerCase().trim();
          tbody.innerHTML = '';
          for (const r of rows) {{
            const matches = !q || r.dataset.title.includes(q);
            r.style.display = matches ? '' : 'none';
            tbody.appendChild(r);
          }}
        }}
        function toCamel(s) {{ return s.replace(/-([a-z])/g, (_, c) => c.toUpperCase()); }}

        let currentSort = 'date';
        tabs.forEach(t => {{
          t.addEventListener('click', () => {{
            tabs.forEach(x => x.classList.remove('active'));
            t.classList.add('active');
            currentSort = t.dataset.sort;
            apply(currentSort, search.value);
          }});
        }});
        search.addEventListener('input', () => apply(currentSort, search.value));
      }})();
    </script>
  </section>"""


def _render_replay(row: dict, player: str, features=None, judged=None) -> str:
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
    hero_section = _render_map_hero(row, player)

    body = f"""
  <section class="eyebrow-row">
    <span class="eyebrow"><a href="/player/{player}" style="color: var(--ink-muted);">← {player}</a>  ·  replay #{row['id']}</span>
  </section>

  {hero_section}

  {warning_html}

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
    {_render_timing_histogram(judged) if judged else ""}
  </section>
"""
    return _html_page(f"replay #{row['id']}", body)


def _render_timing_histogram(judged) -> str:
    """SVG histogram of hit deltas with great/ok window bands overlaid.
    Buckets are 4ms wide; range clamped to ±100ms with an "outlier" bucket
    on each edge for anything beyond."""
    from .judgment import Verdict as _V
    deltas = [j.hit_delta_ms for j in judged.judgments if j.hit_delta_ms is not None]
    if not deltas:
        return ""

    great_w = judged.windows.great
    ok_w = judged.windows.ok

    # Buckets: 4ms wide, from -100 to +100. First/last are "spillover" buckets.
    bucket_size = 4
    range_min, range_max = -100, 100
    n_buckets = (range_max - range_min) // bucket_size  # 50
    counts = [0] * n_buckets
    for d in deltas:
        # Clamp to first/last bucket range for spillover.
        if d < range_min:
            counts[0] += 1
        elif d >= range_max:
            counts[-1] += 1
        else:
            counts[(d - range_min) // bucket_size] += 1

    max_count = max(counts) or 1
    W, H = 720, 180
    pad_l, pad_r, pad_t, pad_b = 32, 12, 8, 32
    plot_w = W - pad_l - pad_r
    plot_h = H - pad_t - pad_b

    def x_of(delta_ms):
        return pad_l + (delta_ms - range_min) / (range_max - range_min) * plot_w
    def y_of(count):
        return pad_t + plot_h - (count / max_count) * plot_h

    # Great/ok/miss window rectangles
    # Great: [-great_w, +great_w]
    # OK:    [-ok_w, +ok_w]  (which contains great)
    ok_x1 = x_of(-ok_w)
    ok_x2 = x_of(ok_w)
    gr_x1 = x_of(-great_w)
    gr_x2 = x_of(great_w)

    bands = (
        f'<rect x="{ok_x1:.1f}" y="{pad_t}" width="{ok_x2-ok_x1:.1f}" height="{plot_h}" fill="var(--ok)" opacity="0.10"/>'
        f'<rect x="{gr_x1:.1f}" y="{pad_t}" width="{gr_x2-gr_x1:.1f}" height="{plot_h}" fill="var(--great)" opacity="0.14"/>'
    )

    # Histogram bars, colored by which window the bucket center falls in.
    bars_svg = []
    for i, c in enumerate(counts):
        if c == 0: continue
        center = range_min + i * bucket_size + bucket_size / 2
        if abs(center) < great_w:
            color = "var(--great)"
        elif abs(center) < ok_w:
            color = "var(--ok)"
        else:
            color = "var(--miss)"
        x1 = x_of(range_min + i * bucket_size)
        x2 = x_of(range_min + (i + 1) * bucket_size)
        y = y_of(c)
        bars_svg.append(f'<rect x="{x1:.1f}" y="{y:.1f}" width="{x2-x1-1:.1f}" height="{pad_t+plot_h-y:.1f}" fill="{color}" opacity="0.85"/>')

    # Center line at 0 and mean line
    mean_delta = sum(deltas) / len(deltas)
    zero_x = x_of(0)
    mean_x = x_of(max(range_min, min(range_max, mean_delta)))
    lines = (
        f'<line x1="{zero_x:.1f}" y1="{pad_t}" x2="{zero_x:.1f}" y2="{pad_t+plot_h}" stroke="var(--ink)" stroke-width="1" opacity="0.5"/>'
        f'<line x1="{mean_x:.1f}" y1="{pad_t}" x2="{mean_x:.1f}" y2="{pad_t+plot_h}" stroke="var(--accent)" stroke-width="1.5" stroke-dasharray="4 3" opacity="0.9"/>'
    )

    # X-axis ticks at -80, -40, 0, 40, 80 ms
    ticks = ""
    for tick in (-80, -40, 0, 40, 80):
        tx = x_of(tick)
        ticks += (
            f'<line x1="{tx:.1f}" y1="{pad_t+plot_h}" x2="{tx:.1f}" y2="{pad_t+plot_h+4}" stroke="var(--ink-faint)"/>'
            f'<text x="{tx:.1f}" y="{pad_t+plot_h+18}" text-anchor="middle" class="ht-tick">{tick:+d} ms</text>'
        )

    # Summary counts
    n_early = sum(1 for d in deltas if d < 0)
    n_late = sum(1 for d in deltas if d > 0)
    bias = "early" if n_early > n_late * 1.15 else "late" if n_late > n_early * 1.15 else "balanced"

    return f"""
    <div class="timing-hist-wrap">
      <svg viewBox="0 0 {W} {H}" class="timing-hist" preserveAspectRatio="xMidYMid meet">
        {bands}
        {"".join(bars_svg)}
        {lines}
        {ticks}
        <text x="{pad_l}" y="{pad_t + plot_h + 18}" class="ht-tick" text-anchor="start">early</text>
        <text x="{W - pad_r}" y="{pad_t + plot_h + 18}" class="ht-tick" text-anchor="end">late</text>
      </svg>
      <div class="timing-hist-meta">
        <span><b>{n_early}</b> early  ·  <b>{n_late}</b> late  ·  bias: <b>{bias}</b></span>
        <span>windows: great ±{great_w:.0f}ms, ok ±{ok_w:.0f}ms  ·  bars beyond ok = timing misses</span>
      </div>
    </div>
    """


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
      <div class="feat-title"><span>technical</span><span class="feat-val">streams {f.streams.stream_count} · longest {f.streams.longest_stream} · hostile-long {f.streams.hostile_long_count}</span></div>
      <div class="feat-row"><span class="k">stream count</span><span class="v">{f.streams.stream_count}</span></div>
      <div class="feat-row"><span class="k">longest stream</span><span class="v">{f.streams.longest_stream}</span></div>
      <div class="feat-row"><span class="k">stream value (agg)</span><span class="v">{f.streams.stream_value:.1f}</span></div>
      <div class="feat-row"><span class="k">hostile-long (≥61 & parity ≥.25)</span><span class="v">{f.streams.hostile_long_count}</span></div>
      <div class="feat-row"><span class="k">top stream color</span><span class="v">{f.streams.top_stream_color:.3f}</span></div>
      <div class="feat-row"><span class="k">divisor mix</span><span class="v" style="font-size: 11px;">{div_row}</span></div>
      <div class="feat-row"><span class="k">mono-run max</span><span class="v">{c.run_length_max}</span></div>
      <div class="feat-row"><span class="k">color-change ratio</span><span class="v">{c.color_change_ratio*100:.0f}%</span></div>
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

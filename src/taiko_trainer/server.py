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
from .player import PlayerSkill
from .report import build_report
from .sessions import group_sessions
from .suggest import suggest_maps
from .workflow import _parse_bytes_as_osu, add_replay


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
        with tempfile.TemporaryDirectory() as td:
            tmp_dir = Path(td)
            replay_path = tmp_dir / (file.filename or "upload.osr")
            with open(replay_path, "wb") as fh:
                shutil.copyfileobj(file.file, fh)
            map_path = None
            if map_file is not None and (map_file.filename or "").endswith(".osu"):
                map_path = tmp_dir / (map_file.filename or "upload.osu")
                with open(map_path, "wb") as fh:
                    shutil.copyfileobj(map_file.file, fh)
            result = add_replay(workspace, str(replay_path), map_path=str(map_path) if map_path else None)
        if not result.ok:
            return HTMLResponse(_render_upload_error(result.message), status_code=400)
        target_player = result.player or ""
        if target_player:
            return RedirectResponse(url=f"/player/{target_player}", status_code=303)
        return RedirectResponse(url="/", status_code=303)

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
.fc-badge { display: inline-block; font-family: var(--font-mono); font-size: 10px; letter-spacing: 0.12em; padding: 2px 6px; border-radius: 3px; vertical-align: middle; margin-right: 6px; font-weight: 600; }
.fc-badge.fc { background: var(--great); color: white; }
.fc-badge.ss { background: linear-gradient(90deg, #d4af37, #f1d475); color: #3a2a00; }
h1 .fc-badge { font-size: 13px; padding: 3px 10px; }
"""


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

    body = f"""
  <section>
    <span class="eyebrow"><a href="/player/{player}" style="color: var(--ink-muted);">← {player}</a>  ·  replay #{row['id']}</span>
    <h1>{header_badge}{row['map_title']} <span style="color: var(--ink-muted); font-size: 20px;">[{row['map_version']}]</span></h1>
    <p class="hint">mapped by {row.get('map_creator','?')}  ·  played {row['played_at'][:19].replace('T', ' ')}</p>
  </section>

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

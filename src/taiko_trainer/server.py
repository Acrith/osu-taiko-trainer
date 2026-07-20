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

from fastapi import Cookie, Depends, FastAPI, File, Form, Header, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from . import auth as auth_module
from . import db as db_module
from .db import (
    browse_maps,
    create_api_token,
    delete_user_completely,
    discover_players,
    ensure_player_db_for_user,
    find_player_name_for_user,
    get_all_maps,
    get_map,
    get_map_content,
    get_player,
    get_replays,
    get_user_by_id,
    get_user_by_username,
    list_api_tokens,
    list_map_roots,
    open_catalog,
    open_plays,
    revoke_api_token,
    set_user_profile_public,
    top_plays_for_map,
    top_users_by_skill,
    upsert_player,
    upsert_user_from_osu,
    verify_api_token,
    workspace_status,
)
from .features import extract_features
from .judgment import Verdict, judge_replay
from .osr_parser import parse_osr_file
from .player import PlayerSkill
from .report import _compute_dim_contributors, build_report
from .scoring import rate_map
from .sessions import group_sessions
from .suggest import suggest_maps
from .workflow import _parse_bytes_as_osu, add_replay
from .db import upsert_map


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


def _is_admin_user(user) -> bool:
    """Admin identity comes from the ADMIN_OSU_USERNAME env var (comma-
    separated for multiple admins). Empty env → no admins, and every /admin
    request returns 403. Matches on `osu_username` (case-sensitive, as
    stored in the users table)."""
    import os as _os
    admins = [n.strip() for n in _os.environ.get("ADMIN_OSU_USERNAME", "").split(",") if n.strip()]
    return bool(user) and user["osu_username"] in admins


def create_app(workspace: str) -> FastAPI:
    app = FastAPI(title="taiko-trainer")

    # Force auth config to load at startup so misconfigured web mode fails
    # fast instead of silently issuing broken sessions.
    if auth_module.is_web_mode():
        auth_module.config()
        print("[auth] running in WEB mode (osu! OAuth login enabled)", flush=True)

        # Seed the osu! API client credentials into catalog_meta so
        # osu_api.is_configured() returns True. The same OAuth app credentials
        # work for both the user-login Authorization Code flow AND the
        # server-to-server Client Credentials flow (map lookup). This
        # unlocks the osu! API fallback in workflow._resolve_map so users
        # can upload replays for maps the server doesn't already have.
        import os as _os
        cid = _os.environ.get("OSU_OAUTH_CLIENT_ID", "")
        csec = _os.environ.get("OSU_OAUTH_CLIENT_SECRET", "")
        if cid and csec:
            from . import osu_api as _oa
            cat = open_catalog(workspace)
            if not _oa.is_configured(cat):
                _oa.save_credentials(cat, cid, csec)
                print("[startup] seeded osu! API client credentials from env", flush=True)
            cat.close()
    else:
        print("[auth] running in LOCAL mode (single implicit user)", flush=True)

    # --- Auth routes (web mode) ------------------------------------------
    # These are always registered so the endpoints exist regardless of mode,
    # but they only do meaningful work in web mode. In local mode /login is
    # a no-op that redirects home.

    @app.get("/login")
    def login():
        """Kick off osu! OAuth flow. Sets a signed state cookie and 302s to
        the osu! authorization endpoint. In local mode there's no login to
        perform — bounce home."""
        if not auth_module.is_web_mode():
            return RedirectResponse(url="/", status_code=302)
        state = auth_module.make_state_token()
        resp = RedirectResponse(url=auth_module.authorize_url(state), status_code=302)
        resp.set_cookie(
            key=auth_module.STATE_COOKIE_NAME,
            value=state,
            max_age=auth_module.STATE_MAX_AGE_S,
            httponly=True,
            secure=True,
            samesite="lax",
            path="/",
        )
        return resp

    @app.get("/oauth/callback")
    def oauth_callback(
        code: str | None = None,
        state: str | None = None,
        error: str | None = None,
        state_cookie: str | None = Cookie(default=None, alias=auth_module.STATE_COOKIE_NAME),
    ):
        """osu! redirects here after user consent. Verify state, exchange
        code for a user access token, fetch /me/taiko, upsert users row,
        issue session cookie, redirect to /me."""
        if not auth_module.is_web_mode():
            return RedirectResponse(url="/", status_code=302)
        if error:
            return HTMLResponse(_render_error(f"osu! login was cancelled or failed: {error}"), status_code=400)
        if not code:
            return HTMLResponse(_render_error("osu! callback missing authorization code"), status_code=400)
        # CSRF: state param must match the one we set in the cookie AND be a
        # valid signed token (unforgeable + freshness-bounded).
        if not state or state != state_cookie or not auth_module.verify_state_token(state):
            return HTMLResponse(_render_error("Login state check failed. Try again."), status_code=400)

        try:
            me = auth_module.exchange_code_for_user(code)
        except Exception as e:
            return HTMLResponse(_render_error(f"osu! login failed: {e}"), status_code=502)

        cat = open_catalog(workspace)
        user_id = upsert_user_from_osu(
            cat,
            osu_user_id=me.id,
            osu_username=me.username,
            osu_avatar_url=me.avatar_url,
            osu_cover_url=me.cover_url,
            osu_country_code=me.country_code,
            osu_global_rank=me.global_rank_taiko,
        )
        user = get_user_by_id(cat, user_id)
        cat.close()

        # First login for a new user creates their per-player DB and links
        # it back to the users row. Subsequent logins refresh display
        # fields but don't touch replay data.
        if user:
            ensure_player_db_for_user(workspace, user)

        cookie = auth_module.make_session_cookie(user_id)
        resp = RedirectResponse(url="/me", status_code=302)
        resp.set_cookie(
            key=auth_module.SESSION_COOKIE_NAME,
            value=cookie,
            max_age=auth_module.SESSION_MAX_AGE_S,
            httponly=True,
            secure=True,
            samesite="lax",
            path="/",
        )
        # Drop the one-shot state cookie now that we've consumed it.
        resp.delete_cookie(key=auth_module.STATE_COOKIE_NAME, path="/")
        return resp

    @app.post("/logout")
    def logout():
        resp = RedirectResponse(url="/", status_code=302)
        resp.delete_cookie(key=auth_module.SESSION_COOKIE_NAME, path="/")
        return resp

    @app.get("/api/auth/me")
    def auth_me(session: str | None = Cookie(default=None, alias=auth_module.SESSION_COOKIE_NAME)):
        """Tiny JSON endpoint used by the header widget to render the login
        state client-side. Cheap: session-cookie decode + one row lookup.
        Anonymous users get {logged_in: false}, no error status."""
        if not auth_module.is_web_mode():
            return JSONResponse({"mode": "local"})
        uid = auth_module.read_session_cookie(session)
        if uid is None:
            return JSONResponse({"mode": "web", "logged_in": False})
        cat = open_catalog(workspace)
        user = get_user_by_id(cat, uid)
        cat.close()
        if not user:
            return JSONResponse({"mode": "web", "logged_in": False})
        return JSONResponse({
            "mode": "web",
            "logged_in": True,
            "osu_user_id": user["osu_user_id"],
            "osu_username": user["osu_username"],
            "osu_avatar_url": user["osu_avatar_url"],
            "is_admin": _is_admin_user(user),
        })

    @app.get("/me")
    def me(session: str | None = Cookie(default=None, alias=auth_module.SESSION_COOKIE_NAME)):
        """Redirect to the logged-in user's public page. In local mode there
        is no /me — send them home. In web mode without a session cookie,
        bounce to /login."""
        if not auth_module.is_web_mode():
            return RedirectResponse(url="/", status_code=302)
        uid = auth_module.read_session_cookie(session)
        if uid is None:
            return RedirectResponse(url="/login", status_code=302)
        cat = open_catalog(workspace)
        user = get_user_by_id(cat, uid)
        cat.close()
        if not user:
            # Session references a deleted user — clear cookie + send home.
            resp = RedirectResponse(url="/", status_code=302)
            resp.delete_cookie(key=auth_module.SESSION_COOKIE_NAME, path="/")
            return resp
        return RedirectResponse(url=f"/u/{user['osu_username']}", status_code=302)

    # --- Settings: API tokens for the uploader companion -----------------

    @app.get("/settings/tokens", response_class=HTMLResponse)
    def tokens_page(
        request: Request,
        session: str | None = Cookie(default=None, alias=auth_module.SESSION_COOKIE_NAME),
    ):
        """List existing API tokens + a form to create new ones. Only the
        newly-created raw token is displayed (once, via query param); after
        page navigation it's gone forever, so users must save it before
        leaving."""
        if not auth_module.is_web_mode():
            return HTMLResponse(_render_error("Token management is only available in web mode."), status_code=404)
        uid = auth_module.read_session_cookie(session)
        if uid is None:
            return RedirectResponse(url="/login", status_code=302)
        cat = open_catalog(workspace)
        user = get_user_by_id(cat, uid)
        if not user:
            cat.close()
            return RedirectResponse(url="/", status_code=302)
        tokens = list_api_tokens(cat, uid)
        cat.close()
        # If the user just created a token, the raw value comes in via query
        # param (?created=<raw>). We show it once and never persist it.
        new_token = request.query_params.get("created", "")
        # Resolve current playstyle from the per-player DB (default 'unknown'
        # if no per-player DB exists yet).
        player_name = find_player_name_for_user(workspace, uid)
        current_style = "unknown"
        if player_name:
            pconn = open_plays(workspace, player_name)
            prow = pconn.execute(
                "SELECT style FROM player_info WHERE name = ?", (player_name,)
            ).fetchone()
            pconn.close()
            if prow and prow["style"]:
                current_style = prow["style"]
        flash_ok = request.query_params.get("ok", "")
        return HTMLResponse(_render_tokens_page(user, tokens, new_token, current_style, flash_ok))

    @app.post("/settings/tokens")
    def create_token(
        label: str = Form(...),
        session: str | None = Cookie(default=None, alias=auth_module.SESSION_COOKIE_NAME),
    ):
        if not auth_module.is_web_mode():
            raise HTTPException(status_code=404)
        uid = auth_module.read_session_cookie(session)
        if uid is None:
            return RedirectResponse(url="/login", status_code=302)
        cat = open_catalog(workspace)
        raw = create_api_token(cat, uid, label)
        cat.close()
        # Query-param the raw token forward exactly once; the tokens page
        # shows it in a copy box + reminder that it's the last time.
        from urllib.parse import quote
        return RedirectResponse(url=f"/settings/tokens?created={quote(raw)}", status_code=303)

    @app.post("/settings/tokens/{token_id}/revoke")
    def do_revoke(
        token_id: int,
        session: str | None = Cookie(default=None, alias=auth_module.SESSION_COOKIE_NAME),
    ):
        if not auth_module.is_web_mode():
            raise HTTPException(status_code=404)
        uid = auth_module.read_session_cookie(session)
        if uid is None:
            return RedirectResponse(url="/login", status_code=302)
        cat = open_catalog(workspace)
        revoke_api_token(cat, uid, token_id)
        cat.close()
        return RedirectResponse(url="/settings/tokens", status_code=303)

    @app.post("/settings/delete-account")
    def delete_account(
        confirm: str = Form(...),
        session: str | None = Cookie(default=None, alias=auth_module.SESSION_COOKIE_NAME),
    ):
        """Irreversible account delete. Requires the user to type their
        osu_username into a confirm field to prevent accidental clicks
        (and to force reading the "everything is gone" hint next to it).
        Wipes users row, revokes all tokens, deletes per-player DB.
        Clears session cookie + redirects home."""
        if not auth_module.is_web_mode():
            raise HTTPException(status_code=404)
        uid = auth_module.read_session_cookie(session)
        if uid is None:
            return RedirectResponse(url="/login", status_code=302)
        cat = open_catalog(workspace)
        user = get_user_by_id(cat, uid)
        cat.close()
        if not user:
            return RedirectResponse(url="/", status_code=302)
        if confirm.strip().lower() != (user["osu_username"] or "").strip().lower():
            return HTMLResponse(
                _render_error(
                    f"Account NOT deleted — confirmation text didn't match your username. "
                    f'<a href="/settings/tokens">Back to settings</a>.'
                ),
                status_code=400,
            )
        summary = delete_user_completely(workspace, uid)
        resp = RedirectResponse(url="/?ok=account-deleted", status_code=303)
        resp.delete_cookie(key=auth_module.SESSION_COOKIE_NAME, path="/")
        # Log summary to server console — useful for debugging accidental
        # deletions if the operator wants to audit.
        print(
            f"[delete-account] user_id={uid} deleted: "
            f"user_deleted={summary['user_deleted']} "
            f"tokens_revoked={summary['tokens_revoked']} "
            f"player_db={summary['player_db_deleted']} "
            f"replays_lost={summary['replays_lost']}",
            flush=True,
        )
        return resp

    @app.post("/settings/style")
    def set_style(
        style: str = Form(...),
        session: str | None = Cookie(default=None, alias=auth_module.SESSION_COOKIE_NAME),
    ):
        """Set the user's playstyle on their per-player DB. Style drives
        stamina + technical scoring (KDDK vs DDKK/KKDD paths) so this needs
        to trigger a refresh of the user's eff-ratings + snapshot to make
        the new numbers actually appear."""
        if not auth_module.is_web_mode():
            raise HTTPException(status_code=404)
        uid = auth_module.read_session_cookie(session)
        if uid is None:
            return RedirectResponse(url="/login", status_code=302)
        if style not in ("kddk", "ddkk", "kkdd"):
            return HTMLResponse(_render_error(f"invalid style: {style!r}"), status_code=400)

        player_name = find_player_name_for_user(workspace, uid)
        if not player_name:
            # User has no per-player DB yet (no plays uploaded). Nothing to
            # attach the style to; direct them to upload first.
            return RedirectResponse(url="/upload", status_code=303)

        conn = open_plays(workspace, player_name)
        upsert_player(conn, player_name, style=style)
        conn.close()

        # Refresh so the new style's scoring recomputes eff-ratings + snapshot.
        # Small workspaces (personal profile) — this is fast.
        from .ingest import refresh_ratings
        try:
            refresh_ratings(str(workspace))
        except Exception as e:
            print(f"[set_style] refresh failed after style change: {e}", flush=True)
            # Non-fatal; the style is set, refresh can be re-run manually.

        return RedirectResponse(url="/settings/tokens?ok=style-set", status_code=303)

    @app.post("/settings/profile-visibility")
    def set_profile_visibility(
        public: str = Form(...),
        session: str | None = Cookie(default=None, alias=auth_module.SESSION_COOKIE_NAME),
    ):
        """Flip profile_public between 0/1. Form-driven for a simple
        HTML toggle; POST-then-303 so back button behavior is sane."""
        if not auth_module.is_web_mode():
            raise HTTPException(status_code=404)
        uid = auth_module.read_session_cookie(session)
        if uid is None:
            return RedirectResponse(url="/login", status_code=302)
        cat = open_catalog(workspace)
        set_user_profile_public(cat, uid, public.lower() == "true")
        cat.close()
        return RedirectResponse(url="/settings/tokens", status_code=303)

    # --- Admin: pending-map moderation queue -----------------------------

    def _require_admin(session_cookie: str | None):
        """Common gate for /admin routes. Returns (user_row, catalog_conn)
        on success; raises HTTPException(403) otherwise. Caller must close
        the catalog conn."""
        if not auth_module.is_web_mode():
            raise HTTPException(status_code=404)
        uid = auth_module.read_session_cookie(session_cookie)
        if uid is None:
            raise HTTPException(status_code=403, detail="not logged in")
        cat = open_catalog(workspace)
        user = get_user_by_id(cat, uid)
        if not _is_admin_user(user):
            cat.close()
            raise HTTPException(status_code=403, detail="not an admin")
        return user, cat

    @app.get("/admin", response_class=HTMLResponse)
    def admin_queue(session: str | None = Cookie(default=None, alias=auth_module.SESSION_COOKIE_NAME)):
        """List every map currently in 'pending' status with approve / reject
        buttons. Admin-only."""
        from .db import list_pending_maps
        user, cat = _require_admin(session)
        pending = list_pending_maps(cat)
        cat.close()
        return HTMLResponse(_render_admin_queue(user, pending))

    @app.post("/admin/maps/{md5}/approve")
    def admin_approve_map(
        md5: str,
        session: str | None = Cookie(default=None, alias=auth_module.SESSION_COOKIE_NAME),
    ):
        """Approve a pending map — recomputes its rating and flips status to
        'approved', then rebuilds skill snapshots for every player who has a
        replay on this map (so the newly-real ratings feed leaderboards)."""
        from .db import set_map_status
        from .workflow import _parse_bytes_as_osu
        user, cat = _require_admin(session)
        row = get_map(cat, md5.lower())
        if not row:
            cat.close()
            raise HTTPException(status_code=404, detail="map not found")
        content = get_map_content(cat, md5.lower())
        if not content:
            cat.close()
            raise HTTPException(status_code=500, detail="map row has no content")
        bm = _parse_bytes_as_osu(content)
        features = extract_features(bm)
        rating = rate_map(features, od=bm.difficulty.overall_difficulty)
        upsert_map(cat, bm, features, rating, content, status='approved')
        set_map_status(cat, md5.lower(), 'approved')  # belt-and-suspenders
        cat.close()

        # Rebuild snapshots for any player who has a replay on this map,
        # so their leaderboard positions pick up the newly-real ratings.
        from .db import discover_players, player_db_path, rebuild_snapshots
        from .ingest import _row_to_perf
        from .player import compute_player_skill
        touched = 0
        for player in discover_players(workspace):
            p = player_db_path(workspace, player)
            if not p.exists():
                continue
            conn = open_plays(workspace, player)
            has = conn.execute(
                "SELECT 1 FROM replays WHERE map_md5 = ? LIMIT 1", (md5.lower(),)
            ).fetchone()
            if has:
                rebuild_snapshots(
                    conn,
                    compute_skill_fn=lambda rows: compute_player_skill(
                        [_row_to_perf(r) for r in rows]
                    ),
                )
                touched += 1
            conn.close()
        return RedirectResponse(url=f"/admin?approved={md5}&touched={touched}", status_code=303)

    @app.post("/admin/maps/{md5}/reject")
    def admin_reject_map(
        md5: str,
        session: str | None = Cookie(default=None, alias=auth_module.SESSION_COOKIE_NAME),
    ):
        """Reject a pending map — hard-deletes the map row and every replay
        that references it, across every player DB. Non-recoverable."""
        from .db import delete_map_cascade
        user, cat = _require_admin(session)
        row = get_map(cat, md5.lower())
        cat.close()
        if not row:
            raise HTTPException(status_code=404, detail="map not found")
        stats = delete_map_cascade(workspace, md5.lower())
        return RedirectResponse(
            url=f"/admin?rejected={md5}&replays={stats['replays_removed']}",
            status_code=303,
        )

    # --- Leaderboards + map database (web mode) --------------------------

    _DIMS = ("speed", "stamina", "gimmick", "technical", "consistency", "reading")
    _DIMS_WITH_TOTAL = ("total",) + _DIMS

    @app.get("/leaderboards", response_class=HTMLResponse)
    def leaderboards_overview():
        """Overall top-N panel (total-skill ranking) featured at top,
        followed by 6 per-dim column panels."""
        overall = top_users_by_skill(workspace, "total", limit=10)
        cols = {dim: top_users_by_skill(workspace, dim, limit=5) for dim in _DIMS}
        return HTMLResponse(_render_leaderboards_overview(overall, cols))

    @app.get("/leaderboards/{dim}", response_class=HTMLResponse)
    def leaderboards_dim(dim: str):
        if dim not in _DIMS_WITH_TOTAL:
            return HTMLResponse(_render_error(f"unknown dimension: {dim}"), status_code=400)
        users = top_users_by_skill(workspace, dim, limit=100)
        return HTMLResponse(_render_leaderboards_dim(dim, users))

    @app.get("/maps", response_class=HTMLResponse)
    def maps_page(
        sort: str = "rating_speed",
        min_rating: float = 0.0,
        q: str = "",
        page: int = 1,
    ):
        page = max(1, page)
        limit = 50
        offset = (page - 1) * limit
        cat = open_catalog(workspace)
        rows, total = browse_maps(
            cat, dim_sort=sort, min_rating=min_rating,
            search=q, limit=limit, offset=offset,
        )
        cat.close()
        return HTMLResponse(_render_maps_page(rows, total, sort, min_rating, q, page, limit))

    @app.get("/map/{md5}", response_class=HTMLResponse)
    def map_detail_page(md5: str):
        cat = open_catalog(workspace)
        row = get_map(cat, md5.lower())
        if not row:
            cat.close()
            return HTMLResponse(_render_error(f"map {md5!r} not in catalog"), status_code=404)
        # Lazy backfill: pull star rating from osu! API if we don't have it.
        # sqlite3.Row doesn't have .get(); convert to dict up-front.
        row = dict(row)
        if row.get("star_rating") is None:
            from .db import ensure_star_rating
            try:
                sr = ensure_star_rating(cat, md5.lower())
                if sr is not None:
                    row["star_rating"] = sr
            except Exception:
                pass
        # Reconstruct features from the stored .osu blob so the feature panel
        # renders the same as on a replay detail page.
        features = None
        content = get_map_content(cat, md5.lower())
        cat.close()
        if content:
            try:
                bm = _parse_bytes_as_osu(content)
                features = extract_features(bm)
            except Exception:
                pass
        plays = top_plays_for_map(workspace, md5.lower(), limit=30)
        return HTMLResponse(_render_map_detail(row, features, plays))

    # --- HTML pages ------------------------------------------------------

    @app.get("/", response_class=HTMLResponse)
    def home(
        request: Request,
        session: str | None = Cookie(default=None, alias=auth_module.SESSION_COOKIE_NAME),
    ):
        # WEB mode branches on auth state:
        # - logged in → straight to /me (which resolves to /u/{osu_username})
        # - anon → landing page. Local-mode workspace stats + upload drop
        #   don't belong here; that's an operator-view surface, not a
        #   user-view landing.
        if auth_module.is_web_mode():
            uid = auth_module.read_session_cookie(session)
            if uid is not None:
                cat = open_catalog(workspace)
                user = get_user_by_id(cat, uid)
                cat.close()
                if user:
                    return RedirectResponse(url=f"/u/{user['osu_username']}", status_code=302)
            # Anon: landing page
            cat = open_catalog(workspace)
            public_users = cat.execute(
                """
                SELECT osu_username, osu_avatar_url
                FROM users
                WHERE profile_public = 1
                ORDER BY last_login_at DESC LIMIT 6
                """
            ).fetchall()
            cat.close()
            return _render_web_landing([dict(r) for r in public_users])

        # LOCAL mode: workspace-operator view (unchanged)
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

    # --- Public player pages (web mode, canonical URLs) ------------------
    # /u/{osu_username} — public profile page. Resolves via catalog.users
    # → linked per-player DB. Respects the user's profile_public flag: a
    # private profile 404s to anonymous viewers but still opens for the
    # profile owner (self-view). Same view function is used for /player/{name}
    # in local mode — the only meaningful difference is how the DB is
    # located and the privacy check.

    def _resolve_public_profile(osu_username: str, viewer_uid: int | None):
        """Look up a user by their osu! username → linked per-player DB →
        privacy check. Returns (report, replays, player_name, target_user,
        is_owner). report/replays/player_name are None when there's no
        play data — the caller renders a welcome empty-state.

        Raises HTTPException(404) only for missing user or private profile
        viewed by non-owner (privacy 404s masquerade to hide existence)."""
        cat = open_catalog(workspace)
        target_user = get_user_by_username(cat, osu_username)
        cat.close()
        if not target_user:
            raise HTTPException(status_code=404, detail=f"unknown user {osu_username!r}")

        is_owner = viewer_uid is not None and viewer_uid == target_user["id"]
        if not target_user.get("profile_public", 1) and not is_owner:
            raise HTTPException(status_code=404, detail="profile not found")

        player_name = find_player_name_for_user(workspace, target_user["id"])
        if not player_name:
            # No per-player DB yet — user exists but hasn't uploaded anything.
            return None, [], None, target_user, is_owner

        conn = open_plays(workspace, player_name)
        report = build_report(conn)
        replays = get_replays(conn) if report else []
        conn.close()
        return report, replays, player_name, target_user, is_owner

    @app.get("/u/{osu_username}", response_class=HTMLResponse)
    def public_player_page(
        osu_username: str,
        request: Request,
        viewer_uid: int | None = Depends(auth_module.current_user_id),
    ):
        try:
            report, replays, player_name, target_user, is_owner = _resolve_public_profile(osu_username, viewer_uid)
        except HTTPException as e:
            return HTMLResponse(_render_error(f"{e.detail}"), status_code=e.status_code)
        if report is None:
            # No plays yet — render a welcome empty-state instead of a 404.
            return HTMLResponse(_render_empty_profile(target_user, is_owner))
        flash_ok = request.query_params.get("ok", "")
        flash_err = request.query_params.get("err", "")
        return _render_report(report, replays, player_name, flash_ok=flash_ok, flash_err=flash_err)

    @app.get("/u/{osu_username}/train/{dim}", response_class=HTMLResponse)
    def public_train_page(
        osu_username: str,
        dim: str,
        viewer_uid: int | None = Depends(auth_module.current_user_id),
    ):
        if dim not in ("speed", "stamina", "gimmick", "technical", "consistency", "reading"):
            return HTMLResponse(_render_error(f"unknown dimension: {dim}"), status_code=400)
        try:
            report, replays, player_name, target_user, is_owner = _resolve_public_profile(osu_username, viewer_uid)
        except HTTPException as e:
            return HTMLResponse(_render_error(f"{e.detail}"), status_code=e.status_code)
        if report is None:
            # No plays yet — training suggestions don't apply, send them to
            # the profile which shows the welcome empty-state.
            return RedirectResponse(url=f"/u/{osu_username}", status_code=302)
        played_md5s = {r["map_md5"] for r in replays}
        conn = open_plays(workspace, player_name)
        from .suggest import suggest_maps
        suggestions = suggest_maps(conn, report.skill, dim, top_n=25, exclude_md5s=played_md5s)
        contribs = report.dim_contributors.get(dim, ()) if hasattr(report, "dim_contributors") else ()
        conn.close()
        return _render_train_page(player_name, dim, report.skill, suggestions, contribs)

    @app.get("/player/{name}/train/{dim}", response_class=HTMLResponse)
    def train_page(name: str, dim: str):
        if dim not in ("speed", "stamina", "gimmick", "technical", "consistency", "reading"):
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
        # Mod-aware: HR/EZ change the effective windows the player saw.
        from .mods import parse_mods as _pmods
        _play_mods = _pmods(row["mods_bitfield"] or rp.meta.mods or 0)
        judged = judge_replay(bm, rp, od_mult=_play_mods.od_mult)
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
                   m.star_rating,
                   -- Show the rating the player actually cleared: mod-adjusted
                   -- effective when present, base map rating for NM. Same
                   -- COALESCE shape as get_replays() so this page's numbers
                   -- match the training-report row for the same play.
                   COALESCE(r.rating_speed_eff,       m.rating_speed)       AS rating_speed,
                   COALESCE(r.rating_stamina_eff,     m.rating_stamina)     AS rating_stamina,
                   COALESCE(r.rating_gimmick_eff,     m.rating_gimmick)     AS rating_gimmick,
                   COALESCE(r.rating_technical_eff,   m.rating_technical)   AS rating_technical,
                   COALESCE(r.rating_consistency_eff, m.rating_consistency) AS rating_consistency,
                   COALESCE(r.rating_reading_eff,     m.rating_reading, 0)  AS rating_reading
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
                    # Mod-aware: features panel should show what the player
                    # actually experienced (DT-scaled BPM, HR-scaled SV,
                    # etc.), and the timing histogram's window bands should
                    # reflect the effective OD (HR / EZ).
                    from .mods import parse_mods as _pmods, apply_mods_to_beatmap as _amod
                    _play_mods = _pmods(row["mods_bitfield"] or 0)
                    play_bm = _amod(bm, _play_mods)
                    features = extract_features(play_bm)
                    # Timing histogram is cheap (~10-50ms per replay).
                    with tempfile.NamedTemporaryFile(suffix=".osr", delete=False) as tmp:
                        tmp.write(bytes(row["content"]))
                        tmp_path = tmp.name
                    try:
                        rp = parse_osr_file(tmp_path)
                    finally:
                        Path(tmp_path).unlink(missing_ok=True)
                    # Judge against the ORIGINAL bm (music-time — see
                    # judge_replay's docstring), but pass od_mult so windows
                    # match what the player actually saw.
                    judged = judge_replay(bm, rp, od_mult=_play_mods.od_mult)
                except Exception as e:
                    # Silent-swallow used to hide the sqlite3.Row.get()
                    # AttributeError, which made the features panel + timing
                    # histogram silently disappear on every replay page.
                    # Log so future silent failures show up in `docker logs`.
                    import traceback
                    print(f"[replay {replay_id}] feature/judge extraction failed: {e!r}",
                          flush=True)
                    traceback.print_exc()
                    features = None
                    judged = None
            # Lazy backfill: fetch star rating from osu! API if we don't
            # have it cached yet. One API call per map, cached forever.
            # sqlite3.Row supports [key] but not .get(); convert to dict
            # so both this check + downstream renderers work uniformly.
            row = dict(row)
            if row.get("star_rating") is None:
                from .db import ensure_star_rating
                try:
                    sr = ensure_star_rating(conn, row["map_md5_ref"])
                    if sr is not None:
                        row["star_rating"] = sr
                except Exception:
                    pass
        conn.close()
        if not row:
            return HTMLResponse(_render_error(f"Replay {replay_id} for {player} not found."), status_code=404)
        return _render_replay(dict(row), player, features, judged)

    # --- upload ---------------------------------------------------------

    @app.get("/api/uploads/active")
    def uploads_active(
        session: str | None = Cookie(default=None, alias=auth_module.SESSION_COOKIE_NAME),
    ):
        """List uploads that are still in progress or recently completed.
        The base template polls this to render the floating status tray.

        In web mode: filter to the session user's own uploads only. Anon
        users get an empty list. Local mode: return everything (single
        implicit user, no isolation needed)."""
        # Determine which uploads this viewer is allowed to see.
        viewer_uid: int | None = None
        if auth_module.is_web_mode():
            viewer_uid = auth_module.read_session_cookie(session)
            # Anonymous viewer in web mode: no uploads to show.
            if viewer_uid is None:
                return JSONResponse([])

        cutoff = time.time() - 15  # keep done/error entries visible 15s
        summary = []
        with _UPLOAD_LOCK:
            for tid, entry in list(_UPLOAD_TASKS.items()):
                # Web mode: skip entries owned by other users.
                if auth_module.is_web_mode() and entry.get("owner_uid") != viewer_uid:
                    continue
                if entry["stage"] not in ("done", "error"):
                    summary.append({"id": tid, **entry})
                elif entry.get("updated_at", 0) >= cutoff:
                    summary.append({"id": tid, **entry})
            # Cleanup: drop entries older than 5 minutes.
            gc_cutoff = time.time() - 300
            for tid in [t for t, e in _UPLOAD_TASKS.items() if e.get("updated_at", 0) < gc_cutoff]:
                _UPLOAD_TASKS.pop(tid, None)
        return JSONResponse(summary)

    # --- Uploader companion API (bearer token auth) ----------------------

    @app.post("/api/v1/maps")
    async def api_upload_map(
        file: UploadFile = File(...),
        authorization: str | None = Header(default=None),
    ):
        """Add a map .osu blob to the server's catalog. Used by the local →
        hosted migration script so replays can reference maps that aren't
        yet in the shared catalog.

        Idempotent — if the map's md5 is already known, no-op. Bearer token
        auth (same tokens as /api/v1/replays; any authenticated user can
        contribute maps to the shared catalog).

        Responses:
          201 Created         first time this map is stored
          200 OK              md5 already in catalog, no-op
          400 Bad Request     unparseable .osu, non-taiko, or missing file
          401 Unauthorized    missing/invalid/revoked token
        """
        raw_token = auth_module.parse_bearer_header(authorization)
        if not raw_token:
            raise HTTPException(status_code=401, detail="missing or malformed Authorization header")
        cat = open_catalog(workspace)
        user_id = verify_api_token(cat, raw_token)
        if user_id is None:
            cat.close()
            raise HTTPException(status_code=401, detail="token unknown or revoked")

        try:
            content = await file.read()
        except Exception as e:
            cat.close()
            raise HTTPException(status_code=400, detail=f"could not read map file: {e}")
        if not content:
            cat.close()
            raise HTTPException(status_code=400, detail="empty map file")

        import hashlib as _h
        md5 = _h.md5(content).hexdigest()
        existing = get_map(cat, md5)
        if existing:
            cat.close()
            return JSONResponse(
                {"ok": True, "md5": md5, "created": False, "title": existing["title"]},
                status_code=200,
            )

        try:
            bm = _parse_bytes_as_osu(content)
        except Exception as e:
            cat.close()
            raise HTTPException(status_code=400, detail=f"could not parse .osu: {e}")

        features = extract_features(bm)
        # Central ingest gate — approve normally, queue marathons, refuse the rest.
        from .workflow import _moderation_verdict_for_map
        from .scoring import DimensionRating as _DR
        verdict, reason = _moderation_verdict_for_map(cat, md5, bm, features)
        if verdict == 'rejected':
            cat.close()
            raise HTTPException(status_code=400, detail=f"map rejected: {reason}")

        if verdict == 'pending':
            pending_rating = _DR(speed=0, stamina=0, gimmick=0, technical=0,
                                 consistency=0, reading=0)
            upsert_map(cat, bm, features, pending_rating, content, status='pending')
            cat.close()
            return JSONResponse(
                {"ok": True, "md5": md5, "created": True, "status": "pending",
                 "message": f"map queued for admin review: {reason}",
                 "title": bm.meta.title},
                status_code=202,
            )

        rating = rate_map(features, od=bm.difficulty.overall_difficulty)
        upsert_map(cat, bm, features, rating, content)
        cat.close()
        return JSONResponse(
            {"ok": True, "md5": md5, "created": True, "status": "approved",
             "title": bm.meta.title},
            status_code=201,
        )

    @app.post("/api/v1/replays")
    async def api_upload_replay(
        file: UploadFile = File(...),
        authorization: str | None = Header(default=None),
    ):
        """Uploader companion endpoint. Bearer token auth (not session cookies —
        this path is invoked by the local watchdog agent, not the browser).

        Same identity guard as the browser upload: the .osr's player field
        must match the token owner's osu_username. Runs the exact same
        add_replay pipeline as browser + local upload, so nothing about
        judgment/rating/features differs between the three code paths.

        Response shapes:
          200 OK             {"replay_id": N, "player": "...", "accuracy": 0.99, ...}
          201 Created        (same, on first-time upload)
          204 No Content     duplicate (same map_md5+played_at already exists)
          401 Unauthorized   missing/invalid/revoked token
          403 Forbidden      .osr player mismatch
          400 Bad Request    unparseable .osr or add_replay failure
        """
        raw_token = auth_module.parse_bearer_header(authorization)
        if not raw_token:
            raise HTTPException(status_code=401, detail="missing or malformed Authorization header")

        cat = open_catalog(workspace)
        user_id = verify_api_token(cat, raw_token)
        if user_id is None:
            cat.close()
            raise HTTPException(status_code=401, detail="token unknown or revoked")
        user = get_user_by_id(cat, user_id)
        cat.close()
        if not user:
            raise HTTPException(status_code=401, detail="token owner no longer exists")

        # Spool to temp so add_replay (which takes filesystem paths) can read it.
        td = Path(tempfile.mkdtemp(prefix="tt-api-upload-"))
        replay_path = td / (file.filename or "upload.osr")
        try:
            with open(replay_path, "wb") as fh:
                shutil.copyfileobj(file.file, fh)

            # Identity check before we do any real work.
            try:
                probe = parse_osr_file(str(replay_path))
            except Exception as e:
                raise HTTPException(status_code=400, detail=f"could not parse replay: {e}")

            expected = (user["osu_username"] or "").lower()
            actual = (probe.meta.player or "").lower()
            if not expected or expected != actual:
                raise HTTPException(
                    status_code=403,
                    detail=(
                        f"replay was played by {probe.meta.player!r}, "
                        f"but the token belongs to {user['osu_username']!r}"
                    ),
                )

            # Same pipeline as the browser upload — synchronous here because
            # the uploader wants an immediate result to know whether to
            # advance its cursor or retry.
            result = add_replay(workspace, str(replay_path))
            if not result.ok:
                # add_replay's most common failure is "map not in catalog and
                # not on disk". For the uploader that's a real error — client
                # should surface it to the user, not silently skip.
                raise HTTPException(status_code=400, detail=result.message.splitlines()[0])

            # Look up the just-inserted replay id + a summary so the client
            # can log something meaningful.
            plays = open_plays(workspace, result.player) if result.player else None
            summary = None
            if plays is not None:
                row = plays.execute(
                    """
                    SELECT r.id, r.accuracy_judged, r.count_great, r.count_ok, r.count_miss,
                           r.mods_label, m.title, m.version
                    FROM replays r JOIN catalog.maps m ON m.md5 = r.map_md5
                    ORDER BY r.id DESC LIMIT 1
                    """
                ).fetchone()
                plays.close()
                if row:
                    summary = {
                        "replay_id": int(row["id"]),
                        "map_title": row["title"],
                        "map_version": row["version"],
                        "mods": row["mods_label"],
                        "accuracy": row["accuracy_judged"],
                        "great": row["count_great"],
                        "ok": row["count_ok"],
                        "miss": row["count_miss"],
                    }
            return JSONResponse(summary or {"ok": True, "player": result.player}, status_code=201)
        finally:
            shutil.rmtree(td, ignore_errors=True)

    @app.get("/upload", response_class=HTMLResponse)
    def upload_page(
        session: str | None = Cookie(default=None, alias=auth_module.SESSION_COOKIE_NAME),
    ):
        """Web-based upload page: drag-drop zone for occasional .osr uploads +
        instructions for the standalone uploader companion. Requires login in
        web mode so the identity gate on POST /upload has a session to check
        against; local mode routes everyone through the single implicit user."""
        if auth_module.is_web_mode():
            uid = auth_module.read_session_cookie(session)
            if uid is None:
                return RedirectResponse(url="/login", status_code=302)
            cat = open_catalog(workspace)
            user = get_user_by_id(cat, uid)
            cat.close()
            username = user["osu_username"] if user else "?"
        else:
            username = None
        return HTMLResponse(_render_upload_page(username))

    @app.post("/upload")
    async def upload(
        request: Request,
        file: UploadFile = File(...),
        map_file: UploadFile | None = File(None),
        session: str | None = Cookie(default=None, alias=auth_module.SESSION_COOKIE_NAME),
    ):
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

        # WEB mode identity gate: uploads require login, and the .osr's
        # player field must match the logged-in user's osu_username.
        # Without this, the competitive/comparison surfaces are meaningless
        # (anyone could upload anyone's replays). LOCAL mode skips both.
        if auth_module.is_web_mode():
            viewer_uid = auth_module.read_session_cookie(session)
            if viewer_uid is None:
                shutil.rmtree(td, ignore_errors=True)
                return HTMLResponse(
                    _render_upload_error("Log in with osu! before uploading."),
                    status_code=401,
                )
            cat = open_catalog(workspace)
            user = get_user_by_id(cat, viewer_uid)
            cat.close()
            if not user:
                shutil.rmtree(td, ignore_errors=True)
                return HTMLResponse(_render_upload_error("Your session references an unknown user. Log in again."), status_code=401)
            try:
                probe = parse_osr_file(str(replay_path))
            except Exception as e:
                shutil.rmtree(td, ignore_errors=True)
                return HTMLResponse(_render_upload_error(f"Could not parse the replay file: {e}"), status_code=400)
            expected = (user["osu_username"] or "").lower()
            actual = (probe.meta.player or "").lower()
            if not expected or expected != actual:
                shutil.rmtree(td, ignore_errors=True)
                return HTMLResponse(
                    _render_upload_error(
                        f"This replay was played by <b>{probe.meta.player!r}</b>, but you are logged in as "
                        f"<b>{user['osu_username']!r}</b>. Uploads must be your own plays."
                    ),
                    status_code=403,
                )

        task_id = uuid.uuid4().hex[:10]
        # In web mode, tag the task with its owner so /api/uploads/active
        # can filter — otherwise the floating tray shows every user's
        # in-flight uploads to everyone on the site. Local mode leaves
        # this None (single implicit user, no isolation needed).
        owner_uid: int | None = None
        if auth_module.is_web_mode():
            owner_uid = auth_module.read_session_cookie(session)
        with _UPLOAD_LOCK:
            _UPLOAD_TASKS[task_id] = {
                "stage": "queued", "label": "Queued", "pct": 0, "note": "",
                "filename": replay_path.name,
                "created_at": time.time(), "updated_at": time.time(),
                "result": None, "error": None,
                "redirect": None,
                "owner_uid": owner_uid,
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
        # In WEB mode, identity is fixed at OAuth login — unlinking makes
        # no sense (you'd break your own report while staying authenticated).
        # Reject the endpoint entirely rather than surprise a curl user.
        if auth_module.is_web_mode():
            raise HTTPException(status_code=404, detail="unlink is not available in web mode")
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
  display: flex; align-items: center; gap: 24px;
  padding-bottom: 20px; border-bottom: 1px solid var(--rule);
}
header.site .logo {
  font-family: var(--font-mono); font-weight: 500; font-size: 22px;
  letter-spacing: -0.01em; color: var(--ink); text-decoration: none;
}
header.site .logo:hover { color: var(--accent); text-decoration: none; }
header.site nav {
  flex: 1; display: flex; align-items: center; gap: 22px;
}
header.site nav a {
  font-family: var(--font-mono); font-size: 11px; letter-spacing: 0.14em;
  text-transform: uppercase; color: var(--ink-muted);
}
header.site nav a:hover { color: var(--ink); text-decoration: none; }
header.site nav a.active { color: var(--accent); }
/* Upload nav item styled as a call-to-action button rather than plain link */
header.site nav a.nav-cta {
  padding: 6px 14px; border: 1px solid var(--accent); border-radius: 3px;
  color: var(--accent); letter-spacing: 0.16em; font-weight: 500;
}
header.site nav a.nav-cta:hover { background: var(--accent); color: white; }
header.site nav a.nav-cta.active { background: var(--accent); color: white; }
/* Native browser tooltip on labels that have title="…". Subtle marker so users
   know something's there without being noisy. */
[title] { cursor: help; }
.feat-row .k[title] { border-bottom: 1px dotted var(--rule); }
.hint span[title] { border-bottom: 1px dotted var(--rule); cursor: help; }

/* Auth widget in the header (web mode only). JS populates it from
   /api/auth/me; local mode leaves the div empty and it takes no space. */
.auth-widget { display: flex; align-items: center; gap: 10px; font-family: var(--font-mono); font-size: 11px; letter-spacing: 0.08em; }
.auth-widget:empty { display: none; }
.auth-widget .avatar { width: 24px; height: 24px; border-radius: 50%; object-fit: cover; border: 1px solid var(--rule); }
.auth-widget .user { color: var(--ink); text-transform: none; letter-spacing: 0; }
.auth-widget .login-btn { padding: 6px 14px; border: 1px solid var(--accent); color: var(--accent); border-radius: 3px; text-transform: uppercase; letter-spacing: 0.14em; }
.auth-widget .login-btn:hover { background: var(--accent); color: white; text-decoration: none; }
.auth-widget form { margin: 0; padding: 0; display: inline; }
.auth-widget .logout-btn { background: none; border: 1px solid var(--rule); color: var(--ink-muted); padding: 4px 10px; border-radius: 3px; font-family: inherit; font-size: 10px; letter-spacing: 0.14em; text-transform: uppercase; cursor: pointer; }
.auth-widget .logout-btn:hover { border-color: var(--miss); color: var(--miss); }
.auth-widget .settings-link { font-size: 16px; color: var(--ink-muted); text-decoration: none; margin: 0 2px; }
.auth-widget .settings-link:hover { color: var(--ink); text-decoration: none; }
.auth-widget .admin-link { padding: 2px 8px; border: 1px solid var(--accent); color: var(--accent); border-radius: 3px; font-family: var(--font-mono); font-size: 10px; letter-spacing: 0.14em; text-decoration: none; }
.auth-widget .admin-link:hover { background: var(--accent); color: white; text-decoration: none; }
.eyebrow { font-family: var(--font-mono); font-size: 11px; letter-spacing: 0.14em; text-transform: uppercase; color: var(--ink-muted); }
h1 { font-family: var(--font-mono); font-weight: 500; font-size: 32px; letter-spacing: -0.015em; margin: 4px 0 0 0; }
h2 { font-family: var(--font-mono); font-weight: 500; font-size: 20px; margin: 0 0 8px 0; }
.card { background: var(--panel); border: 1px solid var(--rule); border-radius: 4px; padding: 20px 24px; }
.grid { display: grid; gap: 16px; }
.grid-2 { grid-template-columns: repeat(2, 1fr); }
.grid-3 { grid-template-columns: repeat(3, 1fr); }
.stats-row { display: grid; grid-template-columns: repeat(auto-fit, minmax(140px, 1fr)); gap: 1px; background: var(--rule); border: 1px solid var(--rule); border-radius: 3px; overflow: hidden; }
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
.hero-skill-mini { grid-template-columns: repeat(6, 1fr) !important; }
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
.hero-experimental {
  display: inline-block; font-family: var(--font-mono); font-size: 10px;
  letter-spacing: 0.08em; text-transform: uppercase; padding: 3px 8px;
  border: 1px dashed rgba(232, 164, 58, 0.6); background: rgba(232, 164, 58, 0.08);
  border-radius: 3px; color: #e8a43a; cursor: help; margin-left: 6px;
}
.hero-experimental:hover { background: rgba(232, 164, 58, 0.15); }
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
.star-pill {
  display: inline-block;
  padding: 5px 12px;
  font-family: var(--font-mono);
  font-size: 12px;
  letter-spacing: 0.04em;
  background: rgba(255, 193, 71, 0.22);
  color: #ffd47a;
  border-radius: 999px;
  border: 1px solid rgba(255, 193, 71, 0.55);
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
.prog-grid { display: grid; grid-template-columns: repeat(6, 1fr); gap: 10px; margin-top: 4px; }
@media (max-width: 1100px) { .prog-grid { grid-template-columns: repeat(3, 1fr); } }
@media (max-width: 700px)  { .prog-grid { grid-template-columns: repeat(2, 1fr); } }
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

/* --- mods chip: DT, HDDT, HR, etc. Amber/gold to signal 'harder than base'. --- */
.mods-chip { display: inline-block; font-family: var(--font-mono); font-size: 10px; letter-spacing: 0.14em; padding: 2px 7px; border-radius: 3px; vertical-align: middle; margin-left: 6px; font-weight: 700; background: linear-gradient(90deg, #d4a02c, #f0c665); color: #2a1a00; text-shadow: 0 1px 0 rgba(255,255,255,0.3); }
.mods-chip-lg { font-size: 12px; padding: 4px 11px; letter-spacing: 0.16em; }

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

import re as _re

_COLOR_TOKEN_RE = _re.compile(r"(?<![A-Za-z])[KkDd·]{3,}(?![A-Za-z])")


def _colorize_signature(sig: str) -> str:
    """The new diagnostic signatures may lead with English labels ('tempo
    shift', 'chunk parity', 'divisor break', 'no signal'). Only colorize
    K/D letters that appear as a standalone taiko-color TOKEN — a run of 3+
    K/D/k/d/· characters not adjacent to letters. Everything else stays as
    plain text so we don't rainbow the whole line."""
    if not sig:
        return ""

    def _repl(match: _re.Match) -> str:
        buf = []
        for ch in match.group(0):
            if ch == "K": buf.append('<span class="wp-K">K</span>')
            elif ch == "D": buf.append('<span class="wp-D">D</span>')
            elif ch == "k": buf.append('<span class="wp-K wp-miss">k</span>')
            elif ch == "d": buf.append('<span class="wp-D wp-miss">d</span>')
            else: buf.append(ch)
        return "".join(buf)

    return _COLOR_TOKEN_RE.sub(_repl, sig)


def _render_weakness_patterns(report, player: str) -> str:
    """Cluster the misses across the player's BEST play of each map by pattern
    signature. Surfaces genuine weaknesses evidenced by consistent misses across
    stable records, not one-off bad plays.

    Hidden entirely until at least one diagnostic signature bit the player
    across ≥ 3 different maps with ≥ 5 misses. Below that threshold, any
    'cluster' is noise — a random miss is nothing to improve. Feature comes
    back automatically once the corpus grows enough for signal."""
    clusters = getattr(report, "weakness_clusters", ()) or ()
    if not clusters:
        return ""
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
    osu! profile. Shows the linked profile (with avatar) OR a form to link.

    In WEB mode this whole section is redundant — the player IS an osu!
    user (identity is fixed at OAuth login) and profile info is already
    visible in the hero card. Returning empty hides the section entirely."""
    if auth_module.is_web_mode():
        return ""
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
    button when already linked. In WEB mode neither button is shown —
    identity is fixed to the OAuth login, "unlink" makes no sense (you
    ARE your osu! account), and "link" is redundant."""
    if auth_module.is_web_mode():
        return ""
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
          {'<span class="hero-experimental" title="The DDKK/KKDD scoring model is a first-pass approximation. It correctly amplifies stamina for mono-color runs (single-hand grinding) and drops KDDK-specific parity friction from technical, but the specific weights and anchors were calibrated for KDDK play data. As real DDKK play history accumulates, the numbers will be refined. Expect some rating drift as the model matures.">DDKK model: experimental ⓘ</span>' if report.style in ("ddkk", "kkdd") else ""}
        </div>
        <h1 class="hero-title">{player}</h1>
        <p class="hero-artist">{_render_osu_subtitle(report)}</p>
        <p class="hero-meta">{report.replays} replays  ·  {sess_count} sessions  ·  {unique_maps} unique maps{("  ·  latest " + latest_date) if latest_date else ""}</p>
        <div class="hero-actions">
          {'<a class="hero-btn" href="/">← Home</a>' if not auth_module.is_web_mode() else ''}
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
          <div><span class="k">read</span><span class="v">{d['reading']:.0f}</span></div>
        </div>
      </div>
    </div>
  </section>"""


def _render_skill_radar(report, player: str) -> str:
    """SVG radar chart of the 5-D skill vector. Values normalized to the map's
    max dimension so shape (relative disparity) reads clearly. Each vertex is
    a click-through to /player/{name}/train/{dim}."""
    import math as _math

    dims = ("speed", "stamina", "gimmick", "technical", "consistency", "reading")
    d = report.skill.as_dict()
    max_val = max(d.values()) or 1

    # Extra width on the sides so the "CONSISTENCY" label (11 chars) doesn't
    # clip the leftmost boundary.
    W, H = 620, 400
    cx, cy = W / 2, H / 2 - 6
    r_max = 130

    # 6 axes at 60° intervals, SPEED at top.
    angles = {dim: -_math.pi/2 + i * (2*_math.pi/6) for i, dim in enumerate(dims)}

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
    # Base values live in the row; mods_bitfield lets us show what the PLAYER
    # actually experienced (DT/HT scale BPM+length, HR/EZ scale OD).
    from .mods import parse_mods as _parse_mods
    _mods = _parse_mods(row.get("mods_bitfield") or 0)
    dur_s = int((row.get("duration_s") or 0) / _mods.speed_mult)   # DT ÷1.5, HT ÷0.75
    duration_str = f"{dur_s // 60}:{dur_s % 60:02d}"
    bpm_min = (row.get("bpm_min") or 0) * _mods.speed_mult
    bpm_max = (row.get("bpm_max") or 0) * _mods.speed_mult
    bpm_str = f"{bpm_min:.0f}" if abs(bpm_min - bpm_max) < 0.5 else f"{bpm_min:.0f}–{bpm_max:.0f}"
    hittable = row.get("hittable_notes") or 0
    od = min((row.get("od") or 0) * _mods.od_mult, 10.0)            # HR ×1.4 cap 10, EZ ×0.5

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
          {('<span class="star-pill">★ ' + f"{row['star_rating']:.2f}" + '</span>') if row.get('star_rating') else ''}
          {_mods_chip(row.get('mods_label'), size='lg')}
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


def _mods_chip(label: str | None, *, size: str = "sm") -> str:
    """Small mods chip appended after the map title / hero-diff pill.
    Empty for NM plays so the common case doesn't get visual clutter."""
    if not label or label == "NM":
        return ""
    css = "mods-chip" if size == "sm" else "mods-chip mods-chip-lg"
    return f' <span class="{css}">{label}</span>'


def _render_admin_queue(user, pending: list[dict]) -> str:
    """Admin approval queue — one row per pending map with an approve/reject
    form. Shown only to users listed in ADMIN_OSU_USERNAME."""
    if not pending:
        body_rows = "<tr><td colspan='7' class='hint' style='text-align:center; padding:24px;'>Queue is empty — no maps awaiting review.</td></tr>"
    else:
        parts = []
        for m in pending:
            dur = int(m["duration_s"] or 0)
            dur_s = f"{dur // 60}:{dur % 60:02d}"
            bpm = f"{int(m['bpm_min'] or 0)}–{int(m['bpm_max'] or 0)}"
            title = f"{m['artist']} — {m['title']} [{m['version']}]"
            osu_link = f"<a href='https://osu.ppy.sh/beatmapsets/{m['beatmapset_id']}' target='_blank' rel='noopener'>osu!</a>" if m["beatmapset_id"] else "—"
            parts.append(f"""
            <tr>
              <td><a href="/map/{m['md5']}"><b>{title}</b></a><br><span class="muted">by {m['creator']}</span></td>
              <td>{dur_s}</td>
              <td>{int(m['hittable_notes'] or 0):,}</td>
              <td>{bpm}</td>
              <td>{m['od']:.1f}</td>
              <td>{osu_link}</td>
              <td class="actions">
                <form method="post" action="/admin/maps/{m['md5']}/approve" style="display:inline">
                  <button class="approve" type="submit">Approve</button>
                </form>
                <form method="post" action="/admin/maps/{m['md5']}/reject" style="display:inline"
                      onsubmit="return confirm('Reject and DELETE this map + all replays that reference it?');">
                  <button class="reject" type="submit">Reject</button>
                </form>
              </td>
            </tr>""")
        body_rows = "".join(parts)

    body = f"""
  <section>
    <span class="eyebrow">admin</span>
    <h1>Approval queue</h1>
    <p class="hint">Maps longer than 10 min land here first. Approving recomputes ratings and pushes them onto leaderboards; rejecting hard-deletes the map + all replays for it.</p>
  </section>
  <section>
    <table class="admin-queue">
      <thead><tr><th>map</th><th>length</th><th>notes</th><th>BPM</th><th>OD</th><th>osu!</th><th></th></tr></thead>
      <tbody>{body_rows}</tbody>
    </table>
  </section>
  <style>
    .admin-queue {{ width: 100%; border-collapse: collapse; font-size: 13px; }}
    .admin-queue th, .admin-queue td {{ padding: 10px 12px; border-bottom: 1px solid var(--rule); text-align: left; }}
    .admin-queue th {{ font-family: var(--font-mono); font-size: 10px; letter-spacing: 0.12em; text-transform: uppercase; color: var(--ink-muted); }}
    .admin-queue td.actions {{ white-space: nowrap; text-align: right; }}
    .admin-queue button {{ padding: 6px 12px; border-radius: 3px; border: 1px solid; font-family: inherit; font-size: 11px; letter-spacing: 0.12em; text-transform: uppercase; cursor: pointer; margin-left: 6px; }}
    .admin-queue button.approve {{ background: var(--great); color: white; border-color: var(--great); }}
    .admin-queue button.reject  {{ background: transparent; color: var(--miss); border-color: var(--miss); }}
    .admin-queue button.approve:hover {{ opacity: 0.85; }}
    .admin-queue button.reject:hover  {{ background: var(--miss); color: white; }}
    .admin-queue .muted {{ color: var(--ink-muted); font-size: 11px; }}
  </style>
"""
    return _html_page(f"Admin queue ({len(pending)})", body, active="admin")


_CAUSE_COLORS = {
    "wrong_color": "#7A4E9E",
    "pattern_parity": "#3E8EBB",
    "speed": "var(--miss)",
    "stamina": "var(--ok)",
    "technical": "var(--accent-cool)",
    "gimmick": "#B060B0",
    "consistency": "var(--great)",
    "reading": "#e8a43a",       # amber — matches the "reading pressure" mods chip
    "unknown": "var(--ink-faint)",
}


def _html_page(title: str, body: str, active: str = "") -> str:
    # Nav items are contextual per mode. Local mode keeps the operator "Home"
    # link so an admin browsing the workspace can get back. Web mode has no
    # "Home" — the logo itself links to `/` (which redirects to /me for
    # authed users, landing for anon).
    if auth_module.is_web_mode():
        nav_items = [
            ("Leaderboards", "/leaderboards", "leaderboards"),
            ("Maps",         "/maps",         "maps"),
            ("Upload",       "/upload",       "upload"),
        ]
    else:
        nav_items = [("Home", "/", "home")]
    def _nav_link(label: str, href: str, key: str) -> str:
        classes = []
        if key == active:
            classes.append("active")
        # The Upload item is a call-to-action — style it as a button rather
        # than a plain nav link so it stands out and reads as "do a thing".
        if key == "upload":
            classes.append("nav-cta")
        cls = f' class="{" ".join(classes)}"' if classes else ""
        return f'<a href="{href}"{cls}>{label}</a>'

    nav_html = " ".join(
        _nav_link(label, href, key) for label, href, key in nav_items
    )
    return f"""<!doctype html>
<html><head>
<meta charset="utf-8">
<title>{title} — taiko-trainer</title>
<style>{_BASE_CSS}</style>
</head><body>
<main>
  <header class="site">
    <a href="/" class="logo">taiko-trainer</a>
    <nav>{nav_html}</nav>
    <div id="auth-widget" class="auth-widget"></div>
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

// Auth widget — populates the header slot from /api/auth/me. Empty in
// local mode. Anonymous in web mode gets a "Log in" button. Authenticated
// gets avatar + username + logout button.
(function() {{
  const slot = document.getElementById('auth-widget');
  if (!slot) return;
  fetch('/api/auth/me').then(r => r.json()).then(a => {{
    if (a.mode !== 'web') return;   // local mode: leave empty
    if (!a.logged_in) {{
      slot.innerHTML = '<a class="login-btn" href="/login">Log in with osu!</a>';
      return;
    }}
    const name = a.osu_username;
    const av = a.osu_avatar_url;
    const admin = a.is_admin ? '<a class="admin-link" href="/admin" title="Admin queue">ADMIN</a>' : '';
    slot.innerHTML =
      (av ? '<img class="avatar" src="' + av + '" alt="">' : '') +
      '<a class="user" href="/u/' + encodeURIComponent(name) + '">' + name + '</a>' +
      admin +
      '<a class="settings-link" href="/settings/tokens" title="Settings">⚙</a>' +
      '<form method="post" action="/logout"><button type="submit" class="logout-btn">Logout</button></form>';
  }}).catch(() => {{}});
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


_PROGRESSION_DIMS = ("speed", "stamina", "gimmick", "technical", "consistency", "reading")

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
        # Coerce None -> 0 for skill_reading on pre-migration snapshots.
        vals = [(s[f"skill_{dim}"] or 0.0) for s in history]
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
    "gimmick":     "chaotic SV — unpredictable scroll manipulations to read",
    "technical":   "pattern awareness — mono runs, mixed divisors, parity",
    "consistency": "unwavering timing — no random drops from bursts / parity flips",
    "reading":     "base scroll velocity — fast reaction time on high-SV / high-BPM",
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
        c_reading     = (r.get("rating_reading") or 0) * acc_scale
        # data-* used by client-side sort + filter
        raw_played = (r.get("played_at") or "").replace("T", " ")
        title_lc = title.lower() + " " + version.lower()
        rows += (
            f'<tr class="row-nav replay-row" data-href="/replay/{player}/{r["id"]}" '
            f'data-idx="{idx}" data-title="{title_lc}" data-date="{raw_played}" '
            f'data-c-speed="{c_speed:.1f}" data-c-stamina="{c_stamina:.1f}" '
            f'data-c-gimmick="{c_gimmick:.1f}" data-c-technical="{c_technical:.1f}" '
            f'data-c-consistency="{c_consistency:.1f}" data-c-reading="{c_reading:.1f}" '
            f'style="cursor:pointer">'
            f'<td class="name">{badge}{title} <span style="color: var(--ink-muted); font-size: 11px;">[{version}]</span>{_mods_chip(r.get("mods_label"))}</td>'
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
        <button class="tab" data-sort="c-reading">Top reading</button>
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
      <div class="stat"><span class="k">reading</span><span class="v">{(row.get('rating_reading') or 0):.0f}</span></div>
    </div>
  </section>

  {features_section}

  {causes_section}

  <section class="card">
    <h2>Timing</h2>
    <p class="hint">
      <span title="Average signed hit-delta across all judged notes. Negative = tended to hit early, positive = tended to hit late. Close to 0 = well-calibrated to the map.">avg delta {row['delta_mean_ms']:+.1f} ms ⓘ</span>
      &nbsp;·&nbsp;
      <span title="Standard deviation of hit deltas — how spread out your timing was. Lower is more consistent. Under 15 ms is very tight; over 25 ms means notable drift.">spread ±{row['delta_stddev_ms']:.1f} ms ⓘ</span>
      &nbsp;·&nbsp;
      <span title="Fraction of note pairs where BOTH keys of a two-color pattern were pressed within ~8ms — 'cheese' in taiko refers to double-tapping when you're supposed to alternate. High rate = you're smashing both keys instead of playing cleanly.">cheese rate {row['cheese_rate']*100:.2f}% ⓘ</span>
      &nbsp;·&nbsp;
      <span title="Absolute count of the fast-cheese pairs (same as cheese-rate, but the raw number). Useful to see if 0.5% comes from 5 pairs on a short map or 50 pairs on a long one.">cheese pairs {row.get('fast_cheese_pairs', 0)} ⓘ</span>
    </p>
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
    # Flag maps where the mapper's declared BPM diverges strongly from what the
    # note stream actually plays at — gimmick maps and half-BPM tricks. The
    # rating uses the trusted value, not the declared one.
    bpm_effective = getattr(m, "bpm_effective", 0.0) or 0.0
    bpm_diverges = bpm_effective > 0 and m.bpm_max > bpm_effective * 1.3
    bpm_effective_row = (
        f'<div class="feat-row"><span class="k" title="BPM derived from the actual note stream, not the .osu timing points. The rating is anchored to this value when it diverges strongly from the declared BPM (mapper tricks / storyboard sync).">effective BPM ⓘ</span><span class="v">{bpm_effective:.0f} <span class="muted" style="font-size:10px;">({bpm_effective/m.bpm_max*100:.0f}% of declared)</span></span></div>'
        if bpm_diverges else ""
    )
    duration_str = f"{int(d.duration_s)//60}:{int(d.duration_s)%60:02d}"

    # Tooltip map for the feature labels that use domain shorthand or
    # abbreviations. Rendered as HTML title="…" — native browser tooltip,
    # no JS. Labels not in this map render without a tooltip.
    tips = {
        "peak 200ms burst": "Highest notes-per-second in any 200ms window. Captures short bursts (1/8 or 1/12 flurries) that show up in speed maps.",
        "peak 1s NPS": "Highest notes-per-second sustained across any 1-second window. A 1s NPS of 15 means the densest second held 15 notes.",
        "dominant divisor": "Most common note-to-note rhythmic gap: 1/4 (streams), 1/6 (bursts), 1/8, etc. Share is the % of note pairs with this gap.",
        "high-density ratio": "Fraction of the map's duration spent in high-density windows (~top 30% of NPS). Roughly \"how much of the map is dense\".",
        "longest sustained": "Longest continuous stretch in seconds where the note density stays above the map's mid-range NPS. Stamina test length.",
        "strain (integrated)": "Sum of per-note strain across the map (Alchyr-style). Each note costs more at high BPM, mid-burst, or after long mono runs. Bigger = more accumulated fatigue by the end.",
        "fatiguing windows": "Number of 10-second windows where accumulated strain exceeds the fatiguing threshold.",
        "stream count": "Number of distinct dense streams (contiguous 1/4-or-tighter runs of ≥8 notes).",
        "longest stream": "Length in notes of the map's single longest stream.",
        "stream value (agg)": "Sum of Alchyr's stream-value curve across all streams. Length-6 ≈ 45; length-60 ≈ 60; length-200 saturates. Higher = more overall stream difficulty.",
        "hostile-long (≥61 & parity ≥.25)": "Streams that are BOTH ≥61 notes long AND have per-note color friction ≥ 0.25. These break KDDK players (long chunks of hostile color = alternation-breaking fatigue).",
        "top stream color": "Highest per-note color friction seen in any stream (0-1). 0 = pure KDDK-friendly alternation, 1 = full mono. 0.25+ is where things get uncomfortable.",
        "divisor mix": "Distribution of note-to-note rhythmic gaps by divisor. A mix like 1/4 80% · 1/6 15% means occasional 1/6 bursts inside a mostly-1/4 map.",
        "mono-run max": "Length of the longest same-color run anywhere in the map (KKKK... or DDDD...). High mono runs are stamina + finger-fatigue tests for KDDK players.",
        "color-change ratio": "Fraction of consecutive note pairs where the color changed. High = alternating shapes (KDKD), low = mono chunks (KKKK).",
        "SV range": "Slider-velocity min and max seen anywhere in the map. Wide range = strong SV manipulation.",
        "SV stddev": "How wildly SV bounces around. Low = uniform scroll; high = chaotic SV changes (gimmick maps).",
        "SV changes/min": "Rate of SV transitions per minute. High = constant scroll manipulation.",
        "low-SV share": "Fraction of notes at SV < 0.75 (slow, clumped notes). Low-SV during dense play is the classic \"unreadable stack\" gimmick.",
        "unreadable ratio": "Fraction of notes that are BOTH low-SV AND in a dense area — the specific case where scroll slows down while notes pile on.",
        "sv-bpm score": "Composite: SV changes × SV stddev × BPM-dampening. High = SV chaos at moderate BPM, the canonical gimmick pattern.",
        "parity mean": "Average per-note KDDK-parity friction across the map. 0 = perfectly alternating; 1 = every note hostile to KDDK alternation.",
        "parity hostile ratio": "Fraction of notes where parity friction is high enough to actively hurt KDDK play (chunks that break the L-R-L-R cycle).",
        "burst count": "Number of short (3-6 note) 1/4-or-tighter clusters. Bursts are speed features, not stream stamina.",
        "burst mean length": "Average length of bursts. 3-4 = quick flurries; 5-6 = pushing into mini-stream territory.",
        "longest burst": "Length in notes of the map's longest burst.",
        "long-burst share (≥7)": "Fraction of bursts that are 7+ notes long — long enough that they start behaving like streams, not bursts.",
        "runway p50 (median dense)": "Median MILLISECONDS a note is visible on the playfield before it must be hit — the actual reaction-time window per note in the dense sections. Computed from stable-taiko's scroll physics (175 × SliderMultiplier × per-note SV × BPM/60, playfield 901.67px @ 16:9). Anchors: >700ms comfort, 500ms brisk, 400ms very hard, <300ms extreme.",
        "runway p95 (peak-stress)": "5th-percentile (SHORTEST) runway_ms — the fastest moments in the map. Captures peak stress that median glosses over. If p50 and p95 diverge a lot, the map has burst sections where reading briefly gets much harder.",
        "notes on screen (p95)": "Upper-tail count of notes visible on the playfield at once. Independent of BPM at fixed divisor/SV — depends on divisor and SV. Higher = more visual crowding (stacked notes to disambiguate). Comfort ~8, crowded ~14, very crowded ~20.",
        "dense p50 (bpm × sv, legacy)": "OLD velocity metric — kept for backward-compat display. Not scored anymore; the runway_ms metric above replaces it.",
        "sustained-fast share (legacy)": "OLD fast-scroll share metric — not scored anymore.",
        "stacked share (legacy)": "OLD low-scroll share metric — not scored anymore.",
    }
    def kv(label: str, value: str) -> str:
        tip = tips.get(label, "")
        title_attr = f' title="{tip}"' if tip else ""
        marker = " ⓘ" if tip else ""
        return (
            f'<div class="feat-row">'
            f'<span class="k"{title_attr}>{label}{marker}</span>'
            f'<span class="v">{value}</span>'
            f'</div>'
        )

    return f"""
  <section class="card">
    <h2>Why this rating</h2>
    <p class="hint">the underlying feature numbers, grouped by the dimension they feed. Hover any label with a ⓘ marker to see what it means.</p>

    <div class="feat-group">
      <div class="feat-title"><span>speed</span><span class="feat-val">{bpm_str} BPM · peak burst {d.peak_nps_200ms:.0f} n/s</span></div>
      {kv("BPM range", bpm_str)}
      {bpm_effective_row}
      {kv("peak 200ms burst", f"{d.peak_nps_200ms:.1f} notes/s")}
      {kv("peak 1s NPS", f"{d.peak_nps:.1f}")}
      {kv("dominant divisor", f"{r.dominant_divisor} ({r.dominant_divisor_share*100:.0f}%)")}
    </div>

    <div class="feat-group">
      <div class="feat-title"><span>stamina</span><span class="feat-val">avg {d.avg_nps:.1f} n/s over {duration_str}</span></div>
      {kv("duration", duration_str)}
      {kv("hittable notes", str(f.hittable_notes))}
      {kv("avg NPS", f"{d.avg_nps:.1f}")}
      {kv("peak 5s NPS", f"{d.peak_nps_5s:.1f}")}
      {kv("high-density ratio", f"{d.high_density_ratio*100:.0f}%")}
      {kv("longest sustained", f"{d.longest_sustained_high_s:.0f} s")}
      {kv("strain (integrated)", f"{s.total:.0f}")}
      {kv("fatiguing windows", str(s.fatiguing_windows))}
    </div>

    <div class="feat-group">
      <div class="feat-title"><span>technical</span><span class="feat-val">streams {f.streams.stream_count} · longest {f.streams.longest_stream} · hostile-long {f.streams.hostile_long_count}</span></div>
      {kv("stream count", str(f.streams.stream_count))}
      {kv("longest stream", str(f.streams.longest_stream))}
      {kv("stream value (agg)", f"{f.streams.stream_value:.1f}")}
      {kv("hostile-long (≥61 & parity ≥.25)", str(f.streams.hostile_long_count))}
      {kv("top stream color", f"{f.streams.top_stream_color:.3f}")}
      {kv("divisor mix", div_row)}
      {kv("mono-run max", str(c.run_length_max))}
      {kv("color-change ratio", f"{c.color_change_ratio*100:.0f}%")}
    </div>

    <div class="feat-group">
      <div class="feat-title"><span>gimmick</span><span class="feat-val">SV σ {m.sv_stddev:.3f} · SV changes/min {m.sv_changes_per_minute:.1f}</span></div>
      {kv("SV range", f"{m.sv_min:.2f} — {m.sv_max:.2f}")}
      {kv("SV stddev", f"{m.sv_stddev:.3f}")}
      {kv("SV changes/min", f"{m.sv_changes_per_minute:.1f}")}
      {kv("low-SV share", f"{g.low_sv_share*100:.0f}%")}
      {kv("unreadable ratio", f"{g.unreadable_ratio*100:.0f}%")}
      {kv("sv-bpm score", f"{g.sv_bpm_score:.1f}")}
    </div>

    <div class="feat-group">
      <div class="feat-title"><span>consistency</span><span class="feat-val">parity {f.parity.hostile_ratio*100:.0f}% hostile · bursts {b.burst_count}</span></div>
      {kv("parity mean", f"{f.parity.mean:.2f}")}
      {kv("parity hostile ratio", f"{f.parity.hostile_ratio*100:.0f}%")}
      {kv("burst count", str(b.burst_count))}
      {kv("burst mean length", f"{b.mean_length:.1f}")}
      {kv("longest burst", str(b.max_length))}
      {kv("long-burst share (≥7)", f"{b.length_7plus_ratio*100:.0f}%")}
    </div>

    <div class="feat-group">
      <div class="feat-title"><span>reading</span><span class="feat-val">runway {getattr(f.reading, 'runway_ms_dense_p50', 0):.0f} ms · {getattr(f.reading, 'notes_on_screen_p95', 0):.1f} notes on screen</span></div>
      {kv("runway p50 (median dense)", f"{getattr(f.reading, 'runway_ms_dense_p50', 0):.0f} ms")}
      {kv("runway p95 (peak-stress)", f"{getattr(f.reading, 'runway_ms_dense_p95', 0):.0f} ms")}
      {kv("notes on screen (p95)", f"{getattr(f.reading, 'notes_on_screen_p95', 0):.1f}")}
      {kv("dense p50 (bpm × sv, legacy)", f"{f.reading.velocity_dense_p50:.0f}")}
      {kv("sustained-fast share (legacy)", f"{f.reading.sustained_share*100:.0f}%")}
      {kv("stacked share (legacy)", f"{getattr(f.reading, 'stacked_share', 0)*100:.0f}%")}
    </div>
  </section>"""


_LEADERBOARD_DIMS = ("speed", "stamina", "gimmick", "technical", "consistency", "reading")


def _rank_medal(rank: int) -> str:
    """Small medal for top-3 ranks, plain # otherwise."""
    if rank == 1: return '<span class="lb-medal gold">1</span>'
    if rank == 2: return '<span class="lb-medal silver">2</span>'
    if rank == 3: return '<span class="lb-medal bronze">3</span>'
    return f'<span class="lb-rank">#{rank}</span>'


def _lb_user_card(rank: int, u: dict, dim: str, show_all_dims: bool = False) -> str:
    """One leaderboard row as a card (not a table row). Card layout means the
    value + other-dims cluster on the right stays visually aligned with the
    player info cluster on the left regardless of how many dim chips there
    are. Works for both the overview (show_all_dims=False, compact) and the
    full per-dim ranking (show_all_dims=True, expanded).

    `dim` can be a real dim or 'total' (sum of six). When 'total', the
    other-dims chips show ALL six dims (no dim is excluded)."""
    av = u.get("osu_avatar_url") or ""
    country = (u.get("osu_country_code") or "").upper()
    main_val = int(u[dim])
    replays = u.get("replays", 0)
    country_html = f'<span class="lb-country" title="{country}">{country}</span>' if country else ""
    other_dims_html = ""
    if show_all_dims:
        # For total: show all 6 dims. For a specific dim: show the other 5.
        chip_dims = _LEADERBOARD_DIMS if dim == "total" else [d for d in _LEADERBOARD_DIMS if d != dim]
        chips = "".join(
            f'<span class="lb-otherdim" title="{d}: {int(u[d]):,}">'
            f'<span class="lb-otherdim-k">{d[:3]}</span>'
            f'<span class="lb-otherdim-v">{int(u[d]):,}</span>'
            f'</span>'
            for d in chip_dims
        )
        other_dims_html = f'<div class="lb-otherdims">{chips}</div>'
    return f"""
    <a class="lb-card" href="/u/{u['osu_username']}">
      <div class="lb-card-rank">{_rank_medal(rank)}</div>
      {'<img class="lb-avatar" src="' + av + '" alt="">' if av else '<span class="lb-avatar-blank"></span>'}
      <div class="lb-card-name">
        <div class="lb-name-row">
          <span class="lb-name">{u['osu_username']}</span>
          {country_html}
        </div>
        <div class="lb-sub">{replays} replays</div>
      </div>
      <div class="lb-card-value">{main_val:,}</div>
      {other_dims_html}
    </a>"""


def _render_leaderboards_overview(overall: list[dict], cols: dict[str, list[dict]]) -> str:
    """Overall top-N panel first (total-skill ranking, cards with all 6 dim
    chips inline), then six per-dim column panels below. Every panel header
    is a single clickable link to its full ranking."""
    overall_cards = "".join(
        _lb_user_card(i + 1, u, "total", show_all_dims=True)
        for i, u in enumerate(overall)
    )
    if not overall_cards:
        overall_cards = '<div class="lb-empty">no plays yet — be the first!</div>'

    col_html = ""
    for dim in _LEADERBOARD_DIMS:
        users = cols.get(dim, [])
        cards = "".join(_lb_user_card(i + 1, u, dim) for i, u in enumerate(users))
        if not cards:
            cards = '<div class="lb-empty">no plays yet</div>'
        col_html += f"""
        <div class="lb-col">
          <a class="lb-col-title" href="/leaderboards/{dim}">
            <span class="lb-col-dim">{dim}</span>
            <span class="lb-col-more">view all →</span>
          </a>
          <div class="lb-list">{cards}</div>
        </div>"""

    body = f"""
  <section>
    <h1 class="page-title">Leaderboards</h1>
    <p class="hint">Total-skill ranking on top; per-dimension ranks below. Ranks are the latest-snapshot values from each player's public profile. Click a header for the full ranking.</p>
  </section>

  <section class="lb-overall-card">
    <a class="lb-col-title lb-col-title-hero" href="/leaderboards/total">
      <span class="lb-col-dim">Overall  ·  total skill</span>
      <span class="lb-col-more">view all →</span>
    </a>
    <div class="lb-list lb-list-full">{overall_cards}</div>
  </section>

  <section class="lb-overview">
    {col_html}
  </section>

  {_leaderboards_css()}
"""
    return _html_page("leaderboards", body)


def _render_leaderboards_dim(dim: str, users: list[dict]) -> str:
    """Full ranking for one dim as a card list (not a table). Cards keep
    the value + other-dim chips visually aligned across rows regardless
    of chip content width."""
    cards_html = "".join(_lb_user_card(i + 1, u, dim, show_all_dims=True) for i, u in enumerate(users))
    if not cards_html:
        cards_html = '<div class="lb-empty">nobody has played yet</div>'
    dim_tabs = " ".join(
        f'<a class="lb-tab {"active" if d == dim else ""}" href="/leaderboards/{d}">{d}</a>'
        for d in ("total",) + _LEADERBOARD_DIMS
    )
    body = f"""
  <section>
    <h1 class="page-title">Leaderboard · {dim}</h1>
    <p class="hint">Top players by skill.{dim}, most-recent snapshot per user. Only public profiles.</p>
    <div class="lb-tabs">{dim_tabs}</div>
  </section>

  <section class="lb-full">
    <div class="lb-list lb-list-full">{cards_html}</div>
  </section>

  {_leaderboards_css()}
"""
    return _html_page(f"leaderboards · {dim}", body)


def _leaderboards_css() -> str:
    return """
  <style>
    .page-title { font-family: var(--font-mono); font-size: 28px; margin: 0 0 8px; color: var(--ink); }
    /* Hero "Overall / total skill" panel — visually distinct from the six
       per-dim columns so it reads as THE headline ranking. */
    .lb-overall-card {
      background: var(--panel); border: 1px solid var(--rule);
      border-radius: 4px; padding: 20px 22px; margin: 20px 0 24px;
      border-top: 2px solid var(--accent);
    }
    .lb-col-title-hero .lb-col-dim { font-size: 15px !important; letter-spacing: 0.14em; }

    .lb-overview { display: grid; grid-template-columns: repeat(auto-fit, minmax(280px, 1fr)); gap: 20px; margin-top: 20px; }
    .lb-col { background: var(--panel); border: 1px solid var(--rule); border-radius: 4px; padding: 16px; }

    /* Whole column header is one link — dim name + view all click the same */
    .lb-col-title { display: flex; justify-content: space-between; align-items: baseline;
                    font-family: var(--font-mono); text-decoration: none; margin-bottom: 12px;
                    padding-bottom: 8px; border-bottom: 1px solid var(--rule); }
    .lb-col-title:hover { text-decoration: none; }
    .lb-col-title:hover .lb-col-dim { color: var(--ink); }
    .lb-col-title:hover .lb-col-more { color: var(--accent); }
    .lb-col-dim { font-size: 12px; letter-spacing: 0.16em; text-transform: uppercase; color: var(--accent); }
    .lb-col-more { font-size: 10px; color: var(--ink-faint); letter-spacing: 0.08em; }

    .lb-tabs { display: flex; flex-wrap: wrap; gap: 8px; margin: 20px 0; }
    .lb-tab { font-family: var(--font-mono); font-size: 11px; letter-spacing: 0.14em; text-transform: uppercase; padding: 6px 14px; border: 1px solid var(--rule); border-radius: 3px; color: var(--ink-muted); text-decoration: none; }
    .lb-tab:hover { border-color: var(--accent-soft); color: var(--ink); text-decoration: none; }
    .lb-tab.active { border-color: var(--accent); color: var(--accent); }

    /* Card-based leaderboard row. Grid layout keeps the value + chip cluster
       right-anchored regardless of chip count, and the player info stays
       left-anchored — no more table-column drift. */
    .lb-list { display: flex; flex-direction: column; gap: 6px; }
    .lb-card {
      display: grid; align-items: center;
      grid-template-columns: 30px 32px 1fr auto;
      grid-template-areas: "rank avatar name value";
      gap: 12px; padding: 8px 12px; border-radius: 4px;
      background: transparent; border: 1px solid transparent; text-decoration: none;
      font-family: var(--font-mono);
    }
    .lb-card:hover { border-color: var(--rule); background: rgba(255,255,255,0.02); text-decoration: none; }
    .lb-card-rank { grid-area: rank; text-align: center; }
    .lb-card .lb-avatar, .lb-card .lb-avatar-blank { grid-area: avatar; }
    .lb-card-name { grid-area: name; min-width: 0; }
    .lb-card-value { grid-area: value; font-size: 20px; font-weight: 500; color: var(--ink); font-variant-numeric: tabular-nums; text-align: right; min-width: 80px; }

    .lb-name-row { display: flex; align-items: center; gap: 8px; }
    .lb-name { color: var(--ink); font-weight: 500; font-size: 14px; }
    .lb-card:hover .lb-name { color: var(--accent); }
    .lb-sub { font-size: 10px; color: var(--ink-faint); margin-top: 2px; }

    /* Country as a small badge next to the name */
    .lb-country {
      display: inline-block; font-size: 9px; letter-spacing: 0.1em;
      padding: 2px 6px; border: 1px solid var(--rule); border-radius: 3px;
      color: var(--ink-muted); font-family: var(--font-mono); font-weight: 500;
      background: var(--panel);
    }

    .lb-avatar, .lb-avatar-blank { width: 32px; height: 32px; border-radius: 50%; object-fit: cover; border: 1px solid var(--rule); flex-shrink: 0; }
    .lb-avatar-blank { background: var(--panel); }

    .lb-rank { color: var(--ink-faint); font-size: 12px; font-weight: 500; }
    .lb-medal { display: inline-block; width: 22px; height: 22px; line-height: 22px; text-align: center; border-radius: 50%; font-weight: 700; font-size: 11px; }
    .lb-medal.gold { background: linear-gradient(180deg, #f0d475, #d4af37); color: #3a2a00; }
    .lb-medal.silver { background: linear-gradient(180deg, #dcdcdc, #a8a8a8); color: #1a1a1a; }
    .lb-medal.bronze { background: linear-gradient(180deg, #d29c6a, #a06a3a); color: #1a1a00; }

    /* Full-dim page: cards get an extra area for other-dim chips, wrapping
       below the main row on narrow viewports. */
    .lb-list-full .lb-card {
      grid-template-columns: 30px 32px 1fr auto auto;
      grid-template-areas: "rank avatar name value chips";
    }
    .lb-otherdims { grid-area: chips; display: flex; flex-wrap: wrap; gap: 4px; justify-content: flex-end; margin-left: 12px; }
    .lb-otherdim { display: inline-flex; gap: 4px; font-size: 10px; padding: 2px 8px; border: 1px solid var(--rule); border-radius: 3px; background: var(--panel); }
    .lb-otherdim-k { color: var(--ink-faint); letter-spacing: 0.06em; }
    .lb-otherdim-v { color: var(--ink); font-variant-numeric: tabular-nums; }

    @media (max-width: 760px) {
      .lb-list-full .lb-card {
        grid-template-columns: 30px 32px 1fr auto;
        grid-template-areas: "rank avatar name value" ".    .      chips chips";
      }
      .lb-otherdims { justify-content: flex-start; margin-left: 0; margin-top: 6px; }
    }

    .lb-empty { color: var(--ink-faint); text-align: center; padding: 20px; font-family: var(--font-mono); font-size: 12px; }
  </style>
"""


def _render_maps_page(
    rows: list[dict], total: int, sort: str, min_rating: float,
    q: str, page: int, limit: int,
) -> str:
    """Browsable map catalog. Search + sort + minimum-rating filter.
    Table shows each map's six ratings; row click → /map/{md5}."""
    _SORTS = [
        ("rating_speed", "speed"),
        ("rating_stamina", "stamina"),
        ("rating_gimmick", "gimmick"),
        ("rating_technical", "technical"),
        ("rating_consistency", "consistency"),
        ("rating_reading", "reading"),
        ("bpm_max", "bpm"),
        ("duration_s", "duration"),
        ("hittable_notes", "notes"),
        ("inserted_at", "recently added"),
    ]
    sort_opts = "".join(
        f'<option value="{v}" {"selected" if v == sort else ""}>{label}</option>'
        for v, label in _SORTS
    )

    body_rows = ""
    for r in rows:
        title = (r.get("title") or "?")
        version = (r.get("version") or "?")
        creator = (r.get("creator") or "?")
        dur = int(r.get("duration_s") or 0)
        dur_str = f"{dur//60}:{dur%60:02d}"
        bpm = r.get("bpm_max") or 0
        od = r.get("od") or 0
        notes = r.get("hittable_notes") or 0
        body_rows += (
            f'<tr class="map-row" data-href="/map/{r["md5"]}">'
            f'<td class="map-title-cell">'
            f'  <div class="map-title">{title}</div>'
            f'  <div class="map-sub">{version}  ·  by {creator}</div>'
            f'</td>'
            f'<td class="tabular">{int(bpm)}</td>'
            f'<td class="tabular">{od:.1f}</td>'
            f'<td class="tabular">{dur_str}</td>'
            f'<td class="tabular">{notes:,}</td>'
            f'<td class="tabular map-rating">{int(r.get("rating_speed") or 0):,}</td>'
            f'<td class="tabular map-rating">{int(r.get("rating_stamina") or 0):,}</td>'
            f'<td class="tabular map-rating">{int(r.get("rating_gimmick") or 0):,}</td>'
            f'<td class="tabular map-rating">{int(r.get("rating_technical") or 0):,}</td>'
            f'<td class="tabular map-rating">{int(r.get("rating_consistency") or 0):,}</td>'
            f'<td class="tabular map-rating">{int(r.get("rating_reading") or 0):,}</td>'
            f'</tr>'
        )
    if not body_rows:
        body_rows = '<tr><td colspan="11" class="lb-empty">no maps match those filters</td></tr>'

    n_pages = max(1, (total + limit - 1) // limit)
    from urllib.parse import urlencode
    def _page_link(p: int) -> str:
        qs = urlencode({"sort": sort, "min_rating": int(min_rating), "q": q, "page": p})
        return f"/maps?{qs}"
    pager = ""
    if n_pages > 1:
        prev_link = _page_link(max(1, page - 1)) if page > 1 else ""
        next_link = _page_link(min(n_pages, page + 1)) if page < n_pages else ""
        pager = f"""
      <div class="pager">
        {'<a class="page-btn" href="' + prev_link + '">← prev</a>' if prev_link else '<span class="page-btn disabled">← prev</span>'}
        <span class="page-info">page {page} of {n_pages}  ·  {total:,} maps total</span>
        {'<a class="page-btn" href="' + next_link + '">next →</a>' if next_link else '<span class="page-btn disabled">next →</span>'}
      </div>
"""

    body = f"""
  <section>
    <h1 class="page-title">Maps</h1>
    <p class="hint">Every map in the catalog. Filter by minimum rating in your target dimension to find calibrated training material.</p>
  </section>

  <section class="card">
    <form class="maps-filters" method="get" action="/maps">
      <label>
        Search
        <input type="search" name="q" value="{q}" placeholder="title, difficulty, or mapper">
      </label>
      <label>
        Sort by
        <select name="sort">{sort_opts}</select>
      </label>
      <label>
        Min rating
        <input type="number" name="min_rating" value="{int(min_rating)}" min="0" max="10000" step="50">
      </label>
      <button type="submit">Apply</button>
    </form>
  </section>

  <section class="card">
    <div style="overflow-x: auto;">
      <table class="maps-table">
        <thead><tr>
          <th>map</th>
          <th title="Maximum BPM in the map. If BPM varies, this is the peak.">bpm</th>
          <th title="OverallDifficulty from the .osu file — affects hit-window tightness.">od</th>
          <th title="Total length of the map's hittable section, MM:SS.">len</th>
          <th title="Number of hittable notes (excludes drumrolls + dendens).">notes</th>
          <th>spd</th>
          <th>stm</th>
          <th>gim</th>
          <th>tch</th>
          <th>cns</th>
          <th>rea</th>
        </tr></thead>
        <tbody>{body_rows}</tbody>
      </table>
    </div>
    {pager}
  </section>

  <style>
    .maps-filters {{ display: flex; flex-wrap: wrap; gap: 16px; align-items: flex-end; margin-top: 8px; }}
    .maps-filters label {{ display: flex; flex-direction: column; gap: 4px; font-family: var(--font-mono); font-size: 11px; letter-spacing: 0.08em; text-transform: uppercase; color: var(--ink-muted); }}
    .maps-filters input, .maps-filters select {{ padding: 8px 12px; border: 1px solid var(--rule); background: var(--panel); color: var(--ink); border-radius: 3px; font-family: var(--font-mono); font-size: 13px; min-width: 160px; }}
    .maps-filters button {{ padding: 9px 20px; border: 1px solid var(--accent); background: var(--accent); color: white; border-radius: 3px; font-family: var(--font-mono); font-size: 11px; letter-spacing: 0.12em; text-transform: uppercase; cursor: pointer; }}
    .maps-table {{ width: 100%; border-collapse: separate; border-spacing: 0 3px; }}
    .maps-table th {{ padding: 8px 10px; font-size: 10px; letter-spacing: 0.14em; text-transform: uppercase; color: var(--ink-faint); text-align: right; cursor: help; }}
    .maps-table th:first-child {{ text-align: left; }}
    .maps-table td {{ padding: 8px 10px; background: transparent; font-family: var(--font-mono); font-size: 12px; text-align: right; }}
    .maps-table td.map-title-cell {{ text-align: left; }}
    .maps-table td.map-rating {{ font-variant-numeric: tabular-nums; color: var(--ink); }}
    .maps-table td.tabular {{ font-variant-numeric: tabular-nums; color: var(--ink-muted); }}
    .maps-table tr.map-row {{ cursor: pointer; }}
    .maps-table tr.map-row:hover td {{ background: rgba(255,255,255,0.02); }}
    .map-title {{ color: var(--ink); font-weight: 500; }}
    .map-sub {{ font-size: 10px; color: var(--ink-faint); margin-top: 2px; }}
    .pager {{ display: flex; justify-content: center; align-items: center; gap: 20px; margin-top: 16px; }}
    .page-btn {{ padding: 6px 14px; border: 1px solid var(--rule); border-radius: 3px; color: var(--ink-muted); text-decoration: none; font-family: var(--font-mono); font-size: 11px; letter-spacing: 0.12em; text-transform: uppercase; }}
    .page-btn:hover {{ border-color: var(--accent); color: var(--accent); text-decoration: none; }}
    .page-btn.disabled {{ opacity: 0.4; cursor: not-allowed; }}
    .page-info {{ font-family: var(--font-mono); font-size: 11px; color: var(--ink-muted); }}
  </style>
  <script>
    document.querySelectorAll('.maps-table tr.map-row').forEach(tr => {{
      tr.addEventListener('click', ev => {{
        if (ev.target.tagName === 'A') return;
        window.location = tr.dataset.href;
      }});
    }});
  </script>
"""
    return _html_page("maps", body)


def _render_map_detail(row: dict, features, plays: list[dict]) -> str:
    """One map's full page: metadata, ratings, feature explanation panel,
    and the top plays leaderboard."""
    title = row.get("title") or "?"
    version = row.get("version") or "?"
    creator = row.get("creator") or "?"
    artist = row.get("artist") or ""
    bid = row.get("beatmap_id")
    bset = row.get("beatmapset_id")
    dur = int(row.get("duration_s") or 0)
    dur_str = f"{dur//60}:{dur%60:02d}"
    bpm_min = row.get("bpm_min") or 0
    bpm_max = row.get("bpm_max") or 0
    same = abs(bpm_min - bpm_max) < 0.5
    bpm_str = f"{bpm_max:.0f}" if same else f"{bpm_min:.0f}–{bpm_max:.0f}"

    osu_link = (
        f'<a class="hero-btn primary" href="https://osu.ppy.sh/beatmaps/{bid}" target="_blank" rel="noopener">Beatmap page</a>'
        if bid else ""
    )

    # Cover image from osu!'s CDN — same pattern as _render_map_hero on
    # replay pages. Falls back to a solid gradient if no beatmapset_id.
    cover_bg = (
        f'background-image: linear-gradient(180deg, rgba(15,17,20,0.35) 0%, rgba(15,17,20,0.92) 100%), '
        f'url("https://assets.ppy.sh/beatmaps/{bset}/covers/cover@2x.jpg");'
        if bset else
        'background: linear-gradient(135deg, var(--accent-cool), var(--accent) 90%);'
    )
    plays_rows = ""
    for i, p in enumerate(plays):
        rank = _rank_medal(i + 1)
        av = p.get("osu_avatar_url") or ""
        mods = p.get("mods_label") or "NM"
        mods_chip = _mods_chip(mods) if mods and mods != "NM" else ""
        acc = p["accuracy"] * 100
        plays_rows += (
            f'<tr class="map-play-row" data-href="/replay/{p["player_name"]}/{p["replay_id"]}">'
            f'<td class="mp-rank">{rank}</td>'
            f'<td class="mp-user">'
            f'{"<img class=\"mp-avatar\" src=\"" + av + "\" alt=\"\">" if av else "<span class=\"mp-avatar-blank\"></span>"}'
            f'<a class="mp-name" href="/u/{p["osu_username"]}">{p["osu_username"]}</a>'
            f'</td>'
            f'<td class="tabular">{acc:.2f}%</td>'
            f'<td>{mods_chip if mods_chip else "<span class=\"muted\">NM</span>"}</td>'
            f'<td class="tabular">{p["great"]}/{p["ok"]}/{p["miss"]}</td>'
            f'<td class="tabular muted">{p["played_at"][:10]}</td>'
            f'</tr>'
        )
    if not plays_rows:
        plays_rows = '<tr><td colspan="6" class="mp-empty">no public plays on this map yet</td></tr>'

    features_html = _render_features_panel(features) if features else ""

    body = f"""
  <section class="eyebrow-row">
    <span class="eyebrow"><a href="/maps" style="color: var(--ink-muted);">← maps</a></span>
  </section>

  <section class="map-hero" style='{cover_bg}'>
    <div class="hero-inner">
      <div class="hero-left">
        <div class="hero-pill-row">
          <span class="diff-pill">{version}</span>
          {('<span class="star-pill">★ ' + f"{row['star_rating']:.2f}" + '</span>') if row.get('star_rating') else ''}
        </div>
        <h1 class="hero-title">{title}</h1>
        <p class="hero-artist">{artist}</p>
        <p class="hero-meta">mapped by <b>{creator}</b>  ·  {row.get('hittable_notes', 0):,} notes</p>
        <div class="hero-actions">{osu_link}</div>
      </div>
      <div class="hero-right">
        <div class="hero-mapinfo">
          <div><span class="k">BPM</span><span class="v">{bpm_str}</span></div>
          <div><span class="k">length</span><span class="v">{dur_str}</span></div>
          <div><span class="k">notes</span><span class="v">{row.get('hittable_notes', 0):,}</span></div>
          <div><span class="k">OD</span><span class="v">{row.get('od', 0):.1f}</span></div>
        </div>
      </div>
    </div>
  </section>

  <section class="card">
    <h2>Rating</h2>
    <div class="stats-row">
      <div class="stat"><span class="k">speed</span><span class="v">{(row.get('rating_speed') or 0):,.0f}</span></div>
      <div class="stat"><span class="k">stamina</span><span class="v">{(row.get('rating_stamina') or 0):,.0f}</span></div>
      <div class="stat"><span class="k">gimmick</span><span class="v">{(row.get('rating_gimmick') or 0):,.0f}</span></div>
      <div class="stat"><span class="k">technical</span><span class="v">{(row.get('rating_technical') or 0):,.0f}</span></div>
      <div class="stat"><span class="k">consistency</span><span class="v">{(row.get('rating_consistency') or 0):,.0f}</span></div>
      <div class="stat"><span class="k">reading</span><span class="v">{(row.get('rating_reading') or 0):,.0f}</span></div>
    </div>
  </section>

  {features_html}

  <section class="card">
    <h2>Top plays  <span class="hint" style="font-size: 12px; margin-left: 8px;">{len(plays)} public plays</span></h2>
    <div style="overflow-x: auto;">
      <table class="mp-table">
        <thead><tr>
          <th>rank</th><th>player</th><th class="tabular">accuracy</th><th>mods</th><th class="tabular">great/ok/miss</th><th class="tabular">played</th>
        </tr></thead>
        <tbody>{plays_rows}</tbody>
      </table>
    </div>
  </section>

  <style>
    .mp-table {{ width: 100%; border-collapse: separate; border-spacing: 0 3px; font-family: var(--font-mono); }}
    .mp-table th {{ padding: 8px 10px; font-size: 10px; letter-spacing: 0.14em; text-transform: uppercase; color: var(--ink-faint); text-align: left; }}
    .mp-table th.tabular {{ text-align: right; }}
    .mp-table td {{ padding: 8px 10px; background: transparent; font-size: 12px; text-align: left; }}
    .mp-table td.tabular {{ font-variant-numeric: tabular-nums; text-align: right; color: var(--ink); }}
    .mp-rank {{ width: 40px; text-align: center; }}
    .mp-user {{ display: flex; align-items: center; gap: 10px; }}
    .mp-avatar, .mp-avatar-blank {{ width: 28px; height: 28px; border-radius: 50%; object-fit: cover; border: 1px solid var(--rule); flex-shrink: 0; }}
    .mp-avatar-blank {{ background: var(--panel); }}
    .mp-name {{ color: var(--ink); font-weight: 500; text-decoration: none; }}
    .mp-name:hover {{ color: var(--accent); text-decoration: none; }}
    .map-play-row {{ cursor: pointer; }}
    .map-play-row:hover td {{ background: rgba(255,255,255,0.02); }}
    .muted {{ color: var(--ink-faint); font-size: 11px; }}
    .mp-empty {{ color: var(--ink-faint); text-align: center; padding: 20px; font-size: 12px; }}
  </style>
  <script>
    document.querySelectorAll('.map-play-row').forEach(tr => {{
      tr.addEventListener('click', ev => {{
        if (ev.target.tagName === 'A' || ev.target.tagName === 'IMG') return;
        window.location = tr.dataset.href;
      }});
    }});
  </script>
"""
    return _html_page(f"{title} [{version}]", body)


def _render_empty_profile(user: dict, is_owner: bool) -> str:
    """Welcome empty-state for a public profile with no plays yet. Owner-view
    shows a CTA to upload; stranger-view says the user hasn't played
    anything yet on this instance."""
    avatar = user.get("osu_avatar_url") or ""
    cover = user.get("osu_cover_url") or ""
    username = user.get("osu_username", "?")
    country = (user.get("osu_country_code") or "").upper()

    # Same cover treatment as regular profile hero
    if cover:
        bg = (
            "background: "
            "linear-gradient(180deg, rgba(15,17,20,0.35) 0%, rgba(15,17,20,0.92) 100%), "
            f'url("{cover}"); '
            "background-size: cover; background-position: center;"
        )
    else:
        bg = (
            "background: "
            "radial-gradient(ellipse at 100% 20%, rgba(176,50,43,0.28) 0%, transparent 55%), "
            "radial-gradient(ellipse at 0% 100%, rgba(75,106,131,0.22) 0%, transparent 55%), "
            "#16181D;"
        )
    avatar_html = (
        f'<img class="hero-avatar" src="{avatar}" alt="{username} avatar">'
        if avatar else ""
    )
    country_pill = f'<span class="hero-country">{country}</span>' if country else ""

    if is_owner:
        cta_html = """
      <div class="empty-cta">
        <h2>Get started</h2>
        <p class="hint">Upload a replay to see your six-dimension skill vector, weakness patterns, and training recommendations.</p>
        <div class="empty-cta-buttons">
          <a class="cta-btn primary" href="/upload">Upload a replay</a>
          <a class="cta-btn" href="/settings/tokens">Set playstyle & mint uploader token</a>
        </div>
        <p class="hint" style="margin-top: 18px;">
          The <b>uploader companion</b> auto-uploads every new play from your osu! Data/r folder,
          so your report updates without opening a browser. Currently developer-install only;
          standalone binaries are being packaged.
        </p>
      </div>
"""
    else:
        cta_html = f"""
      <div class="empty-cta">
        <h2>No plays yet</h2>
        <p class="hint">{username} is signed up but hasn't uploaded any replays here yet. Check back later, or find someone with data on the <a href="/leaderboards">leaderboards</a>.</p>
      </div>
"""

    body = f"""
  <section class="map-hero" style='{bg}'>
    <div class="hero-inner {"has-avatar" if avatar else ""}">
      {avatar_html}
      <div class="hero-left">
        <div class="hero-pill-row">
          <span class="diff-pill">welcome</span>
        </div>
        <h1 class="hero-title">{username}</h1>
        <p class="hero-artist">{country_pill}</p>
        <p class="hero-meta">new profile · no plays yet</p>
      </div>
    </div>
  </section>

  <section class="card">
    {cta_html}
  </section>

  <style>
    .empty-cta {{ padding: 20px 0; }}
    .empty-cta h2 {{ margin-top: 0; }}
    .empty-cta-buttons {{ display: flex; gap: 12px; margin-top: 20px; flex-wrap: wrap; }}
    .cta-btn {{
      display: inline-block; padding: 12px 24px; border: 1px solid var(--rule);
      background: var(--panel); color: var(--ink); border-radius: 3px;
      font-family: var(--font-mono); font-size: 12px; letter-spacing: 0.14em;
      text-transform: uppercase; text-decoration: none;
    }}
    .cta-btn:hover {{ border-color: var(--accent); color: var(--accent); text-decoration: none; }}
    .cta-btn.primary {{ background: var(--accent); color: white; border-color: var(--accent); }}
    .cta-btn.primary:hover {{ opacity: 0.9; color: white; }}
  </style>
"""
    return _html_page(f"{username} · welcome", body)


def _render_upload_page(username: str | None) -> str:
    """Web-mode upload page — drag-drop zone + companion-app instructions.
    Both paths POST to /upload; the companion just automates it. In web mode
    the identity gate rejects .osr where player field != session user."""
    web_mode = auth_module.is_web_mode()
    identity_hint = (
        f'<p class="hint">You are logged in as <b>{username}</b>. '
        f'Uploads must be your own replays — the .osr\'s player field must '
        f'match your osu! username or the server rejects it.</p>'
        if web_mode and username else ""
    )
    body = f"""
  <section>
    <h1 class="page-title">Upload replays</h1>
    <p class="hint">Two ways to get your replays into taiko-trainer: drag-drop below for occasional uploads, or install the uploader companion for auto-upload every time you finish a play.</p>
  </section>

  <section class="card">
    <h2>Web upload  <span class="hint" style="font-size: 12px; margin-left: 8px;">occasional / one-off replays</span></h2>
    {identity_hint}
    <form id="upload-form" method="post" action="/upload" enctype="multipart/form-data">
      <div id="drop-zone" class="drop-zone">
        <div class="drop-zone-hint">
          <div class="drop-icon">↓</div>
          <div class="drop-primary">Drop <code>.osr</code> files here</div>
          <div class="drop-secondary">or <label for="file-picker" class="link-like">browse</label></div>
        </div>
        <input type="file" id="file-picker" name="file" accept=".osr" style="display: none;">
      </div>
      <div id="pending-file" class="pending-file" style="display: none;"></div>
      <button type="submit" id="upload-btn" class="upload-btn" disabled>Upload</button>
    </form>
    <p class="hint" style="margin-top: 14px;">
      Progress toast appears in the bottom-right corner after upload starts.
      Works across page navigation. Uploads persist even if you close this tab.
    </p>
  </section>

  <section class="card">
    <h2>Uploader companion  <span class="hint" style="font-size: 12px; margin-left: 8px;">auto-upload every play</span></h2>
    <p>
      A small local agent that watches your osu! <code>Data/r/</code> folder and posts
      new <code>.osr</code> files to your account seconds after you finish a play.
      Your report updates live without touching a browser.
    </p>
    <div class="companion-status">
      <div class="companion-status-label">Status:</div>
      <div class="companion-status-value">
        <span class="companion-badge">Coming soon — standalone binary</span>
        <div class="companion-note hint">
          Currently only available via the Python source (developer install). Standalone Windows/Mac/Linux binaries are being built. If you're comfortable with Python you can grab it from
          <a href="https://github.com/Acrith/osu-taiko-trainer" target="_blank" rel="noopener">the repo</a> — clone, <code>uv sync</code>, then <code>uv run taiko-uploader init</code>.
        </div>
      </div>
    </div>
  </section>

  <style>
    .page-title {{ font-family: var(--font-mono); font-size: 28px; margin: 0 0 8px; color: var(--ink); }}
    .drop-zone {{
      border: 2px dashed var(--rule); border-radius: 6px; padding: 40px 20px;
      text-align: center; cursor: pointer; transition: all 0.15s ease;
      background: var(--panel);
    }}
    .drop-zone.dragover {{ border-color: var(--accent); background: rgba(232, 100, 40, 0.08); }}
    .drop-zone-hint {{ pointer-events: none; }}
    .drop-icon {{ font-size: 42px; color: var(--ink-faint); margin-bottom: 8px; line-height: 1; }}
    .drop-primary {{ font-family: var(--font-mono); font-size: 15px; color: var(--ink); margin-bottom: 4px; }}
    .drop-secondary {{ font-family: var(--font-mono); font-size: 12px; color: var(--ink-muted); }}
    .link-like {{ color: var(--accent); text-decoration: underline; cursor: pointer; pointer-events: auto; }}
    .pending-file {{
      margin-top: 12px; padding: 10px 14px; background: var(--panel);
      border: 1px solid var(--rule); border-radius: 3px; font-family: var(--font-mono); font-size: 13px;
    }}
    .upload-btn {{
      margin-top: 14px; padding: 10px 28px; background: var(--accent); color: white;
      border: none; border-radius: 3px; font-family: var(--font-mono); font-size: 12px;
      letter-spacing: 0.14em; text-transform: uppercase; cursor: pointer;
    }}
    .upload-btn:disabled {{ background: var(--rule); color: var(--ink-faint); cursor: not-allowed; }}
    .companion-status {{ margin-top: 14px; padding: 16px; background: var(--panel); border: 1px solid var(--rule); border-radius: 4px; }}
    .companion-status-label {{ font-family: var(--font-mono); font-size: 10px; letter-spacing: 0.14em; text-transform: uppercase; color: var(--ink-faint); margin-bottom: 8px; }}
    .companion-badge {{
      display: inline-block; font-family: var(--font-mono); font-size: 11px;
      padding: 4px 10px; border: 1px dashed rgba(232, 164, 58, 0.6);
      background: rgba(232, 164, 58, 0.08); color: #e8a43a; border-radius: 3px;
    }}
    .companion-note {{ margin-top: 10px; font-size: 12px; }}
  </style>
  <script>
    const dz = document.getElementById('drop-zone');
    const picker = document.getElementById('file-picker');
    const pending = document.getElementById('pending-file');
    const btn = document.getElementById('upload-btn');

    function selectFile(f) {{
      if (!f) return;
      if (!f.name.toLowerCase().endsWith('.osr')) {{
        alert('Only .osr replay files are accepted.');
        return;
      }}
      // Move the picker's files programmatically so form submit sends it
      const dt = new DataTransfer();
      dt.items.add(f);
      picker.files = dt.files;
      pending.textContent = '📎 ' + f.name + '  (' + (f.size / 1024).toFixed(1) + ' KB)';
      pending.style.display = 'block';
      btn.disabled = false;
    }}

    dz.addEventListener('click', () => picker.click());
    picker.addEventListener('change', () => selectFile(picker.files[0]));

    dz.addEventListener('dragover', (e) => {{ e.preventDefault(); dz.classList.add('dragover'); }});
    dz.addEventListener('dragleave', () => dz.classList.remove('dragover'));
    dz.addEventListener('drop', (e) => {{
      e.preventDefault();
      dz.classList.remove('dragover');
      selectFile(e.dataTransfer.files[0]);
    }});
  </script>
"""
    return _html_page("upload", body)


def _render_web_landing(recent_users: list[dict]) -> str:
    """Anon landing page for web mode. Explains what the tool is + the
    "log in with osu!" CTA + a small "recently active" strip so a
    first-time visitor sees the service isn't a ghost town."""
    users_html = ""
    if recent_users:
        chips = "".join(
            f'<a class="landing-user" href="/u/{u["osu_username"]}">'
            f'{"<img src=\"" + u["osu_avatar_url"] + "\" alt=\"\">" if u["osu_avatar_url"] else ""}'
            f'<span>{u["osu_username"]}</span></a>'
            for u in recent_users
        )
        users_html = f"""
    <div class="landing-recent">
      <div class="landing-recent-label">Recently active</div>
      <div class="landing-user-strip">{chips}</div>
    </div>
"""

    body = f"""
  <section class="landing-hero">
    <h1 class="landing-title">taiko-trainer</h1>
    <p class="landing-lede">
      Skill diagnosis and training targets for osu!taiko. Upload replays,
      get a six-dimension skill vector, see the exact patterns that hurt
      your accuracy, and get map recommendations calibrated to push what
      you're weakest at.
    </p>
    <div class="landing-cta-row">
      <a class="landing-cta" href="/login">Log in with osu!</a>
    </div>
    <p class="landing-hint">
      Free. Data stays yours — you can delete your account and every
      replay at any time from settings.
    </p>
    {users_html}
  </section>

  <section class="landing-features">
    <div class="landing-feat">
      <div class="landing-feat-title">Six-dim skill vector</div>
      <div class="landing-feat-body">Speed, stamina, gimmick, technical, consistency, reading — each anchored to real KDDK play, each modulated by the mods your replays used (DT, HR, HD, HDDT, HRDT…).</div>
    </div>
    <div class="landing-feat">
      <div class="landing-feat-title">Per-miss classification</div>
      <div class="landing-feat-body">Every miss tagged with the primary cause (wrong color, parity break, speed cap, stamina, technical, gimmick, timing drift) so weakness patterns are diagnostic, not just "you missed some notes".</div>
    </div>
    <div class="landing-feat">
      <div class="landing-feat-title">Uploader companion</div>
      <div class="landing-feat-body">Small local agent watches your Data/r folder and auto-posts new replays to your account. Your report updates seconds after each play. Historic replays never touched unless you explicitly ask.</div>
    </div>
  </section>

  <style>
    .landing-hero {{ text-align: center; padding: 60px 20px 20px; }}
    .landing-title {{ font-family: var(--font-mono); font-size: 56px; letter-spacing: -0.02em; margin: 0 0 16px; color: var(--ink); }}
    .landing-lede {{ font-size: 16px; line-height: 1.6; color: var(--ink-muted); max-width: 640px; margin: 0 auto 32px; }}
    .landing-cta-row {{ margin: 28px 0 12px; }}
    .landing-cta {{ display: inline-block; padding: 14px 36px; background: var(--accent); color: white; border-radius: 3px; font-family: var(--font-mono); font-size: 13px; letter-spacing: 0.16em; text-transform: uppercase; text-decoration: none; }}
    .landing-cta:hover {{ background: var(--accent-hover, #d18944); text-decoration: none; }}
    .landing-hint {{ font-size: 12px; color: var(--ink-faint); margin-top: 8px; }}
    .landing-recent {{ margin-top: 48px; }}
    .landing-recent-label {{ font-family: var(--font-mono); font-size: 10px; letter-spacing: 0.14em; text-transform: uppercase; color: var(--ink-faint); margin-bottom: 14px; }}
    .landing-user-strip {{ display: flex; flex-wrap: wrap; gap: 12px; justify-content: center; }}
    .landing-user {{ display: flex; align-items: center; gap: 8px; padding: 6px 12px; border: 1px solid var(--rule); border-radius: 3px; font-family: var(--font-mono); font-size: 12px; color: var(--ink-muted); text-decoration: none; }}
    .landing-user:hover {{ border-color: var(--accent); color: var(--accent); text-decoration: none; }}
    .landing-user img {{ width: 20px; height: 20px; border-radius: 50%; }}
    .landing-features {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(220px, 1fr)); gap: 20px; margin-top: 60px; }}
    .landing-feat {{ padding: 20px; border: 1px solid var(--rule); border-radius: 4px; background: var(--panel); }}
    .landing-feat-title {{ font-family: var(--font-mono); font-size: 12px; letter-spacing: 0.12em; text-transform: uppercase; color: var(--accent); margin-bottom: 10px; }}
    .landing-feat-body {{ font-size: 13px; line-height: 1.6; color: var(--ink-muted); }}
  </style>
"""
    return _html_page("taiko-trainer", body)


def _render_tokens_page(
    user: dict, tokens: list[dict], new_raw: str,
    current_style: str = "unknown", flash_ok: str = "",
) -> str:
    """Settings page for managing API tokens. Two states:

    - Regular view: form to create a new token, list of existing tokens
      with revoke buttons.
    - Just-created view: shows the raw new token ONCE in a copy box with
      a "this is your only chance to save it" warning. Query param
      driven — reload/navigate away and it's gone.
    """
    new_token_html = ""
    if new_raw:
        new_token_html = f"""
    <div class="new-token-banner">
      <div class="new-token-label">New token created — copy it now, this is the only time it will be shown:</div>
      <div class="new-token-box">
        <code id="new-token-val">{new_raw}</code>
        <button type="button" class="copy-btn" onclick="navigator.clipboard.writeText(document.getElementById('new-token-val').textContent).then(() => this.textContent = 'Copied!')">Copy</button>
      </div>
      <p class="hint">
        Save this in your uploader config file. If you lose it, revoke this token and create a new one — the raw value cannot be recovered from the server.
      </p>
    </div>
"""
    rows_html = ""
    if not tokens:
        rows_html = '<tr><td colspan="4" class="muted" style="text-align: center; padding: 20px;">no tokens yet</td></tr>'
    else:
        for t in tokens:
            revoked = t["revoked_at"] is not None
            last_used = t["last_used_at"][:16].replace("T", " ") if t["last_used_at"] else "never"
            created = t["created_at"][:16].replace("T", " ")
            status_cell = (
                f'<td><span class="pill revoked">revoked</span></td>'
                if revoked
                else f'<td><form method="post" action="/settings/tokens/{t["id"]}/revoke" style="margin:0"><button type="submit" class="revoke-btn" onclick="return confirm(\'Revoke this token? Any uploader still using it will fail.\')">Revoke</button></form></td>'
            )
            rows_html += (
                f'<tr class="{ "revoked-row" if revoked else "" }">'
                f'<td><code class="token-prefix">{t["prefix"]}…</code></td>'
                f'<td>{t["label"]}</td>'
                f'<td class="muted">{created}</td>'
                f'<td class="muted">{last_used}</td>'
                f'{status_cell}'
                f'</tr>'
            )

    # Playstyle picker — must be set before scoring makes sense
    style_flash = ""
    if flash_ok == "style-set":
        style_flash = '<div class="flash flash-ok">✓ playstyle updated · ratings refreshed</div>'
    style_options_html = ""
    for value, label, desc in (
        ("kddk", "KDDK", "outer keys = kat, inner keys = don. Alternates L-R regardless of color. The most common competitive playstyle."),
        ("ddkk", "DDKK", "left hand = all Dons, right hand = all Kats. Long mono-color runs are single-hand stamina."),
        ("kkdd", "KKDD", "mirror of DDKK — left hand = all Kats, right hand = all Dons."),
    ):
        checked = "checked" if current_style == value else ""
        style_options_html += (
            f'<label class="style-opt">'
            f'<input type="radio" name="style" value="{value}" {checked} required>'
            f'<div><strong>{label}</strong><span class="hint">{desc}</span></div>'
            f'</label>'
        )
    unset_warning = ""
    if current_style == "unknown":
        unset_warning = (
            '<div class="unset-warning">⚠  Your playstyle isn\'t set yet — '
            'scoring uses KDDK by default until you pick one below. Skill '
            'vector will refresh with your style\'s cost model after you save.</div>'
        )
    style_section_html = f"""
  <section class="card">
    <h2>Playstyle</h2>
    <p class="hint">
      Which physical fingering do you use? This drives stamina + technical
      scoring — DDKK/KKDD players get a color-to-hand cost model, KDDK gets
      strict alternation with chunk-misalignment friction.
    </p>
    {unset_warning}
    {style_flash}
    <form method="post" action="/settings/style" class="style-form">
      <div class="style-opts">{style_options_html}</div>
      <button type="submit" class="style-btn">Save playstyle</button>
    </form>
  </section>
"""

    current_public = bool(user.get("profile_public", 1))
    profile_toggle_html = f"""
  <section class="card">
    <h2>Profile visibility</h2>
    <p class="hint">
      When public, anyone can see your training report at <code>/u/{user['osu_username']}</code>
      including maps, mods, hit distribution, and skill vector. When private, only you can see it
      (via <code>/me</code>). Toggling doesn't delete data — just hides it from strangers.
    </p>
    <form method="post" action="/settings/profile-visibility" class="visibility-form">
      <div class="visibility-current">
        Currently: <strong class="{'is-public' if current_public else 'is-private'}">{'PUBLIC' if current_public else 'PRIVATE'}</strong>
      </div>
      <button type="submit" name="public" value="{'false' if current_public else 'true'}" class="visibility-btn">
        {'Make private' if current_public else 'Make public'}
      </button>
    </form>
  </section>
"""

    body = f"""
  <section class="eyebrow-row">
    <span class="eyebrow"><a href="/u/{user['osu_username']}" style="color: var(--ink-muted);">← {user['osu_username']}</a>  ·  settings</span>
  </section>

  {style_section_html}

  {profile_toggle_html}

  <section class="card">
    <h2>API tokens</h2>
    <p class="hint">
      Tokens let the uploader companion post replays to your account without
      opening a browser. Create one per machine so you can revoke individually
      if a device is compromised or retired.
    </p>
    {new_token_html}
    <form method="post" action="/settings/tokens" class="token-form">
      <label>Label <input type="text" name="label" placeholder="e.g. Home PC" required maxlength="80"></label>
      <button type="submit">Create token</button>
    </form>
  </section>

  <section class="card">
    <h2>Existing tokens</h2>
    <div style="overflow-x: auto;">
      <table>
        <thead><tr><th>prefix</th><th>label</th><th>created</th><th>last used</th><th>action</th></tr></thead>
        <tbody>{rows_html}</tbody>
      </table>
    </div>
  </section>

  <section class="card danger-card">
    <h2>Delete account</h2>
    <p class="hint">
      Permanent + irreversible. Removes your <code>users</code> row, revokes every API
      token, and deletes your per-player database including all replay data, snapshots,
      and skill history. Your osu! account itself is not affected — you can log back
      in later to start fresh from scratch.
    </p>
    <details class="danger-details">
      <summary>Show delete controls</summary>
      <form method="post" action="/settings/delete-account" class="danger-form"
            onsubmit="return confirm('Really delete your account and everything on the server? This cannot be undone.');">
        <label>
          Type your osu! username (<code>{user['osu_username']}</code>) to confirm:
          <input type="text" name="confirm" required autocomplete="off" placeholder="{user['osu_username']}">
        </label>
        <button type="submit" class="danger-btn">Delete account permanently</button>
      </form>
    </details>
  </section>

  <style>
    .new-token-banner {{ background: var(--accent-faint); border: 1px solid var(--accent-soft); border-radius: 4px; padding: 16px; margin: 12px 0; }}
    .new-token-label {{ font-family: var(--font-mono); font-size: 11px; text-transform: uppercase; letter-spacing: 0.12em; color: var(--accent); margin-bottom: 8px; }}
    .new-token-box {{ display: flex; align-items: center; gap: 8px; background: var(--panel); padding: 8px 12px; border-radius: 3px; font-family: var(--font-mono); overflow-x: auto; }}
    .new-token-box code {{ font-size: 13px; color: var(--ink); flex: 1; white-space: nowrap; overflow-x: auto; }}
    .copy-btn {{ background: var(--accent); color: white; border: none; padding: 6px 14px; border-radius: 3px; font-family: var(--font-mono); font-size: 11px; letter-spacing: 0.12em; text-transform: uppercase; cursor: pointer; }}
    .token-form {{ display: flex; align-items: center; gap: 12px; margin-top: 14px; }}
    .token-form label {{ display: flex; align-items: center; gap: 8px; flex: 1; font-family: var(--font-mono); font-size: 11px; letter-spacing: 0.08em; text-transform: uppercase; color: var(--ink-muted); }}
    .token-form input {{ flex: 1; padding: 8px 12px; border: 1px solid var(--rule); background: var(--panel); color: var(--ink); border-radius: 3px; font-family: var(--font-mono); font-size: 13px; }}
    .token-form button {{ padding: 8px 20px; border: 1px solid var(--accent); background: var(--accent); color: white; border-radius: 3px; font-family: var(--font-mono); font-size: 11px; letter-spacing: 0.12em; text-transform: uppercase; cursor: pointer; }}
    .revoke-btn {{ background: none; border: 1px solid var(--rule); color: var(--ink-muted); padding: 4px 12px; border-radius: 3px; font-family: var(--font-mono); font-size: 10px; letter-spacing: 0.14em; text-transform: uppercase; cursor: pointer; }}
    .revoke-btn:hover {{ border-color: var(--miss); color: var(--miss); }}
    .revoked-row {{ opacity: 0.5; }}
    .pill.revoked {{ font-family: var(--font-mono); font-size: 10px; letter-spacing: 0.14em; text-transform: uppercase; padding: 2px 8px; background: var(--rule); color: var(--ink-muted); border-radius: 3px; }}
    .token-prefix {{ font-family: var(--font-mono); font-size: 12px; color: var(--ink); }}
    .visibility-form {{ display: flex; align-items: center; gap: 20px; margin-top: 14px; }}
    .visibility-current {{ font-family: var(--font-mono); font-size: 12px; letter-spacing: 0.08em; color: var(--ink-muted); }}
    .visibility-current strong.is-public {{ color: var(--great); letter-spacing: 0.14em; }}
    .visibility-current strong.is-private {{ color: var(--miss); letter-spacing: 0.14em; }}
    .visibility-btn {{ padding: 8px 20px; border: 1px solid var(--rule); background: var(--panel); color: var(--ink); border-radius: 3px; font-family: var(--font-mono); font-size: 11px; letter-spacing: 0.12em; text-transform: uppercase; cursor: pointer; }}
    .visibility-btn:hover {{ border-color: var(--accent); color: var(--accent); }}
    .style-opts {{ display: grid; gap: 10px; margin-top: 14px; }}
    .style-opt {{ display: flex; gap: 12px; padding: 12px 14px; border: 1px solid var(--rule); border-radius: 4px; cursor: pointer; align-items: flex-start; }}
    .style-opt:hover {{ border-color: var(--accent-soft); background: rgba(255,255,255,0.02); }}
    .style-opt input {{ margin-top: 3px; }}
    .style-opt strong {{ display: block; font-family: var(--font-mono); font-size: 13px; letter-spacing: 0.12em; color: var(--ink); margin-bottom: 4px; }}
    .style-opt .hint {{ font-size: 12px; margin: 0; }}
    .style-opt input:checked ~ div strong {{ color: var(--accent); }}
    .style-opt:has(input:checked) {{ border-color: var(--accent); background: rgba(232, 100, 40, 0.05); }}
    .style-btn {{ margin-top: 14px; padding: 10px 28px; background: var(--accent); color: white; border: none; border-radius: 3px; font-family: var(--font-mono); font-size: 12px; letter-spacing: 0.14em; text-transform: uppercase; cursor: pointer; }}
    .style-btn:hover {{ opacity: 0.9; }}
    .unset-warning {{ margin-top: 12px; padding: 10px 14px; background: rgba(232, 164, 58, 0.10); border: 1px solid rgba(232, 164, 58, 0.4); border-radius: 4px; color: #e8a43a; font-family: var(--font-mono); font-size: 12px; }}
    .flash.flash-ok {{ margin-top: 12px; padding: 10px 14px; background: rgba(103, 194, 88, 0.10); border: 1px solid rgba(103, 194, 88, 0.4); border-radius: 4px; color: var(--great); font-family: var(--font-mono); font-size: 12px; }}
    .danger-card {{ border-color: var(--miss); }}
    .danger-details {{ margin-top: 12px; }}
    .danger-details summary {{ cursor: pointer; color: var(--ink-muted); font-family: var(--font-mono); font-size: 11px; letter-spacing: 0.08em; }}
    .danger-details summary:hover {{ color: var(--miss); }}
    .danger-form {{ display: flex; flex-direction: column; gap: 12px; margin-top: 14px; padding: 14px; background: var(--panel); border: 1px dashed var(--miss); border-radius: 4px; }}
    .danger-form label {{ display: flex; flex-direction: column; gap: 6px; font-family: var(--font-mono); font-size: 12px; color: var(--ink-muted); }}
    .danger-form input {{ padding: 8px 12px; border: 1px solid var(--rule); background: var(--bg); color: var(--ink); border-radius: 3px; font-family: var(--font-mono); font-size: 14px; }}
    .danger-btn {{ align-self: flex-start; padding: 8px 20px; border: 1px solid var(--miss); background: transparent; color: var(--miss); border-radius: 3px; font-family: var(--font-mono); font-size: 11px; letter-spacing: 0.14em; text-transform: uppercase; cursor: pointer; }}
    .danger-btn:hover {{ background: var(--miss); color: white; }}
  </style>
"""
    return _html_page(f"settings — {user['osu_username']}", body)


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

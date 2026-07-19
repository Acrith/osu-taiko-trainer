"""Authentication for the hosted web build.

Two modes coexist:

- **LOCAL**  — no auth required. A single implicit user (id=1) owns everything.
              This is how the tool has always run and keeps working during
              the transition to hosted.
- **WEB**    — osu! OAuth 2.0 Authorization Code flow. User clicks "Log in
              with osu!" → redirect → callback → server issues a signed
              session cookie. All user-owned data (replays, snapshots) is
              filtered by the resolved user_id.

Mode is picked from env: `TAIKO_TRAINER_MODE=web` opts in. Anything else
(including unset) stays in local mode.

Config, when in web mode, comes from env:

    OSU_OAUTH_CLIENT_ID       — from https://osu.ppy.sh/home/account/edit
    OSU_OAUTH_CLIENT_SECRET   — same
    OSU_OAUTH_REDIRECT_URI    — must match the app registration exactly
                                (e.g. https://your-domain/oauth/callback)
    SESSION_SECRET            — long random string; if missing, we generate
                                one at startup but sessions won't survive
                                a restart. Set this in production.

Design notes:
- Session cookie is signed (HMAC via itsdangerous), NOT encrypted. We only
  put `user_id` and an issued-at timestamp inside — nothing sensitive. The
  cookie proves "the server previously accepted this user_id" and nothing
  else.
- 30-day expiration, refreshed on each request so an actively used session
  never times out.
- State parameter on the OAuth flow is a signed random token bound to the
  session; it protects against CSRF on the callback.
"""
from __future__ import annotations

import os
import secrets
import time
from dataclasses import dataclass

import httpx
from fastapi import Cookie, HTTPException, Request
from fastapi.responses import RedirectResponse
from itsdangerous import BadSignature, SignatureExpired, TimestampSigner
from itsdangerous.url_safe import URLSafeTimedSerializer


# --- Mode -------------------------------------------------------------------

def is_web_mode() -> bool:
    """`TAIKO_TRAINER_MODE=web` opts into web-hosted mode with real auth."""
    return os.environ.get("TAIKO_TRAINER_MODE", "local").lower() == "web"


# --- OAuth config -----------------------------------------------------------

OSU_AUTHORIZE_URL = "https://osu.ppy.sh/oauth/authorize"
OSU_TOKEN_URL = "https://osu.ppy.sh/oauth/token"
OSU_ME_URL = "https://osu.ppy.sh/api/v2/me/taiko"

SESSION_COOKIE_NAME = "tt_session"
STATE_COOKIE_NAME = "tt_oauth_state"
SESSION_MAX_AGE_S = 30 * 24 * 3600   # 30 days
STATE_MAX_AGE_S = 10 * 60             # 10 minutes for the OAuth handoff


class AuthConfigError(RuntimeError):
    """Web mode requested but OAuth env is missing."""


@dataclass(frozen=True)
class OAuthConfig:
    client_id: str
    client_secret: str
    redirect_uri: str
    session_secret: str


def _load_config() -> OAuthConfig:
    """Read OAuth env vars. In web mode, missing values are a hard error —
    the app should refuse to start rather than silently issue useless
    sessions."""
    client_id = os.environ.get("OSU_OAUTH_CLIENT_ID", "")
    client_secret = os.environ.get("OSU_OAUTH_CLIENT_SECRET", "")
    redirect_uri = os.environ.get("OSU_OAUTH_REDIRECT_URI", "")
    session_secret = os.environ.get("SESSION_SECRET", "")

    if is_web_mode():
        missing = [k for k, v in (
            ("OSU_OAUTH_CLIENT_ID", client_id),
            ("OSU_OAUTH_CLIENT_SECRET", client_secret),
            ("OSU_OAUTH_REDIRECT_URI", redirect_uri),
        ) if not v]
        if missing:
            raise AuthConfigError(
                f"web mode requires env vars: {', '.join(missing)}"
            )
        if not session_secret:
            # A generated secret works but sessions die on restart. Warn once
            # here rather than at every request.
            print(
                "WARNING: SESSION_SECRET unset — sessions will not survive "
                "a restart. Set SESSION_SECRET for production.",
                flush=True,
            )
            session_secret = secrets.token_urlsafe(32)
    else:
        # Local mode: a generated secret is fine — nothing sensitive is signed
        # anyway, and the local user doesn't need session persistence.
        session_secret = session_secret or secrets.token_urlsafe(32)

    return OAuthConfig(
        client_id=client_id,
        client_secret=client_secret,
        redirect_uri=redirect_uri,
        session_secret=session_secret,
    )


_CONFIG: OAuthConfig | None = None


def config() -> OAuthConfig:
    """Lazily load + cache the OAuth config. First call is what would raise
    AuthConfigError, so ordering matters — call this from app startup."""
    global _CONFIG
    if _CONFIG is None:
        _CONFIG = _load_config()
    return _CONFIG


# --- Session cookie ---------------------------------------------------------

def _serializer() -> URLSafeTimedSerializer:
    return URLSafeTimedSerializer(config().session_secret, salt="tt-session")


def _state_signer() -> TimestampSigner:
    return TimestampSigner(config().session_secret, salt="tt-oauth-state")


def make_session_cookie(user_id: int) -> str:
    """Sign a session token carrying user_id. Not encrypted — treat the
    payload as public."""
    return _serializer().dumps({"user_id": int(user_id), "iat": int(time.time())})


def read_session_cookie(cookie_value: str | None) -> int | None:
    """Return the user_id embedded in the cookie, or None if absent/invalid/expired."""
    if not cookie_value:
        return None
    try:
        data = _serializer().loads(cookie_value, max_age=SESSION_MAX_AGE_S)
    except (BadSignature, SignatureExpired):
        return None
    uid = data.get("user_id") if isinstance(data, dict) else None
    if not isinstance(uid, int) or uid <= 0:
        return None
    return uid


# --- OAuth flow -------------------------------------------------------------

def make_state_token() -> str:
    """Random string signed with a short timestamp. Bound to the callback,
    protects against CSRF on the OAuth flow."""
    raw = secrets.token_urlsafe(16)
    signed = _state_signer().sign(raw.encode()).decode()
    return signed


def verify_state_token(token: str | None) -> bool:
    if not token:
        return False
    try:
        _state_signer().unsign(token.encode(), max_age=STATE_MAX_AGE_S)
        return True
    except (BadSignature, SignatureExpired):
        return False


def authorize_url(state: str) -> str:
    """URL to redirect the user to for osu! login consent."""
    cfg = config()
    from urllib.parse import urlencode
    params = {
        "client_id": cfg.client_id,
        "redirect_uri": cfg.redirect_uri,
        "response_type": "code",
        "scope": "identify",
        "state": state,
    }
    return f"{OSU_AUTHORIZE_URL}?{urlencode(params)}"


@dataclass(frozen=True)
class OsuMe:
    """Slim subset of /api/v2/me/taiko we care about."""
    id: int
    username: str
    avatar_url: str
    cover_url: str
    country_code: str
    global_rank_taiko: int | None


def exchange_code_for_user(code: str) -> OsuMe:
    """Trade the authorization code for a user access token, then fetch
    /me/taiko. Raises OsuApiError on any failure — the callback route
    catches it and shows an error page."""
    from .osu_api import OsuApiError  # local import to avoid cycles
    cfg = config()
    tok_resp = httpx.post(
        OSU_TOKEN_URL,
        data={
            "client_id": cfg.client_id,
            "client_secret": cfg.client_secret,
            "code": code,
            "grant_type": "authorization_code",
            "redirect_uri": cfg.redirect_uri,
        },
        timeout=15.0,
    )
    if tok_resp.status_code >= 400:
        raise OsuApiError(
            f"osu! token exchange failed: HTTP {tok_resp.status_code} — {tok_resp.text[:200]}"
        )
    token = tok_resp.json().get("access_token")
    if not token:
        raise OsuApiError("osu! token exchange returned no access_token")

    me_resp = httpx.get(
        OSU_ME_URL,
        headers={"Authorization": f"Bearer {token}"},
        timeout=15.0,
    )
    if me_resp.status_code >= 400:
        raise OsuApiError(
            f"osu! /me/taiko failed: HTTP {me_resp.status_code} — {me_resp.text[:200]}"
        )
    me = me_resp.json()
    stats = me.get("statistics", {}) or {}
    cover = (me.get("cover", {}) or {}).get("url", "")
    return OsuMe(
        id=int(me["id"]),
        username=me.get("username", ""),
        avatar_url=me.get("avatar_url", ""),
        cover_url=cover,
        country_code=(me.get("country") or {}).get("code", ""),
        global_rank_taiko=stats.get("global_rank"),
    )


# --- FastAPI dependencies ---------------------------------------------------
# Import Request lazily where needed to avoid a hard fastapi dep in library code.

def current_user_id(
    session: str | None = Cookie(default=None, alias=SESSION_COOKIE_NAME),
) -> int | None:
    """Return the current user_id from the session cookie, or None if
    anonymous. Use this on public routes that decorate output when the
    viewer is logged in (e.g. "this is you" markers)."""
    if not is_web_mode():
        # Local mode: implicit single user.
        return 1
    return read_session_cookie(session)


def require_login(
    session: str | None = Cookie(default=None, alias=SESSION_COOKIE_NAME),
) -> int:
    """Enforce login. Raises 401 (which the app can convert to a redirect)
    if no valid session cookie is present. In local mode, always resolves
    to user_id=1."""
    if not is_web_mode():
        return 1
    uid = read_session_cookie(session)
    if uid is None:
        # 401 with WWW-Authenticate hint the caller can special-case.
        raise HTTPException(status_code=401, detail="login required")
    return uid


def login_redirect() -> RedirectResponse:
    """Build the /login redirect used on 401s. Caller sets the state cookie."""
    return RedirectResponse(url="/login", status_code=302)

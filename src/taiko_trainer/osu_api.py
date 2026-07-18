"""osu! API v2 client and .osz downloader.

Uses OAuth 2.0 Client Credentials flow — no user login required for the
public-data endpoints we need (beatmap lookup, user profile). User registers
an OAuth app at https://osu.ppy.sh/home/account/edit → OAuth → New OAuth
Application, pastes the resulting `client_id` and `client_secret` into the
trainer once per workspace.

Beatmap lookup goes through the official API. Beatmap-set downloads (.osz
files) don't have an official API endpoint — we fetch from a mirror
(beatconnect.io primary, catboy.best fallback, both no-auth).

Config is stored per-workspace in catalog.db's catalog_meta table under keys
`osu_client_id`, `osu_client_secret`, `osu_access_token`, `osu_token_expires_at`.
"""
from __future__ import annotations

import io
import json
import sqlite3
import time
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import httpx

from .db import get_catalog_meta, set_catalog_meta


OSU_BASE_URL = "https://osu.ppy.sh"
OSU_API_URL = f"{OSU_BASE_URL}/api/v2"
OSU_TOKEN_URL = f"{OSU_BASE_URL}/oauth/token"

# .osz mirrors — no auth required. Ordered by preference.
_MIRRORS = (
    "https://beatconnect.io/b/{setid}",
    "https://catboy.best/d/{setid}",
    "https://nerinyan.moe/d/{setid}",
)

DEFAULT_TIMEOUT = 30.0
DOWNLOAD_TIMEOUT = 120.0


class OsuApiError(RuntimeError):
    """Raised for any API/mirror failure the caller should surface."""


class OsuApiNotConfigured(OsuApiError):
    """No client_id / client_secret in catalog_meta yet."""


@dataclass(frozen=True)
class BeatmapLookup:
    beatmap_id: int
    beatmapset_id: int
    title: str
    artist: str
    version: str          # difficulty name
    creator: str          # mapper's username
    md5: str
    star_rating: float
    status: str           # 'ranked' | 'loved' | 'graveyard' | ...
    mode: str             # 'taiko' etc.


@dataclass(frozen=True)
class OsuUser:
    id: int
    username: str
    avatar_url: str
    country_code: str
    global_rank_taiko: int | None


def is_configured(conn: sqlite3.Connection) -> bool:
    return bool(get_catalog_meta(conn, "osu_client_id") and
                get_catalog_meta(conn, "osu_client_secret"))


def save_credentials(conn: sqlite3.Connection, client_id: str, client_secret: str) -> None:
    set_catalog_meta(conn, "osu_client_id", client_id.strip())
    set_catalog_meta(conn, "osu_client_secret", client_secret.strip())
    # Invalidate any cached token — new credentials might belong to a different app.
    set_catalog_meta(conn, "osu_access_token", "")
    set_catalog_meta(conn, "osu_token_expires_at", "0")


def _get_token(conn: sqlite3.Connection) -> str:
    """Return a valid bearer token, minting a new one if the cached one is
    expired or absent. Raises OsuApiNotConfigured if the workspace has no
    OAuth client set yet."""
    client_id = get_catalog_meta(conn, "osu_client_id")
    client_secret = get_catalog_meta(conn, "osu_client_secret")
    if not client_id or not client_secret:
        raise OsuApiNotConfigured(
            "osu! OAuth client not configured for this workspace. "
            "Add credentials via the home page's osu! API settings."
        )
    cached = get_catalog_meta(conn, "osu_access_token")
    expires_str = get_catalog_meta(conn, "osu_token_expires_at") or "0"
    try:
        expires_at = float(expires_str)
    except ValueError:
        expires_at = 0.0
    # Refresh a minute before actual expiry to avoid mid-request expiration.
    if cached and time.time() < expires_at - 60:
        return cached

    resp = httpx.post(
        OSU_TOKEN_URL,
        data={
            "client_id": client_id,
            "client_secret": client_secret,
            "grant_type": "client_credentials",
            "scope": "public",
        },
        timeout=DEFAULT_TIMEOUT,
    )
    if resp.status_code >= 400:
        raise OsuApiError(
            f"osu! token request failed: HTTP {resp.status_code} — {resp.text[:200]}"
        )
    data = resp.json()
    token = data["access_token"]
    ttl = int(data.get("expires_in", 3600))
    set_catalog_meta(conn, "osu_access_token", token)
    set_catalog_meta(conn, "osu_token_expires_at", str(time.time() + ttl))
    return token


def lookup_beatmap(conn: sqlite3.Connection, md5: str) -> BeatmapLookup | None:
    """Find a beatmap by MD5 checksum. Returns None if not found."""
    token = _get_token(conn)
    resp = httpx.get(
        f"{OSU_API_URL}/beatmaps/lookup",
        params={"checksum": md5.lower()},
        headers={"Authorization": f"Bearer {token}"},
        timeout=DEFAULT_TIMEOUT,
    )
    if resp.status_code == 404:
        return None
    if resp.status_code >= 400:
        raise OsuApiError(
            f"osu! beatmap lookup failed: HTTP {resp.status_code} — {resp.text[:200]}"
        )
    data = resp.json()
    bset = data.get("beatmapset", {}) or {}
    return BeatmapLookup(
        beatmap_id=data.get("id", 0),
        beatmapset_id=data.get("beatmapset_id", 0),
        title=bset.get("title", "?"),
        artist=bset.get("artist", "?"),
        version=data.get("version", "?"),
        creator=bset.get("creator", "?"),
        md5=data.get("checksum", md5.lower()),
        star_rating=float(data.get("difficulty_rating", 0.0)),
        status=data.get("status", "?"),
        mode=data.get("mode", "?"),
    )


def lookup_user(conn: sqlite3.Connection, username: str) -> OsuUser | None:
    """Find a user by username. Returns None if not found. Used to pull an
    avatar URL for the player hero card."""
    token = _get_token(conn)
    resp = httpx.get(
        f"{OSU_API_URL}/users/{username}/taiko",
        headers={"Authorization": f"Bearer {token}"},
        timeout=DEFAULT_TIMEOUT,
    )
    if resp.status_code == 404:
        return None
    if resp.status_code >= 400:
        raise OsuApiError(
            f"osu! user lookup failed: HTTP {resp.status_code} — {resp.text[:200]}"
        )
    data = resp.json()
    stats = data.get("statistics", {}) or {}
    return OsuUser(
        id=data.get("id", 0),
        username=data.get("username", username),
        avatar_url=data.get("avatar_url", ""),
        country_code=(data.get("country") or {}).get("code", ""),
        global_rank_taiko=stats.get("global_rank"),
    )


def download_osz(beatmapset_id: int) -> bytes:
    """Fetch the raw .osz zip for a beatmapset from one of our mirrors.
    Tries mirrors in order; first to give a 200 wins. Raises OsuApiError if
    every mirror fails."""
    last_error = None
    for template in _MIRRORS:
        url = template.format(setid=beatmapset_id)
        try:
            resp = httpx.get(url, timeout=DOWNLOAD_TIMEOUT, follow_redirects=True)
            if resp.status_code == 200 and len(resp.content) > 1024:
                return resp.content
            last_error = f"{url} -> HTTP {resp.status_code}"
        except httpx.HTTPError as e:
            last_error = f"{url} -> {e!r}"
    raise OsuApiError(f"all osu! mirrors failed for set {beatmapset_id}: {last_error}")


def extract_osu_files_from_osz(osz_bytes: bytes) -> dict[str, bytes]:
    """Return {member_name: content} for every .osu file in the .osz zip."""
    result: dict[str, bytes] = {}
    with zipfile.ZipFile(io.BytesIO(osz_bytes)) as z:
        for name in z.namelist():
            if name.endswith(".osu"):
                result[name] = z.read(name)
    return result


def fetch_map_for_md5(conn: sqlite3.Connection, md5: str) -> tuple[bytes, BeatmapLookup] | None:
    """High-level: given a beatmap MD5, look it up on the API, download the
    beatmapset .osz, extract the specific .osu whose bytes match the MD5,
    return (content, lookup). Returns None if the MD5 can't be resolved.

    The caller is still responsible for calling _ingest_sibling_maps on the
    other .osu files inside the .osz — this function only returns the
    single played diff's content."""
    import hashlib
    lookup = lookup_beatmap(conn, md5)
    if not lookup or not lookup.beatmapset_id:
        return None
    osz = download_osz(lookup.beatmapset_id)
    files = extract_osu_files_from_osz(osz)
    for content in files.values():
        if hashlib.md5(content).hexdigest().lower() == md5.lower():
            return content, lookup
    return None

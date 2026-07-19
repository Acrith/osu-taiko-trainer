"""Map suggestion engine.

Given a player's 5-D skill vector and a target dimension to train, rank maps
from the DB by their fitness as a training target:

- HIGH growth: the map's rating in the target dim is 10-40% above the player's
  current skill in that dim ("just past your reach — you'll grow by playing it")
- LOW overwhelm: the map's ratings in OTHER dims aren't massively above the
  player's other skills (a technical map won't help you if it also demands
  10× more stamina than you have)

Score per map = growth_score − overwhelm_penalty, where:
- growth_score is a Gaussian centred at +25% above player's target skill
- overwhelm_penalty grows with each other-dim rating that exceeds 1.5× player's
  corresponding skill

The engine returns the top-N maps for the target dimension. Callers can filter
out maps the player has already cleared at high accuracy.
"""
from __future__ import annotations

import math
import sqlite3
from dataclasses import dataclass
from typing import Any

from .db import get_all_maps
from .player import PlayerSkill

# Works uniformly against a raw catalog connection OR a plays connection with
# catalog ATTACHed — both cases route reads to the `maps` table via get_all_maps.


_DIMS = ("speed", "stamina", "gimmick", "technical", "consistency", "reading")


@dataclass(frozen=True)
class MapSuggestion:
    md5: str
    title: str
    version: str
    creator: str
    target_dim: str
    target_rating: float
    target_gain_frac: float
    growth_score: float
    overwhelm_penalty: float
    suggestion_score: float
    map_ratings: dict[str, float]


def _growth_curve(gain_frac: float) -> float:
    """Bell centred at 25% growth; sigma ~ 15%.

    Below player skill → 0. At exactly player skill → 0.2. At +25% → 1.0.
    Beyond +75% → tail off to 0.
    """
    if gain_frac < -0.05:
        return 0.0
    return math.exp(-((gain_frac - 0.25) ** 2) / (2 * 0.15 * 0.15))


def _overwhelm_penalty(
    map_ratings: dict[str, float],
    skill: dict[str, float],
    other_dims: list[str],
) -> float:
    penalty = 0.0
    for od in other_dims:
        map_od = map_ratings[od]
        player_od = skill[od]
        if player_od > 50:
            excess = map_od / player_od - 1.5
            if excess > 0:
                penalty += excess
        elif map_od > 100:
            # Player has ~zero skill in this dim; map demands something → overwhelming.
            penalty += map_od / 200.0
    return penalty


def suggest_maps(
    conn: sqlite3.Connection,
    skill: PlayerSkill,
    target_dim: str,
    top_n: int = 5,
    exclude_md5s: set[str] | None = None,
) -> list[MapSuggestion]:
    """Return top-N suggested maps for pushing the target dimension."""
    if target_dim not in _DIMS:
        raise ValueError(f"target_dim must be one of {_DIMS}, got {target_dim!r}")

    all_maps = get_all_maps(conn)
    skill_d = skill.as_dict()
    other_dims = [d for d in _DIMS if d != target_dim]
    exclude = exclude_md5s or set()

    scored: list[MapSuggestion] = []
    for m in all_maps:
        if m["md5"] in exclude:
            continue
        map_ratings = {
            "speed": m["rating_speed"],
            "stamina": m["rating_stamina"],
            "gimmick": m["rating_gimmick"],
            "technical": m["rating_technical"],
            "consistency": m["rating_consistency"],
            "reading": m.get("rating_reading") or 0.0,
        }
        target_rating = map_ratings[target_dim]
        player_target = skill_d[target_dim]

        # Fractional growth relative to current skill.
        if player_target > 20:
            gain_frac = (target_rating - player_target) / player_target
        elif target_rating > 0:
            gain_frac = min(2.0, target_rating / 100.0)
        else:
            gain_frac = -1.0

        growth = _growth_curve(gain_frac)
        overwhelm = _overwhelm_penalty(map_ratings, skill_d, other_dims)
        score = growth - min(overwhelm * 0.35, 0.8)

        scored.append(MapSuggestion(
            md5=m["md5"], title=m["title"], version=m["version"], creator=m["creator"],
            target_dim=target_dim,
            target_rating=target_rating,
            target_gain_frac=gain_frac,
            growth_score=growth,
            overwhelm_penalty=overwhelm,
            suggestion_score=score,
            map_ratings=map_ratings,
        ))

    scored.sort(key=lambda s: -s.suggestion_score)
    return scored[:top_n]


def find_weakest_dim(skill: PlayerSkill) -> str:
    """Return the dimension where the player is weakest — natural training target."""
    d = skill.as_dict()
    return min(d, key=lambda k: d[k])

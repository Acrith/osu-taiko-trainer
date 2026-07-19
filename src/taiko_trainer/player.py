"""Player skill vector aggregation.

Given a player's clearing history (map ratings × their performance on each),
derive a 5-D skill vector: `speed`, `stamina`, `gimmick`, `technical`,
`consistency`, each unbounded like the map rating scale.

Per-dimension skill is computed as a weighted sum of top-K performances,
where performance for a single replay in dimension D is:

    performance_D = map_rating_D  ×  accuracy_scaling(replay_accuracy)

The `accuracy_scaling` function is a smooth curve: below 85% acc it's 0
(the replay doesn't count as a clear); at 95% it's ~0.6; at 99%+ it's ~1.0.
This mirrors the pp system where accuracy dramatically affects credit.

Peak-weighting: top performances get full weight, subsequent ones get
`0.9^rank`. Sum of top K becomes the player's skill in that dimension.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field, asdict

from .classification import FailureSummary
from .judgment import JudgedReplay
from .scoring import DimensionRating


@dataclass(frozen=True)
class ReplayPerformance:
    """One replay's contribution to the player's skill vector."""
    map_title: str
    map_diff: str
    map_rating: DimensionRating
    accuracy: float
    misses: int


@dataclass(frozen=True)
class PlayerSkill:
    speed: float
    stamina: float
    gimmick: float
    technical: float
    consistency: float
    reading: float = 0.0    # scroll-speed / reaction skill

    def as_dict(self) -> dict[str, float]:
        return asdict(self)


_ACC_FLOOR = 0.85    # below this, replay contributes 0
_ACC_CEIL = 1.0      # SS is the only "full credit" tier; 99.5% now gets slightly
                     # less so perfection is rewarded (real osu! pp gives SS a
                     # ~7% edge over 99.5%; our sqrt curve gives ~1.7%).
_DECAY = 0.9         # weight decay per rank in top-K sum


def _accuracy_scaling(acc: float) -> float:
    """Smooth ramp: 0.85 acc -> 0; 0.995 acc -> ~1; concave in between."""
    if acc <= _ACC_FLOOR:
        return 0.0
    if acc >= _ACC_CEIL:
        return 1.0
    frac = (acc - _ACC_FLOOR) / (_ACC_CEIL - _ACC_FLOOR)
    # sqrt gives a concave curve — mid-range accuracy already contributes meaningfully
    return math.sqrt(frac)


def _weighted_top_sum(performances: list[float]) -> float:
    """Sort desc, weight 0.9^rank, sum. Same structural pattern as pp calc."""
    ordered = sorted(performances, reverse=True)
    return sum(v * (_DECAY ** i) for i, v in enumerate(ordered))


def compute_player_skill(performances: list[ReplayPerformance]) -> PlayerSkill:
    """Aggregate a list of replay performances into a 5-D skill vector.

    Per-map dedup: multiple replays of the same (map_title, map_diff) count
    only once per dimension, at the best (highest contribution) attempt.
    Mirrors osu!'s pp system where only your top score on each beatmap feeds
    your total. Without this, re-uploading the same replay or grinding a map
    twice doubles its contribution to your skill number."""
    if not performances:
        return PlayerSkill(0.0, 0.0, 0.0, 0.0, 0.0, 0.0)

    # per_dim[dim][(title, diff)] = best contribution seen so far
    per_dim: dict[str, dict[tuple[str, str], float]] = {
        "speed": {}, "stamina": {}, "gimmick": {}, "technical": {}, "consistency": {}, "reading": {},
    }
    for p in performances:
        scale = _accuracy_scaling(p.accuracy)
        if scale <= 0:
            continue
        rating = p.map_rating.as_dict()
        key = (p.map_title, p.map_diff)
        for dim, bucket in per_dim.items():
            contribution = rating[dim] * scale
            if contribution <= 0:
                continue
            if contribution > bucket.get(key, 0.0):
                bucket[key] = contribution

    return PlayerSkill(
        speed=_weighted_top_sum(list(per_dim["speed"].values())),
        stamina=_weighted_top_sum(list(per_dim["stamina"].values())),
        gimmick=_weighted_top_sum(list(per_dim["gimmick"].values())),
        technical=_weighted_top_sum(list(per_dim["technical"].values())),
        consistency=_weighted_top_sum(list(per_dim["consistency"].values())),
        reading=_weighted_top_sum(list(per_dim["reading"].values())),
    )

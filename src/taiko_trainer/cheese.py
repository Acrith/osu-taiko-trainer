"""Cheese detection: identify moments where a KDDK player broke alternation.

Per the KDDK model, canonical technique is strict L-R hand alternation — every
consecutive note goes to the opposite hand. When a player uses the SAME hand
for two consecutive notes at fast tempo, that's cheese (waves, sarahna, or
single-hand tapping to bypass a technical alternation puzzle with speed).

This isn't inherently bad — many players intentionally cheese fast patterns
because their alternation can't keep up. But it's diagnostic:
- High cheese rate = player relies on single-hand speed
- Low cheese rate = player has strong alternation technique

We report:
- Total count of same-hand consecutive pairs (aka "cheese pairs")
- Rate per minute
- Per-cheese-moment detail: (time, gap_ms, hand)

See memory/feedback_divisor_semantics.md for the cheese-detection concept and
memory/feedback_kddk_parity.md for the KDDK alternation model.
"""
from __future__ import annotations

from dataclasses import dataclass

from .judgment import JudgedReplay, Judgment, Verdict
from .models import TaikoInput


@dataclass(frozen=True)
class CheeseMoment:
    time_ms: int
    gap_ms: int
    hand: str            # "L" or "R"
    prev_judgment: Judgment
    curr_judgment: Judgment


@dataclass(frozen=True)
class CheeseReport:
    moments: tuple[CheeseMoment, ...]
    total_pairs: int              # number of consecutive-key pairs considered
    same_hand_pairs: int          # same-hand consecutive events (raw cheese count)
    fast_cheese_pairs: int        # same-hand pairs with gap under FAST_GAP threshold
    cheese_rate: float            # fast_cheese_pairs / total_pairs
    duration_s: float
    fast_cheese_per_minute: float


_FAST_GAP_MS = 90   # under this gap between same-hand hits = definitely cheese/wave


def _hand_of(inp: TaikoInput) -> str | None:
    """Return 'L' or 'R' for a single-color key event, or None."""
    if inp & TaikoInput.LEFT_DON or inp & TaikoInput.LEFT_KAT:
        return "L"
    if inp & TaikoInput.RIGHT_DON or inp & TaikoInput.RIGHT_KAT:
        return "R"
    return None


def detect_cheese(judged: JudgedReplay) -> CheeseReport:
    """Walk consecutive-hit judgments; count same-hand pairs and fast-cheese moments."""
    hits = [j for j in judged.judgments if j.hit_input is not None]
    if len(hits) < 2:
        return CheeseReport((), 0, 0, 0, 0.0, 0.0, 0.0)

    moments: list[CheeseMoment] = []
    same_hand = 0
    fast_cheese = 0
    total_pairs = 0

    for i in range(1, len(hits)):
        prev = hits[i - 1]
        curr = hits[i]
        prev_hand = _hand_of(prev.hit_input)
        curr_hand = _hand_of(curr.hit_input)
        if prev_hand is None or curr_hand is None:
            continue

        # Only score pairs where both hits are close enough in TIME to be a real
        # "consecutive input" moment. A same-hand pair with 500ms between them
        # isn't cheese — the player had time to reset.
        if prev.hit_time_ms is None or curr.hit_time_ms is None:
            continue
        gap = curr.hit_time_ms - prev.hit_time_ms
        if gap <= 0 or gap > 400:
            continue

        total_pairs += 1
        if prev_hand == curr_hand:
            same_hand += 1
            if gap < _FAST_GAP_MS:
                fast_cheese += 1
                moments.append(CheeseMoment(
                    time_ms=curr.hit_time_ms,
                    gap_ms=gap,
                    hand=curr_hand,
                    prev_judgment=prev,
                    curr_judgment=curr,
                ))

    cheese_rate = fast_cheese / total_pairs if total_pairs > 0 else 0.0
    span_ms = hits[-1].hit_time_ms - hits[0].hit_time_ms if hits[0].hit_time_ms and hits[-1].hit_time_ms else 0
    duration_s = span_ms / 1000.0
    per_min = fast_cheese / (duration_s / 60.0) if duration_s > 0 else 0.0

    return CheeseReport(
        moments=tuple(moments),
        total_pairs=total_pairs,
        same_hand_pairs=same_hand,
        fast_cheese_pairs=fast_cheese,
        cheese_rate=cheese_rate,
        duration_s=duration_s,
        fast_cheese_per_minute=per_min,
    )

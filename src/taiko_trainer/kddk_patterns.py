"""KDDK stream metric: length-weighted × Alchyr-style per-stream parity friction.

Streams are detected BPM-aware (a note is in a stream if its gap to the next is
<= 1.15 × the 1/4-note gap at its own timing-point BPM), so multi-BPM maps work
correctly. For each stream we compute:
  - length_value: a fast-growth asymptotic curve (0 below 8 notes, saturating
    near 60 above 60 notes) rewarding sustained streams.
  - per-stream color friction: the sum of Alchyr's per-transition color-add
    bonuses, averaged per note. Same-parity losses and repetition losses crush
    long muscle-memory-locked patterns like KDDDDD-repeating.

Stream value = length_value × min(1, per_note_color/0.30), aggregated across
streams via 0.5^rank weighted sum so the longest hostile stream dominates.

A separate "hostile long" count fires when a stream is BOTH ≥ 61 notes AND
its per-note color friction ≥ 0.25 (short-run mixing). This is Blue Army's
signature: multiple sustained streams whose short-run mixing never repeats
enough for muscle memory to lock in.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Sequence

from .models import HitObject


# stream-detection parameters
_MIN_STREAM_LEN = 8         # patterns shorter than this don't create sustained load
_GAP_FACTOR = 1.15          # note counts as stream if gap ≤ 1.15 * local 1/4 gap

# length-value curve: fast growth 8..50, saturating near 60 above 60 notes
_LENGTH_MAX = 60.0
_LENGTH_SLOPE = 0.055
_LENGTH_ZERO = 8

# hostile-long threshold: streams of this size with per-note color ≥ HOSTILE_MIN
# are the distinctive KDDK signature
_HOSTILE_MIN_LEN = 61
_HOSTILE_MIN_COLOR = 0.25

# Alchyr constants for per-transition color-add (see osu-performance issue #61
# and Alchyr/taiko VB reference implementation)
_BASE_SWAP_BONUS = 1.5
_SWAP_SCALE = 1.75
_COLOR_BONUS_CAP = 1.25
_SAME_POLARITY_LOSS = 0.8
_CLOSE_REPEAT_LOSS = 0.525
_LATE_REPEAT_LOSS = 0.75

# stream-value aggregation: top stream weighs 1, next 0.5, then 0.25... — the
# longest hostile stream dominates but shorter streams still contribute a bit.
_STREAM_DECAY = 0.5


@dataclass(frozen=True)
class StreamProfile:
    stream_count: int              # number of qualifying streams (>= 8 notes)
    longest_stream: int            # note count of the longest stream
    stream_value: float            # aggregated length × parity metric; 0..~120
    hostile_long_count: int        # streams ≥ 61 notes AND per-note color ≥ 0.25
    top_stream_color: float        # max per-note color friction across streams


def stream_profile(hittable: Sequence[HitObject]) -> StreamProfile:
    if len(hittable) < _MIN_STREAM_LEN:
        return StreamProfile(0, 0, 0.0, 0, 0.0)

    streams = _extract_streams(hittable)
    if not streams:
        return StreamProfile(0, 0, 0.0, 0, 0.0)

    values: list[float] = []
    hostile_long = 0
    top_color = 0.0
    for s in streams:
        lv = _length_value(len(s))
        cv = _stream_color_avg(s)
        top_color = max(top_color, cv)
        values.append(lv * min(1.0, cv / 0.30))
        if len(s) >= _HOSTILE_MIN_LEN and cv >= _HOSTILE_MIN_COLOR:
            hostile_long += 1
    values.sort(reverse=True)
    sv = sum(v * (_STREAM_DECAY ** i) for i, v in enumerate(values))

    return StreamProfile(
        stream_count=len(streams),
        longest_stream=max(len(s) for s in streams),
        stream_value=sv,
        hostile_long_count=hostile_long,
        top_stream_color=top_color,
    )


def _extract_streams(hittable: Sequence[HitObject]) -> list[list[HitObject]]:
    streams: list[list[HitObject]] = []
    cur: list[HitObject] = [hittable[0]]
    for i in range(1, len(hittable)):
        prev = hittable[i - 1]
        gap = hittable[i].time_ms - prev.time_ms
        bpm = prev.bpm if prev.bpm else 120
        threshold = (60000 / (bpm * 4)) * _GAP_FACTOR
        if gap <= threshold:
            cur.append(hittable[i])
        else:
            if len(cur) >= _MIN_STREAM_LEN:
                streams.append(cur)
            cur = [hittable[i]]
    if len(cur) >= _MIN_STREAM_LEN:
        streams.append(cur)
    return streams


def _length_value(L: int) -> float:
    if L < _LENGTH_ZERO:
        return 0.0
    return _LENGTH_MAX * (1.0 - math.exp(-_LENGTH_SLOPE * (L - _LENGTH_ZERO)))


def _stream_color_avg(stream: Sequence[HitObject]) -> float:
    """Per-note Alchyr color friction summed across the stream, averaged.
    Tracks recent same-color mono lengths for same-polarity and repetition
    decay per Alchyr's color signal."""
    if len(stream) < 2:
        return 0.0
    prev_kat_lens = [0, 0]
    prev_don_lens = [0, 0]
    same_type_count = 1
    prev_is_kat = not stream[0].note_type.is_don
    total = 0.0
    for i in range(1, len(stream)):
        cur_is_kat = not stream[i].note_type.is_don
        if cur_is_kat != prev_is_kat:
            return_val = _BASE_SWAP_BONUS - (_SWAP_SCALE / (same_type_count + 0.65))
            return_mult = 1.0
            if prev_is_kat:
                if (same_type_count % 2) == (prev_don_lens[0] % 2):
                    return_mult *= _SAME_POLARITY_LOSS
                if prev_kat_lens[0] == same_type_count:
                    return_mult *= _CLOSE_REPEAT_LOSS
                if prev_kat_lens[1] == same_type_count:
                    return_mult *= _LATE_REPEAT_LOSS
                prev_kat_lens[1] = prev_kat_lens[0]
                prev_kat_lens[0] = same_type_count
            else:
                if (same_type_count % 2) == (prev_kat_lens[0] % 2):
                    return_mult *= _SAME_POLARITY_LOSS
                if prev_don_lens[0] == same_type_count:
                    return_mult *= _CLOSE_REPEAT_LOSS
                if prev_don_lens[1] == same_type_count:
                    return_mult *= _LATE_REPEAT_LOSS
                prev_don_lens[1] = prev_don_lens[0]
                prev_don_lens[0] = same_type_count
            total += min(_COLOR_BONUS_CAP, return_val) * return_mult
            same_type_count = 1
        else:
            same_type_count += 1
        prev_is_kat = cur_is_kat
    return total / len(stream)

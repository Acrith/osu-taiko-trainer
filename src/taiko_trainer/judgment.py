"""Judgment engine — pair map notes with replay hits and classify.

For each hittable note in the beatmap, find the earliest color-matching
key-down in the replay within the OD-scaled miss window, and classify as
GREAT / OK / MISS based on the hit-delta timing.

Windows for osu!taiko (default, no mods that affect timing):
    great = 50 - 3 * OD    ms   (e.g. OD 7 -> 29 ms)
    ok    = 120 - 8 * OD   ms   (e.g. OD 7 -> 64 ms)
    miss  = 135 - 8 * OD   ms   (e.g. OD 7 -> 79 ms)

For v1 the engine handles single-hit don/kat notes (small and big). Drumrolls
and dendens are excluded from the accuracy count — they follow different rules.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum

from .models import HitObject, TaikoBeatmap, TaikoInput, TaikoReplay


class Verdict(Enum):
    GREAT = "great"   # 300 in classic scoring
    OK = "ok"         # 100 in classic scoring
    MISS = "miss"

    @property
    def rank(self) -> int:
        # Lower rank = better verdict. Used for "prefer better verdict" comparison.
        return {"great": 0, "ok": 1, "miss": 2}[self.value]


@dataclass(frozen=True)
class Judgment:
    note: HitObject
    verdict: Verdict
    hit_time_ms: int | None       # None on MISS
    hit_delta_ms: int | None      # signed: positive = late, negative = early
    hit_input: TaikoInput | None  # None on MISS; single-key IntFlag


def _od_lerp(od: float, at_0: float, at_5: float, at_10: float) -> float:
    """Piecewise-linear interpolation used by osu!'s DifficultyRange.

    Linear from OD 0..5 (at_0 -> at_5), then linear from OD 5..10 (at_5 -> at_10).
    Matches ppy/osu's `IBeatmapDifficultyInfo.DifficultyRange`.
    """
    if od > 5.0:
        return at_5 + (at_10 - at_5) * (od - 5.0) / 5.0
    if od < 5.0:
        return at_5 - (at_5 - at_0) * (5.0 - od) / 5.0
    return at_5


@dataclass(frozen=True)
class JudgmentWindows:
    great: float
    ok: float
    miss: float

    @classmethod
    def from_od(cls, od: float) -> "JudgmentWindows":
        # Anchors from ppy/osu TaikoHitWindows.cs at OD 0 / 5 / 10.
        # osu!stable truncates the interpolated windows to integers before
        # comparison, so a delta of exactly N ms with window 30.5 counts as OK
        # (game floors to 30, then |Δ| < 30 → OK). Match that behavior by
        # flooring the windows here rather than in every callsite.
        import math
        return cls(
            great=math.floor(_od_lerp(od, 50.0, 35.0, 20.0)),
            ok=math.floor(_od_lerp(od, 120.0, 80.0, 50.0)),
            miss=math.floor(_od_lerp(od, 135.0, 95.0, 70.0)),
        )


@dataclass(frozen=True)
class JudgedReplay:
    judgments: tuple[Judgment, ...]
    windows: JudgmentWindows

    @property
    def count_great(self) -> int:
        return sum(1 for j in self.judgments if j.verdict is Verdict.GREAT)

    @property
    def count_ok(self) -> int:
        return sum(1 for j in self.judgments if j.verdict is Verdict.OK)

    @property
    def count_miss(self) -> int:
        return sum(1 for j in self.judgments if j.verdict is Verdict.MISS)

    @property
    def accuracy(self) -> float:
        # Standard osu!taiko accuracy: (great + 0.5 * ok) / total
        total = self.count_great + self.count_ok + self.count_miss
        if total == 0:
            return 0.0
        return (self.count_great + 0.5 * self.count_ok) / total


def judge_replay(beatmap: TaikoBeatmap, replay: TaikoReplay) -> JudgedReplay:
    """Pair map notes with replay key-downs and classify each note."""
    windows = JudgmentWindows.from_od(beatmap.difficulty.overall_difficulty)
    hittable = beatmap.hittable()
    events = replay.key_down_events()  # tuple of (time_ms, single-key TaikoInput)

    judgments: list[Judgment] = []
    # A cursor that advances monotonically through the event stream. We never
    # look at an event that's already been consumed by an earlier note.
    cursor = 0
    n_events = len(events)

    for note in hittable:
        note_time = note.time_ms
        note_is_don = note.note_type.is_don
        note_is_kat = note.note_type.is_kat

        # Skip past events that are too early for this note (they were extras
        # between the previous note and this one, or leftovers from misses).
        earliest = note_time - int(round(windows.miss))
        while cursor < n_events and events[cursor][0] < earliest:
            cursor += 1

        if cursor >= n_events:
            judgments.append(Judgment(
                note=note, verdict=Verdict.MISS,
                hit_time_ms=None, hit_delta_ms=None, hit_input=None,
            ))
            continue

        k_time, k_input = events[cursor]
        delta = k_time - note_time

        if delta > windows.miss:
            # The first event chronologically after `earliest` is past the miss
            # window — no press for this note. MISS, cursor stays put.
            judgments.append(Judgment(
                note=note, verdict=Verdict.MISS,
                hit_time_ms=None, hit_delta_ms=None, hit_input=None,
            ))
            continue

        # First press within the window "attempts" this note. In real osu!taiko
        # you can't rewrite an early miss with a later great — the first press
        # commits. If wrong color, it's a bad (MISS) that still consumes the note.
        k_is_don = bool(k_input & TaikoInput.dons())
        k_is_kat = bool(k_input & TaikoInput.kats())
        color_ok = (k_is_don and note_is_don) or (k_is_kat and note_is_kat)

        abs_delta = abs(delta)
        if not color_ok:
            verdict = Verdict.MISS
        elif abs_delta < windows.great:
            verdict = Verdict.GREAT
        elif abs_delta < windows.ok:
            verdict = Verdict.OK
        else:
            verdict = Verdict.MISS

        judgments.append(Judgment(
            note=note, verdict=verdict,
            hit_time_ms=k_time, hit_delta_ms=delta, hit_input=k_input,
        ))
        cursor += 1

        # BIG NOTE geki bonus: if we hit a big note successfully, additional
        # same-color presses within its OK window are the second-hand press
        # (awards geki, doesn't consume another note). Skip them so they don't
        # leak into the next close-by note. Small notes have no geki mechanic —
        # extras cascade into the next note per real game behavior.
        if verdict is not Verdict.MISS and note.note_type.is_big:
            geki_end = note_time + int(round(windows.ok))
            while cursor < n_events:
                next_time, next_input = events[cursor]
                if next_time > geki_end:
                    break
                next_is_don = bool(next_input & TaikoInput.dons())
                if next_is_don == note_is_don:
                    cursor += 1
                else:
                    break

    return JudgedReplay(judgments=tuple(judgments), windows=windows)



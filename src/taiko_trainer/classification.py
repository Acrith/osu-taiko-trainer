"""Per-note failure classification.

For each MISS or OK judgment, assign a primary cause based on the map's local
features at that note plus the judgment itself. Answers the trainer's core
question: "why did I miss (or under-hit) THIS note?"

Causes:
- WRONG_COLOR: hit_input is present but wrong color (a "bad" — hit but wrong key)
- SPEED: local BPM is in the map's high tier and the note is inside a burst
- STAMINA: strain intensity in this window is high vs the map's peak
- TECHNICAL: preceding or following gap is a hard divisor (1/6, 1/3, 1/8, 1/12)
- GIMMICK: local SV multiplier differs significantly from the map's mode
- CONSISTENCY: hit_delta is a >2-sigma outlier from this replay's mean (drift/late-map fatigue)
- UNKNOWN: no signal strong enough to classify

The classifier picks the strongest signal as primary. Multiple contributing
causes may also be listed.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from .features import MapFeatures
from .judgment import JudgedReplay, Judgment, Verdict
from .models import TaikoBeatmap, TaikoInput


class FailureCause(Enum):
    WRONG_COLOR = "wrong_color"        # random misclick, not pattern-related
    PATTERN_PARITY = "pattern_parity"  # KDDK-hostile pattern (mono-run / chunk-misalignment / big-note disruption)
    SPEED = "speed"
    STAMINA = "stamina"
    TECHNICAL = "technical"
    GIMMICK = "gimmick"
    CONSISTENCY = "consistency"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class FailureClassification:
    judgment: Judgment
    primary: FailureCause
    contributing: tuple[FailureCause, ...]  # secondary causes above threshold
    signals: dict[str, float]                # raw signal scores 0..1


# Threshold above which a signal is considered a contributor
_CONTRIB_THRESHOLD = 0.5


def _local_divisor(gap_ms: float, bpm: float) -> str | None:
    """Classify a note-to-note gap into a rhythmic divisor."""
    if gap_ms <= 0 or bpm <= 0:
        return None
    beat_ms = 60000.0 / bpm
    frac = (gap_ms / beat_ms) % 1
    if frac < 0.001 or frac > 0.999:
        return "1/1"
    for name, val in (("1/2", 0.5), ("1/3", 1/3), ("1/4", 0.25),
                       ("1/6", 1/6), ("1/8", 0.125), ("1/12", 1/12), ("1/16", 0.0625)):
        if abs(frac - val) <= 0.06:
            return name
    return "other"


_HARD_DIVISORS = {"1/6", "1/8", "1/3", "1/12"}


def _sustained_hard_score(gap_divisors: list[str | None], note_idx: int) -> float:
    """Score how much this note sits inside a SUSTAINED hard-divisor context.

    Per divisor-semantics memory: a lone 1/6 gap inside a 1/4 stream is a
    speed-adjacent burst, but a run of 3+ consecutive 1/6 gaps is technical
    (rhythmic-read demand). We distinguish the two by looking at the surrounding
    gap divisors: if 2+ of the ±2 surrounding gaps are hard, this is sustained.
    """
    # For a note at index i, adjacent gaps are gap_divisors[i-1] (before) and
    # gap_divisors[i] (after). Widen the window to ±2 for context.
    lo = max(0, note_idx - 2)
    hi = min(len(gap_divisors), note_idx + 2)
    surrounding = gap_divisors[lo:hi]
    hard_count = sum(1 for d in surrounding if d in _HARD_DIVISORS)
    # Score:
    #   0 hard neighbors  → 0.0  (not technical context)
    #   1 hard neighbor   → 0.4  (isolated hard gap — probably speed burst)
    #   2 hard neighbors  → 0.8  (sustained hard rhythm — technical)
    #   3+ hard neighbors → 1.0
    return min(1.0, hard_count * 0.4)


def classify_failures(
    judged: JudgedReplay,
    beatmap: TaikoBeatmap,
    features: MapFeatures,
) -> tuple[FailureClassification, ...]:
    """Classify each MISS and OK judgment by primary failure cause."""
    hittable = beatmap.hittable()
    if not hittable:
        return ()

    map_start = hittable[0].time_ms
    window_ms = features.strain.window_ms

    # Precompute for CONSISTENCY (drift detection): per-replay hit-delta stats
    deltas = [j.hit_delta_ms for j in judged.judgments if j.hit_delta_ms is not None]
    if deltas:
        mean = sum(deltas) / len(deltas)
        var = sum((d - mean) ** 2 for d in deltas) / len(deltas)
        stddev = var ** 0.5 or 1.0
    else:
        mean, stddev = 0.0, 1.0

    # Precompute for GIMMICK: map's mode SV multiplier
    all_svs = [n.sv_multiplier for n in beatmap.hit_objects if n.note_type.is_hit]
    if all_svs:
        sv_median = sorted(all_svs)[len(all_svs) // 2]
    else:
        sv_median = 1.0

    # Precompute for STAMINA: cumulative-fatigue model.
    # For each 20s window, track cumulative strain up to (and including) that
    # window as a fraction of total map strain. A note in a high-intensity
    # window that comes AFTER significant accumulated strain is a fatigue miss;
    # a high-intensity window at the start of the map is just a hard section.
    peak_strain = features.strain.peak or 1.0
    cum_strain: list[float] = []
    running = 0.0
    for v in features.strain.intensities:
        running += v
        cum_strain.append(running)
    total_strain = cum_strain[-1] if cum_strain else 1.0

    # Divisor of each gap. gap_divisors[i] = divisor between hittable[i] and
    # hittable[i+1]. Length is len(hittable)-1.
    gap_divisors: list[str | None] = []
    for i in range(len(hittable) - 1):
        gap_divisors.append(
            _local_divisor(hittable[i + 1].time_ms - hittable[i].time_ms, hittable[i].bpm)
        )

    # Map from note.time_ms to note index for quick lookup during judgment iteration
    time_to_idx = {n.time_ms: i for i, n in enumerate(hittable)}

    parity_scores = features.parity.per_note

    out: list[FailureClassification] = []
    for j in judged.judgments:
        # Only misses get classified — OKs are just slight timing drift and
        # don't reflect a distinct failure cause worth diagnosing.
        if j.verdict is not Verdict.MISS:
            continue

        note = j.note
        note_idx = time_to_idx.get(note.time_ms, -1)

        signals: dict[str, float] = {}

        # PATTERN_PARITY: local KDDK-hostility score at this note.
        parity_here = parity_scores[note_idx] if 0 <= note_idx < len(parity_scores) else 0.0
        signals["pattern_parity"] = parity_here

        # WRONG_COLOR: hit_input present and wrong color
        wrong_color = False
        if j.hit_input is not None:
            k_is_don = bool(j.hit_input & TaikoInput.dons())
            if k_is_don != note.note_type.is_don:
                wrong_color = True
        signals["wrong_color"] = 1.0 if wrong_color else 0.0

        # STAMINA: local strain × how much of the map's total strain has been
        # experienced by now. A high-strain moment early in the map isn't a
        # fatigue miss — the player is fresh. Same moment late in the map is.
        strain_idx = min(len(features.strain.intensities) - 1,
                          max(0, (note.time_ms - map_start) // window_ms))
        strain_here = features.strain.intensities[strain_idx] if features.strain.intensities else 0.0
        strain_norm = strain_here / peak_strain if peak_strain > 0 else 0.0
        fatigue_frac = cum_strain[strain_idx] / total_strain if total_strain > 0 else 0.0
        # Product means both must be present: hard window AND late-map cumulative
        signals["stamina"] = strain_norm * fatigue_frac

        # SPEED: absolute BPM tier — 180 BPM = neutral, 280+ BPM = full speed pressure.
        # Using absolute not relative-to-map because a 170 BPM map with constant BPM
        # shouldn't tag every miss as "speed" just because 170 == map max.
        signals["speed"] = max(0.0, min(1.0, (note.bpm - 180.0) / 100.0))

        # TECHNICAL: SUSTAINED hard-divisor context, not just a lone hard gap.
        # A single 1/6 gap inside a 1/4 stream is a speed-adjacent burst;
        # multiple consecutive 1/6 gaps around this note are technical.
        signals["technical"] = _sustained_hard_score(gap_divisors, note_idx) if note_idx >= 0 else 0.0

        # GIMMICK: SV multiplier at this note deviates significantly from median
        sv_deviation = abs(note.sv_multiplier - sv_median) / max(sv_median, 0.5)
        signals["gimmick"] = min(1.0, sv_deviation)

        # CONSISTENCY: hit delta far from replay mean (only for OK, or MISS with hit_delta)
        if j.hit_delta_ms is not None:
            z = abs(j.hit_delta_ms - mean) / stddev
            signals["consistency"] = min(1.0, z / 3.0)  # 3-sigma = full
        else:
            signals["consistency"] = 0.0

        # Pick primary cause with unified weighted candidates.
        # Skill-gap-style causes (TECHNICAL, PATTERN_PARITY) take priority over
        # STAMINA because "you missed at a hard divisor / hostile pattern" is a
        # more specific diagnosis than "you were tired at that point". Same for
        # GIMMICK. Wrong-color gets its own slot as a default cause when no
        # other context signal fires.
        candidates: dict[FailureCause, float] = {
            FailureCause.TECHNICAL:      signals["technical"] * 1.5,
            FailureCause.PATTERN_PARITY: signals["pattern_parity"] * 1.4,
            FailureCause.GIMMICK:        signals["gimmick"] * 1.3,
            FailureCause.STAMINA:        signals["stamina"] * 1.2,
            FailureCause.SPEED:          signals["speed"] * 1.0,
            FailureCause.CONSISTENCY:    signals["consistency"] * 1.0,
            FailureCause.WRONG_COLOR:    signals["wrong_color"] * 1.0,
        }
        top_cause, top_score = max(candidates.items(), key=lambda kv: kv[1])
        if top_score < 0.4:
            primary = FailureCause.UNKNOWN
        else:
            primary = top_cause

        # Contributors: other signals above threshold
        contributing = tuple(
            FailureCause(k)
            for k, v in signals.items()
            if v >= _CONTRIB_THRESHOLD and FailureCause(k) is not primary
        )

        out.append(FailureClassification(
            judgment=j,
            primary=primary,
            contributing=contributing,
            signals=signals,
        ))

    return tuple(out)


@dataclass(frozen=True)
class FailureSummary:
    total_failures: int          # count of MISS + OK
    by_cause: dict[str, int]     # cause name -> count
    miss_by_cause: dict[str, int]
    ok_by_cause: dict[str, int]


def extract_miss_patterns(
    classifications: tuple[FailureClassification, ...],
    hittable,
) -> list[dict]:
    """For each MISS classification, capture the pattern context needed to
    cluster weaknesses across the player's play history. One record per miss:

        t         note time_ms (for linking back to the replay)
        bpm       local BPM at this note
        color     'D' | 'K'
        size      'sm' | 'big'
        cause     primary FailureCause value (e.g. 'technical')
        prev_div  incoming gap divisor ('1/6', '1/4', 'other', None)
        next_div  outgoing gap divisor
        run_len   length of the same-color mono run this note is in
        run_pos   0-indexed position within that run

    These get stored per-replay as JSON and re-aggregated at report time to
    surface the specific pattern signatures a player struggles with."""
    if not classifications or not hittable:
        return []

    time_to_idx = {n.time_ms: i for i, n in enumerate(hittable)}
    # Precompute gap divisors + run info per note.
    gap_divisors: list[str | None] = []
    for i in range(len(hittable) - 1):
        gap_divisors.append(
            _local_divisor(hittable[i + 1].time_ms - hittable[i].time_ms, hittable[i].bpm)
        )
    run_len: list[int] = [0] * len(hittable)
    run_pos: list[int] = [0] * len(hittable)
    i = 0
    while i < len(hittable):
        j = i
        while j + 1 < len(hittable) and hittable[j + 1].note_type.is_don == hittable[i].note_type.is_don:
            j += 1
        length = j - i + 1
        for k, idx in enumerate(range(i, j + 1)):
            run_len[idx] = length
            run_pos[idx] = k
        i = j + 1

    def _color_ctx(idx: int, window: int = 2) -> str:
        """5-note color pattern centered on idx. The missed note is marked
        with a lowercase letter (k/d) so the exact position stands out from
        the surrounding uppercase (K/D) context."""
        parts: list[str] = []
        for k in range(idx - window, idx + window + 1):
            if k < 0 or k >= len(hittable):
                parts.append("·")  # edge padding
            else:
                is_don = hittable[k].note_type.is_don
                ch = "D" if is_don else "K"
                if k == idx:
                    ch = ch.lower()  # miss marker
                parts.append(ch)
        return "".join(parts)

    def _rhythm_ctx(idx: int) -> str:
        """Short label for the local divisor mix around the miss. Uses ±2
        gap divisors: reports 'pure 1/6' if all match, 'mixed 1/4+1/6' if
        the miss sits at a boundary."""
        divs: list[str] = []
        for gi in range(max(0, idx - 2), min(len(gap_divisors), idx + 2)):
            d = gap_divisors[gi]
            if d and d != "1/1" and d != "other":
                divs.append(d)
        if not divs:
            return "sparse"
        distinct = list({d for d in divs})
        if len(distinct) == 1:
            return f"pure {distinct[0]}"
        # Two most common in the window
        distinct.sort(key=lambda d: -divs.count(d))
        return f"mixed {distinct[0]}+{distinct[1]}" if len(distinct) >= 2 else f"pure {distinct[0]}"

    records: list[dict] = []
    for cls in classifications:
        if cls.judgment.verdict is not Verdict.MISS:
            continue
        note = cls.judgment.note
        idx = time_to_idx.get(note.time_ms, -1)
        if idx < 0:
            continue
        prev_div = gap_divisors[idx - 1] if idx > 0 else None
        next_div = gap_divisors[idx] if idx < len(gap_divisors) else None
        records.append({
            "t": note.time_ms,
            "bpm": round(note.bpm, 1),
            "color": "D" if note.note_type.is_don else "K",
            "size": "big" if note.note_type.is_big else "sm",
            "cause": cls.primary.value,
            "prev_div": prev_div,
            "next_div": next_div,
            "run_len": run_len[idx],
            "run_pos": run_pos[idx],
            "color_ctx": _color_ctx(idx),      # e.g. "KDdKD" — lowercase = the miss
            "rhythm_ctx": _rhythm_ctx(idx),    # e.g. "pure 1/6" or "mixed 1/4+1/6"
        })
    return records


def summarize_failures(classifications: tuple[FailureClassification, ...]) -> FailureSummary:
    by_cause: dict[str, int] = {c.value: 0 for c in FailureCause}
    miss_by: dict[str, int] = {c.value: 0 for c in FailureCause}
    ok_by: dict[str, int] = {c.value: 0 for c in FailureCause}
    for cls in classifications:
        by_cause[cls.primary.value] += 1
        if cls.judgment.verdict is Verdict.MISS:
            miss_by[cls.primary.value] += 1
        elif cls.judgment.verdict is Verdict.OK:
            ok_by[cls.primary.value] += 1
    return FailureSummary(
        total_failures=len(classifications),
        by_cause=by_cause,
        miss_by_cause=miss_by,
        ok_by_cause=ok_by,
    )

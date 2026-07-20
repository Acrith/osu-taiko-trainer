"""Feature extraction from parsed beatmaps.

These are descriptive stats — not yet skill scores. The goal is that when we
print them side-by-side across our 5 reference maps (one per skill dimension),
the numbers visibly separate the categories the way the labels claim.

Everything here is pure computation from a TaikoBeatmap.
"""
from __future__ import annotations

import math
from collections import Counter
from dataclasses import asdict, dataclass

from .kddk_patterns import StreamProfile, stream_profile
from .models import HitObject, NoteType, TaikoBeatmap
from .parity import ParityProfile, compute_parity


# --- utilities ---------------------------------------------------------------

def _mean(values):
    values = list(values)
    return sum(values) / len(values) if values else 0.0


def _stddev(values):
    values = list(values)
    if len(values) < 2:
        return 0.0
    m = sum(values) / len(values)
    return math.sqrt(sum((v - m) ** 2 for v in values) / len(values))


def _percentile(values, p):
    values = sorted(values)
    if not values:
        return 0.0
    if len(values) == 1:
        return values[0]
    k = (len(values) - 1) * p
    lo = int(math.floor(k))
    hi = int(math.ceil(k))
    if lo == hi:
        return values[lo]
    return values[lo] + (values[hi] - values[lo]) * (k - lo)


def _entropy_bits(counts) -> float:
    total = sum(counts)
    if total <= 0:
        return 0.0
    h = 0.0
    for c in counts:
        if c <= 0:
            continue
        p = c / total
        h -= p * math.log2(p)
    return h


# --- density -----------------------------------------------------------------

@dataclass(frozen=True)
class DensityProfile:
    bucket_ms: int
    duration_s: float
    avg_nps: float           # over the active span (first hit -> last hit)
    peak_nps_200ms: float    # max notes/sec in any 200ms window (instantaneous burst)
    peak_nps: float          # max notes in any 1s bucket
    p95_nps: float
    peak_nps_5s: float       # sustained: max notes in any 5s window
    high_density_ratio: float           # fraction of 1s buckets with NPS >= 0.75 * peak
    longest_sustained_high_s: float     # longest run of consecutive high-density buckets
    section_nps_stddev_30s: float       # variance of NPS across 30s sections


def density_profile(hittable: tuple[HitObject, ...]) -> DensityProfile:
    if not hittable:
        return DensityProfile(1000, 0, 0, 0, 0, 0, 0, 0, 0, 0)

    times = [n.time_ms for n in hittable]
    start = times[0]
    end = times[-1]
    duration_ms = max(1, end - start)
    duration_s = duration_ms / 1000

    # 200ms rolling-window peak — catches instantaneous bursts that 1s buckets
    # miss. A 200ms window is roughly the shortest span at which a human perceives
    # "many notes at once" for short-burst speed evaluation.
    lo = 0
    peak_notes_200ms = 0
    for hi in range(len(times)):
        while times[hi] - times[lo] > 200:
            lo += 1
        peak_notes_200ms = max(peak_notes_200ms, hi - lo + 1)
    peak_nps_200ms = peak_notes_200ms * 5.0  # 200ms -> per second

    bucket_ms = 1000
    # Bucket-based NPS is simple and stable; it double-counts nothing.
    n_buckets = duration_ms // bucket_ms + 1
    buckets = [0] * n_buckets
    for t in times:
        idx = (t - start) // bucket_ms
        if 0 <= idx < n_buckets:
            buckets[idx] += 1

    avg_nps = _mean(buckets)
    peak_nps = float(max(buckets)) if buckets else 0.0
    p95_nps = _percentile(buckets, 0.95)

    # Sustained peak: 5s rolling sum over the same 1s buckets.
    peak_nps_5s = 0.0
    if len(buckets) >= 5:
        window = sum(buckets[:5])
        peak_nps_5s = window / 5.0
        for i in range(5, len(buckets)):
            window += buckets[i] - buckets[i - 5]
            peak_nps_5s = max(peak_nps_5s, window / 5.0)
    else:
        peak_nps_5s = avg_nps

    threshold = 0.75 * peak_nps
    high_flags = [b >= threshold and b > 0 for b in buckets]
    high_density_ratio = sum(high_flags) / len(high_flags) if high_flags else 0.0
    longest_run = 0
    run = 0
    for f in high_flags:
        run = run + 1 if f else 0
        longest_run = max(longest_run, run)

    # Section variance: bucket into 30s sections, take stddev of per-section NPS.
    sec_len = 30
    if duration_s <= sec_len:
        section_stddev = 0.0
    else:
        sec_counts = []
        step = sec_len  # 30s buckets, non-overlapping
        for start_bucket in range(0, len(buckets), step):
            slice_ = buckets[start_bucket:start_bucket + step]
            if len(slice_) >= step // 3:  # ignore trailing partial section
                sec_counts.append(sum(slice_) / len(slice_))
        section_stddev = _stddev(sec_counts)

    return DensityProfile(
        bucket_ms=bucket_ms,
        duration_s=duration_s,
        avg_nps=avg_nps,
        peak_nps_200ms=peak_nps_200ms,
        peak_nps=peak_nps,
        p95_nps=p95_nps,
        peak_nps_5s=peak_nps_5s,
        high_density_ratio=high_density_ratio,
        longest_sustained_high_s=float(longest_run),
        section_nps_stddev_30s=section_stddev,
    )


# --- SV / BPM movement -------------------------------------------------------

@dataclass(frozen=True)
class MovementProfile:
    distinct_bpm_count: int
    bpm_min: float
    bpm_max: float
    bpm_change_events: int      # number of adjacent-note transitions that change BPM
    # Derived from the actual inter-note gaps in dense sections, independent of
    # what the .osu timing points declare. Gimmick maps that use pathological
    # BPMs (e.g. 727 BPM as a mapper trick to sync with storyboard) get sanity-
    # checked by this value; it represents "the tempo at which the fastest
    # sustained rhythm would be 1/4 notes" — i.e. what the PLAYER feels.
    bpm_effective: float
    distinct_sv_count: int
    sv_min: float
    sv_max: float
    sv_range: float
    sv_stddev: float
    sv_change_events: int       # number of adjacent-note transitions that change SV
    sv_changes_per_minute: float


def _effective_bpm_from_gaps(hit_objects: tuple[HitObject, ...]) -> float:
    """The equivalent 1/4-note tempo derived from the note stream itself.

    Collects consecutive inter-note gaps that fall inside a burst (20-500 ms —
    excludes rest periods and pathologically-tight artifacts), takes the median
    of the fastest quartile, and reads it as a 1/4-note interval. Result is
    the BPM that makes those gaps play as quarter notes.

    Anchors against the note stream, not the .osu timing points, so a map with
    declared BPM 727 but actual note density of ~20 nps comes out around 300.
    Returns 0 for maps too short to derive meaningfully."""
    if len(hit_objects) < 10:
        return 0.0
    gaps = []
    for a, b in zip(hit_objects, hit_objects[1:]):
        g = b.time_ms - a.time_ms
        if 20 <= g <= 500:
            gaps.append(g)
    if len(gaps) < 5:
        return 0.0
    gaps.sort()
    top_q = gaps[:max(3, len(gaps) // 4)]
    median_top = top_q[len(top_q) // 2]
    return 15000.0 / median_top


def movement_profile(hit_objects: tuple[HitObject, ...], duration_s: float) -> MovementProfile:
    if not hit_objects:
        return MovementProfile(0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0)

    bpms = [n.bpm for n in hit_objects]
    svs = [n.sv_multiplier for n in hit_objects]

    def _count_transitions(values):
        return sum(1 for a, b in zip(values, values[1:]) if not math.isclose(a, b, rel_tol=1e-6))

    minutes = max(duration_s / 60.0, 1e-9)

    return MovementProfile(
        distinct_bpm_count=len({round(b, 3) for b in bpms}),
        bpm_min=min(bpms),
        bpm_max=max(bpms),
        bpm_change_events=_count_transitions(bpms),
        bpm_effective=_effective_bpm_from_gaps(hit_objects),
        distinct_sv_count=len({round(s, 3) for s in svs}),
        sv_min=min(svs),
        sv_max=max(svs),
        sv_range=max(svs) - min(svs),
        sv_stddev=_stddev(svs),
        sv_change_events=_count_transitions(svs),
        sv_changes_per_minute=_count_transitions(svs) / minutes,
    )


# --- color pattern (don vs kat) ---------------------------------------------

@dataclass(frozen=True)
class ColorProfile:
    hit_count: int
    don_ratio: float
    color_change_ratio: float          # P(next note is different color)
    run_length_mean: float             # mean length of same-color runs
    run_length_max: int
    run_length_entropy_bits: float
    mono_stream_ratio: float           # fraction of notes inside a same-color run of length >= 5


def color_profile(hittable: tuple[HitObject, ...]) -> ColorProfile:
    if not hittable:
        return ColorProfile(0, 0, 0, 0, 0, 0, 0)

    colors = ["D" if n.note_type.is_don else "K" for n in hittable]
    don_ratio = colors.count("D") / len(colors)

    change_count = sum(1 for a, b in zip(colors, colors[1:]) if a != b)
    change_ratio = change_count / max(1, len(colors) - 1)

    # Run-length encoding.
    runs: list[int] = []
    cur = 1
    for a, b in zip(colors, colors[1:]):
        if a == b:
            cur += 1
        else:
            runs.append(cur)
            cur = 1
    runs.append(cur)

    run_lengths = Counter(runs)
    entropy = _entropy_bits(run_lengths.values())
    long_run_notes = sum(r for r in runs if r >= 5)
    mono_ratio = long_run_notes / len(colors)

    return ColorProfile(
        hit_count=len(colors),
        don_ratio=don_ratio,
        color_change_ratio=change_ratio,
        run_length_mean=_mean(runs),
        run_length_max=max(runs) if runs else 0,
        run_length_entropy_bits=entropy,
        mono_stream_ratio=mono_ratio,
    )


# --- rhythm divisors --------------------------------------------------------

# Recognized simple divisors (fractions of a beat). Notes whose gap is close to
# one of these are "on-grid" for that divisor; anything else is bucketed to "other".
_DIVISORS = {
    "1/1": 1.0,
    "1/2": 0.5,
    "1/3": 1 / 3,
    "1/4": 0.25,
    "1/6": 1 / 6,
    "1/8": 0.125,
    "1/12": 1 / 12,
    "1/16": 0.0625,
}
_DIVISOR_TOLERANCE = 0.06  # relative tolerance in "beats" units


@dataclass(frozen=True)
class RhythmProfile:
    dominant_divisor: str
    dominant_divisor_share: float
    divisor_share: dict[str, float]   # includes "other"
    divisor_entropy_bits: float
    off_grid_ratio: float             # share of gaps not matching any recognized divisor
    ioi_median_ms: float              # median inter-onset interval (note-to-note gap)
    ioi_cov: float                    # coefficient of variation of gaps: stddev / mean
                                      # low ~ steady rhythm (locks into L-R-L-R for KDDK),
                                      # high ~ constantly-shifting spacing (technical/read-heavy)


def rhythm_profile(hittable: tuple[HitObject, ...]) -> RhythmProfile:
    if len(hittable) < 2:
        return RhythmProfile("n/a", 0, {}, 0, 0, 0, 0)

    counts: Counter = Counter()
    gaps_ms: list[int] = []
    for a, b in zip(hittable, hittable[1:]):
        dt_ms = b.time_ms - a.time_ms
        if dt_ms <= 0:
            continue
        gaps_ms.append(dt_ms)
        beat_ms = 60000.0 / a.bpm if a.bpm > 0 else 0.0
        if beat_ms <= 0:
            counts["other"] += 1
            continue
        beats = dt_ms / beat_ms
        # For beats > 1, reduce to the fractional part relative to a whole beat.
        # (A whole-beat gap is 1/1 — we treat 2, 3, etc. as 1/1 too, since we
        # only care about *within-beat* rhythm complexity.)
        frac = beats - math.floor(beats)
        if frac < 1e-3:
            counts["1/1"] += 1
            continue
        matched = None
        for name, value in _DIVISORS.items():
            # Compare in beats-units: if frac is within tolerance of `value`, snap to it.
            if abs(frac - value) <= _DIVISOR_TOLERANCE:
                matched = name
                break
        counts[matched or "other"] += 1

    total = sum(counts.values())
    share = {k: v / total for k, v in counts.items()}
    entropy = _entropy_bits(counts.values())

    dominant = max(share.items(), key=lambda kv: kv[1])
    off_grid = share.get("other", 0.0)

    ioi_median = _percentile(gaps_ms, 0.5) if gaps_ms else 0.0
    ioi_mean = _mean(gaps_ms) if gaps_ms else 0.0
    ioi_stddev = _stddev(gaps_ms) if gaps_ms else 0.0
    ioi_cov = (ioi_stddev / ioi_mean) if ioi_mean > 0 else 0.0
    return RhythmProfile(
        dominant_divisor=dominant[0],
        dominant_divisor_share=dominant[1],
        divisor_share=share,
        divisor_entropy_bits=entropy,
        off_grid_ratio=off_grid,
        ioi_median_ms=ioi_median,
        ioi_cov=ioi_cov,
    )


# --- bursts -----------------------------------------------------------------

@dataclass(frozen=True)
class BurstProfile:
    # A "burst" = run of >=3 consecutive notes whose gaps are <= 1/4-beat at the local BPM.
    # Length is note count; intensity is length * local_bpm / 60 (rough notes-per-second peak).
    burst_count: int
    bursts_per_minute: float
    mean_length: float
    max_length: int
    length_3_ratio: float           # share of hittable notes inside bursts of length 3-6
    length_7plus_ratio: float       # share of hittable notes inside bursts of length >=7
    peak_intensity: float           # length * bpm/60 for the burst that scores highest
    mean_intensity_top10: float     # mean of the top-decile bursts' intensity (or all if fewer than 10)


def burst_profile(hittable: tuple[HitObject, ...]) -> BurstProfile:
    if len(hittable) < 3:
        return BurstProfile(0, 0, 0, 0, 0, 0, 0, 0)

    # For each note (except last) compute whether the gap to next is "burst-tight":
    # gap <= 1/4-beat + 15% tolerance. That covers 1/4 and 1/6 (which is denser -> also flagged),
    # but NOT 1/3 or 1/2.
    burst_flags: list[bool] = []
    beat_lengths: list[float] = []
    for a, b in zip(hittable, hittable[1:]):
        beat_ms = 60000.0 / a.bpm if a.bpm > 0 else 0.0
        beat_lengths.append(beat_ms)
        threshold = beat_ms * 0.25 * 1.15 if beat_ms > 0 else 0
        burst_flags.append(threshold > 0 and (b.time_ms - a.time_ms) <= threshold)

    # Extract runs of >=2 consecutive "tight" gaps (which means >=3 notes in a burst).
    bursts: list[tuple[int, int, float]] = []  # (start_idx, length_notes, local_bpm)
    i = 0
    while i < len(burst_flags):
        if burst_flags[i]:
            start = i
            while i < len(burst_flags) and burst_flags[i]:
                i += 1
            length_notes = (i - start) + 1  # gaps + 1 = notes
            if length_notes >= 3:
                local_bpm = hittable[start].bpm
                bursts.append((start, length_notes, local_bpm))
        else:
            i += 1

    if not bursts:
        return BurstProfile(0, 0, 0, 0, 0, 0, 0, 0)

    lengths = [b[1] for b in bursts]
    intensities = [b[1] * (b[2] / 60.0) for b in bursts]

    notes_in_len3_6 = sum(n for n in lengths if 3 <= n <= 6)
    notes_in_len7p = sum(n for n in lengths if n >= 7)
    total_hittable = len(hittable)

    duration_min = max((hittable[-1].time_ms - hittable[0].time_ms) / 60000.0, 1e-9)

    top_intensities = sorted(intensities, reverse=True)[:max(1, len(intensities) // 10)]

    return BurstProfile(
        burst_count=len(bursts),
        bursts_per_minute=len(bursts) / duration_min,
        mean_length=_mean(lengths),
        max_length=max(lengths),
        length_3_ratio=notes_in_len3_6 / total_hittable,
        length_7plus_ratio=notes_in_len7p / total_hittable,
        peak_intensity=max(intensities),
        mean_intensity_top10=_mean(top_intensities),
    )


# --- divisor transitions ----------------------------------------------------

def _divisor_of_gap(dt_ms: int, bpm: float) -> str:
    beat_ms = 60000.0 / bpm if bpm > 0 else 0.0
    if beat_ms <= 0 or dt_ms <= 0:
        return "other"
    beats = dt_ms / beat_ms
    frac = beats - math.floor(beats)
    if frac < 1e-3 or beats >= 1.0 and frac < 1e-3:
        # Whole beats -> treat as 1/1 for transition-purpose.
        return "1/1"
    for name, value in _DIVISORS.items():
        if abs(frac - value) <= _DIVISOR_TOLERANCE:
            return name
    return "other"


@dataclass(frozen=True)
class TransitionProfile:
    total_transitions: int              # consecutive-gap pairs where the divisor label changes
    transitions_per_minute: float
    quarter_sixth_transitions: int      # 1/4 <-> 1/6 specifically (the "speed catch-up" pattern)
    quarter_third_transitions: int      # 1/4 <-> 1/3 (technical shift)


def transition_profile(hittable: tuple[HitObject, ...], duration_s: float) -> TransitionProfile:
    if len(hittable) < 3:
        return TransitionProfile(0, 0, 0, 0)

    divisors: list[str] = []
    for a, b in zip(hittable, hittable[1:]):
        divisors.append(_divisor_of_gap(b.time_ms - a.time_ms, a.bpm))

    total = 0
    q6 = 0
    q3 = 0
    for a, b in zip(divisors, divisors[1:]):
        if a != b:
            total += 1
            pair = frozenset((a, b))
            if pair == frozenset(("1/4", "1/6")):
                q6 += 1
            elif pair == frozenset(("1/4", "1/3")):
                q3 += 1

    minutes = max(duration_s / 60.0, 1e-9)
    return TransitionProfile(
        total_transitions=total,
        transitions_per_minute=total / minutes,
        quarter_sixth_transitions=q6,
        quarter_third_transitions=q3,
    )


# --- density trajectory (does the map get harder over time?) ---------------

@dataclass(frozen=True)
class TrajectoryProfile:
    section_count: int
    first_third_avg_nps: float
    last_third_avg_nps: float
    escalation_ratio: float    # last_third_avg / first_third_avg -- > 1 means it ramps up
    slope_nps_per_min: float   # linear-regression slope of section NPS vs section start time


def trajectory_profile(hittable: tuple[HitObject, ...]) -> TrajectoryProfile:
    if len(hittable) < 10:
        return TrajectoryProfile(0, 0, 0, 1.0, 0)

    start_ms = hittable[0].time_ms
    end_ms = hittable[-1].time_ms
    total_ms = end_ms - start_ms
    if total_ms <= 0:
        return TrajectoryProfile(0, 0, 0, 1.0, 0)

    section_ms = 15_000  # 15s sections; balances resolution vs noise
    section_count = max(3, total_ms // section_ms + 1)
    section_ms = total_ms // section_count if section_count > 0 else section_ms
    if section_ms <= 0:
        return TrajectoryProfile(0, 0, 0, 1.0, 0)

    sections = [0] * section_count
    for n in hittable:
        idx = min(section_count - 1, (n.time_ms - start_ms) // section_ms)
        sections[idx] += 1

    # Per-section NPS = count / (section_ms / 1000).
    section_s = section_ms / 1000.0
    per_section_nps = [c / section_s for c in sections]

    third = max(1, section_count // 3)
    first_third = _mean(per_section_nps[:third])
    last_third = _mean(per_section_nps[-third:])
    escalation = last_third / first_third if first_third > 0 else 1.0

    # Slope via simple least squares (x = section midpoint in minutes, y = NPS).
    xs = [((i + 0.5) * section_ms) / 60000.0 for i in range(section_count)]
    ys = per_section_nps
    x_mean = _mean(xs)
    y_mean = _mean(ys)
    num = sum((x - x_mean) * (y - y_mean) for x, y in zip(xs, ys))
    den = sum((x - x_mean) ** 2 for x in xs)
    slope = num / den if den > 0 else 0.0

    return TrajectoryProfile(
        section_count=section_count,
        first_third_avg_nps=first_third,
        last_third_avg_nps=last_third,
        escalation_ratio=escalation,
        slope_nps_per_min=slope,
    )


# --- stamina: fixed-duration windowed strain ------------------------------

@dataclass(frozen=True)
class StrainProfile:
    """Per-window stamina strain, on fixed 20-second time intervals.

    The map is divided into 20s windows; each window gets an intensity value
    representing how draining that stretch is (density above a floor, weighted
    by BPM and burst structure). Long maps naturally get more windows to
    contribute — duration is not a raw punishment or reward, it's cumulative
    through the number of windows that actually drain.

    This structure is deliberately designed so a downstream player-HP model
    can consume the per-window intensities directly: start HP=100, drain
    window by window, recover during low-intensity windows. Replay analysis
    later aligns hit performance to each window.

    See memory/feedback_stamina_model.md for the model.
    """
    window_ms: int
    intensities: tuple[float, ...]   # length == number of 20s windows
    total: float                     # sum of window intensities (rewards duration)
    peak: float                      # max window intensity (one-spike-wins)
    top3_avg: float                  # avg of top-3 windows
    weighted_sum: float              # sorted desc, weight 0.85^rank, sum — top windows dominate
    fatiguing_windows: int           # count of windows with intensity above 0.6*peak


# Anchors tuned against 6 reference maps so that Fool > (Sonatina, Vicious) >
# (Cyberspace, Dynasty, Drop) — matching user's calibration.
_STAMINA_WINDOW_MS = 20_000
_STAMINA_NPS_FLOOR = 5.0        # below this density in a window, no drain
_STAMINA_BPM_ANCHOR = 180.0     # per-hit cost = 1.0 at this BPM
_STAMINA_BPM_EXP = 1.0          # BPM exponent — slight super-linearity would be > 1
_STAMINA_BURST_WEIGHT = 0.6     # extra intensity from long-burst notes in window
_STAMINA_MONO_WEIGHT = 0.15     # extra intensity from mono-run notes in window
_STAMINA_MONO_MIN_LEN = 3       # minimum run length to count as mono
_STAMINA_WEIGHTED_ALPHA = 0.85  # weight-decay for top-K aggregation


def strain_profile(hittable: tuple[HitObject, ...]) -> StrainProfile:
    if len(hittable) < 2:
        return StrainProfile(_STAMINA_WINDOW_MS, (), 0.0, 0.0, 0.0, 0.0, 0)

    start = hittable[0].time_ms
    end = hittable[-1].time_ms
    n_windows = max(1, math.ceil((end - start + 1) / _STAMINA_WINDOW_MS))

    # Bucket notes into windows.
    window_notes: list[list[HitObject]] = [[] for _ in range(n_windows)]
    for n in hittable:
        w = min(n_windows - 1, (n.time_ms - start) // _STAMINA_WINDOW_MS)
        window_notes[w].append(n)

    intensities: list[float] = []
    for w_notes in window_notes:
        if len(w_notes) < 2:
            intensities.append(0.0)
            continue

        # If the notes fill less than half the window (typically a trailing
        # cluster at the end of the map), use the full window duration so a
        # tiny end-of-map burst doesn't get counted as a 20-second-worth drain.
        # Otherwise use the actual span so mid-map windows aren't diluted.
        note_span_ms = w_notes[-1].time_ms - w_notes[0].time_ms + 1
        if note_span_ms < _STAMINA_WINDOW_MS * 0.5:
            w_dur_ms = _STAMINA_WINDOW_MS
        else:
            w_dur_ms = min(_STAMINA_WINDOW_MS, note_span_ms)
        density = len(w_notes) / (w_dur_ms / 1000.0)
        # No drain below the density floor: a window with 3 NPS doesn't tax stamina
        active_density = max(0.0, density - _STAMINA_NPS_FLOOR)
        if active_density <= 0:
            intensities.append(0.0)
            continue

        avg_bpm = sum(n.bpm for n in w_notes) / len(w_notes)
        bpm_factor = (avg_bpm / _STAMINA_BPM_ANCHOR) ** _STAMINA_BPM_EXP

        # Burst structure inside this window: count notes that sit inside a
        # 3+-length run of 1/4-or-tighter gaps.
        run_length = 1
        burst_notes = 0
        mono_notes = 0
        last_don = w_notes[0].note_type.is_don
        mono_run = 1
        for a, b in zip(w_notes, w_notes[1:]):
            gap_ms = b.time_ms - a.time_ms
            if gap_ms <= 0:
                continue
            beat_ms = 60000.0 / a.bpm if a.bpm > 0 else 500.0
            if gap_ms <= beat_ms * 0.25 * 1.15:
                run_length += 1
                if run_length >= 3:
                    burst_notes += 1
            else:
                run_length = 1
            if b.note_type.is_don == last_don:
                mono_run += 1
                if mono_run >= _STAMINA_MONO_MIN_LEN and run_length >= 3:
                    mono_notes += 1
            else:
                mono_run = 1
            last_don = b.note_type.is_don

        n_hits = len(w_notes)
        burst_ratio = burst_notes / n_hits
        mono_ratio = mono_notes / n_hits

        # Window intensity: active density × BPM factor × burst/mono weighting.
        intensity = active_density * bpm_factor * (
            1.0 + _STAMINA_BURST_WEIGHT * burst_ratio + _STAMINA_MONO_WEIGHT * mono_ratio
        )
        intensities.append(intensity)

    intensities_t = tuple(intensities)
    total = sum(intensities_t)
    peak = max(intensities_t) if intensities_t else 0.0
    sorted_desc = sorted(intensities_t, reverse=True)
    top3_avg = sum(sorted_desc[:3]) / min(3, len(sorted_desc)) if sorted_desc else 0.0
    weighted_sum = sum(v * (_STAMINA_WEIGHTED_ALPHA ** i) for i, v in enumerate(sorted_desc))
    threshold = 0.6 * peak
    fatiguing = sum(1 for x in intensities_t if x > threshold) if peak > 0 else 0

    return StrainProfile(
        window_ms=_STAMINA_WINDOW_MS,
        intensities=intensities_t,
        total=total,
        peak=peak,
        top3_avg=top3_avg,
        weighted_sum=weighted_sum,
        fatiguing_windows=fatiguing,
    )


# --- segmented density (map split into N chunks) --------------------------

@dataclass(frozen=True)
class SegmentProfile:
    n_segments: int
    segment_avg_nps: tuple[float, ...]   # length == n_segments; avg NPS in each chunk
    segment_peak_nps: tuple[float, ...]  # peak 1s NPS in each chunk
    # roll-ups useful for scoring:
    top_segment_avg_nps: float          # densest chunk's avg NPS
    top3_segments_avg_nps: float        # average of the top-3 densest chunks
    density_span: float                 # top - bottom (max variety across map)
    dense_segment_ratio: float          # share of segments with avg NPS >= 0.75 * top
    peak_position: float                # 0..1 fraction where the densest chunk sits
                                         # (0 = start, 1 = end); tells you WHERE the wall is
    monotone_rise: bool                 # True if avg NPS never drops between consecutive chunks


def segment_profile(hittable: tuple[HitObject, ...], n_segments: int = 10) -> SegmentProfile:
    if len(hittable) < n_segments:
        empty = tuple([0.0] * n_segments)
        return SegmentProfile(n_segments, empty, empty, 0, 0, 0, 0, 0.5, False)

    start = hittable[0].time_ms
    end = hittable[-1].time_ms
    span_ms = max(1, end - start)
    seg_ms = span_ms / n_segments

    # Bucket every note into a segment index.
    seg_counts = [0] * n_segments
    # For peak per segment we further bucket by 1s inside each segment.
    per_second: list[dict[int, int]] = [dict() for _ in range(n_segments)]
    for n in hittable:
        rel = n.time_ms - start
        idx = min(n_segments - 1, int(rel / seg_ms))
        seg_counts[idx] += 1
        sec = int(rel / 1000)
        per_second[idx][sec] = per_second[idx].get(sec, 0) + 1

    seg_duration_s = seg_ms / 1000.0
    avg = tuple(c / seg_duration_s for c in seg_counts)
    peak = tuple(float(max(counts.values())) if counts else 0.0 for counts in per_second)

    top = max(avg)
    bottom = min(avg)
    top3 = sum(sorted(avg, reverse=True)[:3]) / 3.0
    dense = sum(1 for a in avg if a >= 0.75 * top) / n_segments if top > 0 else 0.0
    peak_idx = max(range(n_segments), key=lambda i: avg[i])
    peak_position = (peak_idx + 0.5) / n_segments

    monotone = all(avg[i] <= avg[i + 1] for i in range(n_segments - 1))

    return SegmentProfile(
        n_segments=n_segments,
        segment_avg_nps=avg,
        segment_peak_nps=peak,
        top_segment_avg_nps=top,
        top3_segments_avg_nps=top3,
        density_span=top - bottom,
        dense_segment_ratio=dense,
        peak_position=peak_position,
        monotone_rise=monotone,
    )


# --- gimmick overlap: SV depression during dense stretches -----------------

@dataclass(frozen=True)
class GimmickProfile:
    low_sv_share: float             # fraction of hittable notes with SV multiplier < 0.75
    high_sv_share: float            # fraction with SV multiplier > 1.5
    unreadable_ratio: float         # fraction of notes with low SV AND local density above avg NPS
    sv_bpm_score: float             # 0-100ish composite: SV movement * (1 - normalized BPM), so
                                    # heavy SV at low BPM (canonical gimmick) scores highest


def gimmick_profile(hittable: tuple[HitObject, ...], avg_nps: float, movement: MovementProfile) -> GimmickProfile:
    if not hittable:
        return GimmickProfile(0, 0, 0, 0)

    low_sv = 0
    high_sv = 0
    unreadable = 0

    # For "local density above avg NPS", we approximate by using local NPS
    # in a ±500ms window per note (built once as a rolling count).
    times = [n.time_ms for n in hittable]
    n = len(times)
    window_ms = 500
    local_counts = [0] * n
    j_lo = 0
    j_hi = 0
    for i in range(n):
        while j_lo < n and times[j_lo] < times[i] - window_ms:
            j_lo += 1
        while j_hi < n and times[j_hi] <= times[i] + window_ms:
            j_hi += 1
        local_counts[i] = j_hi - j_lo  # notes in ~1s window centred on i

    dense_threshold = max(1, int(avg_nps + 1))  # notes-per-second baseline (approx)

    for note, local in zip(hittable, local_counts):
        if note.sv_multiplier < 0.75:
            low_sv += 1
        if note.sv_multiplier > 1.5:
            high_sv += 1
        if note.sv_multiplier < 0.75 and local >= dense_threshold:
            unreadable += 1

    # SV-BPM score: SV changes/min * SV stddev * bpm-dampening.
    # Gimmick maps are usually mid/low BPM; scale down heavily above 180 BPM.
    bpm = movement.bpm_max
    bpm_dampen = max(0.0, 1.0 - max(0.0, (bpm - 180) / 120.0))
    sv_bpm = movement.sv_changes_per_minute * (movement.sv_stddev + 0.1) * bpm_dampen

    return GimmickProfile(
        low_sv_share=low_sv / n,
        high_sv_share=high_sv / n,
        unreadable_ratio=unreadable / n,
        sv_bpm_score=sv_bpm,
    )


# --- reading: base scroll velocity (how fast is the map visually?) ---------

@dataclass(frozen=True)
class ReadingProfile:
    """Effective scroll velocity is what a player's eyes and hands have to
    react to. It's roughly `BPM × SV_multiplier` on the note — a fast SV at
    low BPM feels similar to a slow SV at high BPM. This is DISTINCT from
    gimmick: gimmick captures chaotic/unpredictable SV, reading captures a
    consistent-but-fast baseline scroll that just needs faster reaction.

    velocity_dense_p50  MEDIAN scroll velocity in dense stretches (top ~20%
                        NPS windows) — the *sustained* scroll the player has
                        to keep up with. Uniform-SV maps have p50=p95; heavy
                        SV-variance maps have p50 well below p95, and it's
                        the p50 that reflects reading load (the p95 spikes
                        are gimmick moments, not reading pressure).
    sustained_share     fraction of hittable notes where scroll velocity
                        SUSTAINS above 280 for a ≥400ms window. A single
                        note at SV=2.5 doesn't count, but a whole section
                        at HR-level scroll does.
    velocity_p95        overall 95th-percentile scroll velocity (unfiltered).
                        Kept for reference / diagnostics, not scored.
    """
    velocity_dense_p50: float
    sustained_share: float
    velocity_p95: float


def reading_profile(hittable: tuple[HitObject, ...]) -> ReadingProfile:
    if not hittable:
        return ReadingProfile(0.0, 0.0, 0.0)

    # Per-note scroll velocity, clamped so SV=0 (invisible/gimmick) doesn't
    # zero out and drag the percentile down.
    velocities = [n.bpm * max(n.sv_multiplier, 0.25) for n in hittable]
    times = [n.time_ms for n in hittable]
    n = len(times)

    # Dense stretches: top ~20% by local NPS. A fast SV during a rest doesn't
    # count as reading pressure — you have time to see it.
    window_ms = 500
    local_counts = [0] * n
    lo = 0
    hi = 0
    for i in range(n):
        while lo < n and times[lo] < times[i] - window_ms:
            lo += 1
        while hi < n and times[hi] <= times[i] + window_ms:
            hi += 1
        local_counts[i] = hi - lo
    density_threshold = sorted(local_counts)[int(0.8 * n)] if n else 0
    dense_velocities = [v for v, d in zip(velocities, local_counts) if d >= density_threshold]

    def _pctile(vals: list[float], p: float) -> float:
        if not vals:
            return 0.0
        s = sorted(vals)
        idx = min(len(s) - 1, int(p * len(s)))
        return s[idx]

    # Sustained-fast-scroll share: fraction of notes whose ±200ms neighborhood
    # (~400ms sustained window) is ALSO above the 280 threshold. A lone SV=2.5
    # gimmick note doesn't count — its neighbors are back at normal scroll.
    # Only stretches where the scroll consistently holds above 280 fire this.
    HIGH = 280
    sustained_flag = [0] * n
    for i in range(n):
        t = times[i]
        # Notes within ±200ms of this one.
        j_lo = i
        while j_lo > 0 and times[j_lo - 1] >= t - 200:
            j_lo -= 1
        j_hi = i
        while j_hi + 1 < n and times[j_hi + 1] <= t + 200:
            j_hi += 1
        window = velocities[j_lo:j_hi + 1]
        if not window:
            continue
        # Sustained = median of the 400ms neighborhood is above threshold.
        window_sorted = sorted(window)
        if window_sorted[len(window_sorted) // 2] > HIGH:
            sustained_flag[i] = 1

    return ReadingProfile(
        velocity_dense_p50=_pctile(dense_velocities, 0.50) if dense_velocities else 0.0,
        sustained_share=sum(sustained_flag) / n,
        velocity_p95=_pctile(velocities, 0.95),
    )


# --- aggregate --------------------------------------------------------------

@dataclass(frozen=True)
class MapFeatures:
    density: DensityProfile
    movement: MovementProfile
    color: ColorProfile
    rhythm: RhythmProfile
    bursts: BurstProfile
    transitions: TransitionProfile
    trajectory: TrajectoryProfile
    gimmick: GimmickProfile
    reading: ReadingProfile
    segments: SegmentProfile
    strain: StrainProfile
    parity: ParityProfile
    streams: "StreamProfile"

    # Convenience roll-ups for headline stats:
    total_notes: int
    hittable_notes: int
    drumroll_notes: int
    denden_notes: int
    big_note_ratio: float

    def as_dict(self) -> dict:
        return {
            "density": asdict(self.density),
            "movement": asdict(self.movement),
            "color": asdict(self.color),
            "rhythm": asdict(self.rhythm),
            "bursts": asdict(self.bursts),
            "transitions": asdict(self.transitions),
            "trajectory": asdict(self.trajectory),
            "gimmick": asdict(self.gimmick),
            "reading": asdict(self.reading),
            "segments": asdict(self.segments),
            "strain": asdict(self.strain),
            "streams": asdict(self.streams),
            "total_notes": self.total_notes,
            "hittable_notes": self.hittable_notes,
            "drumroll_notes": self.drumroll_notes,
            "denden_notes": self.denden_notes,
            "big_note_ratio": self.big_note_ratio,
        }


def extract_features(beatmap: TaikoBeatmap) -> MapFeatures:
    hittable = beatmap.hittable()
    density = density_profile(hittable)
    movement = movement_profile(beatmap.hit_objects, density.duration_s)
    color = color_profile(hittable)
    rhythm = rhythm_profile(hittable)
    bursts = burst_profile(hittable)
    transitions = transition_profile(hittable, density.duration_s)
    trajectory = trajectory_profile(hittable)
    gimmick = gimmick_profile(hittable, density.avg_nps, movement)
    reading = reading_profile(hittable)
    segments = segment_profile(hittable, n_segments=10)
    strain = strain_profile(hittable)
    parity = compute_parity(hittable)
    streams = stream_profile(hittable)

    drumrolls = sum(
        1 for n in beatmap.hit_objects
        if n.note_type in (NoteType.DRUMROLL, NoteType.DRUMROLL_BIG)
    )
    dendens = sum(1 for n in beatmap.hit_objects if n.note_type == NoteType.DENDEN)
    bigs = sum(1 for n in hittable if n.note_type.is_big)

    return MapFeatures(
        density=density,
        movement=movement,
        color=color,
        rhythm=rhythm,
        bursts=bursts,
        transitions=transitions,
        trajectory=trajectory,
        gimmick=gimmick,
        reading=reading,
        segments=segments,
        strain=strain,
        parity=parity,
        streams=streams,
        total_notes=len(beatmap.hit_objects),
        hittable_notes=len(hittable),
        drumroll_notes=drumrolls,
        denden_notes=dendens,
        big_note_ratio=bigs / len(hittable) if hittable else 0.0,
    )

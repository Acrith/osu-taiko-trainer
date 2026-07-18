"""Pattern-parity: KDDK-hostility scoring per hittable note.

Per the KDDK model (see memory/feedback_kddk_parity.md):
- Players never do same-hand finger switches mid-stream — every note alternates
  hands, so per-note hand-mechanics are NOT a difficulty axis.
- Difficulty comes from THREE sources: mono-run fatigue (long same-color runs
  strain a single finger), big-note interruption (disrupts alternation), and
  chunk-misalignment (patterns that don't segment cleanly into 4-note groups
  the dominant hand can lead, forcing offhand switches).

This module produces a per-note hostility score in [0, 1]. Classification then
promotes wrong-color / miss judgments with high parity to PATTERN_PARITY cause
rather than lumping them with random-misclick wrong-color.
"""
from __future__ import annotations

from dataclasses import dataclass

from .models import HitObject


@dataclass(frozen=True)
class ParityProfile:
    per_note: tuple[float, ...]      # 0..1 hostility score for each hittable note
    mean: float
    hostile_ratio: float             # share of notes with score > 0.5
    mono_run_cost: tuple[float, ...] # decomposition: mono-run signal per note
    big_interrupt_cost: tuple[float, ...]
    misalign_cost: tuple[float, ...]


def compute_parity(hittable: tuple[HitObject, ...]) -> ParityProfile:
    n = len(hittable)
    if n == 0:
        return ParityProfile((), 0.0, 0.0, (), (), ())

    mono = [0.0] * n
    big = [0.0] * n
    misalign = [0.0] * n

    # --- Signal 1: mono-run fatigue --------------------------------------
    # Long same-color runs strain a single finger — one hand carries the
    # same color while the other hand keeps alternating. Cost grows with
    # position in the run and total run length.
    run_start = 0
    run_don = hittable[0].note_type.is_don
    for i in range(1, n + 1):
        at_boundary = (i == n) or (hittable[i].note_type.is_don != run_don)
        if not at_boundary:
            continue
        run_len = i - run_start
        if run_len >= 3:
            for j in range(run_start, i):
                pos = j - run_start
                # position cost + length surcharge; capped at 1.0
                cost = min(1.0, pos * 0.12 + max(0, run_len - 3) * 0.08)
                mono[j] = max(mono[j], cost)
        if i < n:
            run_start = i
            run_don = hittable[i].note_type.is_don

    # --- Signal 2: big-note interruption in fast context ----------------
    # A big note forces both hands. If the next note is very close (fast
    # stream), the player has to "restart" alternation, adding friction.
    for i in range(1, n):
        prev = hittable[i - 1]
        curr = hittable[i]
        if not prev.note_type.is_big:
            continue
        gap = curr.time_ms - prev.time_ms
        if gap <= 0 or gap >= 200:
            continue
        big[i] = max(big[i], (1.0 - gap / 200.0) * 0.6)

    # --- Signal 3: chunk misalignment via run-length variance -----------
    # Half-alt KDDK players learn patterns as 4-note chunks the dominant hand
    # leads. Whether a pattern segments cleanly depends on whether the
    # sequence of same-color RUN LENGTHS is uniform, cleanly alternating,
    # or mixed. Reading the user's examples:
    #   KDDDKDDDK → runs 1,3,1,3,1 → alternating → easy
    #   KKDDKKDDK → runs 2,2,2,2,1 → nearly uniform → easy
    #   KDDKDDKDD → runs 1,2,1,2,1,2,2 → mostly alternating → moderate
    #   KKKDDDKKK → runs 3,3,3 → uniform (but mono-cost fires) → moderate
    #   KKKDDDDKK → runs 3,4,2 → mixed lengths → hard (offhand technical)
    #   KKDKKKKDK → runs 2,1,4,1,1 → very mixed → hard
    #
    # For each note, compute the variance of the run lengths in a ~5-run
    # window centred at its run. High variance in the window = the pattern
    # isn't segmenting into repeatable chunks the dominant hand can lead.
    runs: list[tuple[int, int]] = []  # (start_index, length)
    r_start = 0
    r_don = hittable[0].note_type.is_don
    for i in range(1, n + 1):
        if i == n or hittable[i].note_type.is_don != r_don:
            runs.append((r_start, i - r_start))
            if i < n:
                r_start = i
                r_don = hittable[i].note_type.is_don

    # Map each note index to its containing run index.
    note_run_idx = [0] * n
    for r_idx, (r_start_, r_len) in enumerate(runs):
        for j in range(r_start_, r_start_ + r_len):
            note_run_idx[j] = r_idx

    for i in range(n):
        r_idx = note_run_idx[i]
        # Local window of runs around this one.
        w_lo = max(0, r_idx - 2)
        w_hi = min(len(runs), r_idx + 3)
        lengths = [runs[k][1] for k in range(w_lo, w_hi)]
        if len(lengths) < 3:
            continue

        # Uniform = all same length: mono cost already covers this — skip.
        distinct = set(lengths)
        if len(distinct) == 1:
            continue

        # Clean alternating (like 1,3,1,3 or 2,2,2,2 which was caught above):
        # check if lengths[k] == lengths[k % 2] for all k.
        alternating = all(lengths[k] == lengths[k % 2] for k in range(len(lengths)))
        if alternating:
            continue

        # Tempo modulation — mixed chunks at slow tempo are less painful.
        gap_prev = hittable[i].time_ms - hittable[i - 1].time_ms if i > 0 else 500
        gap_next = hittable[i + 1].time_ms - hittable[i].time_ms if i < n - 1 else 500
        avg_gap = (gap_prev + gap_next) / 2.0
        tempo_factor = max(0.0, 1.0 - avg_gap / 250.0)
        if tempo_factor <= 0:
            continue

        # Signal A — traditional variance signal for runs like 3,4,2 (mixed
        # LONG chunks that don't segment cleanly).
        avg_len = sum(lengths) / len(lengths)
        var_len = sum((l - avg_len) ** 2 for l in lengths) / len(lengths)
        var_cost = min(1.0, var_len / 3.0) * 0.7

        # Signal B — short-run mixing where length-3 chunks appear amongst
        # 1s and 2s. Pure 1-2 patterns (KDKDKKDK, common in fast streams) are
        # predictable and NOT KDDK-hostile; the friction comes from length-3
        # spikes breaking the dominant-hand chunking. Blue Army sits on this
        # exact pattern: runs of 1s, 2s, and occasional 3s intermixing.
        short_mix_cost = 0.0
        three_count = sum(1 for l in lengths if l == 3)
        if max(lengths) <= 3 and three_count >= 1 and len(distinct) >= 2:
            # More length-3 spikes AND more distinct lengths = worse.
            short_mix_cost = min(1.0, 0.30 + 0.08 * (len(distinct) - 2) + 0.10 * three_count) * 0.55

        misalign[i] = max(misalign[i], max(var_cost, short_mix_cost) * tempo_factor)

    # --- combine ---------------------------------------------------------
    per_note = tuple(min(1.0, mono[i] + big[i] + misalign[i]) for i in range(n))
    mean_score = sum(per_note) / n if n else 0.0
    hostile = sum(1 for s in per_note if s > 0.5) / n if n else 0.0

    return ParityProfile(
        per_note=per_note,
        mean=mean_score,
        hostile_ratio=hostile,
        mono_run_cost=tuple(mono),
        big_interrupt_cost=tuple(big),
        misalign_cost=tuple(misalign),
    )



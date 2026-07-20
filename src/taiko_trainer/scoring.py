"""5-D map rating on a pp-inspired unbounded scale.

Each map gets 5 independent ratings that grow with difficulty. They are NOT
capped at 100 — a superhuman map can score 1500+ in speed, and a player's
"speed rating" simply means the highest speed-rated map they've cleared.

Design notes cribbed from osu!taiko's current difficulty algorithm
(ppy/osu, TaikoDifficultyCalculator.cs):

  raw_dimension  = weighted sum of feature norms (per-dimension rubric)
  shaped         = max(0, raw)**2 / 15                # super-linear like pp: SR^2.25-ish
  length_bonus   = 1 + 0.25 * H / (H + 4000)          # asymptotic, max +25%
  final          = shaped * length_bonus

Anchor calibration (matches pp intuition — 5★ SS ~= 280 pp, 8★ SS ~= 570 pp):
  raw ~ 60  ->  ~250 rating   (moderate difficulty in that dimension)
  raw ~ 80  ->  ~450 rating   (hard)
  raw ~ 100 ->  ~700 rating   (very hard)
  raw ~ 140 ->  ~1400 rating  (elite)

The rubrics reflect the user's mental model:

- speed:       raw tempo × short-window density × burst shape
- stamina:     sustained density × duration × escalation trajectory
- gimmick:     SV movement × density overlap ("hard to see"), damped by BPM
- technical:   rhythmic-divisor entropy + divisor transitions + off-grid, low BPM boost
- consistency: uniform stable-BPM patterning, with penalties for high-BPM/heavy-SV/burst signals
"""
from __future__ import annotations

from dataclasses import asdict, dataclass

from .features import MapFeatures


def _norm(value: float, lo: float, hi: float) -> float:
    """Piecewise-linear normalisation, saturated: below lo -> 0, above hi -> 1."""
    if hi <= lo:
        return 0.0
    return max(0.0, min(1.0, (value - lo) / (hi - lo)))


def _norm_up(value: float, lo: float, hi: float) -> float:
    """Piecewise-linear normalisation, uncapped upward: below lo -> 0, no ceiling.

    Use for features where beyond-anchor values should keep contributing —
    e.g. BPM (someone maps at 400 BPM), NPS (dense-beyond-Fool bursts), etc.
    Bounded ratios (share of X, ratio 0-1 by construction) should use _norm.
    """
    if hi <= lo:
        return 0.0
    return max(0.0, (value - lo) / (hi - lo))


def _shape(raw: float) -> float:
    """Super-linear compressor: turns raw weighted-sum into an unbounded rating."""
    raw = max(0.0, raw)
    return (raw ** 2) / 15.0


def _length_bonus(hittable_notes: int) -> float:
    """Asymptotic length bonus straight from the pp formula."""
    return 1.0 + 0.25 * hittable_notes / (hittable_notes + 4000)


@dataclass(frozen=True)
class DimensionRating:
    speed: float
    stamina: float
    gimmick: float
    technical: float
    consistency: float
    reading: float = 0.0    # base scroll velocity / reaction-time load

    def as_dict(self) -> dict[str, float]:
        return asdict(self)

    def dominant(self) -> str:
        return max(self.as_dict().items(), key=lambda kv: kv[1])[0]


# --- per-dimension raw scorers ----------------------------------------------
# Each returns an UNBOUNDED raw weighted sum. The shape+length pipeline in
# rate_map() turns raw into the final rating.

def _trusted_bpm(m) -> float:
    """Sanity-capped BPM. Uses `.bpm_max` from the .osu timing points unless
    the note stream disagrees strongly — some gimmick maps declare BPM 727
    (etc.) to sync a storyboard while the actual notes play at ~300. Caps at
    1.3× the effective (note-stream-derived) BPM in that case.

    Falls back to declared bpm_max when effective can't be derived (very short
    maps). For legit high-BPM content the two agree and this is a no-op."""
    if m.bpm_effective <= 0:
        return m.bpm_max
    return min(m.bpm_max, m.bpm_effective * 1.3)


def _bpm_inflation_scale(m) -> float:
    """Ratio of trusted to declared BPM. 1.0 = no inflation; <1.0 = mapper
    trick. Downstream metrics that scale with per-note BPM (reading velocity,
    sv_bpm_score) get scaled down by this ratio so the same protection
    applies without editing feature extraction."""
    if m.bpm_max <= 0:
        return 1.0
    return _trusted_bpm(m) / m.bpm_max


def _raw_speed(f: MapFeatures) -> float:
    # Speed = motor tempo only: BPM, short-window density, burst shape.
    # SV does NOT belong here — SV creates reading/reaction pressure, which
    # belongs to gimmick (low-SV obstruction) or technical (high-SV fast reaction).
    #
    # peak_nps_200ms captures instantaneous burst density (e.g. 5-6 notes in
    # 200ms), which is what a "1/8 or 1/12 flurry" looks like in the note stream.
    # length_3_ratio (bursts of 3-6) is the burst-shape signal per the user's
    # rubric ("length 4-7 at high BPM"). length_7plus is deliberately excluded
    # — long streams belong to stamina.
    # density_span from the 10-chunk profile captures "catch-up shape" — a map
    # that goes from sparse to dense mid-run forces the player to react to a
    # tempo shift within the same map.
    bpm_n = _norm_up(_trusted_bpm(f.movement), 150, 280)
    peak200_n = _norm_up(f.density.peak_nps_200ms, 15, 30)
    peak5s_n = _norm_up(f.density.peak_nps_5s, 8, 16)
    peak1s_n = _norm_up(f.density.peak_nps, 10, 20)
    length3_n = _norm_up(f.bursts.length_3_ratio, 0.0, 0.35)
    span_n = _norm(f.segments.density_span, 2, 8)
    return 45 * bpm_n + 20 * peak200_n + 15 * peak5s_n + 10 * peak1s_n + 10 * length3_n + 15 * span_n


def _raw_stamina(f: MapFeatures, style: str = "kddk") -> float:
    # Stamina = weighted top-K aggregation of 20-second window intensities.
    # Sorted-descending window intensities are weighted by 0.85^rank and summed,
    # so the top-3 to top-5 windows dominate but the tail still contributes.
    # This rewards short-but-intense maps (Vicious Heroism's 7 windows of high
    # 256 BPM stress) against long-and-moderate maps (Dynasty's 19 windows of
    # milder drain), matching the "how exhausting is this?" intuition better
    # than a plain sum ever could.
    #
    # Per-window intensity itself accounts for density, BPM, burst structure,
    # and mono runs. Pattern-parity (KDDK-hostile shapes) is not yet modelled.
    # See memory/feedback_stamina_model.md.
    base = f.strain.weighted_sum / 0.9

    if style in ("ddkk", "kkdd"):
        # DDKK/KKDD map color-to-hand — a mono-color run is the SAME hand
        # doing all the work. Amplify stamina for maps with sustained
        # same-color chunks. First-pass calibration: needs feedback from
        # real DDKK play data to tune the anchors.
        mono_amp = 1.0 + 0.5 * _norm_up(f.color.run_length_max, 10, 50)
        alt_penalty = 1.0 + 0.25 * (1.0 - f.color.color_change_ratio)
        base *= mono_amp * alt_penalty

    return base


def _raw_gimmick(f: MapFeatures) -> float:
    # sv_bpm_score aggregates per-note (bpm × sv). If the map declares a wild
    # BPM to sync a storyboard, that inflates sv_bpm_score too — scale down
    # by the same trusted/declared ratio the other dims use.
    scale = _bpm_inflation_scale(f.movement)
    sv_bpm_n = _norm_up(f.gimmick.sv_bpm_score * scale, 5, 300)
    unread_n = _norm(f.gimmick.unreadable_ratio, 0.005, 0.10)
    sv_changes_n = _norm_up(f.movement.sv_changes_per_minute, 5, 200)
    return 55 * sv_bpm_n + 25 * unread_n + 20 * sv_changes_n


def _raw_technical(f: MapFeatures, style: str = "kddk") -> float:
    # Technical difficulty for KDDK players: hard rhythmic divisors + hard-
    # rhythm-switch transitions + stream-based KDDK-hostility. The stream
    # metric (see kddk_patterns.py) is the primary signal — it applies
    # Alchyr-style per-transition color friction, length-weighted, and only
    # to sustained fast streams. Patterns like The Fool's KDDDDD get crushed
    # by same-parity + repetition decay so they don't inflate technical.
    tech_div_share = (
        f.rhythm.divisor_share.get("1/6", 0.0)
        + f.rhythm.divisor_share.get("1/8", 0.0)
        + f.rhythm.divisor_share.get("1/3", 0.0)
        + f.rhythm.divisor_share.get("1/12", 0.0)
    )
    tech_div_n = _norm(tech_div_share, 0.02, 0.15)

    q_specific = f.transitions.quarter_sixth_transitions + f.transitions.quarter_third_transitions
    q_n = _norm(q_specific, 5, 100)

    # Gate tech_div_n by how well the hard divisors are INTEGRATED into 1/4
    # streams (q_specific = # of 1/4-to-1/6 and 1/4-to-1/3 transitions). A map
    # with 8% 1/6 divisors AND many transitions (Sonatina: 65) is genuinely
    # rhythm-switching technical. Same 8% with only 1-2 transitions (Telepathy
    # [Huh]) has isolated bursts — mash/speed content, not KDDK-technical.
    # Ring My Bell at q=13 keeps ~70% because its 1/6s DO mix in with the 1/4s.
    integration_gate = 0.3 + 0.7 * _norm(q_specific, 3, 20)
    tech_div_n *= integration_gate

    trans_n = _norm_up(f.transitions.transitions_per_minute, 40, 250)
    # off_grid_ratio is a bounded 0..1 share, so it must SATURATE — otherwise
    # Kantan / Futsuu diffs (whose 2×/4×/etc-beat gaps our divisor detector
    # can only bucket as "other") blow up the technical rating. Anchor set
    # loose enough that a real 1/6-1/4 mixing map (Sonatina ~0.8%) still
    # scores meaningfully.
    offgrid_n = _norm(f.rhythm.off_grid_ratio, 0.0, 0.04)
    # Moderate-BPM boost — technical maps are rarely 250 BPM speed monsters.
    low_bpm_boost = _norm(220 - _trusted_bpm(f.movement), 30, 90)

    # Stream-based KDDK signal: aggregated length × parity friction. Blue Army
    # rides on this (159-note streams of short-run mixing with high per-note
    # color); The Fool's long streams are muscle-memory-locked KDDDDD which
    # Alchyr's repetition decay crushes.
    stream_n = _norm(f.streams.stream_value, 3, 60)
    # Bonus: count of streams that are BOTH long (>=61 notes) AND KDDK-hostile
    # (per-note color >=0.25). This is Blue Army's specific signature — 4 such
    # streams on Blue Army INNER ONI, 0 on Fool despite similar length.
    hostile_bonus = min(5.0, f.streams.hostile_long_count)

    if style in ("ddkk", "kkdd"):
        # DDKK/KKDD map color-to-hand, so the KDDK "hostile stream" concept
        # (long streams with hand-alternation friction) doesn't apply the
        # same way. Long same-color streams for DDKK are stamina, not
        # technical. Drop hostile_bonus and heavily discount stream_n.
        # Rhythmic hard-divisor content still counts identically.
        return (
            25 * tech_div_n
            + 18 * q_n
            + 12 * trans_n
            + 8 * offgrid_n
            + 5 * low_bpm_boost
            + 10 * stream_n     # kept small — DDKK still needs to READ dense streams
        )

    return (
        25 * tech_div_n
        + 18 * q_n
        + 12 * trans_n
        + 8 * offgrid_n
        + 5 * low_bpm_boost
        + 32 * stream_n
        + hostile_bonus
    )


def _raw_consistency(f: MapFeatures) -> float:
    # Consistency = uniform challenge across map duration.
    # Polar opposite of TECHNICAL ONLY — a consistency map can still be fast, dense, or
    # gimmicky, as long as the challenge stays predictable throughout. So there is NO
    # BPM penalty and NO SV penalty here; only rhythmic shifting (technical) breaks it.
    # Duration saturates at 8:00. Beyond that a map isn't structurally more
    # consistent — sustaining it becomes a stamina concern, not a map-structure
    # one, and stamina has its own dim. (Under the previous _norm_up this term
    # was unbounded and a 60-min marathon produced dur_n=12+, blowing the dim
    # to 30k+.)
    dur_n = _norm(f.density.duration_s, 120, 480)
    uniform_density = 1.0 - _norm(f.density.section_nps_stddev_30s, 0.5, 2.5)
    # Divisor simplicity: reward share at the "easy" divisors (1/1, 1/2, 1/4) —
    # stray notes and streams — NOT Shannon entropy of the distribution.
    # Dynasty's 45%+46% 1/4-vs-1/2 IS clean-and-simple even though its Shannon
    # entropy is higher than Sonatina's 71% 1/4 (which is stream-dominant with
    # 1/6 mixed in). What matters is what divisors are present, not their balance.
    easy_div_share = (
        f.rhythm.divisor_share.get("1/1", 0.0)
        + f.rhythm.divisor_share.get("1/2", 0.0)
        + f.rhythm.divisor_share.get("1/4", 0.0)
    )
    div_simplicity = _norm(easy_div_share, 0.60, 0.95)
    off_grid_simplicity = 1.0 - _norm(f.rhythm.off_grid_ratio, 0.0, 0.05)
    bpm_stab = 1.0 if f.movement.distinct_bpm_count <= 1 else (0.6 if f.movement.distinct_bpm_count <= 3 else 0.2)

    base = (
        45 * dur_n
        + 30 * uniform_density
        + 25 * div_simplicity
        + 15 * off_grid_simplicity
        + 10 * bpm_stab
        + 5
    )

    # Technical opposition: QUADRATIC penalty on divisor entropy so mid-entropy maps
    # barely lose consistency while high-entropy (rhythmically shifting) maps get
    # crushed. Anchors (1.35 -> 1.85) target the discriminating band between our
    # reference consistency map (entropy 1.51) and technical map (1.78). A gimmick
    # map that carries embedded technical rhythm (entropy 1.98) will also drop
    # sharply — the user's own rubric says gimmick = SV + technical.
    entropy_excess = _norm(f.rhythm.divisor_entropy_bits, 1.35, 1.85)
    tech_penalty = 60 * (entropy_excess ** 2)

    # Flat-trajectory reward, MULTIPLICATIVELY gated by low entropy — a technical
    # map can be uniformly-dense too (low span), but it shouldn't be rewarded for
    # that because its rhythm-shifting is what breaks consistency. So the reward
    # only fires when BOTH span is low AND entropy is low.
    span_flatness = 1.0 - _norm(f.segments.density_span, 2, 8)
    entropy_simplicity = 1.0 - _norm(f.rhythm.divisor_entropy_bits, 1.4, 1.9)
    flat_reward = 12 * span_flatness * entropy_simplicity

    # Long-stream penalty: consistency per the user's rubric is "flowy, rewards
    # consistent accuracy" — grindy stamina maps like Parodia Sonatina are the
    # opposite of flowy even when their density is uniform and rhythm is simple.
    # Anchor (0.3 -> 0.7) leaves moderate-stream maps (Vicious 0.26) untouched
    # and heavily discounts stamina-heavy maps (Sonatina 0.70, Fool 0.59).
    stream_penalty = 25 * _norm(f.bursts.length_7plus_ratio, 0.30, 0.70)

    # Hard-divisor penalty: 1/6, 1/8, 1/3, 1/12 presence breaks consistency
    # regardless of Shannon entropy. Sonatina at 8.4% 1/6 is technical enough
    # that the pattern isn't "consistent-accuracy" material, even though its
    # density and BPM are uniform.
    hard_div_share = (
        f.rhythm.divisor_share.get("1/6", 0.0)
        + f.rhythm.divisor_share.get("1/8", 0.0)
        + f.rhythm.divisor_share.get("1/3", 0.0)
        + f.rhythm.divisor_share.get("1/12", 0.0)
    )
    hard_div_penalty = 30 * _norm(hard_div_share, 0.02, 0.15)

    return base + flat_reward - tech_penalty - hard_div_penalty - stream_penalty


def _raw_reading_parts(f: MapFeatures) -> tuple[float, float]:
    """Return (fast_load, slow_load) — unshaped. Shaped separately by
    rate_map so mod multipliers (HD) land as they read: fast × 1.25 and
    slow × 1.75 at the RATING level, not the raw level (which would square
    them via _shape). See `_raw_reading` docstring for the model itself."""
    return _raw_reading_split(f)


def _raw_reading_split(f: MapFeatures, *, is_hr: bool = False) -> tuple[float, float]:
    """(fast_load, slow_load) for the reading dim, in raw pre-shape units.

    Uses barrysir's stable-taiko scroll physics:
    - runway_ms per note is computed at feature-extraction time assuming NM,
      16:9 aspect. HR is applied here at scoring time via a division by
      1.867 = 1.4 × (16/9) / (4/3), which is the empirically-measured HR
      scroll multiplier for widescreen.
    - notes_on_screen shrinks by the same HR factor (HR spreads notes apart
      → fewer visible at once).

    16:9 assumption: we don't know the player's aspect ratio (not in .osr).
    Modern default. 4:3 players get slightly under-rated reading; 16:9 dead-on.
    """
    HR_FACTOR = 1.867 if is_hr else 1.0

    runway_p50 = getattr(f.reading, "runway_ms_dense_p50", 0.0) or 0.0
    runway_p95 = getattr(f.reading, "runway_ms_dense_p95", 0.0) or 0.0
    on_screen  = getattr(f.reading, "notes_on_screen_p95", 0.0) or 0.0

    if runway_p50 <= 0:
        return 0.0, 0.0

    eff_runway_p50 = runway_p50 / HR_FACTOR
    eff_runway_p95 = runway_p95 / HR_FACTOR
    eff_on_screen  = on_screen  / HR_FACTOR

    # FAST side — shorter runway = more reaction pressure.
    # Anchors (16:9 wallclock ms):
    #   >700 ms  comfortable    (below threshold, ~0 load)
    #   500 ms   hard           (mid-range 1/4 HR)
    #   400 ms   very hard      (Wa~tobi HR)
    #   300 ms   extreme        (HRDT / semifinals)
    #   <250 ms  at reaction-time floor
    runway_load = _norm_up(700.0 - eff_runway_p50, 100, 400)
    peak_load   = _norm_up(700.0 - eff_runway_p95, 100, 400)

    # MOTOR–COGNITIVE COUPLING (per user's insight): below 500ms runway,
    # reaction and pattern-comprehension compete for the same cognitive
    # budget. Simple sparse patterns can still be run on autopilot; DENSE
    # patterns at short runway become "unreadable" territory because the
    # brain can't decode + react in the same window.
    #
    # Multiplier = 1.0 (comfort) → 2.0 (short-runway AND high-density).
    # Fires only when BOTH conditions apply — a lone burst at 400ms in an
    # otherwise sparse map doesn't cross the threshold.
    peak_nps_5s = getattr(f.density, "peak_nps_5s", 0.0) or 0.0
    short_runway_intensity = _norm(500.0 - eff_runway_p50, 0.0, 250.0)  # 0 at 500ms → 1 at 250ms
    density_intensity = _norm(peak_nps_5s, 12.0, 22.0)                   # 0 at 12nps → 1 at 22nps
    motor_coupling = 1.0 + 1.0 * short_runway_intensity * density_intensity

    fast_load = (55 * runway_load + 45 * peak_load) * motor_coupling

    # SLOW side — more notes on screen = crowding / stack pressure.
    #   ≤8   comfortable
    #   14   crowded (1/6 at SV 1.0, or 1/4 at SV ~0.7)
    #   20   very crowded (1/8 at SV 1.0, or heavy low-SV stacks)
    stack_load_n = _norm(eff_on_screen, 8.0, 20.0)
    stack_load = 65 * stack_load_n

    return fast_load, stack_load


def _raw_reading(f: MapFeatures, *, fast_mult: float = 1.0, slow_mult: float = 1.0) -> float:
    """Reading is a TWO-SIDED scroll-velocity pressure.

    Comfort band: ~150-280 units (BPM × SV), roughly "1.0 SV at typical
    tempos". Deviation in either direction is harder to read:

      FAST side (>280):  notes fly by; reaction time is the bottleneck.
                         Extreme cases (semifinals: 400+) dominate.
      SLOW side (<150):  notes visually pile up before the previous one
                         clears. Player must disambiguate order + timing
                         from a static stack. HR partly rescues this
                         (scroll_mult=1.4 pushes velocity toward comfort).
                         HD makes it worse (bigger frame to remember).

    Both sides fire independently and sum — a map can have BOTH fast and
    stacked sections. Distinct from gimmick, which captures chaotic SV
    changes; reading is the sustained velocity level itself.

    Anchors (fast side):
        <180  no meaningful fast-side load (Kantan / low-BPM Futsuu)
        180   floor — scroll starts feeling brisk
        280   "starts feeling fast" (200 BPM HR, 230 BPM standard-SV)
        380   dense_p50 saturation — semifinals-tier reading

    Anchors (slow side):
        >150  comfort or above, no stack pressure
        150   floor — scroll starts feeling stacked
        50    brutal stack (dense_p50 = 50 units → ≈ 0.25 SV at 200 BPM)
    """
    # velocity_dense_p50 is (per-note bpm × sv_multiplier) median in dense
    # sections. Same BPM-inflation issue as gimmick — scale by the trusted
    # ratio so a gimmick 727-BPM declaration doesn't fake a huge reading load.
    scale = _bpm_inflation_scale(f.movement)
    v = f.reading.velocity_dense_p50 * scale

    # This is the LEGACY sum-then-shape entry point, kept for callers that
    # pass a single reading_mult. New code path in rate_map calls
    # `_raw_reading_split` and shapes fast/slow separately so the mults land
    # at the rating level, not the raw level.
    fast_load, stack_load = _raw_reading_split(f)
    return fast_load * fast_mult + stack_load * slow_mult


def _od_pressure(
    od: float,
    od_mult: float = 1.0,
    hit_window_mult: float = 1.0,
) -> float:
    """How much tighter accuracy is vs the OD 5 baseline (great window = 35 ms).

    Returns:
        1.0  at OD 5, nomod (the baseline).
        > 1  when the effective GREAT window is narrower — higher OD, DT, HR, or combos.
        < 1  when it's wider — lower OD, EZ, HT.

    Used to modulate consistency and (to a smaller extent) technical:
    a fast map at OD 4 rewards fewer accuracy skills than the same map at OD 8;
    HR + DT stack this even further because both shrink the window.

    `od_mult` scales the OD number before window lookup (HR = 1.4, EZ = 0.5,
    capped at 10). `hit_window_mult` scales the wall-clock window after
    lookup (DT = 1/1.5, HT = 1/0.75). Same split as `JudgmentWindows.from_od`
    so the pressure the rating sees matches the pressure the judge sees."""
    from .judgment import _od_lerp
    effective_od = min(od * od_mult, 10.0)
    window = _od_lerp(effective_od, 50.0, 35.0, 20.0) * hit_window_mult
    return 35.0 / max(window, 1.0)


# How strongly each dimension responds to accuracy pressure. Consistency is
# ~pure accuracy so it moves the most; technical is partially accuracy (hard
# divisors + timing) so it moves half as much; the rest (speed, stamina,
# gimmick) don't depend on OD in a way that's separable from the structural
# signals they already capture.
_OD_BOOST_K_CONSISTENCY = 0.35
_OD_BOOST_K_TECHNICAL   = 0.20


def rate_map(
    features: MapFeatures,
    *,
    od: float = 5.0,
    od_mult: float = 1.0,
    hit_window_mult: float = 1.0,
    reading_mult: float = 1.0,           # DEPRECATED — use the split multipliers
    reading_fast_mult: float | None = None,
    reading_slow_mult: float | None = None,
    is_hr: bool = False,                 # HR's aspect-dependent scroll factor
    style: str = "kddk",
) -> DimensionRating:
    """Rate the map on the six dimensions.

    `od` is the map's OverallDifficulty (from `.osu`). `od_mult` scales
    the OD number itself (HR = 1.4, EZ = 0.5). `hit_window_mult` scales
    the wall-clock window after OD lookup (DT = 1/1.5, HT = 1/0.75).
    Splitting the two matches judgment's behavior — HR/EZ recompute at
    effective OD, DT/HT scale windows after.

    HD amplifies reading asymmetrically:
      `reading_fast_mult`  1.25 with HD — less visual runway per note.
      `reading_slow_mult`  1.75 with HD — stacked-note frame gets much
                           harder to memorise, fade lands on ambiguous
                           visual state.
    `reading_mult` is the legacy scalar. If the split values aren't given,
    both default to reading_mult (preserving old behavior for callers that
    haven't migrated).

    `style` picks between KDDK (default) and DDKK/KKDD. Only stamina +
    technical have style-specific paths so far."""
    if reading_fast_mult is None:
        reading_fast_mult = reading_mult
    if reading_slow_mult is None:
        reading_slow_mult = reading_mult

    bonus = _length_bonus(features.hittable_notes)
    pressure = _od_pressure(od, od_mult=od_mult, hit_window_mult=hit_window_mult)
    cons_mult = 1.0 + _OD_BOOST_K_CONSISTENCY * (pressure - 1.0)
    tech_mult = 1.0 + _OD_BOOST_K_TECHNICAL   * (pressure - 1.0)
    return DimensionRating(
        speed=_shape(_raw_speed(features)) * bonus,
        stamina=_shape(_raw_stamina(features, style=style)) * bonus,
        gimmick=_shape(_raw_gimmick(features)) * bonus,
        technical=_shape(_raw_technical(features, style=style)) * bonus * tech_mult,
        consistency=_shape(_raw_consistency(features)) * bonus * cons_mult,
        # Reading is shaped per-side so HD's asymmetric mults (fast 1.25×,
        # slow 1.75×) land at the rating level rather than getting squared
        # by _shape. Sum-then-shape (via _raw_reading) would give 1.56× and
        # 3.06× post-shape from the same mults.
        reading=(_shape(_raw_reading_split(features, is_hr=is_hr)[0]) * reading_fast_mult
                 + _shape(_raw_reading_split(features, is_hr=is_hr)[1]) * reading_slow_mult) * bonus,
    )

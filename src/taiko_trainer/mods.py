"""Mod bitfield → effective difficulty & speed for judgment + rating.

osrparse gives us a raw Mod bitfield off the replay. For our purposes the
mods that matter are the ones that change what the map plays like:

- DT / NC: 1.5× speed (notes come faster; hit windows tighten proportionally)
- HR:      1.4× effective OD (tighter hit windows, higher approach — reading load)
- HD:      notes fade (pure reading load; timing unchanged)
- HT:      0.75× speed (rare in ranked; still handled for completeness)
- NF/SD/PF/RL/AT/AP: don't affect what the play tested — flagged only

HDDT is the common serious-play combo (extra reading + speed). HRDT and HDHR
are technically legal but very rare — this module composes them correctly
regardless.

The rest of the codebase treats mods via two knobs:
    speed_mult       — scale hit_object BPM up and time_ms down for features/rating
    hit_window_mult  — scale JudgmentWindows down for timing tolerance
"""
from __future__ import annotations

from dataclasses import dataclass, replace
from enum import IntFlag

from .models import HitObject, TaikoBeatmap


class Mod(IntFlag):
    """Matches osrparse's Mod bitfield values."""
    NONE      = 0
    NF        = 1        # NoFail
    EZ        = 2        # Easy
    HD        = 8        # Hidden
    HR        = 16       # HardRock
    SD        = 32       # SuddenDeath
    DT        = 64       # DoubleTime
    RL        = 128      # Relax
    HT        = 256      # HalfTime
    NC        = 512      # Nightcore (implies DT for speed)
    FL        = 1024     # Flashlight
    AT        = 2048     # Autoplay
    SO        = 4096     # SpunOut (irrelevant for taiko)
    AP        = 8192     # Autopilot (irrelevant for taiko)
    PF        = 16384    # Perfect
    # Higher bits are lazer/keys/etc; ignored for taiko.


# Mods we visibly label in the UI, in a stable canonical order so "HDDT" always
# reads left-to-right the way players write it.
_LABEL_ORDER = [
    (Mod.EZ, "EZ"),
    (Mod.NF, "NF"),
    (Mod.HT, "HT"),
    (Mod.HR, "HR"),
    (Mod.HD, "HD"),
    (Mod.DT, "DT"),   # NC is normalized into DT for label
    (Mod.FL, "FL"),
    (Mod.SD, "SD"),
    (Mod.PF, "PF"),
    (Mod.RL, "RL"),
    (Mod.AT, "AT"),
    (Mod.AP, "AP"),
]


@dataclass(frozen=True)
class ModEffects:
    """Everything downstream cares about, extracted from a raw bitfield.

    Two distinct axes affect the hit windows:

    - od_mult          scales the OD NUMBER before window lookup. HR = 1.4,
                       EZ = 0.5, others = 1.0. This is how osu!taiko really
                       models HR — not "windows × 1/1.4", but "OD × 1.4 then
                       recompute windows". The distinction matters because
                       the OD→window function is piecewise linear (different
                       slope 0-5 vs 5-10), so the two approaches give
                       measurably different windows at the same OD (~2ms at
                       OD 6, enough to convert 300s into 100s).
    - hit_window_mult  scales the final wall-clock window after OD lookup.
                       DT = 1/1.5, HT = 1/0.75. Used for wall-clock speed
                       scaling only; HR/EZ do NOT belong here.

    Combined effective window in ms:
        effective_od = min(od * od_mult, 10.0)
        window       = od_lerp(effective_od, ...) * hit_window_mult
    """
    bitfield: int
    label: str              # "NM", "DT", "HDDT", "HRDT", ...
    speed_mult: float       # 1.0 nm, 1.5 dt/nc, 0.75 ht
    od_mult: float          # 1.0 nm, 1.4 hr, 0.5 ez — multiplier on OD NUMBER
    hit_window_mult: float  # 1.0 nm, 1/1.5 dt, 1/0.75 ht — wall-clock only
    scroll_mult: float      # 1.0 nm, 1.4 hr — visual scroll speed multiplier for
                            # the READING dimension. DT stays 1.0 here because
                            # its 1.5× BPM already amplifies scroll velocity through
                            # the `bpm × sv` product in ReadingProfile.
    reading_mult: float     # DEPRECATED: kept for backwards compat with any external
                            # callers. Use reading_fast_mult + reading_slow_mult below.
                            # For HD-on maps this equals reading_fast_mult (the
                            # milder amplification), preserving old behavior for
                            # scoring paths that only read the single scalar.
    reading_fast_mult: float # 1.0 nm, 1.25 hd — HD amplifies fast-scroll reading
                             # (less visual runway per note flying by).
    reading_slow_mult: float # 1.0 nm, 1.75 hd — HD amplifies slow-scroll reading
                             # more — a bigger visual frame of stacked notes has
                             # to be memorised, and the fade timing lands on notes
                             # that are already visually confused.
    has_hd: bool            # reading challenge marker (no timing effect)
    has_hr: bool
    has_dt: bool            # true for both DT and NC
    has_ht: bool

    @property
    def is_nm(self) -> bool:
        return self.label == "NM"

    @property
    def alters_map(self) -> bool:
        """True if the effective difficulty vector differs from the base map's
        rating. Fires when the play changes what feature extraction sees
        (speed_mult, scroll_mult), what accuracy pressure the rating reflects
        (od_mult, hit_window_mult), OR the reading dim (reading_mult — HD)."""
        return (
            self.speed_mult != 1.0
            or self.od_mult != 1.0
            or self.hit_window_mult != 1.0
            or self.scroll_mult != 1.0
            or self.reading_mult != 1.0
        )


def parse_mods(bitfield: int) -> ModEffects:
    """Extract judgment- and rating-relevant effects from an osr mod bitfield."""
    bf = int(bitfield) if bitfield else 0
    has_dt = bool(bf & (Mod.DT | Mod.NC))
    has_ht = bool(bf & Mod.HT)
    has_hr = bool(bf & Mod.HR)
    has_hd = bool(bf & Mod.HD)
    has_ez = bool(bf & Mod.EZ)

    # DT and HT are mutually exclusive in game; DT wins if both set (defensive).
    speed_mult = 1.5 if has_dt else (0.75 if has_ht else 1.0)

    # OD multiplier — HR bumps the OD NUMBER by 1.4, EZ halves it. Windows are
    # then recomputed at the new OD, matching osu!taiko's actual HR behavior.
    # (HR and EZ are mutually exclusive in game; HR wins if both set.)
    od_mult = 1.4 if has_hr else (0.5 if has_ez else 1.0)

    # Wall-clock hit window multiplier — DT tightens by 1/1.5 (notes come
    # 1.5× faster, so the same OD-derived window is a smaller fraction of
    # a beat). HT widens by the inverse. HR/EZ do NOT belong here; they're
    # captured via od_mult above.
    hit_window_mult = 1.0
    if has_dt: hit_window_mult *= 1.0 / 1.5
    if has_ht: hit_window_mult *= 1.0 / 0.75

    # scroll_mult USED to apply HR's visual bump directly to per-note SV in
    # apply_mods_to_beatmap. That path is now DEPRECATED — reading uses
    # barrysir's physics formula (features.py `_runway_ms`) and applies HR
    # at scoring time via the aspect-dependent factor. Keeping scroll_mult
    # at 1.0 avoids double-counting: apply_mods_to_beatmap no longer
    # touches SV for HR, and the runway calc handles it end-to-end.
    scroll_mult = 1.0

    # HD reading multipliers — HD hits the two reading sides asymmetrically.
    # Fast-scroll side: less visual runway per note (1.25×).
    # Slow-scroll side: much harder because the stacked frame is bigger AND
    # the fade timing lands where the visual is already ambiguous (1.75×).
    # `reading_mult` (the old scalar) equals reading_fast_mult so the
    # ModEffects.alters_map check still fires and legacy callers see the
    # milder value.
    reading_fast_mult = 1.25 if has_hd else 1.0
    reading_slow_mult = 1.75 if has_hd else 1.0
    reading_mult = reading_fast_mult

    # Label: concatenate active mods in canonical order.
    parts: list[str] = []
    for flag, tag in _LABEL_ORDER:
        if bf & flag:
            if tag == "DT" and (bf & Mod.NC):
                parts.append("NC")
            else:
                parts.append(tag)
    label = "".join(parts) or "NM"

    return ModEffects(
        bitfield=bf,
        label=label,
        speed_mult=speed_mult,
        od_mult=od_mult,
        hit_window_mult=hit_window_mult,
        scroll_mult=scroll_mult,
        reading_mult=reading_mult,
        reading_fast_mult=reading_fast_mult,
        reading_slow_mult=reading_slow_mult,
        has_hd=has_hd,
        has_hr=has_hr,
        has_dt=has_dt,
        has_ht=has_ht,
    )


def apply_mods_to_beatmap(bm: TaikoBeatmap, mods: ModEffects) -> TaikoBeatmap:
    """Return a scaled copy of `bm` with:
    - hit-object BPMs × speed_mult, time_ms / speed_mult (DT/HT effects)
    - hit-object sv_multiplier × scroll_mult (HR visual scroll bump)

    Downstream feature extraction then sees the map as the player actually
    experiences it — DT amplifies scroll velocity through bpm×sv, HR
    amplifies it directly through sv.

    NM returns the original beatmap unchanged — no wasted copies for the
    common case. OD is left in `bm.difficulty` and judgment scales windows
    via `mods.hit_window_mult` (keeps source-of-truth OD intact, and
    avoids double-counting DT+HR)."""
    if mods.speed_mult == 1.0 and mods.scroll_mult == 1.0:
        return bm

    inv_speed = 1.0 / mods.speed_mult
    scaled_hits: list[HitObject] = []
    for h in bm.hit_objects:
        scaled_hits.append(replace(
            h,
            time_ms=int(round(h.time_ms * inv_speed)),
            end_time_ms=int(round(h.end_time_ms * inv_speed)),
            bpm=h.bpm * mods.speed_mult,
            sv_multiplier=h.sv_multiplier * mods.scroll_mult,
        ))
    return replace(bm, hit_objects=tuple(scaled_hits))

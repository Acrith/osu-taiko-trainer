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
    """Everything downstream cares about, extracted from a raw bitfield."""
    bitfield: int
    label: str              # "NM", "DT", "HDDT", "HRDT", ...
    speed_mult: float       # 1.0 nm, 1.5 dt/nc, 0.75 ht
    hit_window_mult: float  # 1.0 nm, 1/1.5 dt, 1/1.4 hr, product for combos
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
        rating. Currently gated on speed changes; HR tightens hit windows but
        doesn't change what the map's structure looks like, and HD's reading
        load isn't in the rating model yet — both stay 'base rating' until
        those are modeled. Judgment still applies their windows via
        `hit_window_mult` regardless."""
        return self.speed_mult != 1.0


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
    # Hit-window multiplier: DT tightens by 1/1.5, HR by 1/1.4, EZ widens by
    # 1.5. These compose multiplicatively.
    hit_window_mult = 1.0
    if has_dt: hit_window_mult *= 1.0 / 1.5
    if has_hr: hit_window_mult *= 1.0 / 1.4
    if has_ez: hit_window_mult *= 1.5
    if has_ht: hit_window_mult *= 1.0 / 0.75  # widen — same wall-clock windows but slower notes

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
        hit_window_mult=hit_window_mult,
        has_hd=has_hd,
        has_hr=has_hr,
        has_dt=has_dt,
        has_ht=has_ht,
    )


def apply_mods_to_beatmap(bm: TaikoBeatmap, mods: ModEffects) -> TaikoBeatmap:
    """Return a scaled copy of `bm` with hit-object BPMs multiplied by
    `mods.speed_mult` and time_ms divided by it — so downstream feature
    extraction sees the map as the player actually experiences it.

    NM (or any mods that don't alter timing) returns the original beatmap
    unchanged — no wasted copies for the common case.

    OD is left as the source value in `bm.difficulty`. Judgment scales the
    hit windows via `mods.hit_window_mult` instead of touching OD directly,
    which keeps the source-of-truth OD intact for display and avoids
    double-counting DT+HR."""
    if mods.speed_mult == 1.0:
        return bm

    inv = 1.0 / mods.speed_mult
    scaled_hits: list[HitObject] = []
    for h in bm.hit_objects:
        scaled_hits.append(replace(
            h,
            time_ms=int(round(h.time_ms * inv)),
            end_time_ms=int(round(h.end_time_ms * inv)),
            bpm=h.bpm * mods.speed_mult,
        ))
    return replace(bm, hit_objects=tuple(scaled_hits))

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum, IntFlag


class NoteType(Enum):
    DON = "don"
    KAT = "kat"
    DON_BIG = "don_big"
    KAT_BIG = "kat_big"
    DRUMROLL = "drumroll"
    DRUMROLL_BIG = "drumroll_big"
    DENDEN = "denden"

    @property
    def is_hit(self) -> bool:
        return self in (
            NoteType.DON,
            NoteType.KAT,
            NoteType.DON_BIG,
            NoteType.KAT_BIG,
        )

    @property
    def is_don(self) -> bool:
        return self in (NoteType.DON, NoteType.DON_BIG)

    @property
    def is_kat(self) -> bool:
        return self in (NoteType.KAT, NoteType.KAT_BIG)

    @property
    def is_big(self) -> bool:
        return self in (NoteType.DON_BIG, NoteType.KAT_BIG, NoteType.DRUMROLL_BIG)


@dataclass(frozen=True)
class TimingPoint:
    time_ms: int
    beat_length: float
    meter: int
    uninherited: bool

    @property
    def bpm(self) -> float | None:
        # BPM is only defined by uninherited points.
        if not self.uninherited or self.beat_length <= 0:
            return None
        return 60000.0 / self.beat_length

    @property
    def sv_multiplier(self) -> float | None:
        # Inherited points carry SV as a negative beat_length: -100 == 1.0x.
        if self.uninherited:
            return None
        return 100.0 / -self.beat_length if self.beat_length < 0 else 1.0


@dataclass(frozen=True)
class HitObject:
    time_ms: int
    note_type: NoteType
    end_time_ms: int  # equals time_ms for single hits; > time_ms for drumroll/denden
    bpm: float                  # BPM in effect at this note's time
    sv_multiplier: float        # SV multiplier in effect at this note's time
    raw_type: int               # original .osu type bitfield (kept for debugging)
    raw_hitsound: int           # original .osu hitsound bitfield (kept for debugging)


@dataclass(frozen=True)
class BeatmapMeta:
    title: str
    artist: str
    creator: str
    version: str            # difficulty name, e.g. "Sangwonsa"
    beatmap_id: int | None
    beatmapset_id: int | None
    audio_filename: str


@dataclass(frozen=True)
class Difficulty:
    hp_drain_rate: float
    circle_size: float
    overall_difficulty: float
    approach_rate: float
    slider_multiplier: float
    slider_tick_rate: float


@dataclass(frozen=True)
class TaikoBeatmap:
    meta: BeatmapMeta
    difficulty: Difficulty
    timing_points: tuple[TimingPoint, ...]
    hit_objects: tuple[HitObject, ...]
    mode: int
    beatmap_md5: str        # md5 of the .osu file bytes; used to match with replay's beatmap_hash

    def hittable(self) -> tuple[HitObject, ...]:
        # Just the notes that count for accuracy/score, i.e. don/kat (drumroll and denden are separate mechanics).
        return tuple(n for n in self.hit_objects if n.note_type.is_hit)


class TaikoInput(IntFlag):
    # Bit values match osrparse.KeyTaiko so we can round-trip trivially.
    LEFT_DON = 1
    LEFT_KAT = 2
    RIGHT_DON = 4
    RIGHT_KAT = 8

    @staticmethod
    def dons() -> "TaikoInput":
        return TaikoInput.LEFT_DON | TaikoInput.RIGHT_DON

    @staticmethod
    def kats() -> "TaikoInput":
        return TaikoInput.LEFT_KAT | TaikoInput.RIGHT_KAT


@dataclass(frozen=True)
class ReplayFrame:
    time_ms: int                # absolute (cumulative sum of deltas)
    held: TaikoInput            # keys held at this frame
    pressed: TaikoInput         # rising edges vs previous frame (new key-down events)


@dataclass(frozen=True)
class ReplayMeta:
    player: str
    beatmap_md5: str            # what the replay says it was played on
    score: int
    max_combo: int
    count_300: int              # in taiko: GREAT (large hit)
    count_100: int              # in taiko: GOOD  (small hit)
    count_miss: int
    count_geki: int             # in taiko: also GREAT-adjacent; kept for parity with osrparse
    count_katu: int             # in taiko: also GOOD-adjacent
    perfect_combo: bool
    mods: int                   # raw osrparse Mod bitfield
    timestamp: datetime
    game_version: int


@dataclass(frozen=True)
class TaikoReplay:
    meta: ReplayMeta
    frames: tuple[ReplayFrame, ...]

    def key_down_events(self) -> tuple[tuple[int, TaikoInput], ...]:
        # Flattens pressed-flags into individual (time_ms, single_key) events, in time order.
        events: list[tuple[int, TaikoInput]] = []
        for frame in self.frames:
            if not frame.pressed:
                continue
            # Coalesce simultaneous same-color presses into ONE event per color per
            # frame — this matches how the game treats a big-note two-hand press
            # (one hit that awards a geki bonus, not two hits). If we didn't
            # coalesce, the "extra" simultaneous press could leak into the next
            # close-by note's window and cause spurious misses.
            pressed_dons = frame.pressed & TaikoInput.dons()
            pressed_kats = frame.pressed & TaikoInput.kats()
            if pressed_dons:
                events.append((frame.time_ms, pressed_dons))
            if pressed_kats:
                events.append((frame.time_ms, pressed_kats))
        return tuple(events)

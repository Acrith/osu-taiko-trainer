from __future__ import annotations

import hashlib
from bisect import bisect_right
from pathlib import Path

from .models import (
    BeatmapMeta,
    Difficulty,
    HitObject,
    NoteType,
    TaikoBeatmap,
    TimingPoint,
)


class OsuParseError(ValueError):
    pass


# .osu type-field bits (see https://osu.ppy.sh/wiki/en/Client/File_formats/osu_%28file_format%29#hit-objects)
_TYPE_HIT_CIRCLE = 1 << 0
_TYPE_SLIDER = 1 << 1
_TYPE_SPINNER = 1 << 3

# hitSound bits
_HS_WHISTLE = 1 << 1
_HS_FINISH = 1 << 2
_HS_CLAP = 1 << 3


def parse_osu_file(path: str | Path) -> TaikoBeatmap:
    path = Path(path)
    raw = path.read_bytes()
    md5 = hashlib.md5(raw).hexdigest()
    # osu files are UTF-8; a stray BOM is possible.
    text = raw.decode("utf-8-sig")

    sections = _split_sections(text)

    general = _kv(sections.get("General", ""))
    mode = int(general.get("Mode", "0") or 0)
    if mode != 1:
        # We still parse but flag; caller can decide.
        pass

    metadata = _kv(sections.get("Metadata", ""))
    difficulty_kv = _kv(sections.get("Difficulty", ""))

    meta = BeatmapMeta(
        title=metadata.get("Title", ""),
        artist=metadata.get("Artist", ""),
        creator=metadata.get("Creator", ""),
        version=metadata.get("Version", ""),
        beatmap_id=_maybe_int(metadata.get("BeatmapID")),
        beatmapset_id=_maybe_int(metadata.get("BeatmapSetID")),
        audio_filename=general.get("AudioFilename", ""),
    )

    difficulty = Difficulty(
        hp_drain_rate=float(difficulty_kv.get("HPDrainRate", 5)),
        circle_size=float(difficulty_kv.get("CircleSize", 5)),
        overall_difficulty=float(difficulty_kv.get("OverallDifficulty", 5)),
        approach_rate=float(difficulty_kv.get("ApproachRate", 5)),
        slider_multiplier=float(difficulty_kv.get("SliderMultiplier", 1.4)),
        slider_tick_rate=float(difficulty_kv.get("SliderTickRate", 1)),
    )

    timing_points = _parse_timing_points(sections.get("TimingPoints", ""))
    hit_objects = _parse_hit_objects(
        sections.get("HitObjects", ""),
        timing_points=timing_points,
        slider_multiplier=difficulty.slider_multiplier,
    )

    return TaikoBeatmap(
        meta=meta,
        difficulty=difficulty,
        timing_points=timing_points,
        hit_objects=hit_objects,
        mode=mode,
        beatmap_md5=md5,
    )


def _split_sections(text: str) -> dict[str, str]:
    sections: dict[str, list[str]] = {}
    current: str | None = None
    header_seen = False
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not header_seen:
            # The very first non-empty line is "osu file format vN"; skip it.
            if line.startswith("osu file format"):
                header_seen = True
            continue
        if not line:
            continue
        if line.startswith("//"):
            continue
        if line.startswith("[") and line.endswith("]"):
            current = line[1:-1]
            sections.setdefault(current, [])
            continue
        if current is None:
            continue
        sections[current].append(raw_line)
    return {name: "\n".join(lines) for name, lines in sections.items()}


def _kv(body: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for line in body.splitlines():
        line = line.strip()
        if not line or line.startswith("//"):
            continue
        if ":" not in line:
            continue
        key, _, value = line.partition(":")
        out[key.strip()] = value.strip()
    return out


def _maybe_int(value: str | None) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(value)
    except ValueError:
        return None


def _parse_timing_points(body: str) -> tuple[TimingPoint, ...]:
    points: list[TimingPoint] = []
    for line in body.splitlines():
        line = line.strip()
        if not line or line.startswith("//"):
            continue
        parts = line.split(",")
        if len(parts) < 2:
            continue
        try:
            time_ms = int(round(float(parts[0])))
            beat_length = float(parts[1])
        except ValueError:
            continue
        meter = int(parts[2]) if len(parts) > 2 else 4
        # Older maps omit the uninherited flag; then a positive beat_length implies uninherited.
        if len(parts) > 6:
            uninherited = parts[6].strip() == "1"
        else:
            uninherited = beat_length > 0
        points.append(TimingPoint(
            time_ms=time_ms,
            beat_length=beat_length,
            meter=meter,
            uninherited=uninherited,
        ))
    points.sort(key=lambda p: p.time_ms)
    return tuple(points)


class _TimingLookup:
    """Precomputed per-timing-point context (BPM, SV) with fast lookup by time."""

    def __init__(self, timing_points: tuple[TimingPoint, ...]):
        self._times: list[int] = []
        self._bpms: list[float] = []
        self._svs: list[float] = []
        current_bpm = 120.0  # neutral fallback if the map has zero uninherited points before the first note
        current_sv = 1.0
        for tp in timing_points:
            if tp.uninherited:
                bpm = tp.bpm
                if bpm is not None:
                    current_bpm = bpm
                # Uninherited timing points reset SV to 1.0.
                current_sv = 1.0
            else:
                sv = tp.sv_multiplier
                if sv is not None:
                    current_sv = sv
            self._times.append(tp.time_ms)
            self._bpms.append(current_bpm)
            self._svs.append(current_sv)
        self._first_bpm = self._bpms[0] if self._bpms else 120.0
        self._first_sv = self._svs[0] if self._svs else 1.0

    def context_at(self, time_ms: int) -> tuple[float, float]:
        if not self._times:
            return (120.0, 1.0)
        # Find rightmost timing point with time_ms <= t.
        idx = bisect_right(self._times, time_ms) - 1
        if idx < 0:
            # Note lands before any timing point: use the earliest one's context.
            return (self._first_bpm, self._first_sv)
        return (self._bpms[idx], self._svs[idx])

    def uninherited_beat_length_at(self, time_ms: int) -> float:
        bpm, _ = self.context_at(time_ms)
        return 60000.0 / bpm if bpm > 0 else 0.0


def _parse_hit_objects(
    body: str,
    timing_points: tuple[TimingPoint, ...],
    slider_multiplier: float,
) -> tuple[HitObject, ...]:
    lookup = _TimingLookup(timing_points)
    notes: list[HitObject] = []
    for line in body.splitlines():
        line = line.strip()
        if not line or line.startswith("//"):
            continue
        parts = line.split(",")
        if len(parts) < 5:
            continue
        try:
            time_ms = int(round(float(parts[2])))
            raw_type = int(parts[3])
            raw_hitsound = int(parts[4])
        except ValueError:
            continue

        bpm, sv = lookup.context_at(time_ms)

        # In taiko:
        #   - Whistle or clap  -> KAT (blue)
        #   - Otherwise         -> DON (red)
        #   - Finish            -> big note (both hands)
        is_kat = bool(raw_hitsound & (_HS_WHISTLE | _HS_CLAP))
        is_big = bool(raw_hitsound & _HS_FINISH)

        if raw_type & _TYPE_SLIDER:
            # slider fields: index 5 = curveType|curvePoints, 6 = slides, 7 = length
            slides = int(parts[6]) if len(parts) > 6 else 1
            length_px = float(parts[7]) if len(parts) > 7 else 0.0
            beat_length = lookup.uninherited_beat_length_at(time_ms)
            # Duration formula per osu wiki:
            #   duration = length / (SliderMultiplier * 100 * SV) * beat_length
            denom = slider_multiplier * 100.0 * sv
            single_slide_ms = (length_px / denom) * beat_length if denom > 0 else 0.0
            duration_ms = int(round(single_slide_ms * max(1, slides)))
            note_type = NoteType.DRUMROLL_BIG if is_big else NoteType.DRUMROLL
            end_time = time_ms + duration_ms
        elif raw_type & _TYPE_SPINNER:
            # spinner: index 5 = endTime (denden in taiko)
            end_ms = int(round(float(parts[5]))) if len(parts) > 5 else time_ms
            note_type = NoteType.DENDEN
            end_time = end_ms
        elif raw_type & _TYPE_HIT_CIRCLE:
            if is_kat:
                note_type = NoteType.KAT_BIG if is_big else NoteType.KAT
            else:
                note_type = NoteType.DON_BIG if is_big else NoteType.DON
            end_time = time_ms
        else:
            # Unknown type — skip rather than crash.
            continue

        notes.append(HitObject(
            time_ms=time_ms,
            note_type=note_type,
            end_time_ms=end_time,
            bpm=bpm,
            sv_multiplier=sv,
            raw_type=raw_type,
            raw_hitsound=raw_hitsound,
        ))

    notes.sort(key=lambda n: n.time_ms)
    return tuple(notes)

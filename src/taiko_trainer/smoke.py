"""Smoke test: parse the reference beatmap + replay and print a summary.

Run with:
    uv run taiko-smoke
"""
from __future__ import annotations

import sys
from collections import Counter
from pathlib import Path

from .models import NoteType, TaikoBeatmap, TaikoReplay
from .osr_parser import parse_osr_file
from .osu_parser import parse_osu_file


REFERENCES = Path(__file__).resolve().parents[2] / "references"
DEFAULT_OSU = REFERENCES / "932109 Girl's Day - Ring My Bell [no video]" / "Girl's Day - Ring My Bell (Capu) [Sangwonsa].osu"
DEFAULT_OSR = REFERENCES / "Acrith - Girl's Day - Ring My Bell [Sangwonsa] (2026-07-16) Taiko.osr"


def main() -> int:
    argv = sys.argv[1:]
    osu_path = Path(argv[0]) if len(argv) >= 1 else DEFAULT_OSU
    osr_path = Path(argv[1]) if len(argv) >= 2 else DEFAULT_OSR

    if not osu_path.exists():
        print(f"ERROR: beatmap not found: {osu_path}", file=sys.stderr)
        return 1
    if not osr_path.exists():
        print(f"ERROR: replay not found: {osr_path}", file=sys.stderr)
        return 1

    print(f"== .osu  {osu_path.name}")
    beatmap = parse_osu_file(osu_path)
    _print_beatmap(beatmap)

    print()
    print(f"== .osr  {osr_path.name}")
    replay = parse_osr_file(osr_path)
    _print_replay(replay)

    print()
    _cross_check(beatmap, replay)
    return 0


def _print_beatmap(bm: TaikoBeatmap) -> None:
    print(f"  {bm.meta.artist} - {bm.meta.title} [{bm.meta.version}] mapped by {bm.meta.creator}")
    print(f"  mode={bm.mode}  audio={bm.meta.audio_filename}  md5={bm.beatmap_md5}")
    d = bm.difficulty
    print(f"  HP={d.hp_drain_rate}  OD={d.overall_difficulty}  AR={d.approach_rate}  SM={d.slider_multiplier}")

    uninherited = [t for t in bm.timing_points if t.uninherited]
    inherited = [t for t in bm.timing_points if not t.uninherited]
    bpms = sorted({round(t.bpm, 2) for t in uninherited if t.bpm})
    print(f"  timing points: {len(bm.timing_points)} total  ({len(uninherited)} uninherited, {len(inherited)} inherited)")
    print(f"  distinct BPMs: {bpms}")

    counts = Counter(n.note_type for n in bm.hit_objects)
    ordered = [
        NoteType.DON, NoteType.KAT, NoteType.DON_BIG, NoteType.KAT_BIG,
        NoteType.DRUMROLL, NoteType.DRUMROLL_BIG, NoteType.DENDEN,
    ]
    print("  hit objects:")
    for nt in ordered:
        if counts[nt]:
            print(f"    {nt.value:<13} {counts[nt]}")
    hittable = bm.hittable()
    print(f"  hittable notes (for accuracy): {len(hittable)}")

    if bm.hit_objects:
        first = bm.hit_objects[0]
        last = max(bm.hit_objects, key=lambda n: n.end_time_ms)
        span_s = (last.end_time_ms - first.time_ms) / 1000
        avg_nps = len(hittable) / span_s if span_s > 0 else 0.0
        print(f"  span: {first.time_ms} ms -> {last.end_time_ms} ms  ({span_s:.1f}s)  avg NPS (hittable) = {avg_nps:.2f}")


def _print_replay(rp: TaikoReplay) -> None:
    m = rp.meta
    total_judged = m.count_300 + m.count_100 + m.count_miss
    # Standard osu!taiko accuracy formula: (300s + 0.5 * 100s) / total.
    acc = (m.count_300 + 0.5 * m.count_100) / total_judged if total_judged else 0.0
    print(f"  player={m.player}  score={m.score}  max_combo={m.max_combo}  perfect={m.perfect_combo}")
    print(f"  judgments: 300={m.count_300}  100={m.count_100}  miss={m.count_miss}  (accuracy={acc*100:.2f}%)")
    print(f"  beatmap_hash (replay claims): {m.beatmap_md5}")
    print(f"  played at: {m.timestamp.isoformat()}  game_version={m.game_version}  mods={m.mods}")

    print(f"  frames: {len(rp.frames)}")
    key_events = rp.key_down_events()
    print(f"  key-down events: {len(key_events)}")

    per_key: Counter = Counter()
    for _, key in key_events:
        per_key[key.name] += 1
    print("  per-key press count:")
    for name in ("LEFT_DON", "RIGHT_DON", "LEFT_KAT", "RIGHT_KAT"):
        print(f"    {name:<9} {per_key.get(name, 0)}")

    if rp.frames:
        span_s = (rp.frames[-1].time_ms - rp.frames[0].time_ms) / 1000
        print(f"  frame span: {rp.frames[0].time_ms} ms -> {rp.frames[-1].time_ms} ms  ({span_s:.1f}s)")


def _cross_check(bm: TaikoBeatmap, rp: TaikoReplay) -> None:
    print("== cross-check")
    md5_match = bm.beatmap_md5.lower() == rp.meta.beatmap_md5.lower()
    print(f"  map md5 == replay.beatmap_hash?  {md5_match}")
    if not md5_match:
        print(f"    map:    {bm.beatmap_md5}")
        print(f"    replay: {rp.meta.beatmap_md5}")

    hittable = len(bm.hittable())
    judged = rp.meta.count_300 + rp.meta.count_100 + rp.meta.count_miss
    print(f"  hittable notes ({hittable}) == judged ({judged})?  {hittable == judged}")

    key_events = len(rp.key_down_events())
    print(f"  key-down events in replay: {key_events}  (must be >= judged for a full clear)")


if __name__ == "__main__":
    raise SystemExit(main())

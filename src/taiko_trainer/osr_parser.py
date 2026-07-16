from __future__ import annotations

from pathlib import Path

from osrparse import GameMode, KeyTaiko, Replay

from .models import ReplayFrame, ReplayMeta, TaikoInput, TaikoReplay


class OsrParseError(ValueError):
    pass


def parse_osr_file(path: str | Path) -> TaikoReplay:
    path = Path(path)
    replay = Replay.from_path(str(path))

    if replay.mode != GameMode.TAIKO:
        raise OsrParseError(f"Expected Taiko replay, got mode={replay.mode!r}")

    meta = ReplayMeta(
        player=replay.username,
        beatmap_md5=replay.beatmap_hash,
        score=replay.score,
        max_combo=replay.max_combo,
        count_300=replay.count_300,
        count_100=replay.count_100,
        count_miss=replay.count_miss,
        count_geki=replay.count_geki,
        count_katu=replay.count_katu,
        perfect_combo=replay.perfect,
        mods=int(replay.mods),
        timestamp=replay.timestamp,
        game_version=replay.game_version,
    )

    frames = _build_frames(replay.replay_data)
    return TaikoReplay(meta=meta, frames=frames)


def _build_frames(events) -> tuple[ReplayFrame, ...]:
    frames: list[ReplayFrame] = []
    current_time = 0
    previous_held = TaikoInput(0)
    for event in events:
        # -12345 marks the RNG-seed sentinel frame — skip it. Other negative
        # deltas are legitimate: the first frame typically has a negative delta
        # that positions the replay in the map's lead-in period (audio hasn't
        # started yet). Skipping those shifts every subsequent event by
        # abs(that_delta) ms, breaking judgment for any map with early notes.
        if event.time_delta == -12345:
            continue
        current_time += event.time_delta
        held = _keys_to_input(event.keys)
        pressed = held & ~previous_held  # rising edges only
        frames.append(ReplayFrame(
            time_ms=current_time,
            held=held,
            pressed=pressed,
        ))
        previous_held = held
    return tuple(frames)


def _keys_to_input(keys: KeyTaiko) -> TaikoInput:
    # KeyTaiko bit values match TaikoInput by construction; cast is safe.
    return TaikoInput(int(keys))

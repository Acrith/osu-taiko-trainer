from __future__ import annotations

import lzma
import struct
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

    # osrparse silently strips the initial `y=-500` setup frames. In most replays
    # their combined delta is ~0, so this is invisible. But some replays encode a
    # real audio-lead-in delta (e.g. +8590 ms) in one of those setup frames, and
    # stripping it makes every subsequent event's cumulative time 8590 ms early
    # relative to the game's actual play time. Re-read the raw LZMA to detect
    # that offset and pre-load it into cumulative time.
    initial_offset_ms = _detect_stripped_setup_offset(path)
    frames = _build_frames(replay.replay_data, initial_offset_ms=initial_offset_ms)
    return TaikoReplay(meta=meta, frames=frames)


def _lazer_extra_bytes(replay_bytes: bytes) -> bytes | None:
    """Return the raw bytes past the standard-layout end, or None if the
    replay is stable / unparseable. Lazer appends an LZMA-compressed
    JSON blob (client_version, mods with settings, statistics, ...)."""
    try:
        data = replay_bytes
        p = 1 + 4
        def skip_str():
            nonlocal p
            if data[p] == 0x00:
                p += 1; return
            p += 1
            shift = 0; length = 0
            while True:
                b = data[p]; p += 1
                length |= (b & 0x7f) << shift
                if not (b & 0x80): break
                shift += 7
            p += length
        skip_str(); skip_str(); skip_str()
        p += 2 * 6 + 4 + 2 + 1 + 4
        skip_str()
        p += 8
        rl = struct.unpack_from("<i", data, p)[0]; p += 4
        p += rl
        p += 8
        return data[p:] if len(data) > p else None
    except Exception:
        return None


def extract_lazer_mod_settings(replay_bytes: bytes) -> dict | None:
    """Decompress the lazer trailer and return the parsed JSON dict, or
    None if the replay is stable / trailer unparseable. The JSON includes
    per-mod settings like {"acronym":"DT","settings":{"speed_change":1.34}}.
    """
    import json as _json
    extra = _lazer_extra_bytes(replay_bytes)
    if not extra or len(extra) < 5:
        return None
    try:
        blob_len = struct.unpack_from("<i", extra, 0)[0]
        blob = extra[4:4 + blob_len]
        text = lzma.decompress(blob, format=lzma.FORMAT_ALONE).decode("utf-8", errors="replace")
        return _json.loads(text)
    except Exception:
        return None


def lazer_custom_rate(replay_bytes: bytes) -> float | None:
    """Return the custom speed multiplier if this is a lazer play using a
    non-standard rate for DT/HT, else None. Standard-rate lazer plays
    (DT=1.5×, HT=0.75×) return None — they behave identically to stable.
    """
    settings = extract_lazer_mod_settings(replay_bytes)
    if not settings:
        return None
    for mod in settings.get("mods", []):
        acronym = mod.get("acronym")
        rate = (mod.get("settings") or {}).get("speed_change")
        if rate is None:
            continue
        if acronym in ("DT", "NC") and abs(rate - 1.5) > 0.01:
            return rate
        if acronym in ("HT", "DC") and abs(rate - 0.75) > 0.01:
            return rate
    return None


def detect_lazer_replay(replay_bytes: bytes) -> bool:
    """True if the .osr looks like a lazer replay.

    Two independent signals — either fires:

    (1) `game_version` >= 30000000. Lazer uses 3xxxxxxx (Mekrin's Whispered
        upload = 30000017); stable uses YYYYMMDD dates (~20250815).
    (2) Extra bytes past the standard-layout end. Stable ends at the
        online_score_id long; lazer appends a length-prefixed blob with
        per-mod settings (including SpeedChange for custom-rate DT/HT).

    Returns False on any parse failure — permissive fallback."""
    try:
        data = replay_bytes
        # Signal (1): game_version at bytes 1..4 (mode byte + int).
        game_version = struct.unpack_from("<i", data, 1)[0]
        if game_version >= 30_000_000:
            return True
        # Signal (2): extra bytes past standard end.
        p = 5                           # already consumed mode(1) + game_version(4)
        def skip_str():
            nonlocal p
            if data[p] == 0x00:
                p += 1; return
            p += 1
            shift = 0; length = 0
            while True:
                b = data[p]; p += 1
                length |= (b & 0x7f) << shift
                if not (b & 0x80): break
                shift += 7
            p += length
        skip_str()                      # beatmap_hash
        skip_str()                      # username
        skip_str()                      # replay_hash
        p += 2 * 6                      # counts
        p += 4                          # score
        p += 2                          # max_combo
        p += 1                          # perfect
        p += 4                          # mods bitfield
        skip_str()                      # life bar graph
        p += 8                          # timestamp
        replay_len = struct.unpack_from("<i", data, p)[0]; p += 4
        p += replay_len                 # LZMA blob
        p += 8                          # online_score_id
        return len(data) > p
    except Exception:
        return False


def _detect_stripped_setup_offset(path: Path) -> int:
    """Read the raw replay LZMA and return the sum of any leading y=-500 setup
    frames' time deltas that osrparse dropped. In well-formed replays this is 0.
    Returns 0 on any parse failure — worst case is current behavior.
    """
    try:
        data = path.read_bytes()
    except OSError:
        return 0
    # Locate the LZMA block. The .osr layout has variable-length prefix strings,
    # so we search for the LZMA properties byte (0x5D) preceded by a plausible
    # 4-byte length. This is heuristic but stable for real replays.
    for off in range(1, len(data) - 20):
        if data[off + 4] == 0x5D and data[off + 5] == 0x00 and data[off + 6] == 0x00:
            try:
                lz_len = struct.unpack_from("<I", data, off)[0]
                if not (1000 < lz_len < len(data)):
                    continue
                lz = data[off + 4 : off + 4 + lz_len]
                dec = lzma.decompress(lz, format=lzma.FORMAT_ALONE).decode(errors="ignore")
                break
            except Exception:
                continue
    else:
        return 0

    events = [e for e in dec.split(",") if "|" in e]
    total_stripped_delta = 0
    for ev in events:
        parts = ev.split("|")
        if len(parts) < 4:
            continue
        try:
            delta = int(parts[0])
            y = int(parts[2])
        except ValueError:
            continue
        # Stop as soon as we hit a real frame. The setup-frame convention is
        # y = -500 (and specifically for the initial two-frame block).
        if y != -500:
            break
        total_stripped_delta += delta
    return total_stripped_delta


def _build_frames(events, initial_offset_ms: int = 0) -> tuple[ReplayFrame, ...]:
    frames: list[ReplayFrame] = []
    current_time = initial_offset_ms
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

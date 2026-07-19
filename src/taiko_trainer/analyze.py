"""Cross-dimension dashboard.

Auto-discovers map+replay pairs in references/{speed,stamina,gimmick,technical,consistency}/,
matches .osu -> .osr by md5, and prints a side-by-side table of features so we can
see whether the features actually separate the five categories.

    uv run python -m taiko_trainer.analyze
"""
from __future__ import annotations

import hashlib
import sys
from dataclasses import dataclass
from pathlib import Path

from .config import DEFAULT_CONFIG
from .features import MapFeatures, extract_features
from .models import TaikoBeatmap, TaikoReplay
from .osr_parser import parse_osr_file
from .osu_parser import parse_osu_file
from .scoring import DimensionRating, rate_map


REFERENCES = Path(__file__).resolve().parents[2] / "references"
DIMENSIONS = ("speed", "stamina", "gimmick", "technical", "consistency")


@dataclass
class Pair:
    dimension: str
    replay_path: Path
    beatmap_path: Path
    beatmap: TaikoBeatmap
    replay: TaikoReplay
    features: MapFeatures
    rating: DimensionRating
    md5_match: bool


def _md5(path: Path) -> str:
    return hashlib.md5(path.read_bytes()).hexdigest()


def _find_pair(dim_dir: Path) -> Pair | None:
    osr_files = list(dim_dir.glob("*.osr"))
    if not osr_files:
        print(f"WARN [{dim_dir.name}]: no .osr found", file=sys.stderr)
        return None
    osr_path = osr_files[0]

    mapset_dirs = [p for p in dim_dir.iterdir() if p.is_dir()]
    if not mapset_dirs:
        print(f"WARN [{dim_dir.name}]: no mapset folder", file=sys.stderr)
        return None
    mapset_dir = mapset_dirs[0]

    replay = parse_osr_file(osr_path)
    target_hash = replay.meta.beatmap_md5.lower()

    # First pass: try to match by MD5 (exact same file the replay was played on).
    osu_candidates = list(mapset_dir.glob("*.osu"))
    matched: Path | None = None
    for osu in osu_candidates:
        if _md5(osu) == target_hash:
            matched = osu
            break

    if matched is None:
        # Fallback: match by the diff name in the replay filename.
        # Replay filename shape: "<Player> - <Artist> - <Title> [Diff] (Date) Taiko.osr"
        bracket = _extract_bracket(osr_path.stem)
        if bracket:
            for osu in osu_candidates:
                if f"[{bracket}]" in osu.name:
                    matched = osu
                    break
        if matched is None:
            print(
                f"WARN [{dim_dir.name}]: no .osu matches replay {osr_path.name}; "
                f"target md5={target_hash}",
                file=sys.stderr,
            )
            return None

    beatmap = parse_osu_file(matched)
    features = extract_features(beatmap)
    rating = rate_map(features, od=beatmap.difficulty.overall_difficulty)
    return Pair(
        dimension=dim_dir.name,
        replay_path=osr_path,
        beatmap_path=matched,
        beatmap=beatmap,
        replay=replay,
        features=features,
        rating=rating,
        md5_match=(beatmap.beatmap_md5.lower() == target_hash),
    )


def _extract_bracket(stem: str) -> str | None:
    # Rightmost "[...]" is usually the diff. (Titles occasionally have brackets, so scan right-to-left.)
    end = stem.rfind("]")
    if end == -1:
        return None
    start = stem.rfind("[", 0, end)
    if start == -1:
        return None
    return stem[start + 1:end]


def _accuracy(replay: TaikoReplay) -> float:
    m = replay.meta
    total = m.count_300 + m.count_100 + m.count_miss
    if total == 0:
        return 0.0
    return (m.count_300 + 0.5 * m.count_100) / total


# --- printing ---------------------------------------------------------------

def _fmt(v, spec=""):
    if isinstance(v, float):
        return f"{v:{spec or '.2f'}}"
    if isinstance(v, int):
        return f"{v:{spec or 'd'}}"
    return str(v)


def _print_table(rows, headers):
    col_widths = [len(str(h)) for h in headers]
    str_rows = []
    for row in rows:
        str_row = [str(c) for c in row]
        for i, c in enumerate(str_row):
            col_widths[i] = max(col_widths[i], len(c))
        str_rows.append(str_row)

    def _line(cells):
        return "  ".join(c.ljust(col_widths[i]) for i, c in enumerate(cells))

    print(_line([str(h) for h in headers]))
    print("  ".join("-" * w for w in col_widths))
    for r in str_rows:
        print(_line(r))


def main() -> int:
    pairs: list[Pair] = []
    for dim in DIMENSIONS:
        d = REFERENCES / dim
        if not d.exists():
            print(f"WARN: missing {d}", file=sys.stderr)
            continue
        pair = _find_pair(d)
        if pair:
            pairs.append(pair)

    if not pairs:
        print("ERROR: no dimension pairs found under references/", file=sys.stderr)
        return 1

    # Header row: one column per dimension.
    dim_names = [p.dimension for p in pairs]

    # Setup section: which map/replay we picked for each dimension.
    print(f"== setup  (player style: {DEFAULT_CONFIG.style.name})")
    for p in pairs:
        print(f"  [{p.dimension:<12}] {p.beatmap.meta.artist} - {p.beatmap.meta.title} "
              f"[{p.beatmap.meta.version}] mapped by {p.beatmap.meta.creator}")
        print(f"  {'':<14} replay: {p.replay.meta.player}  "
              f"score={p.replay.meta.score}  "
              f"acc={_accuracy(p.replay)*100:.2f}%  "
              f"miss={p.replay.meta.count_miss}  "
              f"md5_match={p.md5_match}")
    print()

    # A single wide comparison table.
    rows = []

    def _add(label, extractor, spec=".2f"):
        rows.append([label] + [_fmt(extractor(p), spec) for p in pairs])

    _add("hittable notes", lambda p: p.features.hittable_notes, "d")
    _add("drumrolls", lambda p: p.features.drumroll_notes, "d")
    _add("dendens", lambda p: p.features.denden_notes, "d")
    _add("big-note ratio", lambda p: p.features.big_note_ratio * 100, ".1f")
    _add("duration (s)", lambda p: p.features.density.duration_s, ".1f")

    rows.append([""] + [""] * len(pairs))

    _add("avg NPS", lambda p: p.features.density.avg_nps, ".2f")
    _add("peak NPS (1s)", lambda p: p.features.density.peak_nps, ".1f")
    _add("p95 NPS (1s)", lambda p: p.features.density.p95_nps, ".2f")
    _add("peak NPS (5s avg)", lambda p: p.features.density.peak_nps_5s, ".2f")
    _add("high-density ratio", lambda p: p.features.density.high_density_ratio, ".3f")
    _add("longest sustained (s)", lambda p: p.features.density.longest_sustained_high_s, ".0f")
    _add("section NPS stddev (30s)", lambda p: p.features.density.section_nps_stddev_30s, ".2f")

    rows.append([""] + [""] * len(pairs))

    _add("distinct BPMs", lambda p: p.features.movement.distinct_bpm_count, "d")
    _add("BPM min", lambda p: p.features.movement.bpm_min, ".1f")
    _add("BPM max", lambda p: p.features.movement.bpm_max, ".1f")
    _add("distinct SVs", lambda p: p.features.movement.distinct_sv_count, "d")
    _add("SV min / max", lambda p: f"{p.features.movement.sv_min:.2f} / {p.features.movement.sv_max:.2f}", "")
    _add("SV stddev", lambda p: p.features.movement.sv_stddev, ".3f")
    _add("SV changes/min", lambda p: p.features.movement.sv_changes_per_minute, ".2f")

    rows.append([""] + [""] * len(pairs))

    _add("don ratio", lambda p: p.features.color.don_ratio * 100, ".1f")
    _add("color-change ratio", lambda p: p.features.color.color_change_ratio, ".3f")
    _add("mean run length", lambda p: p.features.color.run_length_mean, ".2f")
    _add("max run length", lambda p: p.features.color.run_length_max, "d")
    _add("run-length entropy (bits)", lambda p: p.features.color.run_length_entropy_bits, ".2f")
    _add("mono-stream ratio (>=5)", lambda p: p.features.color.mono_stream_ratio, ".3f")

    rows.append([""] + [""] * len(pairs))

    _add("dominant divisor", lambda p: f"{p.features.rhythm.dominant_divisor} ({p.features.rhythm.dominant_divisor_share*100:.0f}%)", "")
    _add("off-grid ratio", lambda p: p.features.rhythm.off_grid_ratio, ".3f")
    _add("divisor entropy (bits)", lambda p: p.features.rhythm.divisor_entropy_bits, ".2f")
    _add("IOI median (ms)", lambda p: p.features.rhythm.ioi_median_ms, ".1f")
    _add("IOI CoV", lambda p: p.features.rhythm.ioi_cov, ".3f")

    rows.append([""] + [""] * len(pairs))

    _add("replay accuracy (%)", lambda p: _accuracy(p.replay) * 100, ".2f")
    _add("replay misses", lambda p: p.replay.meta.count_miss, "d")
    _add("replay max combo", lambda p: p.replay.meta.max_combo, "d")

    _print_table(rows, headers=["feature"] + dim_names)

    print()
    _print_rating_matrix(pairs)
    return 0


def _print_rating_matrix(pairs: list[Pair]) -> None:
    print("== 5-D map ratings (pp-inspired unbounded scale; diagonal should be the winner)")
    print()
    dims_ordered = ["speed", "stamina", "gimmick", "technical", "consistency"]
    # Rows = maps (labelled by which dimension folder they came from).
    # Cols = the 5 rating dimensions.
    header = ["map / rating ->"] + dims_ordered
    rows = []
    for p in pairs:
        r = p.rating.as_dict()
        cells = [p.dimension]
        for dim in dims_ordered:
            v = r[dim]
            marker = " *" if dim == p.dimension else "  "
            cells.append(f"{v:6.0f}{marker}")
        rows.append(cells)
    _print_table(rows, headers=header)
    print()
    print("  '*' marks the dimension the map was labelled with. Ideally that column is the row's max.")
    for p in pairs:
        r = p.rating.as_dict()
        winner = p.rating.dominant()
        ok = winner == p.dimension
        winning_val = r[winner]
        labelled_val = r[p.dimension]
        gap = winning_val - labelled_val
        mark = "OK " if ok else "MISS"
        if ok:
            print(f"  [{mark}] {p.dimension:<12} winner={winner} ({winning_val:.0f})")
        else:
            print(f"  [{mark}] {p.dimension:<12} winner={winner} ({winning_val:.0f}) vs labelled ({labelled_val:.0f}), gap={gap:.0f}")


if __name__ == "__main__":
    raise SystemExit(main())

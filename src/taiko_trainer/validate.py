"""Reference-map validator.

Runs after any scoring/feature change. Loads each labelled reference map,
computes its 5-D rating, and asserts:

1. The labelled dimension is the row's maximum (diagonal winner)
2. The winning value is at least MIN_DOMINANCE points above the row's runner-up

Prints a pass/fail table and exits with non-zero status on failure — so this
can be wired into a git pre-commit hook or CI later.

    uv run python -m taiko_trainer.validate
"""
from __future__ import annotations

import hashlib
import sys
from pathlib import Path

from .features import extract_features
from .osu_parser import parse_osu_file
from .osr_parser import parse_osr_file
from .scoring import rate_map


REFERENCES = Path(__file__).resolve().parents[2] / "references"
DIMENSIONS = ("speed", "stamina", "gimmick", "technical", "consistency")
MIN_DOMINANCE = 40   # winning dim must beat runner-up by this many rating points


def _find_pair(dim_dir: Path) -> tuple[Path | None, Path | None]:
    """Return (osu_path, osr_path) for the labelled reference in dim_dir."""
    osr_files = list(dim_dir.glob("*.osr"))
    if not osr_files:
        return (None, None)
    osr = osr_files[0]
    mapset_dirs = [p for p in dim_dir.iterdir() if p.is_dir()]
    if not mapset_dirs:
        return (None, osr)
    mapset = mapset_dirs[0]

    rp = parse_osr_file(osr)
    target = rp.meta.beatmap_md5.lower()
    for cand in mapset.glob("*.osu"):
        if hashlib.md5(cand.read_bytes()).hexdigest() == target:
            return (cand, osr)
    # Fallback by diff name in replay filename
    end = osr.stem.rfind("]")
    start = osr.stem.rfind("[", 0, end) if end > 0 else -1
    if end > 0 and start >= 0:
        diff = osr.stem[start + 1: end]
        for cand in mapset.glob("*.osu"):
            if f"[{diff}]" in cand.name:
                return (cand, osr)
    return (None, osr)


def validate() -> int:
    """Return 0 on all-pass, 1 on any failure."""
    print("== reference-map validator ==")
    print(f"  min dominance: {MIN_DOMINANCE} pts (winner must beat runner-up by this)")
    print()

    failures = 0
    header = f"{'label':13} {'winner':13} {'delta':>7}     ratings"
    print(header)
    print("-" * len(header))

    for dim in DIMENSIONS:
        dim_dir = REFERENCES / dim
        if not dim_dir.exists():
            print(f"  MISSING: {dim} directory not found under {REFERENCES}")
            failures += 1
            continue
        osu_path, osr_path = _find_pair(dim_dir)
        if not osu_path:
            print(f"  MISSING: could not match .osu to .osr in {dim}")
            failures += 1
            continue
        bm = parse_osu_file(osu_path)
        f = extract_features(bm)
        r = rate_map(f).as_dict()
        winner = max(r.items(), key=lambda kv: kv[1])[0]
        winner_val = r[winner]
        # runner-up = second-highest value
        sorted_vals = sorted(r.values(), reverse=True)
        runner_up = sorted_vals[1] if len(sorted_vals) > 1 else 0
        delta = winner_val - runner_up

        ratings_str = " ".join(f"{d}={r[d]:.0f}" for d in DIMENSIONS)
        status = "OK  " if (winner == dim and delta >= MIN_DOMINANCE) else "FAIL"
        if status == "FAIL":
            failures += 1
        print(f"  {dim:11} {winner:13} {delta:>5.0f}  [{status}]  {ratings_str}")

    print()
    if failures == 0:
        print(f"ALL PASS ({len(DIMENSIONS)} references)")
        return 0
    print(f"{failures} FAIL(S) — scoring change broke a reference-map diagonal")
    return 1


def main() -> None:
    sys.exit(validate())


if __name__ == "__main__":
    main()

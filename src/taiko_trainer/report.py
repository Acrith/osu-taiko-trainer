"""Training report — cross-replay analysis for a single player."""
from __future__ import annotations

import json
import sqlite3
import sys
from dataclasses import dataclass

from .db import (
    get_all_maps,
    get_latest_snapshot,
    get_player,
    get_replays,
    get_snapshot_before,
    get_snapshot_history,
    open_plays,
)
from .player import PlayerSkill, _ACC_FLOOR, _ACC_CEIL, _DECAY, _accuracy_scaling
from .sessions import Session, group_sessions
from .suggest import MapSuggestion, find_weakest_dim, suggest_maps


@dataclass(frozen=True)
class WeaknessCluster:
    """A pattern signature the player misses on disproportionately, evidenced
    across their play history (deduped per-map so we only look at their best
    play of each map, matching the skill-vector semantics)."""
    cause: str                     # primary FailureCause (technical, gimmick, etc.)
    signature: str                 # human-readable pattern description
    miss_count: int                # total misses in this cluster across best plays
    maps: tuple[tuple[str, str, int], ...]  # (title, version, replay_id) tuples showing the maps


@dataclass(frozen=True)
class SkillContribution:
    map_title: str
    map_diff: str
    map_md5: str
    replay_id: int
    accuracy: float
    raw_rating: float       # the map's rating for this dim
    weighted: float         # what this replay actually contributed to the skill sum


@dataclass(frozen=True)
class TrainingReport:
    player: str
    style: str
    skill: PlayerSkill
    skill_delta: dict[str, float] | None
    prev_snapshot_at: str | None
    weakest_dim: str
    replays: int
    total_misses: int
    misses_by_cause: dict[str, int]
    avg_delta_stddev_ms: float
    avg_cheese_rate: float
    suggestions: tuple[MapSuggestion, ...]
    latest_session: Session | None
    previous_session: Session | None
    dim_contributors: dict[str, tuple[SkillContribution, ...]] = None  # top-5 per dim
    snapshot_history: tuple[dict, ...] = ()  # snapshots oldest -> newest for the progression chart
    weakness_clusters: tuple[WeaknessCluster, ...] = ()  # top pattern signatures the player struggles with
    # osu! profile fields (populated from player_info if linked via /settings/osu-user)
    osu_user_id: int | None = None
    osu_username: str | None = None
    osu_avatar_url: str | None = None
    osu_cover_url: str | None = None
    osu_country_code: str | None = None
    osu_global_rank: int | None = None


def build_report(conn: sqlite3.Connection, top_n_maps: int = 5) -> TrainingReport | None:
    """Build a report from a plays-DB connection (with catalog ATTACHed)."""
    snap = get_latest_snapshot(conn)
    if not snap:
        return None

    # Player name + osu! profile linkage come from player_info.
    prow = conn.execute(
        "SELECT name, style, osu_user_id, osu_username, osu_avatar_url, "
        "osu_cover_url, osu_country_code, osu_global_rank FROM player_info LIMIT 1"
    ).fetchone()
    if not prow:
        return None
    player = prow["name"]
    style = prow["style"]

    skill = PlayerSkill(
        speed=snap["skill_speed"],
        stamina=snap["skill_stamina"],
        gimmick=snap["skill_gimmick"],
        technical=snap["skill_technical"],
        consistency=snap["skill_consistency"],
    )
    weakest = find_weakest_dim(skill)

    replays = get_replays(conn)
    played_md5s = {r["map_md5"] for r in replays}

    total_misses = 0
    misses_by_cause: dict[str, int] = {}
    delta_stddevs: list[float] = []
    cheese_rates: list[float] = []
    for r in replays:
        total_misses += r.get("count_miss") or 0
        if r.get("classification_json"):
            for cause, n in json.loads(r["classification_json"]).items():
                misses_by_cause[cause] = misses_by_cause.get(cause, 0) + n
        if r.get("delta_stddev_ms") is not None:
            delta_stddevs.append(r["delta_stddev_ms"])
        if r.get("cheese_rate") is not None:
            cheese_rates.append(r["cheese_rate"])

    avg_stddev = sum(delta_stddevs) / len(delta_stddevs) if delta_stddevs else 0.0
    avg_cheese = sum(cheese_rates) / len(cheese_rates) if cheese_rates else 0.0

    suggestions = suggest_maps(conn, skill, weakest, top_n=top_n_maps, exclude_md5s=played_md5s)

    sessions = group_sessions(replays)
    latest_session = sessions[0] if sessions else None
    previous_session = sessions[1] if len(sessions) > 1 else None

    # Snapshots table now has one row per training session (see db.snapshot_player_skill),
    # so history[0] = current session's state, history[1] = previous session's state.
    # Delta arrows show "since last time you sat down to play".
    history = get_snapshot_history(conn, limit=2)
    skill_delta = None
    prev_snapshot_at = None
    if len(history) >= 2:
        prev = history[1]
        skill_delta = {
            "speed":       skill.speed       - prev["skill_speed"],
            "stamina":     skill.stamina     - prev["skill_stamina"],
            "gimmick":     skill.gimmick     - prev["skill_gimmick"],
            "technical":   skill.technical   - prev["skill_technical"],
            "consistency": skill.consistency - prev["skill_consistency"],
        }
        prev_snapshot_at = prev["latest_replay_played_at"]

    # Full snapshot history (up to 30 sessions) — chart data.
    # Order by when you actually played (latest_replay_played_at), not when the
    # snapshot was computed, so a re-uploaded old replay doesn't jump to the end.
    full_history = get_snapshot_history(conn, limit=30)
    snapshot_history = tuple(sorted(full_history, key=lambda s: s["latest_replay_played_at"]))

    return TrainingReport(
        player=player,
        style=style,
        skill=skill,
        skill_delta=skill_delta,
        prev_snapshot_at=prev_snapshot_at,
        weakest_dim=weakest,
        replays=len(replays),
        total_misses=total_misses,
        misses_by_cause=misses_by_cause,
        avg_delta_stddev_ms=avg_stddev,
        avg_cheese_rate=avg_cheese,
        osu_user_id=prow["osu_user_id"],
        osu_username=prow["osu_username"],
        osu_avatar_url=prow["osu_avatar_url"],
        osu_cover_url=prow["osu_cover_url"],
        osu_country_code=prow["osu_country_code"],
        osu_global_rank=prow["osu_global_rank"],
        suggestions=tuple(suggestions),
        latest_session=latest_session,
        previous_session=previous_session,
        dim_contributors=_compute_dim_contributors(replays),
        snapshot_history=snapshot_history,
        weakness_clusters=_compute_weakness_clusters(replays),
    )


_DIMS = ("speed", "stamina", "gimmick", "technical", "consistency")


def _compute_dim_contributors(replays: list[dict], top_n: int = 5) -> dict[str, tuple[SkillContribution, ...]]:
    """Mirror player.compute_player_skill's per-dim weighting so the caller can show
    which replays drove each dimension's skill number. Rank is by desc contribution
    (map_rating * accuracy_scaling), weight = 0.9^rank.

    Per-map dedup: only the best attempt per map_md5 shows up in the list. Matches
    the dedup in compute_player_skill so the contributor list actually explains
    the skill number instead of showing duplicates that inflate the impression."""
    result: dict[str, tuple[SkillContribution, ...]] = {}
    for dim in _DIMS:
        rating_col = f"rating_{dim}"
        # Best replay per map for THIS dim: (contribution, replay-row).
        best_per_map: dict[str, tuple[float, dict]] = {}
        for r in replays:
            rating = r.get(rating_col) or 0.0
            acc = r.get("accuracy_judged") or 0.0
            scale = _accuracy_scaling(acc)
            contribution = rating * scale
            if contribution <= 0:
                continue
            md5 = r.get("map_md5") or ""
            if contribution > best_per_map.get(md5, (0.0, None))[0]:
                best_per_map[md5] = (contribution, r)
        candidates = sorted(best_per_map.values(), key=lambda kv: -kv[0])
        contribs: list[SkillContribution] = []
        for rank, (contribution, r) in enumerate(candidates[:top_n]):
            weight = _DECAY ** rank
            contribs.append(SkillContribution(
                map_title=r.get("map_title") or "?",
                map_diff=r.get("map_version") or "?",
                map_md5=r.get("map_md5") or "",
                replay_id=int(r["id"]),
                accuracy=r.get("accuracy_judged") or 0.0,
                raw_rating=r.get(rating_col) or 0.0,
                weighted=contribution * weight,
            ))
        result[dim] = tuple(contribs)
    return result


def _bpm_band(bpm: float) -> str:
    if bpm < 140: return "<140"
    if bpm < 160: return "140-160"
    if bpm < 180: return "160-180"
    if bpm < 200: return "180-200"
    if bpm < 220: return "200-220"
    return "220+"


def _run_len_band(n: int) -> str:
    if n <= 1: return "1"
    if n <= 3: return "2-3"
    if n <= 5: return "4-5"
    if n <= 7: return "6-7"
    return "8+"


def _pattern_signature(cause: str, m: dict) -> str:
    """Turn a per-miss record into a human-readable pattern signature. The
    signature drives the group-by for weakness clustering — misses that share
    a signature roll up into one cluster."""
    bpm_b = _bpm_band(m.get("bpm", 0))
    prev = m.get("prev_div") or "?"
    nxt = m.get("next_div") or "?"
    rl = _run_len_band(m.get("run_len", 0))
    if cause == "technical":
        # For technical: focus on hard divisor around the miss
        hard = ""
        if prev in ("1/6", "1/3", "1/8", "1/12"): hard = prev
        elif nxt in ("1/6", "1/3", "1/8", "1/12"): hard = nxt
        return f"{hard or 'mixed'} at {bpm_b} BPM" if hard else f"hard-divisor at {bpm_b} BPM"
    if cause == "pattern_parity":
        return f"length-{rl} run at {bpm_b} BPM"
    if cause == "gimmick":
        return f"SV shift at {bpm_b} BPM"
    if cause == "stamina":
        return f"length-{rl} at {bpm_b} BPM (late-map)"
    if cause == "speed":
        return f"1/4 at {bpm_b} BPM"
    if cause == "wrong_color":
        c = m.get("color", "?")
        return f"{c} on length-{rl} at {bpm_b} BPM"
    if cause == "consistency":
        return f"drift on 1/4 at {bpm_b} BPM"
    return f"{bpm_b} BPM · {prev}→{nxt}"


def _compute_weakness_clusters(replays: list[dict], top_n: int = 8) -> tuple:
    """For each map, keep only the best-acc replay (matches the skill-vector
    dedup). Aggregate the miss patterns across best plays by (cause, signature),
    surface the top-N clusters by miss count.

    The insight: your BEST play of each map is a stable data point about your
    skill. If you consistently miss the same pattern signature across your best
    plays on multiple maps, that's a genuine weakness worth training."""
    import json as _json
    best_per_map: dict[str, dict] = {}
    for r in replays:
        md5 = r.get("map_md5") or ""
        acc = r.get("accuracy_judged") or 0.0
        cur = best_per_map.get(md5)
        if cur is None or acc > (cur.get("accuracy_judged") or 0.0):
            best_per_map[md5] = r

    # Aggregate: (cause, signature) -> [miss records + originating replay refs]
    clusters: dict[tuple[str, str], dict] = {}
    for r in best_per_map.values():
        raw = r.get("miss_patterns_json")
        if not raw:
            continue
        try:
            patterns = _json.loads(raw)
        except Exception:
            continue
        for m in patterns:
            cause = m.get("cause") or "unknown"
            sig = _pattern_signature(cause, m)
            key = (cause, sig)
            slot = clusters.setdefault(key, {"count": 0, "maps": {}})
            slot["count"] += 1
            map_key = (r.get("map_title") or "?", r.get("map_version") or "?", int(r["id"]))
            slot["maps"][map_key] = slot["maps"].get(map_key, 0) + 1

    # Sort by miss count desc; return top-N.
    ordered = sorted(clusters.items(), key=lambda kv: -kv[1]["count"])[:top_n]
    return tuple(
        WeaknessCluster(
            cause=cause,
            signature=sig,
            miss_count=data["count"],
            maps=tuple(k for k, _ in sorted(data["maps"].items(), key=lambda kv: -kv[1])[:5]),
        )
        for (cause, sig), data in ordered
    )


_STYLE_LABELS = {
    "kddk": "KDDK (outer=kat, inner=don, L-R alternation)",
    "ddkk": "DDKK (color-per-hand: L=don, R=kat)",
    "kkdd": "KKDD (color-per-hand: L=kat, R=don)",
    "unknown": "unknown (set with: taiko-trainer player <workspace> <name> <style>)",
}


def _interpret_cheese(style: str, rate: float) -> str:
    if style == "kddk":
        if rate < 0.02: return "clean alternation (expected for KDDK)"
        if rate < 0.05: return "occasional cheese (fast bursts)"
        if rate < 0.10: return "moderate cheese (waves/single-hand)"
        return "heavy cheese — often bypassing alternation with speed"
    if style in ("ddkk", "kkdd"):
        if rate < 0.05: return "unusually clean for color-per-hand — many mixed-color runs"
        if rate < 0.20: return "typical for color-per-hand"
        return "very high, expected on heavy mono content"
    if rate < 0.02: return "clean alternation (looks like KDDK)"
    if rate < 0.10: return "moderate — could be KDDK bursts or partial waves"
    return "high — likely color-per-hand (DDKK/KKDD) or heavy cheese"


def _fmt_delta(v: float, unit: str = "", flip: bool = False) -> str:
    if abs(v) < 0.005:
        return "   · unchanged"
    up = v > 0
    arrow = "↑" if up else "↓"
    good = (up and not flip) or (not up and flip)
    marker = "✓" if good else "✗"
    return f"  {arrow}{v:+.2f}{unit} {marker}"


def _print_session_summary(report: TrainingReport) -> None:
    latest = report.latest_session
    prev = report.previous_session
    if latest is None:
        return

    print(f"LATEST SESSION ({latest.start})")
    print(f"  {len(latest.replays)} replays played  ·  {latest.total_misses} misses over {latest.total_notes} notes")
    print(f"  accuracy (note-weighted):    {latest.weighted_accuracy*100:>6.2f}%",
          end=(f"  {_fmt_delta((latest.weighted_accuracy - prev.weighted_accuracy) * 100, '%')}" if prev else "\n"))
    if prev:
        print()
    print(f"  avg delta σ:                 {latest.avg_delta_stddev_ms:>6.1f} ms",
          end=(f"  {_fmt_delta(latest.avg_delta_stddev_ms - prev.avg_delta_stddev_ms, ' ms', flip=True)}" if prev else "\n"))
    if prev:
        print()
    print(f"  cheese rate:                 {latest.avg_cheese_rate*100:>6.2f}%",
          end=(f"  {_fmt_delta((latest.avg_cheese_rate - prev.avg_cheese_rate) * 100, '%', flip=(report.style == 'kddk'))}" if prev else "\n"))
    if prev:
        print()

    if latest.misses_by_cause:
        print()
        print("  dominant miss causes (session):")
        total = sum(latest.misses_by_cause.values())
        top = sorted(latest.misses_by_cause.items(), key=lambda kv: -kv[1])[:4]
        for cause, count in top:
            if count == 0:
                continue
            pct = count / total * 100 if total else 0
            delta_str = ""
            if prev and prev.misses_by_cause:
                prev_total = sum(prev.misses_by_cause.values()) or 1
                prev_pct = prev.misses_by_cause.get(cause, 0) / prev_total * 100
                delta_str = _fmt_delta(pct - prev_pct, "%", flip=True)
            print(f"    {cause:15} {count:>5}  ({pct:>5.1f}%){delta_str}")

    if prev:
        print()
        print(f"  compared to session at {prev.start} ({len(prev.replays)} replays)")


def print_report(report: TrainingReport) -> None:
    print(f"== TRAINING REPORT: {report.player} ==")
    print(f"  {report.replays} replays analysed  ·  {report.total_misses} total misses")
    print(f"  playstyle: {_STYLE_LABELS.get(report.style, report.style)}")
    print()

    print("SKILL VECTOR")
    d = report.skill.as_dict()
    ordered = sorted(d.items(), key=lambda kv: -kv[1])
    for i, (dim, v) in enumerate(ordered):
        marker = "  *" if dim == report.weakest_dim else "   "
        rank_marker = "★" if i == 0 else " "
        delta_str = ""
        if report.skill_delta is not None:
            delta = report.skill_delta[dim]
            if abs(delta) < 0.5:
                delta_str = "   ·"
            elif delta > 0:
                delta_str = f"  ↑{delta:>+5.0f}"
            else:
                delta_str = f"  ↓{delta:>+5.0f}"
        print(f"  {marker}{rank_marker} {dim:13} {v:>7.0f}{delta_str}")
    if report.skill_delta is not None:
        print(f"  '*' = weakest dimension  ·  delta = change since previous snapshot ({report.prev_snapshot_at})")
    else:
        print(f"  '*' = weakest dimension = training target  ·  (no previous snapshot to compare)")
    print()

    print("DOMINANT FAILURE CAUSES (across all replays)")
    if report.misses_by_cause:
        total = sum(report.misses_by_cause.values())
        ordered_causes = sorted(report.misses_by_cause.items(), key=lambda kv: -kv[1])
        for cause, count in ordered_causes:
            if count == 0:
                continue
            pct = count / total * 100 if total else 0
            bar = "#" * int(pct / 2)
            print(f"  {cause:15} {count:>5}  ({pct:>5.1f}%)  {bar}")
    else:
        print("  (no misses classified)")
    print()

    print("TIMING PROFILE (all replays)")
    print(f"  avg delta σ across replays:  {report.avg_delta_stddev_ms:>6.1f} ms")
    interp = _interpret_cheese(report.style, report.avg_cheese_rate)
    print(f"  avg cheese rate:             {report.avg_cheese_rate*100:>6.2f}%  ({interp})")
    print()

    if report.latest_session:
        _print_session_summary(report)
        print()

    print(f"SUGGESTED MAPS TO PUSH {report.weakest_dim.upper()}")
    if not report.suggestions:
        print(f"  (no maps in DB — ingest more before suggestions can be made)")
    else:
        for i, s in enumerate(report.suggestions, 1):
            fit = "excellent" if s.suggestion_score > 0.75 else "good" if s.suggestion_score > 0.4 else "modest" if s.suggestion_score > 0.1 else "poor"
            gain_arrow = f"{s.target_gain_frac*100:+.0f}%"
            print(f"  {i}. {s.title[:35]:35} [{s.version[:22]:22}] by {s.creator[:15]}")
            print(f"       rating[{s.target_dim}]={s.target_rating:.0f}  growth={gain_arrow}  fit={fit}  score={s.suggestion_score:.2f}")
    print()


def main() -> None:
    if len(sys.argv) < 3:
        print("usage: python -m taiko_trainer.report <WORKSPACE> <PLAYER>", file=sys.stderr)
        sys.exit(1)
    workspace = sys.argv[1]
    player = sys.argv[2]
    conn = open_plays(workspace, player)
    report = build_report(conn)
    conn.close()
    if report is None:
        print(f"ERROR: no snapshot for {player!r} in workspace {workspace}", file=sys.stderr)
        sys.exit(1)
    print_report(report)


if __name__ == "__main__":
    main()

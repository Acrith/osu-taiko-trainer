"""Session grouping — bucket a player's replays into training sessions.

A "session" is a contiguous run of replays where each replay was played within
`GAP_HOURS` of the previous one. Anything with a larger gap starts a new
session. This lets the report answer "how did I do THIS training session vs
LAST time I sat down to play?" instead of only showing all-time aggregates.

Aggregate stats per session:
- replay_count
- total_notes / total_misses
- weighted-mean accuracy (weighted by replay note count)
- avg delta σ
- avg cheese rate
- misses_by_cause (summed across all replays in the session)
- dominant miss cause
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

GAP_HOURS = 4  # gap larger than this starts a new session


@dataclass(frozen=True)
class Session:
    start: str                                # ISO timestamp of earliest replay
    end: str                                  # ISO timestamp of latest replay
    replays: tuple[dict[str, Any], ...]       # raw replay rows in this session
    total_notes: int
    total_misses: int
    weighted_accuracy: float
    avg_delta_stddev_ms: float
    avg_cheese_rate: float
    misses_by_cause: dict[str, int]


def _parse_ts(s: str) -> datetime:
    # SQLite ISO timestamps may or may not include timezone. Try a couple.
    try:
        return datetime.fromisoformat(s)
    except ValueError:
        # Strip tz suffix if present
        return datetime.fromisoformat(s.split("+")[0].rstrip("Z"))


def group_sessions(replays: list[dict[str, Any]], gap_hours: float = GAP_HOURS) -> list[Session]:
    """Return sessions ordered NEWEST first. Each session's replays sorted oldest→newest."""
    if not replays:
        return []
    # Sort ascending by played_at so we can walk in time order.
    ordered = sorted(replays, key=lambda r: r["played_at"])
    gap = timedelta(hours=gap_hours)

    buckets: list[list[dict[str, Any]]] = []
    current: list[dict[str, Any]] = [ordered[0]]
    prev_ts = _parse_ts(ordered[0]["played_at"])
    for r in ordered[1:]:
        ts = _parse_ts(r["played_at"])
        if ts - prev_ts > gap:
            buckets.append(current)
            current = [r]
        else:
            current.append(r)
        prev_ts = ts
    buckets.append(current)

    sessions: list[Session] = []
    for group in buckets:
        note_totals = [(r["count_great"] or 0) + (r["count_ok"] or 0) + (r["count_miss"] or 0) for r in group]
        total_notes = sum(note_totals)
        total_misses = sum(r["count_miss"] or 0 for r in group)
        # Note-weighted accuracy — a 3000-note replay counts more than a 300-note one.
        num = sum((r["accuracy_judged"] or 0) * n for r, n in zip(group, note_totals))
        weighted_acc = num / total_notes if total_notes else 0.0

        stddevs = [r["delta_stddev_ms"] for r in group if r["delta_stddev_ms"] is not None]
        cheese_rates = [r["cheese_rate"] for r in group if r["cheese_rate"] is not None]
        avg_stddev = sum(stddevs) / len(stddevs) if stddevs else 0.0
        avg_cheese = sum(cheese_rates) / len(cheese_rates) if cheese_rates else 0.0

        misses_by_cause: dict[str, int] = {}
        for r in group:
            if not r["classification_json"]:
                continue
            for cause, n in json.loads(r["classification_json"]).items():
                misses_by_cause[cause] = misses_by_cause.get(cause, 0) + n

        sessions.append(Session(
            start=group[0]["played_at"],
            end=group[-1]["played_at"],
            replays=tuple(group),
            total_notes=total_notes,
            total_misses=total_misses,
            weighted_accuracy=weighted_acc,
            avg_delta_stddev_ms=avg_stddev,
            avg_cheese_rate=avg_cheese,
            misses_by_cause=misses_by_cause,
        ))

    # Sort sessions newest first.
    sessions.sort(key=lambda s: s.start, reverse=True)
    return sessions

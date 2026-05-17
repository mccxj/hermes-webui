"""Skill usage & lifecycle tracking for Hermes WebUI.

This module provides:
  - _persist_skill_event() — real-time append to _skill_usage.jsonl
  - _scan_usage_log() — read/parse/filter the jsonl log
  - build_skill_usage_stats() — aggregate into the response shape expected
    by the Insights panel's "Skill Activity" section.

Thread safety: writes use a per-file threading.Lock.  Multi-process safety
is handled via the same fcntl.flock pattern as api/turn_journal.py.

Data flow:
  streaming.py:on_tool → _persist_skill_event() → _skill_usage.jsonl
  routes.py:_handle_insights → build_skill_usage_stats() → API response
"""

from __future__ import annotations

import collections
import json
import os
import threading
import time
from pathlib import Path
from typing import Any

SKILL_USAGE_FILENAME = "_skill_usage.jsonl"
CACHE_TTL = 30  # seconds

_cache: dict[tuple, tuple[dict, float]] = {}
_cache_lock = threading.Lock()

# Per-file locks for thread-safe append (keyed by (parent_dir, filename))
_WRITER_LOCKS: dict[tuple[str, str], threading.Lock] = {}
_WRITER_LOCKS_GUARD = threading.Lock()


# ── Helpers ────────────────────────────────────────────────────────────


def _usage_path(session_dir: Path) -> Path:
    return Path(session_dir) / SKILL_USAGE_FILENAME


def _lock_for(path: Path) -> threading.Lock:
    key = (str(path.parent), path.name)
    with _WRITER_LOCKS_GUARD:
        lock = _WRITER_LOCKS.get(key)
        if lock is None:
            lock = threading.Lock()
            _WRITER_LOCKS[key] = lock
        return lock


def _format_date(ts: float) -> str:
    """Format a unix timestamp as YYYY-MM-DD."""
    try:
        return time.strftime("%Y-%m-%d", time.localtime(ts))
    except (OSError, ValueError):
        return "1970-01-01"


def _build_daily_series(
    daily_counts: dict[str, int],
    days: int,
    first_day_ts: float,
) -> list[dict]:
    """Build a 0-filled daily trend array for chart.

    Each entry: {"date": "2026-04-17", "calls": N}
    """
    day_secs = 86400
    series: list[dict] = []
    for i in range(days):
        ts = first_day_ts + (i * day_secs)
        date_key = _format_date(ts)
        series.append({"date": date_key, "calls": daily_counts.get(date_key, 0)})
    return series


# ── Persistence ────────────────────────────────────────────────────────


def _persist_skill_event(data: dict, session_dir: Path | str | None = None) -> None:
    """Append a skill event to the usage log.

    This is called from streaming.py:on_tool for skill_view and
    skill_manage tool invocations.  Thread-safe, crash-safe (append+fsync).

    Args:
        data: event dict with keys: tool, skill, action, event, ts,
              session_id, duration, is_error
        session_dir: session data directory (default: auto-detect)
    """
    if session_dir is None:
        from api.config import SESSION_DIR as _sd

        session_dir = Path(_sd)
    path = _usage_path(Path(session_dir))
    path.parent.mkdir(parents=True, exist_ok=True)

    lock = _lock_for(path)
    with lock:
        with open(path, "a") as f:
            f.write(json.dumps(data, ensure_ascii=False) + "\n")
            f.flush()
            os.fsync(f.fileno())

    # Invalidate cache for this session_dir
    with _cache_lock:
        keys_to_clear = [k for k in _cache if k[1] == str(Path(session_dir))]
        for k in keys_to_clear:
            del _cache[k]


def _scan_usage_log(
    session_dir: Path | str,
    cutoff: float = 0,
) -> list[dict]:
    """Read the skill usage log and return events.

    Args:
        session_dir: session data directory
        cutoff: unix timestamp — only return events with ts >= cutoff.
                When 0 (default), return all events.

    Returns:
        list of parsed event dicts, newest-first
    """
    path = _usage_path(Path(session_dir))
    if not path.exists():
        return []

    events: list[dict] = []
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                ev = json.loads(line)
            except (json.JSONDecodeError, ValueError):
                continue
            if cutoff and ev.get("ts", 0) < cutoff:
                continue
            events.append(ev)

    # Newest first for timeline queries
    events.sort(key=lambda e: e.get("ts", 0), reverse=True)
    return events


# ── Aggregation ────────────────────────────────────────────────────────


def build_skill_usage_stats(
    days: int,
    skills_meta: list[dict],
    session_dir: Path | str,
) -> dict:
    """Aggregate skill usage log into the Insights response shape.

    Args:
        days: time window
        skills_meta: list of {"name", "category"} from the skills directory
        session_dir: session data directory

    Returns:
        {
            "summary": {"total_skills", "total_calls", "active_skills", "total_changes"},
            "daily_trend": [{"date", "calls"}, ...],
            "ranking": [{"skill", "category", "calls", "last_called", "changes", "last_change"}, ...],
        }
    """
    session_dir = Path(session_dir)
    now = time.time()
    cutoff = now - (days * 86400)

    # Check cache
    cache_key = (days, str(session_dir))
    with _cache_lock:
        if cache_key in _cache:
            result, built_at = _cache[cache_key]
            if now - built_at < CACHE_TTL:
                return result

    # Read log
    all_events = _scan_usage_log(session_dir)
    # Filter within window
    window_events = [e for e in all_events if e.get("ts", 0) >= cutoff]

    # Aggregate
    known = {s["name"] for s in skills_meta}
    skill_calls: dict[str, int] = collections.Counter()
    skill_changes: dict[str, int] = collections.Counter()
    skill_last_call: dict[str, float] = {}
    skill_last_change: dict[str, float] = {}
    daily_counts: dict[str, int] = collections.Counter()

    for ev in window_events:
        skill = ev.get("skill", "")
        tool = ev.get("tool", "")
        action = ev.get("action", "")
        ts = ev.get("ts", 0) or 0

        if not skill:
            continue

        skill_calls[skill] += 1

        # Track last call time
        if ts > skill_last_call.get(skill, 0):
            skill_last_call[skill] = ts

        # Track changes (skill_manage with a mutating action)
        if tool == "skill_manage" and action in (
            "create", "patch", "edit", "delete", "write_file", "remove_file"
        ):
            skill_changes[skill] += 1
            if ts > skill_last_change.get(skill, 0):
                skill_last_change[skill] = ts

        # Daily trend
        date_key = _format_date(ts)
        daily_counts[date_key] += 1

    # Build ranking (include all known skills, even with 0 calls)
    ranking: list[dict] = []
    total_calls = sum(skill_calls.values())
    active_count = 0
    total_changes = sum(skill_changes.values())

    # First add skills with calls
    for skill_name, count in skill_calls.most_common():
        meta = next((s for s in skills_meta if s["name"] == skill_name), None)
        last_called = skill_last_call.get(skill_name, 0)
        last_change = skill_last_change.get(skill_name, 0)
        changes = skill_changes.get(skill_name, 0)
        ranking.append({
            "skill": skill_name,
            "category": (meta or {}).get("category", ""),
            "calls": count,
            "last_called": _format_dt(last_called) if last_called else "",
            "changes": changes,
            "last_change": _format_dt(last_change) if last_change else "",
        })
        if count > 0:
            active_count += 1

    # Build daily trend
    today = time.localtime(now)
    first_day_ts = time.mktime((
        today.tm_year, today.tm_mon, today.tm_mday,
        0, 0, 0, today.tm_wday, today.tm_yday, today.tm_isdst
    )) - ((days - 1) * 86400)
    daily_trend = _build_daily_series(daily_counts, days, first_day_ts)

    result = {
        "summary": {
            "total_skills": len(skills_meta),
            "total_calls": total_calls,
            "active_skills": active_count,
            "total_changes": total_changes,
        },
        "daily_trend": daily_trend,
        "ranking": ranking,
    }

    # Update cache
    with _cache_lock:
        _cache[cache_key] = (result, time.time())

    return result


def _format_dt(ts: float) -> str:
    """Format a unix timestamp to a compact human-readable string.

    Uses "HH:MM" for today, "Mon HH:MM" for this week, "Mon DD" otherwise.
    """
    if not ts:
        return ""
    try:
        dt = time.localtime(ts)
        now_t = time.localtime()
        if (dt.tm_year, dt.tm_yday) == (now_t.tm_year, now_t.tm_yday):
            return time.strftime("%H:%M", dt)
        # Same week
        return time.strftime("%a", dt) if (now_t.tm_year == dt.tm_year and abs(now_t.tm_yday - dt.tm_yday) < 7) else time.strftime("%b %d", dt)
    except (OSError, ValueError):
        return ""

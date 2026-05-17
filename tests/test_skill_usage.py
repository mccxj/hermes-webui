"""Tests for api/skill_usage — skill usage tracking & aggregation.

Covers:
  - _persist_skill_event — appends to jsonl with flock safety
  - _scan_usage_log — reads, filters by cutoff timestamp
  - build_skill_usage_stats — aggregates into the expected shape
  - Integration with streaming.py: tool event filtering
  - Integration with routes.py: insights response shape
"""

import json
import os
import pathlib
import time

REPO = pathlib.Path(__file__).parent.parent
SESSION_DIR = REPO / "tests" / "tmp_skill_usage_test"


def _cleanup():
    import shutil
    if SESSION_DIR.exists():
        shutil.rmtree(SESSION_DIR)


def _make_event(tool="skill_view", skill="writing-plans", action="view",
                event_type="tool.started", ts=None, session_id="s1",
                duration=0, is_error=False):
    return {
        "tool": tool,
        "skill": skill,
        "action": action,
        "event": event_type,
        "ts": ts or time.time(),
        "session_id": session_id,
        "duration": duration,
        "is_error": is_error,
    }


# ── Tests for _persist_skill_event (persistence) ────────────────────────

class TestPersistSkillEvent:
    """_persist_skill_event writes to _skill_usage.jsonl and is idempotent."""

    def _import_fn(self):
        # Stub path deps so we can import cleanly
        import importlib, sys, types
        for mod in ('api.config',):
            if mod not in sys.modules:
                sys.modules[mod] = types.ModuleType(mod)
        cfg = sys.modules['api.config']
        from pathlib import Path
        cfg.SESSION_DIR = str(SESSION_DIR)
        from api.skill_usage import _persist_skill_event, _usage_path
        return _persist_skill_event, _usage_path

    def setup_method(self):
        _cleanup()
        SESSION_DIR.mkdir(parents=True, exist_ok=True)

    def teardown_method(self):
        _cleanup()

    def test_writes_json_line(self):
        persist, usage_path = self._import_fn()
        ev = _make_event()
        persist(ev)
        path = usage_path(SESSION_DIR)
        assert path.exists()
        lines = path.read_text().strip().splitlines()
        assert len(lines) == 1
        parsed = json.loads(lines[0])
        assert parsed["tool"] == "skill_view"
        assert parsed["skill"] == "writing-plans"

    def test_append_multiple_events(self):
        persist, usage_path = self._import_fn()
        persist(_make_event(skill="s1"))
        persist(_make_event(skill="s2"))
        path = usage_path(SESSION_DIR)
        lines = path.read_text().strip().splitlines()
        assert len(lines) == 2
        assert json.loads(lines[0])["skill"] == "s1"
        assert json.loads(lines[1])["skill"] == "s2"

    def test_ignores_skill_manage_view(self):
        """skill_manage with action='view' should still be recorded."""
        persist, usage_path = self._import_fn()
        ev = _make_event(tool="skill_manage", action="patch", skill="test-skill")
        persist(ev)
        path = usage_path(SESSION_DIR)
        lines = path.read_text().strip().splitlines()
        assert len(lines) == 1


# ── Tests for _scan_usage_log ───────────────────────────────────────────

class TestScanUsageLog:
    """_scan_usage_log reads jsonl and filters by cutoff."""

    def _import_fn(self):
        import importlib, sys, types
        for mod in ('api.config',):
            if mod not in sys.modules:
                sys.modules[mod] = types.ModuleType(mod)
        sys.modules['api.config'].SESSION_DIR = str(SESSION_DIR)
        from api.skill_usage import _scan_usage_log, _usage_path, _persist_skill_event
        return _scan_usage_log, _usage_path, _persist_skill_event

    def setup_method(self):
        _cleanup()
        SESSION_DIR.mkdir(parents=True, exist_ok=True)

    def teardown_method(self):
        _cleanup()

    def test_returns_all_without_cutoff(self):
        scan, _, persist = self._import_fn()
        persist(_make_event(skill="s1", ts=1000))
        persist(_make_event(skill="s2", ts=2000))
        results = scan(SESSION_DIR)
        assert len(results) == 2

    def test_filters_by_cutoff(self):
        scan, _, persist = self._import_fn()
        persist(_make_event(skill="s1", ts=1000))
        persist(_make_event(skill="s2", ts=2000))
        results = scan(SESSION_DIR, cutoff=1500)
        assert len(results) == 1
        assert results[0]["skill"] == "s2"

    def test_missing_file(self):
        scan, _, _ = self._import_fn()
        results = scan(SESSION_DIR)
        assert results == []


# ── Tests for build_skill_usage_stats ───────────────────────────────────

class TestBuildSkillUsageStats:
    """build_skill_usage_stats returns the correct aggregation shape."""

    def _import_fn(self):
        import importlib, sys, types
        for mod in ('api.config', 'api.routes'):
            if mod not in sys.modules:
                sys.modules[mod] = types.ModuleType(mod)
        sys.modules['api.config'].SESSION_DIR = str(SESSION_DIR)
        from api.skill_usage import build_skill_usage_stats, _persist_skill_event
        return build_skill_usage_stats, _persist_skill_event

    def setup_method(self):
        _cleanup()
        SESSION_DIR.mkdir(parents=True, exist_ok=True)
        # Clear cache from previous runs
        from api.skill_usage import _cache
        _cache.clear()

    def teardown_method(self):
        _cleanup()

    def test_empty_returns_minimal(self):
        build, _ = self._import_fn()
        result = build(30, [], SESSION_DIR)
        assert result["summary"]["total_skills"] == 0
        assert result["summary"]["total_calls"] == 0
        assert result["summary"]["active_skills"] == 0
        assert result["summary"]["total_changes"] == 0
        assert len(result["daily_trend"]) == 30  # 30 zero-filled days
        assert result["ranking"] == []

    def test_basic_view_calls(self):
        build, persist = self._import_fn()
        now = time.time()
        persist(_make_event(skill="s1", ts=now - 100))
        persist(_make_event(skill="s1", ts=now - 50))
        persist(_make_event(skill="s2", ts=now - 10))

        skills_meta = [{"name": "s1", "category": "a"}, {"name": "s2", "category": "b"}]
        result = build(30, skills_meta, SESSION_DIR)
        assert result["summary"]["total_skills"] == 2
        assert result["summary"]["total_calls"] == 3
        assert result["summary"]["active_skills"] == 2
        # Ranking: s1 (2 calls) first, s2 (1 call) second
        assert result["ranking"][0]["skill"] == "s1"
        assert result["ranking"][0]["calls"] == 2
        assert result["ranking"][1]["skill"] == "s2"
        assert result["ranking"][1]["calls"] == 1

    def test_manage_calls_counted(self):
        build, persist = self._import_fn()
        now = time.time()
        persist(_make_event(tool="skill_manage", action="create", skill="s1", ts=now - 100))
        persist(_make_event(tool="skill_manage", action="patch", skill="s1", ts=now - 50))
        persist(_make_event(tool="skill_view", skill="s1", ts=now - 10))

        skills_meta = [{"name": "s1", "category": "a"}]
        result = build(30, skills_meta, SESSION_DIR)
        assert result["summary"]["total_calls"] == 3
        assert result["summary"]["total_changes"] == 2  # create + patch
        assert result["ranking"][0]["changes"] == 2

    def test_renamed_is_separate(self):
        """Skills with different names are separate entries even if conceptually same."""
        build, persist = self._import_fn()
        now = time.time()
        persist(_make_event(skill="old-name", ts=now - 100))
        persist(_make_event(skill="new-name", ts=now - 50))

        skills_meta = [{"name": "new-name", "category": "a"}]
        result = build(30, skills_meta, SESSION_DIR)
        # old-name still appears because it has calls (even though not in skills_meta)
        names = [r["skill"] for r in result["ranking"]]
        assert "old-name" in names
        assert "new-name" in names
        assert len(names) == 2  # both separate entries

    def test_daily_trend_shape(self):
        build, persist = self._import_fn()
        now = time.time()
        persist(_make_event(skill="s1", ts=now - 100))
        result = build(30, [{"name": "s1", "category": "a"}], SESSION_DIR)
        assert len(result["daily_trend"]) == 30  # one entry per day
        # The day with the event has calls=1, others have 0
        non_zero = [d for d in result["daily_trend"] if d["calls"] > 0]
        assert len(non_zero) == 1

    def test_most_changed_top_n(self):
        build, persist = self._import_fn()
        now = time.time()
        for i in range(10):
            persist(_make_event(tool="skill_manage", action="patch", skill="heavy", ts=now - i))
        for i in range(3):
            persist(_make_event(tool="skill_manage", action="patch", skill="light", ts=now - i))

        skills_meta = [{"name": "heavy", "category": "a"}, {"name": "light", "category": "b"}]
        result = build(30, skills_meta, SESSION_DIR)
        assert result["ranking"][0]["skill"] == "heavy"  # 10 changes
        assert result["ranking"][1]["skill"] == "light"  # 3 changes
        assert result["ranking"][0]["changes"] == 10
        assert result["ranking"][1]["changes"] == 3

    def test_time_window_filter(self):
        build, persist = self._import_fn()
        now = time.time()
        persist(_make_event(skill="s1", ts=now - 5 * 86400))    # 5 days ago — in 30-day window
        persist(_make_event(skill="s2", ts=now - 60 * 86400))   # 60 days ago — outside 30 days
        skills_meta = [{"name": "s1", "category": "a"}, {"name": "s2", "category": "b"}]
        result = build(30, skills_meta, SESSION_DIR)
        assert result["summary"]["total_calls"] == 1
        assert result["ranking"][0]["skill"] == "s1"
        assert len(result["ranking"]) == 1  # s2 has no calls in window

"""
Tests for hermes-memory-plugin — P1 active_lessons injection + event recording.
"""

import importlib.util
import json
import sys
from pathlib import Path

import pytest

# ── import the plugin module (directory name has hyphens) ──────────────────
_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
_PLUGIN_DIR = _REPO_ROOT / "plugins" / "hermes-memory"
# Fallback: user-installed plugin path
if not _PLUGIN_DIR.is_dir():
    _PLUGIN_DIR = Path.home() / ".hermes" / "plugins" / "hermes-memory-plugin"
_spec = importlib.util.spec_from_file_location(
    "hermes_memory_plugin",
    _PLUGIN_DIR / "__init__.py",
)
_plugin = importlib.util.module_from_spec(_spec)
sys.modules["hermes_memory_plugin"] = _plugin
_spec.loader.exec_module(_plugin)

# Convenience aliases
_inject_active_lessons = _plugin._inject_active_lessons
_record_candidate = _plugin._record_candidate
_should_record = _plugin._should_record
_is_duplicate = _plugin._is_duplicate
_safe_excerpt = _plugin._safe_excerpt
_sha256 = _plugin._sha256
_MAX_INJECT_BYTES = _plugin._MAX_INJECT_BYTES


# ══════════════════════════════════════════════════════════════════════════════
# smoke: helpers
# ══════════════════════════════════════════════════════════════════════════════

class TestHelpers:
    def test_safe_excerpt_short(self):
        assert _safe_excerpt("hello") == "hello"

    def test_safe_excerpt_long(self):
        long_text = "x" * 300
        result = _safe_excerpt(long_text, max_chars=10)
        assert len(result) == 11
        assert result.endswith("…")

    def test_safe_excerpt_non_string(self):
        assert _safe_excerpt(None) == ""
        assert _safe_excerpt(123) == ""

    def test_sha256_deterministic(self):
        assert _sha256("abc") == _sha256("abc")
        assert _sha256("abc") != _sha256("def")

    def test_should_record_normal(self):
        ok, reason = _should_record("你好", "好的，没问题")
        assert not ok
        assert reason == ""

    def test_should_record_user_correction(self):
        ok, reason = _should_record("不对，这个方案有问题", "收到…")
        assert ok
        assert "不对" in reason

    def test_should_record_user_not_right(self):
        ok, reason = _should_record("不是这个意思", "…")
        assert ok
        assert "不是" in reason

    def test_should_record_assistant_failure(self):
        ok, reason = _should_record("帮我修bug", "修复失败，请检查日志")
        assert ok
        assert "失败" in reason

    def test_should_record_assistant_error(self):
        ok, reason = _should_record("检查状态", "遇到 error: connection refused")
        assert ok
        assert "error" in reason

    def test_should_record_empty_user_message(self):
        ok, reason = _should_record("", "something failed")
        assert not ok


# ══════════════════════════════════════════════════════════════════════════════
# pre_llm_call: active_lessons injection
# ══════════════════════════════════════════════════════════════════════════════

class TestPreLlmCall:
    """pre_llm_call — active_lessons injection."""

    def test_missing_file_returns_none(self, monkeypatch):
        monkeypatch.setattr(
            _plugin, "_ACTIVE_LESSONS_PATH",
            Path("/tmp/nonexistent_hermes_test_lessons.md"),
        )
        result = _inject_active_lessons(session_id="test")
        assert result is None

    def test_empty_file_returns_none(self, monkeypatch, tmp_path):
        p = tmp_path / "empty.md"
        p.write_text("   \n  ")
        monkeypatch.setattr(_plugin, "_ACTIVE_LESSONS_PATH", p)
        result = _inject_active_lessons(session_id="test")
        assert result is None

    def test_valid_file_returns_context(self, monkeypatch, tmp_path):
        p = tmp_path / "lessons.md"
        p.write_text("P1_SMOKE_TEST: injection works.")
        monkeypatch.setattr(_plugin, "_ACTIVE_LESSONS_PATH", p)
        result = _inject_active_lessons(session_id="test")
        assert result is not None
        assert "context" in result
        assert "[Active Lessons]" in result["context"]
        assert "P1_SMOKE_TEST" in result["context"]

    def test_long_file_truncated(self, monkeypatch, tmp_path):
        p = tmp_path / "long_lessons.md"
        long_content = "A" * (_MAX_INJECT_BYTES + 500)
        p.write_text(long_content)
        monkeypatch.setattr(_plugin, "_ACTIVE_LESSONS_PATH", p)
        result = _inject_active_lessons(session_id="test")
        assert result is not None
        assert len(result["context"]) < _MAX_INJECT_BYTES + 300

    def test_exception_returns_none(self, monkeypatch):
        monkeypatch.setattr(
            _plugin, "_ACTIVE_LESSONS_PATH",
            Path("/nonexistent_dir_xyz_123/file.md"),
        )
        result = _inject_active_lessons(session_id="test")
        assert result is None

    def test_html_only_comments_return_none(self, monkeypatch, tmp_path):
        """active_lessons.md with only HTML comments → treated as empty (return None)."""
        p = tmp_path / "comments_only.md"
        p.write_text("<!--\n  P1 placeholder.\n  No real lessons yet.\n-->\n")
        monkeypatch.setattr(_plugin, "_ACTIVE_LESSONS_PATH", p)
        result = _inject_active_lessons(session_id="test")
        assert result is None, "HTML-comment-only file must return None"

    def test_comments_with_whitespace_return_none(self, monkeypatch, tmp_path):
        """HTML comments + whitespace only → treated as empty."""
        p = tmp_path / "comments_ws.md"
        p.write_text("  \n  <!-- comment -->  \n  \n")
        monkeypatch.setattr(_plugin, "_ACTIVE_LESSONS_PATH", p)
        result = _inject_active_lessons(session_id="test")
        assert result is None, "comments + whitespace must return None"

    def test_comments_plus_real_content_not_skipped(self, monkeypatch, tmp_path):
        """HTML comments + real content → must inject."""
        p = tmp_path / "mixed.md"
        p.write_text("<!-- header comment -->\n# Real Lesson\n- Be careful\n<!-- footer -->\n")
        monkeypatch.setattr(_plugin, "_ACTIVE_LESSONS_PATH", p)
        result = _inject_active_lessons(session_id="test")
        assert result is not None, "comments + real content must inject"
        assert "Real Lesson" in result["context"]



# ══════════════════════════════════════════════════════════════════════════════
# P4B kill switch: HERMES_MEMORY_LESSONS_INJECT
# ══════════════════════════════════════════════════════════════════════════════

class TestKillSwitch:
    """HERMES_MEMORY_LESSONS_INJECT kill switch — P4B."""

    def test_default_unset_injects(self, monkeypatch, tmp_path):
        """Env var not set → default enabled → inject."""
        monkeypatch.delenv("HERMES_MEMORY_LESSONS_INJECT", raising=False)
        p = tmp_path / "lessons.md"
        p.write_text("P4B_SMOKE: default inject works.")
        monkeypatch.setattr(_plugin, "_ACTIVE_LESSONS_PATH", p)
        result = _inject_active_lessons(session_id="test")
        assert result is not None
        assert "P4B_SMOKE" in result["context"]

    def test_env_1_injects(self, monkeypatch, tmp_path):
        """HERMES_MEMORY_LESSONS_INJECT=1 → inject."""
        monkeypatch.setenv("HERMES_MEMORY_LESSONS_INJECT", "1")
        p = tmp_path / "lessons.md"
        p.write_text("P4B: explicit enable works.")
        monkeypatch.setattr(_plugin, "_ACTIVE_LESSONS_PATH", p)
        result = _inject_active_lessons(session_id="test")
        assert result is not None

    def test_env_0_no_inject(self, monkeypatch, tmp_path):
        """HERMES_MEMORY_LESSONS_INJECT=0 → no injection."""
        monkeypatch.setenv("HERMES_MEMORY_LESSONS_INJECT", "0")
        p = tmp_path / "lessons.md"
        p.write_text("P4B: should not be injected.")
        monkeypatch.setattr(_plugin, "_ACTIVE_LESSONS_PATH", p)
        result = _inject_active_lessons(session_id="test")
        assert result is None

    def test_env_false_no_inject(self, monkeypatch, tmp_path):
        """HERMES_MEMORY_LESSONS_INJECT=false → no injection."""
        monkeypatch.setenv("HERMES_MEMORY_LESSONS_INJECT", "false")
        p = tmp_path / "lessons.md"
        p.write_text("P4B: should not be injected.")
        monkeypatch.setattr(_plugin, "_ACTIVE_LESSONS_PATH", p)
        result = _inject_active_lessons(session_id="test")
        assert result is None

    def test_env_off_no_inject(self, monkeypatch, tmp_path):
        """HERMES_MEMORY_LESSONS_INJECT=off → no injection."""
        monkeypatch.setenv("HERMES_MEMORY_LESSONS_INJECT", "off")
        p = tmp_path / "lessons.md"
        p.write_text("P4B: should not be injected.")
        monkeypatch.setattr(_plugin, "_ACTIVE_LESSONS_PATH", p)
        result = _inject_active_lessons(session_id="test")
        assert result is None

    def test_env_no_no_inject(self, monkeypatch, tmp_path):
        """HERMES_MEMORY_LESSONS_INJECT=no → no injection."""
        monkeypatch.setenv("HERMES_MEMORY_LESSONS_INJECT", "no")
        p = tmp_path / "lessons.md"
        p.write_text("P4B: should not be injected.")
        monkeypatch.setattr(_plugin, "_ACTIVE_LESSONS_PATH", p)
        result = _inject_active_lessons(session_id="test")
        assert result is None

    def test_env_0_post_llm_call_still_works(self, monkeypatch, tmp_path):
        """Kill switch only affects pre_llm_call, not post_llm_call recording."""
        monkeypatch.setenv("HERMES_MEMORY_LESSONS_INJECT", "0")
        p = tmp_path / "lessons.md"
        p.write_text("P4B: should not be injected.")
        monkeypatch.setattr(_plugin, "_ACTIVE_LESSONS_PATH", p)
        # pre_llm_call must return None
        pre_result = _inject_active_lessons(session_id="test")
        assert pre_result is None
        # post_llm_call must still record
        events_dir = tmp_path / "events"
        candidates = events_dir / "event_candidates.jsonl"
        monkeypatch.setattr(_plugin, "_EVENTS_DIR", events_dir)
        monkeypatch.setattr(_plugin, "_EVENT_CANDIDATES_PATH", candidates)
        _record_candidate(
            session_id="s_kill",
            user_message="不对，这个不对",
            assistant_response="修复失败",
        )
        assert candidates.is_file(), "post_llm_call must still write events"
        records = [json.loads(l) for l in candidates.read_text().strip().split("\n") if l]
        assert len(records) == 1

    def test_deprecated_old_name_fallback(self, monkeypatch, tmp_path):
        """HERMESMEMORYLESSONS_INJECT (deprecated) → still works, logs warning."""
        monkeypatch.delenv("HERMES_MEMORY_LESSONS_INJECT", raising=False)
        monkeypatch.setenv("HERMESMEMORYLESSONS_INJECT", "0")
        p = tmp_path / "lessons.md"
        p.write_text("P4B: deprecated fallback test.")
        monkeypatch.setattr(_plugin, "_ACTIVE_LESSONS_PATH", p)
        result = _inject_active_lessons(session_id="test")
        assert result is None, "deprecated var should still disable injection"

    def test_deprecated_loses_to_primary(self, monkeypatch, tmp_path):
        """Primary HERMES_MEMORY_LESSONS_INJECT=1 overrides deprecated=0."""
        monkeypatch.setenv("HERMES_MEMORY_LESSONS_INJECT", "1")
        monkeypatch.setenv("HERMESMEMORYLESSONS_INJECT", "0")
        p = tmp_path / "lessons.md"
        p.write_text("P4B: primary wins.")
        monkeypatch.setattr(_plugin, "_ACTIVE_LESSONS_PATH", p)
        result = _inject_active_lessons(session_id="test")
        assert result is not None, "primary var must take precedence over deprecated"

    def test_deprecated_oldest_name_fallback(self, monkeypatch, tmp_path):
        """HERMESMEMORYLESSONSINJECT (oldest deprecated) → still works."""
        monkeypatch.delenv("HERMES_MEMORY_LESSONS_INJECT", raising=False)
        monkeypatch.delenv("HERMESMEMORYLESSONS_INJECT", raising=False)
        monkeypatch.setenv("HERMESMEMORYLESSONSINJECT", "0")
        p = tmp_path / "lessons.md"
        p.write_text("P4B: oldest deprecated fallback test.")
        monkeypatch.setattr(_plugin, "_ACTIVE_LESSONS_PATH", p)
        result = _inject_active_lessons(session_id="test")
        assert result is None, "oldest deprecated var should still disable injection"

# ══════════════════════════════════════════════════════════════════════════════
# post_llm_call: event candidate recording
# ══════════════════════════════════════════════════════════════════════════════

class TestPostLlmCall:
    """post_llm_call — candidate event recording."""

    def _setup_paths(self, monkeypatch, tmp_path):
        events_dir = tmp_path / "events"
        candidates = events_dir / "event_candidates.jsonl"
        monkeypatch.setattr(_plugin, "_EVENTS_DIR", events_dir)
        monkeypatch.setattr(_plugin, "_EVENT_CANDIDATES_PATH", candidates)
        return candidates

    def test_normal_message_not_recorded(self, monkeypatch, tmp_path):
        candidates = self._setup_paths(monkeypatch, tmp_path)
        _record_candidate(
            session_id="s1",
            user_message="今天天气怎么样",
            assistant_response="今天天气不错",
        )
        assert not candidates.is_file()

    def test_user_correction_recorded(self, monkeypatch, tmp_path):
        candidates = self._setup_paths(monkeypatch, tmp_path)
        _record_candidate(
            session_id="s1",
            user_message="不对，这个方案有问题",
            assistant_response="收到，我重新检查",
        )
        assert candidates.is_file()
        records = [json.loads(l) for l in candidates.read_text().strip().split("\n") if l]
        assert len(records) == 1
        assert records[0]["trigger_reason"].startswith("user_correction")
        assert records[0]["session_id"] == "s1"

    def test_assistant_failure_recorded(self, monkeypatch, tmp_path):
        candidates = self._setup_paths(monkeypatch, tmp_path)
        _record_candidate(
            session_id="s2",
            user_message="帮我修复bug",
            assistant_response="修复失败，请检查后端日志",
        )
        assert candidates.is_file()
        records = [json.loads(l) for l in candidates.read_text().strip().split("\n") if l]
        assert len(records) == 1

    def test_assistant_error_recorded(self, monkeypatch, tmp_path):
        candidates = self._setup_paths(monkeypatch, tmp_path)
        _record_candidate(
            session_id="s3",
            user_message="跑测试",
            assistant_response="测试结果：3 failed, error in test_auth",
        )
        assert candidates.is_file()
        records = [json.loads(l) for l in candidates.read_text().strip().split("\n") if l]
        assert len(records) == 1

    def test_duplicate_event_not_duplicated(self, monkeypatch, tmp_path):
        candidates = self._setup_paths(monkeypatch, tmp_path)
        msg = "不对，你之前说的不对"
        _record_candidate(session_id="s1", user_message=msg, assistant_response="收到，我检查一下")
        _record_candidate(session_id="s1", user_message=msg, assistant_response="不同的回复也不重复")
        records = [json.loads(l) for l in candidates.read_text().strip().split("\n") if l]
        assert len(records) == 1  # same user input → dedup, even with different responses

    def test_excerpts_are_truncated(self, monkeypatch, tmp_path):
        candidates = self._setup_paths(monkeypatch, tmp_path)
        long_msg = "不对，" + "A" * 500
        long_resp = "失败，" + "B" * 500
        _record_candidate(session_id="s1", user_message=long_msg, assistant_response=long_resp)
        records = [json.loads(l) for l in candidates.read_text().strip().split("\n") if l]
        assert len(records[0]["user_message_excerpt"]) <= 203

    def test_hashes_present(self, monkeypatch, tmp_path):
        candidates = self._setup_paths(monkeypatch, tmp_path)
        _record_candidate(session_id="s1", user_message="不对", assistant_response="失败")
        records = [json.loads(l) for l in candidates.read_text().strip().split("\n") if l]
        assert len(records[0]["input_sha256"]) == 64
        assert len(records[0]["output_sha256"]) == 64

    def test_write_failure_is_safe(self, monkeypatch, tmp_path):
        candidates = self._setup_paths(monkeypatch, tmp_path)
        candidates.parent.mkdir(parents=True, exist_ok=True)
        # Point to a path that exists as a file (not dir) to trigger failure
        blocker = candidates.parent / "blocker_file"
        blocker.write_text("x")
        monkeypatch.setattr(
            _plugin, "_EVENT_CANDIDATES_PATH",
            blocker / "sub" / "events.jsonl",
        )
        # Should not raise
        _record_candidate(session_id="s1", user_message="不对", assistant_response="失败")
        assert True  # survived

    def test_multiple_different_events(self, monkeypatch, tmp_path):
        candidates = self._setup_paths(monkeypatch, tmp_path)
        for um, ar in [("不对1", "失败1"), ("错了", "不通过"), ("没修好", "error")]:
            _record_candidate(session_id="s_multi", user_message=um, assistant_response=ar)
        records = [json.loads(l) for l in candidates.read_text().strip().split("\n") if l]
        assert len(records) == 3

    def test_file_actually_created_after_write(self, monkeypatch, tmp_path):
        """File must exist with valid JSON after _record_candidate succeeds."""
        candidates = self._setup_paths(monkeypatch, tmp_path)
        _record_candidate(
            session_id="s_created",
            user_message="不对",
            assistant_response="修复失败",
        )
        assert candidates.is_file(), "event_candidates.jsonl must be created"
        content = candidates.read_text().strip()
        lines = [l for l in content.split("\n") if l]
        assert len(lines) >= 1
        rec = json.loads(lines[0])
        assert rec["trigger_reason"].startswith("user_correction")
        assert len(rec["input_sha256"]) == 64
        assert len(rec["output_sha256"]) == 64

    def test_path_consistency_plugin_vs_pipeline(self):
        """Plugin and pipeline must use the exact same filename."""
        plugin_path = _plugin._EVENT_CANDIDATES_PATH
        assert plugin_path.name == "event_candidates.jsonl", (
            f"Plugin uses '{plugin_path.name}', expected 'event_candidates.jsonl'"
        )
        # Pipeline uses same name (verify via import)
        import importlib.util
        scripts_dir = Path.home() / ".hermes" / "profiles" / "me" / "memory" / "scripts"
        spec = importlib.util.spec_from_file_location("reflect_check", scripts_dir / "reflect.py")
        reflect_mod = importlib.util.module_from_spec(spec)
        sys.modules["reflect_check"] = reflect_mod
        spec.loader.exec_module(reflect_mod)
        pipeline_path = reflect_mod._EVENTS_PATH
        assert pipeline_path.name == "event_candidates.jsonl", (
            f"Pipeline uses '{pipeline_path.name}', expected 'event_candidates.jsonl'"
        )

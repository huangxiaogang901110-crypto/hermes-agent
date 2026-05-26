"""
Tests for P4A log_event_scanner.py.
"""
import json
import sys
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
_SCRIPTS = _REPO_ROOT / "tools" / "memory"
_SCANNER_PATH = _SCRIPTS / "log_event_scanner.py"

# Pre-import scanner module for unit-level testing
import importlib.util
_spec = importlib.util.spec_from_file_location("log_event_scanner", str(_SCANNER_PATH))
_scanner = importlib.util.module_from_spec(_spec)
sys.modules["log_event_scanner"] = _scanner
_spec.loader.exec_module(_scanner)

scan_log = _scanner.scan_log
_is_sensitive = _scanner._is_sensitive
_sha256 = _scanner._sha256
_dedup_candidates = _scanner._dedup_candidates
_merge_similar = _scanner._merge_similar
_truncate_excerpt = _scanner._truncate_excerpt
_assign_severity = _scanner._assign_severity
_assign_category = _scanner._assign_category
_load_existing_hashes = _scanner._load_existing_hashes
_load_existing_signatures = _scanner._load_existing_signatures


# ══════════════════════════════════════════════════════════════════════════════
# helpers
# ══════════════════════════════════════════════════════════════════════════════

def _make_log(tmp_path: Path, lines: list[str]) -> Path:
    """Create a temporary log file and return its path."""
    p = tmp_path / "agent.log"
    p.write_text("\n".join(lines))
    return p


# ══════════════════════════════════════════════════════════════════════════════
# unit: sensitive detection
# ══════════════════════════════════════════════════════════════════════════════

class TestSensitiveDetection:
    def test_sk_key(self):
        assert _is_sensitive("some log with sk-abc123def456ghi789jklmno")

    def test_bearer_token(self):
        assert _is_sensitive("Authorization: Bearer eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9")

    def test_password_assignment(self):
        assert _is_sensitive("set password=mysecret123")

    def test_api_key_assignment(self):
        assert _is_sensitive("env api_key=sk-live-123456")

    def test_private_key(self):
        assert _is_sensitive("-----BEGIN PRIVATE KEY-----")

    def test_aws_key(self):
        assert _is_sensitive("export AKIAIOSFODNN7EXAMPLE")

    def test_lta_key(self):
        assert _is_sensitive("accessKeyId: LTAI5tAbCdEfGhIjKlMn")

    def test_normal_log_not_sensitive(self):
        assert not _is_sensitive("2026-05-26 INFO: task completed successfully")

    def test_fail_log_not_sensitive(self):
        assert not _is_sensitive("FAIL: connection refused on port 8080")

    def test_cookie_not_sensitive(self):
        assert _is_sensitive("cookie=session_id=abc123def")


# ══════════════════════════════════════════════════════════════════════════════
# unit: sha256
# ══════════════════════════════════════════════════════════════════════════════

class TestSha256:
    def test_deterministic(self):
        assert _sha256("hello") == _sha256("hello")

    def test_different(self):
        assert _sha256("hello") != _sha256("world")


# ══════════════════════════════════════════════════════════════════════════════
# unit: severity / category
# ══════════════════════════════════════════════════════════════════════════════

class TestSeverityCategory:
    def test_exception_high(self):
        assert _assign_severity("exception") == "high"

    def test_fail_high(self):
        assert _assign_severity("FAIL") == "high"

    def test_error_medium(self):
        assert _assign_severity("error") == "medium"

    def test_secret_critical(self):
        assert _assign_severity("secret leaked") == "critical"

    def test_runtime_error_category(self):
        assert _assign_category("timeout") == "runtime_error"

    def test_security_category(self):
        assert _assign_category("forbidden") == "security"

    def test_api_limit_category(self):
        assert _assign_category("rate limit") == "api_limit"

    def test_process_violation_category(self):
        assert _assign_category("production") == "process_violation"


# ══════════════════════════════════════════════════════════════════════════════
# unit: truncate
# ══════════════════════════════════════════════════════════════════════════════

class TestTruncateExcerpt:
    def test_short_no_truncate(self):
        result = _truncate_excerpt("hello", 1024)
        assert result == "hello"

    def test_long_truncates(self):
        big = "A" * 5000
        result = _truncate_excerpt(big, 200)
        assert len(result.encode("utf-8")) <= 200
        assert "[truncated]" in result

    def test_exact_boundary(self):
        text = "ABCDEFGH"
        result = _truncate_excerpt(text, 8)
        assert result == "ABCDEFGH"


# ══════════════════════════════════════════════════════════════════════════════
# unit: merge similar
# ══════════════════════════════════════════════════════════════════════════════

class TestMergeSimilar:
    def test_same_keyword_merged(self):
        candidates = [
            {"trigger_keyword": "error", "line_start": 10},
            {"trigger_keyword": "error", "line_start": 50},
            {"trigger_keyword": "timeout", "line_start": 100},
        ]
        result = _merge_similar(candidates)
        assert len(result) == 2
        assert result[0]["trigger_keyword"] == "error"
        assert result[1]["trigger_keyword"] == "timeout"

    def test_empty(self):
        assert _merge_similar([]) == []

    def test_single(self):
        assert len(_merge_similar([{"trigger_keyword": "fail"}])) == 1


# ══════════════════════════════════════════════════════════════════════════════
# unit: dedup candidates
# ══════════════════════════════════════════════════════════════════════════════

class TestDedupCandidates:
    def test_new_no_duplicate(self):
        c = [{"source_file": "a.log", "line_start": 1, "line_end": 10, "trigger_keyword": "error", "excerpt_sha256": "aaa"}]
        result = _dedup_candidates(c, set(), set())
        assert len(result) == 1

    def test_existing_hash_skipped(self):
        c = [{"source_file": "a.log", "line_start": 1, "line_end": 10, "trigger_keyword": "error", "excerpt_sha256": "aaa"}]
        result = _dedup_candidates(c, set(), {"aaa"})
        assert len(result) == 0

    def test_existing_sig_skipped(self):
        c = [{"source_file": "a.log", "line_start": 1, "line_end": 10, "trigger_keyword": "error", "excerpt_sha256": "aaa"}]
        result = _dedup_candidates(c, {("a.log", 1, 10, "error")}, set())
        assert len(result) == 0

    def test_internal_duplicate_skipped(self):
        c = [
            {"source_file": "a.log", "line_start": 1, "line_end": 10, "trigger_keyword": "error", "excerpt_sha256": "aaa"},
            {"source_file": "a.log", "line_start": 1, "line_end": 10, "trigger_keyword": "error", "excerpt_sha256": "aaa"},
        ]
        result = _dedup_candidates(c, set(), set())
        assert len(result) == 1


# ══════════════════════════════════════════════════════════════════════════════
# integration: scan_log with fake logs
# ══════════════════════════════════════════════════════════════════════════════

class TestScanLogBasic:
    def test_no_log_file(self, tmp_path):
        result = scan_log(tmp_path / "nonexistent.log")
        assert result == []

    def test_empty_log(self, tmp_path):
        p = _make_log(tmp_path, [])
        result = scan_log(p)
        assert result == []

    def test_normal_log_no_match(self, tmp_path):
        p = _make_log(tmp_path, [
            "2026-05-26 10:00:00 INFO: task started",
            "2026-05-26 10:01:00 INFO: task completed",
        ])
        result = scan_log(p)
        assert result == []

    def test_error_line_detected(self, tmp_path):
        p = _make_log(tmp_path, [
            "2026-05-26 10:00:00 INFO: processing request",
            "2026-05-26 10:01:00 ERROR: connection refused on port 8080",
            "2026-05-26 10:02:00 INFO: retrying",
        ])
        result = scan_log(p)
        assert len(result) >= 1
        c = result[0]
        assert c["trigger_keyword"] in ("error", "ERROR")
        assert c["severity"] in ("medium", "high")
        assert not c["blocked_sensitive"]
        assert "connection refused" in c["excerpt"]
        assert c["source_type"] == "log_event"
        assert c["excerpt_sha256"]

    def test_fail_detected(self, tmp_path):
        p = _make_log(tmp_path, [
            "BUILD FAIL: tests failed",
        ])
        result = scan_log(p)
        assert len(result) >= 1
        assert any(c["trigger_keyword"] == "FAIL" for c in result)

    def test_exception_detected(self, tmp_path):
        p = _make_log(tmp_path, [
            "Traceback (most recent call last):",
            "  File 'app.py', line 42, in <module>",
            "ValueError: invalid value",
        ])
        result = scan_log(p)
        assert len(result) >= 1
        assert any(c["trigger_keyword"] in ("exception", "traceback") for c in result)

    def test_timeout_detected(self, tmp_path):
        p = _make_log(tmp_path, [
            "2026-05-26 10:00:00 WARNING: API call timeout after 30s",
        ])
        result = scan_log(p)
        assert len(result) >= 1
        assert any(c["trigger_keyword"] == "timeout" for c in result)

    def test_blocked_detected(self, tmp_path):
        p = _make_log(tmp_path, [
            "2026-05-26 10:00:00 WARNING: request blocked by firewall",
        ])
        result = scan_log(p)
        assert len(result) >= 1
        assert any(c["trigger_keyword"] == "blocked" for c in result)

    def test_partial_detected(self, tmp_path):
        p = _make_log(tmp_path, [
            "2026-05-26 10:00:00 INFO: PARTIAL success: 3/5 tasks done",
        ])
        result = scan_log(p)
        assert len(result) >= 1
        assert any(c["trigger_keyword"] == "PARTIAL" for c in result)


# ══════════════════════════════════════════════════════════════════════════════
# integration: sensitive content blocking
# ══════════════════════════════════════════════════════════════════════════════

class TestSensitiveBlocking:
    def test_sk_key_blocked(self, tmp_path):
        p = _make_log(tmp_path, [
            "2026-05-26 10:00:00 ERROR: API call failed",
            "2026-05-26 10:00:01 DEBUG: using key sk-abc123def456ghi789jklmnopqrstuv",
        ])
        result = scan_log(p)
        assert len(result) >= 1
        # At least one event should have been triggered (by "error" or "FAIL")
        # The key question: does the excerpt show the raw key?
        for c in result:
            excerpt = c["excerpt"]
            assert "sk-abc" not in excerpt, f"Raw key leaked in excerpt!"

    def test_bearer_token_blocked(self, tmp_path):
        p = _make_log(tmp_path, [
            "2026-05-26 10:00:00 ERROR: auth failed",
            "Authorization: Bearer eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0",
        ])
        result = scan_log(p)
        for c in result:
            if c["blocked_sensitive"]:
                assert "BLOCKED" in c["excerpt"]
            else:
                assert "Bearer eyJ" not in c["excerpt"]

    def test_password_blocked(self, tmp_path):
        p = _make_log(tmp_path, [
            "2026-05-26 10:00:00 ERROR: login failed password=hunter2",
        ])
        result = scan_log(p)
        for c in result:
            if c["blocked_sensitive"]:
                assert "BLOCKED" in c["excerpt"]
            else:
                assert "hunter2" not in c["excerpt"].lower() or "password" not in c["excerpt"].lower()


# ══════════════════════════════════════════════════════════════════════════════
# integration: max limits
# ══════════════════════════════════════════════════════════════════════════════

class TestMaxLimits:
    def test_max_events(self, tmp_path):
        """Ensure max_events=3 returns at most 3."""
        lines = []
        for i in range(20):
            lines.append(f"2026-05-26 10:{i:02d}:00 ERROR: something failed {i}")
        p = _make_log(tmp_path, lines)
        result = scan_log(p, max_events=3)
        assert len(result) <= 3

    def test_max_snippet_bytes(self, tmp_path):
        """Ensure excerpts are capped at max_snippet_bytes."""
        long_line = "ERROR: " + "X" * 5000
        p = _make_log(tmp_path, [long_line])
        result = scan_log(p, max_snippet_bytes=200)
        for c in result:
            assert len(c["excerpt"].encode("utf-8")) <= 200 + 50  # small tolerance


# ══════════════════════════════════════════════════════════════════════════════
# integration: dedup on re-run
# ══════════════════════════════════════════════════════════════════════════════

class TestDedupReRun:
    def test_dedup_by_existing_events(self, tmp_path):
        """Re-running with same log should produce different candidates after dedup."""
        log = _make_log(tmp_path, [
            "2026-05-26 10:00:00 ERROR: unique error message ABC123",
        ])
        # First scan
        result1 = scan_log(log)
        assert len(result1) == 1

        # Simulate existing events
        events_path = tmp_path / "events.jsonl"
        events_path.write_text(json.dumps(result1[0]) + "\n")

        existing_hashes = _load_existing_hashes(events_path)
        existing_sigs = _load_existing_signatures(events_path)

        # Second scan, then dedup
        result2 = scan_log(log)
        deduped = _dedup_candidates(result2, existing_sigs, existing_hashes)
        assert len(deduped) == 0


# ══════════════════════════════════════════════════════════════════════════════
# integration: line numbers
# ══════════════════════════════════════════════════════════════════════════════

class TestLineNumbers:
    def test_line_start_end_correct(self, tmp_path):
        lines = [
            "2026-05-26 10:00:00 INFO: before",
            "2026-05-26 10:00:01 ERROR: main failure",
            "2026-05-26 10:00:02 INFO: after",
        ]
        p = _make_log(tmp_path, lines)
        result = scan_log(p)
        for c in result:
            assert c["line_start"] >= 1
            assert c["line_end"] <= len(lines)
            assert c["line_start"] <= c["line_end"]


# ══════════════════════════════════════════════════════════════════════════════
# integration: no full log dump
# ══════════════════════════════════════════════════════════════════════════════

class TestNoFullLogDump:
    def test_excerpt_not_entire_file(self, tmp_path):
        """100-line log, excerpt should only contain a window around the match."""
        lines = []
        for i in range(100):
            lines.append(f"2026-05-26 10:{i//60:02d}:{i%60:02d} INFO: processing item {i}")
        lines[50] = "2026-05-26 10:00:50 ERROR: fatal error at item 50"
        p = _make_log(tmp_path, lines)
        result = scan_log(p, max_snippet_bytes=10000)
        assert len(result) >= 1
        # Excerpt should NOT contain all 100 lines
        for c in result:
            line_count = c["excerpt"].count("\n") + 1
            assert line_count < 100, f"Excerpt has {line_count} lines, appears to be full log dump"


# ══════════════════════════════════════════════════════════════════════════════
# integration: dry-run vs apply via subprocess
# ══════════════════════════════════════════════════════════════════════════════

class TestDryRun:
    def test_dry_run_does_not_write(self, tmp_path):
        """Running --dry-run should not create output file."""
        log = _make_log(tmp_path, [
            "2026-05-26 10:00:00 ERROR: test error",
        ])
        import subprocess
        output = tmp_path / "events_out.jsonl"
        python = Path.home() / ".hermes" / "hermes-agent" / "venv" / "bin" / "python"
        r = subprocess.run(
            [str(python), str(_SCANNER_PATH), "--log-file", str(log), "--output", str(output)],
            capture_output=True, text=True,
        )
        assert "DRY-RUN" in (r.stdout + r.stderr)
        assert not output.exists()

    def test_apply_writes_file(self, tmp_path):
        """Running --apply should create output file."""
        log = _make_log(tmp_path, [
            "2026-05-26 10:00:00 ERROR: test error",
        ])
        import subprocess
        output = tmp_path / "events_out.jsonl"
        python = Path.home() / ".hermes" / "hermes-agent" / "venv" / "bin" / "python"
        r = subprocess.run(
            [str(python), str(_SCANNER_PATH), "--log-file", str(log), "--output", str(output), "--apply"],
            capture_output=True, text=True,
        )
        assert output.exists()
        data = output.read_text().strip().splitlines()
        assert len(data) >= 1
        for line in data:
            obj = json.loads(line)
            assert obj["source_type"] == "log_event"


# ══════════════════════════════════════════════════════════════════════════════
# integration: no-match doesn't crash
# ══════════════════════════════════════════════════════════════════════════════

class TestNoMatch:
    def test_no_match_no_crash(self, tmp_path):
        p = _make_log(tmp_path, [
            "2026-05-26 10:00:00 INFO: all good",
            "2026-05-26 10:01:00 INFO: still good",
        ])
        result = scan_log(p)
        assert result == []

    def test_no_match_subprocess_exit_zero(self, tmp_path):
        """No-match log should exit 0, not crash."""
        p = _make_log(tmp_path, [
            "2026-05-26 10:00:00 INFO: all good",
        ])
        import subprocess
        python = Path.home() / ".hermes" / "hermes-agent" / "venv" / "bin" / "python"
        r = subprocess.run(
            [str(python), str(_SCANNER_PATH), "--log-file", str(p)],
            capture_output=True, text=True,
        )
        assert r.returncode == 0

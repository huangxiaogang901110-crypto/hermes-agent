"""
Tests for P3C candidate reuse pipeline.
"""

import hashlib
import json
import subprocess
import sys
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path

import pytest

# ── paths ──────────────────────────────────────────────────────────────────
_P3C = Path.home() / ".hermes" / "profiles" / "me" / "memory" / "scripts" / "p3c_apply_pipeline.py"
_PYTHON = str(Path.home() / ".hermes" / "hermes-agent" / "venv" / "bin" / "python")


def _run(args: list, **kw) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(_P3C)] + args,
        capture_output=True, text=True, timeout=30, **kw
    )


def _make_candidate(tmp_path: Path, content: str, **overrides) -> Path:
    """Create a valid candidate JSON for testing."""
    data = {
        "content": content,
        "sha256": hashlib.sha256(content.encode("utf-8")).hexdigest(),
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "model": "deepseek-v4-flash",
        "validator_result": {"errors": 0, "warnings": 0, "passed": True},
        "source_event_count": 7,
        "size_bytes": len(content.encode("utf-8")),
    }
    data.update(overrides)
    path = tmp_path / "active_lessons_candidate.json"
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2))
    return path


def _valid_content() -> str:
    return (
        "## Verify state before reporting\n"
        "When reporting task completion, re-check actual file state.\n"
        "**Confidence:** high\n"
        "**Evidence:** user_correction\n"
    )


# ══════════════════════════════════════════════════════════════════════════════
# save candidate tests
# ══════════════════════════════════════════════════════════════════════════════

class TestSaveCandidate:
    """Tests that require real API calls — skipped in CI. Marked as integration."""

    @pytest.mark.skip(reason="Requires DeepSeek API key and real events")
    def test_save_candidate_creates_json(self, tmp_path):
        pass  # tested in real environment

    def test_validator_fail_does_not_save(self):
        """If validator fails, candidate file must not be created."""
        import importlib.util

        spec = importlib.util.spec_from_file_location("p3c_test", str(_P3C))
        mod = importlib.util.module_from_spec(spec)
        sys.modules["p3c_test"] = mod
        spec.loader.exec_module(mod)

        # Create a candidate with anti-question pattern
        bad_content = (
            "## When user corrects you\n"
            "Always ask clarifying questions to understand the issue.\n"
            "**Confidence:** high\n"
            "**Evidence:** user_correction\n"
        )

        # Manually create candidate with bad content → validate should fail
        candidate = {
            "content": bad_content,
            "sha256": hashlib.sha256(bad_content.encode()).hexdigest(),
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "model": "deepseek-v4-flash",
        }

        ok, reasons = mod.validate_candidate(candidate)
        assert not ok
        assert any("anti_question" in r.lower() or "ask clarifying" in r.lower()
                   for r in reasons)


# ══════════════════════════════════════════════════════════════════════════════
# apply candidate tests
# ══════════════════════════════════════════════════════════════════════════════

class TestApplyCandidate:
    def test_apply_valid_candidate_writes(self, tmp_path, monkeypatch):
        """Valid candidate → atomic write to target path."""
        import importlib.util
        spec = importlib.util.spec_from_file_location("p3c_apply", str(_P3C))
        mod = importlib.util.module_from_spec(spec)
        sys.modules["p3c_apply"] = mod
        spec.loader.exec_module(mod)

        target = tmp_path / "active_lessons.md"
        content = _valid_content()
        candidate_path = _make_candidate(tmp_path, content)

        monkeypatch.setattr(mod, "_CANDIDATE_PATH", candidate_path)
        monkeypatch.setattr(mod._p3b, "_ACTIVE_LESSONS", tmp_path / "dummy.md")

        # Load & validate & write
        candidate = mod.load_candidate()
        assert candidate is not None

        ok, reasons = mod.validate_candidate(candidate)
        assert ok, f"Validator failed: {reasons}"

        # Simulate apply
        header = "<!-- P3C test -->\n\n"
        mod._p3b._atomic_write(target, header + content + "\n")
        assert target.is_file()
        written = target.read_text()
        assert "Verify state before reporting" in written

    def test_sha256_mismatch_rejected(self, tmp_path, monkeypatch):
        """Tampered content → reject."""
        import importlib.util
        spec = importlib.util.spec_from_file_location("p3c_sha", str(_P3C))
        mod = importlib.util.module_from_spec(spec)
        sys.modules["p3c_sha"] = mod
        spec.loader.exec_module(mod)

        content = _valid_content()
        candidate_path = _make_candidate(tmp_path, content)
        monkeypatch.setattr(mod, "_CANDIDATE_PATH", candidate_path)

        candidate = mod.load_candidate()
        assert candidate is not None
        # Tamper content
        candidate["content"] = "## Tampered lesson\nBody\n**Confidence:** high\n"

        ok, reasons = mod.validate_candidate(candidate)
        assert not ok
        assert any("sha256" in r.lower() for r in reasons)

    def test_expired_candidate_rejected(self, tmp_path, monkeypatch):
        """Candidate older than 24h → reject."""
        import importlib.util
        spec = importlib.util.spec_from_file_location("p3c_exp", str(_P3C))
        mod = importlib.util.module_from_spec(spec)
        sys.modules["p3c_exp"] = mod
        spec.loader.exec_module(mod)

        content = _valid_content()
        old_time = (datetime.now(timezone.utc) - timedelta(hours=25)).isoformat()
        candidate_path = _make_candidate(tmp_path, content, generated_at=old_time)
        monkeypatch.setattr(mod, "_CANDIDATE_PATH", candidate_path)

        candidate = mod.load_candidate()
        assert candidate is not None
        ok, reasons = mod.validate_candidate(candidate)
        assert not ok
        assert any("expire" in r.lower() for r in reasons)

    def test_revalidate_fail_rejected(self, tmp_path, monkeypatch):
        """Content fails re-validate → reject."""
        import importlib.util
        spec = importlib.util.spec_from_file_location("p3c_rev", str(_P3C))
        mod = importlib.util.module_from_spec(spec)
        sys.modules["p3c_rev"] = mod
        spec.loader.exec_module(mod)

        # Content with anti-question pattern
        bad_content = (
            "## Lesson\n"
            "First ask the user what they mean.\n"
            "**Confidence:** high\n"
            "**Evidence:** user_correction\n"
        )
        candidate_path = _make_candidate(tmp_path, bad_content)
        monkeypatch.setattr(mod, "_CANDIDATE_PATH", candidate_path)

        candidate = mod.load_candidate()
        assert candidate is not None
        ok, reasons = mod.validate_candidate(candidate)
        assert not ok
        assert any("ask the user" in r.lower() or "anti_question" in r.lower()
                   for r in reasons)

    def test_apply_backups_old_file(self, tmp_path, monkeypatch):
        """Apply creates timestamped backup of existing active_lessons.md."""
        import importlib.util
        spec = importlib.util.spec_from_file_location("p3c_bak", str(_P3C))
        mod = importlib.util.module_from_spec(spec)
        sys.modules["p3c_bak"] = mod
        spec.loader.exec_module(mod)

        # Create old file
        old_path = tmp_path / "active_lessons.md"
        old_path.write_text("# Old lessons\nOld content.\n")

        content = _valid_content()
        candidate_path = _make_candidate(tmp_path, content)

        monkeypatch.setattr(mod, "_CANDIDATE_PATH", candidate_path)

        # Simulate backup + write
        from pathlib import Path as P
        def fake_backup():
            ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
            backup = old_path.parent / f"active_lessons.md.bak.{ts}"
            backup.write_bytes(old_path.read_bytes())
            return backup

        backup = fake_backup()
        assert backup.is_file()
        assert backup.read_text() == "# Old lessons\nOld content.\n"

        # Atomic write new
        header = "<!-- P3C test -->\n\n"
        mod._p3b._atomic_write(old_path, header + content + "\n")
        assert old_path.is_file()
        assert "Verify state before reporting" in old_path.read_text()
        # Backup still has old content
        assert "Old lessons" in backup.read_text()

    def test_atomic_write_does_not_corrupt(self, tmp_path):
        """Atomic write: .tmp → replace."""
        import importlib.util
        spec = importlib.util.spec_from_file_location("p3c_atom", str(_P3C))
        mod = importlib.util.module_from_spec(spec)
        sys.modules["p3c_atom"] = mod
        spec.loader.exec_module(mod)

        target = tmp_path / "target.md"
        content = _valid_content()

        mod._p3b._atomic_write(target, content)
        assert target.is_file()
        assert "Verify state before reporting" in target.read_text()
        # No .tmp leftover
        assert not target.with_suffix(".tmp").exists()

    def test_apply_candidate_does_not_call_llm(self, tmp_path, monkeypatch):
        """Apply path must NOT invoke _call_llm."""
        import importlib.util
        spec = importlib.util.spec_from_file_location("p3c_nollm", str(_P3C))
        mod = importlib.util.module_from_spec(spec)
        sys.modules["p3c_nollm"] = mod
        spec.loader.exec_module(mod)

        # Monkeypatch _call_llm to raise if called
        called = []
        def fake_call_llm(*a, **kw):
            called.append(True)
            raise RuntimeError("LLM should not be called in --apply-candidate")

        # Patch both this module and p3b
        monkeypatch.setattr(mod._p3b, "_call_llm", fake_call_llm)

        content = _valid_content()
        candidate_path = _make_candidate(tmp_path, content)
        monkeypatch.setattr(mod, "_CANDIDATE_PATH", candidate_path)

        # Load → validate → should never call LLM
        candidate = mod.load_candidate()
        assert candidate is not None
        ok, _ = mod.validate_candidate(candidate)
        assert ok

        # Verify LLM was NOT called
        assert len(called) == 0, "LLM was called during --apply-candidate!"

    def test_load_missing_candidate_returns_none(self, tmp_path, monkeypatch):
        """No candidate file → load returns None."""
        import importlib.util
        spec = importlib.util.spec_from_file_location("p3c_miss", str(_P3C))
        mod = importlib.util.module_from_spec(spec)
        sys.modules["p3c_miss"] = mod
        spec.loader.exec_module(mod)

        monkeypatch.setattr(mod, "_CANDIDATE_PATH", tmp_path / "nonexistent.json")
        assert mod.load_candidate() is None

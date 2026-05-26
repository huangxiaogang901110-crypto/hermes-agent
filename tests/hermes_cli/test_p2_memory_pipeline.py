"""
Tests for P2 scripts: reflect.py, synthesize.py, memory_pipeline.py.
"""

import json
import subprocess
import sys
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
_SCRIPTS = _REPO_ROOT / "tools" / "memory"
# Fallback: user-installed scripts path
if not _SCRIPTS.is_dir():
    _SCRIPTS = Path.home() / ".hermes" / "profiles" / "me" / "memory" / "scripts"
_PYTHON = Path.home() / ".hermes" / "hermes-agent" / "venv" / "bin" / "python"


def _run(cmd: list, **kw):
    return subprocess.run([str(_PYTHON)] + cmd, capture_output=True, text=True, **kw)


def _make_test_events(tmp_path, events: list) -> Path:
    p = tmp_path / "event_candidates.jsonl"
    p.write_text("\n".join(json.dumps(e) for e in events))
    return p


# ══════════════════════════════════════════════════════════════════════════════
# reflect.py
# ══════════════════════════════════════════════════════════════════════════════

class TestReflect:
    def _setup(self, tmp_path, monkeypatch, events=None):
        if events is None:
            events = [{
            "created_at": "2026-05-26T10:00:00Z", "session_id": "t1",
            "platform": "test", "model": "test",
            "user_message_excerpt": "不对，方案有误",
            "assistant_response_excerpt": "收到",
            "trigger_reason": "user_correction:不对",
            "input_sha256": "abc111", "output_sha256": "def111",
        }]
        events_path = _make_test_events(tmp_path, events)
        reflections_dir = tmp_path / "reflections"
        reflections_dir.mkdir(exist_ok=True)

        import importlib.util
        spec = importlib.util.spec_from_file_location("reflect", str(_SCRIPTS / "reflect.py"))
        mod = importlib.util.module_from_spec(spec)
        sys.modules["reflect"] = mod
        spec.loader.exec_module(mod)

        monkeypatch.setattr(mod, "_EVENTS_PATH", events_path)
        monkeypatch.setattr(mod, "_REFLECTIONS_DIR", reflections_dir)
        return mod

    def test_no_events_returns_zero(self, tmp_path, monkeypatch):
        mod = self._setup(tmp_path, monkeypatch, events=[])
        result = mod.process_events(apply=False)
        assert result["processed"] == 0

    def test_single_event_generates_reflection(self, tmp_path, monkeypatch):
        mod = self._setup(tmp_path, monkeypatch)
        result = mod.process_events(apply=False)
        assert result["processed"] == 1
        assert result["skipped"] == 0

    def test_single_event_apply_writes_file(self, tmp_path, monkeypatch):
        mod = self._setup(tmp_path, monkeypatch)
        result = mod.process_events(apply=True)
        assert result["processed"] == 1
        files = list(mod._REFLECTIONS_DIR.glob("*.md"))
        assert len(files) == 1

    def test_duplicate_event_skipped(self, tmp_path, monkeypatch):
        mod = self._setup(tmp_path, monkeypatch)
        mod.process_events(apply=True)
        # Re-run
        result = mod.process_events(apply=True)
        assert result["skipped"] == 1
        assert result["processed"] == 0

    def test_sensitive_content_blocked(self, tmp_path, monkeypatch):
        events = [{
            "created_at": "2026-05-26T10:00:00Z", "session_id": "t1",
            "platform": "test", "model": "test",
            "user_message_excerpt": "my api_key is sk-abc123",
            "assistant_response_excerpt": "ok",
            "trigger_reason": "user_correction:不对",
            "input_sha256": "sens01", "output_sha256": "out01",
        }]
        mod = self._setup(tmp_path, monkeypatch, events=events)
        result = mod.process_events(apply=True)
        assert result["sensitive"] >= 1
        assert result["processed"] == 0

    def test_cli_no_args_dry_run(self, tmp_path, monkeypatch):
        r = _run([str(_SCRIPTS / "reflect.py")])
        assert r.returncode == 0

    def test_cli_apply(self, tmp_path, monkeypatch):
        r = _run([str(_SCRIPTS / "reflect.py"), "--apply"])
        assert r.returncode == 0


# ══════════════════════════════════════════════════════════════════════════════
# synthesize.py
# ══════════════════════════════════════════════════════════════════════════════

class TestSynthesize:
    def _setup(self, tmp_path, monkeypatch, reflections=None):
        ref_dir = tmp_path / "reflections"
        ref_dir.mkdir(exist_ok=True)
        active = tmp_path / "active_lessons.md"

        import importlib.util
        spec = importlib.util.spec_from_file_location("synth", str(_SCRIPTS / "synthesize.py"))
        mod = importlib.util.module_from_spec(spec)
        sys.modules["synth"] = mod
        spec.loader.exec_module(mod)

        monkeypatch.setattr(mod, "_REFLECTIONS_DIR", ref_dir)
        monkeypatch.setattr(mod, "_ACTIVE_LESSONS_PATH", active)
        return mod, ref_dir, active

    def _write_reflection(self, ref_dir, name, lesson, confidence="medium", trigger="test"):
        content = f"""---
trigger_reason: {trigger}
---
## suggested_active_lesson
{lesson}
## confidence
{confidence}
"""
        (ref_dir / f"{name}.md").write_text(content)

    def test_no_reflections_returns_zero(self, tmp_path, monkeypatch):
        mod, _, _ = self._setup(tmp_path, monkeypatch)
        result = mod.synthesize(apply=False)
        assert result["lessons"] == 0

    def test_single_reflection_produces_lesson(self, tmp_path, monkeypatch):
        mod, ref_dir, _ = self._setup(tmp_path, monkeypatch)
        self._write_reflection(ref_dir, "r1", "Do not repeat mistakes")
        result = mod.synthesize(apply=False)
        assert result["lessons"] == 1

    def test_duplicate_lessons_merged(self, tmp_path, monkeypatch):
        mod, ref_dir, _ = self._setup(tmp_path, monkeypatch)
        self._write_reflection(ref_dir, "r1", "Do not repeat mistakes", "high")
        self._write_reflection(ref_dir, "r2", "Do not repeat mistakes", "medium")
        result = mod.synthesize(apply=False)
        assert result["lessons"] == 1

    def test_dry_run_does_not_write(self, tmp_path, monkeypatch):
        mod, ref_dir, active = self._setup(tmp_path, monkeypatch)
        self._write_reflection(ref_dir, "r1", "Lesson text")
        mod.synthesize(apply=False)
        assert not active.exists()

    def test_apply_writes_file(self, tmp_path, monkeypatch):
        mod, ref_dir, active = self._setup(tmp_path, monkeypatch)
        self._write_reflection(ref_dir, "r1", "Apply writes this")
        mod.synthesize(apply=True)
        assert active.exists()
        content = active.read_text()
        assert "Apply writes this" in content

    def test_max_5_lessons(self, tmp_path, monkeypatch):
        mod, ref_dir, _ = self._setup(tmp_path, monkeypatch)
        for i in range(7):
            self._write_reflection(ref_dir, f"r{i}", f"Lesson number {i}", confidence="high", trigger=f"trigger{i}")
        result = mod.synthesize(apply=False)
        assert result["lessons"] <= 5

    def test_cli_no_args_dry_run(self):
        r = _run([str(_SCRIPTS / "synthesize.py")])
        assert r.returncode == 0

    def test_cli_help(self):
        r = _run([str(_SCRIPTS / "synthesize.py"), "--help"])
        assert r.returncode == 0


# ══════════════════════════════════════════════════════════════════════════════
# memory_pipeline.py
# ══════════════════════════════════════════════════════════════════════════════

class TestMemoryPipeline:
    def test_cli_no_args_dry_run(self):
        r = _run([str(_SCRIPTS / "memory_pipeline.py")])
        assert r.returncode == 0

    def test_cli_help(self):
        r = _run([str(_SCRIPTS / "memory_pipeline.py"), "--help"])
        assert r.returncode == 0

    def test_reflect_only_flag(self):
        r = _run([str(_SCRIPTS / "memory_pipeline.py"), "--reflect-only"])
        assert r.returncode == 0

    def test_synthesize_only_flag(self):
        r = _run([str(_SCRIPTS / "memory_pipeline.py"), "--synthesize-only"])
        assert r.returncode == 0

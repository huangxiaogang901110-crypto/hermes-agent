"""
Tests for P3B validator — auto quality gate rules.
"""

import importlib.util
import sys
from pathlib import Path

import pytest

# ── import validator functions from p3b_apply_pipeline ─────────────────────
_PIPELINE = Path.home() / ".hermes" / "profiles" / "me" / "memory" / "scripts" / "p3b_apply_pipeline.py"
_spec = importlib.util.spec_from_file_location("p3b", str(_PIPELINE))
_mod = importlib.util.module_from_spec(_spec)
sys.modules["p3b"] = _mod
_spec.loader.exec_module(_mod)

validate = _mod.validate
validator_passed = _mod.validator_passed
_parse_lessons = _mod._parse_lessons


# ══════════════════════════════════════════════════════════════════════════════
# parse tests
# ══════════════════════════════════════════════════════════════════════════════

class TestParseLessons:
    def test_empty(self):
        assert _parse_lessons("") == []

    def test_single_lesson(self):
        content = (
            "## Lesson 1\n"
            "body text\n"
            "**Confidence:** high\n"
            "**Evidence:** user_correction\n"
        )
        lessons = _parse_lessons(content)
        assert len(lessons) == 1
        assert "Lesson 1" in lessons[0]["title"]
        assert lessons[0]["confidence"] == "high"
        assert "user_correction" in lessons[0]["source"]

    def test_multiple_lessons(self):
        content = (
            "## Lesson A\nbody a\n**Confidence:** medium\n\n"
            "## Lesson B\nbody b\n**Confidence:** high\n\n"
            "## Lesson C\nbody c\n**Confidence:** low\n"
        )
        lessons = _parse_lessons(content)
        assert len(lessons) == 3


# ══════════════════════════════════════════════════════════════════════════════
# anti-question HARD REJECT tests
# ══════════════════════════════════════════════════════════════════════════════

class TestAntiQuestionReject:
    def test_always_ask_clarifying_questions_rejected(self):
        content = (
            "## Lesson 1\n"
            "When user corrects you, always ask clarifying questions.\n"
            "**Confidence:** high\n"
            "**Evidence:** user_correction\n"
        )
        errors, _ = validate(content)
        assert len(errors) >= 1
        assert any("anti_question" in str(e) for e in errors)

    def test_first_ask_the_user_rejected(self):
        content = (
            "## Lesson 1\n"
            "First ask the user what they want.\n"
            "**Confidence:** high\n"
        )
        errors, _ = validate(content)
        assert len(errors) >= 1
        assert any("anti_question" in str(e) for e in errors)

    def test_confirm_expectations_before_proceeding_rejected(self):
        content = (
            "## Lesson 1\n"
            "Always confirm expectations before proceeding.\n"
            "**Confidence:** high\n"
        )
        errors, _ = validate(content)
        assert len(errors) >= 1

    def test_ask_which_rules_they_mean_rejected(self):
        content = (
            "## Lesson 1\n"
            "Ask which rules or context they refer to.\n"
            "**Confidence:** high\n"
        )
        errors, _ = validate(content)
        assert len(errors) >= 1

    def test_ask_user_before_continuing_rejected(self):
        content = (
            "## Lesson 1\n"
            "Ask the user before continuing.\n"
            "**Confidence:** high\n"
        )
        errors, _ = validate(content)
        assert len(errors) >= 1

    def test_chinese_first_ask_rejected(self):
        """先问用户 — must be rejected"""
        content = (
            "## 先确认\n"
            "遇到错误时，先问用户具体发生了什么。\n"
            "**Confidence:** high\n"
        )
        errors, _ = validate(content)
        assert len(errors) >= 1

    def test_chinese_confirm_before_proceed_rejected(self):
        """先确认再执行 — must be rejected"""
        content = (
            "## 确认后执行\n"
            "先确认用户意图再继续执行。\n"
            "**Confidence:** high\n"
        )
        errors, _ = validate(content)
        assert len(errors) >= 1

    def test_ask_clarifying_questions_without_always_rejected(self):
        """ask clarifying questions — even without 'always' — must be rejected"""
        content = (
            "## When user points out error\n"
            "Do immediately acknowledge, ask clarifying questions to understand.\n"
            "**Confidence:** high\n"
            "**Evidence:** user_correction\n"
        )
        errors, _ = validate(content)
        assert len(errors) >= 1
        assert any("anti_question" == e.rule for e in errors)

    def test_acceptable_rewrite_passes(self):
        """re-check assumptions, ask only if critical — should pass"""
        content = (
            "## Verify state before reporting\n"
            "When reporting progress, re-check assumptions first. "
            "Ask only if a critical fact is missing.\n"
            "**Confidence:** high\n"
            "**Evidence:** state_mismatch\n"
        )
        errors, _ = validate(content)
        assert len(errors) == 0


# ══════════════════════════════════════════════════════════════════════════════
# evidence tests
# ══════════════════════════════════════════════════════════════════════════════

class TestEvidence:
    def test_no_evidence_warns(self):
        content = (
            "## Lesson 1\n"
            "Do not make mistakes.\n"
            "**Confidence:** high\n"
        )
        _, warnings = validate(content)
        assert any("evidence" in w.rule for w in warnings)

    def test_has_evidence_no_warn(self):
        content = (
            "## Lesson 1\n"
            "Do not make mistakes.\n"
            "**Confidence:** high\n"
            "**Evidence:** user_correction\n"
        )
        _, warnings = validate(content)
        assert not any("evidence" in w.rule for w in warnings)


# ══════════════════════════════════════════════════════════════════════════════
# limit tests
# ══════════════════════════════════════════════════════════════════════════════

class TestLimits:
    def test_over_5_rejected(self):
        lessons = "\n".join([
            f"## Lesson {i}\nbody\n**Confidence:** high\n"
            for i in range(7)
        ])
        errors, _ = validate(lessons)
        assert any("count" in str(e) for e in errors)

    def test_exactly_5_passes(self):
        lessons = "\n".join([
            f"## Lesson {i}\nbody\n**Confidence:** high\n"
            for i in range(5)
        ])
        errors, _ = validate(lessons)
        assert len(errors) == 0

    def test_over_2kb_rejected(self):
        content = "## Big Lesson\n" + "x" * 2100 + "\n**Confidence:** high\n"
        errors, _ = validate(content)
        assert any("size" in str(e) for e in errors)


# ══════════════════════════════════════════════════════════════════════════════
# security tests
# ══════════════════════════════════════════════════════════════════════════════

class TestSecurity:
    def test_api_key_rejected(self):
        content = (
            "## Lesson 1\n"
            "Use sk-abc123 for API calls.\n"
            "**Confidence:** high\n"
        )
        errors, _ = validate(content)
        assert any("security" in str(e) for e in errors)

    def test_token_rejected(self):
        content = (
            "## Lesson 1\n"
            "Set token=xyz in config.\n"
            "**Confidence:** high\n"
        )
        errors, _ = validate(content)
        assert any("security" in str(e) for e in errors)

    def test_clean_content_passes(self):
        content = (
            "## Verify state\n"
            "Re-check before reporting.\n"
            "**Confidence:** high\n"
        )
        errors, _ = validate(content)
        assert len(errors) == 0


# ══════════════════════════════════════════════════════════════════════════════
# auto-fix simulation tests
# ══════════════════════════════════════════════════════════════════════════════

class TestAutoFixSimulation:
    """Simulate what a critic fix should look like — these patterns must pass."""

    def test_rewrite_always_ask_to_check_first(self):
        content = (
            "## Verify state before responding\n"
            "When user corrects you, re-check assumptions first. "
            "Ask only if a critical fact is missing.\n"
            "**Confidence:** high\n"
            "**Evidence:** user_correction\n"
        )
        errors, _ = validate(content)
        assert len(errors) == 0

    def test_rewrite_confirm_to_proceed_from_context(self):
        content = (
            "## Follow known rules first\n"
            "When user asks to follow rules, proceed from available "
            "context using known rules. Do not ask for clarification "
            "unless the rule cannot be identified.\n"
            "**Confidence:** high\n"
            "**Evidence:** rule_misinterpretation\n"
        )
        errors, _ = validate(content)
        assert len(errors) == 0

    def test_empty_content_errors(self):
        errors, _ = validate("")
        assert len(errors) >= 1

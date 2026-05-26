#!/usr/bin/env python3
"""
p3b_apply_pipeline.py — P3B Auto Quality Gate.

LLM generate → deterministic validate → judge/critic auto-fix → re-validate.
Only writes active_lessons.md when validator passes all rules.

Usage:
  python p3b_apply_pipeline.py              # dry-run (validate only, no write)
  python p3b_apply_pipeline.py --apply      # write active_lessons.md if validator passes
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sys
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

# ── paths ──────────────────────────────────────────────────────────────────
_MEMORY_DIR = Path.home() / ".hermes" / "profiles" / "me" / "memory"
_EVENTS_PATH = _MEMORY_DIR / "events" / "event_candidates.jsonl"
_ACTIVE_LESSONS = _MEMORY_DIR / "active_lessons.md"
_MODELS_DIR = _MEMORY_DIR / "models"

# ── LLM config ─────────────────────────────────────────────────────────────
_MODEL = "deepseek-v4-flash"
_BASE_URL = "https://api.deepseek.com/v1"
_MAX_TOKENS = 1024
_TEMPERATURE = 0.1  # Lower temp for more consistent quality gate

# ── limits ─────────────────────────────────────────────────────────────────
_MAX_LESSONS = 5
_MAX_TOTAL_BYTES = 2048

# ── Anti-question patterns — HARD REJECT ───────────────────────────────────
_ANTI_QUESTION_PATTERNS = [
    # English patterns
    (re.compile(r"(always\s+)?ask\s+clarifying\s+question", re.IGNORECASE),
     "re-check assumptions first, ask only if critical fact missing"),
    (re.compile(r"first\s+ask\s+the\s+user", re.IGNORECASE),
     "proceed from available context, ask only if blocked"),
    (re.compile(r"confirm\s+expectations?\s+before\s+proceeding", re.IGNORECASE),
     "re-verify state, proceed using known rules"),
    (re.compile(r"ask\s+which\s+(rules?|context).{0,20}they\s+(mean|refer)", re.IGNORECASE),
     "use current confirmed context and known rules first"),
    (re.compile(r"ask\s+the\s+user\s+before\s+continuing", re.IGNORECASE),
     "check assumptions, continue unless blocked by missing fact"),
    # Chinese patterns
    (re.compile(r"先问.*(用户|对方|他)"),
     "先复核事实，缺关键信息时才问"),
    (re.compile(r"(反复|总要|每次.*都要).*问"),
     "使用已有上下文推进，不问可自行判断的问题"),
    (re.compile(r"先确认.*再(继续|执行|推进)"),
     "先复核状态，使用已知规则，缺关键事实时才问"),
]

# ── sensitive patterns ─────────────────────────────────────────────────────
_SENSITIVE_RE = re.compile(
    r"(sk-|LTAI|AKIA|Bearer\s|token=|password=|api_key|secret|accesskey|"
    r"Authorization:|x-api-key|PRIVATE KEY)",
    re.IGNORECASE,
)

# ── valid lesson sources ───────────────────────────────────────────────────
_VALID_SOURCES = [
    "user_correction", "assistant_failure", "deprecated_model",
    "state_mismatch", "rule_misinterpretation", "context_loss",
]


# ══════════════════════════════════════════════════════════════════════════════
# API helpers
# ══════════════════════════════════════════════════════════════════════════════

def _load_api_key() -> str:
    env_path = Path.home() / ".hermes" / "profiles" / "me" / ".env"
    if env_path.is_file():
        for line in env_path.read_text().split("\n"):
            if line.startswith("DEEPSEEK_API_KEY="):
                return line.split("=", 1)[1].strip().strip('"').strip("'")
    return os.environ.get("DEEPSEEK_API_KEY", "")


def _call_llm(prompt: str, system: str = "") -> str:
    api_key = _load_api_key()
    if not api_key:
        raise RuntimeError("DEEPSEEK_API_KEY not found")
    body = {
        "model": _MODEL,
        "messages": [],
        "max_tokens": _MAX_TOKENS,
        "temperature": _TEMPERATURE,
    }
    if system:
        body["messages"].append({"role": "system", "content": system})
    body["messages"].append({"role": "user", "content": prompt})

    data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(
        f"{_BASE_URL}/chat/completions",
        data=data,
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key}",
        },
    )
    with urllib.request.urlopen(req) as resp:
        result = json.loads(resp.read().decode("utf-8"))
        return result["choices"][0]["message"]["content"]


def _estimate_tokens(text: str) -> int:
    return max(1, len(text) // 3)


def _load_events() -> list[dict]:
    if not _EVENTS_PATH.is_file():
        return []
    events = []
    for line in _EVENTS_PATH.read_text(encoding="utf-8").strip().split("\n"):
        if not line.strip():
            continue
        try:
            events.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return events


# ══════════════════════════════════════════════════════════════════════════════
# Deterministic Validator — 7 rules
# ══════════════════════════════════════════════════════════════════════════════

class ValidationError(Exception):
    def __init__(self, rule: str, detail: str, lesson_idx: int = -1):
        self.rule = rule
        self.detail = detail
        self.lesson_idx = lesson_idx
        super().__init__(f"[{rule}] Lesson #{lesson_idx}: {detail}")


class ValidationWarning:
    def __init__(self, rule: str, detail: str, lesson_idx: int = -1, suggestion: str = ""):
        self.rule = rule
        self.detail = detail
        self.lesson_idx = lesson_idx
        self.suggestion = suggestion


def _parse_lessons(content: str) -> list[dict]:
    """Parse markdown lessons into structured list."""
    # Split on ## headings
    blocks = re.split(r"\n(?=##\s)", content)
    lessons = []
    for block in blocks:
        block = block.strip()
        if not block or not block.startswith("##"):
            continue
        # Extract lesson text
        header_match = re.match(r"##\s*(.+)", block)
        title = header_match.group(1).strip() if header_match else ""
        # Body is everything after title until next heading or end
        body = block[len(header_match.group(0)):].strip() if header_match else block

        # Extract confidence
        conf_match = re.search(r"\*\*[Cc]onfidence:\*\*\s*(\w+)", body)
        confidence = conf_match.group(1).lower() if conf_match else "medium"

        # Extract source/evidence
        source_match = re.search(r"(?:source|evidence|from|based on)[:\s]+([^\n]+)", body, re.IGNORECASE)
        source = source_match.group(1).strip() if source_match else ""

        # Clean body (remove confidence line, etc.)
        clean_body = re.sub(r"\*\*[Cc]onfidence:\*\*.*", "", body).strip()

        lessons.append({
            "raw": block,
            "title": title[:120],
            "body": clean_body,
            "confidence": confidence,
            "source": source,
        })
    return lessons


def validate(content: str) -> tuple[list[ValidationError], list[ValidationWarning]]:
    """Run all 7 validation rules. Returns (errors, warnings)."""
    errors: list[ValidationError] = []
    warnings: list[ValidationWarning] = []

    # Rule 0: Parse
    lessons = _parse_lessons(content)
    if not lessons:
        errors.append(ValidationError("parse", "No lessons found in content"))
        return errors, warnings

    # ── Rule 1: Anti-question patterns (HARD REJECT) ──────────────────────
    for i, lesson in enumerate(lessons):
        full_text = lesson["raw"].lower()
        for pattern, suggestion in _ANTI_QUESTION_PATTERNS:
            if pattern.search(full_text):
                errors.append(ValidationError(
                    "anti_question",
                    f"'{pattern.pattern}' → rewrite as: '{suggestion}'",
                    lesson_idx=i,
                ))
                break  # One error per lesson

    # ── Rule 2: Evidence required ─────────────────────────────────────────
    for i, lesson in enumerate(lessons):
        if not lesson["source"]:
            warnings.append(ValidationWarning(
                "evidence",
                "No source/evidence for lesson — may be fabricated",
                lesson_idx=i,
                suggestion="Add source event type (user_correction, assistant_failure, etc.)",
            ))

    # ── Rule 3: When→Do format ────────────────────────────────────────────
    for i, lesson in enumerate(lessons):
        text = lesson["body"].lower()
        # Check for actionable pattern
        has_action = bool(re.search(r"(when|do|avoid|re-check|verify|proceed|stop|use|check)", text))
        if not has_action:
            warnings.append(ValidationWarning(
                "format",
                "Not clearly actionable — use When/Do/Avoid format",
                lesson_idx=i,
            ))

    # ── Rule 4: Security scan ─────────────────────────────────────────────
    if _SENSITIVE_RE.search(content):
        errors.append(ValidationError(
            "security",
            "Contains sensitive tokens/keys/secrets — BLOCKED",
        ))

    # ── Rule 5: Count limit ───────────────────────────────────────────────
    if len(lessons) > _MAX_LESSONS:
        errors.append(ValidationError(
            "count",
            f"Has {len(lessons)} lessons, max {_MAX_LESSONS}",
        ))

    # ── Rule 6: Size limit ────────────────────────────────────────────────
    size = len(content.encode("utf-8"))
    if size > _MAX_TOTAL_BYTES:
        errors.append(ValidationError(
            "size",
            f"Content is {size} bytes, max {_MAX_TOTAL_BYTES}",
        ))

    # ── Rule 7: Dedup ─────────────────────────────────────────────────────
    seen = {}
    for i, lesson in enumerate(lessons):
        key = hashlib.md5(lesson["body"].lower().encode()).hexdigest()[:16]
        if key in seen:
            warnings.append(ValidationWarning(
                "dedup",
                f"Similar to lesson #{seen[key]}: '{lesson['title'][:60]}'",
                lesson_idx=i,
                suggestion="Merge or remove duplicate",
            ))
        else:
            seen[key] = i

    # ── Rule 8: One-time test content ─────────────────────────────────────
    for i, lesson in enumerate(lessons):
        text = lesson["raw"].lower()
        if re.search(r"(test|mock|debug\s+only|temporary)", text):
            warnings.append(ValidationWarning(
                "test_content",
                "May contain one-time test content",
                lesson_idx=i,
            ))

    return errors, warnings


def validator_passed(errors: list) -> bool:
    return len(errors) == 0


# ══════════════════════════════════════════════════════════════════════════════
# Phase 1: LLM Generate
# ══════════════════════════════════════════════════════════════════════════════

GENERATE_SYSTEM = (
    "You are a memory lesson synthesizer for a coding assistant.\n\n"
    "Given conversation events where users corrected the assistant, produce "
    "compact, actionable, general-purpose lessons.\n\n"
    "CRITICAL: Output MUST be in English only. Never output Chinese.\n\n"
    "RULES:\n"
    "- Output 3-5 lessons only.\n"
    "- Each lesson starts with '## ' heading.\n"
    "- Format: ## [lesson title]\n  [body text]\n  **Confidence:** high|medium|low\n"
    "  **Evidence:** <source event type, e.g. user_correction>\n"
    "- Use 'When ... Do ... Avoid ...' pattern.\n"
    "- NEVER write: 'always ask clarifying questions', 'first ask the user',\n"
    "  'confirm expectations before proceeding', 'ask which rules/context they mean'.\n"
    "- Instead write: 're-check assumptions first', 'proceed from available context',\n"
    "  'ask only if a critical fact is missing'.\n"
    "- User prefers: concise answers, no unnecessary confirmation, don't bounce\n"
    "  questions back to user, verify facts first then provide actionable fix.\n"
    "- Lessons must be general rules, NOT project-specific model names or file paths.\n"
    "- No tokens, secrets, passwords, or cookies in output.\n"
    "- No one-time test content as permanent rules.\n"
    "- Total output must be under 2 KB."
)


def _generate_lessons(events: list[dict]) -> str:
    items = []
    for i, ev in enumerate(events[-8:], 1):  # Last 8 events max
        reason = ev.get("trigger_reason", "unknown")
        user = ev.get("user_message_excerpt", "")[:100]
        resp = ev.get("assistant_response_excerpt", "")[:100]
        items.append(
            f"Event {i}:\n"
            f"  trigger: {reason}\n"
            f"  user: {user}\n"
            f"  assistant: {resp}\n"
        )

    prompt = "Events from recent conversations:\n\n" + "\n".join(items)
    prompt += "\n\nGenerate the lessons now."

    raw = _call_llm(prompt, system=GENERATE_SYSTEM)
    return raw.strip()


# ══════════════════════════════════════════════════════════════════════════════
# Phase 2: LLM Judge/Critic (auto-fix)
# ══════════════════════════════════════════════════════════════════════════════

CRITIC_SYSTEM = (
    "You are a quality reviewer for coding assistant memory lessons. "
    "Given lessons with validation errors/warnings, rewrite ONLY the problematic "
    "lessons. Keep correct lessons unchanged.\n\n"
    "REWRITE RULES:\n"
    "- Remove all forms of 'always ask clarifying questions', 'first ask the user',\n"
    "  'confirm expectations before proceeding', 'ask which rules they mean'.\n"
    "- Replace with: 're-check assumptions first', 'use available context',\n"
    "  'ask only if a critical fact is missing'.\n"
    "- Keep the When→Do→Avoid actionable format.\n"
    "- Add an **Evidence:** line if missing.\n"
    "- Merge duplicate lessons.\n"
    "- Keep output ≤5 lessons, ≤2KB total.\n"
    "- Output the COMPLETE corrected markdown, not just changed lessons.\n"
    "- Do NOT add new lessons — only fix existing ones.\n"
    "- Write in English.\n"
    "- No tokens/secrets/passwords in output."
)


def _critic_fix(raw_lessons: str, errors: list, warnings: list) -> str:
    issues = []
    for e in errors:
        issues.append(f"ERROR: {e}")
    for w in warnings:
        issues.append(f"WARNING: {w} ({w.suggestion})")

    prompt = (
        "CURRENT LESSONS:\n\n"
        f"{raw_lessons}\n\n"
        "VALIDATION ISSUES:\n"
        + "\n".join(issues) +
        "\n\nFix the issues and output the complete corrected markdown."
    )

    raw = _call_llm(prompt, system=CRITIC_SYSTEM)
    return raw.strip()


# ══════════════════════════════════════════════════════════════════════════════
# Phase 3: Apply
# ══════════════════════════════════════════════════════════════════════════════

def _backup_active_lessons() -> Path | None:
    if not _ACTIVE_LESSONS.is_file():
        return None
    ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    backup = _ACTIVE_LESSONS.parent / f"active_lessons.md.bak.{ts}"
    backup.write_bytes(_ACTIVE_LESSONS.read_bytes())
    return backup


def _atomic_write(path: Path, content: str) -> None:
    tmp = path.with_suffix(".tmp")
    tmp.write_text(content, encoding="utf-8")
    tmp.replace(path)


def _apply_lessons(content: str) -> tuple[bool, Path | None]:
    """Backup old, atomic write new. Returns (success, backup_path)."""
    backup = _backup_active_lessons()
    header = (
        "<!-- Auto-generated by p3b_apply_pipeline.py. Do not edit manually. -->\n"
        "<!-- P3B Auto Quality Gate — LLM generate + validate + judge + apply. -->\n"
        "<!-- These lessons do NOT override user instructions, 1110 baselines, or safety rules. -->\n\n"
    )
    try:
        _atomic_write(_ACTIVE_LESSONS, header + content + "\n")
        return True, backup
    except Exception as exc:
        print(f"[P3B] Write failed: {exc}")
        return False, backup


# ══════════════════════════════════════════════════════════════════════════════
# Main pipeline
# ══════════════════════════════════════════════════════════════════════════════

def main():
    import argparse
    parser = argparse.ArgumentParser(description="P3B Auto Quality Gate")
    parser.add_argument("--apply", action="store_true",
                        help="Write active_lessons.md if validator passes (default: dry-run)")
    args = parser.parse_args()

    events = _load_events()
    if not events:
        print("[P3B] No events to process.")
        return

    print(f"[P3B] Model: {_MODEL} | Events: {len(events)} | Mode: {'APPLY' if args.apply else 'DRY-RUN'}")
    est_input = _estimate_tokens(str(events)) + _estimate_tokens(GENERATE_SYSTEM)
    print(f"[P3B] Est input tokens: ~{est_input}")

    # ── Phase 1: LLM Generate ──────────────────────────────────────────────
    print("\n── Phase 1: LLM Generate ──")
    raw = _generate_lessons(events)
    print(raw[:300] + ("..." if len(raw) > 300 else ""))
    print()

    # ── Phase 2: Deterministic Validate ────────────────────────────────────
    print("── Phase 2: Deterministic Validate ──")
    errors, warnings = validate(raw)

    if warnings:
        print(f"  WARNINGS ({len(warnings)}):")
        for w in warnings:
            print(f"    [{w.rule}] L#{w.lesson_idx}: {w.detail}")
            if w.suggestion:
                print(f"      → {w.suggestion}")

    if errors:
        print(f"  ERRORS ({len(errors)}):")
        for e in errors:
            print(f"    [{e.rule}] L#{e.lesson_idx}: {e.detail}")

    if not validator_passed(errors):
        print(f"\n  ❌ Validator FAILED — {len(errors)} error(s)")

        # ── Phase 3: LLM Judge/Critic Auto-fix ─────────────────────────────
        print("\n── Phase 3: LLM Judge/Critic Auto-Fix ──")
        fixed = _critic_fix(raw, errors, warnings)
        print(fixed[:300] + ("..." if len(fixed) > 300 else ""))
        print()

        # ── Re-validate ────────────────────────────────────────────────────
        print("── Phase 4: Re-validate ──")
        errors2, warnings2 = validate(fixed)
        if warnings2:
            print(f"  WARNINGS: {len(warnings2)}")
            for w in warnings2:
                print(f"    [{w.rule}] L#{w.lesson_idx}: {w.detail}")
        if errors2:
            print(f"  ERRORS: {len(errors2)}")
            for e in errors2:
                print(f"    [{e.rule}] L#{e.lesson_idx}: {e.detail}")

        if not validator_passed(errors2):
            print(f"\n  ❌ Auto-fix FAILED — {len(errors2)} error(s) remain. Will NOT apply.")
            print("  Reasons:")
            for e in errors2:
                print(f"    - {e}")
            return
        else:
            print("\n  ✅ Auto-fix PASSED")
            raw = fixed
    else:
        print("\n  ✅ Validator PASSED (first attempt)")

    # ── Phase 5: Apply ────────────────────────────────────────────────────
    print(f"\n{'='*60}")
    print("FINAL LESSONS:")
    print(f"{'='*60}")
    print(raw)
    print(f"{'='*60}")
    size = len(raw.encode("utf-8"))
    lesson_count = len(_parse_lessons(raw))
    print(f"Lessons: {lesson_count} | Size: {size} bytes | ≤5: {'✅' if lesson_count <= 5 else '❌'} | ≤2KB: {'✅' if size <= 2048 else '❌'}")

    if args.apply:
        print("\n── Apply ──")
        ok, backup = _apply_lessons(raw)
        if ok:
            print(f"  ✅ Written to {_ACTIVE_LESSONS}")
            if backup:
                print(f"  📦 Backup: {backup}")
        else:
            print("  ❌ Write failed — old file preserved")
    else:
        print("\n[P3B] Dry-run complete. Use --apply to write active_lessons.md.")


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""
reflect.py — P2 deterministic reflection generator.

Reads event_candidates.jsonl, generates structured reflection markdown
for each unprocessed candidate.  P2 uses templates — no LLM calls.

Usage:
  python reflect.py                     # dry-run (print what would happen)
  python reflect.py --apply             # write reflection files
  python reflect.py --id <event_id>     # process single event
"""

import argparse
import hashlib
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

_MEMORY_DIR = Path.home() / ".hermes" / "profiles" / "me" / "memory"
_EVENTS_PATH = _MEMORY_DIR / "events" / "event_candidates.jsonl"
_REFLECTIONS_DIR = _MEMORY_DIR / "reflections"
_MODELS_DIR = _MEMORY_DIR / "models"

_MAX_EXCERPT_CHARS = 120
_MAX_LESSON_CHARS = 300

# ── sensitive content filter ────────────────────────────────────────────────
_SENSITIVE_PATTERNS = re.compile(
    r"(sk-|LTAI|AKIA|Bearer\s|token=|password=|api_key|secret|accesskey|"
    r"Authorization:|x-api-key|PRIVATE KEY)",
    re.IGNORECASE,
)


def _is_sensitive(text: str) -> bool:
    return bool(_SENSITIVE_PATTERNS.search(text))


def _slug(text: str, max_len: int = 40) -> str:
    """Generate a safe filename slug."""
    raw = re.sub(r"[^\w\-]", "_", text)[:max_len].strip("_")
    return raw or "reflection"


def _safe_excerpt(text: str, max_chars: int = _MAX_EXCERPT_CHARS) -> str:
    if not isinstance(text, str):
        return ""
    if len(text) <= max_chars:
        return text
    return text[:max_chars] + "…"


def _event_id(event: dict) -> str:
    """Deterministic event ID from input_sha256."""
    return (event.get("input_sha256") or hashlib.sha256(b"").hexdigest())[:16]


def _lesson_from_event(event: dict) -> dict:
    """Extract a deterministic lesson from a candidate event."""
    reason = event.get("trigger_reason", "unknown")
    um = event.get("user_message_excerpt", "")
    ar = event.get("assistant_response_excerpt", "")

    if _is_sensitive(um) or _is_sensitive(ar):
        return None

    # ── mapping trigger → lesson ────────────────────────────────────────────
    lesson_map = {
        "user_correction:不对": {
            "issue": "User indicated the assistant's response was incorrect",
            "lesson": "When user says '不对' (not right), stop and re-examine assumptions. Do not defend the previous answer.",
            "confidence": "high",
        },
        "user_correction:错了": {
            "issue": "User indicated a mistake in assistant's output",
            "lesson": "Acknowledge the error immediately, identify the root cause, and provide corrected output.",
            "confidence": "high",
        },
        "user_correction:不是": {
            "issue": "User rejected assistant's interpretation",
            "lesson": "Clarify intent before proceeding. '不是' means the user's mental model differs from the assistant's.",
            "confidence": "medium",
        },
        "user_correction:没修好": {
            "issue": "A fix did not resolve the problem",
            "lesson": "After a fix, verify with the user before claiming completion. A fix that 'works locally' may not work in production.",
            "confidence": "high",
        },
        "user_correction:失败": {
            "issue": "User reported a failure",
            "lesson": "When a task fails, investigate the root cause before attempting another fix. Repeated blind fixes compound errors.",
            "confidence": "high",
        },
        "user_correction:你理解错了": {
            "issue": "Assistant misunderstood user intent",
            "lesson": "Ask clarifying questions before executing. Misunderstanding user intent leads to wasted work.",
            "confidence": "high",
        },
        "user_correction:不行": {
            "issue": "User rejected the proposed approach",
            "lesson": "When user rejects an approach, propose alternatives. Persisting with a rejected approach erodes trust.",
            "confidence": "high",
        },
        "user_correction:不能这么做": {
            "issue": "Assistant proposed a disallowed action",
            "lesson": "Respect user-defined boundaries. Never bypass explicit constraints.",
            "confidence": "high",
        },
        "user_correction:不是这个意思": {
            "issue": "Assistant misinterpreted user intent",
            "lesson": "Paraphrase user intent before taking action. Confirmation prevents misalignment.",
            "confidence": "high",
        },
        "assistant_failure:失败": {
            "issue": "Assistant reported a failure",
            "lesson": "When reporting failure, always provide: what was tried, what failed, what's next.",
            "confidence": "medium",
        },
        "assistant_failure:不通过": {
            "issue": "A verification or test did not pass",
            "lesson": "Tests not passing means the fix is incomplete. Do not claim completion when tests fail.",
            "confidence": "high",
        },
        "assistant_failure:error": {
            "issue": "An error occurred during execution",
            "lesson": "Catch and report errors with context. Silent errors cause cascading failures.",
            "confidence": "medium",
        },
        "assistant_failure:exception": {
            "issue": "An exception was raised",
            "lesson": "Unhandled exceptions indicate missing error handling. Add guards before retrying.",
            "confidence": "medium",
        },
        "assistant_failure:traceback": {
            "issue": "A traceback occurred",
            "lesson": "Tracebacks are symptoms, not root causes. Diagnose before fixing.",
            "confidence": "medium",
        },
        "assistant_failure:无法": {
            "issue": "Assistant reported inability to complete task",
            "lesson": "When a task cannot be completed, explain why and suggest alternatives. Do not silently give up.",
            "confidence": "medium",
        },
        "assistant_failure:timeout": {
            "issue": "A tool call or model request timed out",
            "lesson": "Timeouts require retry with fallback or timeout increase. Do not retry indefinitely — set max retries and escalate after exhausting.",
            "confidence": "medium",
            "evidence": "Assistant response contains timeout/timeout error message",
            "root_cause": "Subprocess, API, or model call exceeded allowed time limit",
            "correct_rule": "After a timeout, retry with exponential backoff (max 3). If all fail, report failure with details — do not silently proceed.",
            "do_not_repeat": "Retrying the same operation without adjusting timeout or adding fallback logic",
            "applies_when": "When tool calls / network requests / model APIs return timeout errors",
            "does_not_apply_when": "When user mentions 'timeout' or '超时' in a question or comment — only assistant-side timeouts count",
            "next_action": "Audit the failed operation for root cause: network, overload, or timeout too conservative",
        },
        "assistant_failure:超时": {
            "issue": "A tool call or model request timed out (Chinese timeout)",
            "lesson": "Timeouts require retry with fallback or timeout increase. Do not retry indefinitely.",
            "confidence": "medium",
            "evidence": "Assistant response contains 超时/超时错误",
            "root_cause": "Subprocess, API, or model call exceeded allowed time limit",
            "correct_rule": "After a timeout, retry with exponential backoff (max 3). If all fail, report failure with details.",
            "do_not_repeat": "Retrying the same operation without adjusting timeout or adding fallback logic",
            "applies_when": "When tool calls / network requests / model APIs return 超时 errors",
            "does_not_apply_when": "When user mentions '超时' in a question — only assistant-side timeouts count",
            "next_action": "Audit the failed operation for root cause: network, overload, or timeout too conservative",
        },
    }

    entry = lesson_map.get(reason, {
        "issue": f"Unknown trigger: {reason}",
        "lesson": f"Review and document the pattern for trigger: {reason}",
        "confidence": "low",
        "evidence": f"Trigger reason: {reason}",
        "root_cause": f"Unmapped trigger: {reason} — needs manual review",
        "correct_rule": "Review and document this trigger pattern",
        "do_not_repeat": f"Blindly handling trigger: {reason} without root cause analysis",
        "applies_when": f"When trigger '{reason}' is detected",
        "does_not_apply_when": "When the same text appears in user chat (not failure output)",
        "next_action": f"Add explicit mapping for trigger: {reason} in _lesson_from_event",
    })

    # Build enhanced result with schema fields (P5Z-fix)
    result = {
        "issue": entry["issue"],
        "lesson": entry["lesson"],
        "confidence": entry["confidence"],
        "evidence": entry.get("evidence", f"Trigger reason: {reason}"),
        "root_cause": entry.get("root_cause", f"Failure type: {reason}"),
        "correct_rule": entry.get("correct_rule", entry.get("lesson", "")),
        "do_not_repeat": entry.get("do_not_repeat", "Repeating the same error without correction"),
        "applies_when": entry.get("applies_when", f"When {reason} is detected in assistant output"),
        "does_not_apply_when": entry.get("does_not_apply_when", "When the text is in user chat, not failure output"),
        "next_action": entry.get("next_action", "Verify the lesson in next turn and adjust if needed"),
    }
    return result


def _write_reflection(event: dict, lesson: dict, apply: bool = False) -> Path | None:
    """Generate and optionally write a reflection markdown file."""
    eid = _event_id(event)
    reason = event.get("trigger_reason", "unknown")
    slug = _slug(reason)
    ts = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M")
    filename = f"{ts}-{slug}-{eid}.md"
    path = _REFLECTIONS_DIR / filename

    content = f"""---
event_id: {eid}
input_sha256: {event.get("input_sha256", "")}
output_sha256: {event.get("output_sha256", "")}
trigger_reason: {reason}
session_id: {event.get("session_id", "")}
created_at: {datetime.now(timezone.utc).isoformat()}
confidence: {lesson["confidence"]}
source_type: event_candidate
evidence: {_safe_excerpt(lesson.get("evidence", ""), _MAX_EXCERPT_CHARS)}
root_cause: {_safe_excerpt(lesson.get("root_cause", ""), _MAX_EXCERPT_CHARS)}
correct_rule: {_safe_excerpt(lesson.get("correct_rule", ""), _MAX_EXCERPT_CHARS)}
do_not_repeat: {_safe_excerpt(lesson.get("do_not_repeat", ""), _MAX_EXCERPT_CHARS)}
applies_when: {_safe_excerpt(lesson.get("applies_when", ""), _MAX_EXCERPT_CHARS)}
does_not_apply_when: {_safe_excerpt(lesson.get("does_not_apply_when", ""), _MAX_EXCERPT_CHARS)}
next_action: {_safe_excerpt(lesson.get("next_action", ""), _MAX_EXCERPT_CHARS)}
---

# Reflection: {reason}

## observed_issue
{lesson["issue"]}

## user_message_excerpt
{_safe_excerpt(event.get("user_message_excerpt", ""))}

## assistant_response_excerpt
{_safe_excerpt(event.get("assistant_response_excerpt", ""))}

## evidence
{lesson.get("evidence", "N/A")}

## root_cause
{lesson.get("root_cause", "N/A")}

## correct_rule
{lesson.get("correct_rule", lesson.get("lesson", "N/A"))}

## do_not_repeat
{lesson.get("do_not_repeat", "N/A")}

## applies_when
{lesson.get("applies_when", "N/A")}

## does_not_apply_when
{lesson.get("does_not_apply_when", "N/A")}

## next_action
{lesson.get("next_action", "N/A")}

## likely_lesson
{lesson["lesson"]}

## suggested_active_lesson
{lesson["lesson"][:_MAX_LESSON_CHARS]}

## confidence
{lesson["confidence"]}
"""

    if apply:
        _REFLECTIONS_DIR.mkdir(parents=True, exist_ok=True)
        # Atomic write
        tmp = path.with_suffix(".tmp")
        tmp.write_text(content, encoding="utf-8")
        tmp.replace(path)
        print(f"  WROTE: {path}")
    else:
        print(f"  [DRY-RUN] Would write: {path}")
        print(f"    lesson: {lesson['lesson'][:80]}…")

    return path


def _is_processed(input_sha: str) -> bool:
    """Check if this event already has a reflection.

    Strategy (backward compatible):
    1. Search for full input_sha256 in reflection content (new files).
    2. Search for 16-char event_id in reflection filename (old files).
    """
    if not _REFLECTIONS_DIR.is_dir():
        return False

    eid_short = input_sha[:16] if input_sha else ""

    for f in _REFLECTIONS_DIR.glob("*.md"):
        # Fast path: check filename for short event_id (catches old + new)
        if eid_short and eid_short in f.name:
            return True

        # Full content scan for input_sha256 (catches matches even if filename changed)
        text = f.read_text(encoding="utf-8")
        if input_sha and input_sha in text:
            return True

    return False


def process_events(apply: bool = False, event_id: str = None) -> dict:
    """Main entry: process event_candidates.jsonl → reflections."""
    if not _EVENTS_PATH.is_file():
        print("No event_candidates.jsonl — nothing to reflect on.")
        return {"processed": 0, "skipped": 0, "sensitive": 0, "files": []}

    events = []
    for line in _EVENTS_PATH.read_text(encoding="utf-8").split("\n"):
        line = line.strip()
        if not line:
            continue
        try:
            events.append(json.loads(line))
        except json.JSONDecodeError:
            continue

    result = {"processed": 0, "skipped": 0, "sensitive": 0, "files": []}

    for ev in events:
        eid = _event_id(ev)

        # Filter by event_id if specified
        if event_id and eid != event_id:
            continue

        # Skip already processed
        if _is_processed(ev.get("input_sha256", "")):
            print(f"  SKIP (already processed): {eid}")
            result["skipped"] += 1
            continue

        lesson = _lesson_from_event(ev)
        if lesson is None:
            print(f"  BLOCKED (sensitive): {eid}")
            result["sensitive"] += 1
            continue

        path = _write_reflection(ev, lesson, apply=apply)
        if path:
            result["files"].append(str(path))
        result["processed"] += 1

    print(f"\nDone: {result['processed']} processed, {result['skipped']} skipped, {result['sensitive']} blocked")
    return result


def main():
    parser = argparse.ArgumentParser(description="P2 deterministic reflection generator")
    parser.add_argument("--apply", action="store_true", help="Actually write reflection files")
    parser.add_argument("--id", type=str, help="Process single event by event_id prefix")
    args = parser.parse_args()

    process_events(apply=args.apply, event_id=args.id)


if __name__ == "__main__":
    main()

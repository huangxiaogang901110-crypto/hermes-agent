#!/usr/bin/env python3
"""
candidate_quality_gate.py — P5Q Hermes self-review quality gate.

Two-layer filtering for memory candidates:
  1. Rule Gate: deterministic rejection of noise/sensitive/duplicate content.
  2. Hermes Self-Review: heuristic quality judgment based on candidate text.

Output:
  - latest_quality_gate_report.json  (full traceability)
  - review_pending_preview.md        (human-reviewable preview)

NEVER writes active_lessons.md.  NEVER calls --apply.  NO LLM calls.

Usage:
  python candidate_quality_gate.py                  # full run
  python candidate_quality_gate.py --summary-only   # print summary to stdout
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
_ACTIVE_LESSONS_PATH = _MEMORY_DIR / "active_lessons.md"
_QUALITY_REPORT_PATH = _MEMORY_DIR / "latest_quality_gate_report.json"
_PENDING_PREVIEW_PATH = _MEMORY_DIR / "review_pending_preview.md"

_MEMORY_CHAR_LIMIT = 2200
_MAX_EVENT_ID_LEN = 16

# ── sensitive patterns ───────────────────────────────────────────────────
_SENSITIVE_RE = re.compile(
    r"(sk-|LTAI|AKIA|Bearer\s|token=|password=|api_key|secret|accesskey|"
    r"Authorization:|x-api-key|PRIVATE KEY|webhook.*https?://)",
    re.IGNORECASE,
)

# ── noise patterns ──────────────────────────────────────────────────────
_NOISE_PATTERNS = [
    (re.compile(r"(/tmp/|/var/|/home/\w+/\.\w+/\w{8,})"), "temp_path"),
    (re.compile(r":\d{4,5}\b"), "port_number"),
    (re.compile(r"\bPID\s*\d+\b", re.IGNORECASE), "pid"),
    (re.compile(r"\b[0-9a-f]{7,40}\b"), "commit_hash"),
    (re.compile(r"test_[\w]+\.py"), "test_filename"),
    (re.compile(r"\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}"), "ip_address"),
]

# ── generic/vague rejection ─────────────────────────────────────────────
_GENERIC_PATTERNS = [
    re.compile(p, re.IGNORECASE) for p in [
        r"^be (more )?careful( next time)?\.?$",
        r"^pay attention\.?$",
        r"^do better\.?$",
        r"^improve\.?$",
        r"^fix the (bug|issue|problem)\.?$",
        r"^(just )?do it right\.?$",
        r"^make sure it works\.?$",
    ]
]


# ══════════════════════════════════════════════════════════════════════════
# helpers
# ══════════════════════════════════════════════════════════════════════════

def _load_active_lessons() -> str:
    if _ACTIVE_LESSONS_PATH.is_file():
        return _ACTIVE_LESSONS_PATH.read_text(encoding="utf-8").strip()
    return ""


def _load_event_candidates() -> list[dict]:
    events = []
    if _EVENTS_PATH.is_file():
        for line in _EVENTS_PATH.read_text(encoding="utf-8").split("\n"):
            line = line.strip()
            if not line:
                continue
            try:
                events.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return events


def _load_reflections() -> list[dict]:
    reflections = []
    if _REFLECTIONS_DIR.is_dir():
        for rf in sorted(_REFLECTIONS_DIR.glob("*.md")):
            try:
                text = rf.read_text(encoding="utf-8")
                # Parse YAML frontmatter
                if text.startswith("---"):
                    end = text.find("---", 3)
                    if end != -1:
                        frontmatter = {}
                        for line in text[3:end].strip().split("\n"):
                            if ":" in line:
                                k, v = line.split(":", 1)
                                frontmatter[k.strip()] = v.strip()
                        frontmatter["_body"] = text[end + 3:].strip()
                        frontmatter["_filename"] = rf.name
                        reflections.append(frontmatter)
                    else:
                        reflections.append({"_filename": rf.name, "_body": text, "_parse_error": "no closing ---"})
                else:
                    reflections.append({"_filename": rf.name, "_body": text})
            except Exception:
                reflections.append({"_filename": rf.name, "_body": "", "_parse_error": "read error"})
    return reflections


def _normalize(text: str) -> str:
    """Normalize for dedup comparison."""
    return re.sub(r"\s+", " ", str(text).lower()).strip()


def _sha256(text: str) -> str:
    return hashlib.sha256(str(text).encode("utf-8", errors="replace")).hexdigest()


# ══════════════════════════════════════════════════════════════════════════
# Layer 1: Rule Gate
# ══════════════════════════════════════════════════════════════════════════

def rule_gate(lesson_text: str, confidence: str, evidence: str,
              active_lessons_text: str, seen_lessons: set,
              current_byte_budget: int) -> tuple[bool, str, str]:
    """
    Returns (pass, reject_reason, filtered_by).
    """
    norm_text = _normalize(lesson_text)
    norm_evidence = _normalize(evidence or "")

    # 1. Sensitive content
    if _SENSITIVE_RE.search(lesson_text) or _SENSITIVE_RE.search(evidence or ""):
        return False, "sensitive content detected", "rule: sensitive"

    # 2. Confidence gate
    if confidence == "low":
        return False, "confidence=low", "rule: confidence"
    if confidence == "medium" and (not evidence or len(str(evidence).strip()) < 10):
        return False, "medium confidence + no evidence", "rule: medium_no_evidence"

    # 3. Missing evidence
    if not evidence or len(str(evidence).strip()) < 10:
        return False, "missing or insufficient evidence", "rule: no_evidence"

    # 4. Noise patterns
    for pattern, label in _NOISE_PATTERNS:
        if pattern.search(lesson_text) or pattern.search(norm_evidence):
            return False, f"noise pattern: {label}", f"rule: noise_{label}"

    # 5. Generic / vague
    for pattern in _GENERIC_PATTERNS:
        if pattern.match(norm_text.strip()):
            return False, "generic/vague content", "rule: generic"

    # 6. Too short (empty or just a few words)
    if len(norm_text.strip()) < 20:
        return False, "too short (potential empty/vague)", "rule: too_short"

    # 7. Duplicate with existing active_lessons
    if norm_text in _normalize(active_lessons_text):
        return False, "duplicate with existing active_lessons", "rule: dup_existing"

    # 8. Duplicate within batch
    if norm_text[:80] in seen_lessons:
        return False, "duplicate in current batch", "rule: dup_batch"

    # 9. Memory limit
    if current_byte_budget + len(lesson_text.encode("utf-8")) > _MEMORY_CHAR_LIMIT:
        return False, f"exceeds memory budget ({_MEMORY_CHAR_LIMIT}B)", "rule: memory_limit"

    return True, "", "rule: passed"


# ══════════════════════════════════════════════════════════════════════════
# Layer 2: Hermes Self-Review Gate
# ══════════════════════════════════════════════════════════════════════════

def hermes_self_review(lesson_text: str, event: dict, reflection: dict | None,
                       active_lessons_text: str) -> dict:
    """
    Heuristic self-review of a candidate lesson.

    Returns a dict with:
      - decision: keep | reject | rewrite_needed
      - reason: explanation
      - risk: none | low | medium | high
      - suggested_user_action: 入库 | 删除 | 改写
      - should_push_to_user: bool
      - rewrite_suggestion: str or None
    """
    norm_text = _normalize(lesson_text)
    norm_active = _normalize(active_lessons_text)
    trigger = event.get("trigger_reason", "")
    user_excerpt = _normalize(event.get("user_message_excerpt", ""))
    assistant_excerpt = _normalize(event.get("assistant_response_excerpt", ""))
    ref_body = _normalize(reflection.get("_body", "")) if reflection else ""

    # ── 1. REALLY from this event? ──────────────────────────────────────
    # Check if lesson text contains patterns from the event context
    event_context_keywords = set()
    for src in [trigger, user_excerpt, assistant_excerpt, ref_body]:
        for word in src.split():
            if len(word) > 4:
                event_context_keywords.add(word.lower().strip(".,;:!?\"'"))

    lesson_words = set(w.lower().strip(".,;:!?\"'") for w in lesson_text.split() if len(w) > 4)
    overlap = lesson_words & event_context_keywords
    if not overlap and len(lesson_words) > 3:
        return {
            "decision": "reject",
            "reason": "lesson text has no keyword overlap with source event — may be hallucinated/generic",
            "risk": "medium",
            "suggested_user_action": "删除",
            "should_push_to_user": False,
            "rewrite_suggestion": None,
        }

    # ── 2. One-time noise vs reusable? ─────────────────────────────────
    # Project-specific noise indicators
    one_time_indicators = [
        r"\bredo\b", r"\bre-run\b", r"\bagain\b", r"\bretry that\b",
        r"\bthis time\b", r"\bjust now\b", r"\b刚才\b", r"\b再来\b", r"\b重新\b",
    ]
    for pattern in one_time_indicators:
        if re.search(pattern, lesson_text, re.IGNORECASE):
            return {
                "decision": "reject",
                "reason": "appears to be one-time instruction, not reusable lesson",
                "risk": "low",
                "suggested_user_action": "删除",
                "should_push_to_user": False,
                "rewrite_suggestion": None,
            }

    # ── 3. Duplicate with existing 5? ──────────────────────────────────
    if norm_text[:80] in norm_active:
        return {
            "decision": "reject",
            "reason": "duplicate with existing active_lessons",
            "risk": "none",
            "suggested_user_action": "删除",
            "should_push_to_user": False,
            "rewrite_suggestion": None,
        }

    # ── 4. Clear trigger condition? ─────────────────────────────────────
    has_trigger = any(kw in norm_text for kw in [
        "when", "当", "if", "如果", "upon", "in case",
    ])
    has_behavior = any(kw in norm_text for kw in [
        "do", "avoid", "should", "must", "always", "never",
        "做", "避免", "应该", "必须", "始终", "从不",
    ])
    has_prohibition = any(kw in norm_text for kw in [
        "do not", "never", "avoid", "禁止", "不得", "不允许", "不能",
    ])

    # ── 5-6. Quality scoring ────────────────────────────────────────────
    quality_flags = 0
    if has_trigger: quality_flags += 1
    if has_behavior: quality_flags += 1
    if has_prohibition: quality_flags += 1
    if len(lesson_text) > 50: quality_flags += 1

    # ── 7. Is it suitable for user review? ──────────────────────────────
    if quality_flags >= 3:
        # High quality — push to user
        return {
            "decision": "keep",
            "reason": f"quality_score={quality_flags}/4: has trigger condition + behavioral guidance + prohibition",
            "risk": "low",
            "suggested_user_action": "入库",
            "should_push_to_user": True,
            "rewrite_suggestion": None,
        }
    elif quality_flags == 2:
        # Medium — needs rewrite suggestion
        suggestion = []
        if not has_trigger:
            suggestion.append("add trigger condition (when/if)")
        if not has_behavior:
            suggestion.append("add required behavior (do/should/must)")
        if not has_prohibition:
            suggestion.append("add prohibited behavior (do not/never/avoid)")
        if len(lesson_text) <= 50:
            suggestion.append("expand to >50 chars for specificity")

        return {
            "decision": "rewrite_needed",
            "reason": f"quality_score={quality_flags}/4: missing {'trigger' if not has_trigger else ''} {'behavior' if not has_behavior else ''} {'prohibition' if not has_prohibition else ''}",
            "risk": "medium",
            "suggested_user_action": "改写",
            "should_push_to_user": False,
            "rewrite_suggestion": "; ".join(suggestion) if suggestion else None,
        }
    else:
        return {
            "decision": "reject",
            "reason": f"quality_score={quality_flags}/4: lesson too vague — no clear trigger/behavior/prohibition",
            "risk": "high",
            "suggested_user_action": "删除",
            "should_push_to_user": False,
            "rewrite_suggestion": None,
        }


# ══════════════════════════════════════════════════════════════════════════
# Main pipeline
# ══════════════════════════════════════════════════════════════════════════

def run_gate() -> dict:
    active_text = _load_active_lessons()
    events = _load_event_candidates()
    reflections = _load_reflections()

    # Map reflections by event_id
    ref_by_event = {}
    for r in reflections:
        eid = r.get("event_id", "") or r.get("source_event_id", "")
        if eid:
            ref_by_event[eid] = r

    results = []
    seen_lessons = set()
    kept = []
    rejected = []
    rewrite = []
    current_budget = len(active_text.encode("utf-8"))

    for event in events:
        trigger = event.get("trigger_reason", "unknown")
        user_excerpt = event.get("user_message_excerpt", "")
        assistant_excerpt = event.get("assistant_response_excerpt", "")
        event_id = (event.get("input_sha256") or hashlib.sha256(b"").hexdigest())[:16]
        confidence = "medium"  # default from event

        # Find linked reflection
        reflection = None
        for r in reflections:
            if r.get("input_sha256", "") == event.get("input_sha256", ""):
                reflection = r
                break
        if not reflection:
            for r in reflections:
                if event_id in r.get("_body", "") or event_id == r.get("event_id", ""):
                    reflection = r
                    break

        # Extract lesson text
        lesson_text = ""
        evidence = ""
        if reflection and "_body" in reflection:
            body = reflection["_body"]
            # Try suggested_active_lesson
            m = re.search(r"## suggested_active_lesson\n(.+?)(?:\n##|\n---|\Z)", body, re.DOTALL)
            if m:
                lesson_text = m.group(1).strip()
            else:
                # Fallback: likely_lesson
                m = re.search(r"## likely_lesson\n(.+?)(?:\n##|\n---|\Z)", body, re.DOTALL)
                if m:
                    lesson_text = m.group(1).strip()
            # Evidence
            m = re.search(r"## evidence\n(.+?)(?:\n##|\n---|\Z)", body, re.DOTALL)
            if m:
                evidence = m.group(1).strip()
            # Confidence from reflection
            ref_conf = reflection.get("confidence", "")
            if ref_conf:
                confidence = ref_conf

        if not lesson_text:
            # No extractable lesson — skip this event entirely
            results.append({
                "id": f"event_{event_id}",
                "event_id": event_id,
                "trigger": trigger,
                "source_event": user_excerpt[:80] if user_excerpt else trigger,
                "source_reflection": reflection.get("_filename", "N/A") if reflection else "N/A",
                "lesson_text": "(no lesson extractable)",
                "evidence": evidence,
                "rule_gate_decision": "reject",
                "rule_gate_reason": "no extractable lesson",
                "rule_filtered_by": "rule: no_lesson",
                "hermes_self_review_decision": "reject",
                "hermes_review_reason": "no lesson text to review",
                "risk": "none",
                "suggested_user_action": "删除",
                "should_push_to_user": False,
                "rewrite_suggestion": None,
            })
            continue

        # ── Layer 1: Rule Gate ──────────────────────────────────────────
        rule_pass, rule_reason, rule_filter = rule_gate(
            lesson_text, confidence, evidence, active_text, seen_lessons, current_budget
        )

        # ── Layer 2: Hermes Self-Review ─────────────────────────────────
        hermes_result = hermes_self_review(lesson_text, event, reflection, active_text)

        entry = {
            "id": f"event_{event_id}",
            "event_id": event_id,
            "trigger": trigger,
            "source_event": user_excerpt[:80] if user_excerpt else trigger,
            "source_reflection": reflection.get("_filename", "N/A") if reflection else "N/A",
            "lesson_text": lesson_text[:300],
            "lesson_text_sha256": _sha256(lesson_text),
            "evidence": evidence[:200] if evidence else "",
            "confidence": confidence,
            "rule_gate_decision": "pass" if rule_pass else "reject",
            "rule_gate_reason": rule_reason,
            "rule_filtered_by": rule_filter,
            "hermes_self_review_decision": hermes_result["decision"],
            "hermes_review_reason": hermes_result["reason"],
            "risk": hermes_result["risk"],
            "suggested_user_action": hermes_result["suggested_user_action"],
            "should_push_to_user": hermes_result["should_push_to_user"],
            "rewrite_suggestion": hermes_result.get("rewrite_suggestion"),
        }

        if rule_pass and hermes_result["decision"] == "keep":
            kept.append(entry)
            seen_lessons.add(_normalize(lesson_text)[:80])
        elif rule_pass and hermes_result["decision"] == "rewrite_needed":
            rewrite.append(entry)
            seen_lessons.add(_normalize(lesson_text)[:80])  # also dedup rewrite
        else:
            rejected.append(entry)

        results.append(entry)

    # ── Generate report ─────────────────────────────────────────────────
    summary = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "total_candidates": len(results),
        "rule_passed": sum(1 for r in results if r["rule_gate_decision"] == "pass"),
        "rule_rejected": sum(1 for r in results if r["rule_gate_decision"] == "reject"),
        "hermes_kept": len(kept),
        "hermes_rewrite_needed": len(rewrite),
        "hermes_rejected": len(rejected),
        "active_lessons_md5": _sha256(active_text),
        "memory_char_limit": _MEMORY_CHAR_LIMIT,
        "active_lessons_byte_count": len(active_text.encode("utf-8")),
    }

    report = {
        "summary": summary,
        "candidates": results,
    }

    return report, kept, rewrite


def write_outputs(report: dict, kept: list, rewrite: list):
    # JSON report
    _QUALITY_REPORT_PATH.parent.mkdir(parents=True, exist_ok=True)
    _QUALITY_REPORT_PATH.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")

    # Preview markdown
    lines = [
        "# Pending Review Preview",
        f"",
        f"**Generated:** {report['summary']['generated_at']}",
        f"**Total candidates:** {report['summary']['total_candidates']}",
        f"**Rule passed:** {report['summary']['rule_passed']}",
        f"**Hermes KEPT:** {report['summary']['hermes_kept']}",
        f"**Hermes REWRITE_NEEDED:** {report['summary']['hermes_rewrite_needed']}",
        f"**Rejected:** {report['summary']['hermes_rejected']}",
        f"",
        f"**Active lessons md5:** `{report['summary']['active_lessons_md5'][:12]}...` ({report['summary']['active_lessons_byte_count']}B / {report['summary']['memory_char_limit']}B)",
        f"",
        f"---",
        f"",
    ]

    if kept:
        lines.append("## ✅ KEPT — Recommend User Review for 入库")
        lines.append("")
        for i, entry in enumerate(kept, 1):
            lines.append(f"### {i}. {entry['trigger']}")
            lines.append(f"")
            lines.append(f"**Lesson:** {entry['lesson_text'][:200]}")
            lines.append(f"**Evidence:** {entry['evidence'][:120] or 'N/A'}")
            lines.append(f"**Source:** {entry['source_reflection']}")
            lines.append(f"**Risk:** {entry['risk']}")
            lines.append(f"**Action:** {entry['suggested_user_action']}")
            lines.append("")

    if rewrite:
        lines.append("## ✏️ REWRITE_NEEDED — Needs Improvement Before Review")
        lines.append("")
        for i, entry in enumerate(rewrite, 1):
            lines.append(f"### {i}. {entry['trigger']}")
            lines.append(f"")
            lines.append(f"**Current lesson:** {entry['lesson_text'][:200]}")
            lines.append(f"**Suggestion:** {entry['rewrite_suggestion'] or 'N/A'}")
            lines.append(f"**Source:** {entry['source_reflection']}")
            lines.append(f"**Risk:** {entry['risk']}")
            lines.append("")

    lines.append("---")
    lines.append("")
    lines.append("> ⛔ 这是拟入库预览，不是正式 pending 队列。不自动推送，不自动入库。")
    lines.append("> 用户必须手动确认后才可进入 active_lessons.md。")

    _PENDING_PREVIEW_PATH.parent.mkdir(parents=True, exist_ok=True)
    _PENDING_PREVIEW_PATH.write_text("\n".join(lines), encoding="utf-8")


# ══════════════════════════════════════════════════════════════════════════
# CLI
# ══════════════════════════════════════════════════════════════════════════

def main():
    parser = argparse.ArgumentParser(description="P5Q Hermes self-review quality gate")
    parser.add_argument("--summary-only", action="store_true",
                        help="Print summary only, do not write files")
    args = parser.parse_args()

    report, kept, rewrite = run_gate()

    if args.summary_only:
        s = report["summary"]
        print(f"Total: {s['total_candidates']} | Rule passed: {s['rule_passed']} | "
              f"Kept: {s['hermes_kept']} | Rewrite: {s['hermes_rewrite_needed']} | "
              f"Rejected: {s['hermes_rejected']}")
        return

    write_outputs(report, kept, rewrite)
    s = report["summary"]
    print(f"✅ Quality Gate run complete.")
    print(f"   Total: {s['total_candidates']} | Rule passed: {s['rule_passed']} | "
          f"Kept: {s['hermes_kept']} | Rewrite: {s['hermes_rewrite_needed']} | "
          f"Rejected: {s['hermes_rejected']}")
    print(f"   Report: {_QUALITY_REPORT_PATH}")
    print(f"   Preview: {_PENDING_PREVIEW_PATH}")


if __name__ == "__main__":
    main()

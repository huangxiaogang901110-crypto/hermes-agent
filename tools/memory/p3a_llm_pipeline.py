#!/usr/bin/env python3
"""
p3a_llm_pipeline.py — P3A LLM-based reflect + synthesize (dry-run only).

Reads event_candidates.jsonl → LLM reflects → LLM synthesizes.
Writes preview to models/active_lessons_llm_preview.md.
Does NOT touch active_lessons.md or P2 scripts.

Usage:
  python p3a_llm_pipeline.py                    # dry-run, prints output
  python p3a_llm_pipeline.py --write-preview     # write to models/ dir
"""

from __future__ import annotations

import json
import os
import re
import sys
import urllib.request
from pathlib import Path

# ── paths ──────────────────────────────────────────────────────────────────
_MEMORY_DIR = Path.home() / ".hermes" / "profiles" / "me" / "memory"
_EVENTS_PATH = _MEMORY_DIR / "events" / "event_candidates.jsonl"
_MODELS_DIR = _MEMORY_DIR / "models"
_PREVIEW_PATH = _MODELS_DIR / "active_lessons_llm_preview.md"
_DETERMINISTIC_PATH = _MEMORY_DIR / "active_lessons.md"

# ── LLM config ─────────────────────────────────────────────────────────────
_MODEL = "deepseek-v4-flash"
_BASE_URL = "https://api.deepseek.com/v1"
_MAX_TOKENS = 1024
_TEMPERATURE = 0.3

# ── limits ─────────────────────────────────────────────────────────────────
_MAX_LESSONS = 5
_MAX_TOTAL_BYTES = 2048

# ── sensitive patterns (same as P2) ────────────────────────────────────────
_SENSITIVE_RE = re.compile(
    r"(sk-|LTAI|AKIA|Bearer\s|token=|password=|api_key|secret|accesskey|"
    r"Authorization:|x-api-key|PRIVATE KEY)",
    re.IGNORECASE,
)


def _load_api_key() -> str:
    """Load DEEPSEEK_API_KEY from profile .env, never hardcoded."""
    env_path = Path.home() / ".hermes" / "profiles" / "me" / ".env"
    if env_path.is_file():
        for line in env_path.read_text().split("\n"):
            if line.startswith("DEEPSEEK_API_KEY="):
                return line.split("=", 1)[1].strip().strip('"').strip("'")
    # Fallback: environment variable
    return os.environ.get("DEEPSEEK_API_KEY", "")


def _call_llm(prompt: str, system: str = "") -> str:
    """Call DeepSeek API (deepseek-v4-flash). Returns response text."""
    api_key = _load_api_key()
    if not api_key:
        raise RuntimeError("DEEPSEEK_API_KEY not found in .env or environment")

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


def _is_sensitive(text: str) -> bool:
    return bool(_SENSITIVE_RE.search(text))


def _estimate_tokens(text: str) -> int:
    """Rough token estimate: 1 token ≈ 4 chars for English, ~2 chars for Chinese."""
    return max(1, len(text) // 3)


def _load_events() -> list[dict]:
    if not _EVENTS_PATH.is_file():
        print("[P3A] No event_candidates.jsonl found.")
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


def _check_preview_quality(content: str) -> dict:
    """Validate preview content against P3A quality rules."""
    issues = []
    lines_count = content.count("## ")  # approximate lesson count

    if len(content.encode("utf-8")) > _MAX_TOTAL_BYTES:
        issues.append(f"exceeds {_MAX_TOTAL_BYTES} bytes")
    if _is_sensitive(content):
        issues.append("contains sensitive tokens/keys")

    return {
        "size_bytes": len(content.encode("utf-8")),
        "lesson_count": lines_count,
        "issues": issues,
        "ok": len(issues) == 0,
    }


# ══════════════════════════════════════════════════════════════════════════════
# LLM reflect
# ══════════════════════════════════════════════════════════════════════════════

REFLECT_SYSTEM = (
    "You are a memory reflection engine. Given a conversation event where the user "
    "corrected the assistant, analyze what went wrong and extract a concise, "
    "generalizable lesson.\n\n"
    "Rules:\n"
    "- Write in English.\n"
    "- The lesson must be a general rule, NOT specific to one project or codebase.\n"
    "- No tokens, API keys, passwords, or secrets in output.\n"
    "- Keep each lesson under 250 characters.\n"
    "- Output as JSON: {\"issue\": \"...\", \"lesson\": \"...\", \"confidence\": \"high|medium|low\"}"
)


def _reflect_llm(event: dict) -> dict | None:
    prompt = (
        f"User message: {event.get('user_message_excerpt', '')}\n"
        f"Assistant response: {event.get('assistant_response_excerpt', '')}\n"
        f"Trigger reason: {event.get('trigger_reason', '')}\n"
    )
    try:
        raw = _call_llm(prompt, system=REFLECT_SYSTEM)
        # Try to parse JSON from response
        raw = raw.strip()
        if raw.startswith("```"):
            raw = re.sub(r"^```(?:json)?\s*", "", raw)
            raw = re.sub(r"\s*```$", "", raw)
        result = json.loads(raw)
        if _is_sensitive(result.get("lesson", "")):
            print("  [P3A] BLOCKED: LLM output contains sensitive content")
            return None
        return result
    except Exception as exc:
        print(f"  [P3A] LLM reflect failed: {exc}")
        return None


# ══════════════════════════════════════════════════════════════════════════════
# LLM synthesize
# ══════════════════════════════════════════════════════════════════════════════

SYNTHESIZE_SYSTEM = (
    "You are a lesson synthesizer. Given one or more reflection entries, produce "
    "a compact set of active lessons.\n\n"
    "Rules:\n"
    "- Output at most 5 lessons.\n"
    "- Total output must be under 2 KB (2000 bytes).\n"
    "- Write in English.\n"
    "- Lessons must be general rules, not project-specific.\n"
    "- No tokens, API keys, passwords, secrets, or cookies.\n"
    "- Deduplicate similar lessons.\n"
    "- Sort by importance.\n"
    "- Each lesson must be actionable: 'Do X when Y' or 'Never do Z'.\n"
    "- Format as markdown with one ## heading per lesson.\n"
    "- Each lesson includes a **confidence:** tag (high/medium/low).\n"
    "- Do NOT repeat the same lesson in different words.\n"
    "- Do NOT include one-time test content as a rule.\n"
    "- Add a brief comment block at top explaining this is a P3A LLM preview."
)


def _synthesize_llm(reflections: list[dict]) -> str:
    if not reflections:
        return "# No reflections to synthesize.\n"

    items = []
    for i, r in enumerate(reflections, 1):
        items.append(
            f"### Reflection {i}\n"
            f"Issue: {r.get('issue', '')}\n"
            f"Lesson: {r.get('lesson', '')}\n"
            f"Confidence: {r.get('confidence', '')}\n"
        )

    prompt = "\n".join(items)

    try:
        raw = _call_llm(prompt, system=SYNTHESIZE_SYSTEM)
        return raw.strip()
    except Exception as exc:
        print(f"  [P3A] LLM synthesize failed: {exc}")
        return f"# Synthesis failed: {exc}\n"


# ══════════════════════════════════════════════════════════════════════════════
# main
# ══════════════════════════════════════════════════════════════════════════════

def main():
    import argparse
    parser = argparse.ArgumentParser(description="P3A LLM pipeline (dry-run)")
    parser.add_argument("--write-preview", action="store_true",
                        help="Write preview to models/active_lessons_llm_preview.md")
    args = parser.parse_args()

    events = _load_events()
    if not events:
        print("[P3A] No events to process.")
        return

    print(f"[P3A] Loaded {len(events)} event(s) from event_candidates.jsonl")
    print(f"[P3A] Model: {_MODEL}")

    # ── Phase 1: LLM reflect ───────────────────────────────────────────────
    print("\n── Phase 1: LLM Reflect ──")
    llm_reflections = []
    for i, ev in enumerate(events, 1):
        prompt_preview = (
            f"  Event {i}: reason={ev.get('trigger_reason','?')} "
            f"user={ev.get('user_message_excerpt','')[:40]}..."
        )
        print(prompt_preview)

        # Estimate tokens
        est_input = _estimate_tokens(
            ev.get("user_message_excerpt", "") +
            ev.get("assistant_response_excerpt", "") +
            ev.get("trigger_reason", "") +
            REFLECT_SYSTEM
        )
        print(f"    est input tokens: ~{est_input}")

        result = _reflect_llm(ev)
        if result:
            llm_reflections.append(result)
            lesson_preview = result.get("lesson", "")[:80]
            print(f"    → lesson: {lesson_preview}...")
            print(f"    → confidence: {result.get('confidence', '?')}")

    print(f"\n  Reflect done: {len(llm_reflections)} reflection(s) generated")

    # ── Phase 2: LLM synthesize ────────────────────────────────────────────
    print("\n── Phase 2: LLM Synthesize ──")
    preview_content = _synthesize_llm(llm_reflections)

    # ── Quality check ──────────────────────────────────────────────────────
    quality = _check_preview_quality(preview_content)
    print(f"\n  Preview quality: {quality['size_bytes']} bytes, "
          f"~{quality['lesson_count']} lessons, "
          f"issues={quality['issues'] if quality['issues'] else 'none'}")

    print(f"\n{'─'*60}")
    print("LLM Preview Content:")
    print(f"{'─'*60}")
    print(preview_content)
    print(f"{'─'*60}")

    # ── Write preview ──────────────────────────────────────────────────────
    if args.write_preview:
        _MODELS_DIR.mkdir(parents=True, exist_ok=True)
        header = (
            "<!-- P3A LLM-generated preview — NOT injected into runtime. -->\n"
            "<!-- Model: deepseek-v4-flash. Do not edit manually. -->\n"
            "<!-- Compare with active_lessons.md before deciding P3B apply. -->\n\n"
        )
        _PREVIEW_PATH.write_text(header + preview_content, encoding="utf-8")
        print(f"\n[P3A] Preview written: {_PREVIEW_PATH}")
    else:
        print("\n[P3A] Dry-run complete. Use --write-preview to save.")

    # ── Comparison ─────────────────────────────────────────────────────────
    print(f"\n{'='*60}")
    print("Comparison: Deterministic vs LLM")
    print(f"{'='*60}")
    if _DETERMINISTIC_PATH.is_file():
        det = _DETERMINISTIC_PATH.read_text(encoding="utf-8")
        print(f"Deterministic ({len(det.encode('utf-8'))} bytes):")
        print(det[:500])
        print(f"\nLLM ({quality['size_bytes']} bytes):")
        print(preview_content[:500])


if __name__ == "__main__":
    main()

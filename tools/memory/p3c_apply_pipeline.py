#!/usr/bin/env python3
"""
p3c_apply_pipeline.py — P3C Candidate Reuse Pipeline.

Solves the P3B non-determinism issue:
  1. --save-candidate:  LLM generate → validate → save to JSON.  No file write.
  2. --apply-candidate:  Read JSON → re-validate → apply (NO LLM call).

Usage:
  python p3c_apply_pipeline.py --save-candidate   # dry-run + save candidate
  python p3c_apply_pipeline.py --apply-candidate   # apply from saved candidate
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
import urllib.request
from datetime import datetime, timezone, timedelta
from pathlib import Path

# ── Import P3B validators ──────────────────────────────────────────────────
import importlib.util

_P3B_PATH = Path(__file__).resolve().parent / "p3b_apply_pipeline.py"
_spec_p3b = importlib.util.spec_from_file_location("p3b_core", str(_P3B_PATH))
_p3b = importlib.util.module_from_spec(_spec_p3b)
sys.modules["p3b_core"] = _p3b
_spec_p3b.loader.exec_module(_p3b)

# ── paths ──────────────────────────────────────────────────────────────────
_MEMORY_DIR = Path.home() / ".hermes" / "profiles" / "me" / "memory"
_EVENTS_PATH = _MEMORY_DIR / "events" / "event_candidates.jsonl"
_ACTIVE_LESSONS = _MEMORY_DIR / "active_lessons.md"
_MODELS_DIR = _MEMORY_DIR / "models"
_CANDIDATE_PATH = _MODELS_DIR / "active_lessons_candidate.json"

# ── LLM config (same as P3B) ───────────────────────────────────────────────
_MODEL = "deepseek-v4-flash"
_CANDIDATE_MAX_AGE_HOURS = 24


# ══════════════════════════════════════════════════════════════════════════════
# Candidate file IO
# ══════════════════════════════════════════════════════════════════════════════

def save_candidate(content: str, result: dict) -> Path:
    """Save candidate JSON. Returns path."""
    candidate = {
        "content": content,
        "sha256": hashlib.sha256(content.encode("utf-8")).hexdigest(),
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "model": _MODEL,
        "validator_result": result,
        "source_event_count": len(_p3b._load_events()),
        "size_bytes": len(content.encode("utf-8")),
    }
    _MODELS_DIR.mkdir(parents=True, exist_ok=True)
    _CANDIDATE_PATH.write_text(
        json.dumps(candidate, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return _CANDIDATE_PATH


def load_candidate() -> dict | None:
    """Load candidate JSON. Returns None if missing or corrupt."""
    if not _CANDIDATE_PATH.is_file():
        return None
    try:
        data = json.loads(_CANDIDATE_PATH.read_text(encoding="utf-8"))
        required = ["content", "sha256", "generated_at", "model", "validator_result"]
        for key in required:
            if key not in data:
                print(f"[P3C] Candidate missing field: {key}")
                return None
        return data
    except (json.JSONDecodeError, Exception) as exc:
        print(f"[P3C] Candidate load failed: {exc}")
        return None


# ══════════════════════════════════════════════════════════════════════════════
# Candidate validation (no LLM)
# ══════════════════════════════════════════════════════════════════════════════

def validate_candidate(candidate: dict) -> tuple[bool, list[str]]:
    """Validate a loaded candidate. Returns (ok, reasons)."""
    reasons = []

    # 1. sha256 check
    content = candidate["content"]
    expected_sha = candidate["sha256"]
    actual_sha = hashlib.sha256(content.encode("utf-8")).hexdigest()
    if expected_sha != actual_sha:
        reasons.append(f"sha256 mismatch: expected {expected_sha[:16]}..., got {actual_sha[:16]}...")
        return False, reasons

    # 2. age check
    try:
        generated_at = datetime.fromisoformat(candidate["generated_at"])
        age = datetime.now(timezone.utc) - generated_at
        if age > timedelta(hours=_CANDIDATE_MAX_AGE_HOURS):
            reasons.append(
                f"Candidate expired: {age.total_seconds() / 3600:.1f}h old "
                f"(max {_CANDIDATE_MAX_AGE_HOURS}h)"
            )
            return False, reasons
    except (ValueError, TypeError):
        reasons.append("Invalid generated_at timestamp")
        return False, reasons

    # 3. re-validate with P3B validator
    errors, warnings = _p3b.validate(content)
    if not _p3b.validator_passed(errors):
        for e in errors:
            reasons.append(f"Re-validate failed: {e}")
        return False, reasons

    # 4. size check
    size = len(content.encode("utf-8"))
    if size > _p3b._MAX_TOTAL_BYTES:
        reasons.append(f"Size {size} exceeds {_p3b._MAX_TOTAL_BYTES}")
        return False, reasons

    return True, []


# ══════════════════════════════════════════════════════════════════════════════
# Main pipeline
# ══════════════════════════════════════════════════════════════════════════════

def main():
    import argparse
    parser = argparse.ArgumentParser(description="P3C Candidate Reuse Pipeline")
    parser.add_argument("--save-candidate", action="store_true",
                        help="LLM generate → validate → save candidate JSON")
    parser.add_argument("--apply-candidate", action="store_true",
                        help="Load candidate JSON → re-validate → apply (NO LLM)")
    parser.add_argument("--target-path", type=str, default=None,
                        help="Override active_lessons.md path (for testing)")
    args = parser.parse_args()

    active_path = Path(args.target_path) if args.target_path else _ACTIVE_LESSONS

    if args.save_candidate:
        # ── LLM generate + save candidate ──────────────────────────────────
        events = _p3b._load_events()
        if not events:
            print("[P3C] No events to process.")
            return

        print(f"[P3C] Model: {_MODEL} | Events: {len(events)}")
        print("[P3C] Phase 1: LLM Generate → Validate → Save Candidate")

        raw = _p3b._generate_lessons(events)
        errors, warnings = _p3b.validate(raw)

        if warnings:
            print(f"  Warnings ({len(warnings)}):")
            for w in warnings:
                print(f"    [{w.rule}] {w.detail}")

        if not _p3b.validator_passed(errors):
            print(f"\n  ❌ Validator FAILED — candidate NOT saved ({len(errors)} errors)")
            for e in errors:
                print(f"    {e}")
            return

        result = {
            "errors": len(errors),
            "warnings": len(warnings),
            "passed": True,
        }
        path = save_candidate(raw, result)
        size = len(raw.encode("utf-8"))
        print(f"\n  ✅ Candidate saved: {path}")
        print(f"  sha256: {hashlib.sha256(raw.encode('utf-8')).hexdigest()[:32]}...")
        print(f"  size: {size} bytes | model: {_MODEL} | events: {len(events)}")
        print(f"\n  Content preview:")
        print(raw[:400] + ("..." if len(raw) > 400 else ""))
        print("\n[P3C] Candidate saved. Use --apply-candidate to apply (no LLM).")

    elif args.apply_candidate:
        # ── Apply from candidate (NO LLM) ──────────────────────────────────
        print("[P3C] Loading candidate — NO LLM call")
        candidate = load_candidate()
        if candidate is None:
            print("[P3C] No candidate found. Run --save-candidate first.")
            return

        print(f"  Candidate: model={candidate['model']}, "
              f"generated={candidate['generated_at'][:19]}, "
              f"events={candidate.get('source_event_count', '?')}")

        ok, reasons = validate_candidate(candidate)
        if not ok:
            print(f"\n  ❌ Candidate REJECTED ({len(reasons)} reason(s)):")
            for r in reasons:
                print(f"    - {r}")
            return

        print(f"  ✅ Candidate valid — applying...")

        # Backup
        backup = _p3b._backup_active_lessons() if active_path.is_file() else None
        if backup:
            print(f"  📦 Backup: {backup}")

        # Atomic write
        header = (
            "<!-- Auto-generated by p3c_apply_pipeline.py. Do not edit manually. -->\n"
            "<!-- P3C Candidate Reuse — applied from saved candidate (no LLM re-call). -->\n"
            f"<!-- Generated: {candidate['generated_at']} | Model: {candidate['model']} -->\n\n"
        )
        try:
            _p3b._atomic_write(active_path, header + candidate["content"] + "\n")
            print(f"  ✅ Written to {active_path}")
        except Exception as exc:
            print(f"  ❌ Write failed: {exc}")

    else:
        parser.print_help()


if __name__ == "__main__":
    main()

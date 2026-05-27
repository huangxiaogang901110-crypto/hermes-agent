#!/usr/bin/env python3
"""
log_event_scanner.py — P4A: scan agent/gateway logs for error patterns,
generate event_candidates without sending full logs to LLM.

Usage:
  python log_event_scanner.py --log-file ~/.hermes/profiles/me/logs/agent.log --dry-run
  python log_event_scanner.py --log-file ~/.hermes/profiles/me/logs/agent.log --apply
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

# ── paths ──────────────────────────────────────────────────────────────────
_SCRIPTS_DIR = Path(__file__).resolve().parent
_DEFAULT_EVENTS_PATH = (
    Path.home() / ".hermes" / "profiles" / "me" / "memory" / "events" / "event_candidates.jsonl"
)

# ── limits ─────────────────────────────────────────────────────────────────
_DEFAULT_MAX_EVENTS = 3
_DEFAULT_MAX_SNIPPET_BYTES = 4096
_CONTEXT_LINES = 30  # lines around match

# ── keywords ───────────────────────────────────────────────────────────────
_SCAN_KEYWORDS = [
    "FAIL",
    "PARTIAL",
    "blocked",
    "timeout",
    "exception",
    "traceback",
    "error",
    "rollback",
    "forbidden",
    "permission denied",
    "rate limit",
    "cost exceeded",
    "model call failed",
    "tool call failed",
    "sandbox",
    "production",
    "dirty worktree",
    "push master",
    "secret leaked",
]

# ── sensitive patterns ─────────────────────────────────────────────────────
_SENSITIVE_PATTERNS = [
    r"sk-[\w-]{20,}",
    r"LTAI[\w]{16,}",
    r"AKIA[\w]{16,}",
    r"Bearer\s+[\w\-\.\+/=]{20,}",
    r"token\s*[=:]\s*\S{8,}",
    r"password\s*[=:]\s*\S+",
    r"cookie\s*[=:]\s*\S+",
    r"api_key\s*[=:]\s*\S+",
    r"secret\s*[=:]\s*\S+",
    r"accesskey\s*[=:]\s*\S+",
    r"Authorization\s*[=:]\s*\S+",
    r"x-api-key\s*[=:]\s*\S+",
    r"BEGIN\s+(RSA|EC|DSA|OPENSSH)?\s*PRIVATE\s+KEY",
    r"PRIVATE\s+KEY",
]


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _is_sensitive(text: str) -> bool:
    """Return True if text contains any secret-ish pattern."""
    for pat in _SENSITIVE_PATTERNS:
        if re.search(pat, text, re.IGNORECASE):
            return True
    return False


def _load_existing_hashes(events_path: Path) -> set:
    """Load existing excerpt_sha256 values from event_candidates.jsonl."""
    hashes: set = set()
    if not events_path.exists():
        return hashes
    try:
        for line in events_path.read_text(encoding="utf-8").strip().splitlines():
            if not line.strip():
                continue
            obj = json.loads(line)
            if "excerpt_sha256" in obj:
                hashes.add(obj["excerpt_sha256"])
    except (json.JSONDecodeError, OSError):
        pass
    return hashes


def _load_existing_signatures(events_path: Path) -> set:
    """Load source_file + line range + keyword signatures to dedup by origin."""
    sigs: set = set()
    if not events_path.exists():
        return sigs
    try:
        for line in events_path.read_text(encoding="utf-8").strip().splitlines():
            if not line.strip():
                continue
            obj = json.loads(line)
            sf = obj.get("source_file", "")
            ls = obj.get("line_start", -1)
            le = obj.get("line_end", -1)
            kw = obj.get("trigger_keyword", "")
            sigs.add((sf, ls, le, kw))
    except (json.JSONDecodeError, OSError):
        pass
    return sigs


def _assign_severity(keyword: str) -> str:
    kw = keyword.lower()
    if any(w in kw for w in ("secret leaked", "forbidden", "permission denied", "production", "push master")):
        return "critical"
    if any(w in kw for w in ("exception", "traceback", "fail", "blocked", "timeout", "rollback")):
        return "high"
    if any(w in kw for w in ("error", "rate limit", "cost exceeded", "dirty worktree")):
        return "medium"
    return "low"


def _assign_category(keyword: str) -> str:
    kw = keyword.lower()
    if any(w in kw for w in ("exception", "traceback", "fail", "timeout")):
        return "runtime_error"
    if any(w in kw for w in ("secret", "token", "password", "key", "forbidden", "permission")):
        return "security"
    if any(w in kw for w in ("rate limit", "cost exceeded", "model call", "tool call")):
        return "api_limit"
    if any(w in kw for w in ("production", "push master", "dirty worktree", "sandbox")):
        return "process_violation"
    return "other"


def _truncate_excerpt(excerpt: str, max_bytes: int) -> str:
    """Truncate excerpt to max_bytes in utf-8 byte length, accounting for suffix."""
    suffix = "\n…[truncated]"
    suffix_bytes = len(suffix.encode("utf-8"))
    encoded = excerpt.encode("utf-8")
    if len(encoded) <= max_bytes:
        return excerpt
    # trim, leaving room for suffix
    limit = max_bytes - suffix_bytes
    if limit <= 0:
        return suffix
    trimmed = encoded[:limit]
    return trimmed.decode("utf-8", errors="ignore").rstrip() + suffix


def _dedup_candidates(candidates: list[dict], existing_sigs: set, existing_hashes: set) -> list[dict]:
    """Filter out candidates already in existing events."""
    result = []
    seen_sigs: set = set()
    seen_hashes: set = set(existing_hashes)  # not modifying arg
    for c in candidates:
        sig = (c.get("source_file", ""), c.get("line_start", -1), c.get("line_end", -1), c.get("trigger_keyword", ""))
        h = c.get("excerpt_sha256", "")
        if sig in existing_sigs or sig in seen_sigs:
            continue
        if h in seen_hashes:
            continue
        seen_sigs.add(sig)
        seen_hashes.add(h)
        result.append(c)
    return result


def _merge_similar(candidates: list[dict]) -> list[dict]:
    """
    Merge candidates with same trigger_keyword within 30-min (by file + keyword proximity).
    Keeps the first, drops subsequent same-keyword candidates.
    """
    if len(candidates) <= 1:
        return candidates
    result = [candidates[0]]
    seen_keywords: set = {candidates[0]["trigger_keyword"]}
    for c in candidates[1:]:
        if c["trigger_keyword"] in seen_keywords:
            continue
        seen_keywords.add(c["trigger_keyword"])
        result.append(c)
    return result


def scan_log(
    log_file: Path,
    since_minutes: Optional[int] = None,
    max_events: int = _DEFAULT_MAX_EVENTS,
    max_snippet_bytes: int = _DEFAULT_MAX_SNIPPET_BYTES,
) -> list[dict]:
    """
    Scan a log file for error patterns and return event candidates.
    """
    if not log_file.exists():
        print(f"[scanner] Log file not found: {log_file}", file=sys.stderr)
        return []

    lines = log_file.read_text(encoding="utf-8", errors="replace").splitlines()
    total_lines = len(lines)
    if total_lines == 0:
        return []

    # Build keyword regex (case-insensitive)
    keyword_pattern = "|".join(re.escape(kw) for kw in _SCAN_KEYWORDS)
    keyword_re = re.compile(keyword_pattern, re.IGNORECASE)

    # Time-based filtering: parse ISO timestamps if --since-minutes is set
    if since_minutes is not None:
        cutoff = datetime.now(timezone.utc).timestamp() - (since_minutes * 60)
        # Common timestamp patterns in Hermes logs
        ts_patterns = [
            re.compile(r"^(\d{4}-\d{2}-\d{2}[\sT]\d{2}:\d{2}:\d{2})"),  # ISO standard
        ]
        eligible_from = 0
        for i, line in enumerate(lines):
            for pat in ts_patterns:
                m = pat.match(line)
                if m:
                    try:
                        ts_str = m.group(1).replace(" ", "T")
                        ts = datetime.fromisoformat(ts_str).replace(tzinfo=timezone.utc).timestamp()
                        if ts >= cutoff:
                            eligible_from = i
                            break
                    except ValueError:
                        continue
            if eligible_from > 0:
                break
        # If no timestamp found in first 200 lines, scan all
        if eligible_from == 0:
            pass  # scan from beginning
        lines = lines[eligible_from:]
        print(f"[scanner] Time filter: scanning from line {eligible_from+1} (last {since_minutes} min)")

    candidates: list[dict] = []
    matched_line_indices: set = set()

    for keyword in _SCAN_KEYWORDS:
        if len(candidates) >= max_events:
            break
        kw_re = re.compile(re.escape(keyword), re.IGNORECASE)
        for i, line in enumerate(lines):
            if i in matched_line_indices:
                continue
            if len(candidates) >= max_events:
                break
            if not kw_re.search(line):
                continue

            # Determine context window
            line_start = max(0, i - _CONTEXT_LINES)
            line_end = min(total_lines, i + _CONTEXT_LINES + 1)
            excerpt_lines = lines[line_start:line_end]
            excerpt = "\n".join(excerpt_lines)

            # Sensitive check
            blocked = _is_sensitive(excerpt)
            if blocked:
                excerpt_clean = "[BLOCKED: sensitive content detected]"
            else:
                excerpt_clean = _truncate_excerpt(excerpt, max_snippet_bytes)

            candidate = {
                "created_at": datetime.now(timezone.utc).isoformat(),
                "source_type": "log_event",
                "source_file": str(log_file),
                "severity": _assign_severity(keyword),
                "category": _assign_category(keyword),
                "trigger_keyword": keyword,
                "excerpt": excerpt_clean,
                "excerpt_sha256": _sha256(excerpt_clean),
                "blocked_sensitive": blocked,
                "line_start": line_start + 1,  # 1-indexed
                "line_end": line_end,  # 1-indexed
            }
            candidates.append(candidate)
            matched_line_indices.add(i)

    # Merge similar (same keyword) and keep only up to max_events
    candidates = _merge_similar(candidates)
    return candidates[:max_events]


def main():
    parser = argparse.ArgumentParser(
        description="P4A: scan Hermes logs for error patterns → event_candidates"
    )
    parser.add_argument(
        "--log-file", type=Path, required=True,
        help="Path to log file (e.g. ~/.hermes/profiles/me/logs/agent.log)"
    )
    parser.add_argument(
        "--since-minutes", type=int, default=None,
        help="Only scan last N minutes of log (by timestamp)"
    )
    parser.add_argument(
        "--output", type=Path, default=None,
        help="Output path for event_candidates.jsonl (default: runtime events dir)"
    )
    parser.add_argument(
        "--max-events", type=int, default=_DEFAULT_MAX_EVENTS,
        help=f"Max candidates to generate (default: {_DEFAULT_MAX_EVENTS})"
    )
    parser.add_argument(
        "--max-snippet-bytes", type=int, default=_DEFAULT_MAX_SNIPPET_BYTES,
        help=f"Max excerpt bytes per event (default: {_DEFAULT_MAX_SNIPPET_BYTES})"
    )
    parser.add_argument(
        "--apply", action="store_true",
        help="Write to event_candidates.jsonl (otherwise dry-run)"
    )
    parser.add_argument(
        "--dry-run", action="store_true", default=False,
        help="Print summary only, do not write (default behavior)"
    )
    args = parser.parse_args()

    log_file = args.log_file.expanduser().resolve()
    output_path = (args.output or _DEFAULT_EVENTS_PATH).expanduser().resolve()

    # ── scan ───────────────────────────────────────────────────────────────
    print(f"[scanner] Scanning: {log_file}")
    print(f"[scanner] Keywords: {len(_SCAN_KEYWORDS)}")
    print(f"[scanner] Max events: {args.max_events}, Max snippet: {args.max_snippet_bytes} bytes")
    if args.since_minutes:
        print(f"[scanner] Time window: last {args.since_minutes} minutes")

    candidates = scan_log(
        log_file=log_file,
        since_minutes=args.since_minutes,
        max_events=args.max_events,
        max_snippet_bytes=args.max_snippet_bytes,
    )

    print(f"\n[scanner] Found {len(candidates)} candidate(s):")
    for i, c in enumerate(candidates):
        print(f"  [{i+1}] {c['severity']}/{c['category']} | {c['trigger_keyword']} "
              f"| lines {c['line_start']}-{c['line_end']} "
              f"| sensitive={c['blocked_sensitive']} "
              f"| sha256={c['excerpt_sha256'][:12]}")

    # ── dry-run or apply ───────────────────────────────────────────────────
    if args.apply:
        if not candidates:
            print("\n[scanner] No candidates to write.")
            return 0

        existing_hashes = _load_existing_hashes(output_path)
        existing_sigs = _load_existing_signatures(output_path)
        candidates = _dedup_candidates(candidates, existing_sigs, existing_hashes)

        if not candidates:
            print("\n[scanner] All candidates already exist — nothing to write.")
            return 0

        output_path.parent.mkdir(parents=True, exist_ok=True)
        with open(output_path, "a", encoding="utf-8") as f:
            for c in candidates:
                f.write(json.dumps(c, ensure_ascii=False) + "\n")
        print(f"\n[scanner] Wrote {len(candidates)} new event(s) to {output_path}")

        # ── P5A: trigger shadow pipeline after new events ─────────────────
        _trigger_shadow_pipeline_if_new(candidates)
    else:
        print(f"\n[scanner] DRY-RUN — no files written. Use --apply to write.")
        print(f"[scanner] Would write to: {output_path}")

    return 0


def _trigger_shadow_pipeline_if_new(candidates: list) -> None:
    """Trigger memory_pipeline shadow run if we found new non-sensitive candidates."""
    new_count = sum(1 for c in candidates if not c.get("blocked_sensitive"))
    if new_count == 0:
        return
    try:
        pipeline = _SCRIPTS_DIR / "memory_pipeline.py"
        if not pipeline.is_file():
            return
        subprocess.run(
            [sys.executable, str(pipeline), "--reason", "log_scanner"],
            capture_output=True, timeout=60,
        )
        print(f"[scanner] Shadow pipeline triggered for {new_count} new event(s)")
    except Exception:
        pass  # non-fatal


if __name__ == "__main__":
    sys.exit(main())

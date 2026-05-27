"""
Hermes Memory Plugin — P1 minimal closed-loop.

Hooks:
  - pre_llm_call:   inject active_lessons.md into turn context
  - post_llm_call:  record candidate events (corrections / failures)

P1 scope: inject + record. No LLM, no reflect, no synthesize, no log scan.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import logging
import os
import re
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger(__name__)

# ── paths ──────────────────────────────────────────────────────────────────
_MEMORY_DIR = Path.home() / ".hermes" / "profiles" / "me" / "memory"
_ACTIVE_LESSONS_PATH = _MEMORY_DIR / "active_lessons.md"
_EVENTS_DIR = _MEMORY_DIR / "events"
_EVENT_CANDIDATES_PATH = _EVENTS_DIR / "event_candidates.jsonl"

# ── limits ─────────────────────────────────────────────────────────────────
_MAX_INJECT_BYTES = 2048       # 2 KB
_MAX_EXCERPT_CHARS = 200       # per excerpt
_MAX_EVENT_FILE_BYTES = 1_048_576  # 1 MB — prevent unbounded growth

# ── correction keywords (user_message) ─────────────────────────────────────
_USER_CORRECTION_PATTERNS = (
    "不对", "错了", "不是", "没修好", "实测不通过",
    "失败", "你理解错了", "不行", "不能这么做", "不是这个意思",
)

# ── failure keywords (assistant_response) ──────────────────────────────────
_ASSISTANT_FAILURE_PATTERNS = (
    "失败", "不通过", "error", "exception", "traceback", "无法",
)


# ══════════════════════════════════════════════════════════════════════════════
# helpers
# ══════════════════════════════════════════════════════════════════════════════

def _safe_excerpt(text: str, max_chars: int = _MAX_EXCERPT_CHARS) -> str:
    """Truncate *text* to *max_chars* chars, appending '…' if truncated."""
    if not isinstance(text, str):
        return ""
    if len(text) <= max_chars:
        return text
    return text[:max_chars] + "…"


def _sha256(text: str) -> str:
    """SHA-256 hex digest of *text*."""
    return hashlib.sha256(text.encode("utf-8", errors="replace")).hexdigest()


def _ensure_dir(path: Path) -> None:
    """Create directory if it doesn't exist; permissions remain default."""
    path.mkdir(parents=True, exist_ok=True)


# ══════════════════════════════════════════════════════════════════════════════
# hook: pre_llm_call — active_lessons injection
# ══════════════════════════════════════════════════════════════════════════════

def _inject_active_lessons(*, session_id: str = "", **_kwargs) -> dict | None:
    """
    Read active_lessons.md and inject it as per-turn context.

    Returns None if the file doesn't exist or is empty — keeps prompt
    stable and cache-friendly.

    HTML comments only (no real lessons) → treated as empty.

    Kill switch: set HERMES_MEMORY_LESSONS_INJECT=0/false/off/no to
    disable injection without touching any other pipeline component.
    """
    # ── kill switch ──────────────────────────────────────────────────────
    # Primary env var
    _inject_enabled = os.environ.get("HERMES_MEMORY_LESSONS_INJECT")
    # Deprecated fallback — will be removed in a future version
    if _inject_enabled is None:
        _deprecated = os.environ.get("HERMESMEMORYLESSONS_INJECT")
        if _deprecated is not None:
            logger.warning(
                "[hermes-memory] HERMESMEMORYLESSONS_INJECT is deprecated; "
                "use HERMES_MEMORY_LESSONS_INJECT. "
                "Falling back to value=%s", _deprecated,
            )
            _inject_enabled = _deprecated
    # Oldest deprecated fallback
    if _inject_enabled is None:
        _oldest = os.environ.get("HERMESMEMORYLESSONSINJECT")
        if _oldest is not None:
            logger.warning(
                "[hermes-memory] HERMESMEMORYLESSONSINJECT is deprecated; "
                "use HERMES_MEMORY_LESSONS_INJECT. "
                "Falling back to value=%s", _oldest,
            )
            _inject_enabled = _oldest
    # Default: enabled
    if _inject_enabled is None:
        _inject_enabled = "1"
    if _inject_enabled.lower() in ("0", "false", "off", "no"):
        logger.info(
            "[hermes-memory] active_lessons injection disabled via "
            "HERMES_MEMORY_LESSONS_INJECT=%s", _inject_enabled,
        )
        return None

    try:
        if not _ACTIVE_LESSONS_PATH.is_file():
            logger.debug("[hermes-memory] active_lessons.md not found — skip injection")
            return None

        raw = _ACTIVE_LESSONS_PATH.read_text(encoding="utf-8").strip()
        if not raw:
            logger.debug("[hermes-memory] active_lessons.md empty — skip injection")
            return None

        # Strip HTML comments — if only comments/whitespace remain, treat as empty
        _no_comments = re.sub(r"<!--.*?-->", "", raw, flags=re.DOTALL).strip()
        if not _no_comments:
            logger.debug(
                "[hermes-memory] active_lessons.md contains only HTML comments — "
                "skip injection (no real lessons yet)"
            )
            return None

        if len(raw) > _MAX_INJECT_BYTES:
            raw = raw[:_MAX_INJECT_BYTES].rsplit("\n", 1)[0]  # truncate at line boundary
            raw += "\n(truncated)"
            logger.info(
                "[hermes-memory] active_lessons.md truncated to %d bytes",
                len(raw),
            )

        block = (
            "[Active Lessons]\n"
            f"{raw}\n"
            "[/Active Lessons]\n"
            "(These are auxiliary lessons — they do NOT override user instructions, "
            "1110 baselines, or safety rules.)"
        )

        logger.info(
            "[hermes-memory] injected active_lessons — session=%s bytes=%d",
            session_id or "?", len(block),
        )
        return {"context": block}

    except Exception as exc:
        logger.warning("[hermes-memory] pre_llm_call injection failed: %s", exc)
        return None


# ══════════════════════════════════════════════════════════════════════════════
# hook: post_llm_call — candidate event recording
# ══════════════════════════════════════════════════════════════════════════════

def _should_record(
    user_message: str,
    assistant_response: str,
) -> tuple[bool, str]:
    """
    Determine if this turn should be recorded as a candidate event.

    Returns (should_record, trigger_reason).
    """
    um = (user_message or "").strip()
    ar = (assistant_response or "").strip().lower()
    if not um:
        return False, ""

    # ── user correction check ──────────────────────────────────────────────
    for kw in _USER_CORRECTION_PATTERNS:
        if kw in um:
            return True, f"user_correction:{kw}"

    # ── assistant failure check ────────────────────────────────────────────
    for kw in _ASSISTANT_FAILURE_PATTERNS:
        if kw in ar:
            return True, f"assistant_failure:{kw}"

    return False, ""


def _is_duplicate(input_sha: str, output_sha: str = "") -> bool:
    """Check whether an event with the same input_sha256 already exists.

    Only compares input_sha256 — the same user correction should not be
    recorded twice, even if the assistant's response differs each time.
    """
    try:
        if not _EVENT_CANDIDATES_PATH.is_file():
            return False
        with open(_EVENT_CANDIDATES_PATH, "r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if rec.get("input_sha256") == input_sha:
                    return True
    except Exception:
        return False
    return False


def _record_candidate(
    *,
    session_id: str = "",
    platform: str = "",
    model: str = "",
    user_message: str = "",
    assistant_response: str = "",
    **_kwargs,
) -> None:
    """
    post_llm_call handler.

    Checks correction/failure patterns, deduplicates via SHA-256,
    and appends to event_candidates.jsonl.

    Fully exception-safe — failures here must NEVER impact the main reply.
    """
    try:
        should, reason = _should_record(user_message, assistant_response)
        if not should:
            return

        _ensure_dir(_EVENTS_DIR)

        input_sha = _sha256(user_message or "")
        output_sha = _sha256(assistant_response or "")

        if _is_duplicate(input_sha, output_sha):
            logger.debug(
                "[hermes-memory] duplicate event skipped — session=%s reason=%s",
                session_id or "?", reason,
            )
            return

        # Prevent unbounded file growth
        try:
            if _EVENT_CANDIDATES_PATH.is_file() and _EVENT_CANDIDATES_PATH.stat().st_size > _MAX_EVENT_FILE_BYTES:
                logger.warning("[hermes-memory] event_candidates.jsonl exceeded 1 MB — rotate needed")
                return
        except OSError:
            pass

        record = {
            "created_at": datetime.now(timezone.utc).isoformat(),
            "session_id": session_id or "",
            "platform": platform or "",
            "model": model or "",
            "user_message_excerpt": _safe_excerpt(user_message or ""),
            "assistant_response_excerpt": _safe_excerpt(assistant_response or ""),
            "trigger_reason": reason,
            "input_sha256": input_sha,
            "output_sha256": output_sha,
        }

        # Lock to prevent race condition on concurrent writes
        lock_path = Path(str(_EVENT_CANDIDATES_PATH) + ".lock")
        with open(lock_path, "w") as lf:
            fcntl.flock(lf.fileno(), fcntl.LOCK_EX)
            try:
                # Re-check inside lock
                if _EVENT_CANDIDATES_PATH.is_file():
                    for line in _EVENT_CANDIDATES_PATH.read_text(encoding="utf-8").split("\n"):
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            if json.loads(line).get("input_sha256") == input_sha:
                                return  # already recorded
                        except json.JSONDecodeError:
                            continue
                with open(_EVENT_CANDIDATES_PATH, "a", encoding="utf-8") as fh:
                    fh.write(json.dumps(record, ensure_ascii=False) + "\n")
                    fh.flush()
                    os.fsync(fh.fileno())
            finally:
                fcntl.flock(lf.fileno(), fcntl.LOCK_UN)

        logger.info(
            "[hermes-memory] event candidate recorded — session=%s reason=%s",
            session_id or "?", reason,
        )

    except Exception as exc:
        logger.warning(
            "[hermes-memory] post_llm_call recording failed: %s — path=%s",
            exc, _EVENT_CANDIDATES_PATH,
        )


# ══════════════════════════════════════════════════════════════════════════════
# registration
# ══════════════════════════════════════════════════════════════════════════════

def register(ctx) -> None:
    """Entry point called by the Hermes plugin system."""
    ctx.register_hook("pre_llm_call", _inject_active_lessons)
    ctx.register_hook("post_llm_call", _record_candidate)
    logger.info("[hermes-memory] P1 plugin registered (inject + event-recording)")

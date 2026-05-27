"""
Hermes Skill Catalog Plugin — Plan A 最小闭环。

Hooks:
  - pre_llm_call: read skills-catalog.yaml, scan user_message keywords,
                  inject matched SKILL.md context.

Constraints:
  - Max 2 skills injected per round.
  - Usage logged to skill_usage.jsonl (only for actually injected skills).
  - Sticky skills protected (not for archiving — recorded for future use).
  - Exception-safe: failures must NEVER impact the main reply.
"""

from __future__ import annotations

import json
import logging
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml

logger = logging.getLogger(__name__)

# ── paths ──────────────────────────────────────────────────────────────────
_PROFILE_DIR = Path.home() / ".hermes" / "profiles" / "me"
_SKILLS_DIR = _PROFILE_DIR / "skills"
_CATALOG_PATH = _PROFILE_DIR / "skills-catalog.yaml"
_USAGE_LOG_PATH = _PROFILE_DIR / "logs" / "skill_usage.jsonl"

# ── limits ─────────────────────────────────────────────────────────────────
_MAX_INJECT_SKILLS = 2          # hard cap per round
_MAX_SKILL_CONTENT_BYTES = 8192 # max SKILL.md content per skill
_MAX_TOTAL_INJECT_BYTES = 12288 # max total injected context per round


# ══════════════════════════════════════════════════════════════════════════════
# helpers
# ══════════════════════════════════════════════════════════════════════════════

def _load_catalog() -> dict | None:
    """Load skills-catalog.yaml. Returns None on any failure."""
    try:
        if not _CATALOG_PATH.is_file():
            logger.warning("[skill-catalog] catalog not found at %s", _CATALOG_PATH)
            return None
        raw = _CATALOG_PATH.read_text(encoding="utf-8")
        return yaml.safe_load(raw) or {}
    except Exception as exc:
        logger.warning("[skill-catalog] failed to load catalog: %s", exc)
        return None


def _score_skill(skill_cfg: dict, user_message: str) -> int:
    """Count keyword hits in user_message. Case-insensitive."""
    keywords = skill_cfg.get("keywords", [])
    if not keywords:
        return 0
    um_lower = user_message.lower()
    score = 0
    for kw in keywords:
        if kw.lower() in um_lower:
            score += 1
    return score


def _read_skill_content(rel_path: str) -> str | None:
    """Read SKILL.md content. Returns None on failure."""
    try:
        full = _SKILLS_DIR / rel_path
        if not full.is_file():
            return None
        raw = full.read_text(encoding="utf-8")
        if len(raw) > _MAX_SKILL_CONTENT_BYTES:
            raw = raw[:_MAX_SKILL_CONTENT_BYTES] + "\n(content truncated)"
        return raw.strip()
    except Exception as exc:
        logger.debug("[skill-catalog] read failed for %s: %s", rel_path, exc)
        return None


def _load_matched_skills(matches: list[tuple[str, dict, int]]) -> list[dict]:
    """Load SKILL.md for matched skills (with fallback_paths)."""
    loaded = []
    for name, cfg, score in matches:
        contents = []

        # Primary path
        primary = _read_skill_content(cfg["path"])
        if primary:
            contents.append(primary)

        # Fallback paths (e.g. debug-anti-patterns alongside systematic-debugging)
        for fb in cfg.get("fallback_paths", []):
            fb_content = _read_skill_content(fb)
            if fb_content:
                contents.append(fb_content)

        if contents:
            loaded.append({
                "name": name,
                "score": score,
                "content": "\n\n---\n\n".join(contents),
                "paths": [cfg["path"]] + cfg.get("fallback_paths", []),
            })
    return loaded


def _format_inject_block(skills: list[dict]) -> str:
    """Format injected skill context with clear delimiters."""
    parts = ["[Skill Catalog Injected]"]
    for s in skills:
        parts.append(f"--- skill: {s['name']} (score={s['score']}) ---")
        parts.append(s["content"])
    parts.append("[/Skill Catalog Injected]")
    return "\n".join(parts)


def _log_usage(skills: list[dict], session_id: str, platform: str) -> None:
    """Record usage to skill_usage.jsonl — only for actually injected skills."""
    try:
        _USAGE_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        now = datetime.now(timezone.utc).isoformat()
        for s in skills:
            record = {
                "ts": now,
                "skill": s["name"],
                "score": s["score"],
                "paths": s["paths"],
                "session_id": session_id or "",
                "platform": platform or "",
            }
            with open(_USAGE_LOG_PATH, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(record, ensure_ascii=False) + "\n")
                fh.flush()
                os.fsync(fh.fileno())
        logger.info(
            "[skill-catalog] usage recorded — %d skill(s) session=%s",
            len(skills), session_id or "?",
        )
    except Exception as exc:
        logger.warning("[skill-catalog] usage log failed: %s", exc)


# ══════════════════════════════════════════════════════════════════════════════
# hook: pre_llm_call — skill catalog injection
# ══════════════════════════════════════════════════════════════════════════════

def _inject_skill_catalog(
    *,
    user_message: str = "",
    session_id: str = "",
    platform: str = "",
    **_kwargs: Any,
) -> dict | None:
    """
    Scan user_message against skills-catalog.yaml keywords.
    Inject matched SKILL.md content into the turn context.

    Returns None when no match or on error — keeps prompt stable.
    """
    try:
        catalog = _load_catalog()
        if not catalog:
            return None

        settings = catalog.get("settings", {})
        max_inject = settings.get("max_inject_per_round", _MAX_INJECT_SKILLS)
        skills = catalog.get("catalog", {})
        if not skills:
            return None

        # Score every skill against user_message
        scored: list[tuple[str, dict, int]] = []
        for name, cfg in skills.items():
            score = _score_skill(cfg, user_message)
            if score > 0:
                scored.append((name, cfg, score))

        if not scored:
            logger.debug("[skill-catalog] no keyword match for this turn")
            return None

        # Sort by score descending, take top N
        scored.sort(key=lambda x: x[2], reverse=True)
        top = scored[:min(max_inject, len(scored))]

        logger.info(
            "[skill-catalog] matches — %d candidate(s), top=%d session=%s msg=%.60s",
            len(scored), len(top), session_id or "?", user_message,
        )

        # Load SKILL.md content
        loaded = _load_matched_skills(top)
        if not loaded:
            logger.debug("[skill-catalog] all matched skills failed to load")
            return None

        # Format injection block
        block = _format_inject_block(loaded)

        # Hard cap on total injection size
        if len(block.encode("utf-8")) > _MAX_TOTAL_INJECT_BYTES:
            block = block[:_MAX_TOTAL_INJECT_BYTES] + "\n(inject truncated)"
            logger.warning("[skill-catalog] injection truncated to %d bytes", _MAX_TOTAL_INJECT_BYTES)

        # Log usage
        _log_usage(loaded, session_id, platform)

        logger.info(
            "[skill-catalog] injected %d skill(s) — session=%s bytes=%d",
            len(loaded), session_id or "?", len(block.encode("utf-8")),
        )
        return {"context": block}

    except Exception as exc:
        logger.warning("[skill-catalog] pre_llm_call injection failed: %s", exc)
        return None


# ══════════════════════════════════════════════════════════════════════════════
# registration
# ══════════════════════════════════════════════════════════════════════════════

def register(ctx) -> None:
    """Entry point called by the Hermes plugin system."""
    ctx.register_hook("pre_llm_call", _inject_skill_catalog)
    logger.info("[skill-catalog] plugin registered (pre_llm_call catalog injection)")

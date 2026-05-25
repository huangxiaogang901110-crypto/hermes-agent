"""
WeCom 附件内容自动注入 — Phase 2 (txt/md 纯文本).

在 MessageEvent 分发前自动读取 media_urls 中的纯文本文件内容，
注入到 event.text 前缀。不影响图片/语音/普通文件链路。

注入点: wecom.py _on_message / _flush_text_batch 分发前调用。
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import List

logger = logging.getLogger(__name__)

# ── 白名单 ──
TEXT_EXTENSIONS: set[str] = {
    ".txt", ".md", ".log",
    ".json", ".yaml", ".yml",
    ".csv", ".xml",
    ".py", ".sh",
}

# ── 限制 ──
SINGLE_FILE_MAX_BYTES: int = 500 * 1024      # 500 KB
TOTAL_INJECT_MAX_BYTES: int = 800 * 1024      # 800 KB


def _is_text_file(path: Path) -> bool:
    """快速检测：前 8KB 无 NULL 字节 → 文本，否则二进制。"""
    try:
        with open(path, "rb") as f:
            head = f.read(8192)
        return b"\x00" not in head
    except OSError:
        return False


def _read_text_content(path: Path, max_bytes: int) -> str | None:
    """UTF-8 优先 → 失败降级 errors=replace。超过 max_bytes 截断。"""
    try:
        raw = path.read_bytes()
    except OSError:
        return None

    if len(raw) > max_bytes:
        raw = raw[:max_bytes]

    if b"\x00" in raw[:8192]:
        return None  # 二进制跳过

    for encoding in ("utf-8", "utf-8-sig", "latin-1"):
        try:
            return raw.decode(encoding)
        except (UnicodeDecodeError, LookupError):
            continue

    # 最后兜底
    return raw.decode("utf-8", errors="replace")


def inject_text_attachments(media_urls: List[str]) -> str | None:
    """
    从 media_urls 中读取白名单扩展名的纯文本文件内容。

    Returns:
        注入文本块 (含 [附件内容] 前缀)，或 None（无有效文本附件）。
    """
    if not media_urls:
        return None

    blocks: list[str] = []
    total: int = 0

    for url in media_urls:
        path = Path(url)
        if not path.is_file():
            continue

        ext = path.suffix.lower()
        if ext not in TEXT_EXTENSIONS:
            continue

        # 大小检查
        try:
            fsize = path.stat().st_size
        except OSError:
            continue
        if fsize == 0:
            continue
        if fsize > SINGLE_FILE_MAX_BYTES:
            logger.debug(
                "[wecom] 附件过大跳过: %s (%d bytes)", path.name, fsize
            )
            continue

        # 二进制检测
        if not _is_text_file(path):
            logger.debug("[wecom] 二进制文件跳过: %s", path.name)
            continue

        # 读取
        content = _read_text_content(path, SINGLE_FILE_MAX_BYTES)
        if content is None:
            continue

        block = f"\n[附件内容: {path.name}]\n{content}"
        if total + len(block.encode("utf-8", errors="replace")) > TOTAL_INJECT_MAX_BYTES:
            logger.debug("[wecom] 总注入已达上限，跳过: %s", path.name)
            continue

        blocks.append(block)
        total += len(block.encode("utf-8", errors="replace"))

    if not blocks:
        return None

    return "".join(blocks)

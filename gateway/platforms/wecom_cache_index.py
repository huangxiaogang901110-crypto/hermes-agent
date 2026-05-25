"""
WeCom 缓存文件侧车索引 (sidecar index).

当 appmsg 长消息被企微自动转 .docx 并下载到本地缓存后，
写入同名 .sidecar.json 记录元数据，供后续 doc.weixin.qq.com
URL 文本消息查找匹配缓存文件。
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import time
from pathlib import Path
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)

# ── 常量 ──
SIDECAR_SUFFIX = ".sidecar.json"
MAX_AGE_SECONDS = 30 * 60  # 30 分钟

# doc.weixin.qq.com URL 中提取 w3_xxx doc_id
_URL_DOC_ID_RE = re.compile(r"doc\.weixin\.qq\.com/doc/(w3_[A-Za-z0-9_]+)")


def extract_doc_id_from_url(url: str) -> Optional[str]:
    """从 doc.weixin.qq.com URL 提取 w3_xxx doc_id。"""
    m = _URL_DOC_ID_RE.search(url)
    return m.group(1) if m else None


def _sha256_file(path: Path) -> str:
    try:
        h = hashlib.sha256()
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(65536), b""):
                h.update(chunk)
        return h.hexdigest()
    except OSError:
        return ""


def write_sidecar(
    cache_path: str,
    sender: str,
    chat_id: str,
    msgid: str,
    source_url: Optional[str] = None,
) -> Optional[Path]:
    """
    为缓存文件写入侧车 JSON 索引。

    成功返回 sidecar 文件路径，失败返回 None（不炸主流程）。
    """
    cache = Path(cache_path)
    if not cache.is_file():
        return None

    sidecar_path = cache.with_suffix(cache.suffix + SIDECAR_SUFFIX)

    doc_id = extract_doc_id_from_url(source_url) if source_url else None

    record: Dict = {
        "doc_id": doc_id,
        "source_url": source_url,
        "msgid": msgid,
        "sender": sender,
        "chat_id": chat_id,
        "cache_path": str(cache),
        "created_at": time.time(),
        "sha256": _sha256_file(cache),
    }

    try:
        sidecar_path.write_text(json.dumps(record, ensure_ascii=False), encoding="utf-8")
        logger.debug("[wecom] sidecar written: %s", sidecar_path.name)
        return sidecar_path
    except OSError as exc:
        logger.warning("[wecom] failed to write sidecar %s: %s", sidecar_path, exc)
        return None


def _scan_sidecars(cache_dir: Optional[str] = None) -> List[Dict]:
    """扫描缓存目录下所有侧车文件，返回记录列表。"""
    if cache_dir is None:
        from gateway.platforms.base import get_document_cache_dir
        cache_dir = str(get_document_cache_dir())

    records: List[Dict] = []
    try:
        for entry in os.scandir(cache_dir):
            if not entry.is_file() or not entry.name.endswith(SIDECAR_SUFFIX):
                continue
            try:
                data = json.loads(Path(entry.path).read_text(encoding="utf-8"))
                if isinstance(data, dict):
                    records.append(data)
            except (json.JSONDecodeError, OSError) as exc:
                logger.warning("[wecom] corrupt sidecar skipped: %s (%s)", entry.name, exc)
    except OSError:
        pass

    return records


def _is_cache_valid(record: Dict, max_age: float = MAX_AGE_SECONDS) -> bool:
    """检查侧车记录对应的缓存文件是否存在且未过期。"""
    cache_path = Path(str(record.get("cache_path", "")))
    if not cache_path.is_file():
        return False
    age = time.time() - float(record.get("created_at", 0))
    return age <= max_age


def find_by_doc_id(doc_id: str, cache_dir: Optional[str] = None) -> Optional[str]:
    """
    按 doc_id 精确匹配缓存文件。

    Returns:
        cache_path 字符串，或 None。
    """
    if not doc_id:
        return None
    for record in _scan_sidecars(cache_dir):
        if record.get("doc_id") == doc_id and _is_cache_valid(record):
            return str(record["cache_path"])
    return None


def find_by_context(
    sender: str,
    chat_id: str,
    max_age: float = MAX_AGE_SECONDS,
    cache_dir: Optional[str] = None,
) -> Optional[str]:
    """
    按 sender + chat_id + 时间窗口 兜底匹配。

    仅当唯一候选时返回，0 或 2+ 候选均返回 None。

    Returns:
        cache_path 字符串，或 None。
    """
    candidates: List[str] = []
    for record in _scan_sidecars(cache_dir):
        if not _is_cache_valid(record, max_age=max_age):
            continue
        if record.get("sender") != sender:
            continue
        if record.get("chat_id") != chat_id:
            continue
        candidates.append(str(record["cache_path"]))

    if len(candidates) == 1:
        return candidates[0]
    if len(candidates) > 1:
        logger.warning(
            "[wecom] multiple sidecar candidates for sender=%s chat=%s, skipping",
            sender, chat_id,
        )
    return None

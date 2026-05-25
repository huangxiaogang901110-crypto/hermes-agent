"""Tests for WeCom cache sidecar index and doc.weixin.qq.com URL resolution."""

import json
import os
import tempfile
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from gateway.platforms.wecom_cache_index import (
    extract_doc_id_from_url,
    find_by_doc_id,
    find_by_context,
    write_sidecar,
    _scan_sidecars,
    _is_cache_valid,
    MAX_AGE_SECONDS,
)


class TestExtractDocId:
    def test_extracts_w3_doc_id(self):
        url = "https://doc.weixin.qq.com/doc/w3_AWYAwwZ_ABUCNjQmpAvNzTT2A9QNO?scode=xxx"
        assert extract_doc_id_from_url(url) == "w3_AWYAwwZ_ABUCNjQmpAvNzTT2A9QNO"

    def test_extracts_from_text_with_prefix(self):
        text = "规：加载eng-pr https://doc.weixin.qq.com/doc/w3_ABC123?scode=x"
        assert extract_doc_id_from_url(text) == "w3_ABC123"

    def test_no_url_returns_none(self):
        assert extract_doc_id_from_url("hello world") is None

    def test_non_weixin_url_returns_none(self):
        assert extract_doc_id_from_url("https://example.com/doc/w3_xxx") is None


class TestWriteAndScanSidecar:
    def test_write_and_scan(self, tmp_path):
        cache_file = tmp_path / "test.docx"
        cache_file.write_text("hello")
        sidecar = write_sidecar(
            str(cache_file),
            sender="user1",
            chat_id="chat1",
            msgid="msg123",
        )
        assert sidecar is not None
        assert sidecar.name.endswith(".docx.sidecar.json")

        data = json.loads(sidecar.read_text())
        assert data["sender"] == "user1"
        assert data["chat_id"] == "chat1"
        assert data["msgid"] == "msg123"
        assert data["sha256"]

    def test_write_with_source_url_extracts_doc_id(self, tmp_path):
        cache_file = tmp_path / "test.docx"
        cache_file.write_text("world")
        sidecar = write_sidecar(
            str(cache_file),
            sender="u1", chat_id="c1", msgid="m1",
            source_url="https://doc.weixin.qq.com/doc/w3_MYDOCID?scode=x",
        )
        data = json.loads(sidecar.read_text())
        assert data["doc_id"] == "w3_MYDOCID"

    def test_corrupt_json_not_crash(self, tmp_path):
        bad = tmp_path / "bad.docx.sidecar.json"
        bad.write_text("not json{{{")
        records = _scan_sidecars(str(tmp_path))
        assert len(records) == 0  # corrupt → skipped


class TestFindByDocId:
    def test_exact_match(self, tmp_path):
        cache_file = tmp_path / "a.docx"
        cache_file.write_text("content")
        write_sidecar(
            str(cache_file), sender="u1", chat_id="c1", msgid="m1",
            source_url="https://doc.weixin.qq.com/doc/w3_TARGET",
        )
        result = find_by_doc_id("w3_TARGET", cache_dir=str(tmp_path))
        assert result == str(cache_file)

    def test_no_match_returns_none(self, tmp_path):
        cache_file = tmp_path / "b.docx"
        cache_file.write_text("x")
        write_sidecar(str(cache_file), sender="u1", chat_id="c1", msgid="m1")
        assert find_by_doc_id("w3_NOMATCH", cache_dir=str(tmp_path)) is None

    def test_expired_returns_none(self, tmp_path):
        cache_file = tmp_path / "old.docx"
        cache_file.write_text("old")
        sidecar = write_sidecar(
            str(cache_file), sender="u1", chat_id="c1", msgid="m1",
            source_url="https://doc.weixin.qq.com/doc/w3_OLD",
        )
        # Rewrite created_at to be expired
        data = json.loads(sidecar.read_text())
        data["created_at"] = time.time() - MAX_AGE_SECONDS - 60
        sidecar.write_text(json.dumps(data))
        assert find_by_doc_id("w3_OLD", cache_dir=str(tmp_path)) is None


class TestFindByContext:
    def test_single_match(self, tmp_path):
        cache_file = tmp_path / "solo.docx"
        cache_file.write_text("solo")
        write_sidecar(str(cache_file), sender="u1", chat_id="c1", msgid="m1")
        result = find_by_context("u1", "c1", cache_dir=str(tmp_path))
        assert result == str(cache_file)

    def test_multiple_matches_returns_none(self, tmp_path):
        for i in range(3):
            f = tmp_path / f"multi_{i}.docx"
            f.write_text(f"doc{i}")
            write_sidecar(str(f), sender="u1", chat_id="c1", msgid=f"m{i}")
        assert find_by_context("u1", "c1", cache_dir=str(tmp_path)) is None

    def test_different_sender_no_match(self, tmp_path):
        f = tmp_path / "d.docx"
        f.write_text("d")
        write_sidecar(str(f), sender="u1", chat_id="c1", msgid="m1")
        assert find_by_context("u2", "c1", cache_dir=str(tmp_path)) is None

    def test_different_chat_no_match(self, tmp_path):
        f = tmp_path / "e.docx"
        f.write_text("e")
        write_sidecar(str(f), sender="u1", chat_id="c1", msgid="m1")
        assert find_by_context("u1", "c2", cache_dir=str(tmp_path)) is None

    def test_expired_no_match(self, tmp_path):
        f = tmp_path / "expired.docx"
        f.write_text("expired")
        sidecar = write_sidecar(str(f), sender="u1", chat_id="c1", msgid="m1")
        data = json.loads(sidecar.read_text())
        data["created_at"] = time.time() - MAX_AGE_SECONDS - 1
        sidecar.write_text(json.dumps(data))
        assert find_by_context("u1", "c1", cache_dir=str(tmp_path)) is None


class TestCacheValid:
    def test_valid_cache(self, tmp_path):
        f = tmp_path / "valid.docx"
        f.write_text("ok")
        record = {"cache_path": str(f), "created_at": time.time()}
        assert _is_cache_valid(record)

    def test_missing_file_invalid(self, tmp_path):
        record = {"cache_path": str(tmp_path / "ghost.docx"), "created_at": time.time()}
        assert not _is_cache_valid(record)

    def test_expired_invalid(self, tmp_path):
        f = tmp_path / "oldfile.docx"
        f.write_text("old")
        record = {"cache_path": str(f), "created_at": time.time() - MAX_AGE_SECONDS - 1}
        assert not _is_cache_valid(record)

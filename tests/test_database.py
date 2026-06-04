"""
Test database initialization and helper functions.
"""

import os
import sqlite3
from pathlib import Path

import pytest

from server import (
    compute_checksum,
    compute_simhash,
    hamming_distance,
    detect_type,
    _filter_sensitive,
    _compute_confidence,
)


class TestComputeChecksum:
    """Test SHA256-based checksum computation."""

    def test_identical_content_same_checksum(self):
        c1 = compute_checksum("Hello world")
        c2 = compute_checksum("Hello world")
        assert c1 == c2

    def test_different_content_different_checksum(self):
        c1 = compute_checksum("Hello world")
        c2 = compute_checksum("Hello world!")
        assert c1 != c2

    def test_whitespace_stripped(self):
        c1 = compute_checksum("  Hello world  ")
        c2 = compute_checksum("Hello world")
        assert c1 == c2

    def test_empty_string(self):
        result = compute_checksum("")
        assert isinstance(result, str)
        assert len(result) == 16

    def test_unicode_content(self):
        c1 = compute_checksum("你好世界")
        c2 = compute_checksum("你好世界")
        assert c1 == c2


class TestComputeSimhash:
    """Test SimHash fingerprint computation."""

    def test_similar_content_similar_hash(self):
        h1 = compute_simhash("The quick brown fox jumps over the lazy dog")
        h2 = compute_simhash("The quick brown fox jumps over the lazy cat")
        dist = hamming_distance(h1, h2)
        # Similar content should have small Hamming distance
        assert dist < 20, f"Expected small distance, got {dist}"

    def test_different_content_different_hash(self):
        h1 = compute_simhash("Python programming language")
        h2 = compute_simhash("Cooking recipes for desserts")
        dist = hamming_distance(h1, h2)
        # Very different content should have large distance
        assert dist > 15, f"Expected large distance, got {dist}"

    def test_identical_content_identical_hash(self):
        h1 = compute_simhash("Test content here")
        h2 = compute_simhash("Test content here")
        assert h1 == h2

    def test_empty_content(self):
        result = compute_simhash("")
        assert isinstance(result, str)
        # Empty should give all-zero hex string (64 bits = 16 hex chars)
        assert result == "0" * 16 or len(result) == 16

    def test_simhash_length(self):
        result = compute_simhash("Some content here for testing purposes")
        assert len(result) == 16  # 64 bits = 16 hex chars


class TestHammingDistance:
    """Test Hamming distance computation."""

    def test_identical_hashes(self):
        h = "a1b2c3d4e5f6a7b8"
        assert hamming_distance(h, h) == 0

    def test_completely_different(self):
        h1 = "0000000000000000"
        h2 = "ffffffffffffffff"
        # All 64 bits differ -> hamming distance should be 64 or close
        assert hamming_distance(h1, h2) >= 32

    def test_null_handling(self):
        assert hamming_distance("", "abc123") == 64
        assert hamming_distance("abc123", "") == 64
        assert hamming_distance(None, "abc") == 64  # type: ignore
        assert hamming_distance("abc", None) == 64  # type: ignore

    def test_invalid_hex(self):
        assert hamming_distance("zzzz", "aaaa") == 64


class TestDetectType:
    """Test content-type auto-detection."""

    def test_detect_decision(self):
        ct = "I have decided to use Python for this project"
        assert detect_type(ct) in ["decision", "context"]

    def test_detect_rule(self):
        ct = "The naming convention rule is to use snake_case for variables"
        assert detect_type(ct) == "rule"

    def test_detect_learning(self):
        ct = "I learned that PostgreSQL is faster for analytical queries"
        assert detect_type(ct) == "learning"

    def test_detect_preference(self):
        ct = "I prefer using ruff over black for formatting"
        assert detect_type(ct) == "preference"

    def test_detect_procedural(self):
        ct = "标准操作流程：第一步，检查系统状态；第二步，备份数据"
        assert detect_type(ct) in ["procedural", "progress"]

    def test_detect_procedural_english(self):
        ct = "Step by step guide: install, configure, deploy"
        assert detect_type(ct) in ["procedural", "progress"]

    def test_default_to_general(self):
        ct = "Some random text without specific keywords"
        assert detect_type(ct) == "general"

    def test_detect_debugging(self):
        ct = "Found a bug: root cause was a null pointer exception"
        assert detect_type(ct) == "debugging"

    def test_detect_feature(self):
        ct = "Adding a new feature: implement user authentication"
        assert detect_type(ct) == "feature"

    def test_detect_context(self):
        ct = "Project architecture uses microservices with FastAPI"
        assert detect_type(ct) == "context"


class TestFilterSensitive:
    """Test privacy filter for sensitive data."""

    def test_api_key_redacted(self):
        content = "api_key=sk-abc123xyz456def789"
        result = _filter_sensitive(content)
        assert "[REDACTED]" in result
        assert "sk-abc123" not in result

    def test_bearer_token_redacted(self):
        content = "Authorization: Bearer eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0"
        result = _filter_sensitive(content)
        assert "Bearer [REDACTED]" in result

    def test_password_redacted(self):
        content = 'password = "super_secret_12345"'
        result = _filter_sensitive(content)
        assert "[REDACTED]" in result
        assert "super_secret" not in result

    def test_no_sensitive_data(self):
        content = "This is a normal memory without secrets"
        result = _filter_sensitive(content)
        assert result == content

    def test_openai_key_redacted(self):
        # 使用足够长的假key以匹配正则模式 sk-[a-zA-Z0-9]{20,}
        content = "sk-aaaaaaaaaaaaaaaaaaaaaaaaa"
        result = _filter_sensitive(content)
        assert "[API-KEY-REDACTED]" in result
        assert "sk-proj" not in result

    def test_empty_content(self):
        assert _filter_sensitive("") == ""


class TestComputeConfidence:
    """Test confidence score computation."""

    def test_high_confidence_for_procedural(self):
        score = _compute_confidence("procedural", "system", 200)
        assert score >= 0.95

    def test_low_confidence_for_short_content(self):
        score = _compute_confidence("general", "unknown", 10)
        assert score <= 0.7

    def test_source_bonus(self):
        system_score = _compute_confidence("general", "system", 100)
        unknown_score = _compute_confidence("general", "unknown", 100)
        assert system_score > unknown_score

    def test_length_penalty(self):
        short_score = _compute_confidence("general", "unknown", 15)
        long_score = _compute_confidence("general", "unknown", 100)
        assert long_score > short_score

    def test_clamping(self):
        # Should never go below 0.3
        score = _compute_confidence("general", "unknown", 5)
        assert score >= 0.3
        # Should never go above 1.0
        high_score = _compute_confidence("procedural", "system", 500)
        assert high_score <= 1.0


class TestInitDb:
    """Test database initialization (using the server's init_db through the file DB)."""

    def test_init_creates_tables(self):
        """Verify that init_db creates all expected tables."""
        db_path = Path(os.environ["MEMORY_DATA_DIR"]) / "memory.db"
        from server import init_db, get_db

        conn = get_db()
        try:
            init_db(conn)

            # Check core tables exist
            tables = [
                "memories",
                "categories",
                "session_memories",
                "change_log",
                "sync_status",
                "memory_relations",
                "memory_versions",
                "evolution_log",
                "memory_branches",
                "search_audit_log",
                "raw_memories",
            ]
            for table in tables:
                row = conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
                    (table,),
                ).fetchone()
                assert row is not None, f"Table '{table}' not created"
        finally:
            conn.close()

    def test_init_inserts_default_categories(self):
        """Verify default categories are seeded."""
        db_path = Path(os.environ["MEMORY_DATA_DIR"]) / "memory.db"
        from server import init_db, get_db

        conn = get_db()
        try:
            init_db(conn)
            count = conn.execute("SELECT COUNT(*) FROM categories").fetchone()[0]
            assert count >= 5, f"Expected at least 5 default categories, got {count}"
        finally:
            conn.close()

    def test_init_idempotent(self):
        """Calling init_db twice should not raise."""
        from server import init_db, get_db

        conn = get_db()
        try:
            init_db(conn)
            init_db(conn)  # second call
            assert True  # no exception
        finally:
            conn.close()

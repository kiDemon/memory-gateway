"""
pytest fixtures for Memory Gateway testing.

Uses a temporary SQLite database to avoid polluting production data.
Sets MEMORY_DATA_DIR to a temp dir before importing server.py.
"""

import os
import sys
import tempfile
import sqlite3
from pathlib import Path
from typing import Generator

import pytest
from fastapi.testclient import TestClient

# Ensure server module is importable
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# Monkey-patch DATA_DIR and DB_PATH before importing server
_tmp_dir = tempfile.mkdtemp(prefix="memory_gateway_test_")
os.environ["MEMORY_DATA_DIR"] = _tmp_dir
# 设置测试用的API Key（禁用认证）
os.environ["MEMORY_API_KEY"] = "test-api-key-for-testing-only"

# Now import server (it reads MEMORY_DATA_DIR at import time)
from memory_gateway.services.version_service import VersionManager
from memory_gateway.utils.crypto import compute_checksum, compute_simhash, hamming_distance, _find_near_duplicate
from server import (
    app,
    detect_type,
    _filter_sensitive,
    _compute_confidence,
    _build_timeline,
    _extract_key_terms,
    DB_PATH,
    DATA_DIR,
)


@pytest.fixture(scope="session")
def tmp_data_dir() -> str:
    """Return the temporary data directory path."""
    return _tmp_dir


@pytest.fixture(autouse=True)
def _clean_db_dir(tmp_data_dir):
    """Clean the DB file before each test so tests are isolated."""
    db_path = Path(tmp_data_dir) / "memory.db"
    if db_path.exists():
        db_path.unlink()
    for f in [
        db_path.with_suffix(".db-wal"),
        db_path.with_suffix(".db-shm"),
    ]:
        if f.exists():
            f.unlink()
    yield


@pytest.fixture
def db() -> Generator[sqlite3.Connection, None, None]:
    """Create a fresh in-memory scratch database, fully initialized with schema."""
    conn = sqlite3.connect(":memory:", timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=5000")
    _apply_full_schema(conn)
    yield conn
    conn.close()


def _apply_full_schema(conn: sqlite3.Connection):
    """Apply the complete schema (same DDL as init_db)."""
    conn.executescript("""
    CREATE TABLE IF NOT EXISTS categories (
        id          TEXT PRIMARY KEY,
        name        TEXT NOT NULL,
        parent_id   TEXT REFERENCES categories(id),
        icon        TEXT DEFAULT '📁',
        sort_order  INTEGER DEFAULT 0,
        created_at  TEXT NOT NULL DEFAULT (datetime('now'))
    );

    INSERT OR IGNORE INTO categories (id, name, parent_id, icon, sort_order) VALUES
    ('learning', '学习', NULL, '📚', 1),
    ('life', '生活', NULL, '🏠', 2),
    ('work', '工作', NULL, '💼', 3),
    ('innovation', '创新', NULL, '💡', 4),
    ('general', '通用', NULL, '📁', 0),
    ('work_comprehensive', '综合', 'work', '📋', 1),
    ('work_hr', '人力', 'work', '👥', 2),
    ('work_finance', '财务', 'work', '💰', 3),
    ('work_construction', '建设', 'work', '🏗️', 4),
    ('work_maintenance', '维护', 'work', '🔧', 5),
    ('work_bizdev', '行拓', 'work', '🚀', 6),
    ('work_energy', '能源', 'work', '⚡', 7),
    ('work_regional', '区域', 'work', '🌍', 8);

    CREATE TABLE IF NOT EXISTS memories (
        id          TEXT PRIMARY KEY,
        content     TEXT NOT NULL,
        type        TEXT NOT NULL DEFAULT 'general',
        scope       TEXT NOT NULL DEFAULT 'global',
        source      TEXT NOT NULL DEFAULT 'unknown',
        priority    TEXT NOT NULL DEFAULT 'P1',
        confidence  REAL NOT NULL DEFAULT 0.8,
        tags        TEXT DEFAULT '[]',
        category_id TEXT DEFAULT 'general',
        embedding   BLOB,
        hot_tier    INTEGER NOT NULL DEFAULT 0,
        ttl_days    INTEGER NOT NULL DEFAULT 0,
        vector_clock TEXT DEFAULT '{}',
        created_at  TEXT NOT NULL,
        updated_at  TEXT NOT NULL,
        last_recalled TEXT,
        recall_count INTEGER NOT NULL DEFAULT 0,
        archived    INTEGER NOT NULL DEFAULT 0,
        checksum    TEXT NOT NULL,
        simhash     TEXT DEFAULT '',
        insights    TEXT DEFAULT '',
        derived_from TEXT,
        superseded_by TEXT
    );

    CREATE TABLE IF NOT EXISTS session_memories (
        session_id  TEXT NOT NULL,
        memory_id   TEXT NOT NULL,
        created_at  TEXT NOT NULL,
        PRIMARY KEY (session_id, memory_id),
        FOREIGN KEY (memory_id) REFERENCES memories(id)
    );

    CREATE TABLE IF NOT EXISTS change_log (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        memory_id   TEXT NOT NULL,
        action      TEXT NOT NULL,
        snapshot    TEXT,
        timestamp   TEXT NOT NULL
    );

    CREATE TABLE IF NOT EXISTS sync_status (
        tool        TEXT PRIMARY KEY,
        last_sync   TEXT NOT NULL DEFAULT (datetime('now')),
        last_beat   TEXT NOT NULL DEFAULT (datetime('now')),
        total_syncs INTEGER DEFAULT 0,
        last_count  INTEGER DEFAULT 0,
        status      TEXT NOT NULL DEFAULT 'healthy'
    );

    CREATE TABLE IF NOT EXISTS memory_relations (
        source_id TEXT NOT NULL,
        target_id TEXT NOT NULL,
        relation  TEXT NOT NULL DEFAULT 'related_to',
        strength  REAL DEFAULT 1.0,
        created_at TEXT NOT NULL DEFAULT (datetime('now')),
        PRIMARY KEY (source_id, target_id)
    );

    CREATE TABLE IF NOT EXISTS memory_versions (
        id              TEXT PRIMARY KEY,
        memory_id       TEXT NOT NULL,
        version         INTEGER NOT NULL,
        content         TEXT NOT NULL,
        content_hash    TEXT NOT NULL,
        diff_from_prev  TEXT,
        change_type     TEXT NOT NULL DEFAULT 'create',
        changed_by      TEXT DEFAULT 'system',
        change_reason   TEXT,
        metadata_snapshot TEXT,
        created_at      TEXT NOT NULL DEFAULT (datetime('now')),
        FOREIGN KEY (memory_id) REFERENCES memories(id) ON DELETE CASCADE,
        UNIQUE(memory_id, version)
    );

    CREATE TABLE IF NOT EXISTS evolution_log (
        id              INTEGER PRIMARY KEY AUTOINCREMENT,
        memory_id       TEXT NOT NULL,
        event_type      TEXT NOT NULL,
        from_version    INTEGER,
        to_version      INTEGER,
        agent           TEXT,
        details         TEXT,
        created_at      TEXT NOT NULL DEFAULT (datetime('now')),
        FOREIGN KEY (memory_id) REFERENCES memories(id) ON DELETE CASCADE
    );

    CREATE TABLE IF NOT EXISTS memory_branches (
        id              TEXT PRIMARY KEY,
        memory_id       TEXT NOT NULL,
        branch_name     TEXT NOT NULL,
        version         INTEGER NOT NULL,
        source          TEXT DEFAULT 'system',
        created_at      TEXT NOT NULL DEFAULT (datetime('now')),
        FOREIGN KEY (memory_id) REFERENCES memories(id) ON DELETE CASCADE,
        UNIQUE(memory_id, branch_name)
    );

    CREATE TABLE IF NOT EXISTS search_audit_log (
        id              INTEGER PRIMARY KEY AUTOINCREMENT,
        query           TEXT NOT NULL,
        source          TEXT NOT NULL DEFAULT 'unknown',
        result_count    INTEGER NOT NULL DEFAULT 0,
        result_ids      TEXT DEFAULT '[]',
        latency_ms      REAL DEFAULT 0,
        search_type     TEXT NOT NULL DEFAULT 'fts5',
        hit_cache       INTEGER NOT NULL DEFAULT 0,
        created_at      TEXT NOT NULL DEFAULT (datetime('now'))
    );

    CREATE TABLE IF NOT EXISTS scenarios (
        id              TEXT PRIMARY KEY,
        category_id     TEXT NOT NULL DEFAULT 'general',
        title           TEXT NOT NULL,
        summary         TEXT NOT NULL,
        memory_ids      TEXT NOT NULL DEFAULT '[]',
        time_window_start TEXT,
        time_window_end   TEXT,
        created_at      TEXT NOT NULL DEFAULT (datetime('now')),
        updated_at      TEXT NOT NULL DEFAULT (datetime('now'))
    );

    -- L3 画像层：用户/项目/领域画像
    CREATE TABLE IF NOT EXISTS personas (
        id              TEXT PRIMARY KEY,
        persona_type    TEXT NOT NULL,
        name            TEXT NOT NULL,
        profile_md      TEXT NOT NULL DEFAULT '',
        version         INTEGER NOT NULL DEFAULT 1,
        created_at      TEXT NOT NULL DEFAULT (datetime('now')),
        updated_at      TEXT NOT NULL DEFAULT (datetime('now'))
    );

    CREATE TABLE IF NOT EXISTS skills (
        id              TEXT PRIMARY KEY,
        name            TEXT NOT NULL,
        description     TEXT NOT NULL DEFAULT '',
        trigger_pattern TEXT NOT NULL DEFAULT '',
        content_md      TEXT NOT NULL DEFAULT '',
        category        TEXT NOT NULL DEFAULT 'general',
        source_memory_ids TEXT NOT NULL DEFAULT '[]',
        confidence      REAL NOT NULL DEFAULT 0.0,
        recall_count    INTEGER NOT NULL DEFAULT 0,
        agent_scope     TEXT NOT NULL DEFAULT 'all',
        created_at      TEXT NOT NULL DEFAULT (datetime('now')),
        updated_at      TEXT NOT NULL DEFAULT (datetime('now'))
    );

    CREATE TABLE IF NOT EXISTS raw_memories (
        id              TEXT PRIMARY KEY,
        session_id      TEXT,
        source          TEXT NOT NULL DEFAULT 'unknown',
        content         TEXT NOT NULL,
        token_count     INTEGER NOT NULL DEFAULT 0,
        memory_id       TEXT,
        created_at      TEXT NOT NULL DEFAULT (datetime('now'))
    );

    CREATE TABLE IF NOT EXISTS login_attempts (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        ip_address TEXT NOT NULL,
        success INTEGER NOT NULL DEFAULT 0,
        attempted_at TEXT NOT NULL DEFAULT (datetime('now')),
        user_agent TEXT DEFAULT ''
    );

    CREATE TABLE IF NOT EXISTS user_sessions (
        token TEXT PRIMARY KEY,
        ip_address TEXT NOT NULL,
        created_at REAL NOT NULL,
        expires_at REAL NOT NULL
    );

    CREATE TABLE IF NOT EXISTS ip_lockouts (
        ip_address TEXT PRIMARY KEY,
        unlock_time REAL NOT NULL,
        failure_count INTEGER NOT NULL DEFAULT 0
    );

    -- FTS5 全文索引 (trigram prefix=2,1 支持1-2字符前缀查询)
    CREATE VIRTUAL TABLE IF NOT EXISTS memories_fts USING fts5(
        content,
        category_id,
        tags,
        type,
        scope,
        source,
        content=memories,
        content_rowid=rowid,
        tokenize='trigram'
    );

    -- 触发器：插入时同步 FTS
    CREATE TRIGGER IF NOT EXISTS memories_ai AFTER INSERT ON memories BEGIN
        INSERT INTO memories_fts(rowid, content, category_id, tags, type, scope, source)
        VALUES (new.rowid, new.content, new.category_id, new.tags, new.type, new.scope, new.source);
    END;

    -- 触发器：删除时同步 FTS
    CREATE TRIGGER IF NOT EXISTS memories_ad AFTER DELETE ON memories BEGIN
        INSERT INTO memories_fts(memories_fts, rowid, content, category_id, tags, type, scope, source)
        VALUES ('delete', old.rowid, old.content, old.category_id, old.tags, old.type, old.scope, old.source);
    END;

    -- 触发器：更新时同步 FTS
    CREATE TRIGGER IF NOT EXISTS memories_au AFTER UPDATE ON memories BEGIN
        INSERT INTO memories_fts(memories_fts, rowid, content, category_id, tags, type, scope, source)
        VALUES ('delete', old.rowid, old.content, old.category_id, old.tags, old.type, old.scope, old.source);
        INSERT INTO memories_fts(rowid, content, category_id, tags, type, scope, source)
        VALUES (new.rowid, new.content, new.category_id, new.tags, new.type, new.scope, new.source);
    END;

    -- 索引
    CREATE INDEX IF NOT EXISTS idx_memories_type ON memories(type);
    CREATE INDEX IF NOT EXISTS idx_memories_scope ON memories(scope);
    CREATE INDEX IF NOT EXISTS idx_memories_source ON memories(source);
    CREATE INDEX IF NOT EXISTS idx_memories_archived ON memories(archived);
    CREATE INDEX IF NOT EXISTS idx_memories_created ON memories(created_at);
    CREATE INDEX IF NOT EXISTS idx_memories_checksum ON memories(checksum);
    CREATE INDEX IF NOT EXISTS idx_memories_category ON memories(category_id);
    CREATE INDEX IF NOT EXISTS idx_memories_content ON memories(content);
    CREATE INDEX IF NOT EXISTS idx_session_session ON session_memories(session_id);
    CREATE INDEX IF NOT EXISTS idx_relations_source ON memory_relations(source_id);
    CREATE INDEX IF NOT EXISTS idx_relations_target ON memory_relations(target_id);
    CREATE INDEX IF NOT EXISTS idx_versions_memory ON memory_versions(memory_id, version DESC);
    CREATE INDEX IF NOT EXISTS idx_versions_hash ON memory_versions(content_hash);
    CREATE INDEX IF NOT EXISTS idx_evolution_memory ON evolution_log(memory_id, created_at DESC);
    CREATE INDEX IF NOT EXISTS idx_evolution_type ON evolution_log(event_type);
    CREATE INDEX IF NOT EXISTS idx_branches_memory ON memory_branches(memory_id);
    CREATE INDEX IF NOT EXISTS idx_branches_name ON memory_branches(memory_id, branch_name);
    CREATE INDEX IF NOT EXISTS idx_audit_query ON search_audit_log(created_at DESC);
    CREATE INDEX IF NOT EXISTS idx_audit_source ON search_audit_log(source, created_at DESC);
    CREATE INDEX IF NOT EXISTS idx_raw_session ON raw_memories(session_id, created_at DESC);
    CREATE INDEX IF NOT EXISTS idx_raw_memory ON raw_memories(memory_id);
    CREATE INDEX IF NOT EXISTS idx_scenarios_category ON scenarios(category_id, created_at DESC);
    CREATE INDEX IF NOT EXISTS idx_personas_type ON personas(persona_type, name);
    CREATE INDEX IF NOT EXISTS idx_skills_category ON skills(category);
    CREATE INDEX IF NOT EXISTS idx_skills_confidence ON skills(confidence DESC);
    CREATE INDEX IF NOT EXISTS idx_skills_name ON skills(name);
    CREATE INDEX IF NOT EXISTS idx_login_attempts_ip ON login_attempts(ip_address, attempted_at);
    CREATE INDEX IF NOT EXISTS idx_sessions_expires ON user_sessions(expires_at);
    """)
    conn.commit()


@pytest.fixture
def test_client() -> Generator[TestClient, None, None]:
    """FastAPI TestClient that writes to the temp file database."""
    with TestClient(app, headers={"X-API-Key": "test-api-key-for-testing-only"}) as client:
        yield client

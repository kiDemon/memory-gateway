"""
Database schema definition and migration logic.

All CREATE TABLE / INDEX / TRIGGER statements and incremental
schema migrations for the MCP Memory Server live here.
"""

import json
import logging
import sqlite3

from memory_gateway.config import log
from memory_gateway.database.connection import (
    _acquire_migration_lock,
    _release_migration_lock,
)


def init_db(db: sqlite3.Connection) -> None:
    """Initialize schema with categories, FTS5, triggers, and indexes.

    Performs incremental schema migrations (column additions, table
    rebuilds) to keep an existing database up-to-date.
    """
    # 获取迁移锁防止并发迁移
    lock_acquired = _acquire_migration_lock(db)
    if not lock_acquired:
        log.warning("Could not acquire migration lock, skipping migration")
        return

    try:
        # Schema migration: add category_id to old memories table if missing
        cursor = db.execute("PRAGMA table_info(memories)")
        columns = {row[1] for row in cursor.fetchall()}
    finally:
        _release_migration_lock(db)

    if columns and 'category_id' not in columns:
        log.info("Migrating: adding category_id column to memories")
        db.execute("ALTER TABLE memories ADD COLUMN category_id TEXT DEFAULT 'general'")
        db.commit()

    # Schema migration: add simhash to old memories table if missing
    if columns and 'simhash' not in columns:
        log.info("Migrating: adding simhash column to memories")
        db.execute("ALTER TABLE memories ADD COLUMN simhash TEXT DEFAULT ''")
        db.commit()
        # Backfill simhash for existing records
        # (lazy import to avoid circular dependency with server.py)
        from memory_gateway.utils.crypto import compute_simhash

        rows = db.execute(
            "SELECT id, content FROM memories WHERE simhash='' OR simhash IS NULL"
        ).fetchall()
        for r in rows:
            h = compute_simhash(r[1])
            db.execute("UPDATE memories SET simhash=? WHERE id=?", (h, r[0]))
        if rows:
            db.commit()
            log.info(f"Backfilled simhash for {len(rows)} memories")

    # Schema migration: add hot_tier, ttl_days, vector_clock for v5 features
    if columns:
        for col_name, col_default in [
            ("hot_tier", "0"),
            ("ttl_days", "0"),
            ("vector_clock", ""),  # empty string; code reads with `or "{}"` for JSON parsing
        ]:
            if col_name not in columns:
                log.info(f"Migrating: adding {col_name} column to memories")
                db.execute(
                    f"ALTER TABLE memories ADD COLUMN {col_name} TEXT DEFAULT '{col_default}'"
                )
                db.commit()

    # Schema migration: rebuild memory_relations without FK constraints
    # (source_id/target_id are knowledge graph terms, not memory UUIDs)
    try:
        rel_sql = db.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='memory_relations'"
        ).fetchone()
        if rel_sql and 'REFERENCES memories' in (rel_sql[0] or ''):
            log.info("Migrating: rebuilding memory_relations without FK constraints")
            db.executescript("""
                CREATE TABLE memory_relations_new (
                    source_id TEXT NOT NULL,
                    target_id TEXT NOT NULL,
                    relation  TEXT NOT NULL DEFAULT 'related_to',
                    strength  REAL DEFAULT 1.0,
                    created_at TEXT NOT NULL DEFAULT (datetime('now')),
                    PRIMARY KEY (source_id, target_id)
                );
                INSERT OR IGNORE INTO memory_relations_new
                    SELECT source_id, target_id, relation, strength, created_at
                    FROM memory_relations;
                DROP TABLE memory_relations;
                ALTER TABLE memory_relations_new RENAME TO memory_relations;
                CREATE INDEX IF NOT EXISTS idx_relations_source ON memory_relations(source_id);
                CREATE INDEX IF NOT EXISTS idx_relations_target ON memory_relations(target_id);
            """)
            db.commit()
            log.info("Migration complete: memory_relations FK constraints removed")
    except Exception as e:
        log.warning(f"memory_relations migration skipped: {e}", exc_info=True)

    # Schema migration: add derived_from and superseded_by for self-evolution
    if columns:
        for col_name, col_type, col_default in [
            ("derived_from", "TEXT", "NULL"),
            ("superseded_by", "TEXT", "NULL"),
        ]:
            if col_name not in columns:
                log.info(f"Migrating: adding {col_name} column to memories")
                db.execute(
                    f"ALTER TABLE memories ADD COLUMN {col_name} {col_type} DEFAULT {col_default}"
                )
                db.commit()

    # Schema migration: rebuild FTS5 if old schema (no category_id) or missing prefix
    needs_fts_rebuild = False
    try:
        fts_info = db.execute(
            "SELECT sql FROM sqlite_master WHERE name='memories_fts'"
        ).fetchone()
        if fts_info:
            fts_sql = fts_info[0] or ''
            if 'category_id' not in fts_sql:
                needs_fts_rebuild = True
            elif 'prefix' not in fts_sql or 'prefix=2,1' not in fts_sql:
                log.info(
                    "Migrating: rebuilding FTS5 with prefix=2,1 for short query support"
                )
                needs_fts_rebuild = True
            if needs_fts_rebuild:
                log.info("Dropping old FTS5 table for rebuild")
                db.executescript("""
                    DROP TRIGGER IF EXISTS memories_ai;
                    DROP TRIGGER IF EXISTS memories_ad;
                    DROP TRIGGER IF EXISTS memories_au;
                    DROP TABLE IF EXISTS memories_fts;
                """)
                db.commit()
    except Exception:
        log.warning("FTS5 schema check failed, proceeding without rebuild", exc_info=True)

    db.executescript("""
    PRAGMA journal_mode=WAL;
    PRAGMA foreign_keys=ON;
    PRAGMA busy_timeout=5000;

    -- 分类树
    CREATE TABLE IF NOT EXISTS categories (
        id          TEXT PRIMARY KEY,
        name        TEXT NOT NULL,
        parent_id   TEXT REFERENCES categories(id),
        icon        TEXT DEFAULT '\U0001f4c1',
        sort_order  INTEGER DEFAULT 0,
        created_at  TEXT NOT NULL DEFAULT (datetime('now'))
    );

    -- 预置分类（如果不存在）
    INSERT OR IGNORE INTO categories (id, name, parent_id, icon, sort_order) VALUES
    ('learning', '\u5b66\u4e60', NULL, '\U0001f4da', 1),
    ('life', '\u751f\u6d3b', NULL, '\U0001f3e0', 2),
    ('work', '\u5de5\u4f5c', NULL, '\U0001f4bc', 3),
    ('innovation', '\u521b\u65b0', NULL, '\U0001f4a1', 4),
    ('general', '\u901a\u7528', NULL, '\U0001f4c1', 0),
    ('work_comprehensive', '\u7efc\u5408', 'work', '\U0001f4cb', 1),
    ('work_hr', '\u4eba\u529b', 'work', '\U0001f465', 2),
    ('work_finance', '\u8d22\u52a1', 'work', '\U0001f4b0', 3),
    ('work_construction', '\u5efa\u8bbe', 'work', '\U0001f3d7\ufe0f', 4),
    ('work_maintenance', '\u7ef4\u62a4', 'work', '\U0001f527', 5),
    ('work_bizdev', '\u884c\u62d3', 'work', '\U0001f680', 6),
    ('work_energy', '\u80fd\u6e90', 'work', '\u26a1', 7),
    ('work_regional', '\u533a\u57df', 'work', '\U0001f30d', 8);

    -- 记忆表 (v5: +hot_tier, ttl_days, vector_clock)
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
        insights    TEXT DEFAULT ''
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

    -- 同步状态
    CREATE TABLE IF NOT EXISTS sync_status (
        tool        TEXT PRIMARY KEY,
        last_sync   TEXT NOT NULL DEFAULT (datetime('now')),
        last_beat   TEXT NOT NULL DEFAULT (datetime('now')),
        total_syncs INTEGER DEFAULT 0,
        last_count  INTEGER DEFAULT 0,
        status      TEXT NOT NULL DEFAULT 'healthy'
    );

    -- 记忆关联
    CREATE TABLE IF NOT EXISTS memory_relations (
        source_id TEXT NOT NULL,
        target_id TEXT NOT NULL,
        relation  TEXT NOT NULL DEFAULT 'related_to',
        strength  REAL DEFAULT 1.0,
        created_at TEXT NOT NULL DEFAULT (datetime('now')),
        PRIMARY KEY (source_id, target_id)
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

    -- \u2a50\u2a50\u2a50 \u8bb0\u5fc6\u7248\u672c\u63a7\u5236 (Git for Memory) \u2a50\u2a50\u2a50

    -- 记忆版本表：每次修改自动创建快照
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

    -- 进化日志表：记录每次进化事件
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

    -- 版本控制索引
    CREATE INDEX IF NOT EXISTS idx_versions_memory ON memory_versions(memory_id, version DESC);
    CREATE INDEX IF NOT EXISTS idx_versions_hash ON memory_versions(content_hash);
    CREATE INDEX IF NOT EXISTS idx_evolution_memory ON evolution_log(memory_id, created_at DESC);
    CREATE INDEX IF NOT EXISTS idx_evolution_type ON evolution_log(event_type);

    -- \u2a50\u2a50\u2a50 \u8bb0\u5fc6\u5206\u652f (Git-like Branching) \u2a50\u2a50\u2a50
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

    CREATE INDEX IF NOT EXISTS idx_branches_memory ON memory_branches(memory_id);
    CREATE INDEX IF NOT EXISTS idx_branches_name ON memory_branches(memory_id, branch_name);

    -- \u2a50\u2a50\u2a50 \u68c0\u7d22\u5ba1\u8ba1\u65e5\u5fd7 \u2a50\u2a50\u2a50
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
    CREATE INDEX IF NOT EXISTS idx_audit_query ON search_audit_log(created_at DESC);
    CREATE INDEX IF NOT EXISTS idx_audit_source ON search_audit_log(source, created_at DESC);

    -- \u2a50\u2a50\u2a50 \u4e0a\u4e0b\u6587\u5378\u8f7d & 4\u5c42\u6e10\u8fdb\u5b58\u50a8 \u2a50\u2a50\u2a50

    -- L0 \u539f\u59cb\u5c42\uff1a\u957f\u6587\u672c\u539f\u59cb\u5b58\u50a8\uff0c\u4f9b\u94bb\u56de\u67e5\u8be2
    CREATE TABLE IF NOT EXISTS raw_memories (
        id              TEXT PRIMARY KEY,
        session_id      TEXT,
        source          TEXT NOT NULL DEFAULT 'unknown',
        content         TEXT NOT NULL,
        token_count     INTEGER NOT NULL DEFAULT 0,
        memory_id       TEXT,
        created_at      TEXT NOT NULL DEFAULT (datetime('now'))
    );
    CREATE INDEX IF NOT EXISTS idx_raw_session ON raw_memories(session_id, created_at DESC);
    CREATE INDEX IF NOT EXISTS idx_raw_memory ON raw_memories(memory_id);

    -- L2 \u573a\u666f\u5c42\uff1a\u591a\u6761\u8bb0\u5fc6\u805a\u5408\u4e3a\u573a\u666f
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
    CREATE INDEX IF NOT EXISTS idx_scenarios_category ON scenarios(category_id, created_at DESC);

    -- L3 \u753b\u50cf\u5c42\uff1a\u7528\u6237/\u9879\u76ee/\u9886\u57df\u753b\u50cf
    CREATE TABLE IF NOT EXISTS personas (
        id              TEXT PRIMARY KEY,
        persona_type    TEXT NOT NULL,
        name            TEXT NOT NULL,
        profile_md      TEXT NOT NULL DEFAULT '',
        version         INTEGER NOT NULL DEFAULT 1,
        created_at      TEXT NOT NULL DEFAULT (datetime('now')),
        updated_at      TEXT NOT NULL DEFAULT (datetime('now'))
    );
    CREATE INDEX IF NOT EXISTS idx_personas_type ON personas(persona_type, name);

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
    CREATE INDEX IF NOT EXISTS idx_skills_category ON skills(category);
    CREATE INDEX IF NOT EXISTS idx_skills_confidence ON skills(confidence DESC);
    CREATE INDEX IF NOT EXISTS idx_skills_name ON skills(name);
    """)

    # Rebuild FTS5 from existing data after migration
    if needs_fts_rebuild:
        try:
            db.execute("""
                INSERT INTO memories_fts(rowid, content, category_id, tags, type, scope, source)
                SELECT rowid, content, COALESCE(category_id,'general'), tags, type, scope, source
                FROM memories WHERE archived = 0
            """)
            db.commit()
            log.info("FTS5 rebuilt from existing memories")
        except Exception as e:
            log.warning(f"FTS5 rebuild failed: {e}", exc_info=True)

    # ═══ 版本迁移：为现有记忆创建初始版本 ═══
    try:
        version_count = db.execute(
            "SELECT COUNT(*) FROM memory_versions"
        ).fetchone()[0]
        memory_count = db.execute(
            "SELECT COUNT(*) FROM memories WHERE archived=0"
        ).fetchone()[0]

        if version_count == 0 and memory_count > 0:
            log.info(
                f"Migrating: creating initial versions for {memory_count} existing memories..."
            )
            import uuid as _uuid
            # (lazy import to avoid circular dependency with server.py)
            from memory_gateway.utils.crypto import compute_checksum

            # 批量获取所有记忆
            rows = db.execute(
                "SELECT id, content, source, type, category_id, priority, created_at "
                "FROM memories WHERE archived=0"
            ).fetchall()

            batch_size = 500
            for i in range(0, len(rows), batch_size):
                batch = rows[i:i + batch_size]
                version_values = []
                evolution_values = []

                for r in batch:
                    version_id = str(_uuid.uuid4())
                    content_hash = compute_checksum(r["content"])
                    metadata = json.dumps({
                        "type": r["type"],
                        "category": r["category_id"],
                        "priority": r["priority"]
                    }, ensure_ascii=False)

                    version_values.append((
                        version_id, r["id"], 1, r["content"], content_hash,
                        None, "create", r["source"] or "system",
                        "Initial version (migration)", metadata, r["created_at"]
                    ))

                    evolution_values.append((
                        r["id"], "create", 0, 1, r["source"] or "system",
                        json.dumps(
                            {"reason": "Migration: initial version"},
                            ensure_ascii=False,
                        ),
                        r["created_at"]
                    ))

                db.executemany(
                    """INSERT OR IGNORE INTO memory_versions
                       (id, memory_id, version, content, content_hash, diff_from_prev,
                        change_type, changed_by, change_reason, metadata_snapshot, created_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    version_values
                )

                db.executemany(
                    """INSERT INTO evolution_log
                       (memory_id, event_type, from_version, to_version, agent, details, created_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?)""",
                    evolution_values
                )

                db.commit()
                log.info(
                    f"  Migrated {min(i + batch_size, len(rows))}/{len(rows)} memories..."
                )

            log.info(
                f"Version migration complete: {len(rows)} memories now have version history"
            )
        elif version_count > 0:
            log.info(
                f"Version tracking active: {version_count} versions for {memory_count} memories"
            )
    except Exception as e:
        log.warning(f"Version migration failed (non-fatal): {e}", exc_info=True)

    # Schema: login attempts table
    db.execute("""
        CREATE TABLE IF NOT EXISTS login_attempts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ip_address TEXT NOT NULL,
            success INTEGER NOT NULL DEFAULT 0,
            attempted_at TEXT NOT NULL DEFAULT (datetime('now')),
            user_agent TEXT DEFAULT ''
        )
    """)
    db.execute("""
        CREATE INDEX IF NOT EXISTS idx_login_attempts_ip
        ON login_attempts(ip_address, attempted_at)
    """)

    # 会话持久化表
    db.execute("""
        CREATE TABLE IF NOT EXISTS user_sessions (
            token TEXT PRIMARY KEY,
            ip_address TEXT NOT NULL,
            created_at REAL NOT NULL,
            expires_at REAL NOT NULL
        )
    """)
    db.execute("""
        CREATE INDEX IF NOT EXISTS idx_sessions_expires ON user_sessions(expires_at)
    """)

    # IP锁定表
    db.execute("""
        CREATE TABLE IF NOT EXISTS ip_lockouts (
            ip_address TEXT PRIMARY KEY,
            unlock_time REAL NOT NULL,
            failure_count INTEGER NOT NULL DEFAULT 0
        )
    """)

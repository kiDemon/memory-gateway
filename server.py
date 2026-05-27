#!/usr/bin/env python3
"""
MCP Memory Server v4 — Hermes + Claude Code + WorkBuddy 统一记忆系统
部署: 阿里云 1Panel / Docker / 本地
协议: MCP over HTTP (StreamableHTTP)
存储: SQLite + FTS5
"""

import hashlib
import json
import logging
import os
import difflib
import re
import sqlite3
import sys
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

# ── Logging ──────────────────────────────────────────────

LOG_LEVEL = os.environ.get("MEMORY_LOG_LEVEL", "INFO")
logging.basicConfig(
    level=getattr(logging, LOG_LEVEL),
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger("memory-server")

# ── Config ───────────────────────────────────────────────

DATA_DIR = Path(os.environ.get("MEMORY_DATA_DIR", "/home/kidemon/.hermes/memory-gateway/data"))
DATA_DIR.mkdir(parents=True, exist_ok=True)
DB_PATH = DATA_DIR / "memory.db"
KEY_FILE = DATA_DIR / ".api_key"

# ── Database ─────────────────────────────────────────────


def init_db(db: sqlite3.Connection) -> None:
    """Initialize schema with categories, FTS5, triggers, and indexes."""
    # Schema migration: add category_id to old memories table if missing
    cursor = db.execute("PRAGMA table_info(memories)")
    columns = {row[1] for row in cursor.fetchall()}
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
        rows = db.execute("SELECT id, content FROM memories WHERE simhash='' OR simhash IS NULL").fetchall()
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
                db.execute(f"ALTER TABLE memories ADD COLUMN {col_name} TEXT DEFAULT '{col_default}'")
                db.commit()

    # Schema migration: rebuild FTS5 if old schema (no category_id)
    try:
        fts_info = db.execute("SELECT sql FROM sqlite_master WHERE name='memories_fts'").fetchone()
        if fts_info and 'category_id' not in fts_info[0]:
            log.info("Migrating: rebuilding FTS5 table with category_id")
            db.executescript("""
                DROP TRIGGER IF EXISTS memories_ai;
                DROP TRIGGER IF EXISTS memories_ad;
                DROP TRIGGER IF EXISTS memories_au;
                DROP TABLE IF EXISTS memories_fts;
            """)
            db.commit()
            needs_fts_rebuild = True
    except Exception:
        pass
    needs_fts_rebuild = locals().get('needs_fts_rebuild', False)

    db.executescript("""
    PRAGMA journal_mode=WAL;
    PRAGMA foreign_keys=ON;
    PRAGMA busy_timeout=5000;

    -- 分类树
    CREATE TABLE IF NOT EXISTS categories (
        id          TEXT PRIMARY KEY,
        name        TEXT NOT NULL,
        parent_id   TEXT REFERENCES categories(id),
        icon        TEXT DEFAULT '📁',
        sort_order  INTEGER DEFAULT 0,
        created_at  TEXT NOT NULL DEFAULT (datetime('now'))
    );

    -- 预置分类（如果不存在）
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
        simhash     TEXT DEFAULT ''
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
        source_id TEXT NOT NULL REFERENCES memories(id) ON DELETE CASCADE,
        target_id TEXT NOT NULL REFERENCES memories(id) ON DELETE CASCADE,
        relation  TEXT NOT NULL DEFAULT 'related_to',
        strength  REAL DEFAULT 1.0,
        created_at TEXT NOT NULL DEFAULT (datetime('now')),
        PRIMARY KEY (source_id, target_id)
    );

    -- FTS5 全文索引
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
    CREATE INDEX IF NOT EXISTS idx_session_session ON session_memories(session_id);
    CREATE INDEX IF NOT EXISTS idx_relations_source ON memory_relations(source_id);
    CREATE INDEX IF NOT EXISTS idx_relations_target ON memory_relations(target_id);

    -- ═══ 记忆版本控制 (Git for Memory) ═══

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

    -- ═══ 记忆分支 (Git-like Branching) ═══
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

    -- ═══ 检索审计日志 ═══
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

    -- ═══ 上下文卸载 & 4层渐进存储 ═══

    -- L0 原始层：长文本原始存储，供钻回查询
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

    -- L2 场景层：多条记忆聚合为场景
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
    CREATE INDEX IF NOT EXISTS idx_personas_type ON personas(persona_type, name);
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
            log.warning(f"FTS5 rebuild failed: {e}")

    # ═══ 版本迁移：为现有记忆创建初始版本 ═══
    try:
        version_count = db.execute("SELECT COUNT(*) FROM memory_versions").fetchone()[0]
        memory_count = db.execute("SELECT COUNT(*) FROM memories WHERE archived=0").fetchone()[0]

        if version_count == 0 and memory_count > 0:
            log.info(f"Migrating: creating initial versions for {memory_count} existing memories...")
            import uuid as _uuid

            # 批量获取所有记忆
            rows = db.execute(
                "SELECT id, content, source, type, category_id, priority, created_at FROM memories WHERE archived=0"
            ).fetchall()

            batch_size = 500
            for i in range(0, len(rows), batch_size):
                batch = rows[i:i+batch_size]
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
                        json.dumps({"reason": "Migration: initial version"}, ensure_ascii=False),
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
                log.info(f"  Migrated {min(i+batch_size, len(rows))}/{len(rows)} memories...")

            log.info(f"Version migration complete: {len(rows)} memories now have version history")
        elif version_count > 0:
            log.info(f"Version tracking active: {version_count} versions for {memory_count} memories")
    except Exception as e:
        log.warning(f"Version migration failed (non-fatal): {e}")


def get_db() -> sqlite3.Connection:
    db = sqlite3.connect(str(DB_PATH), timeout=10)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("PRAGMA foreign_keys=ON")
    db.execute("PRAGMA busy_timeout=5000")
    return db


@contextmanager
def db_conn() -> sqlite3.Connection:
    conn = get_db()
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


# ── Helpers ──────────────────────────────────────────────

CONTENT_TYPE_KEYWORDS = {
    "decision": ["decided", "decision", "choose", "decided on", "conclusion", "结论", "决定", "选择", "方案"],
    "debugging": ["bug", "fix", "error", "debug", "root cause", "bug", "修复", "错误", "排查", "原因"],
    "progress": ["completed", "done", "finished", "phase", "step", "done", "完成", "阶段", "步骤"],
    "feature": ["feature", "implement", "add", "create", "new", "功能", "实现", "新增", "创建"],
    "rule": ["rule", "convention", "standard", "policy", "must", "always", "规则", "规范", "必须", "始终"],
    "learning": ["learned", "discovered", "realized", "insight", "learned", "发现", "洞察", "原来", "意识到"],
    "preference": ["prefer", "like", "favorite", "use", "instead", "偏好", "喜欢", "用"],
    "context": ["project", "architecture", "stack", "framework", "project", "项目", "架构", "技术栈"],
    "reference": ["link", "url", "doc", "api", "reference", "文档", "链接", "参考"],
    "convention": ["naming", "format", "style", "indent", "naming", "命名", "格式", "风格"],
    "procedural": ["流程", "步骤", "标准操作", "checklist", "工作流", "workflow", "procedure", "step by step", "标准作业", "sop", "protocol"],
}

# ── Privacy Filter ─────────────────────────────────────────

# Patterns to redact before saving: API keys, tokens, passwords
PRIVACY_PATTERNS = [
    (re.compile(r'(?i)(api[_-]?key|apikey)\s*[:=]\s*["\']?[A-Za-z0-9_\-]{16,}'), r'\1 [REDACTED]'),
    (re.compile(r'(?i)(secret|password|token)\s*[:=]\s*["\']?[A-Za-z0-9_\-\.]{8,}'), r'\1 [REDACTED]'),
    (re.compile(r'(?i)(bearer)\s+[A-Za-z0-9_\-\.]{20,}'), 'Bearer [REDACTED]'),
    (re.compile(r'(sk-[a-zA-Z0-9]{20,})'), '[API-KEY-REDACTED]'),
]


def _filter_sensitive(content: str) -> str:
    """Strip sensitive information from content before saving."""
    result = content
    for pattern, replacement in PRIVACY_PATTERNS:
        before = result
        result = pattern.sub(replacement, result)
        if result != before:
            log.debug("Privacy filter redacted content")
    return result


# ── Confidence Score ──────────────────────────────────────

# Default confidence by type (higher = more durable)
TYPE_CONFIDENCE = {
    "procedural": 0.95,   # SOPs, workflows → most trusted
    "rule": 0.90,
    "convention": 0.90,
    "decision": 0.85,
    "preference": 0.85,
    "learning": 0.80,
    "context": 0.80,
    "reference": 0.75,
    "feature": 0.70,
    "progress": 0.65,
    "debugging": 0.60,
    "general": 0.80,
}

# Source trust bonus
SOURCE_CONFIDENCE_BONUS = {
    "hermes": 0.05,
    "claude": 0.05,
    "workbuddy": 0.03,
    "system": 0.10,
}


def _compute_confidence(mem_type: str, source: str, content_length: int) -> float:
    """Compute initial confidence score for a new memory.
    
    Factors:
    - Memory type (procedural/rule → higher)
    - Source trustworthiness
    - Content length (very short = less reliable)
    """
    base = TYPE_CONFIDENCE.get(mem_type, 0.80)
    base += SOURCE_CONFIDENCE_BONUS.get(source, 0.0)
    # Length bonus: 50+ chars → full trust, shorter → scaled down
    if content_length < 20:
        base -= 0.15
    elif content_length < 50:
        base -= 0.05
    # Clip to [0.3, 1.0]
    return max(0.3, min(1.0, base))



def compute_checksum(content: str) -> str:
    return hashlib.sha256(content.strip().encode()).hexdigest()[:16]


def compute_simhash(content: str, hashbits: int = 64) -> str:
    """Compute SimHash fingerprint for fuzzy dedup.

    SimHash produces similar hashes for similar content.
    Hamming distance < 10 means ~80%+ similarity.
    """
    tokens = re.findall(r'[\w\u4e00-\u9fff]+', content.lower())
    if not tokens:
        return "0" * (hashbits // 4)
    # Use shingle of 3 tokens
    v = [0] * hashbits
    for i in range(len(tokens) - 2):
        shingle = tokens[i] + tokens[i+1] + tokens[i+2]
        h = int(hashlib.md5(shingle.encode()).hexdigest(), 16)
        for bit in range(hashbits):
            if h & (1 << bit):
                v[bit] += 1
            else:
                v[bit] -= 1
    fingerprint = 0
    for bit in range(hashbits):
        if v[bit] > 0:
            fingerprint |= (1 << bit)
    return format(fingerprint, f'0{hashbits // 4}x')


def hamming_distance(hash1: str, hash2: str) -> int:
    """Compute Hamming distance between two hex hash strings."""
    if not hash1 or not hash2:
        return 64
    try:
        x = int(hash1, 16) ^ int(hash2, 16)
        return bin(x).count('1')
    except (ValueError, TypeError):
        return 64


def _find_near_duplicate(db: sqlite3.Connection, simhash: str, threshold: int = 10) -> Optional[dict]:
    """Check if a simhash has a near-duplicate in the memories table.

    Returns dict with 'id', 'content', 'simhash', 'distance', 'similarity' if found, None otherwise.
    DRY helper used by mem_save, mem_batch_save, and batch_check endpoints.
    """
    similar = db.execute(
        "SELECT id, content, simhash FROM memories WHERE archived=0 AND simhash != '' LIMIT 1000"
    ).fetchall()
    for r in similar:
        if r["simhash"] and hamming_distance(simhash, r["simhash"]) < threshold:
            return {
                "id": r["id"],
                "content": r.get("content", ""),
                "simhash": r["simhash"],
                "distance": hamming_distance(simhash, r["simhash"]),
                "similarity": round(1.0 - hamming_distance(simhash, r["simhash"]) / 64, 3),
            }
    return None


def _build_timeline(db: sqlite3.Connection, days: int = 30) -> list[dict]:
    """Build a date-by-count timeline for the last N days.

    Returns list of {date: "MM-DD", count: N} ordered ascending.
    DRY helper used by dashboard_overview and _get_stats endpoints.
    """
    timeline = []
    for i in range(days - 1, -1, -1):
        d = datetime.now(timezone.utc) - timedelta(days=i)
        date_str = d.strftime("%Y-%m-%d")
        cnt = db.execute(
            "SELECT COUNT(*) FROM memories WHERE created_at LIKE ?", (date_str + "%",)
        ).fetchone()[0]
        timeline.append({"date": d.strftime("%m-%d"), "count": cnt})
    return timeline


# ══════════════════════════════════════════════════════════
# V5 Optimizations: HotCache, Embedding, Decay, Audit, VClock
# Knowledge Graph Auto-Extraction
# ══════════════════════════════════════════════════════════

# ── Knowledge Graph: Auto-extract key terms & co-occurrence ──

# Stop words: common terms that should NOT become graph nodes
_STOP_WORDS = {
    "的", "了", "是", "在", "有", "和", "不", "也", "人", "这", "中",
    "大", "为", "上", "个", "就", "到", "说", "要", "对", "会", "从",
    "the", "a", "an", "is", "are", "was", "were", "be", "been", "being",
    "have", "has", "had", "do", "does", "did", "will", "would", "could",
    "should", "may", "might", "can", "this", "that", "these", "those",
    "and", "but", "or", "not", "no", "nor", "so", "yet", "both", "either",
    "neither", "each", "every", "all", "any", "few", "more", "most", "other",
    "some", "such", "than", "too", "very", "just", "because", "as", "until",
    "while", "of", "at", "by", "for", "with", "about", "against", "between",
    "through", "during", "before", "after", "above", "below", "to", "from",
    "up", "down", "in", "out", "on", "off", "over", "under", "again",
    "then", "once", "here", "there", "when", "where", "why", "how", "what",
    "which", "who", "whom", "whose", "if", "it", "its", "they", "them",
    "their", "he", "she", "him", "her", "his", "we", "us", "our", "you",
    "your", "my", "me", "i", "am",
    "已经", "可以", "没有", "我们", "自己", "什么", "这个", "那个",
    "一下", "一些", "一样", "不是", "还是", "而且", "因为", "所以",
    "但是", "如果", "虽然", "只是", "或者", "以及", "然后", "之后",
    "之前", "需要", "通过", "进行", "使用", "支持", "包含", "提供",
    "支持", "能够", "可能", "应该", "必须", "一个", "一种", "一条",
}


def _extract_key_terms(content: str) -> list[str]:
    """Extract key terms from content for knowledge graph nodes.

    Strategy (zero-dependency, no LLM):
    1. Chinese: extract 2-4 char noun-like substrings via regex
    2. English: extract capitalized words and technical terms
    3. Filter stop words and very short tokens
    Returns up to 12 unique key terms.
    """
    terms = set()

    # Chinese: extract sequences of 2-4 Chinese characters (likely nouns/phrases)
    cn_matches = re.findall(r'[\u4e00-\u9fff]{2,4}', content)
    for m in cn_matches:
        if m not in _STOP_WORDS and len(m) >= 2:
            terms.add(m)

    # English: extract words 3+ chars, prefer capitalized and technical terms
    en_matches = re.findall(r'[A-Za-z_][A-Za-z0-9_]{2,}', content)
    for m in en_matches:
        lower = m.lower()
        if lower not in _STOP_WORDS and len(m) >= 3:
            # Keep original case for proper nouns, lowercase for common terms
            terms.add(m if m[0].isupper() else lower)

    # Deduplicate by lowercase, keep the most "interesting" variant
    seen = {}
    for t in terms:
        key = t.lower()
        if key not in seen or (t[0].isupper() and not seen[key][0].isupper()):
            seen[key] = t

    return list(seen.values())[:12]


def _auto_create_relations(db: sqlite3.Connection, memory_id: str,
                           content: str, category_id: str) -> int:
    """Auto-create knowledge graph edges from co-occurring key terms.

    For each pair of key terms in the same memory, create/upsert a relation
    with strength incremented. Returns number of relations created/updated.
    """
    terms = _extract_key_terms(content)
    if len(terms) < 2:
        return 0

    now = now_iso()
    relations_created = 0

    # Create relations between all pairs (max C(12,2)=66 pairs)
    for i in range(len(terms)):
        for j in range(i + 1, len(terms)):
            src = terms[i]
            tgt = terms[j]
            # Use term text as pseudo-IDs in relations table
            # relation type derived from category context
            relation_type = f"co_occurs_{category_id}"

            # Upsert: increment strength if relation already exists
            existing = db.execute(
                "SELECT strength FROM memory_relations WHERE source_id=? AND target_id=?",
                (src, tgt)
            ).fetchone()

            if existing:
                new_strength = min(10.0, existing["strength"] + 0.1)
                db.execute(
                    "UPDATE memory_relations SET strength=?, created_at=? WHERE source_id=? AND target_id=?",
                    (new_strength, now, src, tgt)
                )
            else:
                db.execute(
                    """INSERT OR IGNORE INTO memory_relations
                       (source_id, target_id, relation, strength, created_at)
                       VALUES (?, ?, ?, ?, ?)""",
                    (src, tgt, relation_type, 1.0, now)
                )
            relations_created += 1

    if relations_created > 0:
        log.debug(f"Auto-created {relations_created} graph edges for memory {memory_id[:8]}")
    return relations_created


def _get_related_terms(db: sqlite3.Connection, term: str, limit: int = 10) -> list[dict]:
    """Get terms related to a given term via knowledge graph."""
    rows = db.execute(
        """SELECT target_id as related, relation, strength
           FROM memory_relations WHERE source_id=?
           UNION ALL
           SELECT source_id as related, relation, strength
           FROM memory_relations WHERE target_id=?
           ORDER BY strength DESC LIMIT ?""",
        (term, term, limit)
    ).fetchall()
    return [dict(r) for r in rows]

# ── Hot Cache (LRU in-memory) ────────────────────────────

from collections import OrderedDict
import threading
import struct
import math

HOT_CACHE_MAX = int(os.environ.get("MEMORY_HOT_CACHE_SIZE", "200"))
HOT_CACHE_TTL = int(os.environ.get("MEMORY_HOT_CACHE_TTL", "300"))  # seconds


from typing import Any as _Any

class HotCache:
    """Thread-safe LRU cache for frequently accessed memories.

    Auto-promotes P0 + high-recall-count memories to hot tier in DB.
    """

    def __init__(self, max_size: int = HOT_CACHE_MAX, ttl: int = HOT_CACHE_TTL):
        self._cache: OrderedDict[str, tuple[_Any, float]] = OrderedDict()
        self._max = max_size
        self._ttl = ttl
        self._lock = threading.Lock()

    def get(self, key: str) -> _Any | None:
        with self._lock:
            if key not in self._cache:
                return None
            entry, ts = self._cache[key]
            if time.time() - ts > self._ttl:
                del self._cache[key]
                return None
            self._cache.move_to_end(key)
            return entry

    def put(self, key: str, value: _Any) -> None:
        with self._lock:
            if key in self._cache:
                self._cache.move_to_end(key)
            self._cache[key] = (value, time.time())
            while len(self._cache) > self._max:
                self._cache.popitem(last=False)

    def invalidate(self, key: str) -> None:
        with self._lock:
            self._cache.pop(key, None)

    def clear(self) -> None:
        with self._lock:
            self._cache.clear()

    @property
    def size(self) -> int:
        with self._lock:
            return len(self._cache)


hot_cache = HotCache()


def _sync_hot_tier_from_cache(db: sqlite3.Connection) -> None:
    """Periodically sync hot tier status: P0 + frequently recalled → hot_tier=1."""
    db.execute("""
        UPDATE memories SET hot_tier=1
        WHERE archived=0 AND (priority='P0' OR recall_count >= 3)
        AND hot_tier=0
    """)
    # Demote inactive hot items
    db.execute("""
        UPDATE memories SET hot_tier=0
        WHERE hot_tier=1 AND recall_count=0
        AND last_recalled IS NULL
        AND priority != 'P0'
    """)


# ── Embedding (optional, sentence-transformers) ──────────

_embed_model = None
EMBEDDING_DIM = int(os.environ.get("MEMORY_EMBEDDING_DIM", "384"))


def _get_embed_model():
    """Lazy-load the embedding model (all-MiniLM-L6-v2, 384-dim, ~80MB)."""
    global _embed_model
    if _embed_model is not None:
        return _embed_model
    try:
        from sentence_transformers import SentenceTransformer
        model_name = os.environ.get("MEMORY_EMBEDDING_MODEL", "all-MiniLM-L6-v2")
        _embed_model = SentenceTransformer(model_name)
        log.info(f"Embedding model loaded: {model_name} (dim={_embed_model.get_sentence_embedding_dimension()})")
        return _embed_model
    except ImportError:
        log.warning("sentence-transformers not installed — embedding search disabled")
        return None
    except Exception as e:
        log.warning(f"Failed to load embedding model: {e}")
        return None


def _blob_to_vector(blob: bytes) -> list[float] | None:
    """Decode BLOB to float list."""
    if not blob:
        return None
    try:
        n = len(blob) // 4
        return list(struct.unpack(f'{n}f', blob))
    except struct.error:
        return None


def _vector_to_blob(vec: list[float]) -> bytes:
    """Encode float list to BLOB."""
    return struct.pack(f'{len(vec)}f', *vec)


def _cosine_similarity(a: list[float], b: list[float]) -> float:
    """Compute cosine similarity between two vectors."""
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    norm_a = math.sqrt(sum(x * x for x in a))
    norm_b = math.sqrt(sum(x * x for x in b))
    if norm_a == 0 or norm_b == 0:
        return 0.0
    return dot / (norm_a * norm_b)


def _compute_embedding(content: str) -> bytes | None:
    """Compute embedding for content. Returns BLOB or None if model unavailable."""
    model = _get_embed_model()
    if model is None:
        return None
    try:
        vec = model.encode(content, normalize_embeddings=True).tolist()
    except Exception as e:
        log.warning(f"Embedding computation failed: {e}")
        return None
    return _vector_to_blob(vec)


def _hybrid_search(db: sqlite3.Connection, query: str, query_embedding: bytes | None,
                   fts_results: list[sqlite3.Row], limit: int,
                   semantic_weight: float = 0.4) -> list[dict]:
    """Merge FTS5 results with semantic similarity using RRF fusion.

    If query_embedding is None (no embedding model), returns FTS results as-is.
    Incorporates confidence as a final weighting factor.
    """
    if query_embedding is None or not fts_results:
        return [row_to_dict(r) for r in fts_results]

    query_vec = _blob_to_vector(query_embedding)
    if query_vec is None:
        return [row_to_dict(r) for r in fts_results]

    K = 60  # RRF constant

    # Compute semantic scores for all results
    sem_scores = {}
    for i, r in enumerate(fts_results):
        mem_vec = _blob_to_vector(r["embedding"]) if r["embedding"] else None
        if mem_vec:
            sem_scores[i] = _cosine_similarity(query_vec, mem_vec)

    # RRF: combine FTS rank and semantic rank
    scored = []
    for i, r in enumerate(fts_results):
        d = row_to_dict(r)

        fts_rank = i + 1  # 1-indexed FTS rank

        # Semantic rank: position by descending semantic score
        mem_vec = _blob_to_vector(r["embedding"]) if r["embedding"] else None
        if mem_vec and sem_scores:
            my_sem = sem_scores.get(i, 0.0)
            sem_rank = sum(1 for s in sem_scores.values() if s > my_sem) + 1
        else:
            sem_rank = fts_rank  # no semantic → same as FTS

        # RRF score = sum of reciprocal ranks
        rrf_score = (1.0 / (K + fts_rank)) + (1.0 / (K + sem_rank))

        # Confidence weighting: high-confidence memories rank higher
        confidence = d.get("confidence", 0.8)
        rrf_score *= confidence

        d["_rrf_score"] = round(rrf_score, 5)
        d["_fts_rank"] = fts_rank
        d["_sem_rank"] = sem_rank
        scored.append((rrf_score, d))

    scored.sort(key=lambda x: x[0], reverse=True)
    return [d for _, d in scored[:limit]]


# ── Memory Decay & Auto-Cleanup ──────────────────────────

# Default TTLs per priority/memory-type
DEFAULT_TTL = {
    "P0": 0,           # never expire
    "procedural": 0,   # SOPs, workflows → never expire (same as P0)
    "P1": 180,         # 6 months
    "P2": 60,          # 2 months
}

# Minimum recall count to resist decay
DECAY_THRESHOLD = 2


def _apply_decay(db: sqlite3.Connection) -> dict:
    """Apply memory decay using Ebbinghaus forgetting curve.

    Memory strength = confidence * 0.5^(days_since_recall / half_life)
    where half_life increases with recall_count (spacing effect).
    Archives memories when strength drops below threshold.

    Returns stats about what was done.
    """
    now = now_iso()
    stats = {"archived": 0, "decayed": 0}

    # Batch: get all non-exempt memories
    rows = db.execute(
        """SELECT id, priority, type, created_at, last_recalled, recall_count, confidence
           FROM memories
           WHERE archived=0 AND priority != 'P0' AND type != 'procedural'"""
    ).fetchall()

    to_archive = []
    for r in rows:
        priority = r["priority"]
        recall_count = r["recall_count"]
        confidence = r["confidence"]

        # Ebbinghaus half-life: P1 starts at 30d, P2 at 14d
        # Logarithmic growth: recall=0→1x, 3→2x, 7→3x, 15→4x (prevents recall>5 making memory immortal)
        half_life = 30 if priority == "P1" else 14
        half_life *= (1 + math.log2(recall_count + 1))

        # Days since last recall (or creation if never recalled)
        last_time = r["last_recalled"] or r["created_at"]
        try:
            last_dt = datetime.fromisoformat(last_time)
            days_since = (datetime.now(timezone.utc) - last_dt).total_seconds() / 86400.0
        except (ValueError, TypeError):
            continue

        # Exponential decay: strength decays as half-life passes
        strength = confidence * (0.5 ** (days_since / half_life))

        if strength < 0.05:  # archival threshold
            to_archive.append(r["id"])

    if to_archive:
        placeholders = ",".join("?" for _ in to_archive)
        db.execute(
            f"UPDATE memories SET archived=1, updated_at=? WHERE id IN ({placeholders})",
            [now] + to_archive,
        )
        stats["archived"] = len(to_archive)

    # 2. Demote hot_tier for memories not recalled recently
    three_months_ago = (datetime.now(timezone.utc) - timedelta(days=90)).isoformat()
    result = db.execute(
        """UPDATE memories SET hot_tier=0
           WHERE hot_tier=1 AND priority!='P0'
           AND (last_recalled IS NULL OR last_recalled < ?)""",
        (three_months_ago,),
    )
    stats["decayed"] = result.rowcount

    if stats["archived"] > 0:
        log.info(f"Ebbinghaus decay: archived {stats['archived']} memories (strength < 0.05)")
    if stats["decayed"] > 0:
        log.info(f"Decay: demoted {stats['decayed']} cold memories from hot tier")

    return stats


# ── Vector Clock Conflict Detection ──────────────────────


def _update_vector_clock(db: sqlite3.Connection, memory_id: str,
                         source: str) -> dict:
    """Update vector clock for a tool and check for conflicts.

    Returns: {"conflict": bool, "clock": dict, "detail": str}
    """
    row = db.execute(
        "SELECT vector_clock, source FROM memories WHERE id=?", (memory_id,)
    ).fetchone()
    if not row:
        return {"conflict": False, "clock": {}, "detail": "Memory not found"}

    try:
        clock = json.loads(row["vector_clock"] or "{}")
    except json.JSONDecodeError:
        clock = {}

    now = now_iso()
    other_sources = {k: v for k, v in clock.items() if k != source}

    # Conflict: another tool modified this memory AFTER our last known timestamp
    conflict = False
    conflict_detail = ""
    our_timestamp = clock.get(source)

    if our_timestamp:
        for tool, ts in other_sources.items():
            if ts > our_timestamp:
                conflict = True
                conflict_detail += f"{tool} modified at {ts} (after your {our_timestamp}); "

    # Update our timestamp
    clock[source] = now
    db.execute(
        "UPDATE memories SET vector_clock=?, updated_at=? WHERE id=?",
        (json.dumps(clock), now, memory_id),
    )

    return {
        "conflict": conflict,
        "clock": clock,
        "detail": conflict_detail.strip() if conflict else "no conflict",
    }


# ══════════════════════════════════════════════════════════


def detect_type(content: str) -> str:
    lower = content.lower()
    scores = {}
    for t, keywords in CONTENT_TYPE_KEYWORDS.items():
        scores[t] = sum(1 for kw in keywords if kw in lower)
    best = max(scores, key=scores.get)
    return best if scores[best] > 0 else "general"


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def row_to_dict(row: sqlite3.Row) -> dict:
    d = dict(row)
    if "tags" in d and isinstance(d["tags"], str):
        try:
            d["tags"] = json.loads(d["tags"])
        except (json.JSONDecodeError, TypeError):
            d["tags"] = []
    # Parse vector_clock if present
    if "vector_clock" in d and isinstance(d["vector_clock"], str):
        try:
            d["vector_clock"] = json.loads(d["vector_clock"])
        except (json.JSONDecodeError, TypeError):
            d["vector_clock"] = {}
    d.pop("embedding", None)  # keep hidden (binary BLOB)
    return d


# ── Version Manager (Git for Memory) ────────────────────


class VersionManager:
    """记忆版本管理器 - 实现 Git for Memory 的核心功能"""

    @staticmethod
    def create_version(db: sqlite3.Connection, memory_id: str, content: str,
                       change_type: str = "create", changed_by: str = "system",
                       change_reason: str = None, metadata: dict = None) -> int:
        """创建新版本快照，返回版本号（原子化版本号分配）

        使用 INSERT ... SELECT MAX(version)+1 原子化分配版本号，
        避免并发场景下 SELECT + INSERT 之间的竞态条件。
        """
        version_id = str(uuid.uuid4())
        content_hash = compute_checksum(content)
        metadata_snapshot = json.dumps(metadata or {}, ensure_ascii=False)
        now = now_iso()

        # 计算与上一个版本的 diff（仅在已有版本时）
        diff_from_prev = None
        prev_row = db.execute(
            "SELECT content FROM memory_versions WHERE memory_id=? ORDER BY version DESC LIMIT 1",
            (memory_id,)
        ).fetchone()
        if prev_row:
            prev_lines = prev_row["content"].splitlines(keepends=True)
            curr_lines = content.splitlines(keepends=True)
            diff = list(difflib.unified_diff(prev_lines, curr_lines, lineterm=''))
            diff_from_prev = "\n".join(diff) if diff else None

        # 原子化分配版本号：用一个 INSERT 同时完成版本号计算和写入
        # 利用 SQLite 的子查询原子性，MAX(version)+1 在 INSERT 时计算
        try:
            db.execute(
                """INSERT INTO memory_versions
                   (id, memory_id, version, content, content_hash, diff_from_prev,
                    change_type, changed_by, change_reason, metadata_snapshot, created_at)
                   SELECT ?, ?, COALESCE(MAX(version), 0) + 1, ?, ?, ?, ?, ?, ?, ?, ?
                   FROM memory_versions WHERE memory_id=?""",
                (version_id, memory_id, content, content_hash,
                 diff_from_prev, change_type, changed_by, change_reason,
                 metadata_snapshot, now, memory_id)
            )
        except sqlite3.IntegrityError:
            # 极低概率：并发写入导致 UNIQUE(memory_id, version) 冲突，重试一次
            log.warning(f"Version conflict for {memory_id[:8]}..., retrying...")
            db.execute(
                """INSERT INTO memory_versions
                   (id, memory_id, version, content, content_hash, diff_from_prev,
                    change_type, changed_by, change_reason, metadata_snapshot, created_at)
                   SELECT ?, ?, COALESCE(MAX(version), 0) + 1, ?, ?, ?, ?, ?, ?, ?, ?
                   FROM memory_versions WHERE memory_id=?""",
                (str(uuid.uuid4()), memory_id, content, content_hash,
                 diff_from_prev, change_type, changed_by, change_reason,
                 metadata_snapshot, now, memory_id)
            )

        # 获取实际分配的版本号
        row = db.execute(
            "SELECT version FROM memory_versions WHERE id=?", (version_id,)
        ).fetchone()
        new_version = row["version"] if row else 1

        # 记录进化日志
        db.execute(
            """INSERT INTO evolution_log
               (memory_id, event_type, from_version, to_version, agent, details)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (memory_id, change_type, new_version - 1, new_version, changed_by,
             json.dumps({"reason": change_reason, "content_hash": content_hash}, ensure_ascii=False))
        )

        log.info(f"Version {new_version} created for memory {memory_id[:8]}... ({change_type})")

        # Auto-detect bad evolution after version creation
        try:
            bad_result = VersionManager.detect_bad_evolution(db, memory_id, new_version)
            if bad_result.get("is_bad"):
                log.warning(
                    f"Bad evolution detected for memory {memory_id[:8]}... v{new_version}: "
                    f"{bad_result.get('reasons', [])}"
                )
                # Record warning in evolution_log
                db.execute(
                    """INSERT INTO evolution_log
                       (memory_id, event_type, from_version, to_version, agent, details)
                       VALUES (?, 'bad_evolution_warning', ?, ?, ?, ?)""",
                    (memory_id, new_version - 1, new_version, changed_by,
                     json.dumps(bad_result, ensure_ascii=False))
                )
        except Exception as e:
            log.debug(f"Bad evolution check skipped: {e}")

        return new_version

    @staticmethod
    def get_history(db: sqlite3.Connection, memory_id: str, limit: int = 50) -> list[dict]:
        """获取记忆的版本历史"""
        rows = db.execute(
            """SELECT id, version, content, content_hash, change_type, changed_by,
                      change_reason, created_at
               FROM memory_versions
               WHERE memory_id=?
               ORDER BY version DESC
               LIMIT ?""",
            (memory_id, limit)
        ).fetchall()
        return [dict(r) for r in rows]

    @staticmethod
    def get_version(db: sqlite3.Connection, memory_id: str, version: int) -> Optional[dict]:
        """获取指定版本"""
        row = db.execute(
            """SELECT * FROM memory_versions
               WHERE memory_id=? AND version=?""",
            (memory_id, version)
        ).fetchone()
        return dict(row) if row else None

    @staticmethod
    def get_diff(db: sqlite3.Connection, memory_id: str,
                 version_a: int, version_b: int) -> dict:
        """获取两个版本之间的 diff"""
        va = VersionManager.get_version(db, memory_id, version_a)
        vb = VersionManager.get_version(db, memory_id, version_b)

        if not va or not vb:
            return {"error": "Version not found"}

        lines_a = va["content"].splitlines(keepends=True)
        lines_b = vb["content"].splitlines(keepends=True)
        diff = list(difflib.unified_diff(lines_a, lines_b,
                                          fromfile=f"v{version_a}", tofile=f"v{version_b}",
                                          lineterm=''))

        return {
            "memory_id": memory_id,
            "version_a": version_a,
            "version_b": version_b,
            "diff": "\n".join(diff) if diff else "(no changes)",
            "hash_a": va["content_hash"],
            "hash_b": vb["content_hash"],
        }

    @staticmethod
    def rollback(db: sqlite3.Connection, memory_id: str,
                 target_version: int, agent: str = "system") -> dict:
        """回滚到指定版本"""
        target = VersionManager.get_version(db, memory_id, target_version)
        if not target:
            return {"error": f"Version {target_version} not found"}

        # 获取当前记忆信息
        current = db.execute(
            "SELECT * FROM memories WHERE id=?", (memory_id,)
        ).fetchone()
        if not current:
            return {"error": "Memory not found"}

        # 获取当前最新版本号（从 memory_versions 表，而非 memories 表）
        current_ver_row = db.execute(
            "SELECT MAX(version) as v FROM memory_versions WHERE memory_id=?",
            (memory_id,)
        ).fetchone()
        from_version = current_ver_row["v"] or 0

        # 更新记忆内容为历史版本
        now = now_iso()
        db.execute(
            """UPDATE memories
               SET content=?, checksum=?, simhash=?, updated_at=?
               WHERE id=?""",
            (target["content"], target["content_hash"],
             compute_simhash(target["content"]), now, memory_id)
        )

        # 创建新版本记录（标记为 rollback，create_version 内部会自动记录 evolution_log）
        new_ver = VersionManager.create_version(
            db, memory_id, target["content"],
            change_type="rollback",
            changed_by=agent,
            change_reason=f"Rollback from v{from_version} to v{target_version}"
        )

        log.info(f"Memory {memory_id[:8]}... rolled back from v{from_version} to v{target_version}")
        return {
            "success": True,
            "action": "rollback",
            "memory_id": memory_id,
            "from_version": from_version,
            "target_version": target_version,
            "new_version": new_ver
        }

    # ═══ Git for Memory: Branching & Multi-Agent Coordination ═══

    @staticmethod
    def create_branch(db: sqlite3.Connection, memory_id: str,
                      branch_name: str, from_version: int = None,
                      source: str = "system") -> dict:
        """Create a named branch pointing to a specific version.

        If from_version is None, branch from the latest version.
        """
        # Check memory exists
        mem = db.execute("SELECT id FROM memories WHERE id=?", (memory_id,)).fetchone()
        if not mem:
            return {"error": "Memory not found"}

        # Resolve from_version
        if from_version is None:
            row = db.execute(
                "SELECT MAX(version) as max_ver FROM memory_versions WHERE memory_id=?",
                (memory_id,)
            ).fetchone()
            from_version = row["max_ver"] or 1

        # Check version exists
        ver = db.execute(
            "SELECT id FROM memory_versions WHERE memory_id=? AND version=?",
            (memory_id, from_version)
        ).fetchone()
        if not ver:
            return {"error": f"Version {from_version} not found"}

        # Check branch name uniqueness
        existing = db.execute(
            "SELECT id FROM memory_branches WHERE memory_id=? AND branch_name=?",
            (memory_id, branch_name)
        ).fetchone()
        if existing:
            return {"error": f"Branch '{branch_name}' already exists"}

        branch_id = str(uuid.uuid4())
        now = now_iso()
        db.execute(
            """INSERT INTO memory_branches (id, memory_id, branch_name, version, source, created_at)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (branch_id, memory_id, branch_name, from_version, source, now)
        )

        # Log branch creation event
        db.execute(
            """INSERT INTO evolution_log
               (memory_id, event_type, from_version, to_version, agent, details)
               VALUES (?, 'branch_create', ?, ?, ?, ?)""",
            (memory_id, 0, from_version, source,
             json.dumps({"branch_name": branch_name}, ensure_ascii=False))
        )

        log.info(f"Branch '{branch_name}' created for memory {memory_id[:8]}... at v{from_version}")
        return {
            "success": True,
            "action": "branch_create",
            "branch_id": branch_id,
            "memory_id": memory_id,
            "branch_name": branch_name,
            "version": from_version,
        }

    @staticmethod
    def list_branches(db: sqlite3.Connection, memory_id: str) -> list[dict]:
        """List all branches for a memory."""
        rows = db.execute(
            """SELECT id, memory_id, branch_name, version, source, created_at
               FROM memory_branches
               WHERE memory_id=?
               ORDER BY created_at DESC""",
            (memory_id,)
        ).fetchall()
        return [dict(r) for r in rows]

    @staticmethod
    def merge_branch(db: sqlite3.Connection, memory_id: str,
                     source_branch: str, target_branch: str,
                     agent: str = "system") -> dict:
        """Merge source_branch into target_branch.

        Takes the content at source_branch's version, applies it as a new version,
        and updates the target_branch pointer.
        """
        src = db.execute(
            "SELECT * FROM memory_branches WHERE memory_id=? AND branch_name=?",
            (memory_id, source_branch)
        ).fetchone()
        tgt = db.execute(
            "SELECT * FROM memory_branches WHERE memory_id=? AND branch_name=?",
            (memory_id, target_branch)
        ).fetchone()

        if not src:
            return {"error": f"Source branch '{source_branch}' not found"}
        if not tgt:
            return {"error": f"Target branch '{target_branch}' not found"}

        # Get source content
        src_ver = VersionManager.get_version(db, memory_id, src["version"])
        if not src_ver:
            return {"error": f"Source version {src['version']} not found"}

        # Get target current content for diff
        tgt_ver = VersionManager.get_version(db, memory_id, tgt["version"])
        if not tgt_ver:
            return {"error": f"Target version {tgt['version']} not found"}

        # Create new version with merged content (source wins)
        merged_content = src_ver["content"]
        new_ver = VersionManager.create_version(
            db, memory_id, merged_content,
            change_type="merge",
            changed_by=agent,
            change_reason=f"Merge '{source_branch}' into '{target_branch}'"
        )

        # Update target branch pointer
        db.execute(
            "UPDATE memory_branches SET version=?, source=? WHERE id=?",
            (new_ver, agent, tgt["id"])
        )

        # Also update the actual memory content
        now = now_iso()
        db.execute(
            "UPDATE memories SET content=?, checksum=?, simhash=?, updated_at=? WHERE id=?",
            (merged_content, compute_checksum(merged_content),
             compute_simhash(merged_content), now, memory_id)
        )

        # Log merge event
        db.execute(
            """INSERT INTO evolution_log
               (memory_id, event_type, from_version, to_version, agent, details)
               VALUES (?, 'merge', ?, ?, ?, ?)""",
            (memory_id, tgt["version"], new_ver, agent,
             json.dumps({
                 "source_branch": source_branch,
                 "target_branch": target_branch,
                 "source_version": src["version"],
             }, ensure_ascii=False))
        )

        log.info(
            f"Merged '{source_branch}' (v{src['version']}) -> '{target_branch}' "
            f"for memory {memory_id[:8]}... -> v{new_ver}"
        )
        return {
            "success": True,
            "action": "merge",
            "memory_id": memory_id,
            "source_branch": source_branch,
            "target_branch": target_branch,
            "source_version": src["version"],
            "new_version": new_ver,
        }

    @staticmethod
    def detect_bad_evolution(db: sqlite3.Connection, memory_id: str,
                             version: int) -> dict:
        """Detect bad evolution: content catastrophic loss (>80%) or large drift (>30 bits)."""
        curr = db.execute(
            "SELECT content, content_hash FROM memory_versions WHERE memory_id=? AND version=?",
            (memory_id, version)
        ).fetchone()
        if not curr:
            return {"is_bad": False, "reason": "Version not found"}

        prev = db.execute(
            "SELECT content, content_hash FROM memory_versions WHERE memory_id=? AND version=?",
            (memory_id, version - 1)
        ).fetchone()
        if not prev:
            return {"is_bad": False, "reason": "No previous version to compare"}

        reasons = []

        # Check 1: content catastrophic loss > 80%
        prev_len = len(prev["content"])
        curr_len = len(curr["content"])
        if prev_len > 100 and curr_len > 0:  # only check for non-trivial content
            shrink_pct = (prev_len - curr_len) / prev_len * 100
            if shrink_pct > 80:
                reasons.append(f"Content shortened by {shrink_pct:.1f}% ({prev_len}->{curr_len} chars)")

        # Check 2: SimHash drift > 30 bits (content ~53%+ different)
        prev_hash = compute_simhash(prev["content"])
        curr_hash = compute_simhash(curr["content"])
        distance = hamming_distance(prev_hash, curr_hash)
        if distance > 30:
            reasons.append(f"SimHash drifted {distance} bits (threshold: 30)")

        is_bad = len(reasons) > 0
        return {
            "is_bad": is_bad,
            "memory_id": memory_id,
            "version": version,
            "reasons": reasons,
            "shrink_pct": round((prev_len - curr_len) / prev_len * 100, 1) if prev_len > 0 else 0,
            "simhash_distance": distance,
            "prev_simhash": prev_hash,
            "curr_simhash": curr_hash,
        }

    @staticmethod
    def auto_rollback_if_bad(db: sqlite3.Connection, memory_id: str,
                             version: int, agent: str = "system") -> dict:
        """Check for bad evolution and auto-rollback if detected.

        Rolls back to the previous version if bad evolution is confirmed.
        """
        bad_result = VersionManager.detect_bad_evolution(db, memory_id, version)
        if not bad_result.get("is_bad"):
            return {"rolled_back": False, "reason": "Evolution is healthy", **bad_result}

        target_version = version - 1
        rollback_result = VersionManager.rollback(db, memory_id, target_version, agent)

        log.warning(
            f"Auto-rollback for memory {memory_id[:8]}... from v{version} to v{target_version}: "
            f"{bad_result.get('reasons', [])}"
        )

        return {
            "rolled_back": True,
            "reason": "Bad evolution detected",
            "bad_evolution": bad_result,
            "rollback": rollback_result,
        }

# ── Pydantic Models ──────────────────────────────────────


class SaveRequest(BaseModel):
    content: str = Field(..., max_length=100000)
    type: Optional[str] = Field(default=None, pattern=r"^(general|rule|preference|decision|context|learning|reference|convention)$")
    scope: Optional[str] = Field(default="global", pattern=r"^(global|project|agent)$")
    source: Optional[str] = Field(default="unknown", pattern=r"^(hermes|claude|workbuddy|system|unknown)$")
    priority: Optional[str] = Field(default="P1", pattern=r"^(P0|P1|P2)$")
    category_id: Optional[str] = Field(default="general", pattern=r"^[a-z][a-z0-9_]{0,63}$")
    tags: Optional[list[str]] = None
    session_id: Optional[str] = None
    id: Optional[str] = None


class UpdateRequest(BaseModel):
    content: Optional[str] = Field(default=None, max_length=100000)
    type: Optional[str] = Field(default=None, pattern=r"^(general|rule|preference|decision|context|learning|reference|convention)$")
    scope: Optional[str] = Field(default=None, pattern=r"^(global|project|agent)$")
    priority: Optional[str] = Field(default=None, pattern=r"^(P0|P1|P2)$")
    category_id: Optional[str] = Field(default=None, pattern=r"^[a-z][a-z0-9_]{0,63}$")
    tags: Optional[list[str]] = None
    archived: Optional[bool] = None


class SearchRequest(BaseModel):
    q: str
    category_filter: Optional[str] = None
    scope_filter: Optional[str] = None
    source_filter: Optional[str] = None
    type_filter: Optional[str] = None
    limit: int = Field(default=10, ge=1, le=100)
    include_archived: bool = False


class ListRequest(BaseModel):
    since: Optional[str] = None
    category_filter: Optional[str] = None
    scope_filter: Optional[str] = None
    source_filter: Optional[str] = None
    limit: int = Field(default=50, ge=1, le=200)
    include_archived: bool = False


class CategoryRequest(BaseModel):
    id: str
    name: str
    parent_id: Optional[str] = None
    icon: Optional[str] = "📁"
    sort_order: Optional[int] = 0


class CategoryUpdateRequest(BaseModel):
    name: Optional[str] = None
    parent_id: Optional[str] = None
    icon: Optional[str] = None
    sort_order: Optional[int] = None


class SyncHeartbeatRequest(BaseModel):
    tool: str = Field(..., pattern=r"^(hermes|claude|workbuddy|system)$")
    count: int = 0


class RelationRequest(BaseModel):
    source_id: str
    target_id: str
    relation: str = Field(default="related_to", pattern=r"^(related_to|contradicts|supports|duplicates|derived_from)$")
    strength: float = Field(default=1.0, ge=0.0, le=1.0)


class OffloadRequest(BaseModel):
    content: str
    session_id: Optional[str] = None
    source: Optional[str] = "unknown"


class DrilldownRequest(BaseModel):
    memory_id: str


# ── FastAPI App ──────────────────────────────────────────

app = FastAPI(
    title="Memory Gateway",
    version="4.0.0",
    description="MCP Memory Server — Hermes + Claude Code + WorkBuddy",
)

# ── API Key Auth ────────────────────────────────────────

def _generate_api_key() -> str:
    """Generate a cryptographically secure random API key."""
    import secrets
    return "sk-mg-" + secrets.token_urlsafe(36)

def _load_api_key() -> str:
    """Load API key: env var > file > auto-generate and persist.

    Priority:
    1. MEMORY_API_KEY environment variable (highest)
    2. /data/.api_key file on disk
    3. Auto-generate, save to file, print to log (bootstrapping)
    """
    # 1. Environment variable (explicit override)
    env_key = os.environ.get("MEMORY_API_KEY", "").strip()
    if env_key:
        log.info("Using API key from MEMORY_API_KEY environment variable")
        return env_key

    # 2. Persistent key file (survives restarts)
    if KEY_FILE.exists():
        file_key = KEY_FILE.read_text().strip()
        if file_key:
            log.info("Using API key from %s (%s...)", KEY_FILE, file_key[:16])
            return file_key

    # 3. Auto-generate (first run / bootstrap)
    new_key = _generate_api_key()
    KEY_FILE.parent.mkdir(parents=True, exist_ok=True)
    KEY_FILE.write_text(new_key)
    # Restrict permissions so only the server user can read it
    try:
        os.chmod(KEY_FILE, 0o600)
    except Exception:
        pass
    log.warning("=" * 64)
    log.warning("  FIRST RUN — Auto-generated API Key:")
    log.warning("  %s", new_key)
    log.warning("  Saved to: %s", KEY_FILE)
    log.warning("  Check `docker logs memory-gateway` to retrieve it.")
    log.warning("=" * 64)
    return new_key

# ── Runtime API key (loaded at import time) ─────────────

API_KEY = _load_api_key()


# ── Cookie-based session auth ────────────────────────────

COOKIE_NAME = "memory_gateway_session"

def login_page_html(error: str = "") -> str:
    """Return a standalone login page HTML."""
    err_block = f'<div class="error">{error}</div>' if error else ""
    return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head><meta charset="utf-8"><title>Memory Gateway — Login</title>
<style>
  * {{ margin: 0; padding: 0; box-sizing: border-box; }}
  body {{ font-family: -apple-system, 'Segoe UI', sans-serif; min-height: 100vh; display: flex; align-items: center; justify-content: center; background: #0f0f23; color: #e0e0e0; }}
  .card {{ background: #1a1a2e; border-radius: 16px; padding: 48px 40px; width: 420px; max-width: 90vw; box-shadow: 0 20px 60px rgba(0,0,0,0.5); border: 1px solid #2a2a4e; }}
  h1 {{ color: #00d4ff; font-size: 24px; margin-bottom: 8px; }}
  p {{ color: #888; font-size: 14px; margin-bottom: 28px; line-height: 1.6; }}
  .error {{ background: #ff475722; color: #ff4757; border: 1px solid #ff4757; padding: 10px 14px; border-radius: 8px; font-size: 14px; margin-bottom: 20px; }}
  label {{ display: block; font-size: 13px; color: #aaa; margin-bottom: 6px; font-weight: 600; }}
  input[type="password"] {{ width: 100%; padding: 12px 16px; background: #0d1117; color: #e0e0e0; border: 1px solid #333; border-radius: 8px; font-size: 14px; font-family: monospace; }}
  input[type="password"]:focus {{ outline: none; border-color: #00d4ff; }}
  .hint {{ font-size: 12px; color: #666; margin-top: 6px; margin-bottom: 24px; }}
  button {{ width: 100%; padding: 12px; background: #00d4ff; color: #0f0f23; border: none; border-radius: 8px; font-size: 15px; font-weight: bold; cursor: pointer; transition: opacity 0.2s; }}
  button:hover {{ opacity: 0.9; }}
  button:disabled {{ opacity: 0.5; cursor: not-allowed; }}
  .footer {{ margin-top: 24px; text-align: center; font-size: 12px; color: #555; }}
  .loader {{ display: none; width: 16px; height: 16px; border: 2px solid #0f0f23; border-top-color: transparent; border-radius: 50%; animation: spin 0.6s linear infinite; margin: 0 auto; }}
  @keyframes spin {{ to {{ transform: rotate(360deg); }} }}
</style></head>
<body>
<div class="card">
  <h1>Memory Gateway v4</h1>
  <p>输入 API Key 登录管理面板。<br>首次运行请查看 <code>docker logs memory-gateway</code> 获取自动生成的密钥。</p>
  {err_block}
  <form id="loginForm" onsubmit="login(event)">
    <label for="key">API Key</label>
    <input type="password" id="key" placeholder="sk-mg-..." autofocus required>
    <div class="hint">密钥存储在服务器 <code>data/.api_key</code> 文件中</div>
    <button type="submit" id="loginBtn"><span id="btnText">登录</span><div class="loader" id="loader"></div></button>
  </form>
  <div class="footer">MCP Memory Server &mdash; 融合记忆网关</div>
</div>
<script>
async function login(e) {{
  e.preventDefault();
  const key = document.getElementById('key').value.trim();
  if (!key) return;
  const btn = document.getElementById('loginBtn');
  const txt = document.getElementById('btnText');
  const ldr = document.getElementById('loader');
  btn.disabled = true; txt.style.display = 'none'; ldr.style.display = 'block';
  try {{
    const r = await fetch('/admin/login', {{
      method: 'POST',
      headers: {{'Content-Type': 'application/json'}},
      body: JSON.stringify({{key}})
    }});
    const d = await r.json();
    if (r.ok) {{
      localStorage.setItem('memory_gateway_key', key);
      window.location.href = '/admin';
    }} else {{
      document.querySelector('.error')?.remove();
      const errDiv = document.createElement('div');
      errDiv.className = 'error';
      errDiv.textContent = d.detail || '密钥无效';
      document.getElementById('loginForm').insertBefore(errDiv, document.getElementById('loginForm').firstChild);
    }}
  }} catch(e) {{
    document.querySelector('.error')?.remove();
    const errDiv = document.createElement('div');
    errDiv.className = 'error';
    errDiv.textContent = '网络错误: ' + e.message;
    document.getElementById('loginForm').insertBefore(errDiv, document.getElementById('loginForm').firstChild);
  }} finally {{
    btn.disabled = false; txt.style.display = ''; ldr.style.display = 'none';
  }}
}}
</script>
</body></html>"""


@app.middleware("http")
async def api_key_middleware(request: Request, call_next):
    # Allow health check without auth
    if request.url.path == "/health":
        return await call_next(request)

    # Allow login endpoint without auth
    if request.url.path == "/admin/login":
        return await call_next(request)

    # Allow dashboard and its API without auth (has its own session if needed)
    if request.url.path == "/dashboard" or request.url.path.startswith("/api/dashboard/"):
        return await call_next(request)

    if API_KEY:
        # Check header
        key = request.headers.get("X-API-Key", "") or request.headers.get("Authorization", "").removeprefix("Bearer ")
        if key == API_KEY:
            return await call_next(request)

        # Check session cookie
        cookie_key = request.cookies.get(COOKIE_NAME, "")
        if cookie_key and cookie_key == API_KEY:
            return await call_next(request)

        # Auth failed — return login page for browser, JSON for API
        if request.url.path == "/" or request.url.path.startswith("/admin"):
            return HTMLResponse(
                status_code=401,
                content=login_page_html(),
            )
        return JSONResponse(
            status_code=401,
            content={"error": "Unauthorized", "detail": "Valid X-API-Key header required"},
        )

    # No API key configured — open access
    return await call_next(request)


@app.on_event("startup")
async def startup() -> None:
    with db_conn() as db:
        init_db(db)
    log.info("Database ready at %s", DB_PATH)
    if API_KEY:
        log.info("API Key authentication enabled (key starts with: %s...)", API_KEY[:16])
    else:
        log.warning("No API key configured — server is open to all requests")


# ── Health ───────────────────────────────────────────────


@app.get("/health")
async def health() -> dict:
    with db_conn() as db:
        count = db.execute("SELECT COUNT(*) FROM memories WHERE archived=0").fetchone()[0]
    return {"status": "ok", "version": "5.1.0", "memories": count, "db": str(DB_PATH)}


@app.get("/")
async def root():
    """Redirect to the dashboard UI."""
    return RedirectResponse(url="/dashboard", status_code=302)


# ── MCP Endpoints ────────────────────────────────────────


@app.post("/mcp/save")
async def save_memory(req: SaveRequest) -> dict:
    memory_id = req.id or str(uuid.uuid4())
    now = now_iso()
    content = _filter_sensitive(req.content.strip())
    checksum = compute_checksum(content)
    simhash = compute_simhash(content)
    mem_type = req.type or detect_type(content)
    tags_json = json.dumps(req.tags or [])
    category_id = req.category_id or "general"
    confidence = _compute_confidence(mem_type, req.source or "unknown", len(content))

    with db_conn() as db:
        # Check duplicate
        existing = db.execute(
            "SELECT id, checksum FROM memories WHERE checksum=? AND archived=0",
            (checksum,),
        ).fetchone()
        if existing:
            return {
                "success": True,
                "action": "skipped",
                "reason": "duplicate",
                "existing_id": existing["id"],
            }

        # Fuzzy dedup: check simhash for near-duplicates
        near_dup = _find_near_duplicate(db, simhash)
        if near_dup:
            return {
                "success": True,
                "action": "skipped",
                "reason": "near_duplicate",
                "existing_id": near_dup["id"],
                "similarity": near_dup["similarity"],
            }

        # Compute embedding (non-blocking, optional)
        embedding_blob = _compute_embedding(content)
        # Initialize vector clock
        init_clock = json.dumps({req.source: now})

        is_procedural = mem_type == "procedural"

        db.execute(
           """INSERT INTO memories
              (id, content, type, scope, source, priority, confidence, tags, category_id,
                embedding, hot_tier, ttl_days, vector_clock,
                created_at, updated_at, recall_count, archived, checksum, simhash)
              VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, 0, ?, ?)""",
           (memory_id, content, mem_type, req.scope,
            req.source, req.priority or "P1", confidence, tags_json, category_id,
            embedding_blob,
            1 if (req.priority or "P1") == "P0" or is_procedural else 0,  # procedural → hot immediately
            DEFAULT_TTL.get(mem_type) or DEFAULT_TTL.get(req.priority or "P1", 0),
            init_clock,
            now, now, checksum, simhash),
        )

        # 创建初始版本快照
        VersionManager.create_version(
            db, memory_id, content,
            change_type="create",
            changed_by=req.source,
            change_reason="Initial memory creation",
            metadata={"type": mem_type, "category": category_id, "priority": req.priority or "P1"}
        )

        if req.session_id:
            db.execute(
                "INSERT OR IGNORE INTO session_memories (session_id, memory_id, created_at) VALUES (?, ?, ?)",
                (req.session_id, memory_id, now),
            )

        db.execute(
            "INSERT INTO change_log (memory_id, action, snapshot, timestamp) VALUES (?, 'save', ?, ?)",
            (memory_id, content, now),
        )

        # Auto-extract knowledge graph edges from co-occurring terms
        graph_edges = _auto_create_relations(db, memory_id, content, category_id)

    return {"success": True, "action": "saved", "id": memory_id, "type": mem_type, "graph_edges": graph_edges}


# ── 上下文卸载 & 4层渐进存储 ─────────────────────────────


async def offload_memory(req: OffloadRequest) -> dict:
    """将长文本卸载到 raw_memories (L0)，返回索引ID。"""
    raw_id = str(uuid.uuid4())
    token_count = len(req.content) // 4  # 粗略估算 token 数
    now = now_iso()
    with db_conn() as db:
        db.execute(
            """INSERT INTO raw_memories (id, session_id, source, content, token_count, created_at)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (raw_id, req.session_id, req.source, req.content, token_count, now),
        )
        db.commit()
    log.info(f"Offloaded raw memory {raw_id} ({token_count} tokens)")
    return {"success": True, "id": raw_id, "token_count": token_count}


async def drilldown_memory(memory_id: str) -> dict:
    """通过 ID 钻回原始内容 (L0)。"""
    with db_conn() as db:
        row = db.execute(
            "SELECT id, session_id, source, content, token_count, memory_id, created_at "
            "FROM raw_memories WHERE id=?",
            (memory_id,),
        ).fetchone()
    if not row:
        return {"success": False, "error": f"raw memory {memory_id} not found"}
    return {
        "success": True,
        "id": row["id"],
        "session_id": row["session_id"],
        "source": row["source"],
        "content": row["content"],
        "token_count": row["token_count"],
        "memory_id": row["memory_id"],
        "created_at": row["created_at"],
    }


async def get_scenario(category_id: str, days: int = 7) -> dict:
    """获取场景聚合 (L2)。按 category_id 和时间窗口查询。"""
    with db_conn() as db:
        rows = db.execute(
            "SELECT id, category_id, title, summary, memory_ids, "
            "time_window_start, time_window_end, created_at, updated_at "
            "FROM scenarios WHERE category_id=? "
            "ORDER BY created_at DESC LIMIT 50",
            (category_id,),
        ).fetchall()
    results = []
    for r in rows:
        results.append({
            "id": r["id"],
            "category_id": r["category_id"],
            "title": r["title"],
            "summary": r["summary"],
            "memory_ids": json.loads(r["memory_ids"]) if r["memory_ids"] else [],
            "time_window_start": r["time_window_start"],
            "time_window_end": r["time_window_end"],
            "created_at": r["created_at"],
            "updated_at": r["updated_at"],
        })
    return {"success": True, "count": len(results), "scenarios": results}


async def get_persona(persona_type: str, name: str) -> dict:
    """获取画像 (L3)。按 persona_type + name 查询。"""
    with db_conn() as db:
        row = db.execute(
            "SELECT id, persona_type, name, profile_md, version, created_at, updated_at "
            "FROM personas WHERE persona_type=? AND name=?",
            (persona_type, name),
        ).fetchone()
    if not row:
        return {"success": False, "error": f"persona ({persona_type}/{name}) not found"}
    return {
        "success": True,
        "id": row["id"],
        "persona_type": row["persona_type"],
        "name": row["name"],
        "profile_md": row["profile_md"],
        "version": row["version"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


@app.post("/mcp/search")
async def search_memory(req: SearchRequest) -> dict:
    t_start = time.time()
    with db_conn() as db:
        # ── Hot cache check ──
        cache_key = f"search:{req.q}:{req.category_filter}:{req.type_filter}:{req.source_filter}:{req.limit}"
        cached = hot_cache.get(cache_key)
        if cached:
            return {
                "success": True,
                "count": len(cached),
                "results": cached,
                "from_cache": True,
                "cache_size": hot_cache.size,
            }

        safe_q = req.q.replace('"', '""')

        conditions = ["m.archived=0"]
        params: list[Any] = []

        if not req.include_archived:
            pass
        else:
            conditions[0] = "1=1"

        if req.category_filter:
            if req.category_filter == "work":
                conditions.append("(m.category_id = ? OR m.category_id LIKE ?)")
                params.append(req.category_filter)
                params.append("work_%")
            else:
                conditions.append("m.category_id=?")
                params.append(req.category_filter)
        if req.scope_filter:
            conditions.append("m.scope=?")
            params.append(req.scope_filter)
        if req.source_filter:
            conditions.append("m.source=?")
            params.append(req.source_filter)
        if req.type_filter:
            conditions.append("m.type=?")
            params.append(req.type_filter)

        where = " AND ".join(conditions)

        # Short queries (< 3 chars) use LIKE directly
        if len(req.q) < 3:
            like_q = f"%{req.q}%"
            sql = f"""
                SELECT m.*, 0 as rank
                FROM memories m
                WHERE m.content LIKE ? AND {where}
                ORDER BY m.created_at DESC
                LIMIT ?
            """
            rows = db.execute(sql, [like_q] + params + [req.limit]).fetchall()
            search_type = "like"
        else:
            fts_query = f'"{safe_q}"'
            sql = f"""
                SELECT m.*, fts.rank
                FROM memories_fts fts
                JOIN memories m ON m.rowid = fts.rowid
                WHERE memories_fts MATCH ? AND {where}
                ORDER BY fts.rank
                LIMIT ?
            """
            fts_params = [fts_query] + params + [req.limit]

            try:
                rows = db.execute(sql, fts_params).fetchall()
                search_type = "fts5"
            except sqlite3.OperationalError:
                like_q = f"%{req.q}%"
                sql_fallback = f"""
                    SELECT m.*, 0 as rank
                    FROM memories m
                    WHERE m.content LIKE ? AND {where}
                    ORDER BY m.created_at DESC
                    LIMIT ?
                """
                params_fallback = [like_q] + params + [req.limit]
                rows = db.execute(sql_fallback, params_fallback).fetchall()
                search_type = "like_fallback"

        # ── Hybrid rerank (semantic) ──
        query_embedding = _compute_embedding(req.q) if len(req.q) >= 3 else None
        results = _hybrid_search(db, req.q, query_embedding, list(rows),
                                 req.limit, semantic_weight=0.35)

        # Update recall stats (batch)
        now = now_iso()
        ids = [r["id"] for r in results]
        if ids:
            placeholders = ",".join("?" for _ in ids)
            db.execute(
                f"UPDATE memories SET last_recalled=?, recall_count=recall_count+1, "
                f"confidence=MIN(1.0, confidence + 0.02) WHERE id IN ({placeholders})",
                [now] + ids,
            )
            # Promote to hot tier
            _sync_hot_tier_from_cache(db)

        # ── Audit log ──
        latency_ms = round((time.time() - t_start) * 1000, 2)
        db.execute(
            """INSERT INTO search_audit_log
               (query, source, result_count, result_ids, latency_ms, search_type, hit_cache)
               VALUES (?, ?, ?, ?, ?, ?, 0)""",
            (req.q, req.source_filter or "unknown", len(results),
             json.dumps(ids[:20]), latency_ms, search_type),
        )

        # ── Update hot cache ──
        hot_cache.put(cache_key, results)

    return {
        "success": True,
        "count": len(results),
        "results": results,
        "latency_ms": latency_ms,
        "search_type": search_type,
        "has_embedding": query_embedding is not None,
    }


@app.post("/mcp/list")
async def list_memory(req: ListRequest) -> dict:
    with db_conn() as db:
        conditions = []
        params: list[Any] = []

        if not req.include_archived:
            conditions.append("archived=0")

        if req.since:
            conditions.append("created_at >= ?")
            params.append(req.since)

        if req.category_filter:
            # 支持父分类过滤
            if req.category_filter == "work":
                conditions.append("(category_id = ? OR category_id LIKE ?)")
                params.append(req.category_filter)
                params.append("work_%")
            else:
                conditions.append("category_id=?")
                params.append(req.category_filter)

        if req.scope_filter:
            conditions.append("scope=?")
            params.append(req.scope_filter)

        if req.source_filter:
            conditions.append("source=?")
            params.append(req.source_filter)

        where = (" WHERE " + " AND ".join(conditions)) if conditions else ""

        sql = f"SELECT * FROM memories {where} ORDER BY created_at DESC LIMIT ?"
        params.append(req.limit)

        rows = db.execute(sql, params).fetchall()

    results = [row_to_dict(r) for r in rows]
    return {"success": True, "count": len(results), "results": results}


class CheckDuplicatesRequest(BaseModel):
    checksums: list[str] = Field(default_factory=list, description="List of SHA256 checksums to check")
    simhashes: list[dict] = Field(default_factory=list, description="List of {content, simhash} to fuzzy-check")


@app.post("/mcp/check-duplicates")
async def check_duplicates(req: CheckDuplicatesRequest) -> dict:
    """Batch check for exact and near duplicates.

    Returns:
      - exact: set of checksums already in DB
      - near: list of {simhash, existing_id, distance} for near-duplicates
    """
    exact_dupes = set()
    near_dupes = []

    with db_conn() as db:
        # Exact match: batch check checksums
        if req.checksums:
            placeholders = ",".join(["?"] * len(req.checksums))
            rows = db.execute(
                f"SELECT checksum FROM memories WHERE checksum IN ({placeholders}) AND archived=0",
                req.checksums,
            ).fetchall()
            exact_dupes = {r["checksum"] for r in rows}

        # Fuzzy match: check simhashes
        if req.simhashes:
            existing = db.execute(
                "SELECT id, simhash FROM memories WHERE archived=0 AND simhash != '' LIMIT 1000"
            ).fetchall()
            for item in req.simhashes:
                sh = item.get("simhash", "")
                if not sh:
                    continue
                for r in existing:
                    if r["simhash"]:
                        dist = hamming_distance(sh, r["simhash"])
                        if dist < 10:
                            near_dupes.append({
                                "input_simhash": sh,
                                "existing_id": r["id"],
                                "existing_simhash": r["simhash"],
                                "distance": dist,
                                "similarity": round(1.0 - dist / 64, 3),
                            })
                            break

    return {
        "success": True,
        "exact": list(exact_dupes),
        "near": near_dupes,
    }


# ══════════════════════════════════════════════════════════
# V5 Endpoints: Hybrid Search, Audit, Cleanup, Cache Stats
# ══════════════════════════════════════════════════════════


class SearchHybridRequest(BaseModel):
    q: str
    category_filter: Optional[str] = None
    scope_filter: Optional[str] = None
    source_filter: Optional[str] = None
    type_filter: Optional[str] = None
    limit: int = Field(default=10, ge=1, le=100)
    include_archived: bool = False
    semantic_weight: float = Field(default=0.4, ge=0.0, le=1.0,
                                   description="0.0=pure FTS, 1.0=pure semantic")


@app.post("/mcp/search_hybrid")
async def search_hybrid(req: SearchHybridRequest) -> dict:
    """Hybrid search: combines FTS5 keyword + embedding semantic similarity."""
    t_start = time.time()
    with db_conn() as db:
        conditions = ["m.archived=0"]
        params: list[Any] = []

        if not req.include_archived:
            pass
        else:
            conditions[0] = "1=1"

        if req.category_filter:
            if req.category_filter == "work":
                conditions.append("(m.category_id = ? OR m.category_id LIKE ?)")
                params.append(req.category_filter)
                params.append("work_%")
            else:
                conditions.append("m.category_id=?")
                params.append(req.category_filter)
        if req.scope_filter:
            conditions.append("m.scope=?")
            params.append(req.scope_filter)
        if req.source_filter:
            conditions.append("m.source=?")
            params.append(req.source_filter)
        if req.type_filter:
            conditions.append("m.type=?")
            params.append(req.type_filter)

        where = " AND ".join(conditions)
        safe_q = req.q.replace('"', '""')

        # Short queries (< 3 chars) use LIKE directly
        if len(req.q) < 3:
            like_q = f"%{req.q}%"
            sql = f"""
                SELECT m.*, 0 as rank
                FROM memories m
                WHERE m.content LIKE ? AND {where}
                ORDER BY m.created_at DESC
                LIMIT ?
            """
            rows = db.execute(sql, [like_q] + params + [req.limit]).fetchall()
            search_type = "like"
        else:
            fts_query = f'"{safe_q}"'

            sql = f"""
                SELECT m.*, fts.rank
                FROM memories_fts fts
                JOIN memories m ON m.rowid = fts.rowid
                WHERE memories_fts MATCH ? AND {where}
                ORDER BY fts.rank
                LIMIT ?
            """
            fts_params = [fts_query] + params + [req.limit]

            try:
                rows = db.execute(sql, fts_params).fetchall()
                search_type = "fts5"
            except sqlite3.OperationalError:
                # FTS query syntax error, fallback to LIKE
                like_q = f"%{req.q}%"
                sql_fallback = f"""
                    SELECT m.*, 0 as rank
                    FROM memories m
                    WHERE m.content LIKE ? AND {where}
                    ORDER BY m.created_at DESC
                    LIMIT ?
                """
                params_fallback = [like_q] + params + [req.limit]
                rows = db.execute(sql_fallback, params_fallback).fetchall()
                search_type = "like_fallback"

        query_embedding = _compute_embedding(req.q) if len(req.q) >= 3 else None
        results = _hybrid_search(db, req.q, query_embedding, list(rows),
                                 req.limit, semantic_weight=req.semantic_weight)

        latency_ms = round((time.time() - t_start) * 1000, 2)
        ids = [r["id"] for r in results]

        # Update recall stats + promote hot tier (batch)
        if ids:
            now = now_iso()
            placeholders = ",".join("?" for _ in ids)
            db.execute(
                f"UPDATE memories SET last_recalled=?, recall_count=recall_count+1, "
                f"confidence=MIN(1.0, confidence + 0.02) WHERE id IN ({placeholders})",
                [now] + ids,
            )
            _sync_hot_tier_from_cache(db)

        # Audit log
        db.execute(
            """INSERT INTO search_audit_log
               (query, source, result_count, result_ids, latency_ms, search_type, hit_cache)
               VALUES (?, ?, ?, ?, ?, ?, 0)""",
            (req.q, req.source_filter or "unknown", len(results),
             json.dumps(ids[:20]), latency_ms, search_type),
        )

    return {
        "success": True,
        "count": len(results),
        "results": results,
        "latency_ms": latency_ms,
        "semantic_weight": req.semantic_weight,
        "has_embedding": query_embedding is not None,
    }


class AuditSearchRequest(BaseModel):
    source: Optional[str] = None
    limit: int = Field(default=50, ge=1, le=200)
    since: Optional[str] = None


@app.post("/mcp/audit/search")
async def audit_search(req: AuditSearchRequest) -> dict:
    """Query search audit logs."""
    with db_conn() as db:
        conditions = []
        params: list[Any] = []
        if req.source:
            conditions.append("source=?")
            params.append(req.source)
        if req.since:
            conditions.append("created_at >= ?")
            params.append(req.since)
        where = (" WHERE " + " AND ".join(conditions)) if conditions else ""
        sql = f"SELECT * FROM search_audit_log {where} ORDER BY created_at DESC LIMIT ?"
        rows = db.execute(sql, params + [req.limit]).fetchall()
    return {"success": True, "count": len(rows), "audit_logs": [dict(r) for r in rows]}


class CleanupRequest(BaseModel):
    confirm: bool = False


@app.post("/mcp/cleanup")
async def cleanup_memories(req: CleanupRequest) -> dict:
    """Apply memory decay: archive expired + demote cold memories."""
    if not req.confirm:
        return {
            "success": False,
            "error": "Set confirm=true to proceed. This will archive expired memories.",
            "policy": {"ttl": DEFAULT_TTL, "recall_threshold": DECAY_THRESHOLD},
        }
    with db_conn() as db:
        stats = _apply_decay(db)
    hot_cache.clear()  # invalidate cache after cleanup
    return {"success": True, "stats": stats, "cache_cleared": True}


@app.get("/mcp/cache/stats")
async def cache_stats() -> dict:
    """Get hot cache statistics."""
    return {
        "success": True,
        "cache_size": hot_cache.size,
        "cache_max": HOT_CACHE_MAX,
        "cache_ttl_seconds": HOT_CACHE_TTL,
    }


@app.get("/mcp/get/{memory_id}")
async def get_memory(memory_id: str) -> dict:
    with db_conn() as db:
        row = db.execute("SELECT * FROM memories WHERE id=?", (memory_id,)).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail=f"Memory {memory_id} not found")
    return {"success": True, "memory": row_to_dict(row)}


@app.put("/mcp/update/{memory_id}")
async def update_memory(memory_id: str, req: UpdateRequest) -> dict:
    with db_conn() as db:
        existing = db.execute("SELECT * FROM memories WHERE id=?", (memory_id,)).fetchone()
        if not existing:
            raise HTTPException(status_code=404, detail=f"Memory {memory_id} not found")

        # ── Vector clock conflict detection ──
        vc_result = _update_vector_clock(db, memory_id, "api")

        updates = []
        params: list[Any] = []

        if req.content is not None:
            updates.append("content=?")
            params.append(req.content.strip())
            updates.append("checksum=?")
            params.append(compute_checksum(req.content))
            updates.append("simhash=?")
            params.append(compute_simhash(req.content))
            # Re-compute embedding if content changed
            embedding_blob = _compute_embedding(req.content.strip())
            if embedding_blob:
                updates.append("embedding=?")
                params.append(embedding_blob)
        if req.type is not None:
            updates.append("type=?")
            params.append(req.type)
        if req.scope is not None:
            updates.append("scope=?")
            params.append(req.scope)
        if req.priority is not None:
            updates.append("priority=?")
            params.append(req.priority)
            # Recalculate ttl_days and hot_tier when priority changes
            updates.append("ttl_days=?")
            params.append(DEFAULT_TTL.get(req.priority, 0))
            updates.append("hot_tier=?")
            params.append(1 if req.priority == "P0" else 0)
        if req.category_id is not None:
            updates.append("category_id=?")
            params.append(req.category_id)
        if req.tags is not None:
            updates.append("tags=?")
            params.append(json.dumps(req.tags))
        if req.archived is not None:
            updates.append("archived=?")
            params.append(1 if req.archived else 0)

        if not updates:
            return {"success": True, "action": "no_changes"}

        updates.append("updated_at=?")
        params.append(now_iso())

        sql = f"UPDATE memories SET {', '.join(updates)} WHERE id=?"
        params.append(memory_id)
        db.execute(sql, params)

        db.execute(
            "INSERT INTO change_log (memory_id, action, snapshot, timestamp) VALUES (?, 'update', ?, ?)",
            (memory_id, req.content or "", now_iso()),
        )

        # 创建版本快照（如果内容被修改）
        if req.content is not None:
            VersionManager.create_version(
                db, memory_id, req.content.strip(),
                change_type="update",
                changed_by="api",
                change_reason="Content updated via API"
            )

        # Invalidate hot cache (search results may include this memory)
        hot_cache.clear()

    return {
        "success": True,
        "action": "updated",
        "id": memory_id,
        "vector_clock": vc_result,
    }


@app.delete("/mcp/delete/{memory_id}")
async def delete_memory(memory_id: str) -> dict:
    with db_conn() as db:
        existing = db.execute("SELECT id FROM memories WHERE id=?", (memory_id,)).fetchone()
        if not existing:
            raise HTTPException(status_code=404, detail=f"Memory {memory_id} not found")

        # 清理关联表（session_memories 和 change_log 没有 ON DELETE CASCADE）
        db.execute("DELETE FROM session_memories WHERE memory_id=?", (memory_id,))
        db.execute("DELETE FROM change_log WHERE memory_id=?", (memory_id,))
        db.execute("DELETE FROM raw_memories WHERE memory_id=?", (memory_id,))

        # 删除记忆（memory_versions, evolution_log, memory_branches 有 CASCADE 会自动清理）
        db.execute("DELETE FROM memories WHERE id=?", (memory_id,))

        log.info(f"Memory {memory_id[:8]}... deleted with all related data")
        hot_cache.clear()
    return {"success": True, "action": "deleted", "id": memory_id}


# ── Stats & Export ───────────────────────────────────────


def _get_stats(db: sqlite3.Connection) -> dict:
    """Return statistics about stored memories: counts by active/archived status, source, type, and scope."""
    total = db.execute("SELECT COUNT(*) FROM memories").fetchone()[0]
    active = db.execute("SELECT COUNT(*) FROM memories WHERE archived=0").fetchone()[0]
    archived = db.execute("SELECT COUNT(*) FROM memories WHERE archived=1").fetchone()[0]

    by_source = {}
    for row in db.execute("SELECT source, COUNT(*) as c FROM memories WHERE archived=0 GROUP BY source"):
        by_source[row["source"]] = row["c"]

    by_type = {}
    for row in db.execute("SELECT type, COUNT(*) as c FROM memories WHERE archived=0 GROUP BY type"):
        by_type[row["type"]] = row["c"]

    by_scope = {}
    for row in db.execute("SELECT scope, COUNT(*) as c FROM memories WHERE archived=0 GROUP BY scope"):
        by_scope[row["scope"]] = row["c"]

    return {
        "total": total,
        "active": active,
        "archived": archived,
        "by_source": by_source,
        "by_type": by_type,
        "by_scope": by_scope,
    }


# ── Export ───────────────────────────────────────────────


@app.get("/mcp/export")
async def export_memories(scope: Optional[str] = None, source: Optional[str] = None) -> dict:
    with db_conn() as db:
        conditions = ["archived=0"]
        params: list[Any] = []
        if scope:
            conditions.append("scope=?")
            params.append(scope)
        if source:
            conditions.append("source=?")
            params.append(source)

        where = " WHERE " + " AND ".join(conditions)
        sql = f"SELECT * FROM memories {where} ORDER BY created_at DESC"
        rows = db.execute(sql, params).fetchall()

    return {"success": True, "count": len(rows), "memories": [row_to_dict(r) for r in rows]}


# ── Categories ────────────────────────────────────────────


@app.get("/mcp/categories")
async def list_categories(parent_id: Optional[str] = None) -> dict:
    """Get category tree. If parent_id is provided, return children of that category."""
    with db_conn() as db:
        if parent_id is not None:
            rows = db.execute(
                "SELECT * FROM categories WHERE parent_id=? ORDER BY sort_order, name",
                (parent_id,),
            ).fetchall()
        else:
            rows = db.execute(
                "SELECT * FROM categories ORDER BY sort_order, name"
            ).fetchall()
    return {"success": True, "categories": [dict(r) for r in rows]}


@app.get("/mcp/categories/{category_id}")
async def get_category(category_id: str) -> dict:
    """Get a single category by ID."""
    with db_conn() as db:
        row = db.execute("SELECT * FROM categories WHERE id=?", (category_id,)).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail=f"Category {category_id} not found")
    return {"success": True, "category": dict(row)}


@app.post("/mcp/categories")
async def create_category(req: CategoryRequest) -> dict:
    """Create a new custom category."""
    with db_conn() as db:
        existing = db.execute("SELECT id FROM categories WHERE id=?", (req.id,)).fetchone()
        if existing:
            raise HTTPException(status_code=409, detail=f"Category {req.id} already exists")
        db.execute(
            "INSERT INTO categories (id, name, parent_id, icon, sort_order) VALUES (?, ?, ?, ?, ?)",
            (req.id, req.name, req.parent_id, req.icon or "📁", req.sort_order or 0),
        )
    return {"success": True, "category": {"id": req.id, "name": req.name}}


@app.put("/mcp/categories/{category_id}")
async def update_category(category_id: str, req: CategoryUpdateRequest) -> dict:
    """Update a category."""
    with db_conn() as db:
        existing = db.execute("SELECT id FROM categories WHERE id=?", (category_id,)).fetchone()
        if not existing:
            raise HTTPException(status_code=404, detail=f"Category {category_id} not found")
        updates = []
        params: list[Any] = []
        if req.name is not None:
            updates.append("name=?")
            params.append(req.name)
        if req.parent_id is not None:
            updates.append("parent_id=?")
            params.append(req.parent_id)
        if req.icon is not None:
            updates.append("icon=?")
            params.append(req.icon)
        if req.sort_order is not None:
            updates.append("sort_order=?")
            params.append(req.sort_order)
        if updates:
            sql = f"UPDATE categories SET {', '.join(updates)} WHERE id=?"
            params.append(category_id)
            db.execute(sql, params)
    return {"success": True, "category_id": category_id}


@app.delete("/mcp/categories/{category_id}")
async def delete_category(category_id: str) -> dict:
    """Delete a category. Memories using this category will revert to 'general'."""
    with db_conn() as db:
        existing = db.execute("SELECT id FROM categories WHERE id=?", (category_id,)).fetchone()
        if not existing:
            raise HTTPException(status_code=404, detail=f"Category {category_id} not found")
        # Check if it's a system category
        if category_id in ("general", "learning", "life", "work", "innovation"):
            raise HTTPException(status_code=400, detail="Cannot delete system categories")
        # Reassign memories to 'general'
        db.execute("UPDATE memories SET category_id='general' WHERE category_id=?", (category_id,))
        db.execute("DELETE FROM categories WHERE id=?", (category_id,))
    return {"success": True, "action": "deleted", "category_id": category_id}


# ── Sync Status ──────────────────────────────────────────


@app.get("/mcp/sync/status")
async def get_sync_status() -> dict:
    """Get synchronization status for all tools."""
    with db_conn() as db:
        rows = db.execute("SELECT * FROM sync_status ORDER BY tool").fetchall()
        # Also check if any tool is stale (>30 min since last beat)
        results = []
        for row in rows:
            row_dict = dict(row)
            try:
                last_beat = datetime.fromisoformat(row_dict["last_beat"])
                age_minutes = (datetime.now(timezone.utc) - last_beat).total_seconds() / 60
                if age_minutes > 120:
                    row_dict["status"] = "disconnected"
                elif age_minutes > 30:
                    row_dict["status"] = "stale"
                else:
                    row_dict["status"] = "healthy"
            except Exception:
                row_dict["status"] = "unknown"
            results.append(row_dict)
    return {"success": True, "sync_status": results}


@app.post("/mcp/sync/heartbeat")
async def sync_heartbeat(req: SyncHeartbeatRequest) -> dict:
    """Register a heartbeat from a tool. Updates sync status."""
    now = now_iso()
    with db_conn() as db:
        db.execute(
            """INSERT INTO sync_status (tool, last_sync, last_beat, total_syncs, last_count, status)
               VALUES (?, ?, ?, 1, ?, 'healthy')
               ON CONFLICT(tool) DO UPDATE SET
               last_beat=excluded.last_beat,
               total_syncs=sync_status.total_syncs+1,
               last_count=excluded.last_count,
               status='healthy'""",
            (req.tool, now, now, req.count),
        )
    return {"success": True, "tool": req.tool, "timestamp": now}


# ── Memory Relations ─────────────────────────────────────


@app.post("/mcp/relations")
async def create_relation(req: RelationRequest) -> dict:
    """Create a relation between two memories."""
    with db_conn() as db:
        # Verify both memories exist
        src = db.execute("SELECT id FROM memories WHERE id=? AND archived=0", (req.source_id,)).fetchone()
        tgt = db.execute("SELECT id FROM memories WHERE id=? AND archived=0", (req.target_id,)).fetchone()
        if not src:
            raise HTTPException(status_code=404, detail=f"Source memory {req.source_id} not found")
        if not tgt:
            raise HTTPException(status_code=404, detail=f"Target memory {req.target_id} not found")
        db.execute(
            """INSERT OR REPLACE INTO memory_relations (source_id, target_id, relation, strength)
               VALUES (?, ?, ?, ?)""",
            (req.source_id, req.target_id, req.relation, req.strength),
        )
    return {"success": True, "relation": {"source": req.source_id, "target": req.target_id, "type": req.relation}}


@app.get("/mcp/relations/{memory_id}")
async def get_relations(memory_id: str) -> dict:
    """Get all relations for a memory."""
    with db_conn() as db:
        rows = db.execute(
            """SELECT mr.*, m.content as target_content
               FROM memory_relations mr
               JOIN memories m ON m.id = mr.target_id
               WHERE mr.source_id=?
               ORDER BY mr.strength DESC""",
            (memory_id,),
        ).fetchall()
    return {"success": True, "relations": [dict(r) for r in rows]}


@app.delete("/mcp/relations/{source_id}/{target_id}")
async def delete_relation(source_id: str, target_id: str) -> dict:
    """Delete a relation between two memories."""
    with db_conn() as db:
        db.execute(
            "DELETE FROM memory_relations WHERE source_id=? AND target_id=?",
            (source_id, target_id),
        )
    return {"success": True, "action": "deleted"}


@app.get("/mcp/graph")
async def get_graph(term: Optional[str] = None, memory_id: Optional[str] = None, limit: int = 20) -> dict:
    """Query the knowledge graph: get related terms for a given term or memory."""
    with db_conn() as db:
        if memory_id:
            row = db.execute("SELECT content, category_id FROM memories WHERE id=?", (memory_id,)).fetchone()
            if row:
                terms = _extract_key_terms(row["content"])
                graph = {}
                for t in terms:
                    related = _get_related_terms(db, t, limit)
                    if related:
                        graph[t] = related
                return {"memory_id": memory_id, "terms": terms, "graph": graph}
            else:
                return {"error": f"Memory {memory_id} not found"}
        elif term:
            related = _get_related_terms(db, term, limit)
            return {"term": term, "related": related, "count": len(related)}
        else:
            rows = db.execute(
                "SELECT source_id, target_id, relation, strength FROM memory_relations ORDER BY strength DESC LIMIT ?",
                (limit,)
            ).fetchall()
            return {"edges": [dict(r) for r in rows], "count": len(rows)}


# ── Enhanced Stats ────────────────────────────────────────


@app.get("/mcp/stats")
async def stats() -> dict:
    with db_conn() as db:
        base_stats = _get_stats(db)
        # By category
        by_category = {}
        for row in db.execute(
            "SELECT category_id, COUNT(*) as c FROM memories WHERE archived=0 GROUP BY category_id"
        ):
            by_category[row["category_id"]] = row["c"]
        # By priority
        by_priority = {}
        for row in db.execute(
            "SELECT priority, COUNT(*) as c FROM memories WHERE archived=0 GROUP BY priority"
        ):
            by_priority[row["priority"]] = row["c"]
        # Sync status count
        sync_healthy = db.execute(
            "SELECT COUNT(*) FROM sync_status WHERE status='healthy'"
        ).fetchone()[0]
        sync_total = db.execute("SELECT COUNT(*) FROM sync_status").fetchone()[0]
        # Relation count
        relation_count = db.execute("SELECT COUNT(*) FROM memory_relations").fetchone()[0]

        base_stats["by_category"] = by_category
        base_stats["by_priority"] = by_priority
        base_stats["sync"] = {"healthy": sync_healthy, "total": sync_total}
        base_stats["relations"] = relation_count
        # V5: hot/cold tier stats
        hot_count = db.execute(
            "SELECT COUNT(*) FROM memories WHERE archived=0 AND hot_tier=1"
        ).fetchone()[0]
        cold_count = db.execute(
            "SELECT COUNT(*) FROM memories WHERE archived=0 AND hot_tier=0"
        ).fetchone()[0]
        by_hot_tier = {"hot": hot_count, "cold": cold_count}
        # V5: decay stats
        archived_count = db.execute(
            "SELECT COUNT(*) FROM memories WHERE archived=1"
        ).fetchone()[0]
        audit_total = db.execute(
            "SELECT COUNT(*) FROM search_audit_log"
        ).fetchone()[0]
        base_stats["tier"] = by_hot_tier
        base_stats["decay"] = {"archived_total": archived_count, "audit_logs": audit_total}
    return base_stats


# ── MCP JSON-RPC 2.0 Protocol ────────────────────────────

MCP_TOOLS = [
    {
        "name": "mem_save",
        "description": "保存一条记忆到记忆库。支持分类、优先级、标签等元数据。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "content": {"type": "string", "description": "记忆内容"},
                "category_id": {"type": "string", "description": "分类ID (learning/life/work/innovation/general 或 work_* 子分类)", "default": "general"},
                "type": {"type": "string", "enum": ["general", "rule", "preference", "decision", "context", "learning", "reference", "convention"], "default": "general"},
                "priority": {"type": "string", "enum": ["P0", "P1", "P2"], "default": "P1"},
                "tags": {"type": "array", "items": {"type": "string"}, "description": "自定义标签"},
                "source": {"type": "string", "enum": ["hermes", "claude", "workbuddy", "system", "unknown"], "default": "unknown"},
                "scope": {"type": "string", "enum": ["global", "project", "agent"], "default": "global"},
                "session_id": {"type": "string", "description": "会话ID（可选）"}
            },
            "required": ["content"]
        }
    },
    {
        "name": "mem_search",
        "description": "搜索记忆库。支持关键词、分类、标签过滤。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "搜索关键词"},
                "category_filter": {"type": "string", "description": "分类过滤"},
                "type_filter": {"type": "string", "description": "类型过滤"},
                "limit": {"type": "integer", "description": "返回数量", "default": 10}
            },
            "required": ["query"]
        }
    },
    {
        "name": "mem_list",
        "description": "列出记忆。支持增量同步（since参数）。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "since": {"type": "string", "description": "ISO8601 时间戳（增量同步）"},
                "category_filter": {"type": "string", "description": "分类过滤"},
                "limit": {"type": "integer", "description": "返回数量", "default": 50}
            }
        }
    },
    {
        "name": "mem_delete",
        "description": "删除一条记忆。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "id": {"type": "string", "description": "记忆ID"}
            },
            "required": ["id"]
        }
    },
    {
        "name": "mem_categories",
        "description": "获取所有可用的分类列表。",
        "inputSchema": {
            "type": "object",
            "properties": {}
        }
    },
    {
        "name": "mem_stats",
        "description": "获取记忆库统计信息。",
        "inputSchema": {
            "type": "object",
            "properties": {}
        }
    },
    {
        "name": "sync_heartbeat",
        "description": "发送同步心跳，更新工具连接状态。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "tool": {"type": "string", "enum": ["hermes", "claude", "workbuddy", "system"]},
                "count": {"type": "integer", "description": "本次同步条数", "default": 0}
            },
            "required": ["tool"]
        }
    },
    {
        "name": "mem_history",
        "description": "获取记忆的版本历史（Git for Memory）。查看一条记忆的所有变更记录。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "memory_id": {"type": "string", "description": "记忆ID"},
                "limit": {"type": "integer", "description": "返回版本数量", "default": 20}
            },
            "required": ["memory_id"]
        }
    },
    {
        "name": "mem_diff",
        "description": "对比记忆的两个版本差异（Git for Memory）。查看具体改了什么。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "memory_id": {"type": "string", "description": "记忆ID"},
                "version_a": {"type": "integer", "description": "旧版本号"},
                "version_b": {"type": "integer", "description": "新版本号"}
            },
            "required": ["memory_id", "version_a", "version_b"]
        }
    },
    {
        "name": "mem_rollback",
        "description": "回滚记忆到指定版本（Git for Memory）。进化出错时可恢复。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "memory_id": {"type": "string", "description": "记忆ID"},
                "version": {"type": "integer", "description": "要回滚到的版本号"},
                "agent": {"type": "string", "description": "执行回滚的Agent", "default": "system"}
            },
            "required": ["memory_id", "version"]
        }
    },
    {
        "name": "mem_branch",
        "description": "创建或列出记忆分支（Git for Memory）。支持多Agent并行演化。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "memory_id": {"type": "string", "description": "记忆ID"},
                "action": {"type": "string", "enum": ["create", "list"], "description": "操作类型"},
                "branch_name": {"type": "string", "description": "分支名称（create时必填）"},
                "from_version": {"type": "integer", "description": "从哪个版本创建分支（默认最新）"}
            },
            "required": ["memory_id", "action"]
        }
    },
    {
        "name": "mem_merge",
        "description": "合并记忆分支（Git for Memory）。将源分支合并到目标分支。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "memory_id": {"type": "string", "description": "记忆ID"},
                "source_branch": {"type": "string", "description": "源分支名称"},
                "target_branch": {"type": "string", "description": "目标分支名称"},
                "agent": {"type": "string", "description": "执行合并的Agent", "default": "system"}
            },
            "required": ["memory_id", "source_branch", "target_branch"]
        }
    },
    {
        "name": "mem_offload",
        "description": "上下文卸载。将长文本卸载到原始层(L0)，返回索引ID，后续可通过 mem_drilldown 钻回。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "content": {"type": "string", "description": "要卸载的长文本内容"},
                "session_id": {"type": "string", "description": "会话ID（可选）"},
                "source": {"type": "string", "description": "来源", "default": "unknown"}
            },
            "required": ["content"]
        }
    },
    {
        "name": "mem_drilldown",
        "description": "钻回查询。通过 raw memory ID 获取原始卸载内容。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "memory_id": {"type": "string", "description": "raw memory ID（由 mem_offload 返回）"}
            },
            "required": ["memory_id"]
        }
    },
    {
        "name": "mem_scenario",
        "description": "获取场景聚合（L2层）。按分类查询多条记忆聚合的场景。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "category_id": {"type": "string", "description": "分类ID"},
                "days": {"type": "integer", "description": "时间窗口天数", "default": 7}
            },
            "required": ["category_id"]
        }
    },
    {
        "name": "mem_persona",
        "description": "获取画像（L3层）。查询用户/项目/领域画像。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "persona_type": {"type": "string", "enum": ["user", "project", "domain"], "description": "画像类型"},
                "name": {"type": "string", "description": "画像名称"}
            },
            "required": ["persona_type", "name"]
        }
    },
    {
        "name": "mem_search_hybrid",
        "description": "混合搜索：结合FTS5关键词匹配和embedding语义相似度。支持调整语义权重（0.0=纯关键词，1.0=纯语义）。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "搜索关键词"},
                "category_filter": {"type": "string", "description": "分类过滤"},
                "type_filter": {"type": "string", "description": "类型过滤"},
                "semantic_weight": {"type": "number", "description": "语义权重（0.0-1.0，默认0.4）", "default": 0.4},
                "limit": {"type": "integer", "description": "返回数量", "default": 10}
            },
            "required": ["query"]
        }
    },
    {
        "name": "mem_cleanup",
        "description": "触发记忆衰减和过期清理。归档超期P2记忆，降级冷记忆的热度。需要确认参数。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "confirm": {"type": "boolean", "description": "确认执行清理操作", "default": False}
            },
            "required": ["confirm"]
        }
    },
    {
        "name": "mem_audit_search",
        "description": "查询检索审计日志。查看历史搜索记录、延迟、命中率。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "source": {"type": "string", "description": "来源过滤（hermes/claude/workbuddy）"},
                "since": {"type": "string", "description": "ISO8601起始时间"},
                "limit": {"type": "integer", "description": "返回数量", "default": 50}
            }
        }
    },
    {
        "name": "mem_cache_stats",
        "description": "查看热缓存（Hot Cache）统计：缓存大小、最大容量、TTL。",
        "inputSchema": {
            "type": "object",
            "properties": {}
        }
    },
    {
        "name": "mem_graph",
        "description": "查询知识图谱：获取与某个术语相关的所有关联术语及其关联强度。支持按记忆ID查询其关键术语。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "term": {"type": "string", "description": "要查询的术语（如'代维'、'Hermes'）"},
                "memory_id": {"type": "string", "description": "记忆ID（可选，提取该记忆的关键术语）"},
                "limit": {"type": "integer", "description": "返回关联数量", "default": 10}
            }
        }
    },
    {
        "name": "mem_dreams",
        "description": "Dreams 后台整合：扫描记忆库，发现相似记忆、矛盾记忆，生成整合建议。适合记忆量>100时定期运行。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "action": {"type": "string", "enum": ["scan", "merge", "stats"], "description": "操作类型：scan=扫描矛盾/相似，merge=自动合并，stats=统计"},
                "category_filter": {"type": "string", "description": "分类过滤（可选）"},
                "auto_merge": {"type": "boolean", "description": "自动合并相似度>0.9的记忆", "default": False}
            },
            "required": ["action"]
        }
    },
    {
        "name": "mem_evolve",
        "description": "CSSF自进化协议：分析记忆使用模式，生成元洞察（哪些知识被频繁使用、哪些被遗忘），自动优化记忆优先级。建议每周运行一次。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "action": {"type": "string", "enum": ["analyze", "optimize", "insights"], "description": "操作类型：analyze=分析模式，optimize=自动优化优先级，insights=生成元洞察"},
                "days": {"type": "integer", "description": "分析时间窗口（天）", "default": 30}
            },
            "required": ["action"]
        }
    }
]


async def handle_mcp_initialize(request_id: Any, params: dict) -> dict:
    """Handle MCP initialize request."""
    return {
        "jsonrpc": "2.0",
        "id": request_id,
        "result": {
            "protocolVersion": "2025-03-26",
            "capabilities": {
                "tools": {"listChanged": False},
                "resources": {},
                "prompts": {}
            },
            "serverInfo": {
                "name": "memory-gateway",
                "version": "5.1.0"
            }
        }
    }


async def handle_mcp_tools_list(request_id: Any, params: dict) -> dict:
    """Handle tools/list request."""
    return {
        "jsonrpc": "2.0",
        "id": request_id,
        "result": {"tools": MCP_TOOLS}
    }


async def handle_mcp_tools_call(request_id: Any, params: dict) -> dict:
    """Handle tools/call request."""
    tool_name = params.get("name", "")
    arguments = params.get("arguments", {})

    try:
        if tool_name == "mem_save":
            req = SaveRequest(**arguments)
            result = await save_memory(req)
            text = json.dumps(result, ensure_ascii=False)

        elif tool_name == "mem_search":
            search_req = SearchRequest(
                q=arguments.get("query", arguments.get("q", "")),
                category_filter=arguments.get("category_filter"),
                type_filter=arguments.get("type_filter"),
                limit=arguments.get("limit", 10),
            )
            result = await search_memory(search_req)
            text = json.dumps(result, ensure_ascii=False)

        elif tool_name == "mem_list":
            list_req = ListRequest(
                since=arguments.get("since"),
                category_filter=arguments.get("category_filter"),
                limit=arguments.get("limit", 50),
            )
            result = await list_memory(list_req)
            text = json.dumps(result, ensure_ascii=False)

        elif tool_name == "mem_delete":
            result = await delete_memory(arguments.get("id", ""))
            text = json.dumps(result, ensure_ascii=False)

        elif tool_name == "mem_categories":
            result = await list_categories()
            text = json.dumps(result, ensure_ascii=False)

        elif tool_name == "mem_stats":
            result = await stats()
            text = json.dumps(result, ensure_ascii=False)

        elif tool_name == "sync_heartbeat":
            hb_req = SyncHeartbeatRequest(
                tool=arguments.get("tool", "unknown"),
                count=arguments.get("count", 0),
            )
            result = await sync_heartbeat(hb_req)
            text = json.dumps(result, ensure_ascii=False)

        elif tool_name == "mem_history":
            memory_id = arguments.get("memory_id", "")
            limit = arguments.get("limit", 20)
            with db_conn() as db:
                history = VersionManager.get_history(db, memory_id, limit)
            text = json.dumps({"memory_id": memory_id, "versions": history}, ensure_ascii=False, default=str)

        elif tool_name == "mem_diff":
            memory_id = arguments.get("memory_id", "")
            version_a = arguments.get("version_a")
            version_b = arguments.get("version_b")
            with db_conn() as db:
                diff_result = VersionManager.get_diff(db, memory_id, version_a, version_b)
            text = json.dumps(diff_result, ensure_ascii=False, default=str)

        elif tool_name == "mem_rollback":
            memory_id = arguments.get("memory_id", "")
            version = arguments.get("version")
            agent = arguments.get("agent", "system")
            with db_conn() as db:
                rollback_result = VersionManager.rollback(db, memory_id, version, agent)
            text = json.dumps(rollback_result, ensure_ascii=False, default=str)

        elif tool_name == "mem_branch":
            memory_id = arguments.get("memory_id", "")
            action = arguments.get("action", "list")
            if action == "create":
                branch_name = arguments.get("branch_name", "")
                from_version = arguments.get("from_version")
                if not branch_name:
                    text = json.dumps({"error": "branch_name required for create"}, ensure_ascii=False)
                else:
                    with db_conn() as db:
                        result = VersionManager.create_branch(
                            db, memory_id, branch_name, from_version,
                            source=arguments.get("source", "system")
                        )
                    text = json.dumps(result, ensure_ascii=False, default=str)
            else:  # list
                with db_conn() as db:
                    branches = VersionManager.list_branches(db, memory_id)
                text = json.dumps({"memory_id": memory_id, "branches": branches}, ensure_ascii=False, default=str)

        elif tool_name == "mem_merge":
            memory_id = arguments.get("memory_id", "")
            source_branch = arguments.get("source_branch", "")
            target_branch = arguments.get("target_branch", "")
            agent = arguments.get("agent", "system")
            with db_conn() as db:
                merge_result = VersionManager.merge_branch(
                    db, memory_id, source_branch, target_branch, agent
                )
            text = json.dumps(merge_result, ensure_ascii=False, default=str)

        elif tool_name == "mem_offload":
            req = OffloadRequest(
                content=arguments.get("content", ""),
                session_id=arguments.get("session_id"),
                source=arguments.get("source", "unknown"),
            )
            result = await offload_memory(req)
            text = json.dumps(result, ensure_ascii=False)

        elif tool_name == "mem_drilldown":
            result = await drilldown_memory(arguments.get("memory_id", ""))
            text = json.dumps(result, ensure_ascii=False)

        elif tool_name == "mem_scenario":
            result = await get_scenario(
                category_id=arguments.get("category_id", "general"),
                days=arguments.get("days", 7),
            )
            text = json.dumps(result, ensure_ascii=False)

        elif tool_name == "mem_persona":
            result = await get_persona(
                persona_type=arguments.get("persona_type", "user"),
                name=arguments.get("name", ""),
            )
            text = json.dumps(result, ensure_ascii=False)

        elif tool_name == "mem_search_hybrid":
            hybrid_req = SearchHybridRequest(
                q=arguments.get("query", arguments.get("q", "")),
                category_filter=arguments.get("category_filter"),
                type_filter=arguments.get("type_filter"),
                limit=arguments.get("limit", 10),
                semantic_weight=arguments.get("semantic_weight", 0.4),
            )
            result = await search_hybrid(hybrid_req)
            text = json.dumps(result, ensure_ascii=False)

        elif tool_name == "mem_cleanup":
            result = await cleanup_memories(CleanupRequest(
                confirm=arguments.get("confirm", False)
            ))
            text = json.dumps(result, ensure_ascii=False)

        elif tool_name == "mem_audit_search":
            result = await audit_search(AuditSearchRequest(
                source=arguments.get("source"),
                since=arguments.get("since"),
                limit=arguments.get("limit", 50),
            ))
            text = json.dumps(result, ensure_ascii=False)

        elif tool_name == "mem_graph":
            term = arguments.get("term", "")
            memory_id = arguments.get("memory_id")
            limit = arguments.get("limit", 10)
            with db_conn() as db:
                if memory_id:
                    # Extract key terms from a specific memory
                    row = db.execute("SELECT content, category_id FROM memories WHERE id=?", (memory_id,)).fetchone()
                    if row:
                        terms = _extract_key_terms(row["content"])
                        graph = {}
                        for t in terms:
                            related = _get_related_terms(db, t, limit)
                            if related:
                                graph[t] = related
                        result = {"memory_id": memory_id, "terms": terms, "graph": graph}
                    else:
                        result = {"error": f"Memory {memory_id} not found"}
                elif term:
                    related = _get_related_terms(db, term, limit)
                    result = {"term": term, "related": related, "count": len(related)}
                else:
                    # Return top N strongest edges in the graph
                    rows = db.execute(
                        "SELECT source_id, target_id, relation, strength FROM memory_relations ORDER BY strength DESC LIMIT ?",
                        (limit,)
                    ).fetchall()
                    result = {"edges": [dict(r) for r in rows], "count": len(rows)}
            text = json.dumps(result, ensure_ascii=False)

        elif tool_name == "mem_dreams":
            action = arguments.get("action", "scan")
            category_filter = arguments.get("category_filter")
            auto_merge = arguments.get("auto_merge", False)

            with db_conn() as db:
                if action == "stats":
                    total = db.execute("SELECT COUNT(*) FROM memories WHERE archived=0").fetchone()[0]
                    relations = db.execute("SELECT COUNT(*) FROM memory_relations").fetchone()[0]
                    # Find potential duplicates via simhash
                    dupes = db.execute(
                        "SELECT COUNT(*) FROM memories WHERE archived=0 AND simhash != ''"
                    ).fetchone()[0]
                    result = {
                        "total_memories": total,
                        "total_relations": relations,
                        "memories_with_simhash": dupes,
                        "health": "good" if total < 500 else "consider_running_scan",
                    }

                elif action == "scan":
                    # Find similar memory pairs (potential duplicates/contradictions)
                    conditions = ["archived=0"]
                    search_params: list[Any] = []
                    if category_filter:
                        conditions.append("category_id=?")
                        search_params.append(category_filter)

                    where = " WHERE " + " AND ".join(conditions)
                    rows = db.execute(
                        f"SELECT id, content, simhash, checksum, category_id, type, priority {where} ORDER BY created_at DESC LIMIT 500",
                        search_params
                    ).fetchall()

                    similar_pairs = []
                    contradictions = []
                    seen_checksums: dict[str, str] = {}

                    for i, r in enumerate(rows):
                        # Exact duplicate detection via checksum
                        if r["checksum"] in seen_checksums:
                            similar_pairs.append({
                                "id1": seen_checksums[r["checksum"]],
                                "id2": r["id"],
                                "reason": "exact_duplicate",
                                "similarity": 1.0
                            })
                        else:
                            seen_checksums[r["checksum"]] = r["id"]

                        # Fuzzy duplicate detection via simhash
                        if r["simhash"]:
                            for j in range(i + 1, min(i + 50, len(rows))):
                                other = rows[j]
                                if other["simhash"]:
                                    dist = hamming_distance(r["simhash"], other["simhash"])
                                    if dist < 8:  # very similar
                                        similarity = round(1.0 - dist / 64, 3)
                                        similar_pairs.append({
                                            "id1": r["id"],
                                            "id2": other["id"],
                                            "reason": "similar_content",
                                            "similarity": similarity,
                                            "distance": dist
                                        })

                        # Contradiction detection: same topic, different type/priority
                        if r["type"] == "decision":
                            for j in range(i + 1, min(i + 30, len(rows))):
                                other = rows[j]
                                if (other["type"] == "decision" and
                                    other["category_id"] == r["category_id"] and
                                    other["id"] != r["id"]):
                                    # Check if content is different but topic same
                                    r_terms = set(_extract_key_terms(r["content"]))
                                    o_terms = set(_extract_key_terms(other["content"]))
                                    overlap = r_terms & o_terms
                                    if len(overlap) >= 2:  # same topic
                                        contradictions.append({
                                            "id1": r["id"],
                                            "id2": other["id"],
                                            "shared_terms": list(overlap)[:5],
                                            "reason": "same_topic_different_decision"
                                        })

                    result = {
                        "scanned": len(rows),
                        "similar_pairs": similar_pairs[:20],
                        "contradictions": contradictions[:10],
                        "suggestions": []
                    }

                    if similar_pairs:
                        result["suggestions"].append(
                            f"发现 {len(similar_pairs)} 对相似记忆，建议合并以减少冗余"
                        )
                    if contradictions:
                        result["suggestions"].append(
                            f"发现 {len(contradictions)} 对潜在矛盾决策，建议人工审核"
                        )
                    if not similar_pairs and not contradictions:
                        result["suggestions"].append("记忆库状态良好，无明显冗余或矛盾")

                elif action == "merge":
                    # Auto-merge memories with similarity > 0.9
                    rows = db.execute(
                        "SELECT id, content, simhash, checksum, category_id FROM memories WHERE archived=0 AND simhash != '' ORDER BY created_at DESC LIMIT 200"
                    ).fetchall()

                    merged = 0
                    merge_log = []
                    seen: dict[str, str] = {}  # checksum -> id

                    for r in rows:
                        # Exact duplicate: archive the newer one
                        if r["checksum"] in seen:
                            db.execute("UPDATE memories SET archived=1 WHERE id=?", (r["id"],))
                            merge_log.append({"archived": r["id"], "kept": seen[r["checksum"]], "reason": "exact_duplicate"})
                            merged += 1
                        else:
                            seen[r["checksum"]] = r["id"]

                        # Fuzzy duplicate with high similarity
                        if r["simhash"] and auto_merge:
                            for j in range(rows.index(r) + 1, min(rows.index(r) + 30, len(rows))):
                                other = rows[j]
                                if other["simhash"] and not other["id"] in [m.get("archived") for m in merge_log]:
                                    dist = hamming_distance(r["simhash"], other["simhash"])
                                    if dist < 4:  # very high similarity (~94%+)
                                        db.execute("UPDATE memories SET archived=1 WHERE id=?", (other["id"],))
                                        merge_log.append({"archived": other["id"], "kept": r["id"], "reason": "fuzzy_duplicate", "distance": dist})
                                        merged += 1

                    if merged > 0:
                        hot_cache.clear()
                        db.execute("INSERT INTO memories_fts(memories_fts) VALUES('rebuild')")

                    result = {
                        "merged": merged,
                        "log": merge_log[:20],
                        "message": f"合并完成：归档 {merged} 条冗余记忆" if merged > 0 else "无需合并，记忆库无冗余"
                    }
                else:
                    result = {"error": f"Unknown action: {action}"}

            text = json.dumps(result, ensure_ascii=False, default=str)

        elif tool_name == "mem_evolve":
            action = arguments.get("action", "analyze")
            days = arguments.get("days", 30)

            with db_conn() as db:
                if action == "analyze":
                    # Analyze memory usage patterns
                    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
                    
                    # Top recalled memories (valuable knowledge)
                    top_recalled = db.execute(
                        "SELECT id, content, type, category_id, recall_count, confidence, priority "
                        "FROM memories WHERE archived=0 AND recall_count > 0 "
                        "ORDER BY recall_count DESC LIMIT 10"
                    ).fetchall()
                    
                    # Never recalled (potentially stale)
                    never_recalled = db.execute(
                        "SELECT id, content, type, category_id, created_at, priority "
                        "FROM memories WHERE archived=0 AND recall_count=0 "
                        "AND created_at < ? ORDER BY created_at ASC LIMIT 10",
                        (cutoff,)
                    ).fetchall()
                    
                    # High confidence (trusted knowledge)
                    high_confidence = db.execute(
                        "SELECT id, content, type, category_id, confidence "
                        "FROM memories WHERE archived=0 AND confidence > 0.9 "
                        "ORDER BY confidence DESC LIMIT 10"
                    ).fetchall()
                    
                    # Category distribution
                    category_dist = {}
                    for row in db.execute(
                        "SELECT category_id, COUNT(*) as c FROM memories WHERE archived=0 GROUP BY category_id"
                    ):
                        category_dist[row["category_id"]] = row["c"]
                    
                    # Type distribution
                    type_dist = {}
                    for row in db.execute(
                        "SELECT type, COUNT(*) as c FROM memories WHERE archived=0 GROUP BY type"
                    ):
                        type_dist[row["type"]] = row["c"]
                    
                    result = {
                        "analysis_period_days": days,
                        "top_recalled": [dict(r) for r in top_recalled],
                        "never_recalled_stale": [dict(r) for r in never_recalled],
                        "high_confidence": [dict(r) for r in high_confidence],
                        "category_distribution": category_dist,
                        "type_distribution": type_dist,
                        "patterns": []
                    }
                    
                    # Detect patterns
                    if len(top_recalled) > 0:
                        avg_recall = sum(r["recall_count"] for r in top_recalled) / len(top_recalled)
                        result["patterns"].append(
                            f"Top {len(top_recalled)} memories have avg recall {avg_recall:.1f} times"
                        )
                    
                    if len(never_recalled) > 5:
                        result["patterns"].append(
                            f"{len(never_recalled)} memories older than {days} days never recalled — consider archiving"
                        )

                elif action == "optimize":
                    # Auto-optimize: promote high-recall memories, demote stale ones
                    promoted = 0
                    demoted = 0
                    
                    # Promote: recall_count >= 5 and P2 → upgrade to P1
                    rows = db.execute(
                        "SELECT id, priority FROM memories WHERE archived=0 AND recall_count >= 5 AND priority = 'P2'"
                    ).fetchall()
                    for r in rows:
                        db.execute("UPDATE memories SET priority='P1', ttl_days=180 WHERE id=?", (r["id"],))
                        promoted += 1
                    
                    # Demote: never recalled in 90 days and P1 → downgrade to P2
                    cutoff_90 = (datetime.now(timezone.utc) - timedelta(days=90)).isoformat()
                    rows = db.execute(
                        "SELECT id FROM memories WHERE archived=0 AND recall_count=0 "
                        "AND created_at < ? AND priority = 'P1'",
                        (cutoff_90,)
                    ).fetchall()
                    for r in rows:
                        db.execute("UPDATE memories SET priority='P2', ttl_days=60 WHERE id=?", (r["id"],))
                        demoted += 1
                    
                    if promoted > 0 or demoted > 0:
                        hot_cache.clear()
                    
                    result = {
                        "promoted_p2_to_p1": promoted,
                        "demoted_p1_to_p2": demoted,
                        "message": f"优化完成：提升 {promoted} 条高频记忆，降级 {demoted} 条冷门记忆"
                    }

                elif action == "insights":
                    # Generate meta-insights about memory usage
                    total = db.execute("SELECT COUNT(*) FROM memories WHERE archived=0").fetchone()[0]
                    avg_confidence = db.execute("SELECT AVG(confidence) FROM memories WHERE archived=0").fetchone()[0] or 0
                    avg_recall = db.execute("SELECT AVG(recall_count) FROM memories WHERE archived=0").fetchone()[0] or 0
                    
                    # Find knowledge gaps: categories with few memories
                    category_counts = {}
                    for row in db.execute(
                        "SELECT category_id, COUNT(*) as c FROM memories WHERE archived=0 GROUP BY category_id"
                    ):
                        category_counts[row["category_id"]] = row["c"]
                    
                    gaps = []
                    for cat_id, count in category_counts.items():
                        if count < 3:
                            gaps.append(f"{cat_id}: 仅 {count} 条记忆，建议补充")
                    
                    # Find stale knowledge
                    stale_cutoff = (datetime.now(timezone.utc) - timedelta(days=60)).isoformat()
                    stale_count = db.execute(
                        "SELECT COUNT(*) FROM memories WHERE archived=0 AND recall_count=0 AND created_at < ?",
                        (stale_cutoff,)
                    ).fetchone()[0]
                    
                    result = {
                        "total_memories": total,
                        "avg_confidence": round(avg_confidence, 3),
                        "avg_recall_count": round(avg_recall, 2),
                        "knowledge_gaps": gaps,
                        "stale_memories_60d": stale_count,
                        "health_score": min(100, int(avg_confidence * 50 + min(avg_recall, 5) * 10)),
                        "recommendations": []
                    }
                    
                    if stale_count > 10:
                        result["recommendations"].append("建议运行 mem_evolve(action='optimize') 清理冷门记忆")
                    if avg_confidence < 0.7:
                        result["recommendations"].append("平均置信度偏低，建议检查低质量记忆来源")
                    if not gaps and stale_count == 0:
                        result["recommendations"].append("记忆库状态优秀，无需优化")
                else:
                    result = {"error": f"Unknown action: {action}"}

            text = json.dumps(result, ensure_ascii=False, default=str)

        elif tool_name == "mem_cache_stats":
            result = await cache_stats()
            text = json.dumps(result, ensure_ascii=False)

        else:
            return {
                "jsonrpc": "2.0",
                "id": request_id,
                "error": {"code": -32601, "message": f"Unknown tool: {tool_name}"}
            }

        return {
            "jsonrpc": "2.0",
            "id": request_id,
            "result": {
                "content": [{"type": "text", "text": text}]
            }
        }

    except Exception as e:
        return {
            "jsonrpc": "2.0",
            "id": request_id,
            "result": {
                "content": [{"type": "text", "text": json.dumps({"error": str(e)}, ensure_ascii=False)}],
                "isError": True
            }
        }


@app.post("/mcp")
async def mcp_endpoint(request: Request) -> JSONResponse:
    """MCP JSON-RPC 2.0 endpoint for protocol-compliant clients."""
    try:
        body = await request.json()
    except Exception:
        return JSONResponse(
            status_code=400,
            content={"jsonrpc": "2.0", "error": {"code": -32700, "message": "Parse error"}}
        )

    method = body.get("method", "")
    request_id = body.get("id")
    params = body.get("params", {})

    # Notifications don't have an id and don't expect a response
    if method == "notifications/initialized":
        return JSONResponse(status_code=200, content={})

    if method == "ping":
        return JSONResponse(content={"jsonrpc": "2.0", "id": request_id, "result": {}})

    if method == "initialize":
        response = await handle_mcp_initialize(request_id, params)
        return JSONResponse(content=response)

    if method == "tools/list":
        response = await handle_mcp_tools_list(request_id, params)
        return JSONResponse(content=response)

    if method == "tools/call":
        response = await handle_mcp_tools_call(request_id, params)
        return JSONResponse(content=response)

    # Unknown method
    return JSONResponse(
        content={
            "jsonrpc": "2.0",
            "id": request_id,
            "error": {"code": -32601, "message": f"Method not found: {method}"}
        }
    )


# ── History ──────────────────────────────────────────────


@app.get("/mcp/history/{memory_id}")
async def history(memory_id: str) -> dict:
    with db_conn() as db:
        rows = db.execute(
            "SELECT action, snapshot, timestamp FROM change_log WHERE memory_id=? ORDER BY timestamp",
            (memory_id,),
        ).fetchall()
    return {
        "success": True,
        "count": len(rows),
        "history": [dict(r) for r in rows],
    }


# ── Batch Save ──────────────────────────────────────────


class BatchSaveRequest(BaseModel):
    memories: list[SaveRequest]


@app.post("/mcp/batch_save")
async def batch_save(req: BatchSaveRequest) -> dict:
    saved = 0
    skipped = 0
    ids = []

    with db_conn() as db:
        for mem in req.memories:
            memory_id = mem.id or str(uuid.uuid4())
            now = now_iso()
            checksum = compute_checksum(mem.content)
            simhash_val = compute_simhash(mem.content)
            mem_type = mem.type or detect_type(mem.content)
            tags_json = json.dumps(mem.tags or [])
            category_id = mem.category_id or "general"

            existing = db.execute(
                "SELECT id FROM memories WHERE checksum=? AND archived=0",
                (checksum,),
            ).fetchone()
            if existing:
                skipped += 1
                continue

            # Fuzzy dedup: check simhash
            near_dup = _find_near_duplicate(db, simhash_val)
            if near_dup:
                skipped += 1
                continue

            # Compute embedding (optional, best-effort)
            embedding_blob = _compute_embedding(mem.content.strip())
            init_clock = json.dumps({mem.source: now})

            db.execute(
                """INSERT INTO memories
                   (id, content, type, scope, source, priority, confidence, tags, category_id,
                    embedding, hot_tier, ttl_days, vector_clock,
                    created_at, updated_at, recall_count, archived, checksum, simhash)
                   VALUES (?, ?, ?, ?, ?, ?, 0.8, ?, ?, ?, ?, ?, ?, ?, ?, 0, 0, ?, ?)""",
                (memory_id, mem.content.strip(), mem_type, mem.scope,
                 mem.source, mem.priority or "P1", tags_json, category_id,
                 embedding_blob,
                 1 if (mem.priority or "P1") == "P0" else 0,
                 DEFAULT_TTL.get(mem.priority or "P1", 0),
                 init_clock,
                 now, now, checksum, simhash_val),
            )

            if mem.session_id:
                db.execute(
                    "INSERT OR IGNORE INTO session_memories (session_id, memory_id, created_at) VALUES (?, ?, ?)",
                    (mem.session_id, memory_id, now),
                )

            db.execute(
                "INSERT INTO change_log (memory_id, action, snapshot, timestamp) VALUES (?, 'save', ?, ?)",
                (memory_id, mem.content.strip(), now),
            )

            # 创建初始版本快照
            VersionManager.create_version(
                db, memory_id, mem.content.strip(),
                change_type="create",
                changed_by=mem.source or "system",
                change_reason="Batch save",
                metadata={"type": mem_type, "category": category_id, "priority": mem.priority or "P1"}
            )

            saved += 1
            ids.append(memory_id)
    return {"success": True, "saved": saved, "skipped": skipped, "ids": ids}


# ── Batch Delete ─────────────────────────────────────────


class BatchDeleteRequest(BaseModel):
    source: str = "system"
    category_id: str = "learning"
    confirm: bool = False


@app.post("/mcp/batch_delete")
async def batch_delete(req: BatchDeleteRequest) -> dict:
    """批量删除记忆（按 source + category）。"""
    if not req.confirm:
        return {"error": "Set confirm=true to proceed with deletion"}

    with db_conn() as db:
        count = db.execute(
            "SELECT COUNT(*) FROM memories WHERE source=? AND category_id=?",
            (req.source, req.category_id),
        ).fetchone()[0]

        if count == 0:
            return {"success": True, "deleted": 0, "message": "No matching memories found"}

        for table in ["session_memories", "change_log", "raw_memories",
                       "memory_versions", "evolution_log", "memory_branches"]:
            db.execute(f"""
                DELETE FROM {table} WHERE memory_id IN
                (SELECT id FROM memories WHERE source=? AND category_id=?)
            """, (req.source, req.category_id))

        db.execute(
            "DELETE FROM memories WHERE source=? AND category_id=?",
            (req.source, req.category_id),
        )

        db.execute("INSERT INTO memories_fts(memories_fts) VALUES('rebuild')")

        log.info("Batch deleted %d memories (source=%s, category=%s)", count, req.source, req.category_id)
        hot_cache.clear()

    return {"success": True, "deleted": count, "source": req.source, "category": req.category_id}


class SetKeyRequest(BaseModel):
    key: str = Field(..., min_length=16, max_length=256)


class LoginRequest(BaseModel):
    key: str = Field(..., min_length=1)


# ── Admin Endpoints ──────────────────────────────────────


@app.post("/admin/login")
async def admin_login(body: LoginRequest, request: Request):
    """Validate API key and set session cookie."""
    if API_KEY and body.key == API_KEY:
        resp = JSONResponse({"success": True})
        resp.set_cookie(
            key=COOKIE_NAME,
            value=API_KEY,
            max_age=86400 * 30,  # 30 days
            httponly=True,
            samesite="lax",
            path="/",
        )
        return resp
    raise HTTPException(status_code=401, detail="密钥无效，请检查 API Key 是否正确")


@app.get("/admin", response_class=HTMLResponse)
async def admin_page(request: Request) -> str:
    """Admin management page for API key operations."""
    # Mask the key: show first 16 + last 4 chars only
    masked = API_KEY[:16] + "..." + API_KEY[-4:] if len(API_KEY) > 24 else "****"
    return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head><meta charset="utf-8"><title>Admin — Memory Gateway v4</title>
<style>
  body {{ font-family: -apple-system, sans-serif; max-width: 700px; margin: 40px auto; padding: 0 20px; color: #e0e0e0; background: #1a1a2e; }}
  h1 {{ color: #00d4ff; }}
  .section {{ margin: 20px 0; padding: 20px; background: #16213e; border-radius: 8px; border-left: 3px solid #00d4ff; }}
  .section h3 {{ margin-top: 0; color: #00d4ff; }}
  code {{ background: #0d1117; padding: 3px 8px; border-radius: 4px; font-size: 14px; word-break: break-all; }}
  .key-display {{ font-family: monospace; background: #0d1117; padding: 10px 16px; border-radius: 6px; word-break: break-all; color: #58a6ff; font-size: 13px; }}
  button {{ background: #00d4ff; color: #1a1a2e; border: none; padding: 10px 20px; border-radius: 6px; cursor: pointer; font-size: 14px; font-weight: bold; margin-right: 8px; }}
  button.danger {{ background: #ff4757; color: #fff; }}
  button:hover {{ opacity: 0.85; }}
  input {{ background: #0d1117; color: #e0e0e0; border: 1px solid #333; padding: 8px 12px; border-radius: 6px; width: 100%; font-size: 14px; }}
  .toast {{ position: fixed; top: 20px; right: 20px; padding: 12px 20px; border-radius: 8px; color: #fff; font-weight: bold; display: none; z-index: 999; }}
  .toast.success {{ background: #2ed573; color: #1a1a2e; }}
  .toast.error {{ background: #ff4757; }}
  .nav {{ margin-bottom: 24px; }}
  .nav a {{ color: #00d4ff; text-decoration: none; margin-right: 20px; padding: 6px 14px; background: #16213e; border-radius: 6px; }}
  .nav a:hover {{ background: #1f3460; }}
</style></head>
<body>
<div class="nav">
  <a href="/">Home</a>
  <a href="/admin" style="background:#1f3460;">Admin</a>
</div>
<h1>Admin</h1>
<div class="section">
  <h3>Current API Key</h3>
  <div class="key-display" id="keyDisplay">{masked}</div>
  <p style="color:#888;font-size:13px;margin-top:8px;">The full key is stored in <code>data/.api_key</code> on the server.</p>
</div>
<div class="section">
  <h3>Rotate Key</h3>
  <p style="color:#aaa;font-size:14px;">Generate a new random key. The old key is <strong>immediately invalidated</strong>. All connected clients must update their config.</p>
  <button id="rotateBtn" onclick="rotateKey()">Rotate Key</button>
  <button class="danger" id="resetBtn" onclick="resetKey()">Reset + Regenerate</button>
</div>
<div class="section">
  <h3>Set Custom Key</h3>
  <p style="color:#aaa;font-size:14px;">Paste your own key (min 16 characters). This replaces the current key immediately.</p>
  <input type="text" id="customKey" placeholder="sk-mg-your-custom-key-min-16-chars..." style="margin-bottom:10px;">
  <button onclick="setCustomKey()">Set Custom Key</button>
</div>
<div id="toast" class="toast"></div>
<script>
const API_KEY_HINT = "{masked}";
const STORED_KEY = localStorage.getItem('memory_gateway_key');
function authHeaders() {{
  const k = STORED_KEY || '';
  return k ? {{'X-API-Key': k, 'Content-Type': 'application/json'}} : {{}};
}}
function showToast(msg, type) {{
  const t = document.getElementById('toast');
  t.textContent = msg;
  t.className = 'toast ' + type;
  t.style.display = 'block';
  setTimeout(() => t.style.display = 'none', 4000);
}}
async function rotateKey() {{
  if (!confirm('Rotate the API key? All current clients will be disconnected and must update their config.')) return;
  try {{
    const r = await fetch('/admin/apikey/rotate', {{method:'POST', headers: authHeaders()}});
    const d = await r.json();
    if (r.ok) {{
      document.getElementById('keyDisplay').textContent = d.key;
      showToast('Key rotated! New key shown above.', 'success');
    }} else {{
      showToast(d.detail || 'Failed', 'error');
    }}
  }} catch(e) {{ showToast('Network error: ' + e.message, 'error'); }}
}}
async function resetKey() {{
  if (!confirm('DELETE the current key and auto-generate a new one? This cannot be undone.')) return;
  try {{
    const r = await fetch('/admin/apikey/reset', {{method:'POST', headers: authHeaders()}});
    const d = await r.json();
    if (r.ok) {{
      document.getElementById('keyDisplay').textContent = d.key;
      showToast('New key generated!', 'success');
    }} else {{
      showToast(d.detail || 'Failed', 'error');
    }}
  }} catch(e) {{ showToast('Network error: ' + e.message, 'error'); }}
}}
async function setCustomKey() {{
  const newKey = document.getElementById('customKey').value.trim();
  if (newKey.length < 16) {{ showToast('Key must be at least 16 characters', 'error'); return; }}
  if (!confirm('Replace the current key with your custom key? All clients will be disconnected.')) return;
  try {{
    const r = await fetch('/admin/apikey/set', {{
      method:'POST',
      headers: authHeaders(),
      body: JSON.stringify({{key: newKey}})
    }});
    const d = await r.json();
    if (r.ok) {{
      document.getElementById('keyDisplay').textContent = d.masked;
      document.getElementById('customKey').value = '';
      showToast('Custom key set!', 'success');
    }} else {{
      showToast(d.detail || 'Failed', 'error');
    }}
  }} catch(e) {{ showToast('Network error: ' + e.message, 'error'); }}
}}
</script>
</body></html>"""


@app.get("/admin/apikey")
async def get_apikey_info() -> dict:
    """Return current API key info (masked)."""
    masked = API_KEY[:16] + "..." + API_KEY[-4:] if len(API_KEY) > 24 else "****"
    return {
        "masked": masked,
        "length": len(API_KEY),
        "source": "environment" if os.environ.get("MEMORY_API_KEY", "").strip() else (
            "file" if KEY_FILE.exists() else "auto-generated"
        ),
    }


@app.post("/admin/apikey/rotate")
async def rotate_apikey() -> dict:
    """Generate a new API key, persist to file, and update runtime.

    The old key is immediately invalidated.
    Environment variable key cannot be rotated — set MEMORY_API_KEY to empty first.
    """
    global API_KEY

    if os.environ.get("MEMORY_API_KEY", "").strip():
        raise HTTPException(
            status_code=400,
            detail="Cannot rotate key set via MEMORY_API_KEY env var. Unset the env var and restart, then rotate."
        )

    new_key = _generate_api_key()
    KEY_FILE.write_text(new_key)
    try:
        os.chmod(KEY_FILE, 0o600)
    except Exception:
        pass
    API_KEY = new_key
    log.warning("API Key rotated — new key saved to %s", KEY_FILE)
    return {"success": True, "key": new_key, "message": "Key rotated. All clients must update."}


@app.post("/admin/apikey/reset")
async def reset_apikey() -> dict:
    """Delete the key file and auto-generate a new key."""
    global API_KEY

    if os.environ.get("MEMORY_API_KEY", "").strip():
        raise HTTPException(
            status_code=400,
            detail="Cannot reset key set via MEMORY_API_KEY env var. Unset the env var and restart."
        )

    if KEY_FILE.exists():
        KEY_FILE.unlink()
    new_key = _generate_api_key()
    KEY_FILE.write_text(new_key)
    try:
        os.chmod(KEY_FILE, 0o600)
    except Exception:
        pass
    API_KEY = new_key
    log.warning("API Key reset — new key saved to %s", KEY_FILE)
    return {"success": True, "key": new_key, "message": "Key reset and regenerated."}


class SetKeyRequest(BaseModel):
    key: str = Field(..., min_length=16, max_length=256)


@app.post("/admin/apikey/set")
async def set_apikey(req: SetKeyRequest) -> dict:
    """Set a custom API key. Replaces the current key immediately."""
    global API_KEY

    if os.environ.get("MEMORY_API_KEY", "").strip():
        raise HTTPException(
            status_code=400,
            detail="Cannot override key set via MEMORY_API_KEY env var."
        )

    new_key = req.key.strip()
    KEY_FILE.write_text(new_key)
    try:
        os.chmod(KEY_FILE, 0o600)
    except Exception:
        pass
    API_KEY = new_key
    masked = new_key[:16] + "..." + new_key[-4:] if len(new_key) > 24 else "****"
    log.warning("API Key manually set — saved to %s", KEY_FILE)
    return {"success": True, "masked": masked, "message": "Custom key set."}


# ── Dashboard ────────────────────────────────────────────

STATIC_DIR = Path(__file__).parent / "static"
if STATIC_DIR.exists():
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


@app.get("/dashboard", response_class=HTMLResponse)
async def dashboard_page():
    dashboard_file = STATIC_DIR / "dashboard.html"
    if not dashboard_file.exists():
        return HTMLResponse("<h1>Dashboard not found</h1>", status_code=404)
    return HTMLResponse(dashboard_file.read_text(encoding="utf-8"))


@app.get("/api/dashboard/overview")
async def dashboard_overview():
    """Return overview data: metrics + category/source distribution + timeline."""
    with db_conn() as db:
        total = db.execute("SELECT COUNT(*) FROM memories").fetchone()[0]
        active = db.execute("SELECT COUNT(*) FROM memories WHERE archived=0").fetchone()[0]
        archived = db.execute("SELECT COUNT(*) FROM memories WHERE archived=1").fetchone()[0]

        # Today new
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        today_new = db.execute(
            "SELECT COUNT(*) FROM memories WHERE created_at LIKE ?", (today + "%",)
        ).fetchone()[0]

        # Active agents (from sync_status with heartbeat within the last 5 mins)
        cutoff = (datetime.now(timezone.utc) - timedelta(minutes=5)).strftime("%Y-%m-%d %H:%M:%S")
        active_agents = db.execute(
            "SELECT COUNT(*) FROM sync_status WHERE last_beat >= ?", (cutoff,)
        ).fetchone()[0]

        # Total versions
        total_versions = db.execute("SELECT COUNT(*) FROM memory_versions").fetchone()[0]

        # By category
        by_category = {}
        for row in db.execute(
            "SELECT COALESCE(c.name, m.category_id) as name, COUNT(m.id) as cnt "
            "FROM memories m LEFT JOIN categories c ON m.category_id = c.id "
            "WHERE m.archived=0 GROUP BY COALESCE(c.name, m.category_id) ORDER BY cnt DESC"
        ):
            by_category[row["name"]] = row["cnt"]

        # By source
        by_source = {}
        for row in db.execute(
            "SELECT source, COUNT(*) as c FROM memories WHERE archived=0 GROUP BY source ORDER BY c DESC"
        ):
            by_source[row["source"] or "unknown"] = row["c"]

        # Timeline (last 30 days)
        timeline = _build_timeline(db, 30)

    return {
        "total": total,
        "active": active,
        "archived": archived,
        "today_new": today_new,
        "active_agents": active_agents,
        "total_versions": total_versions,
        "by_category": by_category,
        "by_source": by_source,
        "timeline": timeline,
    }


@app.get("/api/dashboard/categories")
async def dashboard_categories():
    """Return category list for filters."""
    with db_conn() as db:
        rows = db.execute(
            "SELECT id, name, icon FROM categories ORDER BY sort_order"
        ).fetchall()
        return [dict(r) for r in rows]


@app.get("/api/dashboard/memories")
async def dashboard_memories(
    page: int = 1,
    page_size: int = 20,
    q: Optional[str] = None,
    category: Optional[str] = None,
    source: Optional[str] = None,
    priority: Optional[str] = None,
):
    # 限制 page_size 上界，防止全表扫描
    page_size = min(max(page_size, 1), 200)
    page = max(page, 1)
    """Return paginated memory list with optional filters."""
    offset = (max(1, page) - 1) * page_size
    conditions = ["archived=0"]
    params: list[Any] = []

    if q:
        conditions.append("content LIKE ?")
        params.append(f"%{q}%")
    if category:
        conditions.append("category_id=?")
        params.append(category)
    if source:
        conditions.append("source=?")
        params.append(source)
    if priority:
        conditions.append("priority=?")
        params.append(priority)

    where = " AND ".join(conditions)

    with db_conn() as db:
        total = db.execute(f"SELECT COUNT(*) FROM memories WHERE {where}", params).fetchone()[0]
        rows = db.execute(
            f"SELECT id, content, category_id, type, source, priority, scope, tags, "
            f"created_at, updated_at FROM memories WHERE {where} "
            f"ORDER BY updated_at DESC LIMIT ? OFFSET ?",
            params + [page_size, offset],
        ).fetchall()

    total_pages = max(1, (total + page_size - 1) // page_size)
    return {
        "items": [dict(r) for r in rows],
        "total": total,
        "page": page,
        "page_size": page_size,
        "total_pages": total_pages,
    }


@app.get("/api/dashboard/memories/{memory_id}")
async def dashboard_memory_detail(memory_id: str):
    """Return memory detail + version history."""
    with db_conn() as db:
        row = db.execute(
            "SELECT id, content, category_id, type, source, priority, scope, tags, "
            "simhash, checksum, created_at, updated_at "
            "FROM memories WHERE id=?", (memory_id,)
        ).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Memory not found")

        versions = db.execute(
            "SELECT id, version, change_type, changed_by, change_reason, "
            "diff_from_prev, created_at FROM memory_versions "
            "WHERE memory_id=? ORDER BY version DESC", (memory_id,)
        ).fetchall()

    mem_dict = dict(row)
    return {
        **mem_dict,
        "versions": [dict(v) for v in versions],
    }


@app.get("/api/dashboard/timeline")
async def dashboard_timeline(days: int = 30):
    """Return daily creation counts for the last N days."""
    with db_conn() as db:
        timeline = _build_timeline(db, days)
    return {"timeline": timeline, "days_total": days}


@app.get("/api/dashboard/evolution/{memory_id}")
async def dashboard_evolution(memory_id: str):
    """Return evolution history for a specific memory."""
    with db_conn() as db:
        versions = db.execute(
            "SELECT id, version, content, content_hash, diff_from_prev, "
            "change_type, changed_by, change_reason, metadata_snapshot, created_at "
            "FROM memory_versions WHERE memory_id=? ORDER BY version ASC",
            (memory_id,),
        ).fetchall()

        evolutions = db.execute(
            "SELECT id, event_type, from_version, to_version, agent, details, created_at "
            "FROM evolution_log WHERE memory_id=? ORDER BY created_at ASC",
            (memory_id,),
        ).fetchall()

    return {
        "memory_id": memory_id,
        "versions": [dict(v) for v in versions],
        "evolutions": [dict(e) for e in evolutions],
    }


@app.get("/api/dashboard/health")
async def dashboard_health():
    """Return system health: DB stats, sync status, version stats."""

    with db_conn() as db:
        total = db.execute("SELECT COUNT(*) FROM memories").fetchone()[0]
        active = db.execute("SELECT COUNT(*) FROM memories WHERE archived=0").fetchone()[0]
        archived = db.execute("SELECT COUNT(*) FROM memories WHERE archived=1").fetchone()[0]

        db_size = 0
        try:
            db_size = round(os.path.getsize(str(DB_PATH)) / (1024 * 1024), 2)
        except Exception:
            pass

        sync_rows = db.execute("SELECT * FROM sync_status ORDER BY tool").fetchall()

        total_versions = db.execute("SELECT COUNT(*) FROM memory_versions").fetchone()[0]
        total_evolutions = db.execute("SELECT COUNT(*) FROM evolution_log").fetchone()[0]
        total_relations = db.execute("SELECT COUNT(*) FROM memory_relations").fetchone()[0]
        total_change_log = db.execute("SELECT COUNT(*) FROM change_log").fetchone()[0]

    return {
        "db_stats": {
            "path": str(DB_PATH),
            "size_mb": db_size,
            "total_records": total,
            "active": active,
            "archived": archived,
        },
        "sync": [dict(r) for r in sync_rows],
        "version_stats": {
            "total_versions": total_versions,
            "total_evolutions": total_evolutions,
            "total_relations": total_relations,
            "total_change_log": total_change_log,
        },
    }


# ── Run ──────────────────────────────────────────────────

if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("MEMORY_PORT", "8650"))
    host = os.environ.get("MEMORY_HOST", "0.0.0.0")
    log.info("Starting Memory Gateway v4 on %s:%d", host, port)
    uvicorn.run(app, host=host, port=port, log_level=LOG_LEVEL.lower())

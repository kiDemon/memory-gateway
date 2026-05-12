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
import sqlite3
import sys
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse
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

# ── Database ─────────────────────────────────────────────


def init_db(db: sqlite3.Connection) -> None:
    """Initialize schema with FTS5, triggers, and indexes."""
    db.executescript("""
    PRAGMA journal_mode=WAL;
    PRAGMA foreign_keys=ON;

    CREATE TABLE IF NOT EXISTS memories (
        id          TEXT PRIMARY KEY,
        content     TEXT NOT NULL,
        type        TEXT NOT NULL DEFAULT 'general',
        scope       TEXT NOT NULL DEFAULT 'global',
        source      TEXT NOT NULL DEFAULT 'unknown',
        priority    TEXT NOT NULL DEFAULT 'P1',
        confidence  REAL NOT NULL DEFAULT 0.8,
        tags        TEXT DEFAULT '[]',
        embedding   BLOB,
        created_at  TEXT NOT NULL,
        updated_at  TEXT NOT NULL,
        last_recalled TEXT,
        recall_count INTEGER NOT NULL DEFAULT 0,
        archived    INTEGER NOT NULL DEFAULT 0,
        checksum    TEXT NOT NULL
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

    CREATE VIRTUAL TABLE IF NOT EXISTS memories_fts USING fts5(
        content,
        type,
        scope,
        source,
        tags,
        content=memories,
        content_rowid=rowid,
        tokenize='trigram'
    );

    CREATE TRIGGER IF NOT EXISTS memories_ai AFTER INSERT ON memories BEGIN
        INSERT INTO memories_fts(rowid, content, type, scope, source, tags)
        VALUES (new.rowid, new.content, new.type, new.scope, new.source, new.tags);
    END;

    CREATE TRIGGER IF NOT EXISTS memories_ad AFTER DELETE ON memories BEGIN
        INSERT INTO memories_fts(memories_fts, rowid, content, type, scope, source, tags)
        VALUES ('delete', old.rowid, old.content, old.type, old.scope, old.source, old.tags);
    END;

    CREATE TRIGGER IF NOT EXISTS memories_au AFTER UPDATE ON memories BEGIN
        INSERT INTO memories_fts(memories_fts, rowid, content, type, scope, source, tags)
        VALUES ('delete', old.rowid, old.content, old.type, old.scope, old.source, old.tags);
        INSERT INTO memories_fts(rowid, content, type, scope, source, tags)
        VALUES (new.rowid, new.content, new.type, new.scope, new.source, new.tags);
    END;

    CREATE INDEX IF NOT EXISTS idx_memories_type ON memories(type);
    CREATE INDEX IF NOT EXISTS idx_memories_scope ON memories(scope);
    CREATE INDEX IF NOT EXISTS idx_memories_source ON memories(source);
    CREATE INDEX IF NOT EXISTS idx_memories_archived ON memories(archived);
    CREATE INDEX IF NOT EXISTS idx_memories_created ON memories(created_at);
    CREATE INDEX IF NOT EXISTS idx_memories_checksum ON memories(checksum);
    CREATE INDEX IF NOT EXISTS idx_session_session ON session_memories(session_id);
    """)


def get_db() -> sqlite3.Connection:
    db = sqlite3.connect(str(DB_PATH), timeout=10)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("PRAGMA foreign_keys=ON")
    return db


@contextmanager
def db_conn():
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
}


def compute_checksum(content: str) -> str:
    return hashlib.sha256(content.strip().encode()).hexdigest()[:16]


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
    d.pop("embedding", None)
    return d


# ── Pydantic Models ──────────────────────────────────────


class SaveRequest(BaseModel):
    content: str
    type: Optional[str] = None
    scope: Optional[str] = Field(default="global", pattern="^(global|project|agent)$")
    source: Optional[str] = Field(default="unknown", pattern="^(hermes|claude|workbuddy|system|unknown)$")
    priority: Optional[str] = Field(default="P1", pattern="^(P0|P1|P2)$")
    tags: Optional[list[str]] = None
    session_id: Optional[str] = None
    id: Optional[str] = None


class UpdateRequest(BaseModel):
    content: Optional[str] = None
    type: Optional[str] = None
    scope: Optional[str] = None
    priority: Optional[str] = None
    tags: Optional[list[str]] = None
    archived: Optional[bool] = None


class SearchRequest(BaseModel):
    q: str
    scope_filter: Optional[str] = None
    source_filter: Optional[str] = None
    type_filter: Optional[str] = None
    limit: int = Field(default=10, ge=1, le=100)
    include_archived: bool = False


class ListRequest(BaseModel):
    since: Optional[str] = None
    scope_filter: Optional[str] = None
    source_filter: Optional[str] = None
    limit: int = Field(default=50, ge=1, le=200)
    include_archived: bool = False


# ── FastAPI App ──────────────────────────────────────────

app = FastAPI(
    title="Memory Gateway",
    version="4.0.0",
    description="MCP Memory Server — Hermes + Claude Code + WorkBuddy",
)

# ── API Key Auth ────────────────────────────────────────

API_KEY = os.environ.get("MEMORY_API_KEY", "")


@app.middleware("http")
async def api_key_middleware(request: Request, call_next):
    # Allow health check and root page without auth
    if request.url.path in ("/health", "/"):
        return await call_next(request)

    if API_KEY:
        key = request.headers.get("X-API-Key", "") or request.headers.get("Authorization", "").removeprefix("Bearer ")
        if key != API_KEY:
            return JSONResponse(
                status_code=401,
                content={"error": "Unauthorized", "detail": "Valid X-API-Key header required"},
            )

    return await call_next(request)


@app.on_event("startup")
async def startup() -> None:
    with db_conn() as db:
        init_db(db)
    log.info("Database ready at %s", DB_PATH)
    if API_KEY:
        log.info("API Key authentication enabled")
    else:
        log.warning("No MEMORY_API_KEY set — server is open to all requests")


# ── Health ───────────────────────────────────────────────


@app.get("/health")
async def health() -> dict:
    with db_conn() as db:
        count = db.execute("SELECT COUNT(*) FROM memories WHERE archived=0").fetchone()[0]
    return {"status": "ok", "version": "4.0.0", "memories": count, "db": str(DB_PATH)}


@app.get("/", response_class=HTMLResponse)
async def index() -> str:
    with db_conn() as db:
        stats = _get_stats(db)
    return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head><meta charset="utf-8"><title>Memory Gateway v4</title>
<style>
  body {{ font-family: -apple-system, sans-serif; max-width: 700px; margin: 40px auto; padding: 0 20px; color: #e0e0e0; background: #1a1a2e; }}
  h1 {{ color: #00d4ff; }} .stat {{ margin: 12px 0; padding: 12px 16px; background: #16213e; border-radius: 8px; }}
  .stat strong {{ color: #00d4ff; }} code {{ background: #16213e; padding: 2px 6px; border-radius: 4px; font-size: 14px; }}
  a {{ color: #00d4ff; }} table {{ border-collapse: collapse; width: 100%; }}
  th, td {{ padding: 8px 12px; text-align: left; border-bottom: 1px solid #333; }}
</style></head>
<body>
<h1>Memory Gateway v4</h1>
<div class="stat">Status: <strong>{'running'}</strong></div>
<div class="stat">Total memories: <strong>{stats['total']}</strong></div>
<div class="stat">Active: <strong>{stats['active']}</strong> | Archived: <strong>{stats['archived']}</strong></div>
<div class="stat">By source: {json.dumps(stats['by_source'])}</div>
<div class="stat">By type: {json.dumps(stats['by_type'])}</div>
<h2>MCP Endpoints</h2>
<table>
<tr><th>Method</th><th>Path</th><th>Description</th></tr>
<tr><td>POST</td><td><code>/mcp/save</code></td><td>Save a memory</td></tr>
<tr><td>POST</td><td><code>/mcp/search</code></td><td>Full-text search</td></tr>
<tr><td>POST</td><td><code>/mcp/list</code></td><td>List recent memories</td></tr>
<tr><td>GET</td><td><code>/mcp/get/{id}</code></td><td>Get single memory</td></tr>
<tr><td>PUT</td><td><code>/mcp/update/{id}</code></td><td>Update a memory</td></tr>
<tr><td>DELETE</td><td><code>/mcp/delete/{id}</code></td><td>Delete a memory</td></tr>
<tr><td>GET</td><td><code>/mcp/stats</code></td><td>Server statistics</td></tr>
<tr><td>GET</td><td><code>/mcp/export</code></td><td>Export all active memories as JSON</td></tr>
</table>
</body></html>"""


# ── MCP Endpoints ────────────────────────────────────────


@app.post("/mcp/save")
async def save_memory(req: SaveRequest) -> dict:
    memory_id = req.id or str(uuid.uuid4())
    now = now_iso()
    checksum = compute_checksum(req.content)
    mem_type = req.type or detect_type(req.content)
    tags_json = json.dumps(req.tags or [])

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

        db.execute(
            """INSERT INTO memories
               (id, content, type, scope, source, priority, confidence, tags,
                created_at, updated_at, recall_count, archived, checksum)
               VALUES (?, ?, ?, ?, ?, ?, 0.8, ?, ?, ?, 0, 0, ?)""",
            (memory_id, req.content.strip(), mem_type, req.scope,
             req.source, req.priority or "P1", tags_json, now, now, checksum),
        )

        if req.session_id:
            db.execute(
                "INSERT OR IGNORE INTO session_memories (session_id, memory_id, created_at) VALUES (?, ?, ?)",
                (req.session_id, memory_id, now),
            )

        db.execute(
            "INSERT INTO change_log (memory_id, action, snapshot, timestamp) VALUES (?, 'save', ?, ?)",
            (memory_id, req.content.strip(), now),
        )

    return {"success": True, "action": "saved", "id": memory_id, "type": mem_type}


@app.post("/mcp/search")
async def search_memory(req: SearchRequest) -> dict:
    with db_conn() as db:
        safe_q = req.q.replace('"', '""')

        conditions = ["m.archived=0"]
        params: list[Any] = []

        if not req.include_archived:
            pass
        else:
            conditions[0] = "1=1"

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

        # Short queries (< 3 chars) use LIKE directly — trigram needs >= 3 chars
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
            params = [fts_query] + params + [req.limit]

            try:
                rows = db.execute(sql, params).fetchall()
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
                params_fallback = [like_q] + [p for p in params[1:-1]] + [req.limit]
                rows = db.execute(sql_fallback, params_fallback).fetchall()

        # Update recall stats
        now = now_iso()
        for row in rows:
            db.execute(
                "UPDATE memories SET last_recalled=?, recall_count=recall_count+1 WHERE id=?",
                (now, row["id"]),
            )

    results = [row_to_dict(r) for r in rows]
    return {"success": True, "count": len(results), "results": results}


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

        updates = []
        params: list[Any] = []

        if req.content is not None:
            updates.append("content=?")
            params.append(req.content.strip())
            updates.append("checksum=?")
            params.append(compute_checksum(req.content))
        if req.type is not None:
            updates.append("type=?")
            params.append(req.type)
        if req.scope is not None:
            updates.append("scope=?")
            params.append(req.scope)
        if req.priority is not None:
            updates.append("priority=?")
            params.append(req.priority)
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

    return {"success": True, "action": "updated", "id": memory_id}


@app.delete("/mcp/delete/{memory_id}")
async def delete_memory(memory_id: str) -> dict:
    with db_conn() as db:
        existing = db.execute("SELECT id FROM memories WHERE id=?", (memory_id,)).fetchone()
        if not existing:
            raise HTTPException(status_code=404, detail=f"Memory {memory_id} not found")
        db.execute("DELETE FROM memories WHERE id=?", (memory_id,))
        db.execute(
            "INSERT INTO change_log (memory_id, action, snapshot, timestamp) VALUES (?, 'delete', '', ?)",
            (memory_id, now_iso()),
        )
    return {"success": True, "action": "deleted", "id": memory_id}


# ── Stats & Export ───────────────────────────────────────


def _get_stats(db: sqlite3.Connection) -> dict:
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


@app.get("/mcp/stats")
async def stats() -> dict:
    with db_conn() as db:
        return _get_stats(db)


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
            mem_type = mem.type or detect_type(mem.content)
            tags_json = json.dumps(mem.tags or [])

            existing = db.execute(
                "SELECT id FROM memories WHERE checksum=? AND archived=0",
                (checksum,),
            ).fetchone()
            if existing:
                skipped += 1
                continue

            db.execute(
                """INSERT INTO memories
                   (id, content, type, scope, source, priority, confidence, tags,
                    created_at, updated_at, recall_count, archived, checksum)
                   VALUES (?, ?, ?, ?, ?, ?, 0.8, ?, ?, ?, 0, 0, ?)""",
                (memory_id, mem.content.strip(), mem_type, mem.scope,
                 mem.source, mem.priority or "P1", tags_json, now, now, checksum),
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
            saved += 1
            ids.append(memory_id)

    return {"success": True, "saved": saved, "skipped": skipped, "ids": ids}


# ── Run ──────────────────────────────────────────────────

if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("MEMORY_PORT", "8650"))
    host = os.environ.get("MEMORY_HOST", "0.0.0.0")
    log.info("Starting Memory Gateway v4 on %s:%d", host, port)
    uvicorn.run(app, host=host, port=port, log_level=LOG_LEVEL.lower())

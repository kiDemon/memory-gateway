"""
Memories router - memory CRUD and management API endpoints.

Extracted from server.py (/mcp/save, /mcp/search, /mcp/list, etc.).
"""

import json
import logging
import sqlite3
import time
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from fastapi import APIRouter, HTTPException
from memory_gateway.database.connection import db_conn
from memory_gateway.models.requests import (
    AuditSearchRequest,
    BatchDeleteRequest,
    BatchSaveRequest,
    CheckDuplicatesRequest,
    CleanupRequest,
    ListRequest,
    OffloadRequest,
    RelationRequest,
    SaveRequest,
    SearchHybridRequest,
    SearchRequest,
    SyncHeartbeatRequest,
    UpdateRequest,
)
from memory_gateway.services.version_service import VersionManager
from memory_gateway.utils import now_iso
from memory_gateway.utils.crypto import compute_checksum, compute_simhash, hamming_distance, _find_near_duplicate
from memory_gateway.utils.embedding import _compute_embedding
from memory_gateway.utils.privacy import _filter_sensitive
from memory_gateway.utils.helpers import _build_timeline

from memory_gateway.routers._shared import (
    DEFAULT_TTL,
    DECAY_THRESHOLD,
    HOT_CACHE_MAX,
    HOT_CACHE_TTL,
    _apply_decay,
    _auto_create_relations,
    _compute_confidence,
    _compute_embedding,
    _extract_key_terms,
    _get_related_terms,
    _hybrid_search,
    _sync_hot_tier_from_cache,
    _update_vector_clock,
    detect_type,
    hot_cache,
    row_to_dict,
)

log = logging.getLogger("memory-server")


# ── 保存路径公共逻辑（save / batch_save / smart_save 共用）─────
# 目的：保证多条写入路径行为完全一致（敏感信息过滤、去重指纹、自动标签、
# TTL/hot_tier、insights/血缘/收敛字段），避免路径漂移导致去重失效或字段丢失。


def _prepare_save(req: SaveRequest, now: str, memory_id: str) -> dict:
    """统一的保存前处理。checksum/simhash 基于最终存储内容计算。"""
    content = _filter_sensitive(req.content.strip())
    mem_type = req.type or detect_type(content)
    priority = req.priority or "P1"
    source = req.source or "unknown"
    tags = req.tags
    auto_tagged = False
    if not tags:
        tags = _extract_key_terms(content)[:5]
        auto_tagged = True
    return {
        "id": memory_id,
        "content": content,
        "checksum": compute_checksum(content),
        "simhash": compute_simhash(content),
        "type": mem_type,
        "tags_json": json.dumps(tags),
        "auto_tagged": auto_tagged,
        "category_id": req.category_id or "general",
        "priority": priority,
        "source": source,
        "scope": req.scope,
        "confidence": _compute_confidence(mem_type, source, len(content)),
        "hot_tier": 1 if priority == "P0" or mem_type == "procedural" else 0,
        "ttl_days": DEFAULT_TTL.get(mem_type) or DEFAULT_TTL.get(priority, 0),
        "embedding": _compute_embedding(content),
        "vector_clock": json.dumps({source: now}),
        "insights": req.insights or "",
        "derived_from": json.dumps(req.derived_from) if req.derived_from else None,
        "superseded_by": req.superseded_by,  # 语义 = 本条取代的旧条 ID
        "session_id": req.session_id,
    }


def _insert_memory(db: sqlite3.Connection, p: dict, now: str) -> int:
    """INSERT + 会话关联 + 收敛归档 + change_log + 版本快照 + 图谱关系。

    返回 _auto_create_relations 的图谱边数。
    checksum 唯一索引冲突（并发竞态）时抛 sqlite3.IntegrityError，由调用方决定语义。
    """
    db.execute(
        """INSERT INTO memories
             (id, content, type, scope, source, priority, confidence, tags, category_id,
              embedding, hot_tier, ttl_days, vector_clock,
              created_at, updated_at, recall_count, archived, checksum, simhash,
              insights, derived_from, superseded_by)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, 0, ?, ?, ?, ?, NULL)""",
        (p["id"], p["content"], p["type"], p["scope"], p["source"], p["priority"],
         p["confidence"], p["tags_json"], p["category_id"], p["embedding"],
         p["hot_tier"], p["ttl_days"], p["vector_clock"], now, now,
         p["checksum"], p["simhash"], p["insights"], p["derived_from"]),
    )

    if p["session_id"]:
        db.execute(
            "INSERT OR IGNORE INTO session_memories (session_id, memory_id, created_at) VALUES (?, ?, ?)",
            (p["session_id"], p["id"], now),
        )

    # 写入侧收敛：superseded_by 参数 = 被取代的旧条 ID，
    # 旧条 archive + superseded_by 字段（新条ID）写回旧条，追溯链可读。
    if p["superseded_by"]:
        old = db.execute(
            "SELECT id, archived FROM memories WHERE id=?",
            (p["superseded_by"],),
        ).fetchone()
        if old and old["archived"] == 0:
            db.execute(
                "UPDATE memories SET archived=1, superseded_by=?, updated_at=? WHERE id=?",
                (p["id"], now, p["superseded_by"]),
            )
            db.execute(
                "INSERT INTO change_log (memory_id, action, snapshot, timestamp) "
                "VALUES (?, 'archived_by_supersede', ?, ?)",
                (p["superseded_by"], f"superseded_by={p['id']}", now),
            )
            log.info("save: archived %s (superseded_by=%s)", p["superseded_by"][:8], p["id"][:8])

    db.execute(
        "INSERT INTO change_log (memory_id, action, snapshot, timestamp) VALUES (?, 'save', ?, ?)",
        (p["id"], p["content"], now),
    )

    VersionManager.create_version(
        db, p["id"], p["content"],
        change_type="create",
        changed_by=p["source"],
        change_reason="Initial memory creation",
        metadata={"type": p["type"], "category": p["category_id"], "priority": p["priority"]},
    )

    return _auto_create_relations(db, p["id"], p["content"], p["category_id"])


def _bump_recall(db: sqlite3.Connection, ids: list, now: str, gain: float = 0.02) -> None:
    """检索命中统计：召回 +1、last_recalled 更新、confidence 饱和增长。

    饱和增长（趋近 0.95）：confidence = c + gain*(1-c)，
    避免高频检索导致 confidence 全体通胀到 1.0 而失去区分度。
    """
    if not ids:
        return
    placeholders = ",".join("?" for _ in ids)
    db.execute(
        f"UPDATE memories SET last_recalled=?, recall_count=recall_count+1, "
        f"confidence=MIN(0.95, confidence + ? * (1.0 - confidence)) "
        f"WHERE id IN ({placeholders})",
        [now, gain] + list(ids),
    )

router = APIRouter(tags=["memories"])


# ── Save ──────────────────────────────────────────────────


@router.post("/mcp/save")
async def save_memory(req: SaveRequest) -> dict:
    memory_id = req.id or str(uuid.uuid4())
    now = now_iso()
    p = _prepare_save(req, now, memory_id)
    if p["auto_tagged"]:
        log.info(f"Auto-generated tags for memory {memory_id[:8]}: {json.loads(p['tags_json'])}")
    if (req.source or "unknown") == "unknown":
        log.warning(f"Memory {memory_id[:8]} saved with source='unknown'")

    with db_conn() as db:
        if req.id:
            dup_id = db.execute("SELECT id FROM memories WHERE id=?", (req.id,)).fetchone()
            if dup_id:
                raise HTTPException(status_code=409, detail=f"Memory {req.id} already exists")

        existing = db.execute(
            "SELECT id, checksum FROM memories WHERE checksum=? AND archived=0",
            (p["checksum"],),
        ).fetchone()
        if existing:
            return {
                "success": True,
                "action": "skipped",
                "reason": "duplicate",
                "existing_id": existing["id"],
            }

        near_dup = _find_near_duplicate(db, p["simhash"])
        if near_dup:
            return {
                "success": True,
                "action": "skipped",
                "reason": "near_duplicate",
                "existing_id": near_dup["id"],
                "similarity": near_dup["similarity"],
            }

        try:
            graph_edges = _insert_memory(db, p, now)
        except sqlite3.IntegrityError:
            # checksum 唯一索引兜底并发竞态：同内容并发写入时后到者按重复跳过
            existing = db.execute(
                "SELECT id FROM memories WHERE checksum=? AND archived=0",
                (p["checksum"],),
            ).fetchone()
            return {
                "success": True,
                "action": "skipped",
                "reason": "duplicate",
                "existing_id": existing["id"] if existing else None,
            }

        _sync_hot_tier_from_cache(db)
        hot_cache.clear()  # 新写入后立即失效搜索缓存

    return {
        "success": True,
        "action": "saved",
        "id": memory_id,
        "type": p["type"],
        "graph_edges": graph_edges,
        "archived_superseded": req.superseded_by or None,
    }


# ── Context Offload & 4-Layer Storage ────────────────────


@router.post("/mcp/offload")
async def offload_memory(req: OffloadRequest) -> dict:
    """Unload long text to raw_memories (L0), return index ID."""
    raw_id = str(uuid.uuid4())
    token_count = len(req.content) // 4
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


@router.get("/mcp/drilldown/{memory_id}")
async def drilldown_memory(memory_id: str) -> dict:
    """Drill back to original content (L0) by ID."""
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
    """Get scenario aggregation (L2). Query by category_id and time window."""
    with db_conn() as db:
        rows = db.execute(
            "SELECT id, category_id, title, summary, memory_ids, "
            "time_window_start, time_window_end, created_at, updated_at "
            "FROM scenarios WHERE category_id=? AND created_at >= datetime('now', ? || ' days') "
            "ORDER BY created_at DESC LIMIT 50",
            (category_id, f"-{days}"),
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
    """Get persona (L3). Query by persona_type + name."""
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


# ── Search ────────────────────────────────────────────────


def _escape_like(s: str) -> str:
    """Escape LIKE wildcards (% and _) so they are treated as literal characters."""
    return (
        s.replace("\\", "\\\\")
        .replace("%", "\\%")
        .replace("_", "\\_")
    )


@router.post("/mcp/search")
async def search_memory(req: SearchRequest) -> dict:
    t_start = time.time()
    with db_conn() as db:
        cache_key = f"search:{req.q}:{req.category_filter}:{req.type_filter}:{req.source_filter}:{req.scope_filter}:{req.limit}:{req.include_archived}"
        cached = hot_cache.get(cache_key)
        if cached:
            # 缓存命中也计入召回统计与审计（否则 recall_count 偏低、审计断链）
            now = now_iso()
            cached_ids = [r["id"] for r in cached]
            _bump_recall(db, cached_ids, now)
            db.execute(
                "INSERT INTO search_audit_log "
                "(query, source, result_count, result_ids, latency_ms, search_type, hit_cache) "
                "VALUES (?, ?, ?, ?, ?, 'cache', 1)",
                (req.q, req.source_filter or "unknown", len(cached),
                 json.dumps(cached_ids), round((time.time() - t_start) * 1000, 2)),
            )
            return {
                "success": True,
                "count": len(cached),
                "results": cached,
                "from_cache": True,
                "cache_size": hot_cache.size,
            }

        safe_q = req.q.replace('"', '""')

        # 收敛后默认语义：archived=0 且未被 supersede 的记忆才是"当前生效"。
        # 调用方显式 include_archived=True 时切换到全量视图（含 archive + superseded）。
        conditions = ["m.archived=0", "(m.superseded_by IS NULL OR m.superseded_by='')"]
        params: list[Any] = []

        if req.include_archived:
            conditions[0] = "1=1"
            conditions[1] = "1=1"

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
        q_len = len(req.q)

        # trigram 分词器最少需要 3 个字符才能形成 token：
        # 1-2 字查询（如中文两字词"网关""排班"）走 FTS 只会静默返回空，必须走 LIKE。
        if q_len <= 2:
            escaped_q = _escape_like(req.q)
            prefix_q = f"{escaped_q}%"
            sql = f"""
                SELECT m.*, 0 as rank
                FROM memories m
                WHERE m.content LIKE ? ESCAPE '\\' AND {where}
                ORDER BY m.created_at DESC
                LIMIT ?
            """
            rows = db.execute(sql, [prefix_q] + params + [req.limit]).fetchall()
            if len(rows) < 5:
                like_q = f"%{escaped_q}%"
                sql_wide = f"""
                    SELECT m.*, 0 as rank
                    FROM memories m
                    WHERE m.content LIKE ? ESCAPE '\\' AND {where}
                    ORDER BY m.created_at DESC
                    LIMIT ?
                """
                rows = db.execute(sql_wide, [like_q] + params + [req.limit]).fetchall()
                search_type = "like_wide_short"
            else:
                search_type = "like_prefix_short"

        else:
            fts_query = f'"{safe_q}"*'
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
                search_type = "fts5_prefix"
                if len(rows) < 3:
                    exact_query = f'"{safe_q}"'
                    sql_exact = f"""
                        SELECT m.*, fts.rank
                        FROM memories_fts fts
                        JOIN memories m ON m.rowid = fts.rowid
                        WHERE memories_fts MATCH ? AND {where}
                        ORDER BY fts.rank
                        LIMIT ?
                    """
                    exact_rows = db.execute(sql_exact, [exact_query] + params + [req.limit]).fetchall()
                    if exact_rows:
                        rows = exact_rows
                        search_type = "fts5_exact"
            except sqlite3.OperationalError:
                prefix_q = f"{req.q}%"
                sql_fallback = f"""
                    SELECT m.*, 0 as rank
                    FROM memories m
                    WHERE m.content LIKE ? AND {where}
                    ORDER BY m.created_at DESC
                    LIMIT ?
                """
                rows = db.execute(sql_fallback, [prefix_q] + params + [req.limit]).fetchall()
                if len(rows) < 5:
                    like_q = f"%{req.q}%"
                    sql_wide = f"""
                        SELECT m.*, 0 as rank
                        FROM memories m
                        WHERE m.content LIKE ? AND {where}
                        ORDER BY m.created_at DESC
                        LIMIT ?
                    """
                    rows = db.execute(sql_wide, [like_q] + params + [req.limit]).fetchall()
                    search_type = "like_wide_fallback"
                else:
                    search_type = "like_prefix_fallback"

        query_embedding = _compute_embedding(req.q) if len(req.q) >= 3 else None
        results = _hybrid_search(db, req.q, query_embedding, list(rows),
                                 req.limit, semantic_weight=0.35)

        now = now_iso()
        ids = [r["id"] for r in results]
        _bump_recall(db, ids, now)
        if ids:
            _sync_hot_tier_from_cache(db)

        latency_ms = round((time.time() - t_start) * 1000, 2)
        db.execute(
            """INSERT INTO search_audit_log
               (query, source, result_count, result_ids, latency_ms, search_type, hit_cache)
               VALUES (?, ?, ?, ?, ?, ?, 0)""",
            (req.q, req.source_filter or "unknown", len(results),
             json.dumps(ids[:20]), latency_ms, search_type),
        )

        hot_cache.put(cache_key, results)

    return {
        "success": True,
        "count": len(results),
        "results": results,
        "latency_ms": latency_ms,
        "search_type": search_type,
        "has_embedding": query_embedding is not None,
    }


# ── List ───────────────────────────────────────────────────


@router.post("/mcp/list")
async def list_memory(req: ListRequest) -> dict:
    with db_conn() as db:
        conditions = ["archived=0", "(superseded_by IS NULL OR superseded_by='')"]
        params: list[Any] = []

        if req.include_archived:
            conditions[0] = "1=1"
            conditions[1] = "1=1"

        if req.since:
            # 增量同步：新创建 + 旧条被更新都要拉到，否则更新过的旧记忆永远同步不到
            conditions.append("(updated_at >= ? OR created_at >= ?)")
            params.extend([req.since, req.since])

        if req.category_filter:
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


# ── Check Duplicates ──────────────────────────────────────


@router.post("/mcp/check-duplicates")
async def check_duplicates(req: CheckDuplicatesRequest) -> dict:
    """Batch check for exact and near duplicates."""
    results = []
    with db_conn() as db:
        for item in req.items:
            content = item.strip()
            checksum = compute_checksum(content)
            simhash_val = compute_simhash(content)
            existing = db.execute(
                "SELECT id, content FROM memories WHERE checksum=? AND archived=0",
                (checksum,),
            ).fetchone()
            if existing:
                results.append({
                    "content": content[:200],
                    "status": "exact_duplicate",
                    "existing_id": existing["id"],
                    "similarity": 1.0,
                })
                continue
            near_dup = _find_near_duplicate(db, simhash_val)
            if near_dup:
                results.append({
                    "content": content[:200],
                    "status": "near_duplicate",
                    "existing_id": near_dup["id"],
                    "similarity": near_dup["similarity"],
                })
                continue
            results.append({
                "content": content[:200],
                "status": "unique",
            })
    return {"success": True, "count": len(results), "results": results}


# ── Hybrid Search ─────────────────────────────────────────


@router.post("/mcp/search_hybrid")
async def search_hybrid(req: SearchHybridRequest) -> dict:
    """Hybrid search: combines FTS5 keyword + embedding semantic similarity."""
    t_start = time.time()
    with db_conn() as db:
        safe_q = req.q.replace('"', '""')
        conditions = ["m.archived=0", "(m.superseded_by IS NULL OR m.superseded_by='')"]
        params: list[Any] = []

        if req.include_archived:
            conditions[0] = "1=1"
            conditions[1] = "1=1"

        if req.category_filter:
            conditions.append("m.category_id=?")
            params.append(req.category_filter)
        if req.type_filter:
            conditions.append("m.type=?")
            params.append(req.type_filter)
        if req.scope_filter:
            conditions.append("m.scope=?")
            params.append(req.scope_filter)

        where = " AND ".join(conditions)

        # trigram 最少 3 字符才能成 token：短查询直接 LIKE，FTS 只会静默返回空
        if len(req.q) < 3:
            escaped_q = _escape_like(req.q)
            sql_short = f"""
                SELECT m.*, 0 as rank
                FROM memories m
                WHERE m.content LIKE ? ESCAPE '\\' AND {where}
                ORDER BY m.created_at DESC
                LIMIT ?
            """
            rows = db.execute(sql_short, [f"%{escaped_q}%"] + params + [req.limit]).fetchall()
        else:
            fts_query = f'"{safe_q}"*'
            sql = f"""
                SELECT m.*, fts.rank
                FROM memories_fts fts
                JOIN memories m ON m.rowid = fts.rowid
                WHERE memories_fts MATCH ? AND {where}
                ORDER BY fts.rank
                LIMIT ?
            """
            params_all = [fts_query] + params + [req.limit]

            try:
                rows = db.execute(sql, params_all).fetchall()
            except sqlite3.OperationalError:
                escaped_q = _escape_like(req.q)
                like_q = f"%{escaped_q}%"
                sql_fallback = f"""
                    SELECT m.*, 0 as rank
                    FROM memories m
                    WHERE m.content LIKE ? ESCAPE '\\' AND {where}
                    ORDER BY m.created_at DESC
                    LIMIT ?
                """
                rows = db.execute(sql_fallback, [like_q] + params + [req.limit]).fetchall()

        query_embedding = _compute_embedding(req.q)
        results = _hybrid_search(db, req.q, query_embedding, list(rows),
                                 req.limit, semantic_weight=req.semantic_weight)

        now = now_iso()
        ids = [r["id"] for r in results]
        _bump_recall(db, ids, now)

        # Audit log（列名与 schema 对齐：result_count / created_at，此前写入永远失败被吞）
        latency_ms = round((time.time() - t_start) * 1000, 2)
        try:
            db.execute(
                "INSERT INTO search_audit_log "
                "(query, source, result_count, result_ids, latency_ms, search_type, hit_cache) "
                "VALUES (?, ?, ?, ?, ?, 'hybrid', 0)",
                (req.q, req.source_filter or "unknown", len(results),
                 json.dumps(ids), latency_ms),
            )
        except sqlite3.Error:
            log.warning("search_hybrid: audit log write failed", exc_info=True)

    return {
        "success": True,
        "count": len(results),
        "results": results,
        "latency_ms": latency_ms,
        "has_embedding": query_embedding is not None,
    }


# ── Audit Search Log ──────────────────────────────────────


@router.post("/mcp/audit/search")
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


# ── Cleanup ────────────────────────────────────────────────


@router.post("/mcp/cleanup")
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
    hot_cache.clear()
    return {"success": True, "stats": stats, "cache_cleared": True}


# ── Cache Stats ────────────────────────────────────────────


@router.get("/mcp/cache/stats")
async def cache_stats() -> dict:
    """Get hot cache statistics."""
    return {
        "success": True,
        "cache_size": hot_cache.size,
        "cache_max": HOT_CACHE_MAX,
        "cache_ttl_seconds": HOT_CACHE_TTL,
    }


# ── Get, Update, Delete ───────────────────────────────────


@router.get("/mcp/get/{memory_id}")
async def get_memory(memory_id: str) -> dict:
    with db_conn() as db:
        row = db.execute("SELECT * FROM memories WHERE id=?", (memory_id,)).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail=f"Memory {memory_id} not found")
    mem = row_to_dict(row)
    # row_to_dict 为控制列表/搜索响应体积会剔除 insights；
    # 单条读取时显式带回（自我蒸馏的结论需要可读可追溯）
    mem["insights"] = row["insights"] if "insights" in row.keys() else None
    return {"success": True, "memory": mem}


@router.put("/mcp/update/{memory_id}")
async def update_memory(memory_id: str, req: UpdateRequest) -> dict:
    with db_conn() as db:
        existing = db.execute("SELECT * FROM memories WHERE id=?", (memory_id,)).fetchone()
        if not existing:
            raise HTTPException(status_code=404, detail=f"Memory {memory_id} not found")

        vc_result = _update_vector_clock(db, memory_id, "api")

        updates = []
        params: list[Any] = []

        content_val = _filter_sensitive(req.content.strip()) if req.content is not None else ""
        if req.content is not None:
            updates.append("content=?")
            params.append(content_val)
            updates.append("checksum=?")
            params.append(compute_checksum(content_val))
            updates.append("simhash=?")
            params.append(compute_simhash(content_val))
            embedding_blob = _compute_embedding(content_val)
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
            eff_type = req.type or existing["type"]
            updates.append("priority=?")
            params.append(req.priority)
            updates.append("ttl_days=?")
            params.append(DEFAULT_TTL.get(eff_type) or DEFAULT_TTL.get(req.priority, 0))
            updates.append("hot_tier=?")
            params.append(1 if req.priority == "P0" or eff_type == "procedural" else 0)
        if req.category_id is not None:
            updates.append("category_id=?")
            params.append(req.category_id)
        if req.tags is not None:
            updates.append("tags=?")
            params.append(json.dumps(req.tags))
        if req.insights is not None:
            updates.append("insights=?")
            params.append(req.insights)
        if req.archived is not None:
            updates.append("archived=?")
            params.append(1 if req.archived else 0)

        if not updates:
            return {"success": True, "action": "no_changes"}

        updates.append("updated_at=?")
        params.append(now_iso())

        sql = f"UPDATE memories SET {', '.join(updates)} WHERE id=?"
        params.append(memory_id)
        try:
            db.execute(sql, params)
        except sqlite3.IntegrityError:
            # checksum 唯一约束：更新后内容与现存活跃记忆重复
            raise HTTPException(status_code=409, detail="checksum 与现存活跃记忆重复（去重约束）")

        db.execute(
            "INSERT INTO change_log (memory_id, action, snapshot, timestamp) VALUES (?, 'update', ?, ?)",
            (memory_id, content_val, now_iso()),
        )

        if req.content is not None:
            VersionManager.create_version(
                db, memory_id, content_val,
                change_type="update",
                changed_by="api",
                change_reason="Content updated via API"
            )

        hot_cache.clear()

    return {
        "success": True,
        "action": "updated",
        "id": memory_id,
        "vector_clock": vc_result,
    }


@router.delete("/mcp/delete/{memory_id}")
async def delete_memory(memory_id: str) -> dict:
    with db_conn() as db:
        existing = db.execute("SELECT id FROM memories WHERE id=?", (memory_id,)).fetchone()
        if not existing:
            raise HTTPException(status_code=404, detail=f"Memory {memory_id} not found")

        db.execute("DELETE FROM session_memories WHERE memory_id=?", (memory_id,))
        db.execute("DELETE FROM change_log WHERE memory_id=?", (memory_id,))
        db.execute("DELETE FROM raw_memories WHERE memory_id=?", (memory_id,))
        db.execute("DELETE FROM memory_relations WHERE source_id=? OR target_id=?", (memory_id, memory_id))
        # resolved_contradictions 的 FK 无 ON DELETE CASCADE，不清理会 IntegrityError 500
        db.execute("DELETE FROM resolved_contradictions WHERE memory_id_a=? OR memory_id_b=?", (memory_id, memory_id))
        db.execute("DELETE FROM memories WHERE id=?", (memory_id,))

        log.info(f"Memory {memory_id[:8]}... deleted with all related data")
        hot_cache.clear()
    return {"success": True, "action": "deleted", "id": memory_id}


# ── Export ─────────────────────────────────────────────────


@router.get("/mcp/export")
async def export_memories(scope: Optional[str] = None, source: Optional[str] = None) -> dict:
    with db_conn() as db:
        # export 默认与 search/list 保持一致：当前生效层（不包含已 superseded 的活跃条）
        conditions = ["archived=0", "(superseded_by IS NULL OR superseded_by='')"]
        params: list[Any] = []
        if scope:
            conditions.append("scope=?")
            params.append(scope)
        if source:
            conditions.append("source=?")
            params.append(source)
        where = " AND ".join(conditions)
        sql = f"SELECT * FROM memories WHERE {where} ORDER BY created_at DESC"
        rows = db.execute(sql, params).fetchall()
    return {"success": True, "count": len(rows), "memories": [row_to_dict(r) for r in rows]}


# ── Stats ──────────────────────────────────────────────────


def _get_stats(db: sqlite3.Connection) -> dict:
    """Return statistics about stored memories."""
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


@router.get("/mcp/stats")
async def stats() -> dict:
    with db_conn() as db:
        base_stats = _get_stats(db)
        by_category = {}
        for row in db.execute(
            "SELECT category_id, COUNT(*) as c FROM memories WHERE archived=0 GROUP BY category_id"
        ):
            by_category[row["category_id"]] = row["c"]
        by_priority = {}
        for row in db.execute(
            "SELECT priority, COUNT(*) as c FROM memories WHERE archived=0 GROUP BY priority"
        ):
            by_priority[row["priority"]] = row["c"]
        sync_healthy = db.execute(
            "SELECT COUNT(*) FROM sync_status WHERE status='healthy'"
        ).fetchone()[0]
        sync_total = db.execute("SELECT COUNT(*) FROM sync_status").fetchone()[0]
        relation_count = db.execute("SELECT COUNT(*) FROM memory_relations").fetchone()[0]

        base_stats["by_category"] = by_category
        base_stats["by_priority"] = by_priority
        base_stats["sync"] = {"healthy": sync_healthy, "total": sync_total}
        base_stats["relations"] = relation_count

        hot_count = db.execute(
            "SELECT COUNT(*) FROM memories WHERE archived=0 AND hot_tier=1"
        ).fetchone()[0]
        cold_count = db.execute(
            "SELECT COUNT(*) FROM memories WHERE archived=0 AND hot_tier=0"
        ).fetchone()[0]
        by_hot_tier = {"hot": hot_count, "cold": cold_count}
        archived_count = db.execute(
            "SELECT COUNT(*) FROM memories WHERE archived=1"
        ).fetchone()[0]
        audit_total = db.execute(
            "SELECT COUNT(*) FROM search_audit_log"
        ).fetchone()[0]
        base_stats["tier"] = by_hot_tier
        base_stats["decay"] = {"archived_total": archived_count, "audit_logs": audit_total}
    return base_stats


# ── Sync Status ──────────────────────────────────────────


@router.get("/mcp/sync/status")
async def get_sync_status() -> dict:
    """Get synchronization status for all tools."""
    with db_conn() as db:
        rows = db.execute("SELECT * FROM sync_status ORDER BY tool").fetchall()
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
                log.warning("Failed to compute sync status age, defaulting to unknown", exc_info=True)
                row_dict["status"] = "unknown"
            results.append(row_dict)
    return {"success": True, "sync_status": results}


@router.post("/mcp/sync/heartbeat")
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


# ── Relations ─────────────────────────────────────────────


@router.post("/mcp/relations")
async def create_relation(req: RelationRequest) -> dict:
    """Create a relation between two memories."""
    with db_conn() as db:
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


@router.get("/mcp/relations/{memory_id}")
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


@router.delete("/mcp/relations/{source_id}/{target_id}")
async def delete_relation(source_id: str, target_id: str) -> dict:
    """Delete a relation between two memories."""
    with db_conn() as db:
        db.execute(
            "DELETE FROM memory_relations WHERE source_id=? AND target_id=?",
            (source_id, target_id),
        )
    return {"success": True, "action": "deleted"}


# ── Graph ──────────────────────────────────────────────────


@router.get("/mcp/graph")
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


# ── History ────────────────────────────────────────────────


@router.get("/mcp/history/{memory_id}")
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


# ── Batch Save ─────────────────────────────────────────────


@router.post("/mcp/batch_save")
async def batch_save(req: BatchSaveRequest) -> dict:
    saved = 0
    skipped = 0
    ids = []
    now = now_iso()

    with db_conn() as db:
        for mem in req.memories:
            memory_id = mem.id or str(uuid.uuid4())
            # 统一走 _prepare_save + _insert_memory：与 /mcp/save 行为完全一致
            # （敏感信息过滤、自动标签、TTL/hot_tier、insights/血缘/收敛、图谱、版本）
            p = _prepare_save(mem, now, memory_id)

            existing = db.execute(
                "SELECT id FROM memories WHERE checksum=? AND archived=0",
                (p["checksum"],),
            ).fetchone()
            if existing:
                skipped += 1
                continue

            near_dup = _find_near_duplicate(db, p["simhash"])
            if near_dup:
                skipped += 1
                continue

            try:
                _insert_memory(db, p, now)
            except sqlite3.IntegrityError:
                # checksum 唯一索引兜底：批内/并发重复按跳过处理
                skipped += 1
                continue

            ids.append(memory_id)
            saved += 1

        if saved > 0:
            _sync_hot_tier_from_cache(db)
            hot_cache.clear()  # 批量写入后失效搜索缓存
    return {"success": True, "saved": saved, "skipped": skipped, "ids": ids}


# ── Batch Delete ───────────────────────────────────────────


@router.post("/mcp/batch_delete")
async def batch_delete(req: BatchDeleteRequest) -> dict:
    """Batch delete memories by source + category."""
    if not req.confirm:
        return {"error": "Set confirm=true to proceed with deletion"}

    with db_conn() as db:
        rows = db.execute(
            "SELECT id FROM memories WHERE source=? AND category_id=?",
            (req.source, req.category_id),
        ).fetchall()

        count = len(rows)
        if count == 0:
            return {"success": True, "deleted": 0, "message": "No matching memories found"}

        id_params = [(row["id"],) for row in rows]

        for table in ["session_memories", "change_log", "raw_memories",
                       "memory_versions", "evolution_log", "memory_branches"]:
            db.executemany(
                f"DELETE FROM {table} WHERE memory_id=?", id_params
            )

        db.executemany("DELETE FROM memories WHERE id=?", id_params)
        log.info(f"Batch deleted {count} memories (source={req.source}, category={req.category_id})")
        hot_cache.clear()  # 清除搜索缓存

    return {"success": True, "deleted": count}


# ── Lint ──────────────────────────────────────────────────


async def lint_memories() -> dict:
    """Check memory health and return issues found."""
    from memory_gateway.routers._shared import _extract_key_terms

    now = datetime.now(timezone.utc)
    issues = {
        "stale": [],
        "empty_tags": [],
        "zero_recall": [],
        "unknown_source": [],
        "high_conf_no_recall": [],
    }

    with db_conn() as db:
        # Get all non-archived memories
        rows = db.execute(
            "SELECT id, content, source, priority, confidence, tags, created_at, "
            "last_recalled, recall_count, archived, superseded_by "
            "FROM memories WHERE archived=0"
        ).fetchall()

        total_checked = len(rows)
        log.info(f"mem_lint: scanning {total_checked} active memories")

        # ── 0. superseded_but_active (收敛遗留脏数据) ──
        superseded_active_rows = db.execute(
            "SELECT id, content, superseded_by FROM memories "
            "WHERE archived=0 AND superseded_by IS NOT NULL AND superseded_by != ''"
        ).fetchall()
        for r in superseded_active_rows:
            issues.setdefault("superseded_but_active", []).append({
                "id": r["id"],
                "superseded_by": r["superseded_by"],
                "content_preview": r["content"][:120],
            })

        # ── 2. Stale (archived=0, priority!=P0, last_recalled=NULL, created > 90d) ──
        stale_cutoff = now - timedelta(days=90)
        stale_iso = stale_cutoff.isoformat()
        stale_rows = db.execute(
            "SELECT id, content, created_at FROM memories "
            "WHERE archived=0 AND priority!='P0' AND last_recalled IS NULL "
            "AND created_at < ?",
            (stale_iso,),
        ).fetchall()
        for r in stale_rows:
            created = datetime.fromisoformat(r["created_at"])
            days_idle = (now - created).days
            issues["stale"].append({
                "id": r["id"],
                "content_preview": r["content"][:120],
                "days_idle": days_idle,
            })

        # ── 3. Empty tags ──
        empty_tag_rows = db.execute(
            "SELECT id, content FROM memories WHERE archived=0 AND (tags IS NULL OR tags='[]')"
        ).fetchall()
        for r in empty_tag_rows:
            issues["empty_tags"].append({
                "id": r["id"],
                "content_preview": r["content"][:120],
            })

        # ── 4. Zero recall (archived=0, recall_count=0, created > 30d) ──
        zero_recall_cutoff = now - timedelta(days=30)
        zero_recall_iso = zero_recall_cutoff.isoformat()
        zero_rows = db.execute(
            "SELECT id, content, created_at FROM memories "
            "WHERE archived=0 AND recall_count=0 AND created_at < ?",
            (zero_recall_iso,),
        ).fetchall()
        for r in zero_rows:
            issues["zero_recall"].append({
                "id": r["id"],
                "content_preview": r["content"][:120],
                "created_at": r["created_at"],
            })

        # ── 6. Unknown source ──
        unknown_rows = db.execute(
            "SELECT id, content FROM memories WHERE archived=0 AND source='unknown'"
        ).fetchall()
        for r in unknown_rows:
            issues["unknown_source"].append({
                "id": r["id"],
                "content_preview": r["content"][:120],
            })

        # ── 7. High confidence, zero recall ──
        high_conf_rows = db.execute(
            "SELECT id, content, confidence FROM memories "
            "WHERE archived=0 AND confidence > 0.9 AND recall_count=0"
        ).fetchall()
        for r in high_conf_rows:
            issues["high_conf_no_recall"].append({
                "id": r["id"],
                "confidence": round(r["confidence"], 4),
                "content_preview": r["content"][:120],
            })

        # ── 1. Orphaned memories ──
        # Extract key terms from each memory and check if any appear in memory_relations.
        # Memories whose extracted terms have no entries in the graph are orphaned.
        orphaned = []
        graph_terms = set()
        for row in db.execute(
            "SELECT DISTINCT source_id FROM memory_relations "
            "UNION SELECT DISTINCT target_id FROM memory_relations"
        ):
            graph_terms.add(row[0])

        for r in rows:
            terms = _extract_key_terms(r["content"])
            if terms and not any(t in graph_terms for t in terms):
                orphaned.append({
                    "id": r["id"],
                    "content_preview": r["content"][:120],
                })

        if orphaned:
            issues["orphaned"] = orphaned

    total_issues = sum(len(v) for v in issues.values())
    return {
        "success": True,
        "summary": {
            "total_checked": total_checked,
            "issues_found": total_issues,
        },
        "issues": issues,
    }

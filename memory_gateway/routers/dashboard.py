"""
Dashboard endpoints.

Routes:
  /dashboard              — Main dashboard page (HTML)
  /api/dashboard/*        — Dashboard data APIs
"""

import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

from fastapi import APIRouter, HTTPException
from fastapi.responses import HTMLResponse

from memory_gateway.config import DB_PATH, STATIC_DIR, log
from memory_gateway.database.connection import db_conn
from memory_gateway.utils import _build_timeline

router = APIRouter()


# ── Dashboard Page ────────────────────────────────────────


@router.get("/dashboard", response_class=HTMLResponse)
async def dashboard_page():
    dashboard_file = STATIC_DIR / "dashboard.html"
    if not dashboard_file.exists():
        return HTMLResponse("<h1>Dashboard not found</h1>", status_code=404)
    return HTMLResponse(dashboard_file.read_text(encoding="utf-8"))


# ── Dashboard API Data Endpoints ──────────────────────────


@router.get("/api/dashboard/overview")
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


@router.get("/api/dashboard/categories")
async def dashboard_categories():
    """Return category list for filters."""
    with db_conn() as db:
        rows = db.execute(
            "SELECT id, name, icon FROM categories ORDER BY sort_order"
        ).fetchall()
        return [dict(r) for r in rows]


@router.get("/api/dashboard/memories")
async def dashboard_memories(
    page: int = 1,
    page_size: int = 20,
    q: Optional[str] = None,
    category: Optional[str] = None,
    source: Optional[str] = None,
    priority: Optional[str] = None,
):
    """Return paginated memory list with optional filters.

    普通关键词优先走 FTS5；UUID 精确 id 查询；FTS 失败回退 LIKE。
    """
    import re

    # 限制 page_size 上界，防止全表扫描
    page_size = min(max(page_size, 1), 200)
    page = max(page, 1)
    offset = (max(1, page) - 1) * page_size
    conditions = ["m.archived=0"]
    params: list[Any] = []
    join_fts = ""
    order_by = "m.updated_at DESC"

    if q:
        q_stripped = q.strip()
        uuid_pattern = r'^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
        if re.match(uuid_pattern, q_stripped, re.IGNORECASE):
            conditions.append("m.id=?")
            params.append(q_stripped)
        else:
            # FTS5 安全转义：双引号包裹 + 前缀匹配
            safe = q_stripped.replace('"', '""')
            fts_query = f'"{safe}"*' if len(safe) >= 2 else f'"{safe}"'
            join_fts = "JOIN memories_fts fts ON fts.rowid = m.rowid"
            conditions.append("fts MATCH ?")
            params.append(fts_query)
            order_by = "bm25(fts) ASC, m.updated_at DESC"
    if category:
        conditions.append("m.category_id=?")
        params.append(category)
    if source:
        conditions.append("m.source=?")
        params.append(source)
    if priority:
        conditions.append("m.priority=?")
        params.append(priority)

    where = " AND ".join(conditions)
    select_cols = (
        "m.id, m.content, m.category_id, m.type, m.source, m.priority, m.scope, m.tags, "
        "m.created_at, m.updated_at"
    )

    with db_conn() as db:
        try:
            total = db.execute(
                f"SELECT COUNT(*) FROM memories m {join_fts} WHERE {where}",
                params,
            ).fetchone()[0]
            rows = db.execute(
                f"SELECT {select_cols} FROM memories m {join_fts} WHERE {where} "
                f"ORDER BY {order_by} LIMIT ? OFFSET ?",
                params + [page_size, offset],
            ).fetchall()
        except Exception as e:
            # FTS 不可用或语法失败 → 回退 LIKE（不带 fts join）
            log.warning("dashboard FTS search failed, fallback LIKE: %s", e)
            conditions = ["archived=0"]
            params = []
            if q:
                q_stripped = q.strip()
                uuid_pattern = r'^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
                if re.match(uuid_pattern, q_stripped, re.IGNORECASE):
                    conditions.append("id=?")
                    params.append(q_stripped)
                else:
                    conditions.append("content LIKE ?")
                    params.append(f"%{q_stripped}%")
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
            total = db.execute(f"SELECT COUNT(*) FROM memories WHERE {where}", params).fetchone()[0]
            rows = db.execute(
                f"SELECT id, content, category_id, type, source, priority, scope, tags, "
                f"created_at, updated_at FROM memories WHERE {where} "
                f"ORDER BY updated_at DESC LIMIT ? OFFSET ?",
                params + [page_size, offset],
            ).fetchall()

    items = []
    for r in rows:
        d = dict(r)
        if "category_id" in d and "category" not in d:
            d["category"] = d["category_id"]
        items.append(d)

    total_pages = max(1, (total + page_size - 1) // page_size)
    return {
        "items": items,
        "total": total,
        "page": page,
        "page_size": page_size,
        "total_pages": total_pages,
    }


@router.get("/api/dashboard/memories/{memory_id}")
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


@router.get("/api/dashboard/timeline")
async def dashboard_timeline(days: int = 30):
    """Return daily creation counts for the last N days."""
    with db_conn() as db:
        timeline = _build_timeline(db, days)
    return {"timeline": timeline, "days_total": days}


@router.get("/api/dashboard/evolution/{memory_id}")
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


@router.get("/api/dashboard/graph")
async def dashboard_graph(limit: int = 200):
    """Return knowledge graph data for D3.js visualization.

    Returns nodes (terms) and edges (relations) suitable for a force-directed graph.
    Degree 用单次 GROUP BY 聚合，避免 N+1。
    """
    with db_conn() as db:
        rows = db.execute(
            """SELECT source_id, target_id, relation, strength
               FROM memory_relations
               ORDER BY strength DESC LIMIT ?""",
            (limit,),
        ).fetchall()
        edges = [dict(r) for r in rows]

        node_ids = set()
        for e in edges:
            node_ids.add(e["source_id"])
            node_ids.add(e["target_id"])

        # 一次聚合拿 degree（两端都算）
        degree_map: dict[str, int] = {}
        if node_ids:
            for row in db.execute(
                """SELECT term, COUNT(*) AS cnt FROM (
                       SELECT source_id AS term FROM memory_relations
                       UNION ALL
                       SELECT target_id AS term FROM memory_relations
                   ) GROUP BY term"""
            ):
                if row["term"] in node_ids:
                    degree_map[row["term"]] = row["cnt"]

        nodes = [{"id": term, "degree": degree_map.get(term, 0)} for term in node_ids]
        total_edges = db.execute("SELECT COUNT(*) FROM memory_relations").fetchone()[0]
        total_terms = len(node_ids)

    return {
        "nodes": nodes,
        "edges": edges,
        "stats": {"total_terms": total_terms, "total_edges": total_edges, "showing": len(edges)},
    }


@router.get("/api/dashboard/health")
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

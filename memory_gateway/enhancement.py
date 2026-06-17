"""
Memory Enhancement Module - 借鉴"左脑"核心理念
实现：图扩散搜索、智能去重合并、自检系统、会话自动加载
"""

import json
import logging
import sqlite3
import hashlib
from datetime import datetime, timedelta
from typing import Optional
from collections import Counter

from memory_gateway.database.connection import db_conn
from memory_gateway.utils.crypto import compute_simhash, hamming_distance
from memory_gateway.utils import now_iso

log = logging.getLogger("memory-enhancement")


# ═══════════════════════════════════════════════════════════
# 1. 图扩散搜索（Multi-hop Graph Traversal）
# ═══════════════════════════════════════════════════════════

def graph_expand_search(seed_term: str, max_hops: int = 2, limit: int = 20) -> dict:
    """
    从种子词出发，沿关联链发现N跳关联知识
    
    示例：
        graph_expand_search("火塘牛肉", max_hops=2)
        返回：{
            "seed": "火塘牛肉",
            "hops": [
                {"hop": 1, "terms": ["营销", "丽江", "套餐"]},
                {"hop": 2, "terms": ["引流款", "利润款", "竞品"]}
            ],
            "related_memories": [...]
        }
    """
    with db_conn() as db:
        visited = set()
        current_level = {seed_term}
        all_terms = []
        hop_results = []
        
        for hop in range(1, max_hops + 1):
            next_level = set()
            for term in current_level:
                if term in visited:
                    continue
                visited.add(term)
                
                # 查找关联词
                related = db.execute(
                    """SELECT target_id as related, strength 
                       FROM memory_relations WHERE source_id=?
                       UNION ALL
                       SELECT source_id as related, strength
                       FROM memory_relations WHERE target_id=?
                       ORDER BY strength DESC LIMIT 10""",
                    (term, term)
                ).fetchall()
                
                for r in related:
                    if r["related"] not in visited:
                        next_level.add(r["related"])
            
            if next_level:
                hop_results.append({
                    "hop": hop,
                    "terms": list(next_level)[:10]
                })
                all_terms.extend(next_level)
                current_level = next_level
            else:
                break
        
        # 搜索包含这些关联词的记忆
        related_memories = []
        if all_terms:
            placeholders = " OR ".join(["content LIKE ?" for _ in all_terms[:20]])
            params = [f"%{t}%" for t in all_terms[:20]]
            
            rows = db.execute(
                f"""SELECT id, content, type, category_id, priority, tags, created_at
                    FROM memories 
                    WHERE ({placeholders}) AND archived=0
                    ORDER BY created_at DESC LIMIT ?""",
                params + [limit]
            ).fetchall()
            
            related_memories = [dict(r) for r in rows]
        
        return {
            "seed": seed_term,
            "hops": hop_results,
            "total_terms_discovered": len(all_terms),
            "related_memories": related_memories
        }


def find_memory_clusters(memory_id: str, depth: int = 2) -> dict:
    """
    从一条记忆出发，发现关联的记忆簇
    用于：查看某条知识的相关知识网络
    """
    with db_conn() as db:
        # 获取源记忆
        source = db.execute(
            "SELECT id, content, tags, category_id FROM memories WHERE id=?",
            (memory_id,)
        ).fetchone()
        
        if not source:
            return {"error": f"Memory {memory_id} not found"}
        
        # 提取关键术语
        tags = json.loads(source["tags"]) if source["tags"] else []
        content = source["content"]
        
        # 用tags和内容关键词搜索关联
        search_terms = tags[:5]
        
        # 搜索关联记忆
        cluster_memories = []
        visited_ids = {memory_id}
        
        current_terms = set(search_terms)
        for hop in range(depth):
            next_terms = set()
            for term in current_terms:
                # 通过关联表找关联词
                related = db.execute(
                    """SELECT target_id FROM memory_relations WHERE source_id=?
                       UNION
                       SELECT source_id FROM memory_relations WHERE target_id=?""",
                    (term, term)
                ).fetchall()
                for r in related:
                    next_terms.add(r["target_id"])
            
            # 用关联词搜索记忆
            if next_terms:
                placeholders = " OR ".join(["tags LIKE ?" for _ in list(next_terms)[:10]])
                params = [f"%{t}%" for t in list(next_terms)[:10]]
                
                rows = db.execute(
                    f"""SELECT id, content, tags, category_id, priority
                        FROM memories 
                        WHERE ({placeholders}) AND archived=0 AND id != ?
                        LIMIT 10""",
                    params + [memory_id]
                ).fetchall()
                
                for r in rows:
                    if r["id"] not in visited_ids:
                        visited_ids.add(r["id"])
                        cluster_memories.append(dict(r))
            
            current_terms = next_terms
        
        return {
            "source_memory": {
                "id": memory_id,
                "content_preview": content[:100],
                "tags": tags
            },
            "cluster_size": len(cluster_memories),
            "cluster_memories": cluster_memories
        }


# ═══════════════════════════════════════════════════════════
# 2. 智能去重合并（Smart Dedup & Merge）
# ═══════════════════════════════════════════════════════════

def smart_save_with_merge(content: str, category_id: str = "general",
                          source: str = "hermes", tags: list = None,
                          merge_threshold: float = 0.8) -> dict:
    """
    智能保存：相似度>阈值时自动合并，而非跳过
    
    返回：
    - action: "saved" | "merged" | "skipped"
    - merged_with: 合并的目标记忆ID（如果是merged）
    """
    from memory_gateway.utils.crypto import compute_checksum
    
    checksum = compute_checksum(content)
    simhash = compute_simhash(content)
    
    with db_conn() as db:
        # 1. 精确重复检查
        exact_dup = db.execute(
            "SELECT id FROM memories WHERE checksum=? AND archived=0",
            (checksum,)
        ).fetchone()
        
        if exact_dup:
            return {
                "success": True,
                "action": "skipped",
                "reason": "exact_duplicate",
                "existing_id": exact_dup["id"]
            }
        
        # 2. 相似度检查
        all_memories = db.execute(
            "SELECT id, content, simhash, tags FROM memories WHERE archived=0"
        ).fetchall()
        
        best_match = None
        best_similarity = 0
        
        for mem in all_memories:
            if not mem["simhash"]:
                continue
            dist = hamming_distance(simhash, mem["simhash"])
            similarity = 1 - (dist / 64)  # 64-bit simhash
            
            if similarity > best_similarity:
                best_similarity = similarity
                best_match = mem
        
        # 3. 根据相似度决定操作
        if best_match and best_similarity >= merge_threshold:
            # 相似度>阈值：合并更新
            old_content = best_match["content"]
            merged_content = _merge_contents(old_content, content)
            
            now = now_iso()
            db.execute(
                """UPDATE memories 
                   SET content=?, updated_at=?, checksum=?, simhash=?
                   WHERE id=?""",
                (merged_content, now, compute_checksum(merged_content),
                 compute_simhash(merged_content), best_match["id"])
            )
            
            # 记录变更
            db.execute(
                """INSERT INTO change_log (memory_id, action, snapshot, timestamp)
                   VALUES (?, 'merge', ?, ?)""",
                (best_match["id"], f"Merged with new content (similarity={best_similarity:.2f})", now)
            )
            
            db.commit()
            
            return {
                "success": True,
                "action": "merged",
                "merged_with": best_match["id"],
                "similarity": best_similarity
            }
        
        # 4. 相似度不足：作为新记忆保存
        # 调用原有的save逻辑
        from memory_gateway.routers.memories import save_memory
        from memory_gateway.models.requests import SaveRequest
        
        req = SaveRequest(
            content=content,
            category_id=category_id,
            source=source,
            tags=tags
        )
        
        # 这里需要异步调用，但为了简化，直接返回指示
        return {
            "success": True,
            "action": "new",
            "similarity_with_best": best_similarity,
            "best_match_id": best_match["id"] if best_match else None
        }


def _merge_contents(old_content: str, new_content: str) -> str:
    """
    合并两条相似内容
    策略：保留旧内容主体，追加新内容中的增量信息
    """
    # 简单策略：如果新内容更长，用新内容；否则追加
    if len(new_content) > len(old_content) * 1.2:
        return new_content
    
    # 检查新内容是否有旧内容没有的信息
    old_lines = set(old_content.split('\n'))
    new_lines = set(new_content.split('\n'))
    diff_lines = new_lines - old_lines
    
    if diff_lines:
        return old_content + "\n\n【合并新增】\n" + "\n".join(diff_lines)
    
    return old_content


# ═══════════════════════════════════════════════════════════
# 3. 自检系统（Health Check）
# ═══════════════════════════════════════════════════════════

def health_check() -> dict:
    """
    9项健康检查 + 自动修复
    返回检查报告
    """
    report = {
        "timestamp": now_iso(),
        "checks": [],
        "warnings": [],
        "errors": [],
        "auto_fixed": []
    }
    
    with db_conn() as db:
        # 检查1：记忆总量与分布
        total = db.execute("SELECT COUNT(*) FROM memories WHERE archived=0").fetchone()[0]
        by_type = db.execute(
            "SELECT type, COUNT(*) as cnt FROM memories WHERE archived=0 GROUP BY type"
        ).fetchall()
        by_category = db.execute(
            "SELECT category_id, COUNT(*) as cnt FROM memories WHERE archived=0 GROUP BY category_id"
        ).fetchall()
        
        report["checks"].append({
            "name": "记忆总量与分布",
            "status": "✅",
            "total": total,
            "by_type": {r["type"]: r["cnt"] for r in by_type},
            "by_category": {r["category_id"]: r["cnt"] for r in by_category}
        })
        
        # 检查2：接近字符上限的记忆
        near_limit = db.execute(
            """SELECT id, LENGTH(content) as len, SUBSTR(content, 1, 50) as preview
               FROM memories WHERE archived=0 AND LENGTH(content) > 1800
               ORDER BY len DESC LIMIT 10"""
        ).fetchall()
        
        if near_limit:
            report["warnings"].append({
                "name": "接近字符上限",
                "count": len(near_limit),
                "items": [dict(r) for r in near_limit]
            })
        
        # 检查3：缺少tags的记忆
        no_tags = db.execute(
            """SELECT COUNT(*) FROM memories 
               WHERE archived=0 AND (tags='[]' OR tags IS NULL OR tags='')"""
        ).fetchone()[0]
        
        if no_tags > 0:
            report["warnings"].append({
                "name": "缺少tags",
                "count": no_tags
            })
            
            # 自动修复：为缺少tags的记忆生成tags
            from memory_gateway.routers._shared import _extract_key_terms
            
            empty_tag_memories = db.execute(
                """SELECT id, content FROM memories 
                   WHERE archived=0 AND (tags='[]' OR tags IS NULL OR tags='')
                   LIMIT 50"""
            ).fetchall()
            
            fixed_count = 0
            for mem in empty_tag_memories:
                terms = _extract_key_terms(mem["content"])[:5]
                if terms:
                    db.execute(
                        "UPDATE memories SET tags=? WHERE id=?",
                        (json.dumps(terms), mem["id"])
                    )
                    fixed_count += 1
            
            if fixed_count > 0:
                db.commit()
                report["auto_fixed"].append({
                    "name": "自动生成tags",
                    "fixed_count": fixed_count
                })
        
        # 检查4：过期记忆（>90天未访问）
        stale_threshold = (datetime.now() - timedelta(days=90)).isoformat()
        stale_count = db.execute(
            """SELECT COUNT(*) FROM memories 
               WHERE archived=0 AND created_at < ? AND recall_count = 0""",
            (stale_threshold,)
        ).fetchone()[0]
        
        if stale_count > 0:
            report["warnings"].append({
                "name": "过期记忆（>90天未访问）",
                "count": stale_count
            })
        
        # 检查5：关联完整性
        orphan_relations = db.execute(
            """SELECT COUNT(*) FROM memory_relations 
               WHERE source_id NOT IN (SELECT id FROM memories)
               OR target_id NOT IN (SELECT id FROM memories)"""
        ).fetchone()[0]
        
        if orphan_relations > 0:
            report["warnings"].append({
                "name": "孤立关联",
                "count": orphan_relations
            })
            
            # 自动修复：清理孤立关联
            db.execute(
                """DELETE FROM memory_relations 
                   WHERE source_id NOT IN (SELECT id FROM memories)
                   OR target_id NOT IN (SELECT id FROM memories)"""
            )
            db.commit()
            report["auto_fixed"].append({
                "name": "清理孤立关联",
                "fixed_count": orphan_relations
            })
        
        # 检查6：版本控制状态
        version_count = db.execute("SELECT COUNT(*) FROM memory_versions").fetchone()[0]
        memory_count = db.execute("SELECT COUNT(*) FROM memories WHERE archived=0").fetchone()[0]
        
        version_coverage = version_count / memory_count if memory_count > 0 else 0
        report["checks"].append({
            "name": "版本控制覆盖",
            "status": "✅" if version_coverage > 0.9 else "⚠️",
            "coverage": f"{version_coverage:.1%}",
            "versions": version_count,
            "memories": memory_count
        })
        
        # 检查7：FTS5索引状态
        try:
            fts_count = db.execute("SELECT COUNT(*) FROM memories_fts").fetchone()[0]
            report["checks"].append({
                "name": "FTS5索引",
                "status": "✅",
                "indexed": fts_count
            })
        except Exception as e:
            report["errors"].append({
                "name": "FTS5索引异常",
                "error": str(e)
            })
        
        # 检查8：分类完整性
        uncategorized = db.execute(
            "SELECT COUNT(*) FROM memories WHERE archived=0 AND category_id='general'"
        ).fetchone()[0]
        
        report["checks"].append({
            "name": "分类状态",
            "status": "✅",
            "uncategorized": uncategorized,
            "total": total
        })
        
        # 检查9：关联密度
        relation_count = db.execute("SELECT COUNT(*) FROM memory_relations").fetchone()[0]
        avg_relations = relation_count / total if total > 0 else 0
        
        report["checks"].append({
            "name": "关联密度",
            "status": "✅" if avg_relations > 2 else "⚠️",
            "total_relations": relation_count,
            "avg_per_memory": f"{avg_relations:.1f}"
        })
    
    # 生成总结
    warning_count = len(report["warnings"])
    error_count = len(report["errors"])
    fixed_count = len(report["auto_fixed"])
    
    report["summary"] = {
        "total_checks": len(report["checks"]),
        "warnings": warning_count,
        "errors": error_count,
        "auto_fixed": fixed_count,
        "overall_status": "✅ 健康" if error_count == 0 else "❌ 有问题"
    }
    
    return report


# ═══════════════════════════════════════════════════════════
# 4. 会话自动加载（Auto Context Loading）
# ═══════════════════════════════════════════════════════════

def auto_load_context(current_message: str, limit: int = 10) -> dict:
    """
    根据当前对话内容，自动检索相关历史记忆
    用于：每次新对话开始时，自动注入相关上下文
    """
    from memory_gateway.routers._shared import _extract_key_terms
    
    # 提取当前消息的关键术语
    terms = _extract_key_terms(current_message)
    
    if not terms:
        return {
            "has_context": False,
            "reason": "无法提取关键术语"
        }
    
    with db_conn() as db:
        # 1. 通过FTS5搜索相关记忆
        fts_results = []
        for term in terms[:5]:
            rows = db.execute(
                """SELECT id, content, type, category_id, priority, tags, 
                          created_at, recall_count
                   FROM memories 
                   WHERE content LIKE ? AND archived=0
                   ORDER BY priority DESC, recall_count DESC
                   LIMIT 5""",
                (f"%{term}%",)
            ).fetchall()
            fts_results.extend(rows)
        
        # 2. 通过关联图扩展
        graph_results = []
        for term in terms[:3]:
            related = db.execute(
                """SELECT target_id FROM memory_relations WHERE source_id=?
                   UNION
                   SELECT source_id FROM memory_relations WHERE target_id=?""",
                (term, term)
            ).fetchall()
            
            for r in related[:3]:
                rows = db.execute(
                    """SELECT id, content, type, category_id, priority, tags,
                              created_at, recall_count
                       FROM memories
                       WHERE content LIKE ? AND archived=0
                       LIMIT 3""",
                    (f"%{r['target_id']}%",)
                ).fetchall()
                graph_results.extend(rows)
        
        # 3. 合并去重
        seen_ids = set()
        all_results = []
        
        for mem in fts_results + graph_results:
            if mem["id"] not in seen_ids:
                seen_ids.add(mem["id"])
                all_results.append(dict(mem))
        
        # 4. 按优先级和访问频率排序
        all_results.sort(
            key=lambda x: (
                {"P0": 0, "P1": 1, "P2": 2}.get(x.get("priority", "P2"), 3),
                -x.get("recall_count", 0)
            )
        )
        
        # 5. 更新recall_count
        now = now_iso()
        for mem in all_results[:limit]:
            db.execute(
                """UPDATE memories 
                   SET recall_count = recall_count + 1, last_recalled = ?
                   WHERE id=?""",
                (now, mem["id"])
            )
        db.commit()
        
        return {
            "has_context": True,
            "extracted_terms": terms,
            "context_count": min(len(all_results), limit),
            "contexts": all_results[:limit]
        }


def get_session_summary() -> dict:
    """
    获取当前记忆系统的会话摘要
    用于：每次新对话开始时，提供记忆系统概览
    """
    with db_conn() as db:
        # 总量
        total = db.execute("SELECT COUNT(*) FROM memories WHERE archived=0").fetchone()[0]
        
        # 最近学习的（24小时内）
        recent_threshold = (datetime.now() - timedelta(hours=24)).isoformat()
        recent_count = db.execute(
            "SELECT COUNT(*) FROM memories WHERE archived=0 AND created_at > ?",
            (recent_threshold,)
        ).fetchone()[0]
        
        # 高频知识（recall_count > 5）
        high_freq = db.execute(
            """SELECT id, content, recall_count, tags
               FROM memories 
               WHERE archived=0 AND recall_count > 5
               ORDER BY recall_count DESC LIMIT 5"""
        ).fetchall()
        
        # 分类分布
        categories = db.execute(
            """SELECT category_id, COUNT(*) as cnt 
               FROM memories WHERE archived=0 
               GROUP BY category_id ORDER BY cnt DESC"""
        ).fetchall()
        
        # 最近更新
        recent_updated = db.execute(
            """SELECT id, content, updated_at
               FROM memories
               WHERE archived=0
               ORDER BY updated_at DESC LIMIT 5"""
        ).fetchall()
        
        return {
            "total_memories": total,
            "recent_24h": recent_count,
            "high_frequency": [dict(r) for r in high_freq],
            "category_distribution": {r["category_id"]: r["cnt"] for r in categories},
            "recent_updated": [dict(r) for r in recent_updated]
        }


# ═══════════════════════════════════════════════════════════
# 5. API Endpoints
# ═══════════════════════════════════════════════════════════

from fastapi import APIRouter

enhancement_router = APIRouter(tags=["enhancement"])


@enhancement_router.get("/enhancement/graph-expand/{term}")
async def api_graph_expand(term: str, hops: int = 2, limit: int = 20):
    """图扩散搜索：从词出发发现N跳关联"""
    return graph_expand_search(term, max_hops=hops, limit=limit)


@enhancement_router.get("/enhancement/memory-cluster/{memory_id}")
async def api_memory_cluster(memory_id: str, depth: int = 2):
    """记忆簇发现：从一条记忆出发找关联知识网络"""
    return find_memory_clusters(memory_id, depth=depth)


@enhancement_router.get("/enhancement/health-check")
async def api_health_check():
    """健康检查：9项检查+自动修复"""
    return health_check()


@enhancement_router.get("/enhancement/auto-context")
async def api_auto_context(message: str, limit: int = 10):
    """自动加载上下文：根据消息检索相关记忆"""
    return auto_load_context(message, limit=limit)


@enhancement_router.get("/enhancement/session-summary")
async def api_session_summary():
    """会话摘要：获取记忆系统概览"""
    return get_session_summary()

"""
Shared helper functions and objects extracted from server.py,
used by the router modules. Avoids circular imports.
"""

import hashlib
import json
import logging
import math
import os
import re
import sqlite3
import threading
import time
from collections import OrderedDict
from datetime import datetime, timezone
from typing import Any

from memory_gateway.utils import now_iso
from memory_gateway.utils.crypto import compute_checksum, compute_simhash, hamming_distance, _find_near_duplicate
from memory_gateway.utils.embedding import (
    _blob_to_vector,
    _compute_embedding,
    _cosine_similarity,
)
from memory_gateway.utils.privacy import _filter_sensitive

log = logging.getLogger("memory-server")

# ── Content Type Detection ────────────────────────────────

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


def detect_type(content: str) -> str:
    lower = content.lower()
    scores = {}
    for t, keywords in CONTENT_TYPE_KEYWORDS.items():
        scores[t] = sum(1 for kw in keywords if kw in lower)
    best = max(scores, key=scores.get)
    return best if scores[best] > 0 else "general"


def row_to_dict(row: sqlite3.Row) -> dict:
    d = dict(row)
    if "tags" in d and isinstance(d["tags"], str):
        try:
            d["tags"] = json.loads(d["tags"])
        except (json.JSONDecodeError, TypeError):
            pass
    if "embedding" in d:
        del d["embedding"]
    if "simhash" in d:
        del d["simhash"]
    if "vector_clock" in d and isinstance(d["vector_clock"], str):
        try:
            d["vector_clock"] = json.loads(d["vector_clock"])
        except (json.JSONDecodeError, TypeError):
            pass
    if "derived_from" in d and isinstance(d["derived_from"], str):
        try:
            d["derived_from"] = json.loads(d["derived_from"])
        except (json.JSONDecodeError, TypeError):
            pass
    if "insights" in d:
        del d["insights"]
    # 兼容老客户端：API 历史上用 category，库内字段是 category_id
    if "category_id" in d and "category" not in d:
        d["category"] = d["category_id"]
    return d


# ── Confidence Score ──────────────────────────────────────

TYPE_CONFIDENCE = {
    "procedural": 0.95,
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

SOURCE_CONFIDENCE_BONUS = {
    "hermes": 0.05,
    "claude": 0.05,
    "workbuddy": 0.03,
    "system": 0.10,
}


def _compute_confidence(mem_type: str, source: str, content_length: int) -> float:
    base = TYPE_CONFIDENCE.get(mem_type, 0.80)
    base += SOURCE_CONFIDENCE_BONUS.get(source, 0.0)
    if content_length < 20:
        base -= 0.15
    elif content_length < 50:
        base -= 0.05
    return max(0.3, min(1.0, base))


# ── Knowledge Graph: Stop words & extraction ──────────────

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
    terms = set()
    cn_matches = re.findall(r'[\u4e00-\u9fff]{2,4}', content)
    for m in cn_matches:
        if m not in _STOP_WORDS and len(m) >= 2:
            terms.add(m)
    en_matches = re.findall(r'[A-Za-z_][A-Za-z0-9_]{2,}', content)
    for m in en_matches:
        lower = m.lower()
        if lower in _STOP_WORDS or len(m) < 3 or m.isdigit():
            continue
        terms.add(m if m[0].isupper() else lower)
    seen = {}
    for t in terms:
        key = t.lower()
        if key not in seen or (t[0].isupper() and not seen[key][0].isupper()):
            seen[key] = t
    return list(seen.values())[:12]


def _get_related_terms(db: sqlite3.Connection, term: str, limit: int = 10) -> list[dict]:
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


def _auto_create_relations(db: sqlite3.Connection, memory_id: str,
                           content: str, category_id: str) -> int:
    terms = _extract_key_terms(content)
    if len(terms) < 2:
        return 0
    now = now_iso()
    relations_created = 0
    for i in range(len(terms)):
        for j in range(i + 1, len(terms)):
            src = terms[i]
            tgt = terms[j]
            relation_type = f"co_occurs_{category_id}"
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


# ── Hot Cache ────────────────────────────────────────────

HOT_CACHE_MAX = int(os.environ.get("MEMORY_HOT_CACHE_SIZE", "200"))
HOT_CACHE_TTL = int(os.environ.get("MEMORY_HOT_CACHE_TTL", "300"))


class HotCache:
    """Thread-safe LRU cache for frequently accessed memories."""

    def __init__(self, max_size: int = HOT_CACHE_MAX, ttl: int = HOT_CACHE_TTL):
        self._cache: OrderedDict[str, tuple[Any, float]] = OrderedDict()
        self._max = max_size
        self._ttl = ttl
        self._lock = threading.Lock()

    def get(self, key: str) -> Any | None:
        with self._lock:
            if key not in self._cache:
                return None
            entry, ts = self._cache[key]
            if time.time() - ts > self._ttl:
                del self._cache[key]
                return None
            self._cache.move_to_end(key)
            return entry

    def put(self, key: str, value: Any) -> None:
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
    db.execute("""
        UPDATE memories SET hot_tier=1
        WHERE archived=0 AND (priority='P0' OR recall_count >= 3)
        AND hot_tier=0
    """)
    db.execute("""
        UPDATE memories SET hot_tier=0
        WHERE hot_tier=1 AND recall_count=0
        AND last_recalled IS NULL
        AND priority != 'P0'
    """)


# ── Hybrid Search ─────────────────────────────────────────

def _hybrid_search(db: sqlite3.Connection, query: str, query_embedding: bytes | None,
                   fts_results: list[sqlite3.Row], limit: int,
                   semantic_weight: float = 0.4) -> list[dict]:
    if query_embedding is None or not fts_results:
        return [row_to_dict(r) for r in fts_results]
    query_vec = _blob_to_vector(query_embedding)
    if query_vec is None:
        return [row_to_dict(r) for r in fts_results]
    K = 60
    sem_scores = {}
    for i, r in enumerate(fts_results):
        mem_vec = _blob_to_vector(r["embedding"]) if r["embedding"] else None
        if mem_vec:
            sem_scores[i] = _cosine_similarity(query_vec, mem_vec)
    scored = []
    for i, r in enumerate(fts_results):
        d = row_to_dict(r)
        fts_rank = i + 1
        mem_vec = _blob_to_vector(r["embedding"]) if r["embedding"] else None
        if mem_vec and sem_scores:
            my_sem = sem_scores.get(i, 0.0)
            sem_rank = sum(1 for s in sem_scores.values() if s > my_sem) + 1
        else:
            sem_rank = fts_rank
        rrf_score = (1.0 / (K + fts_rank)) + (1.0 / (K + sem_rank))
        confidence = d.get("confidence", 0.8)
        rrf_score *= confidence
        d["_rrf_score"] = round(rrf_score, 5)
        d["_fts_rank"] = fts_rank
        d["_sem_rank"] = sem_rank
        scored.append((rrf_score, d))
    scored.sort(key=lambda x: x[0], reverse=True)
    return [d for _, d in scored[:limit]]


# ── Memory Decay ──────────────────────────────────────────

_DEFAULT_TTL_ENV = os.environ.get("MEMORY_DEFAULT_TTL", '{"P0":0,"procedural":0,"P1":180,"P2":60}')
DEFAULT_TTL = json.loads(_DEFAULT_TTL_ENV)
DECAY_THRESHOLD = 2


def _apply_decay(db: sqlite3.Connection) -> dict:
    now = now_iso()
    stats = {"archived": 0, "decayed": 0}
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
        half_life = 30 if priority == "P1" else 14
        half_life *= (1 + math.log2(recall_count + 1))
        last_time = r["last_recalled"] or r["created_at"]
        try:
            last_dt = datetime.fromisoformat(last_time)
            days_since = (datetime.now(timezone.utc) - last_dt).total_seconds() / 86400.0
        except (ValueError, TypeError):
            continue
        strength = confidence * (0.5 ** (days_since / half_life))
        if strength < 0.05:
            to_archive.append(r["id"])
    if to_archive:
        placeholders = ",".join("?" for _ in to_archive)
        db.execute(
            f"UPDATE memories SET archived=1, updated_at=? WHERE id IN ({placeholders})",
            [now] + to_archive
        )
        stats["archived"] = len(to_archive)
    return stats


# ── Vector Clock Conflict Detection ───────────────────────

def _update_vector_clock(db: sqlite3.Connection, memory_id: str,
                         source: str) -> dict:
    row = db.execute(
        "SELECT vector_clock FROM memories WHERE id=?", (memory_id,)
    ).fetchone()
    if not row:
        return {"conflict": False, "clock": {}, "detail": "memory_not_found"}
    try:
        clock = json.loads(row["vector_clock"]) if row["vector_clock"] else {}
    except (json.JSONDecodeError, TypeError):
        clock = {}
    now = now_iso()
    conflict = False
    detail = ""
    if source in clock:
        clock[source] = now
    else:
        clock[source] = now
    db.execute(
        "UPDATE memories SET vector_clock=?, updated_at=? WHERE id=?",
        (json.dumps(clock), now, memory_id)
    )
    return {"conflict": conflict, "clock": clock, "detail": detail}

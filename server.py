#!/usr/bin/env python3
"""
MCP Memory Server — Hermes + Claude Code + WorkBuddy 统一记忆系统
部署: Docker / 本地
协议: MCP over HTTP (StreamableHTTP)
存储: SQLite + FTS5
"""

import hashlib
import json
import logging
import os
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
from fastapi.middleware.cors import CORSMiddleware

from memory_gateway.config import DATA_DIR, DB_PATH, KEY_FILE, LOG_LEVEL, log
from memory_gateway.database.connection import get_db, db_conn
from memory_gateway.database.schema import init_db
from memory_gateway.middleware import (
    api_key_middleware,
    security_headers_middleware,
    _create_session,
    _validate_session,
    _delete_session,
    _is_ip_locked,
    _record_failure,
    _clear_failures,
    login_page_html,
    _is_same_origin,
    COOKIE_NAME,
    SESSION_DURATION,
    LOCKOUT_DURATION,
    _sessions_cache,
    _locked_ips_cache,
)
from memory_gateway.models.requests import (
    AuditSearchRequest,
    BatchDeleteRequest,
    BatchSaveRequest,
    CategoryRequest,
    CategoryUpdateRequest,
    CheckDuplicatesRequest,
    CleanupRequest,
    DrilldownRequest,
    ListRequest,
    LoginRequest,
    OffloadRequest,
    RelationRequest,
    SaveRequest,
    SearchHybridRequest,
    SearchRequest,
    SetKeyRequest,
    SkillExtractRequest,
    SkillListRequest,
    SkillMatchRequest,
    SkillSaveRequest,
    SyncHeartbeatRequest,
    UpdateRequest,
)
from memory_gateway.services.version_service import VersionManager
from memory_gateway.utils import now_iso
from memory_gateway.utils.helpers import _build_timeline, _generate_api_key


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
# (PRIVACY_PATTERNS and _filter_sensitive moved to memory_gateway/utils/privacy.py)
from memory_gateway.utils.privacy import PRIVACY_PATTERNS, _filter_sensitive


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



# (compute_checksum, compute_simhash, hamming_distance, _find_near_duplicate moved to memory_gateway/utils/crypto.py)
from memory_gateway.utils.crypto import compute_checksum, compute_simhash, hamming_distance, _find_near_duplicate


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
    "比较", "非常", "特别", "主要", "基本", "目前", "现在", "今天",
    "昨天", "明天", "最近", "刚才", "马上", "立刻", "一直", "从来",
}

# jieba 分词器（延迟加载）
_jieba = None

# 自定义词典
_CUSTOM_WORDS = [
    "Memory Gateway", "Hermes Agent", "Claude Code", "WorkBuddy",
    "低空目标", "铁塔工匠", "智联业务", "应急预案", "代维",
    "GIS可视化", "塔娃AI", "腾讯元宝", "知识图谱",
    "备电评估", "微信工单", "4A权限", "管控系统",
]

def _get_jieba():
    """延迟加载 jieba 分词器"""
    global _jieba
    if _jieba is None:
        try:
            import jieba
            import jieba.posseg as pseg
            # 添加自定义词典
            for word in _CUSTOM_WORDS:
                jieba.add_word(word)
            _jieba = pseg
            log.info("jieba 分词器加载成功，已添加 %d 个自定义词汇", len(_CUSTOM_WORDS))
        except ImportError:
            log.warning("jieba 未安装，使用正则回退方案")
            _jieba = False
    return _jieba


def _extract_key_terms_with_jieba(content: str) -> set[str]:
    """使用 jieba 提取关键字"""
    terms = set()
    
    pseg = _get_jieba()
    if not pseg or pseg is False:
        # 回退到正则
        cn_matches = re.findall(r'[\u4e00-\u9fff]{2,4}', content)
        for m in cn_matches:
            if m not in _STOP_WORDS and len(m) >= 2:
                terms.add(m)
        return terms
    
    # 使用 jieba 词性标注提取名词短语
    words = pseg.cut(content)
    for word, flag in words:
        word = word.strip()
        if len(word) < 2:
            continue
        # 保留名词、动词、英文、以及自定义词汇（词性为 x 表示非语素字，通常是自定义词）
        if flag.startswith(('n', 'v', 'eng')) or flag == 'x' or word in _CUSTOM_WORDS:
            if word not in _STOP_WORDS:
                # 英文保持原样，中文直接添加
                if re.match(r'^[A-Za-z]', word):
                    terms.add(word if word[0].isupper() else word.lower())
                else:
                    terms.add(word)
    
    return terms


def _extract_key_terms(content: str) -> list[str]:
    """Extract key terms from content for knowledge graph nodes.

    Strategy (priority order):
    1. Try LLM for high-quality extraction (sync, requires API key)
    2. Fallback to jieba for Chinese NLP
    3. Final fallback to regex
    Returns up to 15 unique key terms.
    """
    terms = set()
    
    # 尝试使用 LLM 提取（同步调用）
    try:
        from memory_gateway.utils.llm_extract import extract_keywords_with_llm_sync, get_cached_keywords, cache_keywords
        
        # 检查缓存
        cached = get_cached_keywords(content)
        if cached:
            log.debug(f"Using cached LLM keywords for: {content[:50]}...")
            return cached[:15]
        
        # 调用 LLM（同步）
        llm_terms = extract_keywords_with_llm_sync(content)
        if llm_terms:
            log.info(f"LLM extracted {len(llm_terms)} terms from: {content[:50]}...")
            cache_keywords(content, llm_terms)
            return llm_terms[:15]
        else:
            log.warning(f"LLM extraction returned None for: {content[:50]}..., falling back to jieba")
            terms = _extract_key_terms_with_jieba(content)
    except Exception as e:
        log.warning(f"LLM extraction error: {e}, falling back to jieba", exc_info=True)
        terms = _extract_key_terms_with_jieba(content)
    
    # 英文提取（无论是否使用 LLM，都补充英文术语）
    en_matches = re.findall(r'[A-Za-z_][A-Za-z0-9_]{2,}', content)
    for m in en_matches:
        lower = m.lower()
        # Skip: pure numbers, stop words, very short
        if lower in _STOP_WORDS or len(m) < 3 or m.isdigit():
            continue
        # Keep original case for proper nouns, lowercase for common terms
        terms.add(m if m[0].isupper() else lower)

    # Deduplicate by lowercase, keep the most "interesting" variant
    seen = {}
    for t in terms:
        key = t.lower()
        if key not in seen or (t[0].isupper() and not seen[key][0].isupper()):
            seen[key] = t

    return list(seen.values())[:15]


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


# (Embedding utilities moved to memory_gateway/utils/embedding.py)
from memory_gateway.utils.embedding import (
    _embed_model,
    EMBEDDING_DIM,
    _get_embed_model,
    _blob_to_vector,
    _vector_to_blob,
    _cosine_similarity,
    _compute_embedding,
)


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

# Default TTLs per priority/memory-type (days)
# MEMORY_DEFAULT_TTL env var: JSON dict with priority -> days mapping
# Example: '{"P0":0,"procedural":0,"P1":180,"P2":60}'
_DEFAULT_TTL_ENV = os.environ.get("MEMORY_DEFAULT_TTL", '{"P0":0,"procedural":0,"P1":180,"P2":60}')
DEFAULT_TTL = json.loads(_DEFAULT_TTL_ENV)

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


# ── FastAPI App ──────────────────────────────────────────

app = FastAPI(
    title="Memory Gateway",
    version="4.0.0",
    description="MCP Memory Server — Hermes + Claude Code + WorkBuddy",
)

# ── CORS Middleware ──────────────────────────────────────

# Dynamically build allowed origins from env or sensible defaults.
# Wildcard origins are NOT compatible with credentials (cookies), so we
# enumerate specific origins when credentials are needed.
#
# 环境变量 MEMORY_ALLOWED_ORIGINS 可以配置允许的来源（逗号分隔）
# 例如: MEMORY_ALLOWED_ORIGINS="https://your-domain.com,http://localhost:8650"
_ALLOWED_ORIGINS = os.environ.get("MEMORY_ALLOWED_ORIGINS", "")
if _ALLOWED_ORIGINS:
    origins = [o.strip() for o in _ALLOWED_ORIGINS.split(",") if o.strip()]
else:
    # Safe default: Dashboard + localhost origins for development
    origins = [
        "http://localhost:8650",      # Memory Gateway Dashboard
        "http://127.0.0.1:8650",     # Memory Gateway Dashboard (alternative)
        "http://localhost:3000",      # Common dev server
        "http://localhost:8093",      # Hermes Web UI
        "http://127.0.0.1:3000",
        "http://127.0.0.1:8093",
        "http://localhost:8080",
        "http://127.0.0.1:8080",
    ]

app.add_middleware(
    CORSMiddleware,
    allow_origins=origins,
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"],
    allow_headers=["Content-Type", "Authorization", "X-API-Key", "X-CSRF-Token"],
)

# ── API Key Auth ────────────────────────────────────────

# (_generate_api_key moved to memory_gateway/utils/helpers.py)

def _load_api_key() -> str:
    """Load API key: file is the single source of truth.

    Design:
    - /data/.api_key file is the ONLY authoritative key store
    - MEMORY_API_KEY env var is used ONLY for first-time initialization
      (if env var set AND file doesn't exist → write env var to file)
    - rotate/reset/set endpoints always write to file
    - If nothing exists → auto-generate, write to file

    This eliminates the dual-source problem where env var and file
    could hold different keys, causing intermittent 401 errors.
    """
    # 1. First-time bootstrap: seed file from env var (one-way, non-destructive)
    env_key = os.environ.get("MEMORY_API_KEY", "").strip()
    if env_key and not KEY_FILE.exists():
        KEY_FILE.parent.mkdir(parents=True, exist_ok=True)
        KEY_FILE.write_text(env_key)
        try:
            os.chmod(KEY_FILE, 0o600)
        except Exception:
            pass
        log.info("Seeded API key from MEMORY_API_KEY env var → %s", KEY_FILE)

    # 2. Read from file (single source of truth)
    if KEY_FILE.exists():
        file_key = KEY_FILE.read_text().strip()
        if file_key:
            log.info("Using API key from %s (hash: %s)", KEY_FILE, hashlib.sha256(file_key.encode()).hexdigest()[:12])
            return file_key

    # 3. Auto-generate (first run, no env var, no file)
    new_key = _generate_api_key()
    KEY_FILE.parent.mkdir(parents=True, exist_ok=True)
    KEY_FILE.write_text(new_key)
    try:
        os.chmod(KEY_FILE, 0o600)
    except Exception:
        log.warning("Could not set permissions on API key file (non-fatal)", exc_info=True)
    log.warning("=" * 64)
    log.warning("  FIRST RUN — Auto-generated API Key (masked): %s...%s", new_key[:8], new_key[-4:])
    log.warning("  Saved to: %s", KEY_FILE)
    log.warning("  Check `docker logs memory-gateway` to retrieve it.")
    log.warning("=" * 64)
    return new_key

# ── Runtime API key (loaded at import time) ─────────────

API_KEY = _load_api_key()

# Propagate API_KEY to the auth middleware module so it can perform
# credential checking without a circular import.
import memory_gateway.middleware.auth as _auth_mw
_auth_mw.API_KEY = API_KEY

# ── Register custom middleware ───────────────────────────

app.middleware("http")(security_headers_middleware)
app.middleware("http")(api_key_middleware)


@app.on_event("startup")
async def startup() -> None:
    with db_conn() as db:
        init_db(db)
    log.info("Database ready at %s", DB_PATH)
    if API_KEY:
        log.info("API Key authentication enabled (hash: %s)", hashlib.sha256(API_KEY.encode()).hexdigest()[:12])
    else:
        log.warning("No API key configured — server is open to all requests")


# ── Health ───────────────────────────────────────────────


@app.get("/health")
async def health() -> dict:
    with db_conn() as db:
        count = db.execute("SELECT COUNT(*) FROM memories WHERE archived=0").fetchone()[0]
    return {"status": "ok", "version": "5.1.0", "memories": count}


@app.get("/")
async def root():
    """Redirect to the dashboard UI."""
    return RedirectResponse(url="/dashboard", status_code=302)


# ── Register Routers ─────────────────────────────────────

from memory_gateway.routers.admin import router as admin_router
from memory_gateway.routers.dashboard import router as dashboard_router

app.include_router(admin_router)
app.include_router(dashboard_router)

# ── Static files mount ──────────────────────────────────

from memory_gateway.config import STATIC_DIR

if STATIC_DIR.exists():
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


# ── Register Routers ─────────────────────────────────────

from memory_gateway.routers.memories import router as memories_router
from memory_gateway.routers.categories import router as categories_router
from memory_gateway.routers.mcp import router as mcp_router

app.include_router(memories_router)
app.include_router(categories_router)
app.include_router(mcp_router)

# ── Enhancement Router (借鉴左脑理念) ─────────────────
from memory_gateway.enhancement import enhancement_router
app.include_router(enhancement_router)


# ── Run ──────────────────────────────────────────────────

if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("MEMORY_PORT", "8650"))
    host = os.environ.get("MEMORY_HOST", "0.0.0.0")
    log.info("Starting Memory Gateway v4 on %s:%d", host, port)
    uvicorn.run(app, host=host, port=port, log_level=LOG_LEVEL.lower())

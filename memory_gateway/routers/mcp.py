"""
MCP router - JSON-RPC 2.0 protocol endpoint for MCP-compliant clients.

Extracted from server.py (/mcp endpoint and related handlers).
"""

import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from memory_gateway.database.connection import db_conn
from memory_gateway.models.requests import (
    AuditSearchRequest,
    CleanupRequest,
    ListRequest,
    OffloadRequest,
    SaveRequest,
    SearchHybridRequest,
    SearchRequest,
    SyncHeartbeatRequest,
)
from memory_gateway.services.version_service import VersionManager
from memory_gateway.utils.crypto import _find_near_duplicate, hamming_distance
from memory_gateway.utils.embedding import _compute_embedding

from memory_gateway.routers._shared import (
    HOT_CACHE_MAX,
    HOT_CACHE_TTL,
    _extract_key_terms,
    _get_related_terms,
    detect_type,
    hot_cache,
    row_to_dict,
)
from memory_gateway.routers.memories import (
    audit_search,
    batch_delete,
    batch_save,
    cache_stats,
    cleanup_memories,
    create_relation,
    delete_memory,
    drilldown_memory,
    get_graph,
    get_persona,
    get_relations,
    get_scenario,
    history,
    lint_memories,
    list_memory,
    offload_memory,
    save_memory,
    search_hybrid,
    search_memory,
    stats,
    sync_heartbeat,
)
from memory_gateway.routers.categories import list_categories
from memory_gateway.utils import now_iso

log = logging.getLogger("memory-server")

router = APIRouter(tags=["mcp"])


# ── MCP Tool Definitions ──────────────────────────────────

MCP_TOOLS = [
    {
        "name": "mem_save",
        "description": "保存一条记忆到记忆库。支持分类、优先级、标签等元数据。支持血缘追踪(derived_from)和替代关系(superseded_by)。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "content": {"type": "string", "description": "记忆内容"},
                "category_id": {"type": "string", "description": "分类ID (learning/life/work/innovation/general 或 work_* 子分类)", "default": "general"},
                "type": {"type": "string", "enum": ["general", "rule", "preference", "decision", "context", "learning", "reference", "convention", "insight"], "default": "general"},
                "priority": {"type": "string", "enum": ["P0", "P1", "P2"], "default": "P1"},
                "tags": {"type": "array", "items": {"type": "string"}, "description": "自定义标签"},
                "source": {"type": "string", "enum": ["hermes", "claude", "workbuddy", "system", "unknown"], "default": "unknown"},
                "scope": {"type": "string", "enum": ["global", "project", "agent"], "default": "global"},
                "session_id": {"type": "string", "description": "会话ID（可选）"},
                "derived_from": {"type": "array", "items": {"type": "string"}, "description": "来源记忆ID列表（进化产物血缘追踪）"},
                "superseded_by": {"type": "string", "description": "被哪条记忆取代（指向新记忆ID）"}
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
        "name": "mem_insights_generate",
        "description": "自我蒸馏：从高频记忆中自动提炼 insights（目标/摩擦点/结论），生成结构化的学习笔记。借鉴 Claude Code 的 facets 模式。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "action": {"type": "string", "enum": ["generate", "list", "update"], "description": "操作类型：generate=从高频记忆生成insights，list=列出已有insights，update=更新指定记忆的insights"},
                "memory_id": {"type": "string", "description": "指定记忆ID（update操作必填）"},
                "insights": {"type": "string", "description": "insights内容（update操作必填）"},
                "min_recall_count": {"type": "integer", "description": "最低召回次数阈值", "default": 3},
                "limit": {"type": "integer", "description": "最大处理数量", "default": 20}
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
    },
    {
        "name": "mem_skill_save",
        "description": "保存一个可复用技能到技能库。技能是从高频记忆中提取的结构化知识。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "技能名称"},
                "description": {"type": "string", "description": "技能描述"},
                "trigger_pattern": {"type": "string", "description": "触发模式（关键词或正则）"},
                "content_md": {"type": "string", "description": "技能内容（Markdown格式）"},
                "category": {"type": "string", "description": "分类", "default": "general"},
                "source_memory_ids": {"type": "array", "items": {"type": "string"}, "description": "来源记忆ID列表"},
                "confidence": {"type": "number", "description": "置信度(0-1)", "default": 0.8},
                "agent_scope": {"type": "string", "enum": ["all", "hermes", "claude", "workbuddy"], "description": "适用Agent范围", "default": "all"}
            },
            "required": ["name", "content_md"]
        }
    },
    {
        "name": "mem_skill_match",
        "description": "根据查询匹配相关技能。用于Agent在处理任务时查找可用技能。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "查询内容"},
                "category_filter": {"type": "string", "description": "分类过滤"},
                "min_confidence": {"type": "number", "description": "最低置信度", "default": 0.5},
                "limit": {"type": "integer", "description": "返回数量", "default": 5}
            },
            "required": ["query"]
        }
    },
    {
        "name": "mem_skill_list",
        "description": "列出所有已保存的技能。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "category_filter": {"type": "string", "description": "分类过滤"},
                "min_confidence": {"type": "number", "description": "最低置信度", "default": 0.0},
                "limit": {"type": "integer", "description": "返回数量", "default": 50}
            }
        }
    },
    {
        "name": "mem_skill_extract",
        "description": "从高频记忆中自动提取技能。分析recall_count高或P0优先级的记忆，识别可复用的模式。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "min_recall_count": {"type": "integer", "description": "最低召回次数", "default": 3},
                "min_priority": {"type": "string", "enum": ["P0", "P1", "P2"], "description": "最低优先级", "default": "P0"},
                "limit": {"type": "integer", "description": "处理数量", "default": 10},
                "dry_run": {"type": "boolean", "description": "仅分析不创建", "default": False}
            }
        }
    },
    {
        "name": "mem_lint",
        "description": "检查记忆库健康状态。扫描并报告孤立记忆、过期记忆、空标签、零召回、未知来源、高置信低召回等问题。",
        "inputSchema": {
            "type": "object",
            "properties": {}
        }
    },
    {
        "name": "mem_cleanup_db",
        "description": "执行数据库清理：删除旧版本、旧关系、旧日志，并可选执行 VACUUM 优化。解决数据库膨胀问题。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "version_keep_count": {"type": "integer", "description": "每个记忆保留的版本数", "default": 10},
                "relation_max_age_days": {"type": "integer", "description": "关系最大保留天数", "default": 90},
                "change_log_max_age_days": {"type": "integer", "description": "变更日志最大保留天数", "default": 30},
                "evolution_log_max_age_days": {"type": "integer", "description": "进化日志最大保留天数", "default": 180},
                "search_audit_max_age_days": {"type": "integer", "description": "搜索审计日志最大保留天数", "default": 30},
                "vacuum": {"type": "boolean", "description": "是否执行 VACUUM 优化", "default": True}
            }
        }
    }
]


# ── MCP JSON-RPC 2.0 Handlers ────────────────────────────


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
                history_result = VersionManager.get_history(db, memory_id, limit)
            text = json.dumps({"memory_id": memory_id, "versions": history_result}, ensure_ascii=False, default=str)

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
            # 回滚后清除热缓存，确保搜索结果一致
            if rollback_result.get("success"):
                hot_cache.clear()
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
            else:
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
            mem_id = arguments.get("memory_id")
            limit = arguments.get("limit", 10)
            with db_conn() as db:
                if mem_id:
                    row = db.execute("SELECT content, category_id FROM memories WHERE id=?", (mem_id,)).fetchone()
                    if row:
                        terms = _extract_key_terms(row["content"])
                        graph = {}
                        for t in terms:
                            related = _get_related_terms(db, t, limit)
                            if related:
                                graph[t] = related
                        result = {"memory_id": mem_id, "terms": terms, "graph": graph}
                    else:
                        result = {"error": f"Memory {mem_id} not found"}
                elif term:
                    related = _get_related_terms(db, term, limit)
                    result = {"term": term, "related": related, "count": len(related)}
                else:
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
                    conditions = ["archived=0"]
                    search_params: list = []
                    if category_filter:
                        conditions.append("category_id=?")
                        search_params.append(category_filter)

                    where = " WHERE " + " AND ".join(conditions)
                    rows = db.execute(
                        f"SELECT id, content, simhash, checksum, category_id, type, priority FROM memories {where} ORDER BY created_at DESC LIMIT 500",
                        search_params
                    ).fetchall()

                    similar_pairs = []
                    contradictions = []
                    seen_checksums: dict[str, str] = {}

                    for i, r in enumerate(rows):
                        if r["checksum"] in seen_checksums:
                            similar_pairs.append({
                                "id1": seen_checksums[r["checksum"]],
                                "id2": r["id"],
                                "reason": "exact_duplicate",
                                "similarity": 1.0
                            })
                        else:
                            seen_checksums[r["checksum"]] = r["id"]

                        if r["simhash"]:
                            for j in range(i + 1, min(i + 50, len(rows))):
                                other = rows[j]
                                if other["simhash"]:
                                    dist = hamming_distance(r["simhash"], other["simhash"])
                                    if dist < 8:
                                        similarity = round(1.0 - dist / 64, 3)
                                        similar_pairs.append({
                                            "id1": r["id"],
                                            "id2": other["id"],
                                            "reason": "similar_content",
                                            "similarity": similarity,
                                            "distance": dist
                                        })

                        if r["type"] == "decision":
                            for j in range(i + 1, min(i + 30, len(rows))):
                                other = rows[j]
                                if (other["type"] == "decision" and
                                    other["category_id"] == r["category_id"] and
                                    other["id"] != r["id"]):
                                    r_terms = set(_extract_key_terms(r["content"]))
                                    o_terms = set(_extract_key_terms(other["content"]))
                                    overlap = r_terms & o_terms
                                    if len(overlap) >= 2:
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
                    rows = db.execute(
                        "SELECT id, content, simhash, checksum, category_id FROM memories WHERE archived=0 AND simhash != '' ORDER BY created_at DESC LIMIT 200"
                    ).fetchall()

                    merged = 0
                    merge_log = []
                    seen: dict[str, str] = {}
                    potential_merges = []  # 存储潜在的合并对

                    for i, r in enumerate(rows):
                        if r["checksum"] in seen:
                            # 精确重复：直接归档
                            db.execute("UPDATE memories SET archived=1 WHERE id=?", (r["id"],))
                            merge_log.append({"archived": r["id"], "kept": seen[r["checksum"]], "reason": "exact_duplicate"})
                            merged += 1
                        else:
                            seen[r["checksum"]] = r["id"]

                        # 扫描模糊重复（无论 auto_merge 是否为 True）
                        if r["simhash"]:
                            for j in range(i + 1, min(i + 50, len(rows))):
                                other = rows[j]
                                if other["simhash"] and other["id"] not in [m.get("archived") for m in merge_log]:
                                    dist = hamming_distance(r["simhash"], other["simhash"])
                                    if dist < 10:  # 扩大扫描范围
                                        similarity = round(1.0 - dist / 64, 3)
                                        potential_merge = {
                                            "id1": r["id"],
                                            "id2": other["id"],
                                            "distance": dist,
                                            "similarity": similarity,
                                            "reason": "fuzzy_duplicate"
                                        }
                                        
                                        # 根据相似度决定是否自动合并
                                        if auto_merge and dist < 6:  # 高相似度：自动合并
                                            db.execute("UPDATE memories SET archived=1 WHERE id=?", (other["id"],))
                                            merge_log.append({"archived": other["id"], "kept": r["id"], "reason": "fuzzy_duplicate", "distance": dist, "auto_merged": True})
                                            merged += 1
                                        else:
                                            # 记录为潜在合并候选
                                            potential_merge["auto_merged"] = False
                                            potential_merge["action_required"] = "manual_review"
                                            merge_log.append(potential_merge)

                    if merged > 0:
                        hot_cache.clear()
                        db.execute("INSERT INTO memories_fts(memories_fts) VALUES('rebuild')")

                    # 统计自动合并和需要人工审核的数量
                    auto_merged_count = sum(1 for m in merge_log if m.get("auto_merged"))
                    manual_review_count = sum(1 for m in merge_log if m.get("action_required") == "manual_review")
                    
                    result = {
                        "merged": merged,
                        "auto_merged": auto_merged_count,
                        "manual_review_required": manual_review_count,
                        "log": merge_log[:30],
                        "message": f"合并完成：自动归档 {merged} 条冗余记忆" if merged > 0 else 
                                   f"发现 {manual_review_count} 对潜在重复，需要人工审核" if manual_review_count > 0 else 
                                   "无需合并，记忆库无冗余"
                    }
                else:
                    result = {"error": f"Unknown action: {action}"}

            text = json.dumps(result, ensure_ascii=False, default=str)

        elif tool_name == "mem_insights_generate":
            action = arguments.get("action", "generate")
            memory_id = arguments.get("memory_id")
            insights_text = arguments.get("insights")
            min_recall = arguments.get("min_recall_count", 3)
            limit = arguments.get("limit", 20)

            with db_conn() as db:
                if action == "generate":
                    rows = db.execute(
                        "SELECT id, content, type, category_id, recall_count, priority, source "
                        "FROM memories WHERE archived=0 AND recall_count >= ? AND (insights IS NULL OR insights='') "
                        "ORDER BY recall_count DESC LIMIT ?",
                        (min_recall, limit)
                    ).fetchall()

                    generated = []
                    for r in rows:
                        content = r["content"]
                        insight_parts = []

                        if any(kw in content for kw in ["目标", "目的", "要", "需要", "应该"]):
                            insight_parts.append("目标明确")
                        if any(kw in content for kw in ["问题", "困难", "bug", "错误", "失败", "卡住"]):
                            insight_parts.append("存在摩擦点")
                        if any(kw in content for kw in ["结论", "结果", "发现", "学到", "经验", "教训"]):
                            insight_parts.append("有明确结论")
                        if not insight_parts:
                            insight_parts.append(f"高频使用({r['recall_count']}次)")

                        insight = " | ".join(insight_parts)
                        generated.append({"id": r["id"], "content_preview": content[:100], "insight": insight})
                        db.execute(
                            "UPDATE memories SET insights=? WHERE id=?",
                            (insight, r["id"])
                        )

                    result = {
                        "action": "generate",
                        "generated_count": len(generated),
                        "memories": generated,
                        "message": f"为 {len(generated)} 条高频记忆生成了 insights"
                    }

                elif action == "list":
                    rows = db.execute(
                        "SELECT id, content, insights, recall_count, category_id "
                        "FROM memories WHERE archived=0 AND insights IS NOT NULL AND insights != '' "
                        "ORDER BY recall_count DESC LIMIT ?",
                        (limit,)
                    ).fetchall()
                    result = {
                        "action": "list",
                        "count": len(rows),
                        "memories": [dict(r) for r in rows]
                    }

                elif action == "update":
                    if not memory_id or not insights_text:
                        result = {"error": "memory_id and insights are required for update action"}
                    else:
                        existing = db.execute(
                            "SELECT id FROM memories WHERE id=? AND archived=0",
                            (memory_id,)
                        ).fetchone()
                        if not existing:
                            result = {"error": f"Memory {memory_id} not found"}
                        else:
                            db.execute(
                                "UPDATE memories SET insights=?, updated_at=? WHERE id=?",
                                (insights_text, now_iso(), memory_id)
                            )
                            result = {
                                "action": "update",
                                "success": True,
                                "memory_id": memory_id,
                                "insights": insights_text
                            }
                else:
                    result = {"error": f"Unknown action: {action}"}

            text = json.dumps(result, ensure_ascii=False, default=str)

        elif tool_name == "mem_evolve":
            action = arguments.get("action", "analyze")
            days = arguments.get("days", 30)

            with db_conn() as db:
                if action == "analyze":
                    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
                    top_recalled = db.execute(
                        "SELECT id, content, type, category_id, recall_count, confidence, priority "
                        "FROM memories WHERE archived=0 AND recall_count > 0 "
                        "ORDER BY recall_count DESC LIMIT 10"
                    ).fetchall()
                    never_recalled = db.execute(
                        "SELECT id, content, type, category_id, created_at, priority "
                        "FROM memories WHERE archived=0 AND recall_count=0 "
                        "AND created_at < ? ORDER BY created_at ASC LIMIT 10",
                        (cutoff,)
                    ).fetchall()
                    high_confidence = db.execute(
                        "SELECT id, content, type, category_id, confidence "
                        "FROM memories WHERE archived=0 AND confidence > 0.9 "
                        "ORDER BY confidence DESC LIMIT 10"
                    ).fetchall()
                    category_dist = {}
                    for row in db.execute(
                        "SELECT category_id, COUNT(*) as c FROM memories WHERE archived=0 GROUP BY category_id"
                    ):
                        category_dist[row["category_id"]] = row["c"]
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
                    promoted = 0
                    demoted = 0
                    rows = db.execute(
                        "SELECT id, priority FROM memories WHERE archived=0 AND recall_count >= 5 AND priority = 'P2'"
                    ).fetchall()
                    for r in rows:
                        db.execute("UPDATE memories SET priority='P1', ttl_days=180 WHERE id=?", (r["id"],))
                        promoted += 1
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
                    total = db.execute("SELECT COUNT(*) FROM memories WHERE archived=0").fetchone()[0]
                    avg_confidence = db.execute("SELECT AVG(confidence) FROM memories WHERE archived=0").fetchone()[0] or 0
                    avg_recall = db.execute("SELECT AVG(recall_count) FROM memories WHERE archived=0").fetchone()[0] or 0
                    category_counts = {}
                    for row in db.execute(
                        "SELECT category_id, COUNT(*) as c FROM memories WHERE archived=0 GROUP BY category_id"
                    ):
                        category_counts[row["category_id"]] = row["c"]
                    gaps = []
                    for cat_id, count in category_counts.items():
                        if count < 3:
                            gaps.append(f"{cat_id}: 仅 {count} 条记忆，建议补充")
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

        elif tool_name == "mem_skill_save":
            from memory_gateway.models.requests import SkillSaveRequest
            req = SkillSaveRequest(**arguments)
            result = await save_skill(req)
            text = json.dumps(result, ensure_ascii=False)

        elif tool_name == "mem_skill_match":
            from memory_gateway.models.requests import SkillMatchRequest
            req = SkillMatchRequest(**arguments)
            result = await match_skill(req)
            text = json.dumps(result, ensure_ascii=False)

        elif tool_name == "mem_skill_list":
            from memory_gateway.models.requests import SkillListRequest
            req = SkillListRequest(**arguments)
            result = await list_skills(req)
            text = json.dumps(result, ensure_ascii=False)

        elif tool_name == "mem_skill_extract":
            from memory_gateway.models.requests import SkillExtractRequest
            req = SkillExtractRequest(**arguments)
            result = await extract_skills(req)
            text = json.dumps(result, ensure_ascii=False)

        elif tool_name == "mem_lint":
            result = await lint_memories()
            text = json.dumps(result, ensure_ascii=False)

        elif tool_name == "mem_cleanup_db":
            from memory_gateway.utils.cleanup import full_cleanup
            from memory_gateway.config import DB_PATH
            
            # 执行数据库清理
            result = full_cleanup(
                db_path=str(DB_PATH),
                version_keep_count=arguments.get("version_keep_count", 10),
                relation_max_age_days=arguments.get("relation_max_age_days", 90),
                change_log_max_age_days=arguments.get("change_log_max_age_days", 30),
                evolution_log_max_age_days=arguments.get("evolution_log_max_age_days", 180),
                search_audit_max_age_days=arguments.get("search_audit_max_age_days", 30),
                vacuum=arguments.get("vacuum", True)
            )
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
        log.error("MCP handler failed: %s", e, exc_info=True)
        return {
            "jsonrpc": "2.0",
            "id": request_id,
            "result": {
                "content": [{"type": "text", "text": json.dumps({"error": str(e)}, ensure_ascii=False)}],
                "isError": True
            }
        }


# ── Skill Functions (MCP backend handlers) ────────────────


async def save_skill(req) -> dict:
    """Save a skill to the skill library."""
    import uuid
    skill_id = req.id or str(uuid.uuid4())
    source_ids = json.dumps(req.source_memory_ids or [])

    with db_conn() as db:
        existing = db.execute(
            "SELECT id FROM skills WHERE name = ?", (req.name,)
        ).fetchone()

        if existing:
            db.execute("""
                UPDATE skills SET
                    description = ?, trigger_pattern = ?, content_md = ?,
                    category = ?, source_memory_ids = ?, confidence = ?,
                    agent_scope = ?, updated_at = datetime('now')
                WHERE id = ?
            """, (req.description, req.trigger_pattern, req.content_md,
                  req.category, source_ids, req.confidence,
                  req.agent_scope, existing["id"]))
            skill_id = existing["id"]
            action = "updated"
        else:
            db.execute("""
                INSERT INTO skills (id, name, description, trigger_pattern,
                    content_md, category, source_memory_ids, confidence, agent_scope)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (skill_id, req.name, req.description, req.trigger_pattern,
                  req.content_md, req.category, source_ids, req.confidence,
                  req.agent_scope))
            action = "created"

    return {
        "id": skill_id,
        "action": action,
        "name": req.name,
        "category": req.category,
        "confidence": req.confidence
    }


async def match_skill(req) -> dict:
    """Match skills by query."""
    with db_conn() as db:
        sql = """
            SELECT id, name, description, trigger_pattern, content_md,
                   category, confidence, recall_count, agent_scope
            FROM skills
            WHERE confidence >= ?
        """
        params: list = [req.min_confidence]

        if req.category_filter:
            sql += " AND category = ?"
            params.append(req.category_filter)

        sql += " AND (name LIKE ? OR description LIKE ? OR trigger_pattern LIKE ? OR content_md LIKE ?)"
        query_pattern = f"%{req.query}%"
        params.extend([query_pattern] * 4)

        sql += " ORDER BY confidence DESC, recall_count DESC LIMIT ?"
        params.append(req.limit)

        results = []
        for row in db.execute(sql, params).fetchall():
            skill = dict(row)
            db.execute(
                "UPDATE skills SET recall_count = recall_count + 1 WHERE id = ?",
                (skill["id"],)
            )
            results.append(skill)

    return {
        "query": req.query,
        "matches": len(results),
        "skills": results
    }


async def list_skills(req) -> dict:
    """List all skills."""
    with db_conn() as db:
        sql = "SELECT * FROM skills WHERE confidence >= ?"
        params: list = [req.min_confidence]

        if req.category_filter:
            sql += " AND category = ?"
            params.append(req.category_filter)

        sql += " ORDER BY confidence DESC, recall_count DESC LIMIT ?"
        params.append(req.limit)

        results = [dict(row) for row in db.execute(sql, params).fetchall()]

    return {
        "total": len(results),
        "skills": results
    }


async def extract_skills(req) -> dict:
    """Extract skills from high-frequency memories."""
    with db_conn() as db:
        sql = """
            SELECT id, content, category_id, type, priority, recall_count, tags
            FROM memories
            WHERE archived = 0 AND (
                recall_count >= ? OR priority = ?
            )
            ORDER BY recall_count DESC, priority ASC
            LIMIT ?
        """
        candidates = []
        for row in db.execute(sql, (req.min_recall_count, req.min_priority, req.limit)).fetchall():
            candidates.append(dict(row))

    if req.dry_run:
        return {
            "mode": "dry_run",
            "candidates_found": len(candidates),
            "candidates": [
                {
                    "id": c["id"],
                    "content_preview": c["content"][:100],
                    "recall_count": c["recall_count"],
                    "priority": c["priority"],
                    "category": c["category_id"]
                }
                for c in candidates
            ]
        }

    extracted_skills = []
    for mem in candidates:
        content = mem["content"]
        skill_name = ""
        skill_desc = ""
        trigger = ""
        category = mem["category_id"] or "general"

        if any(kw in content for kw in ["步骤", "流程", "方法", "如何", "怎么", "操作"]):
            skill_name = f"流程：{content[:50]}..."
            skill_desc = f"从记忆 {mem['id'][:8]} 提取的工作流程"
            trigger = "步骤|流程|方法|操作"
            category = "workflow"

        elif any(kw in content for kw in ["偏好", "喜欢", "习惯", "规则", "要求", "必须"]):
            skill_name = f"偏好：{content[:50]}..."
            skill_desc = f"从记忆 {mem['id'][:8]} 提取的用户偏好"
            trigger = "偏好|喜欢|习惯|规则"
            category = "preference"

        elif any(kw in content for kw in ["技术", "架构", "配置", "部署", "命令", "代码"]):
            skill_name = f"技术：{content[:50]}..."
            skill_desc = f"从记忆 {mem['id'][:8]} 提取的技术知识"
            trigger = "技术|架构|配置|部署|命令|代码"
            category = mem["category_id"] or "technical"

        else:
            skill_name = f"通用：{content[:50]}..."
            skill_desc = f"从记忆 {mem['id'][:8]} 提取的通用知识"
            trigger = content[:20]
            category = mem["category_id"] or "general"

        if skill_name and skill_desc:
            import uuid
            skill_id = str(uuid.uuid4())
            source_ids = json.dumps([mem["id"]])
            with db_conn() as db:
                db.execute("""
                    INSERT INTO skills (id, name, description, trigger_pattern,
                        content_md, category, source_memory_ids, confidence, agent_scope)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (skill_id, skill_name, skill_desc, trigger,
                      content, category, source_ids, 0.7, "all"))

            extracted_skills.append({
                "id": skill_id,
                "name": skill_name,
                "category": category,
                "source_memory": mem["id"][:8]
            })

    return {
        "mode": "extracted",
        "candidates_processed": len(candidates),
        "skills_created": len(extracted_skills),
        "skills": extracted_skills
    }


# ── Route: MCP JSON-RPC Endpoint ──────────────────────────


@router.post("/mcp")
async def mcp_endpoint(request: Request) -> JSONResponse:
    """MCP JSON-RPC 2.0 endpoint for protocol-compliant clients."""
    try:
        body = await request.json()
    except Exception:
        log.warning("MCP endpoint: failed to parse request body as JSON", exc_info=True)
        return JSONResponse(
            status_code=400,
            content={"jsonrpc": "2.0", "error": {"code": -32700, "message": "Parse error"}}
        )

    method = body.get("method", "")
    request_id = body.get("id")
    params = body.get("params", {})

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

    return JSONResponse(
        content={
            "jsonrpc": "2.0",
            "id": request_id,
            "error": {"code": -32601, "message": f"Method not found: {method}"}
        }
    )

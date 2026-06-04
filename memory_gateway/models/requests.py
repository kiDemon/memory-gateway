"""Pydantic request models for Memory Gateway API.

Extracted from server.py to support multi-module architecture.
All model definitions remain unchanged.
"""

from typing import Optional

from pydantic import BaseModel, Field


class SaveRequest(BaseModel):
    content: str = Field(..., max_length=100000)
    type: Optional[str] = Field(default=None, pattern=r"^(general|rule|preference|decision|context|learning|reference|convention|procedural|insight)$")
    scope: Optional[str] = Field(default="global", pattern=r"^(global|project|agent)$")
    source: Optional[str] = Field(default="unknown", pattern=r"^(hermes|claude|workbuddy|system|unknown)$")
    priority: Optional[str] = Field(default="P1", pattern=r"^(P0|P1|P2)$")
    category_id: Optional[str] = Field(default="general", pattern=r"^[a-z][a-z0-9_]{0,63}$")
    tags: Optional[list[str]] = None
    session_id: Optional[str] = None
    id: Optional[str] = None
    derived_from: Optional[list[str]] = Field(default=None, description="来源记忆ID列表（进化产物血缘）")
    superseded_by: Optional[str] = Field(default=None, description="被哪条记忆取代（指向新记忆ID）")
    insights: Optional[str] = Field(default=None, max_length=5000, description="提炼结论：从这条记忆学到了什么")


class UpdateRequest(BaseModel):
    content: Optional[str] = Field(default=None, max_length=100000)
    type: Optional[str] = Field(default=None, pattern=r"^(general|rule|preference|decision|context|learning|reference|convention|procedural)$")
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


class SkillSaveRequest(BaseModel):
    name: str = Field(..., min_length=1, max_length=200)
    description: str = Field(default="", max_length=1000)
    trigger_pattern: str = Field(default="", max_length=500)
    content_md: str = Field(..., max_length=50000)
    category: str = Field(default="general", pattern=r"^[a-z][a-z0-9_]{0,63}$")
    source_memory_ids: Optional[list[str]] = None
    confidence: float = Field(default=0.8, ge=0.0, le=1.0)
    agent_scope: str = Field(default="all", pattern=r"^(all|hermes|claude|workbuddy)$")
    id: Optional[str] = None


class SkillMatchRequest(BaseModel):
    query: str = Field(..., min_length=1, max_length=500)
    category_filter: Optional[str] = None
    min_confidence: float = Field(default=0.5, ge=0.0, le=1.0)
    limit: int = Field(default=5, ge=1, le=20)


class SkillListRequest(BaseModel):
    category_filter: Optional[str] = None
    min_confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    limit: int = Field(default=50, ge=1, le=200)


class SkillExtractRequest(BaseModel):
    min_recall_count: int = Field(default=3, ge=1, le=100)
    min_priority: str = Field(default="P0", pattern=r"^(P0|P1|P2)$")
    limit: int = Field(default=10, ge=1, le=50)
    dry_run: bool = Field(default=False, description="仅分析，不实际创建skill")


class CheckDuplicatesRequest(BaseModel):
    checksums: list[str] = Field(default_factory=list, description="List of SHA256 checksums to check")
    simhashes: list[dict] = Field(default_factory=list, description="List of {content, simhash} to fuzzy-check")


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


class AuditSearchRequest(BaseModel):
    source: Optional[str] = None
    limit: int = Field(default=50, ge=1, le=200)
    since: Optional[str] = None


class CleanupRequest(BaseModel):
    confirm: bool = False


class BatchSaveRequest(BaseModel):
    memories: list[SaveRequest]


class BatchDeleteRequest(BaseModel):
    source: str = "system"
    category_id: str = "learning"
    confirm: bool = False


class SetKeyRequest(BaseModel):
    key: str = Field(..., min_length=16, max_length=256)


class LoginRequest(BaseModel):
    key: str = Field(..., min_length=1)

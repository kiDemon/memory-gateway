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
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
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
    db.executescript("""
    PRAGMA journal_mode=WAL;
    PRAGMA foreign_keys=ON;

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

    -- 记忆表
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
    scope: Optional[str] = Field(default="global", pattern=r"^(global|project|agent)$")
    source: Optional[str] = Field(default="unknown", pattern=r"^(hermes|claude|workbuddy|system|unknown)$")
    priority: Optional[str] = Field(default="P1", pattern=r"^(P0|P1|P2)$")
    category_id: Optional[str] = "general"
    tags: Optional[list[str]] = None
    session_id: Optional[str] = None
    id: Optional[str] = None


class UpdateRequest(BaseModel):
    content: Optional[str] = None
    type: Optional[str] = None
    scope: Optional[str] = None
    priority: Optional[str] = None
    category_id: Optional[str] = None
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
    return {"status": "ok", "version": "4.0.0", "memories": count, "db": str(DB_PATH)}


@app.get("/", response_class=HTMLResponse)
async def index() -> str:
    with db_conn() as db:
        stats_data = _get_stats(db)
        # By category
        by_category = {}
        for row in db.execute(
            "SELECT c.name, c.icon, COUNT(m.id) as cnt FROM categories c "
            "LEFT JOIN memories m ON m.category_id = c.id AND m.archived = 0 "
            "GROUP BY c.id ORDER BY c.sort_order"
        ):
            by_category[f"{row['icon']} {row['name']}"] = row["cnt"]
        # By priority
        by_priority = {}
        for row in db.execute(
            "SELECT priority, COUNT(*) as c FROM memories WHERE archived=0 GROUP BY priority"
        ):
            by_priority[row["priority"]] = row["c"]
        # Sync status
        sync_rows = db.execute("SELECT * FROM sync_status ORDER BY tool").fetchall()
        sync_status = [dict(r) for r in sync_rows]
    return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head><meta charset="utf-8"><title>Memory Gateway v4.1</title>
<style>
  body {{ font-family: -apple-system, sans-serif; max-width: 900px; margin: 40px auto; padding: 0 20px; color: #e0e0e0; background: #1a1a2e; }}
  h1 {{ color: #00d4ff; }} h2 {{ color: #58a6ff; margin-top: 30px; }}
  .stat {{ margin: 12px 0; padding: 12px 16px; background: #16213e; border-radius: 8px; }}
  .stat strong {{ color: #00d4ff; }}
  .grid {{ display: grid; grid-template-columns: repeat(auto-fill, minmax(200px, 1fr)); gap: 12px; margin: 16px 0; }}
  .card {{ background: #16213e; padding: 14px; border-radius: 8px; text-align: center; }}
  .card .num {{ font-size: 24px; font-weight: bold; color: #00d4ff; }}
  .card .label {{ font-size: 13px; color: #888; margin-top: 4px; }}
  .nav {{ margin-bottom: 24px; }}
  .nav a {{ color: #00d4ff; text-decoration: none; margin-right: 20px; padding: 6px 14px; background: #16213e; border-radius: 6px; }}
  .nav a:hover {{ background: #1f3460; }}
  .sync-item {{ display: flex; justify-content: space-between; padding: 8px 12px; background: #0d1117; border-radius: 6px; margin: 4px 0; }}
  .sync-item .status {{ font-weight: bold; }}
  .status-healthy {{ color: #2ed573; }}
  .status-stale {{ color: #ffa502; }}
  .status-disconnected {{ color: #ff4757; }}
  .mcp-section {{ background: #16213e; padding: 16px; border-radius: 8px; margin: 12px 0; }}
  code {{ background: #0d1117; padding: 2px 6px; border-radius: 4px; font-size: 13px; }}
  table {{ border-collapse: collapse; width: 100%; }}
  th, td {{ padding: 8px 12px; text-align: left; border-bottom: 1px solid #333; }}
</style></head>
<body>
<h1>Memory Gateway v4.1</h1>
<div class="nav">
  <a href="/">Home</a>
  <a href="/admin">Admin</a>
</div>

<div class="grid">
  <div class="card"><div class="num">{stats_data['total']}</div><div class="label">总记忆</div></div>
  <div class="card"><div class="num">{stats_data['active']}</div><div class="label">活跃</div></div>
  <div class="card"><div class="num">{stats_data['archived']}</div><div class="label">归档</div></div>
  <div class="card"><div class="num">{len(sync_status)}</div><div class="label">已连接工具</div></div>
</div>

<h2>📂 分类分布</h2>
<div class="grid">
{''.join(f'<div class="card"><div class="num">{v}</div><div class="label">{k}</div></div>' for k, v in by_category.items() if v > 0)}
</div>

<h2>⭐ 优先级分布</h2>
<div class="grid">
{''.join(f'<div class="card"><div class="num">{v}</div><div class="label">{p}</div></div>' for p, v in by_priority.items())}
</div>

<h2>🔄 同步状态</h2>
<div class="mcp-section">
{''.join(f'<div class="sync-item"><span>{r["tool"]}</span><span class="status status-{r["status"]}">{r["status"]}</span><span>同步{r["total_syncs"]}次</span></div>' for r in sync_status) if sync_status else '<div style="color:#888;">暂无工具连接</div>'}
</div>

<h2>🔌 API 端点</h2>
<div class="mcp-section">
<h3>REST API</h3>
<table>
<tr><th>Method</th><th>Path</th><th>Description</th></tr>
<tr><td>POST</td><td><code>/mcp/save</code></td><td>Save memory</td></tr>
<tr><td>POST</td><td><code>/mcp/search</code></td><td>Full-text search</td></tr>
<tr><td>POST</td><td><code>/mcp/list</code></td><td>List memories</td></tr>
<tr><td>GET</td><td><code>/mcp/categories</code></td><td>Get categories</td></tr>
<tr><td>GET</td><td><code>/mcp/stats</code></td><td>Statistics</td></tr>
<tr><td>POST</td><td><code>/mcp/sync/heartbeat</code></td><td>Sync heartbeat</td></tr>
<tr><td>GET</td><td><code>/mcp/sync/status</code></td><td>Sync status</td></tr>
</table>
<h3>MCP Protocol (JSON-RPC 2.0)</h3>
<table>
<tr><th>Endpoint</th><th>Methods</th></tr>
<tr><td><code>POST /mcp</code></td><td>initialize, tools/list, tools/call, ping</td></tr>
</table>
</div>

<h2>🛠️ 连接配置</h2>
<div class="mcp-section">
<p>WorkBuddy 自定义 MCP 配置：</p>
<code>http://YOUR_SERVER:8650/mcp</code>
<p style="color:#888;font-size:13px;margin-top:8px;">在 WorkBuddy 的自定义 MCP 设置中添加此地址</p>
</div>

</body></html>"""


# ── MCP Endpoints ────────────────────────────────────────


@app.post("/mcp/save")
async def save_memory(req: SaveRequest) -> dict:
    memory_id = req.id or str(uuid.uuid4())
    now = now_iso()
    checksum = compute_checksum(req.content)
    mem_type = req.type or detect_type(req.content)
    tags_json = json.dumps(req.tags or [])
    category_id = req.category_id or "general"

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
               (id, content, type, scope, source, priority, confidence, tags, category_id,
                created_at, updated_at, recall_count, archived, checksum)
               VALUES (?, ?, ?, ?, ?, ?, 0.8, ?, ?, ?, ?, 0, 0, ?)""",
            (memory_id, req.content.strip(), mem_type, req.scope,
             req.source, req.priority or "P1", tags_json, category_id, now, now, checksum),
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

        if req.category_filter:
            # 支持父分类过滤：category=work 也会匹配 work_comprehensive, work_hr 等
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
    return {"success": True, "sync_status": [dict(r) for r in rows]}


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
                "version": "4.1.0"
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

            db.execute(
                """INSERT INTO memories
                   (id, content, type, scope, source, priority, confidence, tags, category_id,
                    created_at, updated_at, recall_count, archived, checksum)
                   VALUES (?, ?, ?, ?, ?, ?, 0.8, ?, ?, ?, ?, 0, 0, ?)""",
                (memory_id, mem.content.strip(), mem_type, mem.scope,
                 mem.source, mem.priority or "P1", tags_json, category_id, now, now, checksum),
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


# ── Run ──────────────────────────────────────────────────

if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("MEMORY_PORT", "8650"))
    host = os.environ.get("MEMORY_HOST", "0.0.0.0")
    log.info("Starting Memory Gateway v4 on %s:%d", host, port)
    uvicorn.run(app, host=host, port=port, log_level=LOG_LEVEL.lower())

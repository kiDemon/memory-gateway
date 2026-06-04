# Memory Gateway 代码审查报告

**审查时间**: 2026-06-03  
**代码版本**: v5.1.0  
**文件**: server.py (5060行, 206KB)  
**审查范围**: 架构设计、代码质量、安全性、性能、可维护性

---

## 一、架构问题

### 1.1 单文件架构（严重）

**问题**: 整个系统5060行代码全部写在一个`server.py`文件中，违反单一职责原则。

**影响**:
- 代码导航困难，定位问题耗时
- 多人协作冲突率高
- 测试难以隔离
- 重构风险大

**建议**: 按职责拆分为：
```
memory_gateway/
├── main.py              # FastAPI app 入口
├── config.py            # 配置管理
├── database/
│   ├── __init__.py
│   ├── schema.py        # 数据库Schema定义
│   ├── migrations.py    # 迁移逻辑
│   └── connection.py    # 连接管理
├── models/
│   ├── __init__.py
│   ├── memory.py        # Pydantic模型
│   └── category.py
├── routers/
│   ├── __init__.py
│   ├── mcp.py           # MCP协议端点
│   ├── memories.py      # 记忆CRUD
│   ├── categories.py    # 分类管理
│   ├── admin.py         # 管理后台
│   └── dashboard.py     # Dashboard API
├── services/
│   ├── __init__.py
│   ├── memory_service.py
│   ├── search_service.py
│   ├── version_service.py
│   └── sync_service.py
├── middleware/
│   ├── __init__.py
│   ├── auth.py          # 认证中间件
│   └── security.py      # 安全头
└── utils/
    ├── __init__.py
    ├── crypto.py         # checksum, simhash
    ├── privacy.py        # 敏感信息过滤
    └── embedding.py      # 向量嵌入
```

---

## 二、数据库问题

### 2.1 Schema定义与实际表不匹配（严重）

**问题**: 代码中定义了以下表，但本地数据库未创建：
- `memory_versions`
- `evolution_log`
- `memory_branches`
- `search_audit_log`
- `raw_memories`

**本地数据库实际表**:
```
categories, memories, session_memories, change_log, 
sqlite_sequence, sync_status, memory_relations, memories_fts
```

**原因分析**:
1. Schema迁移逻辑在`init_db()`中，依赖`PRAGMA table_info`检测
2. 本地数据库是早期版本创建，缺少新表的迁移代码
3. Docker环境与本地环境Schema不同步

**修复建议**:
```python
# 在init_db()中添加显式表创建检查
REQUIRED_TABLES = [
    'memory_versions', 'evolution_log', 'memory_branches',
    'search_audit_log', 'raw_memories'
]

for table in REQUIRED_TABLES:
    exists = db.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
        (table,)
    ).fetchone()
    if not exists:
        log.warning(f"Missing table: {table}, triggering full migration")
        # 执行完整Schema创建
```

### 2.2 迁移逻辑存在竞态条件

**问题**: `init_db()`中的迁移代码在并发启动时可能重复执行。

**当前代码**:
```python
if columns and 'category_id' not in columns:
    db.execute("ALTER TABLE memories ADD COLUMN category_id TEXT DEFAULT 'general'")
    db.commit()
```

**风险**: 多个worker同时启动时，可能同时检测到缺少列并执行迁移。

**修复建议**:
```python
import sqlite3
from contextlib import contextmanager

@contextmanager
def db_lock(db: sqlite3.Connection):
    """获取数据库锁，防止并发迁移"""
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("BEGIN IMMEDIATE")
    try:
        yield
        db.commit()
    except:
        db.rollback()
        raise
```

### 2.3 数据库连接管理不一致

**问题**: 代码中混合使用两种连接模式：
1. `get_db()` - 返回连接，不自动关闭
2. `db_conn()` - 上下文管理器，自动提交/回滚

**风险**: 部分代码可能忘记关闭连接，导致连接泄漏。

**修复建议**: 统一使用`db_conn()`上下文管理器，废弃`get_db()`。

---

## 三、安全性问题

### 3.1 会话存储在内存中（中等）

**问题**: `_sessions`字典存储在内存中：
```python
_sessions: dict[str, dict] = {}  # token -> {ip, created_at, expires_at}
```

**影响**:
- 服务重启后所有会话丢失
- 无法水平扩展
- 内存泄漏风险（无清理机制）

**修复建议**:
```python
# 方案1: 使用SQLite存储会话
# 方案2: 使用Redis（需要额外依赖）
# 方案3: 使用JWT token（无状态）

# 推荐方案3: JWT
import jwt
from datetime import datetime, timedelta

SECRET_KEY = os.environ.get("JWT_SECRET", secrets.token_urlsafe(32))

def create_session(ip: str) -> str:
    payload = {
        "ip": ip,
        "exp": datetime.utcnow() + timedelta(hours=24),
        "iat": datetime.utcnow()
    }
    return jwt.encode(payload, SECRET_KEY, algorithm="HS256")

def validate_session(token: str) -> bool:
    try:
        jwt.decode(token, SECRET_KEY, algorithms=["HS256"])
        return True
    except jwt.ExpiredSignatureError:
        return False
```

### 3.2 IP锁定机制不持久化（低）

**问题**: `_failed_logins`字典在内存中：
```python
_failed_logins: dict[str, list[float]] = {}  # ip -> [timestamps]
```

**影响**: 重启后锁定状态丢失，攻击者可重置计数器。

**修复建议**: 使用SQLite表存储失败记录，或使用Redis。

### 3.3 API Key日志泄露（低）

**问题**: 启动时日志输出API Key前16位：
```python
log.info("API Key authentication enabled (key starts with: %s...)", API_KEY[:16])
```

**风险**: 日志被第三方获取后可缩小暴力破解范围。

**修复建议**: 只输出Key的哈希或最后4位：
```python
log.info("API Key enabled (hash: %s...)", hashlib.sha256(API_KEY.encode()).hexdigest()[:8])
```

---

## 四、性能问题

### 4.1 FTS5索引未优化（中等）

**问题**: FTS5使用`trigram`分词器，对中文支持有限。

**当前代码**:
```python
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
```

**影响**: 
- 中文搜索依赖trigram，召回率低
- 短查询(<3字符)直接走LIKE，性能差

**修复建议**:
```python
# 方案1: 使用jieba分词 + FTS5
# 方案2: 集成Elasticsearch（重量级）
# 方案3: 使用SQLite的json1扩展 + 自定义分词

# 推荐: 保持trigram但优化查询逻辑
if len(req.q) < 3:
    # 短查询使用前缀匹配而非LIKE
    fts_query = f'"{req.q}"*'
```

### 4.2 热缓存未生效（低）

**问题**: `hot_cache`只缓存搜索结果，但`_sync_hot_tier_from_cache`函数名误导。

**实际逻辑**:
```python
def _sync_hot_tier_from_cache(db: sqlite3.Connection) -> None:
    """Periodically sync hot tier status: P0 + frequently recalled → hot_tier=1."""
    db.execute("""
        UPDATE memories SET hot_tier=1
        WHERE archived=0 AND (priority='P0' OR recall_count >= 3)
        AND hot_tier=0
    """)
```

**问题**: 这个函数实际上是从DB同步到DB，不是从缓存同步。

**修复建议**: 重命名为`_update_hot_tier()`，并添加定时触发逻辑。

### 4.3 批量操作缺少事务优化（低）

**问题**: `batch_save`和`batch_delete`逐条执行SQL，未使用`executemany`。

**当前代码**:
```python
@app.post("/mcp/batch_save")
async def batch_save(req: BatchSaveRequest) -> dict:
    for item in req.memories:
        # 逐条插入
        db.execute("INSERT INTO memories ...", ...)
```

**修复建议**:
```python
# 使用executemany批量插入
values = [(item.id, item.content, ...) for item in req.memories]
db.executemany("INSERT INTO memories ...", values)
```

---

## 五、代码质量问题

### 5.1 过度使用异常捕获（中等）

**问题**: 19处`except Exception`捕获，部分吞掉异常：
```python
except Exception:
    pass  # 静默忽略
```

**风险**: 隐藏真实错误，调试困难。

**修复建议**:
```python
# 至少记录日志
except Exception as e:
    log.warning(f"Operation failed: {e}", exc_info=True)
```

### 5.2 硬编码配置（低）

**问题**: 部分配置硬编码：
```python
HOT_CACHE_MAX = 200
HOT_CACHE_TTL = 300  # 5分钟
DEFAULT_TTL = {...}
```

**修复建议**: 移至环境变量或配置文件。

### 5.3 类型提示不完整（低）

**问题**: 部分函数缺少返回类型提示：
```python
def detect_type(content: str) -> str:  # 有
def _extract_key_terms(content: str) -> list[str]:  # 有
def _build_timeline(db, days=30):  # 缺少
```

---

## 六、可维护性问题

### 6.1 缺少测试（严重）

**问题**: 项目中未发现测试文件。

**建议**: 添加单元测试和集成测试：
```
tests/
├── test_database.py
├── test_api.py
├── test_search.py
├── test_versioning.py
└── conftest.py
```

### 6.2 缺少API文档（中等）

**问题**: FastAPI自动生成的文档可能不完整。

**建议**: 为每个端点添加详细的docstring和示例。

### 6.3 日志级别不统一（低）

**问题**: 部分使用`log.info()`，部分使用`log.warning()`，无统一规范。

**建议**: 制定日志规范：
- DEBUG: 开发调试
- INFO: 正常操作
- WARNING: 可恢复错误
- ERROR: 需要干预的错误
- CRITICAL: 系统不可用

---

## 七、优先级建议

### P0（立即修复）
1. Schema迁移竞态条件
2. 会话存储持久化
3. 添加基础测试

### P1（一周内）
1. 单文件架构拆分
2. 统一数据库连接管理
3. FTS5中文搜索优化

### P2（两周内）
1. 代码质量改进（异常处理、类型提示）
2. 配置外部化
3. API文档完善

### P3（长期）
1. 集成测试覆盖
2. 性能基准测试
3. 监控告警

---

## 八、总结

**当前状态**: 功能完整，但架构债务严重。5060行单文件是最大痛点，导致维护成本高、测试困难、多人协作冲突。

**核心风险**:
1. 数据库Schema迁移可能在并发启动时出错
2. 内存存储的会话和锁定状态不可靠
3. 中文搜索效果受限于trigram分词器

**建议行动**:
1. 短期: 修复数据库迁移和会话存储
2. 中期: 拆分架构，添加测试
3. 长期: 优化搜索，提升可维护性

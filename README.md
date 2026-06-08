# Memory Gateway

> **跨 AI Agent 的统一记忆层** — 让 Hermes、Claude Code、WorkBuddy 共享同一个的长期记忆。

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Python](https://img.shields.io/badge/Python-3.12+-blue)](https://www.python.org/)
[![FastAPI](https://img.shields.io/badge/FastAPI-0.109+-teal)](https://fastapi.tiangolo.com/)
[![MCP](https://img.shields.io/badge/MCP-StreamableHTTP-purple)](https://modelcontextprotocol.io/)

Memory Gateway 是一个轻量级、可自部署的云端记忆服务器。它为多个 AI Agent 提供持久化、可检索、自动演化的共享记忆层——写一次记忆，所有 Agent 都能看到。

---

## 目录

- [为什么需要 Memory Gateway](#为什么需要-memory-gateway)
- [核心特性](#核心特性)
- [架构总览](#架构总览)
- [快速开始](#快速开始)
- [配置](#配置)
- [API 参考](#api-参考)
- [集成指南](#集成指南)
  - [Hermes Agent 集成](#hermes-agent-集成)
  - [Claude Code 集成](#claude-code-集成)
  - [通用 MCP 接入](#通用-mcp-接入)
- [仪表盘](#仪表盘)
- [与类似项目对比](#与类似项目对比)
- [开发](#开发)
- [路线图](#路线图)
- [许可证](#许可证)

---

## 为什么需要 Memory Gateway

### 痛点

当前 AI Agent 的记忆方案各有局限：

| 方案 | 局限 |
|------|------|
| 内置记忆文件 (memory.md) | 容量小（~2K字符），**仅限本 Agent** |
| 会话全文搜索 (state.db) | FTS5 无法语义检索，**不跨工具共享** |
| 各厂商封闭记忆 | 无法打通 Hermes ↔ Claude ↔ 自有工具 |
| 纯 FTS5 方案 | 无衰减、无去重、无版本控制 |

### Memory Gateway 的解决思路

**写一次，所有 Agent 读。** Memory Gateway 作为独立服务运行，所有 AI Agent 通过 MCP 协议或 REST API 读写同一份记忆存储。它还内置了：

- **自动去重** — 精确(checksum) + 模糊(simhash) 双重防重复
- **知识图谱自动抽取** — 保存记忆时自动提取关键词→共现关系，无需人工维护
- **Dreams 后台整合** — 扫描记忆库发现相似/矛盾记忆，自动合并冗余
- **知识分层** — 从原始记录→事实→工作流→画像，四层渐进
- **艾宾浩斯衰减** — 常用记忆自动保鲜，冷门记忆自然归档
- **版本控制** — 每次修改自动创建快照，可回溯到任意历史版本
- **冲突检测** — 多 Agent 同时写同一记忆时检测冲突（向量时钟）

---

## 核心特性

### 存储层

| 特性 | 说明 |
|------|------|
| 存储引擎 | SQLite + FTS5（trigram 分词） |
| 去重机制 | SHA256 精确去重 + SimHash 模糊去重（汉明距离 < 10） |
| 分类树 | 13 个预置分类（支持 work 子分类树），可扩展 |
| 标签系统 | 自定义 tags 任意维度标记 |
| 关联图谱 | source-target-relation-strength 四维关系表 + **自动抽取关键词共现** |
| 版本控制 | Git-like 版本链 + diff + 分支 + 进化日志 |
| 4 层渐进存储 | L0 原始(全文) → L1 记忆(结构化) → L2 场景(聚合) → L3 画像(人物) |

### 检索层

| 特性 | 说明 |
|------|------|
| FTS5 全文搜索 | trigram 分词，支持中英文、模糊匹配 |
| RRF 融合排序 | Reciprocal Rank Fusion 组合 FTS + 语义排名，无需调权重 |
| 热缓存 | LRU 200 条目，TTL 5 分钟 |
| 检索审计 | 每次搜索记录查询/来源/延迟/命中数，可查询统计 |
| 置信度加权 | 搜索结果按记忆置信度（类型+来源+长度+召回次数）加权排序 |

### 演化层

| 特性 | 说明 |
|------|------|
| 优先级体系 | P0 永不过期 / P1 正常衰减 / P2 短生命 |
| 艾宾浩斯衰减 | `strength = confidence × 0.5^(天数/半衰期)`，对数增长防滥用 |
| 置信度动态 | 保存时按类型+来源算分，召回时+0.02，长期不召回自然衰减 |
| Dreams 后台整合 | 扫描相似/矛盾记忆，自动合并冗余，人工审核矛盾决策 |
| 知识图谱 | 自动提取关键词→共现关系，strength 随重复出现递增 |
| 多 Agent 冲突检测 | 向量时钟追踪各 Agent 最后一次修改时间，检测并发写冲突 |
| 隐私过滤 | 自动脱敏 API Key、Token、密码后入库 |

### 运维层

| 特性 | 说明 |
|------|------|
| 部署 | Docker 单容器部署，数据持久化到 volume |
| 健康检查 | Docker HEALTHCHECK 30s 间隔 |
| 日志 | 结构化日志，支持 DEBUG/INFO/WARNING/ERROR 级别 |
| API 认证 | API Key 头部认证（自动生成或手动设置）|
| 管理员面板 | 浏览器 Dashboard → 概览/分类/记忆列表/时间线/健康度 |
| MCP 协议 | 原生 StreamableHTTP MCP 支持，tools list/call 自动发现 |

---

## 架构总览

```
┌─────────────────────────────────────────────────────────────┐
│                     AI Agent 端                               │
│  ┌──────────┐  ┌──────────┐  ┌──────────┐  ┌──────────┐     │
│  │  Hermes  │  │ClaudeCode│  │WorkBuddy │  │  其他    │     │
│  │  Agent   │  │  CLI     │  │          │  │  MCP工具 │     │
│  └────┬─────┘  └────┬─────┘  └────┬─────┘  └────┬─────┘     │
│       │             │             │             │            │
│       └─────────────┼─────────────┼─────────────┘            │
│                     │   MCP / REST API                       │
└─────────────────────┼───────────────────────────────────────┘
                      │
                      ▼
┌─────────────────────────────────────────────────────────────┐
│                   Memory Gateway 服务端                       │
│                                                              │
│  ┌─────────┐  ┌──────────┐  ┌──────────┐  ┌──────────────┐  │
│  │  MCP    │  │   REST   │  │Dashboard │  │   Auth       │  │
│  │  Endpoint│  │  Endpoints│  │  Web UI  │  │   (API Key)  │  │
│  └────┬────┘  └────┬─────┘  └────┬─────┘  └──────┬───────┘  │
│       │             │             │                │          │
│       └─────────────┼─────────────┼────────────────┘          │
│                     ▼                                         │
│        ┌──────────────────────────────┐                       │
│        │        核心引擎              │                       │
│        │  ┌────────┐ ┌──────────┐    │                       │
│        │  │ 去重   │ │ 衰减     │    │                       │
│        │  │(simhash)│ │(Ebbinghaus)│  │                       │
│        │  ├────────┤ ├──────────┤    │                       │
│        │  │ 检索   │ │ 版本     │    │                       │
│        │  │(RRF)   │ │(Version) │    │                       │
│        │  ├────────┤ ├──────────┤    │                       │
│        │  │ 缓存   │ │ 冲突检测 │    │                       │
│        │  │(LRU)   │ │(VC)     │    │                       │
│        │  └────────┘ └──────────┘    │                       │
│        └──────────────────────────────┘                       │
│                      │                                        │
│                      ▼                                        │
│        ┌──────────────────────────────┐                       │
│        │   SQLite + FTS5 (WAL模式)    │                       │
│        │  ┌────┐ ┌──────┐ ┌──────┐   │                       │
│        │  │mem │ │fts   │ │rel   │   │                       │
│        │  │ories│ │5     │ │ations│   │                       │
│        │  ├────┤ ├──────┤ ├──────┤   │                       │
│        │  │vers│ │categ │ │evolu │   │                       │
│        │  │ions│ │ories │ │tion  │   │                       │
│        │  └────┘ └──────┘ └──────┘   │                       │
│        └──────────────────────────────┘                       │
└─────────────────────────────────────────────────────────────┘
```

### 数据流

```
Agent 保存记忆                          Agent 搜索记忆
    │                                      │
    ▼                                      ▼
  save_memory()                          search_memory()
    │                                      │
    ├─ 隐私过滤                             ├─ 热缓存命中? → 直接返回
    ├─ 去重 (checksum + simhash)            ├─ FTS5 全文搜索
    ├─ 类型检测 (rule/preference/procedural)├─ RRF 融合排序 (FTS + 语义)
    ├─ 置信度打分                           ├─ 置信度加权
    ├─ 向量时钟初始化                        ├─ 召回统计 + 置信度+0.02
    ├─ 写入 SQLite + FTS5                  └─ 审计日志
    ├─ 版本快照创建
    ├─ 知识图谱自动抽取 (关键词→共现关系)
    └─ 热缓存同步 (P0/procedural)
```

---

## 快速开始

### 前提

- Docker & Docker Compose
- 防火墙放行 `8650` 端口

### 一键部署

```bash
git clone https://github.com/kiDemon/memory-gateway.git
cd memory-gateway
docker compose up -d

# 查看自动生成的 API Key
docker logs memory-gateway 2>&1 | grep "API key"
```

### 验证

```bash
# 健康检查
curl http://localhost:8650/health

# 存入一条记忆
curl -X POST http://localhost:8650/mcp/save \
  -H "Content-Type: application/json" \
  -H "X-API-Key: <your-api-key>" \
  -d '{"content":"项目采用 JWT 中间件认证","source":"hermes","priority":"P1"}'

# 搜索
curl -X POST http://localhost:8650/mcp/search \
  -H "Content-Type: application/json" \
  -H "X-API-Key: <your-api-key>" \
  -d '{"q":"JWT 认证","limit":5}'

# 查看统计
curl http://localhost:8650/mcp/stats
```

### 仪表盘

浏览器打开 `http://localhost:8650/dashboard` → 查看记忆概览、分类统计、时间线、健康度。

#### 记忆浏览器功能

| 功能 | 说明 |
|------|------|
| **内容搜索** | 输入关键词，模糊匹配记忆内容 |
| **ID 精确查询** | 输入记忆 UUID（如 `62936bba-40a1-40a5-8a08-dcf7346fcffc`），直接定位到特定记忆 |
| **分类筛选** | 按 category_id 筛选（work, learning, general 等）|
| **来源筛选** | 按 source 筛选（hermes, claude, workbuddy 等）|
| **优先级筛选** | 按 priority 筛选（P0, P1, P2）|
| **分页浏览** | 每页 20 条，支持翻页 |

#### 记忆详情面板

点击记忆行可打开详情面板，显示：
- 完整记忆内容
- 元数据（分类、来源、优先级、类型、创建/更新时间）
- 版本历史（所有修改记录）

---

## 配置

### 环境变量

| 变量 | 默认值 | 说明 |
|------|--------|------|
| `MEMORY_API_KEY` | `auto` | API 认证密钥。`auto`=自动生成并持久化到 `/data/.api_key` |
| `MEMORY_DATA_DIR` | `/data` | 数据目录（SQLite 数据库 + API Key 持久化）|
| `MEMORY_PORT` | `8650` | HTTP 服务端口 |
| `MEMORY_HOST` | `0.0.0.0` | 监听地址 |
| `MEMORY_LOG_LEVEL` | `INFO` | 日志级别: DEBUG / INFO / WARNING / ERROR |
| `MEMORY_HOT_CACHE_SIZE` | `200` | 热缓存最大条目数 |
| `MEMORY_HOT_CACHE_TTL` | `300` | 热缓存 TTL（秒） |

### API 认证

Gateway 默认自动生成 API Key，存储在 `/data/.api_key` 中。也可以通过 Web UI `http://localhost:8650/admin` 或 REST API 进行密钥管理。

所有 API 请求（健康检查除外）需要携带头部：

```
X-API-Key: sk-mg-...xxxx
```

Dashboard 和 API 端点都需要认证。Dashboard 首次打开会显示登录页，输入 API Key 后保存到浏览器 localStorage。

---

## API 参考

### MCP 端点 (`/mcp`)

| 方法 | 端点 | 说明 |
|------|------|------|
| `POST` | `/mcp/save` | 保存记忆（自动去重 + 类型检测 + 版本快照） |
| `POST` | `/mcp/update/{id}` | 更新记忆（自动创建版本快照 + 冲突检测） |
| `DELETE` | `/mcp/delete/{id}` | 删除记忆 |
| `GET` | `/mcp/get/{id}` | 获取单条记忆 |
| `POST` | `/mcp/search` | 全文搜索（FTS5 + RRF 融合排序） |
| `POST` | `/mcp/list` | 列表查询（支持分类/来源/类型过滤 + 游标翻页） |
| `POST` | `/mcp/batch_save` | 批量保存 |
| `POST` | `/mcp/batch_delete` | 批量删除 |
| `GET` | `/mcp/stats` | 统计概览（按来源/类型/范围汇总） |
| `GET` | `/mcp/export` | 导出记忆 |
| `GET` | `/mcp/history/{id}` | 查看记忆版本历史 |
| `POST` | `/mcp/cleanup` | 触发衰减清理（艾宾浩斯 + 热缓存降级） |
| `GET` | `/mcp/cache/stats` | 热缓存状态 |
| `POST` | `/mcp/audit/search` | 搜索审计日志查询 |

### 分类管理 (`/mcp/category`)

| 方法 | 端点 | 说明 |
|------|------|------|
| `GET` | `/mcp/categories` | 列出分类树 |
| `GET` | `/mcp/category/{id}` | 获取分类详情 |
| `POST` | `/mcp/category` | 创建分类 |
| `PUT` | `/mcp/category/{id}` | 更新分类 |
| `DELETE` | `/mcp/category/{id}` | 删除分类 |

### 关系图谱 (`/mcp`)

| 方法 | 端点 | 说明 |
|------|------|------|
| `POST` | `/mcp/relation` | 创建记忆间关系 |
| `GET` | `/mcp/relations/{id}` | 获取某记忆的所有关系 |
| `DELETE` | `/mcp/relations/{src}/{tgt}` | 删除关系 |

### 仪表盘 (`/`)

| 方法 | 端点 | 说明 |
|------|------|------|
| `GET` | `/dashboard` | 浏览器管理面板 |
| `GET` | `/api/dashboard/overview` | 面板概览数据 |
| `GET` | `/api/dashboard/memories` | 记忆列表 |
| `GET` | `/api/dashboard/categories` | 分类统计 |
| `GET` | `/api/dashboard/timeline` | 记忆时间线 |
| `GET` | `/api/dashboard/health` | 健康状态 |

### 请求参数说明

#### `POST /mcp/save`

```json
{
  "id": "可选，不传自动生成 UUID",
  "content": "记忆内容",
  "type": "可选，auto/procedural/rule/preference/learning/context/feature/progress/debugging/reference/convention",
  "scope": "可选，默认 global",
  "source": "hermes / claude / workbuddy / system / 自定义",
  "priority": "P0 / P1 / P2",
  "tags": ["标签1", "标签2"],
  "category_id": "learning / life / work / innovation / general 或 work_comprehensive 等子分类",
  "session_id": "可选，关联的会话 ID"
}
```

#### `POST /mcp/search`

```json
{
  "q": "搜索关键词",
  "limit": 10,
  "category_filter": "可选，分类筛选",
  "type_filter": "可选，类型筛选",
  "source_filter": "可选，来源筛选",
  "scope_filter": "可选，范围筛选",
  "include_archived": false
}
```

---

## 集成指南

### Hermes Agent 集成

Hermes Agent 原生支持 Memory Gateway 作为记忆后端。在 `~/.hermes/config.yaml` 中配置：

```yaml
memory:
  provider: memory_gateway
  mcp_server: memory-gateway  # 引用下方的 mcp_servers 配置

mcp_servers:
  memory-gateway:
    transport: http
    url: http://localhost:8650/mcp
    headers:
      X-API-Key: "sk-mg-xxxxxxxxxxxxxxxx"
```

启用后，Hermes Agent 自动：
- 每轮对话结束时将关键信息保存到 Memory Gateway（prefetch + save）
- 新会话开始时自动预取相关记忆（按 topic 搜索）
- 跨会话共享记忆（Hermes ↔ Claude Code ↔ WorkBuddy）

### Claude Code 集成

Claude Code 通过 MCP 配置文件接入：

```json
{
  "mcpServers": {
    "memory-gateway": {
      "type": "http",
      "url": "http://localhost:8650/mcp",
      "headers": {
        "X-API-Key": "sk-mg-xxxxxxxxxxxxxxxx"
      }
    }
  }
}
```

### 通用 MCP 接入

任何支持 MCP 协议的 Agent 都可以接入：

```python
# MCP Tools 自动发现
# 1. 调用 /mcp 服务端，获取工具列表
call: {"method": "tools/list", "params": {}}

# 2. 调用记忆操作工具
call: {"method": "tools/call", "params": {
    "name": "mem_save",
    "arguments": {
        "content": "记忆内容",
        "source": "my-agent",
        "priority": "P1"
    }
}}
```

### MCP 工具列表（18个）

| 类别 | 工具 | 说明 |
|------|------|------|
| **CRUD** | `mem_save` | 保存记忆（自动去重+类型检测+隐私过滤+知识图谱抽取） |
| | `mem_search` | 搜索记忆（FTS5+RRF融合+置信度加权） |
| | `mem_list` | 列出记忆（支持增量同步） |
| | `mem_delete` | 删除记忆 |
| | `mem_categories` | 获取分类列表 |
| | `mem_stats` | 获取统计信息 |
| **版本控制** | `mem_history` | 查看记忆版本历史 |
| | `mem_diff` | 对比两个版本差异 |
| | `mem_rollback` | 回滚到指定版本 |
| | `mem_branch` | 创建/列出记忆分支 |
| | `mem_merge` | 合并记忆分支 |
| **4层存储** | `mem_offload` | 卸载长文本到 L0 原始层 |
| | `mem_drilldown` | 钻回原始内容 |
| | `mem_scenario` | 获取场景聚合 |
| | `mem_persona` | 获取用户/项目画像 |
| **高级检索** | `mem_search_hybrid` | 混合搜索（FTS5+语义+RRF） |
| | `mem_audit_search` | 搜索审计日志 |
| | `mem_cleanup` | 清理过期记忆 |
| | `mem_cache_stats` | 热缓存统计 |
| **v5.1 新增** | `mem_graph` | 知识图谱查询（按术语/记忆ID） |
| | `mem_dreams` | Dreams 后台整合（scan/merge/stats） |
| | `mem_evolve` | CSSF 自进化（analyze/optimize/insights） |

---

## 仪表盘

浏览器打开 `http://your-server:8650/dashboard` 进入管理面板（需要 API Key 认证）：

| 功能区 | 内容 |
|--------|------|
| **概览** | 记忆总数、活跃/归档统计、按来源分布、按分类分布 |
| **记忆浏览器** | 所有记忆的搜索/筛选/详情查看，支持分类/来源/优先级过滤 |
| **进化追踪** | 单条记忆的版本历史、diff 对比、进化时间线 |
| **🛠️ 智能工具** | 知识图谱查询、Dreams 后台整合、CSSF 自进化分析 |
| **系统监控** | 同步状态、数据库统计、版本控制统计、健康度 |

Dashboard 支持 API Key 认证：首次打开会显示登录页，输入 API Key 后保存到浏览器 localStorage。

---

## 与类似项目对比

| 维度 | Memory Gateway | agentmemory | Honcho | Mem0 | Claude Memory Files |
|------|---------------|-------------|--------|------|---------------------|
| **部署** | 自部署 (Docker) | 自部署 (npm) | 云端/自部署 | 云端/自部署 | Anthropic 内建 |
| **存储** | SQLite+FTS5 | 内存+文件 | PostgreSQL | 向量数据库 | 文件系统 |
| **去重** | SHA256+SimHash | LLM 去重 | 会话级 | 语义去重 | ~ |
| **版本控制** | Git-like (diff/分支/回滚) | ~ | ~ | ~ | ~ |
| **衰减** | 艾宾浩斯曲线 | 艾宾浩斯曲线 | 会话窗口 | 自定义 | Dreams 整合 |
| **语义搜索** | 可选 (sentence-transformers) | BM25+向量+图谱 | 向量检索 | 向量检索 | 按需检索 |
| **知识图谱** | ✅ 自动抽取关键词共现 | 自动抽取+图谱遍历 | ~ | ~ | ~ |
| **Dreams 整合** | ✅ 扫描相似/矛盾+自动合并 | ~ | ~ | ~ | Dreams 整合 |
| **CSSF 自进化** | ✅ analyze/optimize/insights | ✅ 5步螺旋 | ~ | ~ | ~ |
| **隐私过滤** | ✅ 内置 | ✅ hooks 层 | ~ | ~ | ~ |
| **MCP 协议** | ✅ 原生 | ✅ MCP Server | ❌ | ❌ | ❌ |
| **多Agent冲突** | ✅ 向量时钟 | ❌ | ✅ session级 | ❌ | Dreams 整合 |
| **Dashboard** | ✅ 内建（认证保护） | ✅ Viewer | ❌ | ❌ | ❌ |
| **开源** | ✅ MIT | ✅ MIT | ✅ Apache 2 | ✅ Apache 2 | ❌ 闭源 |
| **跨工具** | Hermes+Claude+WorkBuddy | Claude Code+Codex+Cursor | Hermes | Hermes | Claude 生态 |

> ~ 表示该功能不存在

**Memory Gateway 的核心差异化优势：**
1. **版本控制** — 唯一拥有 Git-like 版本链+分支+进化日志的方案
2. **多Agent冲突检测** — 向量时钟追踪跨 Agent 写冲突
3. **MCP 原生** — 与 Hermes/Claude Code 开箱即用
4. **隐私过滤** — 保存时自动脱敏，跨 Agent 不泄露密钥
5. **轻量零依赖** — 单 Python 文件 + SQLite，无外部数据库 / 向量引擎 / GPU 需求

**与 agentmemory 的定位差异：**
- agentmemory 是**嵌入 Agent 内部的记忆引擎**（通过 hooks 捕获事件），强在"自动记录"
- Memory Gateway 是**独立运行的记忆服务**（各 Agent 通过网络读写），强在"跨 Agent 共享 + 统一管理"
- 两者可以互补使用：agentmemory 做本地实时记忆，Memory Gateway 做云端持久化层

---

## 开发

### 本地开发

```bash
# 克隆项目
git clone https://github.com/kiDemon/memory-gateway.git
cd memory-gateway

# 创建虚拟环境
python3 -m venv venv
source venv/bin/activate

# 安装依赖
pip install -r requirements.txt

# 启动服务（开发模式，热重载）
MEMORY_DATA_DIR=./data uvicorn server:app --reload --host 0.0.0.0 --port 8650
```

### 目录结构

```
memory-gateway/
├── server.py              # 主服务（4300+行，包含所有功能）
├── Dockerfile             # 生产构建
├── docker-compose.yml     # 一键部署
├── entrypoint.sh          # 容器入口
├── requirements.txt       # Python 依赖
├── static/
│   └── dashboard.html     # 浏览器管理面板
├── data/                  # 数据持久化（不git追踪）
├── .env.example           # 环境变量模板
├── .gitignore
├── .dockerignore
├── LICENSE
└── README.md
```

### 编码规范

- 单文件架构，所有逻辑集中在 `server.py`（易于部署和调试）
- 函数按功能区域分组，用注释 `# ── Section Name ──` 分隔
- Pydantic BaseModel 集中定义在文件尾部
- 新增功能遵循「不破坏现有 API 兼容性」原则
- 文档变更同步更新 README.md

---

## 路线图

### v5.1（已完成）✅
- [x] Dreams 后台整合（跨记忆合并+矛盾检测+模式发现）
- [x] 知识图谱自动抽取（save 时自动提取关键词→共现关系）
- [x] CSSF 自进化协议（mem_evolve: analyze/optimize/insights）
- [x] Dashboard 智能工具面板（知识图谱/Dreams/CSSF 可视化）
- [x] Dashboard 认证保护（API Key 认证）

### v6.0（中期）
- [ ] 语义搜索轻量版（onnxruntime + MiniLM，替代 PyTorch）
- [ ] Memory Viewer Web App（关系图谱可视化）
- [ ] 多 Provider 存储（可选 PostgreSQL / Redis 后端）
- [ ] 导入/导出（JSON / Markdown / Obsidian 格式）

### v7.0（远期）
- [ ] 联合记忆（多个 Gateway 间同步 + Conflict Resolution）
- [ ] 联邦学习（跨实例统计模式提取，不上传原始记忆）

---

## 许可证

[MIT License](LICENSE) © 2026 kiDemon

---

*Memory Gateway — Write once, remember everywhere.*

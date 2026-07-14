# 写入侧收敛：v5.2.0 改动（2026-07-15）

> 配合 4 个文件的小改动，把"读当前生效层"从口号变成默认值。

## 核心想法

其它 Agent 拉取还是「活跃池 + 相关度截断」，**不发明新过滤**。  
进化靠**写入侧收敛**完成：让活跃池本身就接近「当前生效」。

```
活跃层 = archived=0 AND superseded_by IS NULL
        ↑ Agent prefetch/search/list 默认看的就是这一层
```

## 改动清单

### 1. `memory_gateway/models/requests.py`
- `SaveRequest.superseded_by` 字段说明更新为「**本条取代的旧条 ID**」  
  旧文档的"被哪条记忆取代"语义反了，已纠正。

### 2. `memory_gateway/routers/memories.py`
- **`save_memory`** 写入收敛段：  
  - 调用方传 `superseded_by=<旧id>` 时  
  - 服务端自动把旧条 `archived=1` + 旧条 `superseded_by = <新条id>`  
  - 同时写 `change_log(action='archived_by_supersede')`  
  - 返回里加 `archived_superseded` 字段  
- **新条 INSERT 时不再写 `superseded_by` 字段**（语义修正：superseded_by 属于"被取代的旧条"）
- **`search_memory` / `search_hybrid` / `list_memory` / `export_memories`**  
  默认条件加上 `(superseded_by IS NULL OR superseded_by='')`
- `include_archived=True` 时把 `archived=0` 和 `superseded_by` 条件都改成 `1=1`
- **`lint_memories`** 加 `superseded_but_active` 报告类（v5.1.1 之前的脏数据）

### 3. `memory_gateway/routers/mcp.py`
- `mem_save` 工具描述补充收敛语义

### 4. `server.py`
- `APP_VERSION` 推到 `5.2.0`

### 5. 客户端规约
- `~/.hermes/profiles/coder/cron/jobs.json`  
  Reflection prompt 加「写入收敛」章节，把"首选收敛"列在清理清单首位
- `~/obsidian-vault/MEMORY-ROUTING.md`（真源：VMware 共享盘）  
  加 §6.1 写入收敛规约：分层表 + 取代写法 + 不堆 insight

## 验证（TestClient + 临时 /tmp DB）

- save r1 / r2（两条冲突事实）
- save r3 传 `superseded_by=r1.id`  
  ✅ r1.archived=1, r1.superseded_by=r3.id  
  ✅ r3.archived=0, r3.superseded_by=None  
- 默认 search "V4预案" → 只见 r2 + r3（不见 r1）  
  ✅  
- `include_archived=True` → 见全链 r3 → r1（archived）→ r2  
  ✅  
- 再 save r4 `superseded_by=r2.id`  
  ✅ r2 也被 archive + superseded_by=r4  
- mem_lint 不再报 `superseded_but_active`（写入路径自动清理）  
  ✅  
- `/mcp/stats` total=3 active=2 archived=1  
  ✅  
- `/health` 返回 `version: 5.2.0`  
  ✅  

## 对三个 profile 的意义

| Profile | 取代链会发生吗 | 谁触发 |
|---------|----------------|--------|
| coder | 部署坑、API 写法、新技术取代旧的 | 写入端 coder agent + Reflection |
| work | 预案定稿、流程版本、政策更新 | Reflection（23:00 daily）+ write-in agent |
| daily | 很少（生活偏好稳定） | 几乎不触发 |

## 拉取语义（不变）

- 其它 Agent prefetch top5 / search limit=10 仍按相关度截断活跃层
- 默认活跃层 = 现在的「当前生效」
- 版本历史仍在 `memory_versions`，不会自动注入；要看时 `mem_history`

## 部署

- 本地：4 个文件改动 → `git commit` → push
- 生产：服务器 `git pull` + `docker compose build --no-cache && up -d` → `/health` 期望 5.2.0
- 部署后建议跑一次 Reflection：它会扫到所有 `superseded_but_active` 旧脏数据并清理

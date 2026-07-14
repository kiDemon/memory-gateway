# Code Fixes 2026-07-14

基于 `references/code-review-2026-07-14.md` 的全量修复（本地源码）。

## 版本

- 统一为 **5.1.1**（`APP_VERSION` / OpenAPI / `/health` / 登录页）

## P0

| 项 | 文件 | 修复 |
|----|------|------|
| 短文本 SimHash 全 0 | `utils/crypto.py` | 1/2 token 走 unigram/bigram；空内容 hash 原文；全 0 指纹跳过 near_dup |
| near_dup 只扫 1000 | `utils/crypto.py` | `ORDER BY created_at DESC LIMIT 5000`，跳过全 0 simhash |
| 双 HotCache | `server.py` | 改为 alias `_shared.hot_cache`，唯一实例 |
| save 不失效缓存 | `routers/memories.py` | `save_memory` / `batch_save` 成功后 `hot_cache.clear()` |

## P1

| 项 | 文件 | 修复 |
|----|------|------|
| API Key `==` | `middleware/auth.py` | `hmac.compare_digest` + 等长检查 |
| localStorage 双 key | login HTML + `dashboard.html` | 统一写/读 `mg_api_key`，兼容旧 `memory_gateway_key`；登录直跳 `/dashboard` |
| Key 轮转不清 session | `admin.py` + `_clear_all_sessions` | rotate/reset/set（admin + settings）均清会话 |
| 版本漂移 | `server.py` / login | 5.1.1 |
| category 缺失 | `_shared.row_to_dict` + dashboard list | 增加 `category` alias |

## P2

| 项 | 文件 | 修复 |
|----|------|------|
| Dashboard LIKE 搜索 | `dashboard.py` | 关键词优先 FTS5 + bm25，失败回退 LIKE |
| Graph N+1 | `dashboard.py` | degree 一次 GROUP BY |
| offload 无 REST | `memories.py` | `POST /mcp/offload`、`GET /mcp/drilldown/{id}` |
| CORS 缺生产 IP | `server.py` | 默认 origins 含 `http://8.137.178.236:8650` |
| Obsidian skill 路径 | coder skill | fallback 软链/共享真源 |

## 顺带（已有未提交改动中）

- `memories.py`：`_escape_like` + LIKE ESCAPE；hybrid search 补 recall/audit；scenario 按 days 过滤  
  （本次顺手修了 `_escape_like` 字符串转义语法错误）

## 本地验证

```bash
/home/kidemon/.hermes/hermes-agent/venv/bin/python - <<'PY'
# simhash 7 样本 unique、无全 0；APP_VERSION 5.1.1；
# server/shared hot_cache 同 id；offload/drilldown 路由存在
PY
```

## 部署（生产未自动推）

```bash
# 本机
cd ~/.hermes/memory-gateway
git add -A && git commit -m "fix: P0/P1/P2 audit fixes for Web UI, simhash, cache, auth (v5.1.1)"
# push 若 TLS 超时：重试或 git push --set-upstream origin main

# 阿里云 1Panel / SSH
cd <app-dir> && git pull && docker compose build --no-cache && docker compose up -d
curl -s http://127.0.0.1:8650/health   # version 应为 5.1.1
```

**注意**：Key 轮转后会话会清空，浏览器需重新登录；客户端 config 两处 key 仍需人工同步。

# Memory Gateway — 安全加固 + Dashboard 设置整合 实施计划

## 目标
1. 把 `/admin` 的 API Key 管理功能合并到 dashboard 的新"设置"标签页
2. 添加完整的登录/登出流程
3. 添加登录日志 + 失败锁定（只能 Docker 解锁）
4. 修复关键安全漏洞

## 架构
- 后端：server.py 添加新路由 + SQLite 登录日志表 + session token 管理
- 前端：dashboard.html 添加第6个标签页"设置" + 登出按钮
- 解锁：只能通过 `docker exec` 执行 SQL 解锁，无 HTTP 解锁端点

## 改动文件
- `server.py` — 后端路由 + 安全中间件
- `static/dashboard.html` — 前端设置页面 + 登出

---

## Task 1: 后端 — 登录日志表 + 失败锁定

**文件**: `server.py`

在 `init_db()` 中添加 `login_attempts` 表：

```sql
CREATE TABLE IF NOT EXISTS login_attempts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ip_address TEXT NOT NULL,
    success BOOLEAN NOT NULL DEFAULT 0,
    attempted_at TEXT NOT NULL DEFAULT (datetime('now')),
    user_agent TEXT DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_login_attempts_ip ON login_attempts(ip_address, attempted_at);
```

添加锁定配置：
- 连续失败 5 次 → 锁定 30 分钟
- 锁定状态存储在内存 dict 中（重启自动解锁）

---

## Task 2: 后端 — 登录端点加固

**文件**: `server.py` — `/admin/login` 路由

改造点：
1. 记录每次登录尝试到 `login_attempts` 表（IP + 成功/失败 + user_agent）
2. 检查 IP 是否被锁定（内存 dict）
3. 登录成功后生成随机 session token（而非直接存 API Key 到 cookie）
4. session token 存储在内存 dict 中，带过期时间

---

## Task 3: 后端 — 登出端点

**文件**: `server.py`

添加 `POST /admin/logout`：
1. 从 cookie 获取 session token
2. 从内存 session dict 中删除
3. 清除 cookie
4. 返回 `{success: true}`

---

## Task 4: 后端 — 设置 API 路由

**文件**: `server.py`

添加以下路由（合并 admin 功能）：
- `GET /api/settings/apikey` — 获取当前 key 信息（masked）
- `POST /api/settings/apikey/rotate` — 轮换 key
- `POST /api/settings/apikey/set` — 设置自定义 key
- `GET /api/settings/login-logs` — 获取最近 50 条登录日志
- `GET /api/settings/lockout-status` — 获取当前锁定状态

---

## Task 5: 后端 — CORS 中间件

**文件**: `server.py`

添加 CORS 中间件，只允许同源请求：
```python
from fastapi.middleware.cors import CORSMiddleware
app.add_middleware(CORSMiddleware, allow_origins=[], allow_credentials=True, ...)
```

---

## Task 6: 前端 — 添加设置标签页

**文件**: `static/dashboard.html`

添加第6个标签页"⚙️ 设置"，包含：
1. **API Key 管理**：显示当前 key（masked）、轮换、设置自定义 key
2. **登出按钮**：清除 cookie + localStorage，跳转登录页
3. **登录日志**：表格显示最近 50 条登录尝试（时间、IP、成功/失败、UA）
4. **锁定状态**：显示当前被锁定的 IP 列表

---

## Task 7: 前端 — 登出按钮 + 认证改进

**文件**: `static/dashboard.html`

1. 在页面右上角添加"登出"按钮
2. 登出时调用 `POST /admin/logout` + 清除 localStorage
3. API 请求改用 session cookie（而非 localStorage 中的 key）

---

## Task 8: Docker 解锁脚本

在 Dockerfile 或 entrypoint.sh 中添加解锁命令：
```bash
# 解锁指定 IP
docker exec memory-gateway python3 -c "
import sqlite3, json
db = sqlite3.connect('/data/memory.db')
# 清除锁定状态
print('Lockout cleared')
"
```

---

## 验证步骤
1. 登录成功 → 检查 login_attempts 表有记录
2. 连续 5 次错误密码 → 检查锁定
3. 锁定后尝试登录 → 返回锁定提示
4. Docker 解锁 → 重新登录成功
5. 点击登出 → 清除 session → 跳转登录页
6. 设置页面 → 能看到 key 信息、日志、锁定状态

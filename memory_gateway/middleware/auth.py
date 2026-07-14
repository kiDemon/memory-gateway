"""
Authentication middleware and session management for Memory Gateway.

Provides:
  - In-memory + SQLite-persisted session token management
  - IP-based failure tracking and lockout
  - Login page HTML generator
  - ``api_key_middleware`` (FastAPI middleware) that validates X-API-Key header
    or cookie-based session tokens

Module-level state
------------------
``API_KEY`` must be set by the application after import::

    from memory_gateway.middleware.auth import API_KEY as auth_API_KEY
    auth_API_KEY = api_key_value   # set by server.py at module init
"""
import hashlib
import hmac
import logging
import os
import secrets
import time

from fastapi import Request
from fastapi.responses import HTMLResponse, JSONResponse

from memory_gateway.config import log
from memory_gateway.database.connection import db_conn
from memory_gateway.middleware.security import _is_same_origin

# ── Runtime API key ──────────────────────────────────────
# Set by server.py after loading the key (avoids circular import).
API_KEY: str = ""

# ── Session Token Management ──────────────────────────────

# 内存缓存用于快速查找，SQLite用于持久化
_sessions_cache: dict[str, dict] = {}  # token -> {ip, created_at, expires_at}
_login_failures_cache: dict[str, list[float]] = {}  # ip -> [timestamps]
_locked_ips_cache: dict[str, float] = {}  # ip -> unlock_time

MAX_LOGIN_FAILURES = int(os.environ.get("MEMORY_MAX_LOGIN_FAILURES", "5"))
LOCKOUT_DURATION = int(os.environ.get("MEMORY_LOCKOUT_DURATION", "1800"))  # 30 minutes
SESSION_DURATION = int(os.environ.get("MEMORY_SESSION_DURATION", "604800"))  # 7 days (86400 * 7)

COOKIE_NAME = "memory_gateway_session"


# ── Session helpers ───────────────────────────────────────


def _create_session(ip: str) -> str:
    """创建会话并持久化到SQLite。"""
    token = secrets.token_urlsafe(32)
    now = time.time()
    expires_at = now + SESSION_DURATION

    # 写入内存缓存
    _sessions_cache[token] = {
        "ip": ip,
        "created_at": now,
        "expires_at": expires_at,
    }

    # 持久化到SQLite
    try:
        with db_conn() as db:
            db.execute(
                "INSERT INTO user_sessions (token, ip_address, created_at, expires_at) VALUES (?, ?, ?, ?)",
                (token, ip, now, expires_at),
            )
    except Exception as e:
        log.warning(f"Failed to persist session: {e}", exc_info=True)

    return token


def _validate_session(token: str) -> bool:
    """验证会话，优先从内存缓存读取，回退到SQLite。"""
    # 先检查内存缓存
    if token in _sessions_cache:
        if time.time() > _sessions_cache[token]["expires_at"]:
            del _sessions_cache[token]
            # 也从SQLite删除
            try:
                with db_conn() as db:
                    db.execute("DELETE FROM user_sessions WHERE token=?", (token,))
            except Exception:
                log.warning("Failed to delete expired session from DB (non-fatal)", exc_info=True)
            return False
        return True

    # 内存缓存未命中，从SQLite读取
    try:
        with db_conn() as db:
            row = db.execute(
                "SELECT ip_address, created_at, expires_at FROM user_sessions WHERE token=?",
                (token,),
            ).fetchone()
            if not row:
                return False
            if time.time() > row["expires_at"]:
                db.execute("DELETE FROM user_sessions WHERE token=?", (token,))
                return False
            # 回填到内存缓存
            _sessions_cache[token] = {
                "ip": row["ip_address"],
                "created_at": row["created_at"],
                "expires_at": row["expires_at"],
            }
            return True
    except Exception as e:
        log.warning(f"Failed to validate session: {e}", exc_info=True)
        return False


def _delete_session(token: str) -> None:
    """删除会话。"""
    _sessions_cache.pop(token, None)
    try:
        with db_conn() as db:
            db.execute("DELETE FROM user_sessions WHERE token=?", (token,))
    except Exception:
        log.warning("Failed to delete session (non-fatal)", exc_info=True)


def _clear_all_sessions() -> int:
    """Key 轮转/重置时清空全部会话，避免旧 cookie 继续访问管理端。"""
    count = len(_sessions_cache)
    _sessions_cache.clear()
    try:
        with db_conn() as db:
            cur = db.execute("DELETE FROM user_sessions")
            # sqlite3 rowcount may be -1; prefer cache size as floor
            if cur.rowcount and cur.rowcount > 0:
                count = max(count, cur.rowcount)
    except Exception:
        log.warning("Failed to clear all sessions from DB (non-fatal)", exc_info=True)
    log.warning("Cleared %s session(s) after API key change", count)
    return count


# ── IP lockout helpers ────────────────────────────────────


def _is_ip_locked(ip: str) -> bool:
    """检查IP是否被锁定。"""
    # 检查内存缓存
    if ip in _locked_ips_cache:
        if time.time() >= _locked_ips_cache[ip]:
            del _locked_ips_cache[ip]
            # 也从SQLite删除
            try:
                with db_conn() as db:
                    db.execute("DELETE FROM ip_lockouts WHERE ip_address=?", (ip,))
            except Exception:
                log.warning("Failed to delete expired IP lockout from DB (non-fatal)", exc_info=True)
            return False
        return True

    # 检查SQLite
    try:
        with db_conn() as db:
            row = db.execute(
                "SELECT unlock_time FROM ip_lockouts WHERE ip_address=?",
                (ip,),
            ).fetchone()
            if not row:
                return False
            if time.time() >= row["unlock_time"]:
                db.execute("DELETE FROM ip_lockouts WHERE ip_address=?", (ip,))
                return False
            _locked_ips_cache[ip] = row["unlock_time"]
            return True
    except Exception:
        log.warning("Failed to check IP lockout status (returning False)", exc_info=True)
        return False


def _record_failure(ip: str) -> bool:
    """记录登录失败，返回True表示IP被锁定。"""
    now = time.time()
    cutoff = now - LOCKOUT_DURATION

    # 更新内存缓存
    if ip not in _login_failures_cache:
        _login_failures_cache[ip] = []
    _login_failures_cache[ip] = [t for t in _login_failures_cache[ip] if t > cutoff]
    _login_failures_cache[ip].append(now)

    if len(_login_failures_cache[ip]) >= MAX_LOGIN_FAILURES:
        _locked_ips_cache[ip] = now + LOCKOUT_DURATION
        log.warning(
            "IP %s locked out for %d seconds (failed %d times)",
            ip,
            LOCKOUT_DURATION,
            len(_login_failures_cache[ip]),
        )

        # 持久化锁定到SQLite
        try:
            with db_conn() as db:
                db.execute(
                    "INSERT OR REPLACE INTO ip_lockouts (ip_address, unlock_time, failure_count) VALUES (?, ?, ?)",
                    (ip, now + LOCKOUT_DURATION, len(_login_failures_cache[ip])),
                )
                # 记录失败尝试
                db.execute(
                    "INSERT INTO login_attempts (ip_address, success, user_agent) VALUES (?, 0, 'memory-gateway')",
                    (ip,),
                )
        except Exception as e:
            log.warning(f"Failed to persist lockout: {e}")

        return True

    # 记录失败尝试
    try:
        with db_conn() as db:
            db.execute(
                "INSERT INTO login_attempts (ip_address, success, user_agent) VALUES (?, 0, 'memory-gateway')",
                (ip,),
            )
    except Exception:
        log.warning("Failed to record login failure (non-fatal)", exc_info=True)

    return False


def _clear_failures(ip: str) -> None:
    """清除IP的失败记录和锁定。"""
    _login_failures_cache.pop(ip, None)
    _locked_ips_cache.pop(ip, None)
    try:
        with db_conn() as db:
            db.execute("DELETE FROM ip_lockouts WHERE ip_address=?", (ip,))
    except Exception:
        log.warning("Failed to clear IP lockouts (non-fatal)", exc_info=True)


# ── Cookie-based session auth ────────────────────────────


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
  <h1>Memory Gateway v5.1.1</h1>
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
      // 与 dashboard 统一使用 mg_api_key；兼容旧 key 一并写入
      localStorage.setItem('mg_api_key', key);
      localStorage.setItem('memory_gateway_key', key);
      window.location.href = '/dashboard';
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


# ── Auth middleware ────────────────────────────────────────


async def api_key_middleware(request: Request, call_next):
    """FastAPI middleware that authenticates requests.

    Handles three auth methods in order:
    1. X-API-Key header (or Authorization: Bearer)
    2. Cookie-based session token (with CSRF protection for mutating methods)
    3. Returns 401 / login page when no valid credentials are found

    Must be registered via ``app.middleware("http")(api_key_middleware)``.
    """
    global API_KEY

    # Allow health check without auth
    if request.url.path == "/health":
        return await call_next(request)

    # Allow login endpoint without auth
    if request.url.path == "/admin/login":
        return await call_next(request)

    # Allow static files (CSS, JS, images) without auth
    if request.url.path.startswith("/static/"):
        return await call_next(request)

    if API_KEY:
        # Check header (X-API-Key or Authorization: Bearer)
        key = request.headers.get("X-API-Key", "") or request.headers.get(
            "Authorization", ""
        ).removeprefix("Bearer ")
        # compare_digest 要求等长，否则抛 ValueError
        if key and len(key) == len(API_KEY) and hmac.compare_digest(key, API_KEY):
            return await call_next(request)

        # Check session cookie (uses token, not raw API Key)
        cookie_token = request.cookies.get(COOKIE_NAME, "")
        if cookie_token and _validate_session(cookie_token):
            # CSRF check: for state-changing requests authenticated via cookie,
            # require same-origin Referer/Origin to prevent CSRF.
            if request.method in ("POST", "PUT", "DELETE", "PATCH"):
                origin = request.headers.get("Origin", "")
                referer = request.headers.get("Referer", "")
                # Allow requests with API key header (already checked above)
                # For cookie-only auth: check origin/referer is same-origin
                if not _is_same_origin(request, origin or referer):
                    log.warning(
                        "CSRF attempt blocked: %s %s from Origin=%s Referer=%s",
                        request.method,
                        request.url.path,
                        origin,
                        referer,
                    )
                    return JSONResponse(
                        status_code=403,
                        content={
                            "error": "Forbidden",
                            "detail": "CSRF check failed: mismatched origin",
                        },
                    )
            return await call_next(request)

        # Auth failed — return login page for browser, JSON for API
        if request.url.path == "/" or request.url.path.startswith("/admin") or request.url.path == "/dashboard":
            return HTMLResponse(
                status_code=401,
                content=login_page_html(),
            )
        return JSONResponse(
            status_code=401,
            content={
                "error": "Unauthorized",
                "detail": "Valid X-API-Key header required",
            },
        )

    # No API key configured — open access
    return await call_next(request)

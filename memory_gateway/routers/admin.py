"""
Admin and Settings API endpoints.

Routes:
  /admin/*             — Admin management (login, logout, API key management)
  /api/settings/*      — Settings API (API key info, login logs, lockout status)
"""

import hmac
import os
import time
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse, RedirectResponse

from memory_gateway.config import KEY_FILE, STATIC_DIR, log
from memory_gateway.database.connection import db_conn
from memory_gateway.middleware import (
    COOKIE_NAME,
    LOCKOUT_DURATION,
    SESSION_DURATION,
    _clear_failures,
    _create_session,
    _delete_session,
    _is_ip_locked,
    _is_same_origin,
    _locked_ips_cache,
    _record_failure,
    _sessions_cache,
    login_page_html,
)
from memory_gateway.middleware import auth as _auth_mw
from memory_gateway.models.requests import LoginRequest, SetKeyRequest
from memory_gateway.utils import _generate_api_key

router = APIRouter()


# ── Login / Logout ────────────────────────────────────────


@router.post("/admin/login")
async def admin_login(body: LoginRequest, request: Request):
    """Validate API key and set session cookie with token-based session.
    Includes login logging, IP-based failure tracking, and lockout."""
    ip = request.client.host if request.client else "unknown"
    ua = request.headers.get("User-Agent", "")

    # Check IP lockout
    if _is_ip_locked(ip):
        remaining = int(_locked_ips_cache[ip] - time.time())
        log.warning("Login attempt from locked IP %s (remaining: %ds)", ip, remaining)
        with db_conn() as db:
            db.execute(
                "INSERT INTO login_attempts (ip_address, success, user_agent) VALUES (?, 0, ?)",
                (ip, ua),
            )
        raise HTTPException(
            status_code=429,
            detail=f"IP 已被临时锁定，请在 {remaining} 秒后重试",
        )

    if _auth_mw.API_KEY and hmac.compare_digest(body.key, _auth_mw.API_KEY):
        # Successful login
        _clear_failures(ip)
        token = _create_session(ip)
        with db_conn() as db:
            db.execute(
                "INSERT INTO login_attempts (ip_address, success, user_agent) VALUES (?, 1, ?)",
                (ip, ua),
            )
        log.info("Successful login from %s", ip)
        is_https = request.url.scheme == "https"
        resp = JSONResponse({"success": True})
        resp.set_cookie(
            key=COOKIE_NAME,
            value=token,
            max_age=SESSION_DURATION,
            httponly=True,
            secure=is_https,
            samesite="strict" if is_https else "lax",
            path="/",
        )
        return resp

    # Failed login
    with db_conn() as db:
        db.execute(
            "INSERT INTO login_attempts (ip_address, success, user_agent) VALUES (?, 0, ?)",
            (ip, ua),
        )
    just_locked = _record_failure(ip)
    if just_locked:
        log.warning("IP %s locked due to excessive login failures", ip)
        raise HTTPException(
            status_code=429,
            detail=f"登录失败次数过多，IP 已被锁定 {LOCKOUT_DURATION} 秒",
        )
    raise HTTPException(status_code=401, detail="密钥无效，请检查 API Key 是否正确")


@router.post("/admin/logout")
async def admin_logout(request: Request):
    """Logout: delete session token and clear cookie."""
    origin = request.headers.get("Origin", "")
    referer = request.headers.get("Referer", "")
    if not _is_same_origin(request, origin or referer):
        raise HTTPException(status_code=403, detail="CSRF check failed")
    token = request.cookies.get(COOKIE_NAME, "")
    if token and token in _sessions_cache:
        del _sessions_cache[token]
        # 也从SQLite删除
        try:
            with db_conn() as db:
                db.execute("DELETE FROM user_sessions WHERE token=?", (token,))
        except Exception:
            log.warning("Failed to delete session on logout (non-fatal)", exc_info=True)
    resp = JSONResponse({"success": True, "message": "已登出"})
    resp.delete_cookie(
        key=COOKIE_NAME,
        path="/",
    )
    return resp


# ── Admin Pages ───────────────────────────────────────────


@router.get("/admin")
async def admin_page(request: Request):
    """Redirect to unified dashboard."""
    return RedirectResponse(url="/dashboard", status_code=302)


# ── API Key Management ────────────────────────────────────


@router.get("/admin/apikey")
async def get_apikey_info() -> dict:
    """Return current API key info (masked)."""
    key = _auth_mw.API_KEY
    masked = key[:16] + "..." + key[-4:] if len(key) > 24 else "****"
    return {
        "masked": masked,
        "length": len(key),
        "source": "environment" if os.environ.get("MEMORY_API_KEY", "").strip() else (
            "file" if KEY_FILE.exists() else "auto-generated"
        ),
    }


@router.post("/admin/apikey/rotate")
async def rotate_apikey(request: Request) -> dict:
    """Generate a new API key, persist to file, and update runtime.

    The old key is immediately invalidated.
    Environment variable key cannot be rotated — set MEMORY_API_KEY to empty first.
    """
    origin = request.headers.get("Origin", "")
    referer = request.headers.get("Referer", "")
    if not _is_same_origin(request, origin or referer):
        raise HTTPException(status_code=403, detail="CSRF check failed")

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
        log.warning("Could not set permissions on API key file (non-fatal)", exc_info=True)
    _auth_mw.API_KEY = new_key
    log.warning("API Key rotated — new key saved to %s", KEY_FILE)
    masked = new_key[:16] + "..." + new_key[-4:] if len(new_key) > 24 else "****"
    return {"success": True, "masked": masked, "message": "Key rotated. All clients must update."}


@router.post("/admin/apikey/reset")
async def reset_apikey(request: Request) -> dict:
    """Delete the key file and auto-generate a new key."""
    origin = request.headers.get("Origin", "")
    referer = request.headers.get("Referer", "")
    if not _is_same_origin(request, origin or referer):
        raise HTTPException(status_code=403, detail="CSRF check failed")

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
        log.warning("Could not set permissions on API key file (non-fatal)", exc_info=True)
    _auth_mw.API_KEY = new_key
    log.warning("API Key reset — new key saved to %s", KEY_FILE)
    masked = new_key[:16] + "..." + new_key[-4:] if len(new_key) > 24 else "****"
    return {"success": True, "masked": masked, "message": "Key reset and regenerated."}


@router.post("/admin/apikey/set")
async def set_apikey(req: SetKeyRequest, request: Request) -> dict:
    """Set a custom API key. Replaces the current key immediately."""
    origin = request.headers.get("Origin", "")
    referer = request.headers.get("Referer", "")
    if not _is_same_origin(request, origin or referer):
        raise HTTPException(status_code=403, detail="CSRF check failed")

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
        log.warning("Could not set permissions on API key file (non-fatal)", exc_info=True)
    _auth_mw.API_KEY = new_key
    masked = new_key[:16] + "..." + new_key[-4:] if len(new_key) > 24 else "****"
    log.warning("API Key manually set — saved to %s", KEY_FILE)
    return {"success": True, "masked": masked, "message": "Custom key set."}


# ── Settings API Routes ──────────────────────────────────


@router.get("/api/settings/apikey")
async def settings_apikey_info() -> dict:
    """Return current API key info (masked + source)."""
    key = _auth_mw.API_KEY
    masked = key[:16] + "..." + key[-4:] if len(key) > 24 else "****"
    return {
        "masked": masked,
        "length": len(key),
        "source": "environment" if os.environ.get("MEMORY_API_KEY", "").strip() else (
            "file" if KEY_FILE.exists() else "auto-generated"
        ),
    }


@router.post("/api/settings/apikey/rotate")
async def settings_apikey_rotate(request: Request) -> dict:
    """Generate a new API key, persist to file, and update runtime."""
    origin = request.headers.get("Origin", "")
    referer = request.headers.get("Referer", "")
    if not _is_same_origin(request, origin or referer):
        raise HTTPException(status_code=403, detail="CSRF check failed")

    if os.environ.get("MEMORY_API_KEY", "").strip():
        raise HTTPException(
            status_code=400,
            detail="Cannot rotate key set via MEMORY_API_KEY env var."
        )

    new_key = _generate_api_key()
    KEY_FILE.write_text(new_key)
    try:
        os.chmod(KEY_FILE, 0o600)
    except Exception:
        log.warning("Could not set permissions on API key file (non-fatal)", exc_info=True)
    _auth_mw.API_KEY = new_key
    log.warning("API Key rotated via /api/settings/apikey/rotate — saved to %s", KEY_FILE)
    masked = new_key[:16] + "..." + new_key[-4:] if len(new_key) > 24 else "****"
    return {"success": True, "masked": masked, "message": "Key rotated."}


@router.post("/api/settings/apikey/set")
async def settings_apikey_set(req: SetKeyRequest, request: Request) -> dict:
    """Set a custom API key."""
    origin = request.headers.get("Origin", "")
    referer = request.headers.get("Referer", "")
    if not _is_same_origin(request, origin or referer):
        raise HTTPException(status_code=403, detail="CSRF check failed")

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
        log.warning("Could not set permissions on API key file (non-fatal)", exc_info=True)
    _auth_mw.API_KEY = new_key
    masked = new_key[:16] + "..." + new_key[-4:] if len(new_key) > 24 else "****"
    log.warning("API Key manually set via /api/settings/apikey/set — saved to %s", KEY_FILE)
    return {"success": True, "masked": masked, "message": "Custom key set."}


@router.get("/api/settings/login-logs")
async def settings_login_logs(limit: int = 50):
    """Return recent login attempt logs (IPs are masked for privacy)."""
    with db_conn() as db:
        rows = db.execute(
            "SELECT id, ip_address, success, attempted_at, user_agent "
            "FROM login_attempts ORDER BY attempted_at DESC LIMIT ?",
            (limit,),
        ).fetchall()
    logs = []
    for r in rows:
        entry = dict(r)
        ip = entry.get("ip_address", "")
        # Mask last octet for privacy (IPv4 only)
        if ip and ":" not in ip:
            parts = ip.rsplit(".", 1)
            if len(parts) == 2:
                entry["ip_address"] = parts[0] + ".xxx"
        elif ip and ":" in ip:
            # IPv6: mask last 4 groups
            parts = ip.rsplit(":", 4)
            if len(parts) == 5:
                entry["ip_address"] = parts[0] + ":xxxx:xxxx:xxxx:xxxx"
        logs.append(entry)
    return {"logs": logs, "total": len(logs)}


@router.get("/api/settings/lockout-status")
async def settings_lockout_status():
    """Return currently locked IPs."""
    now = time.time()
    locked = {}

    # 从内存缓存获取
    for ip, unlock_time in list(_locked_ips_cache.items()):
        if now < unlock_time:
            # Mask last octet for privacy (same as login-logs)
            masked_ip = ip
            if ip and ":" not in ip:
                parts = ip.rsplit(".", 1)
                if len(parts) == 2:
                    masked_ip = parts[0] + ".xxx"
            elif ip and ":" in ip:
                parts = ip.rsplit(":", 4)
                if len(parts) == 5:
                    masked_ip = parts[0] + ":xxxx:xxxx:xxxx:xxxx"
            locked[masked_ip] = {
                "unlock_at": datetime.fromtimestamp(unlock_time, tz=timezone.utc).isoformat(),
                "remaining_seconds": int(unlock_time - now),
            }
    return {"locked_ips": locked, "count": len(locked)}

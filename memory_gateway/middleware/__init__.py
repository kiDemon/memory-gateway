"""
middleware — Auth and security middleware for Memory Gateway.

Modules:
  auth     Session management, login page, API key + cookie authentication
  security Security headers (CSP, X-Frame-Options, etc.) and CSRF origin check
"""

from memory_gateway.middleware.auth import (
    API_KEY,
    COOKIE_NAME,
    SESSION_DURATION,
    LOCKOUT_DURATION,
    _sessions_cache,
    _locked_ips_cache,
    _create_session,
    _validate_session,
    _delete_session,
    _is_ip_locked,
    _record_failure,
    _clear_failures,
    login_page_html,
    api_key_middleware,
)
from memory_gateway.middleware.security import (
    _is_same_origin,
    security_headers_middleware,
)

__all__ = [
    "API_KEY",
    "COOKIE_NAME",
    "SESSION_DURATION",
    "LOCKOUT_DURATION",
    "_sessions_cache",
    "_locked_ips_cache",
    "_create_session",
    "_validate_session",
    "_delete_session",
    "_is_ip_locked",
    "_record_failure",
    "_clear_failures",
    "login_page_html",
    "api_key_middleware",
    "_is_same_origin",
    "security_headers_middleware",
]

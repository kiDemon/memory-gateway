"""
Security headers middleware and CSRF origin check helper.

Implements:
  - security_headers_middleware: Adds CSP, X-Frame-Options, and other security
    headers to every HTTP response.
  - _is_same_origin: Validates that a URL/Origin/Referer header matches the
    server's own origin (used for CSRF protection).
"""

from fastapi import Request

from memory_gateway.config import log


def _is_same_origin(request: Request, url_str: str) -> bool:
    """Check if a URL/Origin/Referer matches the server's own origin."""
    if not url_str:
        return False
    try:
        from urllib.parse import urlparse

        parsed = urlparse(url_str)
        # No host means relative URL (same-origin)
        if not parsed.hostname:
            return True
        # Compare hostname and port against the request URL
        req_parsed = urlparse(str(request.url))
        if parsed.hostname == req_parsed.hostname and parsed.port == req_parsed.port:
            return True
        # Also accept localhost variants
        if parsed.hostname in ("localhost", "127.0.0.1") and req_parsed.hostname in (
            "localhost",
            "127.0.0.1",
        ):
            return True
    except Exception:
        log.warning("Failed to validate URL origin (returning False)", exc_info=True)
    return False


async def security_headers_middleware(request: Request, call_next):
    """Add security headers to every response.

    This middleware must be registered on the FastAPI app via
    ``app.middleware("http")(security_headers_middleware)``.
    """
    resp = await call_next(request)
    resp.headers.setdefault("X-Content-Type-Options", "nosniff")
    resp.headers.setdefault("X-Frame-Options", "DENY")
    resp.headers.setdefault("X-XSS-Protection", "1; mode=block")
    resp.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
    resp.headers.setdefault(
        "Content-Security-Policy",
        "default-src 'self'; script-src 'self' 'unsafe-inline' cdn.jsdelivr.net; "
        "connect-src 'self' cdn.jsdelivr.net; style-src 'self' 'unsafe-inline'",
    )
    return resp

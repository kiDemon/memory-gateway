"""Privacy — sensitive information filtering."""

import logging
import re

log = logging.getLogger("memory-server")

PRIVACY_PATTERNS = [
    (re.compile(r'(?i)(api[_-]?key|apikey)\s*[:=]\s*["\']?[A-Za-z0-9_\-]{16,}'), r'\1 [REDACTED]'),
    (re.compile(r'(?i)(secret|password|token)\s*[:=]\s*["\']?[A-Za-z0-9_\-\.]{8,}'), r'\1 [REDACTED]'),
    (re.compile(r'(?i)(bearer)\s+[A-Za-z0-9_\-\.]{20,}'), 'Bearer [REDACTED]'),
    (re.compile(r'(sk-[a-zA-Z0-9]{20,})'), '[API-KEY-REDACTED]'),
]


def _filter_sensitive(content: str) -> str:
    """Strip sensitive information from content before saving."""
    result = content
    for pattern, replacement in PRIVACY_PATTERNS:
        before = result
        result = pattern.sub(replacement, result)
        if result != before:
            log.debug("Privacy filter redacted content")
    return result

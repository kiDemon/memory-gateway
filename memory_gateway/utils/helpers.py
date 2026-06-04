"""
Shared helper functions extracted from server.py.

Provides:
  - _generate_api_key  — cryptographically secure API key generation
  - _build_timeline    — date-by-count timeline builder (used by dashboard + stats)
"""

import secrets
import sqlite3
from datetime import datetime, timedelta, timezone


def _generate_api_key() -> str:
    """Generate a cryptographically secure random API key."""
    return "sk-mg-" + secrets.token_urlsafe(36)


def _build_timeline(db: sqlite3.Connection, days: int = 30) -> list[dict]:
    """Build a date-by-count timeline for the last N days.

    Returns list of {date: "MM-DD", count: N} ordered ascending.
    DRY helper used by dashboard and stats endpoints.
    """
    timeline = []
    for i in range(days - 1, -1, -1):
        d = datetime.now(timezone.utc) - timedelta(days=i)
        date_str = d.strftime("%Y-%m-%d")
        cnt = db.execute(
            "SELECT COUNT(*) FROM memories WHERE created_at LIKE ?", (date_str + "%",)
        ).fetchone()[0]
        timeline.append({"date": d.strftime("%m-%d"), "count": cnt})
    return timeline

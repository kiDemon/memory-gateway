"""
Database connection management.

Provides get_db() for raw connections and db_conn() as a context manager.
Also provides migration-lock helpers used by schema.py.
"""

import logging
import os
import sqlite3
from contextlib import contextmanager

from memory_gateway.config import DB_PATH, log

# ── Connection ───────────────────────────────────────────


def get_db() -> sqlite3.Connection:
    """Open a new SQLite connection to the memory database."""
    db = sqlite3.connect(str(DB_PATH), timeout=10)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("PRAGMA foreign_keys=ON")
    db.execute("PRAGMA busy_timeout=5000")
    return db


@contextmanager
def db_conn() -> sqlite3.Connection:
    """Context manager that yields a database connection and auto-commits."""
    conn = get_db()
    try:
        yield conn
        conn.commit()
    except Exception:
        log.warning("Database transaction failed, rolling back", exc_info=True)
        conn.rollback()
        raise
    finally:
        conn.close()


# ── Migration Lock ────────────────────────────────────────


def _acquire_migration_lock(db: sqlite3.Connection) -> bool:
    """Acquire a migration lock to prevent concurrent migrations."""
    try:
        db.execute("BEGIN IMMEDIATE")
        result = db.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='migration_lock'"
        ).fetchone()
        if not result:
            db.execute("""
                CREATE TABLE IF NOT EXISTS migration_lock (
                    id INTEGER PRIMARY KEY DEFAULT 1,
                    locked_at TEXT NOT NULL,
                    locked_by TEXT DEFAULT 'system'
                )
            """)
            db.commit()

        db.execute("DELETE FROM migration_lock WHERE id=1")
        db.execute(
            "INSERT INTO migration_lock (id, locked_at, locked_by) VALUES (1, datetime('now'), ?)",
            (os.environ.get('HOSTNAME', 'local'),)
        )
        db.commit()
        log.info("Migration lock acquired")
        return True
    except Exception as e:
        log.warning(f"Failed to acquire migration lock: {e}", exc_info=True)
        return False


def _release_migration_lock(db: sqlite3.Connection) -> None:
    """Release the migration lock."""
    try:
        db.execute("DELETE FROM migration_lock WHERE id=1")
        db.commit()
        log.info("Migration lock released")
    except Exception as e:
        log.warning(f"Failed to release migration lock: {e}", exc_info=True)

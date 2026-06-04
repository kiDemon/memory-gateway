"""
memory_gateway — MCP Memory Server package.

Modules:
  config       Configuration (environment vars, paths, logging)
  database     Database connection & schema management
"""

from memory_gateway.config import DATA_DIR, DB_PATH, KEY_FILE, LOG_LEVEL, log
from memory_gateway.database.connection import get_db, db_conn
from memory_gateway.database.schema import init_db

__all__ = [
    "DATA_DIR", "DB_PATH", "KEY_FILE", "LOG_LEVEL", "log",
    "get_db", "db_conn", "init_db",
]

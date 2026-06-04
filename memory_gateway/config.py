"""
Configuration management for the MCP Memory Server.

Reads environment variables, defines paths and constants.
Priority: environment variable > file-based overrides > built-in defaults.
"""

import logging
import os
from pathlib import Path

# ── Logging ──────────────────────────────────────────────

LOG_LEVEL = os.environ.get("MEMORY_LOG_LEVEL", "INFO")
logging.basicConfig(
    level=getattr(logging, LOG_LEVEL),
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger("memory-server")

# ── Paths ────────────────────────────────────────────────

DATA_DIR = Path(
    os.environ.get("MEMORY_DATA_DIR", "/home/kidemon/.hermes/memory-gateway/data")
)
DATA_DIR.mkdir(parents=True, exist_ok=True)

DB_PATH = DATA_DIR / "memory.db"
KEY_FILE = DATA_DIR / ".api_key"
STATIC_DIR = Path(__file__).parent.parent / "static"

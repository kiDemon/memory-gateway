"""Cryptographic and similarity utilities — checksum, simhash, near-duplicate detection."""

import hashlib
import logging
import re
import sqlite3
from typing import Optional

log = logging.getLogger("memory-server")


def compute_checksum(content: str) -> str:
    return hashlib.sha256(content.strip().encode()).hexdigest()[:16]


def compute_simhash(content: str, hashbits: int = 64) -> str:
    """Compute SimHash fingerprint for fuzzy dedup.

    SimHash produces similar hashes for similar content.
    Hamming distance < 10 means ~80%+ similarity.
    """
    tokens = re.findall(r'[\w\u4e00-\u9fff]+', content.lower())
    if not tokens:
        return "0" * (hashbits // 4)
    # Use shingle of 3 tokens
    v = [0] * hashbits
    for i in range(len(tokens) - 2):
        shingle = tokens[i] + tokens[i+1] + tokens[i+2]
        h = int(hashlib.md5(shingle.encode()).hexdigest(), 16)
        for bit in range(hashbits):
            if h & (1 << bit):
                v[bit] += 1
            else:
                v[bit] -= 1
    fingerprint = 0
    for bit in range(hashbits):
        if v[bit] > 0:
            fingerprint |= (1 << bit)
    return format(fingerprint, f'0{hashbits // 4}x')


def hamming_distance(hash1: str, hash2: str) -> int:
    """Compute Hamming distance between two hex hash strings."""
    if not hash1 or not hash2:
        return 64
    try:
        x = int(hash1, 16) ^ int(hash2, 16)
        return bin(x).count('1')
    except (ValueError, TypeError):
        return 64


def _find_near_duplicate(db: sqlite3.Connection, simhash: str, threshold: int = 10) -> Optional[dict]:
    """Check if a simhash has a near-duplicate in the memories table.

    Returns dict with 'id', 'content', 'simhash', 'distance', 'similarity' if found, None otherwise.
    DRY helper used by mem_save, mem_batch_save, and batch_check endpoints.
    """
    similar = db.execute(
        "SELECT id, content, simhash FROM memories WHERE archived=0 AND simhash != '' LIMIT 1000"
    ).fetchall()
    for r in similar:
        if r["simhash"] and hamming_distance(simhash, r["simhash"]) < threshold:
            return {
                "id": r["id"],
                "content": r.get("content", ""),
                "simhash": r["simhash"],
                "distance": hamming_distance(simhash, r["simhash"]),
                "similarity": round(1.0 - hamming_distance(simhash, r["simhash"]) / 64, 3),
            }
    return None

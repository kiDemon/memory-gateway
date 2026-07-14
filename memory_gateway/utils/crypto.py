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

    Short texts (<3 tokens) fall back to unigram/bigram features so the
    fingerprint is never the all-zero trap that caused false near-dups.
    """
    tokens = re.findall(r'[\w\u4e00-\u9fff]+', content.lower())
    if not tokens:
        # Empty after tokenize: hash raw stripped content to avoid all-zero collision
        raw = (content or "").strip().lower() or "empty"
        h = int(hashlib.md5(raw.encode()).hexdigest(), 16)
        return format(h & ((1 << hashbits) - 1), f'0{hashbits // 4}x')

    # Feature selection by length:
    # - 1 token: unigram only
    # - 2 tokens: unigrams + bigram
    # - 3+ tokens: 3-token shingles (classic SimHash)
    features: list[str] = []
    if len(tokens) == 1:
        features = [tokens[0]]
    elif len(tokens) == 2:
        features = [tokens[0], tokens[1], tokens[0] + tokens[1]]
    else:
        for i in range(len(tokens) - 2):
            features.append(tokens[i] + tokens[i + 1] + tokens[i + 2])

    v = [0] * hashbits
    for feat in features:
        h = int(hashlib.md5(feat.encode()).hexdigest(), 16)
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

    Skips the all-zero fingerprint (legacy short-text trap) so old bad
    hashes do not false-positive against each other.
    """
    if not simhash or simhash == ("0" * len(simhash)):
        return None

    # Prefer recent rows; raise scan cap so large libraries still dedup.
    similar = db.execute(
        "SELECT id, content, simhash FROM memories "
        "WHERE archived=0 AND simhash != '' AND simhash != ? "
        "ORDER BY created_at DESC LIMIT 5000",
        ("0" * len(simhash),),
    ).fetchall()
    for r in similar:
        other = r["simhash"]
        if not other or other == ("0" * len(other)):
            continue
        dist = hamming_distance(simhash, other)
        if dist < threshold:
            return {
                "id": r["id"],
                "content": r["content"] if "content" in r.keys() else "",
                "simhash": other,
                "distance": dist,
                "similarity": round(1.0 - dist / 64, 3),
            }
    return None

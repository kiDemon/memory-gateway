"""Utility modules extracted from server.py."""
from datetime import datetime, timezone

from memory_gateway.utils.crypto import compute_checksum, compute_simhash, hamming_distance, _find_near_duplicate
from memory_gateway.utils.helpers import _build_timeline, _generate_api_key
from memory_gateway.utils.privacy import PRIVACY_PATTERNS, _filter_sensitive
from memory_gateway.utils.embedding import (
    _embed_model,
    EMBEDDING_DIM,
    _get_embed_model,
    _blob_to_vector,
    _vector_to_blob,
    _cosine_similarity,
    _compute_embedding,
)


def now_iso() -> str:
    """Return current UTC time as ISO 8601 string."""
    return datetime.now(timezone.utc).isoformat()


__all__ = [
    "now_iso",
    "_build_timeline",
    "_generate_api_key",
]

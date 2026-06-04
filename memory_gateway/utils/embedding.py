"""Embedding utilities — lazy-loading model, vector encode/decode, similarity."""

import logging
import math
import os
import struct

log = logging.getLogger("memory-server")

_embed_model = None
EMBEDDING_DIM = int(os.environ.get("MEMORY_EMBEDDING_DIM", "384"))


def _get_embed_model():
    """Lazy-load the embedding model (all-MiniLM-L6-v2, 384-dim, ~80MB)."""
    global _embed_model
    if _embed_model is not None:
        return _embed_model
    try:
        from sentence_transformers import SentenceTransformer
        model_name = os.environ.get("MEMORY_EMBEDDING_MODEL", "all-MiniLM-L6-v2")
        _embed_model = SentenceTransformer(model_name)
        log.info(f"Embedding model loaded: {model_name} (dim={_embed_model.get_sentence_embedding_dimension()})")
        return _embed_model
    except ImportError:
        log.warning("sentence-transformers not installed — embedding search disabled", exc_info=True)
        return None
    except Exception as e:
        log.warning(f"Failed to load embedding model: {e}", exc_info=True)
        return None


def _blob_to_vector(blob: bytes) -> list[float] | None:
    """Decode BLOB to float list."""
    if not blob:
        return None
    try:
        n = len(blob) // 4
        return list(struct.unpack(f'{n}f', blob))
    except struct.error:
        return None


def _vector_to_blob(vec: list[float]) -> bytes:
    """Encode float list to BLOB."""
    return struct.pack(f'{len(vec)}f', *vec)


def _cosine_similarity(a: list[float], b: list[float]) -> float:
    """Compute cosine similarity between two vectors."""
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    norm_a = math.sqrt(sum(x * x for x in a))
    norm_b = math.sqrt(sum(x * x for x in b))
    if norm_a == 0 or norm_b == 0:
        return 0.0
    return dot / (norm_a * norm_b)


def _compute_embedding(content: str) -> bytes | None:
    """Compute embedding for content. Returns BLOB or None if model unavailable."""
    model = _get_embed_model()
    if model is None:
        return None
    try:
        vec = model.encode(content, normalize_embeddings=True).tolist()
    except Exception as e:
        log.warning(f"Embedding computation failed: {e}", exc_info=True)
        return None
    return _vector_to_blob(vec)

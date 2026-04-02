from __future__ import annotations

import structlog

logger = structlog.get_logger()

# Lazy singleton — model loads on first call.
# NEVER import this module from the API process. Only from worker tasks.
_model = None


def _get_model():  # type: ignore[no-untyped-def]
    global _model
    if _model is None:
        logger.info("loading_embedding_model", model="BAAI/bge-m3")
        from sentence_transformers import SentenceTransformer
        _model = SentenceTransformer("BAAI/bge-m3")
        logger.info("embedding_model_loaded")
    return _model


def get_embeddings(texts: list[str]) -> list[list[float]]:
    """Encode a batch of texts into 1024-dim vectors using BGE-M3.

    Args:
        texts: List of strings to encode.

    Returns:
        List of 1024-dimensional float vectors.
    """
    if not texts:
        return []
    model = _get_model()  # type: ignore[no-untyped-call]
    embeddings = model.encode(texts, normalize_embeddings=True)
    return [vec.tolist() for vec in embeddings]

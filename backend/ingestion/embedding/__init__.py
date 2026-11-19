"""Embedding subsystem contracts."""

from backend.ingestion.embedding.base import (
    EmbeddingProvider,
    EmbeddingResult,
)
from backend.ingestion.embedding.bge_m3 import (
    BgeM3EmbeddingService,
    BgeM3ModelManager,
    get_embedding_service,
    resolve_device,
)
from backend.ingestion.embedding.models import (
    BgeM3Config,
    EmbeddingBatchStats,
)

__all__ = [
    "EmbeddingProvider",
    "EmbeddingResult",
    "BgeM3EmbeddingService",
    "BgeM3ModelManager",
    "BgeM3Config",
    "EmbeddingBatchStats",
    "get_embedding_service",
    "resolve_device",
]


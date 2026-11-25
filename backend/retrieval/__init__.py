"""Hybrid retrieval and cross-encoder reranking subsystem for the Enterprise Knowledge Agent."""

from backend.retrieval.config import RetrievalSettings, get_retrieval_settings
from backend.retrieval.exceptions import (
    DenseRetrievalError,
    EmptyRetrievalResultWarning,
    RerankerInferenceError,
    RerankerTimeoutError,
    RetrievalError,
    RetrievalSecurityViolationError,
    SparseRetrievalError,
)
from backend.retrieval.types import (
    ChunkLocatorCoordinates,
    RetrievalContext,
    RetrievedChunk,
    RetrievedChunkSource,
)

__all__ = [
    "ChunkLocatorCoordinates",
    "DenseRetrievalError",
    "EmptyRetrievalResultWarning",
    "RerankerInferenceError",
    "RerankerTimeoutError",
    "RetrievalContext",
    "RetrievalError",
    "RetrievalSecurityViolationError",
    "RetrievalSettings",
    "RetrievedChunk",
    "RetrievedChunkSource",
    "SparseRetrievalError",
    "get_retrieval_settings",
]

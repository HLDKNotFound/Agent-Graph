"""Hybrid retrieval and cross-encoder reranking subsystem for the Enterprise Knowledge Agent."""

from backend.retrieval.config import RetrievalSettings, get_retrieval_settings
from backend.retrieval.dense.qdrant_search import QdrantDenseRetriever
from backend.retrieval.dense.query_embedder import QueryEmbedder
from backend.retrieval.exceptions import (
    DenseRetrievalError,
    EmptyRetrievalResultWarning,
    RerankerInferenceError,
    RerankerTimeoutError,
    RetrievalError,
    RetrievalSecurityViolationError,
    SparseRetrievalError,
)
from backend.retrieval.fusion.rrf import ReciprocalRankFusionEngine
from backend.retrieval.hybrid_retriever import (
    HybridRetrievalResult,
    HybridRetriever,
    RetrievalTelemetry,
)
from backend.retrieval.reranking.cross_encoder import (
    CrossEncoderModelManager,
    CrossEncoderReranker,
    sigmoid,
)
from backend.retrieval.sparse.pg_bm25_search import PostgresBM25Retriever
from backend.retrieval.types import (
    ChunkLocatorCoordinates,
    RetrievalContext,
    RetrievedChunk,
    RetrievedChunkSource,
)

__all__ = [
    "ChunkLocatorCoordinates",
    "CrossEncoderModelManager",
    "CrossEncoderReranker",
    "DenseRetrievalError",
    "EmptyRetrievalResultWarning",
    "HybridRetrievalResult",
    "HybridRetriever",
    "PostgresBM25Retriever",
    "QdrantDenseRetriever",
    "QueryEmbedder",
    "ReciprocalRankFusionEngine",
    "RerankerInferenceError",
    "RerankerTimeoutError",
    "RetrievalContext",
    "RetrievalError",
    "RetrievalSecurityViolationError",
    "RetrievalSettings",
    "RetrievalTelemetry",
    "RetrievedChunk",
    "RetrievedChunkSource",
    "SparseRetrievalError",
    "get_retrieval_settings",
    "sigmoid",
]

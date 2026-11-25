"""Dynamic environment settings for the hybrid retrieval and reranking subsystem.

Adheres strictly to the Zero Hardcoding Principle via Pydantic Settings.
"""

from __future__ import annotations

import os
from functools import lru_cache
from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class RetrievalSettings(BaseSettings):
    """Dynamic configuration governing dense vector search, sparse BM25, RRF fusion, and cross-encoder reranking."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # Relational Database Persistence (PostgreSQL 16+)
    database_url: str = Field(
        default="postgresql://postgres:postgres@localhost:5432/enterprise_rag?schema=public",
        validation_alias="DATABASE_URL",
        description="PostgreSQL connection string for relational data and GIN full-text search",
    )

    # Vector Database (Qdrant v1.8+)
    qdrant_host: str = Field(
        default="localhost",
        validation_alias="QDRANT_HOST",
        description="Qdrant cluster hostname or IP",
    )
    qdrant_port: int = Field(
        default=6333,
        validation_alias="QDRANT_PORT",
        ge=1,
        le=65535,
        description="Qdrant HTTP port",
    )
    qdrant_grpc_port: int = Field(
        default=6334,
        validation_alias="QDRANT_GRPC_PORT",
        ge=1,
        le=65535,
        description="Qdrant gRPC port",
    )
    qdrant_api_key: str | None = Field(
        default=None,
        validation_alias="QDRANT_API_KEY",
        description="Qdrant security API key",
    )
    qdrant_url: str | None = Field(
        default=None,
        validation_alias="QDRANT_URL",
        description="Full Qdrant URL or ':memory:' for isolated testing",
    )
    qdrant_collection: str = Field(
        default="enterprise_knowledge_chunks",
        validation_alias="QDRANT_COLLECTION",
        description="Target Qdrant collection name",
    )
    qdrant_hnsw_ef: int = Field(
        default=64,
        validation_alias="QDRANT_HNSW_EF",
        ge=16,
        le=512,
        description="Qdrant runtime HNSW query exploration factor ef (spec: 64)",
    )

    # Dense Vector Embedding (BGE-M3)
    embedding_model_name: str = Field(
        default="BAAI/bge-m3",
        validation_alias="EMBEDDING_MODEL_NAME",
        description="HuggingFace model identifier for 1024-dim dense embeddings",
    )
    embedding_device: str = Field(
        default="auto",
        validation_alias="EMBEDDING_DEVICE",
        description="Inference compute device for embedding ('auto', 'cuda', 'cpu')",
    )

    # Cross-Encoder Reranker (BGE-Reranker-v2-m3)
    reranker_model_name: str = Field(
        default="BAAI/bge-reranker-v2-m3",
        validation_alias="RERANKER_MODEL_NAME",
        description="Cross-encoder model identifier for deep semantic relevance scoring",
    )
    reranker_device: str = Field(
        default="auto",
        validation_alias="RERANKER_DEVICE",
        description="Inference compute device for reranking ('auto', 'cuda', 'cpu')",
    )
    reranker_batch_size: int = Field(
        default=16,
        validation_alias="RERANKER_BATCH_SIZE",
        ge=1,
        le=128,
        description="Batch size for cross-encoder inference",
    )
    reranker_timeout_seconds: float = Field(
        default=1.5,
        validation_alias="RERANKER_TIMEOUT_SECONDS",
        ge=0.1,
        le=10.0,
        description="Maximum execution timeout for reranker before graceful fallback to RRF",
    )
    reranker_min_score_threshold: float = Field(
        default=0.35,
        validation_alias="RERANKER_MIN_SCORE_THRESHOLD",
        ge=0.0,
        le=1.0,
        description="Minimum normalized relevance score [0.0, 1.0] to qualify passage for context",
    )

    # Candidate Retrieval Limits (Stage 1 & Stage 2)
    dense_top_k: int = Field(
        default=40,
        validation_alias="DENSE_TOP_K",
        ge=1,
        le=200,
        description="Candidate limit K1 for Stage 1 dense vector search (spec: 40)",
    )
    sparse_top_k: int = Field(
        default=40,
        validation_alias="SPARSE_TOP_K",
        ge=1,
        le=200,
        description="Candidate limit K1 for Stage 1 sparse lexical search (spec: 40)",
    )
    fused_top_k: int = Field(
        default=40,
        validation_alias="FUSED_TOP_K",
        ge=1,
        le=200,
        description="Candidate limit for Stage 2 Reciprocal Rank Fusion",
    )
    final_top_k: int = Field(
        default=8,
        validation_alias="FINAL_TOP_K",
        ge=1,
        le=30,
        description="Final selected passages K2 for LLM context generation (spec: 5 to 8)",
    )

    # Reciprocal Rank Fusion (RRF) Parameters
    rrf_k: int = Field(
        default=60,
        validation_alias="RRF_K",
        ge=1,
        le=200,
        description="RRF smoothing constant k (spec: 60)",
    )
    rrf_dense_weight: float = Field(
        default=0.7,
        validation_alias="RRF_DENSE_WEIGHT",
        ge=0.0,
        le=1.0,
        description="Weight factor for dense vector search (spec: 0.7)",
    )
    rrf_sparse_weight: float = Field(
        default=0.3,
        validation_alias="RRF_SPARSE_WEIGHT",
        ge=0.0,
        le=1.0,
        description="Weight factor for sparse lexical search (spec: 0.3)",
    )


@lru_cache(maxsize=1)
def get_retrieval_settings() -> RetrievalSettings:
    """Return cached singleton instance of RetrievalSettings."""
    return RetrievalSettings()

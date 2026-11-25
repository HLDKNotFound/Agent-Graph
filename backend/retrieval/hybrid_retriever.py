"""Hybrid Retrieval Orchestrator unifying dense semantic, sparse BM25, RRF, and cross-encoder reranking."""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from typing import Any, Sequence

from pydantic import BaseModel, ConfigDict, Field

from backend.retrieval.config import RetrievalSettings, get_retrieval_settings
from backend.retrieval.dense.qdrant_search import QdrantDenseRetriever
from backend.retrieval.dense.query_embedder import QueryEmbedder
from backend.retrieval.exceptions import (
    RetrievalError,
    RetrievalSecurityViolationError,
)
from backend.retrieval.fusion.rrf import ReciprocalRankFusionEngine
from backend.retrieval.reranking.cross_encoder import CrossEncoderReranker
from backend.retrieval.sparse.pg_bm25_search import PostgresBM25Retriever
from backend.retrieval.types.chunk import RetrievedChunk
from backend.retrieval.types.context import RetrievalContext

logger = logging.getLogger(__name__)


class RetrievalTelemetry(BaseModel):
    """Detailed latency and count breakdown for hybrid retrieval execution."""

    model_config = ConfigDict(frozen=True)

    embedding_ms: float = 0.0
    parallel_search_ms: float = 0.0
    dense_search_ms: float = 0.0
    sparse_search_ms: float = 0.0
    fusion_ms: float = 0.0
    rerank_ms: float = 0.0
    total_latency_ms: float = 0.0
    dense_candidates_count: int = 0
    sparse_candidates_count: int = 0
    fused_candidates_count: int = 0
    final_passages_count: int = 0


class HybridRetrievalResult(BaseModel):
    """Unified result container returned by HybridRetriever.retrieve()."""

    model_config = ConfigDict(frozen=True)

    query: str
    passages: list[RetrievedChunk]
    telemetry: RetrievalTelemetry
    context: RetrievalContext


class HybridRetriever:
    """Enterprise Hybrid Retrieval Orchestrator.

    Executes the 4-stage retrieval lifecycle:
    1. Online Dense Query Embedding (BGE-M3 Singleton)
    2. Concurrent Dense Qdrant + Sparse PostgreSQL GIN Search (`asyncio.gather`)
    3. Reciprocal Rank Fusion (RRF k=60, w_Dense=0.7, w_Sparse=0.3)
    4. Cross-Encoder Semantic Reranking (BGE-Reranker-v2-m3 with SLA fallback)
    """

    def __init__(
        self,
        settings: RetrievalSettings | None = None,
        query_embedder: QueryEmbedder | None = None,
        dense_retriever: QdrantDenseRetriever | None = None,
        sparse_retriever: PostgresBM25Retriever | None = None,
        fusion_engine: ReciprocalRankFusionEngine | None = None,
        reranker: CrossEncoderReranker | None = None,
    ) -> None:
        self.settings = settings or get_retrieval_settings()
        self.query_embedder = query_embedder or QueryEmbedder(settings=self.settings)
        self.dense_retriever = dense_retriever or QdrantDenseRetriever(settings=self.settings)
        self.sparse_retriever = sparse_retriever or PostgresBM25Retriever(settings=self.settings)
        self.fusion_engine = fusion_engine or ReciprocalRankFusionEngine(settings=self.settings)
        self.reranker = reranker or CrossEncoderReranker(settings=self.settings)

    async def retrieve(
        self,
        query: str,
        context: RetrievalContext,
        top_k: int | None = None,
    ) -> HybridRetrievalResult:
        """Execute end-to-end hybrid retrieval with strict zero-trust multi-tenancy.

        Args:
            query: User search query or rewritten query.
            context: Authenticated zero-trust RetrievalContext.
            top_k: Final passage limit K2 (defaults to settings.final_top_k = 8).

        Returns:
            HybridRetrievalResult with ranked passages and telemetry breakdown.
        """
        clean_query = query.strip()
        if not clean_query:
            return HybridRetrievalResult(
                query=query,
                passages=[],
                telemetry=RetrievalTelemetry(),
                context=context,
            )

        if not context.tenant_id:
            raise RetrievalSecurityViolationError("Tenant ID is required for hybrid retrieval")

        t_start = time.perf_counter()

        # Step 1: Dense Query Embedding
        t_embed_start = time.perf_counter()
        query_vector = await self.query_embedder.embed_query(clean_query)
        embedding_ms = (time.perf_counter() - t_embed_start) * 1000.0

        # Step 2: Concurrent Dense + Sparse Candidate Retrieval
        t_search_start = time.perf_counter()
        dense_task = asyncio.create_task(
            self.dense_retriever.search(
                query_vector=query_vector,
                context=context,
                top_k=self.settings.dense_top_k,
            )
        )
        sparse_task = asyncio.create_task(
            self.sparse_retriever.search(
                query_text=clean_query,
                context=context,
                top_k=self.settings.sparse_top_k,
            )
        )

        dense_candidates, sparse_candidates = await asyncio.gather(
            dense_task,
            sparse_task,
            return_exceptions=False,
        )
        parallel_search_ms = (time.perf_counter() - t_search_start) * 1000.0

        # Step 3: Reciprocal Rank Fusion (RRF)
        t_fusion_start = time.perf_counter()
        fused_candidates = self.fusion_engine.fuse(
            dense_candidates=dense_candidates,
            sparse_candidates=sparse_candidates,
            top_k=self.settings.fused_top_k,
        )
        fusion_ms = (time.perf_counter() - t_fusion_start) * 1000.0

        # Step 4: Cross-Encoder Semantic Reranking with SLA Fallback
        t_rerank_start = time.perf_counter()
        final_top_k = top_k or self.settings.final_top_k
        final_passages = await self.reranker.rerank(
            query=clean_query,
            candidates=fused_candidates,
            top_k=final_top_k,
            min_threshold=self.settings.reranker_min_score_threshold,
        )
        rerank_ms = (time.perf_counter() - t_rerank_start) * 1000.0

        total_latency_ms = (time.perf_counter() - t_start) * 1000.0

        telemetry = RetrievalTelemetry(
            embedding_ms=round(embedding_ms, 2),
            parallel_search_ms=round(parallel_search_ms, 2),
            dense_search_ms=round(parallel_search_ms, 2),
            sparse_search_ms=round(parallel_search_ms, 2),
            fusion_ms=round(fusion_ms, 2),
            rerank_ms=round(rerank_ms, 2),
            total_latency_ms=round(total_latency_ms, 2),
            dense_candidates_count=len(dense_candidates),
            sparse_candidates_count=len(sparse_candidates),
            fused_candidates_count=len(fused_candidates),
            final_passages_count=len(final_passages),
        )

        logger.info(
            "Hybrid retrieval completed in %.1fms (Dense=%d, Sparse=%d, Fused=%d, Final=%d)",
            total_latency_ms,
            len(dense_candidates),
            len(sparse_candidates),
            len(fused_candidates),
            len(final_passages),
        )

        return HybridRetrievalResult(
            query=clean_query,
            passages=final_passages,
            telemetry=telemetry,
            context=context,
        )

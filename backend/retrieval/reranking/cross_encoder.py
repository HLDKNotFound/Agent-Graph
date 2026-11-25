"""Cross-Encoder reranker service with resilient SLA fallback."""

from __future__ import annotations

import asyncio
import logging
import math
import threading
from typing import Any, Sequence

import numpy as np
import torch
from sentence_transformers import CrossEncoder

from backend.ingestion.embedding.bge_m3 import resolve_device
from backend.retrieval.config import RetrievalSettings, get_retrieval_settings
from backend.retrieval.exceptions import (
    RerankerInferenceError,
    RerankerTimeoutError,
)
from backend.retrieval.types.chunk import RetrievedChunk

logger = logging.getLogger(__name__)


def sigmoid(z: float) -> float:
    """Map raw real logit into normalized probability score [0.0, 1.0]."""
    if z < -40.0:
        return 0.0
    if z > 40.0:
        return 1.0
    return 1.0 / (1.0 + math.exp(-z))


class CrossEncoderModelManager:
    """Thread-safe lifecycle manager and registry for CrossEncoder models."""

    _instance: CrossEncoderModelManager | None = None
    _lock = threading.Lock()

    def __init__(self) -> None:
        self._models: dict[tuple[str, str], Any] = {}
        self._registry_lock = threading.Lock()

    @classmethod
    def get_instance(cls) -> CrossEncoderModelManager:
        """Provide or create shared singleton CrossEncoderModelManager."""
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    cls._instance = cls()
        return cls._instance

    def get_model(
        self,
        model_name: str = "BAAI/bge-reranker-v2-m3",
        device: str = "auto",
    ) -> Any:
        """Retrieve cached CrossEncoder or load weights onto device.

        Args:
            model_name: HuggingFace model identifier.
            device: Compute device string ('auto', 'cuda', 'cpu').

        Returns:
            Instantiated CrossEncoder model.
        """
        canonical_device = resolve_device(device)
        cache_key = (model_name, canonical_device)

        if cache_key in self._models:
            return self._models[cache_key]

        with self._registry_lock:
            if cache_key in self._models:
                return self._models[cache_key]

            logger.info("Loading CrossEncoder '%s' onto device '%s'...", model_name, canonical_device)
            try:
                model = CrossEncoder(
                    model_name,
                    max_length=512,
                    device=canonical_device,
                )
                self._models[cache_key] = model
                logger.info("CrossEncoder '%s' successfully loaded onto '%s'.", model_name, canonical_device)
                return model
            except Exception as exc:
                logger.error("Failed to load CrossEncoder '%s': %s", model_name, exc, exc_info=True)
                raise RerankerInferenceError(
                    f"Failed to load cross-encoder '{model_name}': {exc}",
                    details={"model_name": model_name, "device": canonical_device, "error": str(exc)},
                ) from exc


class CrossEncoderReranker:
    """Stage-3 Cross-Encoder Reranker using BAAI/bge-reranker-v2-m3 with SLA fallback.

    Capabilities:
    1. Deep Semantic Cross-Attention: Jointly attends over (query, document) pairs.
    2. Sigmoid Score Normalization: Maps unbounded cross-encoder logits into normalized [0.0, 1.0].
    3. Relevance Threshold Filtering: Prunes irrelevant noise below threshold (default >= 0.35).
    4. Resilient Fallback Mechanism: If reranking encounters timeout (> 1.5s) or compute/OOM errors,
       smoothly falls back to top K2 candidates ordered by Reciprocal Rank Fusion (RRF).
    """

    def __init__(
        self,
        settings: RetrievalSettings | None = None,
        model_manager: CrossEncoderModelManager | None = None,
        model: Any | None = None,
    ) -> None:
        self.settings = settings or get_retrieval_settings()
        self.model_manager = model_manager or CrossEncoderModelManager.get_instance()
        self._model = model  # Injected model for unit tests
        self.device = resolve_device(self.settings.reranker_device)

    def _get_model(self) -> Any:
        if self._model is not None:
            return self._model
        return self.model_manager.get_model(
            model_name=self.settings.reranker_model_name,
            device=self.device,
        )

    def rerank_sync(
        self,
        query: str,
        candidates: Sequence[RetrievedChunk],
        top_k: int | None = None,
        min_threshold: float | None = None,
    ) -> list[RetrievedChunk]:
        """Synchronously score candidates using cross-encoder and filter by threshold.

        Args:
            query: User query text.
            candidates: Candidate passages from RRF fusion.
            top_k: Final passage limit (defaults to settings.final_top_k = 8).
            min_threshold: Minimum score threshold (defaults to settings.reranker_min_score_threshold = 0.35).

        Returns:
            Ranked and filtered list of RetrievedChunk instances.
        """
        if not candidates or not query.strip():
            return []

        limit = top_k or self.settings.final_top_k
        threshold = min_threshold if min_threshold is not None else self.settings.reranker_min_score_threshold

        model = self._get_model()

        # Build sentence pairs
        sentence_pairs = [(query.strip(), chunk.content) for chunk in candidates]

        with torch.inference_mode():
            raw_scores = model.predict(
                sentence_pairs,
                batch_size=self.settings.reranker_batch_size,
                show_progress_bar=False,
            )

        # Normalize raw scores to [0.0, 1.0] using sigmoid
        scored_candidates: list[RetrievedChunk] = []
        for chunk, raw_score in zip(candidates, raw_scores):
            score_val = float(raw_score)
            normalized_score = sigmoid(score_val)

            # Reconstruct chunk with rerank_score
            scored_chunk = RetrievedChunk(
                chunk_id=chunk.chunk_id,
                document_id=chunk.document_id,
                document_version_id=chunk.document_version_id,
                content=chunk.content,
                chunk_index=chunk.chunk_index,
                token_count=chunk.token_count,
                locator=chunk.locator,
                dense_score=chunk.dense_score,
                sparse_score=chunk.sparse_score,
                rrf_score=chunk.rrf_score,
                rerank_score=normalized_score,
                dense_rank=chunk.dense_rank,
                sparse_rank=chunk.sparse_rank,
                source=chunk.source,
                classification=chunk.classification,
                department_id=chunk.department_id,
                document_title=chunk.document_title,
                metadata=chunk.metadata,
            )
            scored_candidates.append(scored_chunk)

        # Sort descending by rerank_score
        scored_candidates.sort(
            key=lambda c: (c.rerank_score if c.rerank_score is not None else 0.0),
            reverse=True,
        )

        # Filter by minimum relevance threshold
        filtered = [c for c in scored_candidates if (c.rerank_score or 0.0) >= threshold]

        # Guard: if nothing meets threshold, keep top candidate if relevance > 0.1
        if not filtered and scored_candidates:
            if (scored_candidates[0].rerank_score or 0.0) >= 0.10:
                filtered = [scored_candidates[0]]

        return filtered[:limit]

    async def rerank(
        self,
        query: str,
        candidates: Sequence[RetrievedChunk],
        top_k: int | None = None,
        min_threshold: float | None = None,
    ) -> list[RetrievedChunk]:
        """Asynchronously rerank candidates with SLA timeout and graceful fallback to RRF order.

        Args:
            query: User query text.
            candidates: Candidate passages from RRF fusion.
            top_k: Final passage limit.
            min_threshold: Minimum score threshold.

        Returns:
            Ranked list of passages from reranker, or RRF fallback on timeout/error.
        """
        if not candidates:
            return []

        limit = top_k or self.settings.final_top_k
        timeout = self.settings.reranker_timeout_seconds

        try:
            # Wrap execution with strict SLA timeout
            return await asyncio.wait_for(
                asyncio.to_thread(self.rerank_sync, query, candidates, limit, min_threshold),
                timeout=timeout,
            )
        except asyncio.TimeoutError:
            logger.warning(
                "Cross-encoder reranking timed out (> %.2fs). Gracefully falling back to RRF rank order.",
                timeout,
            )
            return self._rrf_fallback(candidates, limit)
        except Exception as exc:
            logger.warning(
                "Cross-encoder reranking failed (%s). Gracefully falling back to RRF rank order.",
                exc,
            )
            return self._rrf_fallback(candidates, limit)

    @staticmethod
    def _rrf_fallback(candidates: Sequence[RetrievedChunk], limit: int) -> list[RetrievedChunk]:
        """Fall back to top candidates ordered by RRF score."""
        sorted_candidates = sorted(
            candidates,
            key=lambda c: (
                c.rrf_score if c.rrf_score is not None else 0.0,
                c.dense_score if c.dense_score is not None else 0.0,
            ),
            reverse=True,
        )
        return list(sorted_candidates[:limit])

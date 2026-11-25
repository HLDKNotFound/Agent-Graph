"""Reciprocal Rank Fusion (RRF) ranking engine merging dense and sparse candidates."""

from __future__ import annotations

import logging
import uuid
from typing import Sequence

from backend.retrieval.config import RetrievalSettings, get_retrieval_settings
from backend.retrieval.types.chunk import (
    ChunkLocatorCoordinates,
    RetrievedChunk,
    RetrievedChunkSource,
)

logger = logging.getLogger(__name__)


class ReciprocalRankFusionEngine:
    """Stage-2 Reciprocal Rank Fusion (RRF) ranking engine.

    Merges ranked candidate lists from Stage-1 Dense Semantic Search and Stage-1
    Sparse Lexical BM25 search using the spec-compliant formula:

        RRF_Score(d) = (w_Dense / (k + rank_Dense(d))) + (w_Sparse / (k + rank_Sparse(d)))

    Capabilities:
    1. Spec-Compliant Parameters: k=60, w_Dense=0.7, w_Sparse=0.3.
    2. Deduplication by Chunk ID: Fuses chunks present in both candidate pools into a single
       representation with unified scores and preserved lineage coordinates.
    3. Deterministic Tie-Breaking: Breaks identical RRF scores via dense similarity then chunk UUID.
    4. Configurable Candidate Ceiling: Truncates merged candidate pool to top K1=40 for reranking.
    """

    def __init__(
        self,
        settings: RetrievalSettings | None = None,
        k: int | None = None,
        dense_weight: float | None = None,
        sparse_weight: float | None = None,
    ) -> None:
        self.settings = settings or get_retrieval_settings()
        self.k = k if k is not None else self.settings.rrf_k
        self.dense_weight = dense_weight if dense_weight is not None else self.settings.rrf_dense_weight
        self.sparse_weight = sparse_weight if sparse_weight is not None else self.settings.rrf_sparse_weight

    def fuse(
        self,
        dense_candidates: Sequence[RetrievedChunk],
        sparse_candidates: Sequence[RetrievedChunk],
        top_k: int | None = None,
    ) -> list[RetrievedChunk]:
        """Fuse dense and sparse candidate pools into a unified, deduplicated ranked list.

        Args:
            dense_candidates: List of chunks retrieved via Qdrant dense vector search.
            sparse_candidates: List of chunks retrieved via PostgreSQL GIN lexical search.
            top_k: Candidate ceiling (defaults to settings.fused_top_k = 40).

        Returns:
            Deduplicated, RRF-scored and sorted list of RetrievedChunk instances.
        """
        limit = top_k or self.settings.fused_top_k

        # Map by chunk_id
        chunk_map: dict[uuid.UUID, dict[str, Any]] = {}

        # 1. Process Dense candidates
        for idx, chunk in enumerate(dense_candidates, start=1):
            rank = chunk.dense_rank if chunk.dense_rank is not None else idx
            score_contrib = self.dense_weight / (self.k + rank)

            chunk_map[chunk.chunk_id] = {
                "base_chunk": chunk,
                "dense_chunk": chunk,
                "sparse_chunk": None,
                "dense_rank": rank,
                "sparse_rank": None,
                "dense_score": chunk.dense_score,
                "sparse_score": None,
                "rrf_score": score_contrib,
                "sources": {RetrievedChunkSource.DENSE},
            }

        # 2. Process Sparse candidates
        for idx, chunk in enumerate(sparse_candidates, start=1):
            rank = chunk.sparse_rank if chunk.sparse_rank is not None else idx
            score_contrib = self.sparse_weight / (self.k + rank)

            if chunk.chunk_id in chunk_map:
                # Chunk appears in both pools -> Fused
                entry = chunk_map[chunk.chunk_id]
                entry["sparse_chunk"] = chunk
                entry["sparse_rank"] = rank
                entry["sparse_score"] = chunk.sparse_score
                entry["rrf_score"] += score_contrib
                entry["sources"].add(RetrievedChunkSource.SPARSE)
                # If sparse chunk has richer metadata/locator, combine
                entry["base_chunk"] = self._merge_locators(entry["base_chunk"], chunk)
            else:
                chunk_map[chunk.chunk_id] = {
                    "base_chunk": chunk,
                    "dense_chunk": None,
                    "sparse_chunk": chunk,
                    "dense_rank": None,
                    "sparse_rank": rank,
                    "dense_score": None,
                    "sparse_score": chunk.sparse_score,
                    "rrf_score": score_contrib,
                    "sources": {RetrievedChunkSource.SPARSE},
                }

        # 3. Formulate unified RetrievedChunk list
        fused_list: list[RetrievedChunk] = []
        for chunk_id, entry in chunk_map.items():
            base = entry["base_chunk"]
            sources = entry["sources"]
            if len(sources) > 1:
                source = RetrievedChunkSource.FUSED
            elif RetrievedChunkSource.DENSE in sources:
                source = RetrievedChunkSource.DENSE
            else:
                source = RetrievedChunkSource.SPARSE

            # Formulate updated immutable chunk with rrf_score
            fused_chunk = RetrievedChunk(
                chunk_id=base.chunk_id,
                document_id=base.document_id,
                document_version_id=base.document_version_id,
                content=base.content,
                chunk_index=base.chunk_index,
                token_count=base.token_count,
                locator=base.locator,
                dense_score=entry["dense_score"],
                sparse_score=entry["sparse_score"],
                rrf_score=float(entry["rrf_score"]),
                rerank_score=None,
                dense_rank=entry["dense_rank"],
                sparse_rank=entry["sparse_rank"],
                source=source,
                classification=base.classification,
                department_id=base.department_id,
                document_title=base.document_title,
                metadata=base.metadata,
            )
            fused_list.append(fused_chunk)

        # 4. Sort descending by RRF score, with stable tie-breaking
        fused_list.sort(
            key=lambda c: (
                c.rrf_score if c.rrf_score is not None else 0.0,
                c.dense_score if c.dense_score is not None else 0.0,
                c.sparse_score if c.sparse_score is not None else 0.0,
                str(c.chunk_id),
            ),
            reverse=True,
        )

        return fused_list[:limit]

    @staticmethod
    def _merge_locators(dense_chunk: RetrievedChunk, sparse_chunk: RetrievedChunk) -> RetrievedChunk:
        """Merge locator coordinates to ensure non-empty provenance is preserved."""
        d_loc = dense_chunk.locator
        s_loc = sparse_chunk.locator

        merged_locator = ChunkLocatorCoordinates(
            page_number=d_loc.page_number if d_loc.page_number is not None else s_loc.page_number,
            section_heading=d_loc.section_heading if d_loc.section_heading is not None else s_loc.section_heading,
            sheet_name=d_loc.sheet_name if d_loc.sheet_name is not None else s_loc.sheet_name,
            row_range=d_loc.row_range if d_loc.row_range is not None else s_loc.row_range,
            char_start=d_loc.char_start if d_loc.char_start is not None else s_loc.char_start,
            char_end=d_loc.char_end if d_loc.char_end is not None else s_loc.char_end,
            bounding_box=d_loc.bounding_box or s_loc.bounding_box,
        )

        # Merge metadata dictionaries
        merged_meta = dict(dense_chunk.metadata)
        merged_meta.update(sparse_chunk.metadata)

        return RetrievedChunk(
            chunk_id=dense_chunk.chunk_id,
            document_id=dense_chunk.document_id,
            document_version_id=dense_chunk.document_version_id or sparse_chunk.document_version_id,
            content=dense_chunk.content or sparse_chunk.content,
            chunk_index=dense_chunk.chunk_index,
            token_count=dense_chunk.token_count or sparse_chunk.token_count,
            locator=merged_locator,
            dense_score=dense_chunk.dense_score,
            sparse_score=sparse_chunk.sparse_score,
            rrf_score=None,
            rerank_score=None,
            dense_rank=dense_chunk.dense_rank,
            sparse_rank=sparse_chunk.sparse_rank,
            source=RetrievedChunkSource.FUSED,
            classification=dense_chunk.classification,
            department_id=dense_chunk.department_id or sparse_chunk.department_id,
            document_title=dense_chunk.document_title or sparse_chunk.document_title,
            metadata=merged_meta,
        )

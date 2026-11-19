"""High-level MinHash LSH Chunk Deduplication Service.

Provides end-to-end near-duplicate chunk detection, multi-version comparison,
exact vs. near-duplicate classification, non-destructive annotation, and
deterministic multi-match resolution without coupling to embedding models.
"""

from __future__ import annotations

import logging
from typing import Sequence

from backend.ingestion.config import IngestionSettings, get_ingestion_settings
from backend.ingestion.deduplication.lsh import MinHashLshIndex
from backend.ingestion.deduplication.minhash import MinHashSigner
from backend.ingestion.deduplication.models import (
    ChunkDuplicateMatch,
    ChunkDuplicateReport,
    ComparisonScope,
    DuplicateAction,
    DuplicateType,
    MinHashConfig,
)
from backend.ingestion.types.chunking import DocumentChunkInput
from backend.ingestion.types.context import IngestionContext

logger = logging.getLogger(__name__)


class MinHashChunkDeduplicator:
    """Production-grade near-duplicate chunk detection engine using MinHash and LSH.

    Guarantees:
    1. Zero-Trust Multi-Tenancy: Compares chunks strictly within the tenant boundary.
    2. Version-Aware Tracking: Detects similar chunks across different versions of the same document.
    3. Exact vs. Near-Duplicate Discrimination: Flags exact SHA-256 and high Jaccard matches.
    4. Non-Destructive Invariant: Never deletes or mutates original document chunks.
    5. Decoupled Architecture: Independent of vector embeddings and relational DB engines.
    6. Deterministic Multi-Match Behavior: Resolves candidate collisions with predictable ranking.
    """

    def __init__(
        self,
        config: MinHashConfig | None = None,
        signer: MinHashSigner | None = None,
        index: MinHashLshIndex | None = None,
        settings: IngestionSettings | None = None,
    ) -> None:
        self.settings = settings or get_ingestion_settings()
        self.config = config or MinHashConfig(
            num_permutations=self.settings.minhash_num_permutations,
            shingle_size=self.settings.minhash_shingle_size,
            ngram_type=self.settings.minhash_ngram_type,
            similarity_threshold=self.settings.minhash_similarity_threshold,
            exact_threshold=self.settings.minhash_exact_threshold,
            lsh_bands=self.settings.minhash_lsh_bands,
            lsh_rows=self.settings.minhash_lsh_rows,
            seed=self.settings.minhash_seed,
        )

        self.signer = signer or MinHashSigner(config=self.config)
        self.index = index or MinHashLshIndex(config=self.config, signer=self.signer)

    def detect_duplicates(
        self,
        chunk: DocumentChunkInput,
        scope: ComparisonScope = ComparisonScope.ALL,
        threshold: float | None = None,
        exact_threshold: float | None = None,
        auto_index: bool = False,
        version_number: int | None = None,
    ) -> ChunkDuplicateReport:
        """Analyze a single DocumentChunkInput against the LSH index for duplicates.

        Args:
            chunk: Input document chunk contract.
            scope: ComparisonScope (CROSS_VERSION, CROSS_DOCUMENT, SAME_DOCUMENT, ALL).
            threshold: Jaccard similarity threshold override.
            exact_threshold: Exact duplicate threshold override.
            auto_index: Whether to add this chunk to the index after querying.
            version_number: Optional document version sequence number.

        Returns:
            ChunkDuplicateReport detailing matches, similarity, and recommended action.
        """
        # 1. Compute MinHash signature
        sig = self.signer.compute_signature(chunk.content)

        effective_version = version_number
        if effective_version is None and chunk.metadata:
            effective_version = chunk.metadata.get("version_number")

        # 2. Query LSH index
        matches = self.index.query(
            signature=sig,
            tenant_id=chunk.tenant_id,
            query_chunk_id=chunk.chunk_id,
            document_id=chunk.document_id,
            document_version_id=chunk.document_version_id,
            document_version_number=effective_version,
            content_hash=chunk.content_hash,
            raw_text=chunk.content,
            scope=scope,
            threshold=threshold,
            exact_threshold=exact_threshold,
        )

        # 3. Classify report status
        has_duplicates = len(matches) > 0
        primary_match = matches[0] if has_duplicates else None
        top_duplicate_type = primary_match.duplicate_type if primary_match else DuplicateType.UNIQUE

        # Formulate action
        if top_duplicate_type == DuplicateType.EXACT:
            action = DuplicateAction.RETURN_EXISTING
            msg = (
                f"Exact duplicate detected (matched chunk {primary_match.chunk_id} in "
                f"document {primary_match.document_id}, similarity {primary_match.similarity_score:.4f})"
            )
        elif top_duplicate_type == DuplicateType.NEAR_DUPLICATE:
            action = DuplicateAction.PROCEED
            msg = (
                f"Near-duplicate detected (matched chunk {primary_match.chunk_id} in "
                f"document {primary_match.document_id}, similarity {primary_match.similarity_score:.4f})"
            )
        else:
            action = DuplicateAction.PROCEED
            msg = "Unique chunk; no duplicates detected"

        report = ChunkDuplicateReport(
            chunk_id=chunk.chunk_id,
            document_id=chunk.document_id,
            tenant_id=chunk.tenant_id,
            document_version_id=chunk.document_version_id,
            has_duplicates=has_duplicates,
            duplicate_type=top_duplicate_type,
            primary_match=primary_match,
            all_matches=matches,
            total_matches_count=len(matches),
            action=action,
            minhash_signature=sig,
            message=msg,
        )

        # 4. Auto-index if requested
        if auto_index:
            self.index.index_chunk(chunk, signature=sig, version_number=effective_version)

        return report

    def detect_duplicates_batch(
        self,
        chunks: Sequence[DocumentChunkInput],
        scope: ComparisonScope = ComparisonScope.ALL,
        threshold: float | None = None,
        exact_threshold: float | None = None,
        auto_index: bool = True,
        version_number: int | None = None,
    ) -> list[ChunkDuplicateReport]:
        """Analyze a sequence of chunks against the LSH index in deterministic sequence.

        Args:
            chunks: Sequence of DocumentChunkInput instances.
            scope: ComparisonScope boundary policy.
            threshold: Jaccard similarity threshold override.
            exact_threshold: Exact threshold override.
            auto_index: Whether each chunk is indexed after its query completes.
            version_number: Optional document version sequence number.

        Returns:
            List of ChunkDuplicateReport instances corresponding to input chunks.
        """
        reports: list[ChunkDuplicateReport] = []
        for chunk in chunks:
            report = self.detect_duplicates(
                chunk=chunk,
                scope=scope,
                threshold=threshold,
                exact_threshold=exact_threshold,
                auto_index=auto_index,
                version_number=version_number,
            )
            reports.append(report)
        return reports

    def compare_chunks(
        self,
        chunk1: DocumentChunkInput,
        chunk2: DocumentChunkInput,
        threshold: float | None = None,
        exact_threshold: float | None = None,
    ) -> ChunkDuplicateMatch | None:
        """Pairwise compare two DocumentChunkInput instances without modifying the LSH index.

        Args:
            chunk1: First document chunk.
            chunk2: Second document chunk.
            threshold: Jaccard threshold override.
            exact_threshold: Exact threshold override.

        Returns:
            ChunkDuplicateMatch if similarity >= threshold, or None if unique.
        """
        effective_threshold = (
            threshold if threshold is not None else self.config.similarity_threshold
        )
        effective_exact_threshold = (
            exact_threshold if exact_threshold is not None else self.config.exact_threshold
        )

        sig1 = self.signer.compute_signature(chunk1.content)
        sig2 = self.signer.compute_signature(chunk2.content)

        sim = self.signer.estimate_similarity(sig1, sig2)

        # Exact SHA match or high Jaccard match
        is_exact_sha = bool(
            chunk1.content_hash
            and chunk2.content_hash
            and chunk1.content_hash == chunk2.content_hash
        )
        is_exact_jaccard = sim >= effective_exact_threshold

        if is_exact_sha or is_exact_jaccard:
            dup_type = DuplicateType.EXACT
            effective_sim = 1.0 if is_exact_sha else sim
        elif sim >= effective_threshold:
            dup_type = DuplicateType.NEAR_DUPLICATE
            effective_sim = sim
        else:
            return None

        v_num = chunk2.metadata.get("version_number") if chunk2.metadata else None

        return ChunkDuplicateMatch(
            chunk_id=chunk2.chunk_id,
            document_id=chunk2.document_id,
            document_version_id=chunk2.document_version_id,
            document_version_number=v_num,
            chunk_index=chunk2.chunk_index,
            duplicate_type=dup_type,
            similarity_score=round(effective_sim, 4),
            exact_content_match=is_exact_sha,
            content_hash=chunk2.content_hash,
            content_preview=chunk2.content[:160].strip(),
            locator=chunk2.locator,
            metadata=dict(chunk2.metadata),
        )

    def annotate_chunks(
        self,
        chunks: list[DocumentChunkInput],
        context: IngestionContext | None = None,
        scope: ComparisonScope = ComparisonScope.ALL,
        auto_index: bool = True,
        version_number: int | None = None,
    ) -> list[DocumentChunkInput]:
        """Annotate chunks with MinHash LSH deduplication metadata without deleting any chunks.

        Adheres strictly to the Non-Destructive Invariant (Requirement 9):
        All original document chunks are retained and returned intact. Deduplication
        findings are injected directly into chunk.metadata for downstream inspection.

        Injected Metadata:
            - duplicate_type: "EXACT", "NEAR_DUPLICATE", or "UNIQUE"
            - has_duplicates: bool
            - total_matches_count: int
            - primary_duplicate_match: dict representation of top match if found
            - minhash_signature: list[int]

        Args:
            chunks: List of DocumentChunkInput instances.
            context: IngestionContext providing runtime options.
            scope: ComparisonScope boundary policy.
            auto_index: Whether to index chunks as they are evaluated.
            version_number: Optional document version sequence number.

        Returns:
            The same list of DocumentChunkInput instances, augmented with metadata.
        """
        threshold = None
        if context and context.options:
            threshold = context.options.minhash_similarity_threshold

        reports = self.detect_duplicates_batch(
            chunks=chunks,
            scope=scope,
            threshold=threshold,
            auto_index=auto_index,
            version_number=version_number,
        )

        for chunk, report in zip(chunks, reports):
            # Annotate chunk metadata without mutating source content or deleting chunks
            chunk.metadata["minhash_signature"] = report.minhash_signature
            chunk.metadata["has_duplicates"] = report.has_duplicates
            chunk.metadata["duplicate_type"] = report.duplicate_type.value
            chunk.metadata["total_matches_count"] = report.total_matches_count

            if report.primary_match:
                chunk.metadata["primary_duplicate_match"] = {
                    "matched_chunk_id": report.primary_match.chunk_id,
                    "matched_document_id": report.primary_match.document_id,
                    "matched_version_id": report.primary_match.document_version_id,
                    "matched_version_number": report.primary_match.document_version_number,
                    "similarity_score": report.primary_match.similarity_score,
                    "duplicate_type": report.primary_match.duplicate_type.value,
                    "exact_content_match": report.primary_match.exact_content_match,
                }

        return chunks

    def index_chunk(
        self,
        chunk: DocumentChunkInput,
        version_number: int | None = None,
    ) -> list[int]:
        """Index a single chunk into the LSH index."""
        return self.index.index_chunk(chunk, version_number=version_number)

    def index_chunks(
        self,
        chunks: Sequence[DocumentChunkInput],
        version_number: int | None = None,
    ) -> list[list[int]]:
        """Index multiple chunks into the LSH index."""
        return self.index.index_chunks(chunks, version_number=version_number)

    def remove_chunk(self, tenant_id: str, chunk_id: str) -> bool:
        """Remove a chunk from the LSH index."""
        return self.index.remove_chunk(tenant_id, chunk_id)

    def clear_tenant(self, tenant_id: str) -> None:
        """Purge all index entries for a tenant."""
        self.index.clear_tenant(tenant_id)


# Module-level default singleton for easy consumption
_default_chunk_deduplicator: MinHashChunkDeduplicator | None = None


def get_chunk_deduplicator() -> MinHashChunkDeduplicator:
    """Provide or initialize the shared singleton MinHashChunkDeduplicator."""
    global _default_chunk_deduplicator
    if _default_chunk_deduplicator is None:
        _default_chunk_deduplicator = MinHashChunkDeduplicator()
    return _default_chunk_deduplicator

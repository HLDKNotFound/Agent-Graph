"""Locality-Sensitive Hashing (LSH) index for fast sub-linear near-duplicate chunk retrieval.

Partitions MinHash signatures into b bands of r rows to index candidates into hash buckets.
Enforces zero-trust multi-tenant isolation, version-aware filtering, and deterministic multi-match ranking.
"""

from __future__ import annotations

import logging
from collections import defaultdict
from typing import Any, Sequence

from pydantic import BaseModel, ConfigDict, Field

from backend.ingestion.config import get_ingestion_settings
from backend.ingestion.deduplication.minhash import MinHashSigner
from backend.ingestion.deduplication.models import (
    ChunkDuplicateMatch,
    ComparisonScope,
    DuplicateType,
    MinHashConfig,
)
from backend.ingestion.types.chunking import DocumentChunkInput
from backend.ingestion.types.coordinates import ChunkLocator

logger = logging.getLogger(__name__)


def compute_optimal_lsh_params(
    num_permutations: int = 128,
    threshold: float = 0.92,
) -> tuple[int, int]:
    """Compute optimal (bands, rows) parameter pair for MinHash LSH given threshold.

    The probability that two signatures with Jaccard similarity s become candidates is:
        P(candidate) = 1 - (1 - s^r)^b
    The S-curve inflection point threshold is approximately:
        t ~ (1 / b)^(1 / r)

    Args:
        num_permutations: Total permutation count N (default: 128).
        threshold: Target similarity threshold (default: 0.92).

    Returns:
        Tuple of (bands, rows) where bands * rows <= num_permutations.
    """
    if threshold <= 0.0 or threshold >= 1.0:
        raise ValueError(f"LSH threshold must be in (0.0, 1.0), got {threshold}")

    best_b, best_r = 16, 8
    best_score = float("inf")

    # Search parameter space prioritizing target recall >= 90% at threshold
    # while minimizing false positive candidate collisions (noise)
    for b in range(1, num_permutations + 1):
        for r in range(1, (num_permutations // b) + 1):
            if b * r < int(num_permutations * 0.70):
                continue
            p_target = 1.0 - (1.0 - (threshold ** r)) ** b
            p_noise = 1.0 - (1.0 - ((threshold * 0.5) ** r)) ** b
            if p_target < 0.90:
                continue
            score = p_noise + (1.0 - p_target) * 3.0 + (num_permutations - (b * r)) / num_permutations * 0.1
            if score < best_score:
                best_score = score
                best_b, best_r = b, r

    return best_b, best_r


class ChunkIndexRecord(BaseModel):
    """Internal registry record persisted in the LSH index for candidate evaluation."""

    model_config = ConfigDict(extra="ignore")

    chunk_id: str
    tenant_id: str
    document_id: str
    document_version_id: str | None = None
    document_version_number: int | None = None
    chunk_index: int = 0
    content_hash: str
    signature: list[int]
    content_preview: str = ""
    locator: ChunkLocator | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class MinHashLshIndex:
    """Locality-Sensitive Hashing (LSH) index for fast, multi-tenant near-duplicate chunk lookup.

    Features:
    1. Zero-Trust Multi-Tenancy: Buckets and records are partitioned by tenant_id.
    2. Sub-linear Query Time: Candidates retrieved in O(b) hash table lookups.
    3. Scope-Aware Filtering: Supports CROSS_VERSION, CROSS_DOCUMENT, SAME_DOCUMENT, ALL.
    4. Exact vs. Near-Duplicate Discrimination: Flags exact SHA-256 and Jaccard matches.
    5. Deterministic Multi-Match Resolution: Ranks candidates by severity, similarity, version recency.
    """

    def __init__(
        self,
        config: MinHashConfig | None = None,
        signer: MinHashSigner | None = None,
        threshold: float | None = None,
        num_permutations: int | None = None,
        bands: int | None = None,
        rows: int | None = None,
    ) -> None:
        settings = get_ingestion_settings()
        self.config = config or MinHashConfig(
            num_permutations=num_permutations
            if num_permutations is not None
            else settings.minhash_num_permutations,
            similarity_threshold=threshold
            if threshold is not None
            else settings.minhash_similarity_threshold,
            lsh_bands=bands if bands is not None else settings.minhash_lsh_bands,
            lsh_rows=rows if rows is not None else settings.minhash_lsh_rows,
        )

        self.signer = signer or MinHashSigner(config=self.config)
        self.threshold = threshold if threshold is not None else self.config.similarity_threshold
        self.exact_threshold = self.config.exact_threshold
        self.num_permutations = self.config.num_permutations

        # Determine bands (b) and rows (r)
        if self.config.lsh_bands and self.config.lsh_rows:
            self.bands = self.config.lsh_bands
            self.rows = self.config.lsh_rows
        else:
            self.bands, self.rows = compute_optimal_lsh_params(
                num_permutations=self.num_permutations,
                threshold=self.threshold,
            )

        if self.bands * self.rows > self.num_permutations:
            raise ValueError(
                f"Invalid LSH parameters: bands ({self.bands}) * rows ({self.rows}) = "
                f"{self.bands * self.rows} > num_permutations ({self.num_permutations})"
            )

        # Multi-Tenant Inverted Index Buckets:
        # _buckets[tenant_id][(band_idx, band_hash)] -> set of chunk_ids
        self._buckets: dict[str, dict[tuple[int, int], set[str]]] = defaultdict(
            lambda: defaultdict(set)
        )

        # Multi-Tenant Stored Chunk Records:
        # _chunk_records[tenant_id][chunk_id] -> ChunkIndexRecord
        self._chunk_records: dict[str, dict[str, ChunkIndexRecord]] = defaultdict(dict)

    def _hash_band(self, band_slice: Sequence[int]) -> int:
        """Compute 64-bit integer hash for a band slice."""
        return hash(tuple(band_slice))

    def index_chunk(
        self,
        chunk: DocumentChunkInput,
        signature: list[int] | None = None,
        version_number: int | None = None,
        content_preview_len: int = 160,
    ) -> list[int]:
        """Index a DocumentChunkInput into the tenant's LSH buckets.

        Args:
            chunk: Input document chunk contract.
            signature: Optional precomputed MinHash signature.
            version_number: Optional version number integer.
            content_preview_len: Length of preview string to store.

        Returns:
            Computed or provided MinHash signature.
        """
        tenant_id = chunk.tenant_id
        chunk_id = chunk.chunk_id

        if signature is None:
            signature = self.signer.compute_signature(chunk.content)

        # Derive version number from metadata if not explicitly supplied
        effective_version = version_number
        if effective_version is None and chunk.metadata:
            effective_version = chunk.metadata.get("version_number")

        preview = chunk.content[:content_preview_len].strip()
        record = ChunkIndexRecord(
            chunk_id=chunk_id,
            tenant_id=tenant_id,
            document_id=chunk.document_id,
            document_version_id=chunk.document_version_id,
            document_version_number=effective_version,
            chunk_index=chunk.chunk_index,
            content_hash=chunk.content_hash,
            signature=signature,
            content_preview=preview,
            locator=chunk.locator,
            metadata=dict(chunk.metadata),
        )

        self._chunk_records[tenant_id][chunk_id] = record

        # Insert chunk_id into each band bucket
        for band_idx in range(self.bands):
            start = band_idx * self.rows
            end = start + self.rows
            band_slice = signature[start:end]
            band_hash = self._hash_band(band_slice)
            self._buckets[tenant_id][(band_idx, band_hash)].add(chunk_id)

        return signature

    def index_chunks(
        self,
        chunks: Sequence[DocumentChunkInput],
        version_number: int | None = None,
    ) -> list[list[int]]:
        """Batch index multiple chunks into the LSH index.

        Args:
            chunks: Sequence of DocumentChunkInput instances.
            version_number: Optional version number.

        Returns:
            List of generated MinHash signatures in chunk order.
        """
        return [
            self.index_chunk(chunk, version_number=version_number)
            for chunk in chunks
        ]

    def query(
        self,
        signature: list[int],
        tenant_id: str,
        query_chunk_id: str | None = None,
        document_id: str | None = None,
        document_version_id: str | None = None,
        document_version_number: int | None = None,
        content_hash: str | None = None,
        raw_text: str | None = None,
        scope: ComparisonScope = ComparisonScope.ALL,
        threshold: float | None = None,
        exact_threshold: float | None = None,
    ) -> list[ChunkDuplicateMatch]:
        """Query the LSH index for near-duplicate and exact-duplicate chunk candidates.

        Retrieves candidates colliding in >= 1 band, applies scope filtering, evaluates
        MinHash Jaccard similarity, and ranks qualifying matches.

        Args:
            signature: MinHash signature of the query chunk.
            tenant_id: Owning tenant UUID enforcing strict boundary isolation.
            query_chunk_id: UUID of query chunk to prevent self-collision.
            document_id: Document UUID of query chunk for scope filtering.
            document_version_id: Version UUID of query chunk.
            document_version_number: Sequence version number of query chunk.
            content_hash: SHA-256 hash of query chunk for exact matching.
            raw_text: Source text of query chunk for fallback exact match.
            scope: ComparisonScope boundary policy (CROSS_VERSION, CROSS_DOCUMENT, SAME_DOCUMENT, ALL).
            threshold: Jaccard threshold override (defaults to self.threshold).
            exact_threshold: Exact threshold override (defaults to self.exact_threshold).

        Returns:
            List of ChunkDuplicateMatch instances meeting threshold, deterministically ranked.
        """
        effective_threshold = threshold if threshold is not None else self.threshold
        effective_exact_threshold = (
            exact_threshold if exact_threshold is not None else self.exact_threshold
        )

        tenant_buckets = self._buckets.get(tenant_id)
        if not tenant_buckets:
            return []

        tenant_records = self._chunk_records.get(tenant_id, {})
        if not tenant_records:
            return []

        # 1. Candidate Generation: Union of colliding bucket chunk IDs
        candidate_ids: set[str] = set()
        for band_idx in range(self.bands):
            start = band_idx * self.rows
            end = start + self.rows
            band_slice = signature[start:end]
            band_hash = self._hash_band(band_slice)
            bucket_chunks = tenant_buckets.get((band_idx, band_hash))
            if bucket_chunks:
                candidate_ids.update(bucket_chunks)

        # 2. Scope & Invariant Filtering
        matches: list[ChunkDuplicateMatch] = []
        for cand_id in candidate_ids:
            # Exclude self
            if query_chunk_id and cand_id == query_chunk_id:
                continue

            record = tenant_records.get(cand_id)
            if not record:
                continue

            # Scope Filtering
            if scope == ComparisonScope.CROSS_VERSION:
                # Must be same document, but DIFFERENT version
                if document_id and record.document_id != document_id:
                    continue
                # If version IDs or numbers are available, verify they differ
                if document_version_id and record.document_version_id == document_version_id:
                    continue
                if (
                    document_version_number is not None
                    and record.document_version_number is not None
                    and record.document_version_number == document_version_number
                ):
                    continue

            elif scope == ComparisonScope.CROSS_DOCUMENT:
                # Must be a DIFFERENT document
                if document_id and record.document_id == document_id:
                    continue

            elif scope == ComparisonScope.SAME_DOCUMENT:
                # Must be the SAME document
                if document_id and record.document_id != document_id:
                    continue

            # 3. Similarity Evaluation
            similarity = self.signer.estimate_similarity(signature, record.signature)

            # Check exact matching criteria
            is_exact_sha = bool(
                content_hash and record.content_hash and content_hash == record.content_hash
            )
            is_exact_jaccard = similarity >= effective_exact_threshold

            if is_exact_sha or is_exact_jaccard:
                dup_type = DuplicateType.EXACT
                # If exact SHA match, treat similarity as 1.0
                effective_sim = 1.0 if is_exact_sha else similarity
            elif similarity >= effective_threshold:
                dup_type = DuplicateType.NEAR_DUPLICATE
                effective_sim = similarity
            else:
                # Candidate false positive from LSH band collision, below threshold
                continue

            match = ChunkDuplicateMatch(
                chunk_id=record.chunk_id,
                document_id=record.document_id,
                document_version_id=record.document_version_id,
                document_version_number=record.document_version_number,
                chunk_index=record.chunk_index,
                duplicate_type=dup_type,
                similarity_score=round(effective_sim, 4),
                exact_content_match=is_exact_sha,
                content_hash=record.content_hash,
                content_preview=record.content_preview,
                locator=record.locator,
                metadata=record.metadata,
            )
            matches.append(match)

        # 4. Multi-Match Deterministic Ordering (Requirement 12)
        # Sort priority:
        # 1. EXACT duplicates first (-1 for EXACT, 0 for NEAR_DUPLICATE)
        # 2. Similarity score descending (-similarity)
        # 3. Document version descending (-version_number)
        # 4. Chunk index ascending
        # 5. Chunk ID ascending (tie-breaker)
        def _sort_key(m: ChunkDuplicateMatch) -> tuple[int, float, int, int, str]:
            severity_rank = 0 if m.duplicate_type == DuplicateType.EXACT else 1
            version_rank = -(m.document_version_number or 0)
            return (
                severity_rank,
                -m.similarity_score,
                version_rank,
                m.chunk_index if m.chunk_index is not None else 0,
                m.chunk_id,
            )

        matches.sort(key=_sort_key)
        return matches

    def remove_chunk(self, tenant_id: str, chunk_id: str) -> bool:
        """Remove a chunk from the LSH index and bucket tables.

        Ensures clean rollback and prevents orphan references.
        """
        record = self._chunk_records.get(tenant_id, {}).pop(chunk_id, None)
        if not record:
            return False

        tenant_buckets = self._buckets.get(tenant_id)
        if tenant_buckets:
            for band_idx in range(self.bands):
                start = band_idx * self.rows
                end = start + self.rows
                band_slice = record.signature[start:end]
                band_hash = self._hash_band(band_slice)
                bucket = tenant_buckets.get((band_idx, band_hash))
                if bucket:
                    bucket.discard(chunk_id)
                    if not bucket:
                        tenant_buckets.pop((band_idx, band_hash), None)

        return True

    def clear_tenant(self, tenant_id: str) -> None:
        """Purge all index entries and buckets for a specific tenant."""
        self._buckets.pop(tenant_id, None)
        self._chunk_records.pop(tenant_id, None)

    def count_chunks(self, tenant_id: str) -> int:
        """Return the number of indexed chunks under a tenant boundary."""
        return len(self._chunk_records.get(tenant_id, {}))

"""Deduplication subsystem contracts, streaming hash calculators, and policies."""

from backend.ingestion.deduplication.base import (
    DeduplicationResult,
    DeduplicationStrategy,
)
from backend.ingestion.deduplication.hasher import (
    StreamHasher,
    compute_bytes_sha256,
    compute_content_sha256,
    compute_file_sha256,
    compute_stream_sha256,
)
from backend.ingestion.deduplication.chunk_deduplicator import (
    MinHashChunkDeduplicator,
    get_chunk_deduplicator,
)
from backend.ingestion.deduplication.lsh import (
    ChunkIndexRecord,
    MinHashLshIndex,
    compute_optimal_lsh_params,
)
from backend.ingestion.deduplication.minhash import (
    MERSENNE_PRIME,
    MinHashSigner,
    generate_shingles,
    normalize_chunk_text,
)
from backend.ingestion.deduplication.models import (
    ChunkDuplicateMatch,
    ChunkDuplicateReport,
    ComparisonScope,
    DocumentCreateInput,
    DocumentVersionCreateInput,
    DuplicateAction,
    DuplicateHandlingPolicy,
    DuplicateType,
    ExistingDocumentSummary,
    MinHashConfig,
)
from backend.ingestion.deduplication.repository import (
    DocumentRepository,
    InMemoryDocumentRepository,
    PostgresDocumentRepository,
)
from backend.ingestion.deduplication.service import (
    DocumentDeduplicator,
    MinHashDeduplicationStrategy,
    Sha256ExactDeduplicationStrategy,
)

__all__ = [
    # Contracts & Models
    "DeduplicationResult",
    "DeduplicationStrategy",
    "DuplicateHandlingPolicy",
    "DuplicateAction",
    "DuplicateType",
    "ComparisonScope",
    "MinHashConfig",
    "ChunkDuplicateMatch",
    "ChunkDuplicateReport",
    "ExistingDocumentSummary",
    "DocumentCreateInput",
    "DocumentVersionCreateInput",
    # Service & Strategy
    "DocumentDeduplicator",
    "Sha256ExactDeduplicationStrategy",
    "MinHashDeduplicationStrategy",
    "MinHashChunkDeduplicator",
    "get_chunk_deduplicator",
    # MinHash & LSH Subsystem
    "MinHashSigner",
    "normalize_chunk_text",
    "generate_shingles",
    "MinHashLshIndex",
    "compute_optimal_lsh_params",
    "ChunkIndexRecord",
    "MERSENNE_PRIME",
    # Repositories
    "DocumentRepository",
    "PostgresDocumentRepository",
    "InMemoryDocumentRepository",
    # Streaming Hasher
    "StreamHasher",
    "compute_stream_sha256",
    "compute_file_sha256",
    "compute_bytes_sha256",
    "compute_content_sha256",
]

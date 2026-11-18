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
from backend.ingestion.deduplication.models import (
    DocumentCreateInput,
    DocumentVersionCreateInput,
    DuplicateAction,
    DuplicateHandlingPolicy,
    ExistingDocumentSummary,
)
from backend.ingestion.deduplication.repository import (
    DocumentRepository,
    InMemoryDocumentRepository,
    PostgresDocumentRepository,
)
from backend.ingestion.deduplication.service import (
    DocumentDeduplicator,
    Sha256ExactDeduplicationStrategy,
)

__all__ = [
    # Contracts & Models
    "DeduplicationResult",
    "DeduplicationStrategy",
    "DuplicateHandlingPolicy",
    "DuplicateAction",
    "ExistingDocumentSummary",
    "DocumentCreateInput",
    "DocumentVersionCreateInput",
    # Service & Strategy
    "DocumentDeduplicator",
    "Sha256ExactDeduplicationStrategy",
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

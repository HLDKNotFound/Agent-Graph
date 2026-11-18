"""Deduplication contracts for exact file hash and near-duplicate chunk detection."""

import abc
from typing import Any
from pydantic import BaseModel, ConfigDict, Field

from backend.ingestion.types.chunking import DocumentChunkInput
from backend.ingestion.types.context import IngestionContext


from backend.ingestion.deduplication.models import (
    DuplicateAction,
    DuplicateHandlingPolicy,
    ExistingDocumentSummary,
)


class DeduplicationResult(BaseModel):
    """Outcome report for document-level or chunk-level deduplication analysis."""

    model_config = ConfigDict(frozen=True)

    is_duplicate: bool = Field(..., description="Whether duplicate was detected")
    action: DuplicateAction = Field(
        default=DuplicateAction.PROCEED,
        description="Action taken by the deduplication engine (PROCEED, RETURN_EXISTING, REJECTED, NEW_VERSION_CREATED)",
    )
    similarity_score: float = Field(
        default=0.0,
        ge=0.0,
        le=1.0,
        description="Calculated similarity metric (1.0 for exact SHA-256 match, Jaccard for MinHash)",
    )
    content_hash: str | None = Field(
        default=None,
        description="Calculated SHA-256 hexadecimal hash",
    )
    file_size_bytes: int | None = Field(
        default=None,
        description="Calculated file size in bytes from streaming read",
    )
    existing_document_id: str | None = Field(
        default=None,
        description="UUID of existing document if exact match found in database",
    )
    existing_document: ExistingDocumentSummary | None = Field(
        default=None,
        description="Full summary snapshot of existing document if active duplicate detected",
    )
    existing_chunk_id: str | None = Field(
        default=None,
        description="UUID of existing chunk if near-duplicate found in vector/LSH index",
    )
    message: str | None = Field(
        default=None,
        description="Informational reason or match diagnosis",
    )


class DeduplicationStrategy(abc.ABC):
    """Abstract contract for document-level SHA-256 and chunk-level MinHash LSH deduplication."""

    @property
    @abc.abstractmethod
    def name(self) -> str:
        """Deduplication engine identifier."""
        raise NotImplementedError

    @abc.abstractmethod
    async def check_document_duplicate(
        self,
        file_hash_sha256: str,
        context: IngestionContext,
    ) -> DeduplicationResult:
        """Verify whether an identical document SHA-256 hash exists under the authenticated tenant."""
        raise NotImplementedError

    @abc.abstractmethod
    async def filter_duplicate_chunks(
        self,
        chunks: list[DocumentChunkInput],
        context: IngestionContext,
    ) -> list[DocumentChunkInput]:
        """Filter or annotate near-duplicate chunks using MinHash LSH shingle indexing."""
        raise NotImplementedError

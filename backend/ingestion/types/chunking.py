"""Chunking configuration and chunk data contracts for relational and vector persistence."""

import uuid
from typing import Any
from pydantic import BaseModel, ConfigDict, Field

from backend.ingestion.types.coordinates import ChunkLocator
from backend.ingestion.types.enums import ChunkingStrategyType
from backend.ingestion.types.metadata import AccessControlPayload


class ChunkingConfig(BaseModel):
    """Configuration options governing chunk segmentation and structural boundary detection."""

    model_config = ConfigDict(frozen=True)

    strategy: ChunkingStrategyType = Field(
        default=ChunkingStrategyType.RECURSIVE_STRUCTURAL,
        description="Chunking algorithm to employ",
    )
    chunk_size_tokens: int = Field(
        default=512,
        ge=64,
        le=8192,
        description="Target maximum token count per chunk",
    )
    chunk_overlap_tokens: int = Field(
        default=64,
        ge=0,
        le=1024,
        description="Number of overlapping tokens between adjacent sequential chunks (~12.5%)",
    )
    separators: list[str] = Field(
        default_factory=lambda: ["\n\n# ", "\n\n## ", "\n\n### ", "\n\n", "\n", ". "],
        description="Hierarchical text split separators in order of structural priority",
    )
    preserve_headers: bool = Field(
        default=True,
        description="Prepend nearest parent section heading to child chunks to retain semantic context",
    )


class DocumentChunkInput(BaseModel):
    """Fully attributed document chunk contract ready for persistence in PostgreSQL and Qdrant.

    Adheres strictly to the DocumentChunk schema and Qdrant zero-trust vector payload:
    - Content hashing: SHA-256
    - Lineage coordinates: page_number, section_heading, sheet_name, row_range, [char_start, char_end)
    - Zero-trust access control: tenant_id, roles, departments, users
    """

    model_config = ConfigDict(extra="allow")

    chunk_id: str = Field(
        default_factory=lambda: str(uuid.uuid4()),
        description="UUID v4 identifier for the chunk",
    )
    tenant_id: str = Field(..., description="Tenant UUID for multi-tenant isolation")
    document_id: str = Field(..., description="Parent document UUID")
    document_version_id: str | None = Field(
        default=None,
        description="Document version UUID if versioned",
    )
    chunk_index: int = Field(..., ge=0, description="0-indexed deterministic sequence order")
    content: str = Field(..., min_length=1, description="Textual body content of chunk")
    content_hash: str = Field(
        ...,
        min_length=64,
        max_length=64,
        pattern=r"^[a-fA-F0-9]{64}$",
        description="SHA-256 hash of chunk content for exact deduplication",
    )
    token_count: int = Field(..., ge=0, description="Calculated token length")
    locator: ChunkLocator = Field(
        default_factory=ChunkLocator,
        description="Source provenance coordinates",
    )
    acl: AccessControlPayload = Field(
        ...,
        description="Zero-trust access control metadata for Qdrant payload and RBAC filtering",
    )
    metadata: dict[str, Any] = Field(
        default_factory=dict,
        description="Additional custom chunk metadata",
    )
    embedding_id: str | None = Field(
        default=None,
        description="Identifier or hash of vector representation in embedding cache",
    )
    vector_storage_id: str | None = Field(
        default=None,
        description="Point ID in Qdrant vector database collection",
    )

    def to_qdrant_payload(self) -> dict[str, Any]:
        """Generate the standardized Qdrant payload as required by Step 5 of Ingestion Pipeline."""
        return {
            "document_id": self.document_id,
            "chunk_index": self.chunk_index,
            "content": self.content,
            "source_location": self.locator.to_provenance_payload(),
            "acl": self.acl.to_qdrant_acl(),
            "metadata": self.metadata,
        }

    def to_db_record(self) -> dict[str, Any]:
        """Generate dict representation matching PostgreSQL document_chunks table schema."""
        return {
            "id": self.chunk_id,
            "tenant_id": self.tenant_id,
            "document_id": self.document_id,
            "document_version_id": self.document_version_id,
            "chunk_index": self.chunk_index,
            "content": self.content,
            "content_hash": self.content_hash,
            "token_count": self.token_count,
            "page_number": self.locator.page_number,
            "section_heading": self.locator.section_heading,
            "sheet_name": self.locator.sheet_name,
            "row_range": self.locator.row_range,
            "char_start": self.locator.char_start,
            "char_end": self.locator.char_end,
            "embedding_id": self.embedding_id,
            "vector_storage_id": self.vector_storage_id,
            "metadata": self.metadata,
        }

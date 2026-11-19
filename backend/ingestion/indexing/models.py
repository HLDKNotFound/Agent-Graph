"""Data contracts, payload serialization, and configuration options for Qdrant vector indexing."""

from __future__ import annotations

import uuid
from typing import Any
from pydantic import BaseModel, ConfigDict, Field

from backend.ingestion.types.chunking import DocumentChunkInput


def to_point_id(chunk_id: str) -> str:
    """Generate a deterministic, valid Qdrant point UUID from a database chunk_id.

    If chunk_id is already a valid UUID string, returns its normalized canonical representation.
    If chunk_id is a custom identifier (e.g. 'chk_doc1_0001'), generates a deterministic UUIDv5
    within the DNS namespace to ensure 100% stability across idempotent operations.

    Args:
        chunk_id: Database chunk identifier.

    Returns:
        Canonical 36-character hyphenated UUID string.
    """
    try:
        return str(uuid.UUID(str(chunk_id).strip()))
    except (ValueError, AttributeError):
        return str(uuid.uuid5(uuid.NAMESPACE_DNS, str(chunk_id).strip()))


def serialize_chunk_payload(chunk: DocumentChunkInput) -> dict[str, Any]:
    """Serialize a DocumentChunkInput into a standard searchable Qdrant point payload.

    Implements payload serialization satisfying:
    - Requirement 7: tenant_id, document_id, chunk_id, allowed_roles, allowed_departments,
      page_number, section_heading.
    - Requirement 8: sheet_name, row_range, char_start, char_end, bounding_box.
    - Zero-trust security: full ACL object for RBAC filtering.
    - Round-trip provenance reconstruction for citation badges.

    Args:
        chunk: Attributed DocumentChunkInput contract.

    Returns:
        Dictionary payload ready for Qdrant PointStruct storage.
    """
    return {
        # Core Identity & Isolation (Requirement 7 & 10)
        "tenant_id": chunk.tenant_id,
        "document_id": chunk.document_id,
        "chunk_id": chunk.chunk_id,
        "chunk_index": chunk.chunk_index,
        "document_version_id": chunk.document_version_id,
        # Textual & Content Metadata
        "content": chunk.content,
        "content_hash": chunk.content_hash,
        "token_count": chunk.token_count,
        # Access Control & RBAC Filtering (Requirement 7 & Module 8)
        "allowed_roles": list(chunk.acl.roles),
        "allowed_departments": list(chunk.acl.departments),
        "allowed_users": list(chunk.acl.users),
        "acl": chunk.acl.to_qdrant_acl(),
        # Source Lineage & Provenance Coordinates (Requirement 7 & 8)
        "page_number": chunk.locator.page_number,
        "section_heading": chunk.locator.section_heading,
        "sheet_name": chunk.locator.sheet_name,
        "row_range": chunk.locator.row_range,
        "char_start": chunk.locator.char_start,
        "char_end": chunk.locator.char_end,
        "source_location": chunk.locator.to_provenance_payload(),
        # Custom Metadata
        "metadata": dict(chunk.metadata),
    }


class QdrantConfig(BaseModel):
    """Configuration options governing Qdrant vector database connections and collections."""

    model_config = ConfigDict(frozen=True)

    host: str = Field(default="localhost", description="Qdrant host or IP")
    port: int = Field(default=6333, ge=1, le=65535, description="Qdrant HTTP port")
    grpc_port: int = Field(default=6334, ge=1, le=65535, description="Qdrant gRPC port")
    api_key: str | None = Field(default=None, description="Qdrant API key")
    url: str | None = Field(default=None, description="Full Qdrant URL (e.g. cloud or ':memory:')")
    path: str | None = Field(default=None, description="Local on-disk storage path or ':memory:'")
    collection_name: str = Field(
        default="enterprise_knowledge_chunks",
        description="Target Qdrant collection name",
    )
    vector_dimension: int = Field(
        default=1024,
        ge=1,
        description="Dense vector dimension (1024 for BGE-M3)",
    )
    distance: str = Field(
        default="Cosine",
        description="Distance metric ('Cosine', 'Dot', 'Euclid')",
    )
    hnsw_m: int = Field(
        default=16,
        ge=4,
        le=128,
        description="Number of edges per node in HNSW index",
    )
    hnsw_ef_construct: int = Field(
        default=128,
        ge=16,
        le=512,
        description="Candidate list size during HNSW construction",
    )
    hnsw_ef: int = Field(
        default=64,
        ge=8,
        le=512,
        description="Search candidate list size at runtime",
    )
    prefer_grpc: bool = Field(
        default=False,
        description="Whether to use gRPC transport instead of HTTP",
    )
    batch_size: int = Field(
        default=64,
        ge=1,
        le=500,
        description="Default batch size for vector indexing operations",
    )
    timeout: float = Field(
        default=30.0,
        ge=1.0,
        description="Connection and query timeout in seconds",
    )


class QdrantPointRecord(BaseModel):
    """Retrieved vector point representation from Qdrant."""

    model_config = ConfigDict(frozen=True)

    point_id: str = Field(..., description="Qdrant point UUID")
    chunk_id: str = Field(..., description="Original database chunk identifier")
    document_id: str = Field(..., description="Parent document identifier")
    tenant_id: str = Field(..., description="Owning tenant identifier")
    vector: list[float] | None = Field(default=None, description="Dense embedding vector array if requested")
    payload: dict[str, Any] = Field(default_factory=dict, description="Stored payload metadata")


class QdrantBatchIndexResult(BaseModel):
    """Comprehensive outcome report for batch indexing operations."""

    model_config = ConfigDict(frozen=True)

    collection_name: str = Field(..., description="Collection targeted")
    points_indexed: int = Field(..., ge=0, description="Number of points successfully upserted")
    points_failed: int = Field(default=0, ge=0, description="Number of points that failed indexing")
    vector_storage_ids: list[str] = Field(default_factory=list, description="List of created/updated point UUIDs")
    failed_chunk_ids: list[str] = Field(default_factory=list, description="IDs of chunks that failed")
    error_details: dict[str, Any] = Field(default_factory=dict, description="Structured failure details if any")

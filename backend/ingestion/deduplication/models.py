"""Data contracts, models, and enums for document deduplication and duplicate handling."""

from datetime import datetime
from enum import Enum
from typing import Any
from pydantic import BaseModel, ConfigDict, Field


class DuplicateHandlingPolicy(str, Enum):
    """Configures system behavior when an exact document content hash match is detected."""

    RETURN_EXISTING = "RETURN_EXISTING"
    """Reuse and return existing document record; bypass redundant extraction/storage."""

    REJECT = "REJECT"
    """Reject upload and raise DuplicateDocumentError."""

    NEW_VERSION = "NEW_VERSION"
    """Create a new version under the existing document model if identical hash is treated as revision."""


class DuplicateAction(str, Enum):
    """Action outcome recorded during document deduplication check."""

    PROCEED = "PROCEED"
    """No active duplicate detected; proceed to extraction and chunking pipeline."""

    RETURN_EXISTING = "RETURN_EXISTING"
    """Active duplicate document found; returned existing document reference."""

    REJECTED = "REJECTED"
    """Duplicate detected and rejected according to REJECT policy."""

    NEW_VERSION_CREATED = "NEW_VERSION_CREATED"
    """New version snapshot created under parent document lineage."""


class ExistingDocumentSummary(BaseModel):
    """Structured summary of an existing document record matching a content hash."""

    model_config = ConfigDict(extra="ignore")

    document_id: str = Field(..., description="UUID of the existing document")
    tenant_id: str = Field(..., description="UUID of owning tenant")
    department_id: str | None = Field(default=None, description="Department UUID if assigned")
    uploaded_by_id: str | None = Field(default=None, description="Uploader user UUID")
    current_version_id: str | None = Field(default=None, description="UUID of current active version")
    original_filename: str = Field(..., description="Original filename upon upload")
    normalized_filename: str | None = Field(default=None, description="Sanitized/normalized filename")
    storage_uri: str | None = Field(default=None, description="Canonical storage URI (s3:// or file://)")
    mime_type: str | None = Field(default=None, description="MIME content type")
    file_size_bytes: int = Field(default=0, description="Size of file in bytes")
    content_hash: str = Field(..., description="SHA-256 hexadecimal digest")
    classification: str = Field(default="INTERNAL", description="Document security classification")
    status: str = Field(default="PENDING", description="Document ingestion status")
    metadata: dict[str, Any] = Field(default_factory=dict, description="Custom JSON metadata attributes")
    version_number: int | None = Field(default=1, description="Current version number")
    created_at: datetime | None = Field(default=None, description="Document creation timestamp")
    updated_at: datetime | None = Field(default=None, description="Document update timestamp")


class DocumentCreateInput(BaseModel):
    """Typed payload for inserting a new document record with race safety."""

    model_config = ConfigDict(extra="ignore")

    document_id: str | None = Field(default=None, description="Explicit UUID or auto-generated")
    tenant_id: str = Field(..., description="Owning tenant UUID")
    department_id: str | None = Field(default=None, description="Associated department UUID")
    uploaded_by_id: str | None = Field(default=None, description="Uploader user UUID")
    original_filename: str = Field(..., description="Original uploaded filename")
    normalized_filename: str = Field(..., description="Sanitized filename")
    storage_uri: str = Field(..., description="Canonical storage URI")
    mime_type: str = Field(default="application/octet-stream", description="MIME type")
    file_size_bytes: int = Field(..., ge=0, description="Exact file size in bytes")
    content_hash: str = Field(..., min_length=64, max_length=64, description="SHA-256 hex digest")
    classification: str = Field(default="INTERNAL", description="Classification enum")
    status: str = Field(default="PENDING", description="Ingestion status enum")
    metadata: dict[str, Any] = Field(default_factory=dict, description="Metadata JSON payload")


class DocumentVersionCreateInput(BaseModel):
    """Typed payload for creating an incremental version snapshot."""

    model_config = ConfigDict(extra="ignore")

    document_id: str = Field(..., description="Parent document UUID")
    tenant_id: str = Field(..., description="Parent tenant UUID")
    created_by_id: str | None = Field(default=None, description="Creator user UUID")
    original_filename: str = Field(..., description="Version original filename")
    storage_uri: str = Field(..., description="Version storage URI")
    mime_type: str = Field(default="application/octet-stream", description="MIME type")
    file_size_bytes: int = Field(..., ge=0, description="Version size in bytes")
    content_hash: str = Field(..., min_length=64, max_length=64, description="SHA-256 hex digest")
    status: str = Field(default="PENDING", description="Version ingestion status")
    metadata: dict[str, Any] = Field(default_factory=dict, description="Version metadata JSON")
    is_current: bool = Field(default=True, description="Whether to promote as current version")

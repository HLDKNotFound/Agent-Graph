"""Storage data models for persisted enterprise documents."""

from datetime import datetime, timezone
from typing import Any
from pydantic import BaseModel, ConfigDict, Field


class StoredFileMetadata(BaseModel):
    """Metadata report for a stored document artifact, maintaining content hash and provenance."""

    model_config = ConfigDict(frozen=True)

    storage_key: str = Field(..., description="Normalized relative object storage key")
    storage_uri: str = Field(..., description="Canonical URI (e.g. file:///... or s3://...)")
    tenant_id: str = Field(..., description="Owning tenant UUID ensuring zero-trust boundary")
    document_id: str = Field(..., description="Target enterprise document UUID")
    version_id: str | None = Field(default=None, description="Document version snapshot UUID")
    filename: str = Field(..., description="Sanitized original document filename")
    file_size_bytes: int = Field(..., ge=0, description="Exact file size in bytes")
    content_hash: str = Field(
        ...,
        min_length=64,
        max_length=64,
        description="SHA-256 digest of stored binary content",
    )
    mime_type: str = Field(..., description="Confirmed MIME content type")
    created_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc),
        description="Storage creation timestamp",
    )
    updated_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc),
        description="Last modified timestamp",
    )
    metadata: dict[str, Any] = Field(
        default_factory=dict,
        description="Driver-specific physical metadata (ETag, storage class, etc.)",
    )

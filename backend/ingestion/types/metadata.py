"""Metadata and access control data contracts for enterprise documents."""

from datetime import datetime, timezone
from typing import Any
from uuid import UUID
from pydantic import BaseModel, ConfigDict, Field

from backend.ingestion.types.enums import DocumentClassification, DocumentType


class AccessControlPayload(BaseModel):
    """Zero-trust RBAC access control list for document vector payloads and database filtering."""

    model_config = ConfigDict(frozen=True)

    tenant_id: str = Field(..., description="Tenant identifier for strict multi-tenant isolation")
    roles: list[str] = Field(default_factory=list, description="Roles permitted to view chunk")
    departments: list[str] = Field(
        default_factory=list,
        description="Departments permitted to view chunk",
    )
    users: list[str] = Field(
        default_factory=list,
        description="Specific user IDs explicitly permitted to view chunk",
    )

    def to_qdrant_acl(self) -> dict[str, Any]:
        """Format as Qdrant vector payload ACL object."""
        return {
            "tenant_id": self.tenant_id,
            "roles": list(self.roles),
            "departments": list(self.departments),
            "users": list(self.users),
        }


class DocumentMetadata(BaseModel):
    """Enterprise document metadata attributes matching database models and TRD specifications."""

    model_config = ConfigDict(extra="allow")

    document_id: str = Field(..., description="Unique document UUID")
    tenant_id: str = Field(..., description="Tenant UUID")
    filename: str = Field(..., description="Original filename with extension")
    file_hash_sha256: str = Field(
        ...,
        min_length=64,
        max_length=64,
        pattern=r"^[a-fA-F0-9]{64}$",
        description="SHA-256 digest of original raw binary content",
    )
    file_size_bytes: int = Field(..., ge=0, description="Raw binary file size in bytes")
    mime_type: str = Field(..., description="Detected or declared MIME content type")
    document_type: DocumentType = Field(..., description="Normalized document type")
    classification: DocumentClassification = Field(
        default=DocumentClassification.INTERNAL,
        description="Security classification level",
    )
    department_id: str | None = Field(default=None, description="Owning department UUID")
    uploaded_by_id: str | None = Field(default=None, description="User UUID of uploader")
    author: str | None = Field(default=None, description="Extracted or declared author")
    title: str | None = Field(default=None, description="Document title extracted or declared")
    total_pages: int | None = Field(default=None, ge=1, description="Total document pages")
    version_number: int = Field(default=1, ge=1, description="Current version number")
    storage_uri: str | None = Field(default=None, description="S3 / Object storage URI reference")
    custom_metadata: dict[str, Any] = Field(
        default_factory=dict,
        description="Arbitrary additional key-value properties",
    )
    created_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc),
        description="Record creation timestamp",
    )
    updated_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc),
        description="Record update timestamp",
    )

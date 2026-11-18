"""Ingestion execution context and runtime option parameters."""

import uuid
from typing import Any
from pydantic import BaseModel, ConfigDict, Field

from backend.ingestion.types.chunking import ChunkingConfig
from backend.ingestion.types.enums import (
    DocumentClassification,
    DocumentIngestionStatus,
    PipelineStage,
)
from backend.ingestion.types.metadata import AccessControlPayload


class IngestionOptions(BaseModel):
    """Runtime toggles and hyperparameters configuring an ingestion pipeline run."""

    model_config = ConfigDict(frozen=True)

    enable_ocr: bool = Field(
        default=False,
        description="Enable OCR processing for image-only or scanned pages",
    )
    ocr_language: str = Field(
        default="eng+vie",
        description="OCR language pack identifier (e.g. 'eng+vie' for English & Vietnamese)",
    )
    chunking_config: ChunkingConfig = Field(
        default_factory=ChunkingConfig,
        description="Algorithm configuration for chunk splitting",
    )
    deduplication_enabled: bool = Field(
        default=True,
        description="Enable pre-ingestion SHA-256 and chunk MinHash LSH deduplication",
    )
    duplicate_handling_policy: str = Field(
        default="RETURN_EXISTING",
        description="Duplicate handling strategy: 'RETURN_EXISTING', 'REJECT', or 'NEW_VERSION'",
    )
    minhash_similarity_threshold: float = Field(
        default=0.92,
        ge=0.0,
        le=1.0,
        description="Jaccard similarity threshold for near-duplicate chunk detection",
    )
    generate_embeddings: bool = Field(
        default=True,
        description="Execute embedding generation for produced chunks",
    )
    index_vectors: bool = Field(
        default=True,
        description="Upsert embedding vectors into Qdrant vector database collection",
    )
    emit_table_elements: bool = Field(
        default=True,
        description="Emit TableElement representing structured tables for spreadsheet worksheets",
    )
    emit_row_elements: bool = Field(
        default=True,
        description="Emit SpreadsheetRowElement representing individual row records",
    )
    max_rows_per_sheet: int | None = Field(
        default=None,
        description="Optional maximum rows to parse per worksheet before truncation",
    )


class IngestionContext(BaseModel):
    """Multi-tenant, security-aware execution context for an ingestion workflow.

    Enforces zero-trust RBAC by capturing authenticated tenant identity, user claims,
    and target document parameters.
    """

    model_config = ConfigDict(extra="allow")

    tenant_id: str = Field(..., description="Authenticated tenant identifier")
    document_id: str = Field(..., description="Target document UUID")
    version_id: str | None = Field(default=None, description="Document revision version UUID")
    user_id: str | None = Field(default=None, description="Authenticated actor user UUID")
    department_id: str | None = Field(default=None, description="Target department identifier")
    roles: list[str] = Field(
        default_factory=list,
        description="Authorized roles permitted to access this document's chunks",
    )
    departments: list[str] = Field(
        default_factory=list,
        description="Authorized departments permitted to access this document's chunks",
    )
    allowed_users: list[str] = Field(
        default_factory=list,
        description="Specific user IDs permitted to access this document",
    )
    classification: DocumentClassification = Field(
        default=DocumentClassification.INTERNAL,
        description="Security classification level",
    )
    options: IngestionOptions = Field(
        default_factory=IngestionOptions,
        description="Ingestion execution options",
    )
    trace_id: str = Field(
        default_factory=lambda: str(uuid.uuid4()),
        description="Distributed tracing identifier (e.g. Langfuse / OpenTelemetry)",
    )
    request_id: str | None = Field(
        default=None,
        description="HTTP or message queue request ID",
    )
    status: DocumentIngestionStatus = Field(
        default=DocumentIngestionStatus.PENDING,
        description="Current ingestion lifecycle status",
    )
    current_stage: PipelineStage = Field(
        default=PipelineStage.VALIDATION,
        description="Current pipeline stage being executed",
    )

    def to_acl_payload(self) -> AccessControlPayload:
        """Derive AccessControlPayload from context for chunking and vector indexing."""
        return AccessControlPayload(
            tenant_id=self.tenant_id,
            roles=self.roles,
            departments=self.departments,
            users=self.allowed_users,
        )

"""Ingestion orchestration and pipeline coordinator contracts."""

import abc
from pathlib import Path
from typing import Any, BinaryIO
from pydantic import BaseModel, ConfigDict, Field

from backend.ingestion.types.context import IngestionContext
from backend.ingestion.types.enums import DocumentIngestionStatus, PipelineStage
from backend.ingestion.types.metadata import DocumentMetadata


class IngestionPipelineResult(BaseModel):
    """End-to-end outcome report of a document ingestion pipeline execution."""

    model_config = ConfigDict(extra="allow")

    document_id: str = Field(..., description="Ingested document UUID")
    tenant_id: str = Field(..., description="Target tenant UUID")
    status: DocumentIngestionStatus = Field(..., description="Final processing state")
    final_stage: PipelineStage = Field(..., description="Last completed pipeline stage")
    chunks_created: int = Field(default=0, ge=0, description="Number of chunks generated")
    points_indexed: int = Field(default=0, ge=0, description="Number of vector points indexed")
    job_id: str | None = Field(default=None, description="Ingestion job UUID for observability tracking")
    duration_seconds: float = Field(default=0.0, ge=0.0, description="Total pipeline execution runtime")
    metadata: DocumentMetadata | None = Field(default=None, description="Persisted document metadata")
    warnings: list[str] = Field(default_factory=list, description="Aggregated non-fatal warnings")
    error_message: str | None = Field(default=None, description="Failure diagnostic message if FAILED")
    metrics: dict[str, Any] | None = Field(default=None, description="Granular stage and throughput telemetry")


class IngestionOrchestrator(abc.ABC):
    """Abstract contract for coordinating the full multi-stage ingestion workflow.

    Enforces zero-trust isolation and orchestrates:
    Validation -> Extraction -> Chunking -> Deduplication -> Embedding -> Indexing -> Relational Persistence
    """

    @abc.abstractmethod
    async def process_document(
        self,
        content: bytes | BinaryIO | Path,
        filename: str,
        context: IngestionContext,
    ) -> IngestionPipelineResult:
        """Execute end-to-end document ingestion workflow."""
        raise NotImplementedError

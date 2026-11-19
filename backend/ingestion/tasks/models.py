"""Data contracts, stage definitions, and result reporting for Celery background ingestion tasks."""

from __future__ import annotations

import enum
from typing import Any
from pydantic import BaseModel, ConfigDict, Field


class TaskStage(str, enum.Enum):
    """Pipeline stages executed by asynchronous Celery workers."""

    EXTRACTION = "EXTRACTION"
    CHUNKING = "CHUNKING"
    EMBEDDING = "EMBEDDING"
    INDEXING = "INDEXING"
    PIPELINE = "PIPELINE"


class TaskExecutionStatus(str, enum.Enum):
    """Lifecycle status of an asynchronous background task execution."""

    PENDING = "PENDING"
    STARTED = "STARTED"
    SUCCESS = "SUCCESS"
    RETRY = "RETRY"
    FAILURE = "FAILURE"
    SKIPPED = "SKIPPED"


class IngestionJobPayload(BaseModel):
    """Metadata payload passed through Celery broker to reference document tasks.

    Adheres strictly to Requirement 4: passes lightweight document and job identifiers
    rather than massive binary streams or raw text content through the broker.
    """

    model_config = ConfigDict(frozen=True)

    job_id: str = Field(..., description="Unique ingestion job UUID")
    document_id: str = Field(..., description="Target document UUID in PostgreSQL")
    tenant_id: str = Field(..., description="Authenticated tenant UUID for boundary isolation")
    storage_path: str = Field(..., description="Relative physical storage path or object key")
    filename: str = Field(..., description="Original filename with extension")
    mime_type: str | None = Field(default=None, description="Declared or detected MIME type")
    document_version_id: str | None = Field(default=None, description="Document version UUID if versioned")
    options: dict[str, Any] = Field(default_factory=dict, description="Custom pipeline execution options")


class TaskStageResult(BaseModel):
    """Structured report returned upon completion of a background task stage."""

    model_config = ConfigDict(extra="allow")

    job_id: str = Field(..., description="Ingestion job identifier")
    document_id: str = Field(..., description="Target document identifier")
    tenant_id: str = Field(..., description="Owning tenant identifier")
    stage: TaskStage = Field(..., description="Pipeline stage executed")
    status: TaskExecutionStatus = Field(..., description="Final stage outcome status")
    items_count: int = Field(default=0, ge=0, description="Number of elements/chunks/vectors produced")
    duration_seconds: float = Field(default=0.0, ge=0.0, description="Execution duration in seconds")
    retry_count: int = Field(default=0, ge=0, description="Number of retries attempted")
    output_summary: dict[str, Any] = Field(default_factory=dict, description="Stage output summary metadata")
    error: str | None = Field(default=None, description="Diagnostic error message if failed")


class TaskFailureReport(BaseModel):
    """Actionable failure diagnostic report for task failure reporting (Requirement 10)."""

    model_config = ConfigDict(frozen=True)

    job_id: str = Field(..., description="Ingestion job identifier")
    document_id: str = Field(..., description="Target document identifier")
    tenant_id: str = Field(..., description="Owning tenant identifier")
    stage: TaskStage = Field(..., description="Stage where failure occurred")
    error_type: str = Field(..., description="Python exception class name")
    error_message: str = Field(..., description="Descriptive error explanation")
    retry_count: int = Field(default=0, description="Total retry attempts executed")
    is_terminal: bool = Field(default=True, description="Whether the failure is non-retryable")
    details: dict[str, Any] = Field(default_factory=dict, description="Additional context and telemetry")

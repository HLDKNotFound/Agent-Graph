"""Ingestion state machine and stage execution tracker.

Guarantees:
- Formal, verifiable state machine tracking DocumentIngestionStatus and PipelineStage.
- Monotonic or restartable state transitions preventing illegal transitions.
- Granular progress reporting (0% to 100%) and stage execution timing.
- Immutable stage execution history with error diagnostics and recovery metadata.
"""

from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
import logging
from typing import Any
from pydantic import BaseModel, ConfigDict, Field

from backend.ingestion.exceptions import OrchestrationError
from backend.ingestion.types.enums import DocumentIngestionStatus, PipelineStage

logger = logging.getLogger(__name__)


class StageStatus(str, Enum):
    """Discrete status of an individual pipeline stage execution."""

    PENDING = "PENDING"
    IN_PROGRESS = "IN_PROGRESS"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    SKIPPED = "SKIPPED"


class InvalidStateTransitionError(OrchestrationError):
    """Raised when an illegal status or stage transition is attempted."""

    def __init__(
        self,
        current_status: DocumentIngestionStatus,
        target_status: DocumentIngestionStatus,
        stage: PipelineStage | None = None,
        reason: str | None = None,
    ) -> None:
        msg = f"Invalid ingestion state transition from {current_status} to {target_status}"
        if stage:
            msg += f" during stage {stage}"
        if reason:
            msg += f": {reason}"
        super().__init__(msg, details={
            "current_status": str(current_status),
            "target_status": str(target_status),
            "stage": str(stage) if stage else None,
            "reason": reason,
        })
        self.current_status = current_status
        self.target_status = target_status
        self.stage = stage


class StageExecutionRecord(BaseModel):
    """Execution telemetry record for an individual pipeline stage."""

    model_config = ConfigDict(frozen=True)

    stage: PipelineStage = Field(..., description="Pipeline processing stage")
    status: StageStatus = Field(default=StageStatus.PENDING, description="Execution status of this stage")
    started_at: datetime = Field(..., description="Stage initiation timestamp (UTC)")
    completed_at: datetime | None = Field(default=None, description="Stage termination timestamp (UTC)")
    duration_seconds: float = Field(default=0.0, ge=0.0, description="Stage execution runtime in seconds")
    items_processed: int = Field(default=0, ge=0, description="Count of items processed (elements, chunks, points)")
    message: str | None = Field(default=None, description="Informational stage output or skip reason")
    error: str | None = Field(default=None, description="Diagnostic error message if FAILED")


class IngestionStateMachine:
    """Manages document ingestion lifecycle, enforcing deterministic state transitions.

    Supported DocumentIngestionStatus states:
    - PENDING: Document record created, waiting for execution.
    - PROCESSING: Pipeline is actively executing across stages.
    - COMPLETED: Document, chunks, embeddings, and Qdrant points are consistent.
    - FAILED: Pipeline failed permanently or exhausted retries; compensation executed.
    - CANCELLED: Processing was aborted by administrative or user request.
    """

    # Stage to standard progress percentage mapping
    STAGE_PROGRESS_MAP: dict[PipelineStage, int] = {
        PipelineStage.VALIDATION: 10,
        PipelineStage.DEDUPLICATION: 20,
        PipelineStage.EXTRACTION: 35,
        PipelineStage.CHUNKING: 50,
        PipelineStage.EMBEDDING: 75,
        PipelineStage.PERSISTENCE: 85,
        PipelineStage.INDEXING: 95,
    }

    # Permitted top-level lifecycle transitions
    VALID_STATUS_TRANSITIONS: dict[DocumentIngestionStatus, set[DocumentIngestionStatus]] = {
        DocumentIngestionStatus.PENDING: {
            DocumentIngestionStatus.PROCESSING,
            DocumentIngestionStatus.CANCELLED,
            DocumentIngestionStatus.FAILED,
        },
        DocumentIngestionStatus.PROCESSING: {
            DocumentIngestionStatus.PROCESSING,
            DocumentIngestionStatus.COMPLETED,
            DocumentIngestionStatus.FAILED,
            DocumentIngestionStatus.CANCELLED,
        },
        DocumentIngestionStatus.COMPLETED: set(),  # Terminal unless reset
        DocumentIngestionStatus.FAILED: {
            DocumentIngestionStatus.PROCESSING,   # Safe boundary retry
            DocumentIngestionStatus.CANCELLED,
        },
        DocumentIngestionStatus.CANCELLED: {
            DocumentIngestionStatus.PROCESSING,   # Re-trigger
        },
    }

    def __init__(
        self,
        document_id: str,
        tenant_id: str,
        initial_status: DocumentIngestionStatus = DocumentIngestionStatus.PENDING,
    ) -> None:
        self.document_id = document_id
        self.tenant_id = tenant_id
        self._status = initial_status
        self._current_stage: PipelineStage | None = None
        self._stage_history: list[StageExecutionRecord] = []
        self._progress_percent: int = 0
        self._started_at: datetime = datetime.now(timezone.utc)
        self._updated_at: datetime = self._started_at
        self._current_stage_start: datetime | None = None
        self._warnings: list[str] = []
        self._last_error: str | None = None

    @property
    def status(self) -> DocumentIngestionStatus:
        return self._status

    @property
    def current_stage(self) -> PipelineStage | None:
        return self._current_stage

    @property
    def progress_percent(self) -> int:
        return self._progress_percent

    @property
    def stage_history(self) -> list[StageExecutionRecord]:
        return list(self._stage_history)

    @property
    def warnings(self) -> list[str]:
        return list(self._warnings)

    @property
    def last_error(self) -> str | None:
        return self._last_error

    def add_warning(self, warning: str) -> None:
        """Record non-fatal pipeline warning."""
        self._warnings.append(warning)
        logger.warning(
            "[doc=%s tenant=%s] Pipeline warning: %s",
            self.document_id,
            self.tenant_id,
            warning,
        )

    def start_processing(self) -> None:
        """Transition from PENDING or FAILED to PROCESSING."""
        self._validate_transition(DocumentIngestionStatus.PROCESSING)
        self._status = DocumentIngestionStatus.PROCESSING
        self._updated_at = datetime.now(timezone.utc)
        logger.info("[doc=%s tenant=%s] Ingestion status -> PROCESSING", self.document_id, self.tenant_id)

    def enter_stage(self, stage: PipelineStage, message: str | None = None) -> None:
        """Enter a new pipeline execution stage."""
        if self._status != DocumentIngestionStatus.PROCESSING:
            self.start_processing()

        now = datetime.now(timezone.utc)
        self._current_stage = stage
        self._current_stage_start = now
        self._progress_percent = self.STAGE_PROGRESS_MAP.get(stage, self._progress_percent)
        self._updated_at = now

        logger.info(
            "[doc=%s tenant=%s stage=%s] Starting stage (%d%%)%s",
            self.document_id,
            self.tenant_id,
            stage.value,
            self._progress_percent,
            f": {message}" if message else "",
        )

    def complete_stage(
        self,
        stage: PipelineStage,
        items_processed: int = 0,
        message: str | None = None,
    ) -> StageExecutionRecord:
        """Mark a pipeline stage as successfully completed."""
        now = datetime.now(timezone.utc)
        started_at = self._current_stage_start or now
        duration = max(0.0, (now - started_at).total_seconds())

        record = StageExecutionRecord(
            stage=stage,
            status=StageStatus.COMPLETED,
            started_at=started_at,
            completed_at=now,
            duration_seconds=round(duration, 4),
            items_processed=items_processed,
            message=message,
        )
        self._stage_history.append(record)
        self._updated_at = now

        logger.info(
            "[doc=%s tenant=%s stage=%s] Completed stage in %.3fs (items=%d)%s",
            self.document_id,
            self.tenant_id,
            stage.value,
            duration,
            items_processed,
            f": {message}" if message else "",
        )
        return record

    def skip_stage(self, stage: PipelineStage, reason: str) -> StageExecutionRecord:
        """Record that a stage was safely bypassed."""
        now = datetime.now(timezone.utc)
        record = StageExecutionRecord(
            stage=stage,
            status=StageStatus.SKIPPED,
            started_at=now,
            completed_at=now,
            duration_seconds=0.0,
            items_processed=0,
            message=reason,
        )
        self._stage_history.append(record)
        self._updated_at = now
        logger.info("[doc=%s tenant=%s stage=%s] Skipped stage: %s", self.document_id, self.tenant_id, stage.value, reason)
        return record

    def fail_stage(
        self,
        stage: PipelineStage,
        error: Exception | str,
        mark_document_failed: bool = True,
    ) -> StageExecutionRecord:
        """Record a failure in the current stage and optionally mark document FAILED."""
        now = datetime.now(timezone.utc)
        started_at = self._current_stage_start or now
        duration = max(0.0, (now - started_at).total_seconds())
        err_msg = str(error)
        self._last_error = err_msg

        record = StageExecutionRecord(
            stage=stage,
            status=StageStatus.FAILED,
            started_at=started_at,
            completed_at=now,
            duration_seconds=round(duration, 4),
            items_processed=0,
            error=err_msg,
        )
        self._stage_history.append(record)
        self._updated_at = now

        if mark_document_failed:
            self._validate_transition(DocumentIngestionStatus.FAILED)
            self._status = DocumentIngestionStatus.FAILED

        logger.error(
            "[doc=%s tenant=%s stage=%s] Stage FAILED after %.3fs: %s",
            self.document_id,
            self.tenant_id,
            stage.value,
            duration,
            err_msg,
        )
        return record

    def complete_pipeline(self, message: str | None = None) -> None:
        """Transition pipeline to terminal COMPLETED state."""
        self._validate_transition(DocumentIngestionStatus.COMPLETED)
        self._status = DocumentIngestionStatus.COMPLETED
        self._progress_percent = 100
        self._updated_at = datetime.now(timezone.utc)
        logger.info(
            "[doc=%s tenant=%s] Ingestion pipeline successfully COMPLETED (100%%)%s",
            self.document_id,
            self.tenant_id,
            f": {message}" if message else "",
        )

    def cancel_pipeline(self, reason: str) -> None:
        """Cancel pipeline execution."""
        self._validate_transition(DocumentIngestionStatus.CANCELLED)
        self._status = DocumentIngestionStatus.CANCELLED
        self._updated_at = datetime.now(timezone.utc)
        logger.warning("[doc=%s tenant=%s] Ingestion CANCELLED: %s", self.document_id, self.tenant_id, reason)

    def reset_for_retry(self) -> None:
        """Reset state machine from FAILED/CANCELLED to allow resuming."""
        if self._status not in (DocumentIngestionStatus.FAILED, DocumentIngestionStatus.CANCELLED):
            raise InvalidStateTransitionError(
                self._status,
                DocumentIngestionStatus.PROCESSING,
                reason="Cannot reset a document that is not FAILED or CANCELLED",
            )
        self._status = DocumentIngestionStatus.PROCESSING
        self._updated_at = datetime.now(timezone.utc)
        self._last_error = None
        logger.info("[doc=%s tenant=%s] Ingestion reset for retry -> PROCESSING", self.document_id, self.tenant_id)

    def _validate_transition(self, target: DocumentIngestionStatus) -> None:
        """Ensure transition is permitted by lifecycle rules."""
        if target == self._status:
            return  # Idempotent self-transition allowed
        allowed = self.VALID_STATUS_TRANSITIONS.get(self._status, set())
        if target not in allowed:
            raise InvalidStateTransitionError(
                current_status=self._status,
                target_status=target,
                stage=self._current_stage,
                reason=f"Transition from {self._status} to {target} is not allowed",
            )

    def to_metadata_dict(self) -> dict[str, Any]:
        """Serialize state machine telemetry into document metadata format."""
        return {
            "ingestion_status": self._status.value,
            "current_stage": self._current_stage.value if self._current_stage else None,
            "progress_percent": self._progress_percent,
            "updated_at": self._updated_at.isoformat(),
            "started_at": self._started_at.isoformat(),
            "last_error": self._last_error,
            "warnings": self._warnings,
            "stage_history": [r.model_dump(mode="json") for r in self._stage_history],
        }

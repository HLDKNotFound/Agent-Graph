"""Asynchronous Celery task processing subsystem for document ingestion."""

from backend.ingestion.tasks.celery_app import celery_app, create_celery_app
from backend.ingestion.tasks.document_tasks import (
    chunk_document_task,
    embed_chunks_task,
    extract_document_task,
    index_vectors_task,
    process_document_pipeline_task,
)
from backend.ingestion.tasks.lock import (
    InMemoryLockStore,
    JobIdempotencyGuard,
    get_idempotency_guard,
)
from backend.ingestion.tasks.logging import (
    TaskLoggerAdapter,
    get_task_logger,
)
from backend.ingestion.tasks.models import (
    IngestionJobPayload,
    TaskExecutionStatus,
    TaskFailureReport,
    TaskStage,
    TaskStageResult,
)

__all__ = [
    "celery_app",
    "create_celery_app",
    "extract_document_task",
    "chunk_document_task",
    "embed_chunks_task",
    "index_vectors_task",
    "process_document_pipeline_task",
    "JobIdempotencyGuard",
    "InMemoryLockStore",
    "get_idempotency_guard",
    "TaskLoggerAdapter",
    "get_task_logger",
    "IngestionJobPayload",
    "TaskStage",
    "TaskExecutionStatus",
    "TaskStageResult",
    "TaskFailureReport",
]

"""Structured logging adapters and formatters for Celery worker tasks."""

from __future__ import annotations

import logging
from typing import Any


class TaskLoggerAdapter(logging.LoggerAdapter):
    """LoggerAdapter providing structured document and job contextual attributes.

    Satisfies Requirement 9: Worker logs identify the document and job,
    outputting consistent structured log formats across all pipeline stages.
    """

    def __init__(
        self,
        logger: logging.Logger,
        job_id: str = "",
        document_id: str = "",
        tenant_id: str = "",
        stage: str = "",
        retry_count: int = 0,
    ) -> None:
        extra: dict[str, Any] = {
            "job_id": job_id,
            "document_id": document_id,
            "tenant_id": tenant_id,
            "stage": stage,
            "retry_count": retry_count,
        }
        super().__init__(logger, extra)

    def process(self, msg: str, kwargs: Any) -> tuple[str, Any]:
        job_id = self.extra.get("job_id", "")
        doc_id = self.extra.get("document_id", "")
        tenant_id = self.extra.get("tenant_id", "")
        stage = self.extra.get("stage", "")
        retry = self.extra.get("retry_count", 0)

        prefix_parts = []
        if job_id:
            prefix_parts.append(f"job={job_id}")
        if doc_id:
            prefix_parts.append(f"doc={doc_id}")
        if tenant_id:
            prefix_parts.append(f"tenant_id={tenant_id}")
        if stage:
            prefix_parts.append(f"stage={stage}")
        if retry > 0:
            prefix_parts.append(f"retry={retry}")

        prefix = f"[{' '.join(prefix_parts)}] " if prefix_parts else ""
        return f"{prefix}{msg}", kwargs


def get_task_logger(
    name: str = "backend.ingestion.tasks",
    job_id: str = "",
    document_id: str = "",
    tenant_id: str = "",
    stage: str = "",
    retry_count: int = 0,
) -> TaskLoggerAdapter:
    """Provide a structured TaskLoggerAdapter bound to specific task coordinates."""
    base_logger = logging.getLogger(name)
    return TaskLoggerAdapter(
        logger=base_logger,
        job_id=job_id,
        document_id=document_id,
        tenant_id=tenant_id,
        stage=stage,
        retry_count=retry_count,
    )

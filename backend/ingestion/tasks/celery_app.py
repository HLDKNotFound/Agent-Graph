"""Celery application factory and configuration for document ingestion workers."""

from __future__ import annotations

import logging
from celery import Celery

from backend.ingestion.config import IngestionSettings, get_ingestion_settings

logger = logging.getLogger(__name__)


def create_celery_app(settings: IngestionSettings | None = None) -> Celery:
    """Instantiate and configure Celery application for enterprise document ingestion.

    Applies production settings satisfying:
    - Requirement 1: Redis message broker integration.
    - Requirement 2: Worker initialization and application configuration.
    - Requirement 6: Time limits, concurrency, late acks, and prefetch behavior.
    """
    settings = settings or get_ingestion_settings()

    app = Celery(
        "agentgraph_ingestion",
        broker=settings.celery_broker_url,
        backend=settings.celery_result_backend,
    )

    # Configure Celery operational behavior
    app.conf.update(
        # Serialization & Security
        task_serializer="json",
        result_serializer="json",
        accept_content=["json"],
        timezone="UTC",
        enable_utc=True,
        # Queuing & Routing
        task_default_queue=settings.celery_task_default_queue,
        task_routes={
            "backend.ingestion.tasks.*": {"queue": settings.celery_task_default_queue},
        },
        # Worker Concurrency & Prefetch
        worker_concurrency=settings.celery_worker_concurrency,
        worker_prefetch_multiplier=settings.celery_worker_prefetch_multiplier,
        # Acknowledgement & Reliability (Requirement 6)
        task_acks_late=settings.celery_task_acks_late,
        task_reject_on_worker_lost=True,
        # Timeouts & Safety Guardrails (Requirement 6)
        task_time_limit=settings.celery_task_time_limit,
        task_soft_time_limit=settings.celery_task_soft_time_limit,
        # Testing & Local Dev Support (Requirement 13)
        task_always_eager=settings.celery_task_always_eager,
        task_eager_propagates=True,
        # Result Storage
        result_expires=86400,  # Retain task results for 24 hours
    )

    return app


# Module-level Celery application singleton
celery_app: Celery = create_celery_app()

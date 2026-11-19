"""Standalone entrypoint script for launching Celery background workers.

Usage:
    # Option 1: Via Python runner directly
    python -m backend.worker

    # Option 2: Via Celery CLI directly
    celery -A backend.worker.celery_app worker --loglevel=INFO -Q document_ingestion -c 4
"""

import sys
from backend.ingestion.tasks import (
    celery_app,
    chunk_document_task,
    embed_chunks_task,
    extract_document_task,
    index_vectors_task,
    process_document_pipeline_task,
)

__all__ = [
    "celery_app",
    "extract_document_task",
    "chunk_document_task",
    "embed_chunks_task",
    "index_vectors_task",
    "process_document_pipeline_task",
]

if __name__ == "__main__":
    args = sys.argv[1:] if len(sys.argv) > 1 else [
        "worker",
        "--loglevel=INFO",
        "-Q", "document_ingestion",
        "-c", "4",
    ]
    celery_app.start(argv=args)

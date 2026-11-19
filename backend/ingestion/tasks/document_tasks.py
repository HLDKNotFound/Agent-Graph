"""Celery worker tasks defining asynchronous document ingestion stage boundaries.

Implements thin, idempotent background tasks delegating business logic to
domain services:
- Task 1: extract_document_task (Document extraction)
- Task 2: chunk_document_task (Chunk generation)
- Task 3: embed_chunks_task (Embedding generation via BGE-M3)
- Task 4: index_vectors_task (Vector indexing via Qdrant)
- Pipeline: process_document_pipeline_task (Coordinated end-to-end workflow)
"""

from __future__ import annotations

import asyncio
import io
import time
from typing import Any
from celery.exceptions import SoftTimeLimitExceeded

from backend.ingestion.chunking import (
    ChunkingEngine,
    default_chunking_engine,
    get_chunking_strategy,
)
from backend.ingestion.types.enums import ChunkingStrategyType
from backend.ingestion.embedding import (
    EmbeddingResult,
    get_embedding_service,
)
from backend.ingestion.exceptions import (
    ChunkingError,
    EmbeddingError,
    ExtractionError,
    IndexingError,
    IngestionError,
    StorageFileNotFoundError,
    TenantStorageViolationError,
    ValidationError,
)
from backend.ingestion.extraction import default_extractor_registry
from backend.ingestion.indexing import (
    QdrantVectorIndexService,
    get_qdrant_index_service,
)
from backend.ingestion.storage import get_storage_service
from backend.ingestion.tasks.celery_app import celery_app
from backend.ingestion.tasks.lock import get_idempotency_guard
from backend.ingestion.tasks.logging import get_task_logger
from backend.ingestion.tasks.models import (
    TaskExecutionStatus,
    TaskFailureReport,
    TaskStage,
    TaskStageResult,
)
from backend.ingestion.types.chunking import ChunkingConfig, DocumentChunkInput
from backend.ingestion.types.context import IngestionContext, IngestionOptions
from backend.ingestion.types.elements import (
    BaseExtractedElement,
    ExtractedElement,
    SpreadsheetRowElement,
    TableElement,
)
from backend.ingestion.types.enums import DocumentType
from backend.ingestion.types.extraction import ExtractionResult
from backend.ingestion.types.metadata import DocumentMetadata


def _compute_backoff(retry_count: int, factor: int = 2, max_delay: int = 60) -> int:
    """Compute exponential backoff delay with jitter cap."""
    return min(max_delay, factor ** (retry_count + 1))


# ---------------------------------------------------------------------------
# Stage 1: Document Extraction Task
# ---------------------------------------------------------------------------

@celery_app.task(
    bind=True,
    name="backend.ingestion.tasks.extract_document_task",
    max_retries=3,
    default_retry_delay=2,
    acks_late=True,
)
def extract_document_task(
    self,
    job_id: str,
    document_id: str,
    tenant_id: str,
    storage_path: str,
    filename: str,
    mime_type: str | None = None,
    options: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Execute document content and structure extraction in Celery worker.

    Satisfies:
    - Requirement 4: passes lightweight identifiers, not file contents.
    - Requirement 5: loads document data from configured storage layer inside worker.
    - Requirement 11: respects document tenant ownership.
    """
    start_time = time.time()
    t_logger = get_task_logger(
        job_id=job_id,
        document_id=document_id,
        tenant_id=tenant_id,
        stage=TaskStage.EXTRACTION.value,
        retry_count=self.request.retries,
    )
    t_logger.info("Starting document extraction for file '%s' (storage_path='%s')", filename, storage_path)

    context = IngestionContext(
        tenant_id=tenant_id,
        document_id=document_id,
        options=IngestionOptions(**(options or {})),
    )

    try:
        # Load raw file stream from storage backend (Requirement 5)
        storage_service = get_storage_service()
        extractor = default_extractor_registry.get_extractor_for_file(filename, mime_type)
        t_logger.info("Using extractor '%s' for '%s'", type(extractor).__name__, filename)

        # Execute extraction
        async def _run_extraction():
            file_bytes = await storage_service.get_file(storage_path, context)
            stream = io.BytesIO(file_bytes)
            return await extractor.extract(stream, context)

        extraction_result = asyncio.run(_run_extraction())

        elements_data = [el.model_dump() for el in extraction_result.elements]
        duration = time.time() - start_time
        t_logger.info("Extraction completed: extracted %d elements in %.2fs", len(elements_data), duration)

        return TaskStageResult(
            job_id=job_id,
            document_id=document_id,
            tenant_id=tenant_id,
            stage=TaskStage.EXTRACTION,
            status=TaskExecutionStatus.SUCCESS,
            items_count=len(elements_data),
            duration_seconds=duration,
            retry_count=self.request.retries,
            output_summary={
                "elements": elements_data,
                "stats": extraction_result.stats.model_dump() if extraction_result.stats else None,
                "document_type": extraction_result.document_type.value,
                "mime_type": extraction_result.mime_type,
            },
        ).model_dump()

    except SoftTimeLimitExceeded as exc:
        t_logger.error("Extraction task timed out (soft time limit exceeded): %s", exc)
        raise

    except (StorageFileNotFoundError, ValidationError, TenantStorageViolationError) as exc:
        # Non-retryable terminal errors
        t_logger.error("Terminal extraction error (non-retryable): %s", exc)
        return TaskStageResult(
            job_id=job_id,
            document_id=document_id,
            tenant_id=tenant_id,
            stage=TaskStage.EXTRACTION,
            status=TaskExecutionStatus.FAILURE,
            duration_seconds=time.time() - start_time,
            retry_count=self.request.retries,
            error=str(exc),
        ).model_dump()

    except Exception as exc:
        t_logger.error("Transient extraction failure: %s", exc, exc_info=True)
        if self.request.retries < self.max_retries:
            delay = _compute_backoff(self.request.retries)
            t_logger.warning("Retrying extraction task in %ds (attempt %d)", delay, self.request.retries + 1)
            raise self.retry(exc=exc, countdown=delay)

        return TaskStageResult(
            job_id=job_id,
            document_id=document_id,
            tenant_id=tenant_id,
            stage=TaskStage.EXTRACTION,
            status=TaskExecutionStatus.FAILURE,
            duration_seconds=time.time() - start_time,
            retry_count=self.request.retries,
            error=str(exc),
        ).model_dump()


# ---------------------------------------------------------------------------
# Stage 2: Chunk Generation Task
# ---------------------------------------------------------------------------

@celery_app.task(
    bind=True,
    name="backend.ingestion.tasks.chunk_document_task",
    max_retries=3,
    default_retry_delay=2,
    acks_late=True,
)
def chunk_document_task(
    self,
    job_id: str,
    document_id: str,
    tenant_id: str,
    elements_data: list[dict[str, Any]],
    chunking_config: dict[str, Any] | None = None,
    options: dict[str, Any] | None = None,
    document_type: str | None = None,
    mime_type: str | None = None,
) -> dict[str, Any]:
    """Execute document structural chunking in Celery worker."""
    start_time = time.time()
    t_logger = get_task_logger(
        job_id=job_id,
        document_id=document_id,
        tenant_id=tenant_id,
        stage=TaskStage.CHUNKING.value,
        retry_count=self.request.retries,
    )
    t_logger.info("Starting chunking for %d extracted elements", len(elements_data))

    context = IngestionContext(
        tenant_id=tenant_id,
        document_id=document_id,
        options=IngestionOptions(**(options or {})),
    )

    try:
        # Reconstruct typed ExtractedElement elements
        elements = [BaseExtractedElement.from_dict(ed) for ed in elements_data]

        doc_type_val = DocumentType(document_type) if document_type else DocumentType.TXT
        if any(isinstance(el, (TableElement, SpreadsheetRowElement)) for el in elements):
            doc_type_val = DocumentType.CSV

        extraction_result = ExtractionResult(
            document_id=document_id,
            document_type=doc_type_val,
            mime_type=mime_type or "text/plain",
            elements=elements,
            metadata=DocumentMetadata(
                document_id=document_id,
                tenant_id=tenant_id,
                filename=f"{document_id}.txt",
                file_hash_sha256="0" * 64,
                file_size_bytes=sum(
                    len(getattr(e, "text", "") or getattr(e, "content", "") or "") for e in elements
                ),
                mime_type=mime_type or "text/plain",
                document_type=doc_type_val,
            ),
        )

        cfg = ChunkingConfig(**(chunking_config or {})) if chunking_config else None
        engine = ChunkingEngine()

        chunks = asyncio.run(engine.chunk_document(extraction_result, context, config=cfg))
        chunks_data = [chk.model_dump() for chk in chunks]

        duration = time.time() - start_time
        t_logger.info("Chunking completed: generated %d chunks in %.2fs", len(chunks_data), duration)

        return TaskStageResult(
            job_id=job_id,
            document_id=document_id,
            tenant_id=tenant_id,
            stage=TaskStage.CHUNKING,
            status=TaskExecutionStatus.SUCCESS,
            items_count=len(chunks_data),
            duration_seconds=duration,
            retry_count=self.request.retries,
            output_summary={"chunks": chunks_data},
        ).model_dump()

    except SoftTimeLimitExceeded as exc:
        t_logger.error("Chunking task timed out (soft time limit exceeded): %s", exc)
        raise

    except Exception as exc:
        t_logger.error("Chunking task failed: %s", exc, exc_info=True)
        if self.request.retries < self.max_retries:
            delay = _compute_backoff(self.request.retries)
            raise self.retry(exc=exc, countdown=delay)

        return TaskStageResult(
            job_id=job_id,
            document_id=document_id,
            tenant_id=tenant_id,
            stage=TaskStage.CHUNKING,
            status=TaskExecutionStatus.FAILURE,
            duration_seconds=time.time() - start_time,
            retry_count=self.request.retries,
            error=str(exc),
        ).model_dump()


# ---------------------------------------------------------------------------
# Stage 3: Embedding Generation Task
# ---------------------------------------------------------------------------

@celery_app.task(
    bind=True,
    name="backend.ingestion.tasks.embed_chunks_task",
    max_retries=3,
    default_retry_delay=2,
    acks_late=True,
)
def embed_chunks_task(
    self,
    job_id: str,
    document_id: str,
    tenant_id: str,
    chunks_data: list[dict[str, Any]],
    options: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Generate dense BGE-M3 vector embeddings for chunks in Celery worker."""
    start_time = time.time()
    t_logger = get_task_logger(
        job_id=job_id,
        document_id=document_id,
        tenant_id=tenant_id,
        stage=TaskStage.EMBEDDING.value,
        retry_count=self.request.retries,
    )
    t_logger.info("Starting embedding generation for %d chunks", len(chunks_data))

    context = IngestionContext(
        tenant_id=tenant_id,
        document_id=document_id,
        options=IngestionOptions(**(options or {})),
    )

    try:
        chunks = [DocumentChunkInput.model_validate(cd) for cd in chunks_data]
        embed_service = get_embedding_service()

        # Run async embedding generation
        embeddings = asyncio.run(embed_service.embed_chunks(chunks, context))
        embeddings_data = [emb.model_dump() for emb in embeddings]

        duration = time.time() - start_time
        t_logger.info("Embedding completed: generated %d 1024-dim vectors in %.2fs", len(embeddings_data), duration)

        return TaskStageResult(
            job_id=job_id,
            document_id=document_id,
            tenant_id=tenant_id,
            stage=TaskStage.EMBEDDING,
            status=TaskExecutionStatus.SUCCESS,
            items_count=len(embeddings_data),
            duration_seconds=duration,
            retry_count=self.request.retries,
            output_summary={"embeddings": embeddings_data},
        ).model_dump()

    except Exception as exc:
        t_logger.error("Embedding generation failed: %s", exc, exc_info=True)
        if self.request.retries < self.max_retries:
            delay = _compute_backoff(self.request.retries)
            raise self.retry(exc=exc, countdown=delay)

        return TaskStageResult(
            job_id=job_id,
            document_id=document_id,
            tenant_id=tenant_id,
            stage=TaskStage.EMBEDDING,
            status=TaskExecutionStatus.FAILURE,
            duration_seconds=time.time() - start_time,
            retry_count=self.request.retries,
            error=str(exc),
        ).model_dump()


# ---------------------------------------------------------------------------
# Stage 4: Vector Indexing Task
# ---------------------------------------------------------------------------

@celery_app.task(
    bind=True,
    name="backend.ingestion.tasks.index_vectors_task",
    max_retries=3,
    default_retry_delay=2,
    acks_late=True,
)
def index_vectors_task(
    self,
    job_id: str,
    document_id: str,
    tenant_id: str,
    chunks_data: list[dict[str, Any]],
    embeddings_data: list[dict[str, Any]],
    options: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Persist dense embeddings and searchable payloads in Qdrant in Celery worker."""
    start_time = time.time()
    t_logger = get_task_logger(
        job_id=job_id,
        document_id=document_id,
        tenant_id=tenant_id,
        stage=TaskStage.INDEXING.value,
        retry_count=self.request.retries,
    )
    t_logger.info("Starting Qdrant vector indexing for %d points", len(chunks_data))

    context = IngestionContext(
        tenant_id=tenant_id,
        document_id=document_id,
        options=IngestionOptions(**(options or {})),
    )

    try:
        chunks = [DocumentChunkInput.model_validate(cd) for cd in chunks_data]
        embeddings = [EmbeddingResult.model_validate(ed) for ed in embeddings_data]

        qdrant_service = get_qdrant_index_service()
        index_result = asyncio.run(qdrant_service.index_chunks(chunks, embeddings, context))

        duration = time.time() - start_time
        t_logger.info("Indexing completed: upserted %d points into '%s' in %.2fs", index_result.points_indexed, index_result.collection_name, duration)

        return TaskStageResult(
            job_id=job_id,
            document_id=document_id,
            tenant_id=tenant_id,
            stage=TaskStage.INDEXING,
            status=TaskExecutionStatus.SUCCESS,
            items_count=index_result.points_indexed,
            duration_seconds=duration,
            retry_count=self.request.retries,
            output_summary={
                "collection_name": index_result.collection_name,
                "vector_storage_ids": index_result.vector_storage_ids,
            },
        ).model_dump()

    except Exception as exc:
        t_logger.error("Vector indexing failed: %s", exc, exc_info=True)
        if self.request.retries < self.max_retries:
            delay = _compute_backoff(self.request.retries)
            raise self.retry(exc=exc, countdown=delay)

        return TaskStageResult(
            job_id=job_id,
            document_id=document_id,
            tenant_id=tenant_id,
            stage=TaskStage.INDEXING,
            status=TaskExecutionStatus.FAILURE,
            duration_seconds=time.time() - start_time,
            retry_count=self.request.retries,
            error=str(exc),
        ).model_dump()


# ---------------------------------------------------------------------------
# Coordinated End-to-End Pipeline Task
# ---------------------------------------------------------------------------

@celery_app.task(
    bind=True,
    name="backend.ingestion.tasks.process_document_pipeline_task",
    max_retries=3,
    default_retry_delay=2,
    acks_late=True,
)
def process_document_pipeline_task(
    self,
    job_id: str,
    document_id: str,
    tenant_id: str,
    storage_path: str,
    filename: str,
    mime_type: str | None = None,
    options: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Execute end-to-end document ingestion pipeline with idempotency and retry safety.

    Pipeline Flow:
    1. Idempotency Check: Avoid duplicate processing if job is already running or completed.
    2. Extraction: Extract structured elements from document storage.
    3. Chunking: Structure-aware chunk segmentation.
    4. Embedding: BGE-M3 1024-dim dense representation.
    5. Vector Indexing: Qdrant zero-trust vector upsert.
    6. Lock Release & Completion Marking.
    """
    start_time = time.time()
    t_logger = get_task_logger(
        job_id=job_id,
        document_id=document_id,
        tenant_id=tenant_id,
        stage=TaskStage.PIPELINE.value,
        retry_count=self.request.retries,
    )
    guard = get_idempotency_guard()

    # Requirement 8: Check if job is already completed (duplicate delivery prevention)
    if guard.is_job_completed(job_id):
        t_logger.info("Job '%s' has already completed previously. Skipping execution.", job_id)
        return TaskStageResult(
            job_id=job_id,
            document_id=document_id,
            tenant_id=tenant_id,
            stage=TaskStage.PIPELINE,
            status=TaskExecutionStatus.SKIPPED,
            output_summary={"reason": "already_completed"},
        ).model_dump()

    # Requirement 8: Acquire job processing lock
    if not guard.acquire_job_lock(job_id):
        t_logger.warning("Job '%s' is currently locked by another worker. Skipping duplicate.", job_id)
        return TaskStageResult(
            job_id=job_id,
            document_id=document_id,
            tenant_id=tenant_id,
            stage=TaskStage.PIPELINE,
            status=TaskExecutionStatus.SKIPPED,
            output_summary={"reason": "concurrently_locked"},
        ).model_dump()

    try:
        t_logger.info("Executing pipeline for '%s' (storage_path='%s')", filename, storage_path)

        # 1. Extraction
        extract_res = extract_document_task(
            job_id=job_id,
            document_id=document_id,
            tenant_id=tenant_id,
            storage_path=storage_path,
            filename=filename,
            mime_type=mime_type,
            options=options,
        )
        if extract_res.get("status") != TaskExecutionStatus.SUCCESS.value:
            guard.release_job_lock(job_id)
            return extract_res

        elements_data = extract_res.get("output_summary", {}).get("elements", [])
        doc_type_extracted = extract_res.get("output_summary", {}).get("document_type")
        mime_type_extracted = extract_res.get("output_summary", {}).get("mime_type")

        # 2. Chunking
        chunk_res = chunk_document_task(
            job_id=job_id,
            document_id=document_id,
            tenant_id=tenant_id,
            elements_data=elements_data,
            document_type=doc_type_extracted,
            mime_type=mime_type_extracted or mime_type,
            options=options,
        )
        if chunk_res.get("status") != TaskExecutionStatus.SUCCESS.value:
            guard.release_job_lock(job_id)
            return chunk_res

        chunks_data = chunk_res.get("output_summary", {}).get("chunks", [])

        # 3. Embedding
        embed_res = embed_chunks_task(
            job_id=job_id,
            document_id=document_id,
            tenant_id=tenant_id,
            chunks_data=chunks_data,
            options=options,
        )
        if embed_res.get("status") != TaskExecutionStatus.SUCCESS.value:
            guard.release_job_lock(job_id)
            return embed_res

        embeddings_data = embed_res.get("output_summary", {}).get("embeddings", [])

        # 4. Vector Indexing
        index_res = index_vectors_task(
            job_id=job_id,
            document_id=document_id,
            tenant_id=tenant_id,
            chunks_data=chunks_data,
            embeddings_data=embeddings_data,
            options=options,
        )
        if index_res.get("status") != TaskExecutionStatus.SUCCESS.value:
            guard.release_job_lock(job_id)
            return index_res

        # Mark job completed and release lock
        guard.mark_job_completed(job_id)
        duration = time.time() - start_time
        t_logger.info("Pipeline succeeded: %d chunks indexed in %.2fs", len(chunks_data), duration)

        return TaskStageResult(
            job_id=job_id,
            document_id=document_id,
            tenant_id=tenant_id,
            stage=TaskStage.PIPELINE,
            status=TaskExecutionStatus.SUCCESS,
            items_count=len(chunks_data),
            duration_seconds=duration,
            retry_count=self.request.retries,
            output_summary={
                "elements_count": len(elements_data),
                "chunks_count": len(chunks_data),
                "vector_storage_ids": index_res.get("output_summary", {}).get("vector_storage_ids", []),
            },
        ).model_dump()

    except SoftTimeLimitExceeded as exc:
        guard.release_job_lock(job_id)
        t_logger.error("Pipeline task exceeded soft time limit: %s", exc)
        raise

    except Exception as exc:
        guard.release_job_lock(job_id)
        t_logger.error("Pipeline task error: %s", exc, exc_info=True)
        if self.request.retries < self.max_retries:
            delay = _compute_backoff(self.request.retries)
            raise self.retry(exc=exc, countdown=delay)

        return TaskStageResult(
            job_id=job_id,
            document_id=document_id,
            tenant_id=tenant_id,
            stage=TaskStage.PIPELINE,
            status=TaskExecutionStatus.FAILURE,
            duration_seconds=time.time() - start_time,
            retry_count=self.request.retries,
            error=str(exc),
        ).model_dump()

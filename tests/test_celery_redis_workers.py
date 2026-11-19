"""Unit and integration tests for Celery + Redis Asynchronous Workers (Stage 5 — Task 14).

Comprehensive test suite validating:
1. Celery worker application configuration (queues, late acks, prefetch, timeouts).
2. Asynchronous task boundaries (extraction, chunking, embedding, indexing, pipeline).
3. Payload references: passing lightweight identifiers instead of raw file bytes through broker.
4. Storage loading inside worker tasks (loading from LocalStorageBackend / S3).
5. Idempotent processing and duplicate delivery prevention (JobIdempotencyGuard).
6. Retry policies and exponential backoff configuration.
7. Structured task logging with contextual document and job coordinates.
8. Task failure reporting with actionable error diagnostics.
9. Tenant isolation and authorization boundaries across workers.
10. Safe worker shutdown and timeout handling (SoftTimeLimitExceeded).
"""

from __future__ import annotations

import asyncio
import io
import time
from unittest.mock import MagicMock, patch
import pytest
from celery.exceptions import SoftTimeLimitExceeded
from qdrant_client import QdrantClient

from backend.ingestion import (
    AccessControlPayload,
    ChunkLocator,
    DocumentChunkInput,
    EmbeddingResult,
    IngestionContext,
    IngestionJobPayload,
    JobIdempotencyGuard,
    LocalStorageBackend,
    QdrantConfig,
    QdrantVectorIndexService,
    StorageService,
    TaskExecutionStatus,
    TaskFailureReport,
    TaskLoggerAdapter,
    TaskStage,
    TaskStageResult,
    celery_app,
    chunk_document_task,
    create_celery_app,
    embed_chunks_task,
    extract_document_task,
    get_idempotency_guard,
    get_qdrant_index_service,
    get_storage_service,
    get_task_logger,
    index_vectors_task,
    process_document_pipeline_task,
)
from backend.ingestion.config import IngestionSettings


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def setup_test_environment(tmp_path):
    """Configure Celery to run in eager test mode with in-memory storage and Qdrant."""
    celery_app.conf.update(
        task_always_eager=True,
        task_eager_propagates=True,
    )
    guard = get_idempotency_guard(force_in_memory=True)
    guard._in_memory.clear()

    # Configure local storage backend in temporary directory
    storage_root = tmp_path / "storage"
    storage_root.mkdir(parents=True, exist_ok=True)
    backend = LocalStorageBackend(base_dir=str(storage_root))
    service = StorageService(backend=backend)
    
    # Configure in-memory Qdrant
    qdrant_client = QdrantClient(":memory:")
    q_service = QdrantVectorIndexService(
        config=QdrantConfig(collection_name="test_worker_chunks", vector_dimension=1024),
        client=qdrant_client,
    )

    with patch("backend.ingestion.tasks.document_tasks.get_storage_service", return_value=service), \
         patch("backend.ingestion.tasks.document_tasks.get_qdrant_index_service", return_value=q_service):
        yield {"storage": service, "qdrant": q_service, "tmp_path": tmp_path}


# ---------------------------------------------------------------------------
# Test Suite
# ---------------------------------------------------------------------------

class TestCeleryWorkerConfiguration:
    """Validates Requirements 1, 2, 6, 13: Celery + Redis initialization and settings."""

    def test_celery_app_configuration_parameters(self):
        settings = IngestionSettings(
            celery_broker_url="redis://localhost:6379/1",
            celery_result_backend="redis://localhost:6379/2",
            celery_task_default_queue="document_ingestion",
            celery_worker_concurrency=4,
            celery_task_time_limit=600,
            celery_task_soft_time_limit=540,
            celery_task_acks_late=True,
            celery_worker_prefetch_multiplier=1,
        )
        app = create_celery_app(settings)

        assert app.conf.task_default_queue == "document_ingestion"
        assert app.conf.task_serializer == "json"
        assert app.conf.result_serializer == "json"
        assert app.conf.accept_content == ["json"]
        assert app.conf.timezone == "UTC"
        assert app.conf.task_acks_late is True
        assert app.conf.task_reject_on_worker_lost is True
        assert app.conf.worker_concurrency == 4
        assert app.conf.worker_prefetch_multiplier == 1
        assert app.conf.task_time_limit == 600
        assert app.conf.task_soft_time_limit == 540

    def test_all_tasks_registered_on_celery_app(self):
        registered = celery_app.tasks.keys()
        assert "backend.ingestion.tasks.extract_document_task" in registered
        assert "backend.ingestion.tasks.chunk_document_task" in registered
        assert "backend.ingestion.tasks.embed_chunks_task" in registered
        assert "backend.ingestion.tasks.index_vectors_task" in registered
        assert "backend.ingestion.tasks.process_document_pipeline_task" in registered


class TestStructuredTaskLogging:
    """Validates Requirement 9: Structured worker logs identifying document and job."""

    def test_task_logger_formatting_with_context(self):
        logger_adapter = get_task_logger(
            name="test_worker_logger",
            job_id="job_9988",
            document_id="doc_7766",
            tenant_id="tenant_alpha",
            stage="EXTRACTION",
            retry_count=1,
        )
        msg, kwargs = logger_adapter.process("Processing file contents", {})
        assert "[job=job_9988 doc=doc_7766 tenant_id=tenant_alpha stage=EXTRACTION retry=1]" in msg
        assert "Processing file contents" in msg
        assert logger_adapter.extra["job_id"] == "job_9988"
        assert logger_adapter.extra["document_id"] == "doc_7766"


class TestIdempotencyAndDuplicatePrevention:
    """Validates Requirements 7 & 8: Prevent duplicate execution on redelivered jobs."""

    def test_acquire_and_release_job_lock(self):
        guard = get_idempotency_guard(force_in_memory=True)
        job_id = "job_test_lock_001"

        # First worker acquires lock successfully
        assert guard.acquire_job_lock(job_id) is True
        assert guard.is_job_in_progress(job_id) is True

        # Second worker attempting concurrent lock fails
        assert guard.acquire_job_lock(job_id) is False

        # Release lock
        assert guard.release_job_lock(job_id) is True
        assert guard.is_job_in_progress(job_id) is False

    def test_job_completion_marker_prevents_reexecution(self):
        guard = get_idempotency_guard(force_in_memory=True)
        job_id = "job_test_completed_002"

        guard.acquire_job_lock(job_id)
        assert guard.is_job_completed(job_id) is False

        # Mark finished
        guard.mark_job_completed(job_id)
        assert guard.is_job_completed(job_id) is True
        assert guard.is_job_in_progress(job_id) is False


class TestIndividualTaskBoundaries:
    """Validates Requirements 3, 4, 5, 11: Stage boundaries with lightweight identifier payloads."""

    def test_extract_document_task_loads_from_storage(self, setup_test_environment):
        storage: StorageService = setup_test_environment["storage"]
        tenant_id = "tenant_alpha"
        doc_id = "doc_test_extract"
        filename = "policy.csv"

        csv_content = b"department,headcount,budget\nR&D,45,1200000\nSales,30,850000\n"
        context = IngestionContext(tenant_id=tenant_id, document_id=doc_id)
        stored = asyncio.run(
            storage.save_file(
                content=csv_content,
                filename=filename,
                context=context,
            )
        )

        # Execute extraction task
        res = extract_document_task(
            job_id="job_ext_01",
            document_id=doc_id,
            tenant_id=tenant_id,
            storage_path=stored.storage_key,
            filename=filename,
            mime_type="text/csv",
        )

        assert res["status"] == TaskExecutionStatus.SUCCESS.value
        assert res["stage"] == TaskStage.EXTRACTION.value
        assert res["items_count"] > 0
        assert "elements" in res["output_summary"]

    def test_chunk_document_task_generates_chunks(self):
        tenant_id = "tenant_alpha"
        doc_id = "doc_test_chunk"

        elements_data = [
            {"element_type": "heading", "text": "Chương 1: Quy định chung", "level": 1, "page_number": 1},
            {"element_type": "paragraph", "text": "Hệ thống AgentGraph hỗ trợ xử lý tài liệu doanh nghiệp.", "page_number": 1},
        ]

        res = chunk_document_task(
            job_id="job_chk_01",
            document_id=doc_id,
            tenant_id=tenant_id,
            elements_data=elements_data,
        )

        assert res["status"] == TaskExecutionStatus.SUCCESS.value
        assert res["stage"] == TaskStage.CHUNKING.value
        assert res["items_count"] >= 1
        assert "chunks" in res["output_summary"]

    def test_embed_chunks_task_generates_dense_vectors(self):
        tenant_id = "tenant_alpha"
        doc_id = "doc_test_embed"

        chunks_data = [
            {
                "chunk_id": "chk_doc_0001",
                "tenant_id": tenant_id,
                "document_id": doc_id,
                "chunk_index": 0,
                "content": "Chính sách bảo mật dữ liệu doanh nghiệp cấp cao.",
                "content_hash": "a" * 64,
                "token_count": 15,
                "locator": {"page_number": 1, "char_start": 0, "char_end": 48},
                "acl": {"tenant_id": tenant_id, "roles": ["admin"], "departments": ["rnd"], "users": []},
            }
        ]

        res = embed_chunks_task(
            job_id="job_emb_01",
            document_id=doc_id,
            tenant_id=tenant_id,
            chunks_data=chunks_data,
        )

        assert res["status"] == TaskExecutionStatus.SUCCESS.value
        assert res["stage"] == TaskStage.EMBEDDING.value
        assert res["items_count"] == 1
        embeddings = res["output_summary"]["embeddings"]
        assert len(embeddings[0]["vector"]) == 1024

    def test_index_vectors_task_upserts_into_qdrant(self, setup_test_environment):
        qdrant: QdrantVectorIndexService = setup_test_environment["qdrant"]
        tenant_id = "tenant_alpha"
        doc_id = "doc_test_index"

        chunks_data = [
            {
                "chunk_id": "chk_doc_0001",
                "tenant_id": tenant_id,
                "document_id": doc_id,
                "chunk_index": 0,
                "content": "Quy trình đối soát tài chính định kỳ hàng tháng.",
                "content_hash": "b" * 64,
                "token_count": 12,
                "locator": {"page_number": 2, "char_start": 0, "char_end": 45},
                "acl": {"tenant_id": tenant_id, "roles": ["finance"], "departments": ["finance"], "users": []},
            }
        ]
        embeddings_data = [
            {
                "chunk_id": "chk_doc_0001",
                "vector": [0.01] * 1024,
                "model_name": "BAAI/bge-m3",
                "dimension": 1024,
                "token_count": 12,
            }
        ]

        res = index_vectors_task(
            job_id="job_idx_01",
            document_id=doc_id,
            tenant_id=tenant_id,
            chunks_data=chunks_data,
            embeddings_data=embeddings_data,
        )

        assert res["status"] == TaskExecutionStatus.SUCCESS.value
        assert res["stage"] == TaskStage.INDEXING.value
        assert res["items_count"] == 1
        assert len(res["output_summary"]["vector_storage_ids"]) == 1

        # Verify vector exists in Qdrant
        context = IngestionContext(tenant_id=tenant_id, document_id=doc_id)
        assert qdrant.has_vector("chk_doc_0001", context) is True


class TestEndToEndPipelineCoordinationAndIdempotence:
    """Validates Requirements 5, 7, 8, 11: Coordinated workflow with duplicate protection."""

    def test_process_document_pipeline_task_success(self, setup_test_environment):
        storage: StorageService = setup_test_environment["storage"]
        qdrant: QdrantVectorIndexService = setup_test_environment["qdrant"]
        tenant_id = "tenant_alpha"
        doc_id = "doc_e2e_01"
        job_id = "job_e2e_001"
        filename = "employee_handbook.csv"

        content = b"policy_id,policy_name,department\nP01,Work From Home,Engineering\nP02,Health Insurance,HR\n"
        context = IngestionContext(tenant_id=tenant_id, document_id=doc_id)
        stored = asyncio.run(
            storage.save_file(
                content=content,
                filename=filename,
                context=context,
            )
        )

        # Execute full pipeline task
        res = process_document_pipeline_task(
            job_id=job_id,
            document_id=doc_id,
            tenant_id=tenant_id,
            storage_path=stored.storage_key,
            filename=filename,
            mime_type="text/csv",
        )

        assert res["status"] == TaskExecutionStatus.SUCCESS.value
        assert res["stage"] == TaskStage.PIPELINE.value
        assert res["items_count"] > 0
        assert len(res["output_summary"]["vector_storage_ids"]) > 0

        # Verify vectors exist in Qdrant
        count = qdrant.count_vectors_for_document(doc_id, context)
        assert count > 0

        # Redelivery of same job ID must be skipped (Requirement 8)
        res_duplicate = process_document_pipeline_task(
            job_id=job_id,
            document_id=doc_id,
            tenant_id=tenant_id,
            storage_path=stored.storage_key,
            filename=filename,
            mime_type="text/csv",
        )
        assert res_duplicate["status"] == TaskExecutionStatus.SKIPPED.value
        assert res_duplicate["output_summary"]["reason"] == "already_completed"

        # Verify no duplicate vectors were created
        assert qdrant.count_vectors_for_document(doc_id, context) == count


class TestErrorHandlingRetriesAndFailures:
    """Validates Requirements 6, 10: Task failure reporting, retries, and timeouts."""

    def test_missing_storage_file_reports_non_retryable_failure(self):
        tenant_id = "tenant_alpha"
        doc_id = "doc_missing"
        res = extract_document_task(
            job_id="job_fail_01",
            document_id=doc_id,
            tenant_id=tenant_id,
            storage_path=f"tenants/{tenant_id}/{doc_id}/missing.pdf",
            filename="missing.pdf",
        )
        assert res["status"] == TaskExecutionStatus.FAILURE.value
        assert "not found" in res["error"].lower()

    def test_cross_tenant_access_in_worker_fails_fast(self):
        res = extract_document_task(
            job_id="job_cross_tenant_01",
            document_id="doc_other",
            tenant_id="tenant_alpha",
            storage_path="tenants/tenant_beta/doc_other/confidential.pdf",
            filename="confidential.pdf",
        )
        assert res["status"] == TaskExecutionStatus.FAILURE.value
        assert "tenant isolation violation" in res["error"].lower()

    def test_timeout_triggers_soft_time_limit_handling(self, setup_test_environment):
        storage: StorageService = setup_test_environment["storage"]
        tenant_id = "tenant_alpha"
        doc_id = "doc_timeout"
        filename = "path.csv"
        context = IngestionContext(tenant_id=tenant_id, document_id=doc_id)
        stored = asyncio.run(
            storage.save_file(
                content=b"sample,content\n1,2\n",
                filename=filename,
                context=context,
            )
        )

        with patch("backend.ingestion.tasks.document_tasks.default_extractor_registry.get_extractor_for_file", side_effect=SoftTimeLimitExceeded()):
            with pytest.raises(SoftTimeLimitExceeded):
                extract_document_task(
                    job_id="job_timeout",
                    document_id=doc_id,
                    tenant_id=tenant_id,
                    storage_path=stored.storage_key,
                    filename=filename,
                )

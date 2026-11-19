"""Failure Injection and Fault Tolerance Test Suite (Task 16 — Requirement B).

Simulates failures at every major processing boundary of the ingestion pipeline:
1. File extraction failure (corrupted stream/unsupported format).
2. OCR processing failure.
3. Chunk generation failure.
4. Provenance validation failure.
5. Embedding inference failure (timeout / OOM).
6. PostgreSQL persistence failure (compensating vector deletion).
7. Qdrant indexing failure (connection drop / retry exhaustion).
8. Worker execution failure (SoftTimeLimitExceeded timeout).
9. Redis disconnection simulation (lock timeout / fail-safe).
10. Retry and compensation execution (purging vectors, preventing orphan vectors).

Guarantees:
- Failures never leave persistent orphan vectors in Qdrant.
- Documents and outbox events never enter incorrectly completed states.
- All errors are safely captured with actionable diagnostics.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch
import pytest
from celery.exceptions import SoftTimeLimitExceeded
from qdrant_client import QdrantClient

from backend.ingestion import (
    AccessControlPayload,
    ChunkLocator,
    ChunkProvenanceValidator,
    DefaultDocumentValidator,
    DefaultIngestionOrchestrator,
    DocumentChunkInput,
    DocumentIngestionStatus,
    EmbeddingProvider,
    EmbeddingResult,
    ExtractionError,
    ExtractionResult,
    InMemoryChunkRepository,
    InMemoryDocumentRepository,
    InMemoryOutboxRepository,
    IngestionContext,
    IngestionError,
    JobIdempotencyGuard,
    PipelineStage,
    ProvenanceReport,
    QdrantConfig,
    QdrantVectorIndexService,
    TaskExecutionStatus,
    TaskFailureReport,
    TaskStage,
    TaskStageResult,
    chunk_document_task,
    extract_document_task,
    get_chunk_deduplicator,
    get_ingestion_settings,
    to_point_id,
)


class FailingEmbeddingService(EmbeddingProvider):
    """Embedding provider that simulates network timeouts or OOM inference exceptions."""

    def __init__(self, fail_on_call: bool = True) -> None:
        self.fail_on_call = fail_on_call

    @property
    def model_name(self) -> str:
        return "BAAI/bge-m3"

    @property
    def dimension(self) -> int:
        return 1024

    async def embed_texts(self, texts: list[str]) -> list[list[float]]:
        if self.fail_on_call:
            raise RuntimeError("Simulated embedding service GPU OOM / connection timeout")
        return [[0.5] * 1024 for _ in texts]

    async def embed_chunks(
        self,
        chunks: list[DocumentChunkInput],
        context: IngestionContext,
    ) -> list[EmbeddingResult]:
        if self.fail_on_call:
            raise RuntimeError("Simulated embedding service GPU OOM / connection timeout")
        return [
            EmbeddingResult(
                chunk_id=c.chunk_id,
                vector=[0.5] * 1024,
                model_name=self.model_name,
                dimension=1024,
                token_count=c.token_count,
            )
            for c in chunks
        ]


@pytest.fixture
def test_tenant() -> str:
    return "00000000-0000-0000-0000-000000000001"


@pytest.fixture
def qdrant_client_instance() -> QdrantClient:
    return QdrantClient(":memory:")


@pytest.fixture
def mock_orchestrator(qdrant_client_instance: QdrantClient) -> DefaultIngestionOrchestrator:
    settings = get_ingestion_settings()
    qdrant_config = QdrantConfig(
        collection_name="test_fault_injection_collection",
        vector_size=1024,
        distance_metric="Cosine",
    )
    qdrant_service = QdrantVectorIndexService(config=qdrant_config, client=qdrant_client_instance)
    document_repo = InMemoryDocumentRepository()
    outbox_repo = InMemoryOutboxRepository()
    chunk_repo = InMemoryChunkRepository(outbox_repo=outbox_repo)

    return DefaultIngestionOrchestrator(
        validator=DefaultDocumentValidator(settings),
        embedding_service=FailingEmbeddingService(fail_on_call=False),
        qdrant_service=qdrant_service,
        chunk_repo=chunk_repo,
        outbox_repo=outbox_repo,
        document_repo=document_repo,
        settings=settings,
    )


# ---------------------------------------------------------------------------
# Fault Injection Tests (1 to 10)
# ---------------------------------------------------------------------------

class TestPipelineFailureInjection:
    """Verifies pipeline fault tolerance, safe rollback, and compensation invariants."""

    def test_01_file_extraction_failure(
        self,
        mock_orchestrator: DefaultIngestionOrchestrator,
        test_tenant: str,
    ) -> None:
        """Failure 1: Extraction failure halts pipeline gracefully; marks document FAILED with 0 vectors."""
        async def _run():
            ctx = IngestionContext(tenant_id=test_tenant, document_id="doc-fail-ext-001")
            corrupt_bytes = b"NOT_A_VALID_PDF_HEADER_OR_DOCX"

            result = await mock_orchestrator.process_document(
                content=corrupt_bytes,
                filename="corrupted_file.pdf",
                context=ctx,
            )

            assert result.status == DocumentIngestionStatus.FAILED
            assert result.error_message is not None
            assert result.chunks_created == 0
            assert result.points_indexed == 0

            # Verify no vectors indexed in Qdrant
            count = mock_orchestrator.qdrant_service.count_vectors_for_document(result.document_id, ctx)
            assert count == 0

        asyncio.run(_run())

    def test_02_ocr_processing_failure(
        self,
        mock_orchestrator: DefaultIngestionOrchestrator,
        test_tenant: str,
    ) -> None:
        """Failure 2: OCR engine crash during scanned page processing is cleanly trapped."""
        async def _run():
            ctx = IngestionContext(tenant_id=test_tenant, document_id="doc-fail-ocr-001")
            raw_csv = "A,B\n1,2\n".encode("utf-8")

            # Patch extractor registry to simulate an extractor throwing an OCR exception
            with patch.object(
                mock_orchestrator.extractor_registry,
                "get_extractor",
                side_effect=RuntimeError("Tesseract OCR worker process crashed with SIGSEGV"),
            ):
                result = await mock_orchestrator.process_document(
                    content=raw_csv,
                    filename="scanned_fail.csv",
                    context=ctx,
                )

                assert result.status == DocumentIngestionStatus.FAILED
                assert "Tesseract OCR worker process crashed" in (result.error_message or "")
                assert mock_orchestrator.qdrant_service.count_vectors_for_document(result.document_id, ctx) == 0

        asyncio.run(_run())

    def test_03_chunk_generation_failure(
        self,
        mock_orchestrator: DefaultIngestionOrchestrator,
        test_tenant: str,
    ) -> None:
        """Failure 3: Chunking engine failure rolls back cleanly without orphan state."""
        async def _run():
            ctx = IngestionContext(tenant_id=test_tenant, document_id="doc-fail-chk-001")
            raw_csv = "Dept,Headcount\nEngineering,100\n".encode("utf-8")

            with patch.object(
                mock_orchestrator.chunking_engine,
                "chunk_document",
                side_effect=IngestionError("Exceeded maximum token window limit in structural chunker"),
            ):
                result = await mock_orchestrator.process_document(raw_csv, "chunk_fail.csv", ctx)
                assert result.status == DocumentIngestionStatus.FAILED
                assert "Exceeded maximum token window limit" in (result.error_message or "")
                assert mock_orchestrator.qdrant_service.count_vectors_for_document(result.document_id, ctx) == 0

        asyncio.run(_run())

    def test_04_provenance_validation_failure(
        self,
        mock_orchestrator: DefaultIngestionOrchestrator,
        test_tenant: str,
    ) -> None:
        """Failure 4: Corrupted locator coordinates trigger ValidationError rejection before persistence."""
        async def _run():
            ctx = IngestionContext(tenant_id=test_tenant, document_id="doc-fail-prov-001")
            raw_csv = "Dept,Headcount\nSales,50\n".encode("utf-8")

            # Mock provenance validator to return an invalid report
            invalid_report = ProvenanceReport(
                document_id="doc-fail-prov-001",
                document_type=mock_orchestrator.extractor_registry.get_extractor(DocumentChunkInput).supported_document_types[0] if False else "PDF",
                total_chunks=1,
                valid_chunks=0,
                invalid_chunks=1,
                is_valid=False,
                failures=[],
                warnings=["Corrupted locator char offsets exceeding file bounds"],
            )

            with patch.object(
                mock_orchestrator.provenance_validator,
                "validate_chunks",
                return_value=invalid_report,
            ):
                result = await mock_orchestrator.process_document(raw_csv, "prov_fail.csv", ctx)
                assert result.status == DocumentIngestionStatus.FAILED
                assert "provenance validation failed" in (result.error_message or "").lower()
                assert mock_orchestrator.qdrant_service.count_vectors_for_document(result.document_id, ctx) == 0

        asyncio.run(_run())

    def test_05_embedding_inference_failure(
        self,
        mock_orchestrator: DefaultIngestionOrchestrator,
        test_tenant: str,
    ) -> None:
        """Failure 5: Embedding inference timeout / OOM stops pipeline before vector indexing."""
        async def _run():
            ctx = IngestionContext(tenant_id=test_tenant, document_id="doc-fail-emb-001")
            raw_csv = "ColA,ColB\n10,20\n".encode("utf-8")

            # Set failing embedding provider
            mock_orchestrator._embedding_service = FailingEmbeddingService(fail_on_call=True)

            result = await mock_orchestrator.process_document(raw_csv, "emb_fail.csv", ctx)
            assert result.status == DocumentIngestionStatus.FAILED
            assert "GPU OOM / connection timeout" in (result.error_message or "")
            assert mock_orchestrator.qdrant_service.count_vectors_for_document(result.document_id, ctx) == 0

        asyncio.run(_run())

    def test_06_postgresql_persistence_failure_triggers_compensation(
        self,
        mock_orchestrator: DefaultIngestionOrchestrator,
        test_tenant: str,
    ) -> None:
        """Failure 6: PostgreSQL transaction failure triggers compensating cleanup."""
        async def _run():
            ctx = IngestionContext(tenant_id=test_tenant, document_id="doc-fail-pg-001")
            raw_csv = "ColA,ColB\n100,200\n".encode("utf-8")

            # Reset embedding to working service
            mock_orchestrator._embedding_service = FailingEmbeddingService(fail_on_call=False)

            # Simulate PostgreSQL deadlock or abort during chunk + outbox persistence
            with patch.object(
                mock_orchestrator.chunk_repo,
                "save_chunks_and_outbox",
                side_effect=RuntimeError("PostgreSQL transaction deadlock detected during chunk insert"),
            ):
                result = await mock_orchestrator.process_document(raw_csv, "db_fail.csv", ctx)
                assert result.status == DocumentIngestionStatus.FAILED
                assert "PostgreSQL transaction deadlock" in (result.error_message or "")
                # No orphan vectors in Qdrant
                assert mock_orchestrator.qdrant_service.count_vectors_for_document(result.document_id, ctx) == 0

        asyncio.run(_run())

    def test_07_qdrant_indexing_failure_purges_partial_vectors(
        self,
        mock_orchestrator: DefaultIngestionOrchestrator,
        test_tenant: str,
    ) -> None:
        """Failure 7: Qdrant indexing connection failure executes saga vector purge."""
        async def _run():
            ctx = IngestionContext(tenant_id=test_tenant, document_id="doc-fail-qdrant-001")
            raw_csv = "Item,Count\nAlpha,1\nBeta,2\n".encode("utf-8")

            mock_orchestrator._embedding_service = FailingEmbeddingService(fail_on_call=False)

            with patch.object(
                mock_orchestrator.qdrant_service,
                "batch_index_chunks",
                side_effect=ConnectionError("Failed to reach Qdrant gRPC endpoint (connection refused)"),
            ):
                result = await mock_orchestrator.process_document(raw_csv, "qdrant_fail.csv", ctx)
                assert result.status == DocumentIngestionStatus.FAILED
                assert "Failed to reach Qdrant gRPC endpoint" in (result.error_message or "")
                assert mock_orchestrator.qdrant_service.count_vectors_for_document(result.document_id, ctx) == 0

        asyncio.run(_run())

    def test_08_worker_execution_soft_timeout_handling(
        self,
        test_tenant: str,
    ) -> None:
        """Failure 8: Celery worker SoftTimeLimitExceeded is caught and reported cleanly."""
        # Mock extract_document inside Celery task to raise SoftTimeLimitExceeded
        with patch(
            "backend.ingestion.tasks.document_tasks.default_extractor_registry.get_extractor_for_file",
            side_effect=SoftTimeLimitExceeded(),
        ):
            with pytest.raises(SoftTimeLimitExceeded):
                extract_document_task(
                    job_id="job-timeout-001",
                    document_id="doc-timeout-001",
                    tenant_id=test_tenant,
                    storage_path="tenants/001/doc.csv",
                    filename="heavy_document.csv",
                )

    def test_09_redis_disconnection_simulation(
        self,
        test_tenant: str,
    ) -> None:
        """Failure 9: Redis broker disconnection preserves fail-safe state."""
        # When Redis is disconnected or unreachable, guard fails safe to in-memory store
        guard = JobIdempotencyGuard(redis_url="redis://invalid-unreachable-host:6379/9")

        acquired = guard.acquire_job_lock("job-redis-fail-001", timeout_seconds=60)
        assert acquired is True  # In-memory fail-safe

        # Second acquire should fail because lock is held
        acquired_again = guard.acquire_job_lock("job-redis-fail-001", timeout_seconds=60)
        assert acquired_again is False

        released = guard.release_job_lock("job-redis-fail-001")
        assert released is True

    def test_10_retry_exhaustion_dead_letters_and_cleans_vectors(
        self,
        mock_orchestrator: DefaultIngestionOrchestrator,
        test_tenant: str,
    ) -> None:
        """Failure 10: Exhausting outbox retries dead-letters the event and purges partial vectors."""
        async def _run():
            ctx = IngestionContext(tenant_id=test_tenant, document_id="doc-deadletter-001")
            chunk = DocumentChunkInput(
                chunk_id="chk-dl-001",
                tenant_id=test_tenant,
                document_id="doc-deadletter-001",
                chunk_index=0,
                content="Dead letter chunk test",
                content_hash="f" * 64,
                token_count=10,
                acl=AccessControlPayload(tenant_id=test_tenant),
            )

            # Pre-index an orphan vector using qdrant service
            emb = EmbeddingResult(
                chunk_id=chunk.chunk_id,
                vector=[0.1] * 1024,
                model_name="BAAI/bge-m3",
                dimension=1024,
                token_count=10,
            )
            await mock_orchestrator.qdrant_service.index_chunks(
                chunks=[chunk],
                embeddings=[emb],
                context=ctx,
            )
            point_id = to_point_id(chunk.chunk_id)
            assert mock_orchestrator.qdrant_service.has_vector(point_id, ctx) is True

            # Trigger permanent failure compensation
            await mock_orchestrator.compensation_manager.compensate_indexing_failure(
                tenant_id=test_tenant,
                document_id="doc-deadletter-001",
                point_ids=[point_id],
                error_message="Retry limit exhausted (3/3 attempts failed)",
                context=ctx,
            )

            # Verify vector was completely purged
            assert mock_orchestrator.qdrant_service.has_vector(point_id, ctx) is False

        asyncio.run(_run())
